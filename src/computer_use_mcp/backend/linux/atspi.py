"""
AT-SPI 读取层（Linux backend 的「感知 + 元素级操作」核心）。

复用 demo/probe_atspi.py、probe_element.py、action_vs_coord.py 已验证逻辑：
  - role_name / get_actions / get_extents 直接沿用 demo 写法；
  - INTERACTIVE_ROLES 复用 probe_element.py；
  - do_action 选 click/activate/press 优先，复用 action_vs_coord.py 路径A；
  - 所有 gi 调用包 try/except（demo 证明 AT-SPI 调用易抛异常）。

本模块只负责「读 AT-SPI + 元素级 do_action/set_value」，
坐标注入（xdotool）在 inject.py，几何校准在 core/geometry.py。
"""

from __future__ import annotations

import os
import threading
import warnings
from typing import Any, Iterator

# gi 的 Atspi 绑定把部分仍可用的旧 API 标了 DeprecationWarning（如 get_action_name/get_text），
# 功能正常、暂无等价新 API，针对性静音以免污染输出（仅忽略这两条精确消息）。
warnings.filterwarnings("ignore", message=r".*get_action_name is deprecated.*")
warnings.filterwarnings("ignore", message=r".*get_text is deprecated.*")

from ..._bootstrap import import_atspi
from ...utils.logging import get_logger
from ..base import Element, ElementDetail, QueryResult, Rect, UINode

log = get_logger(__name__)

# 可交互角色（有操作意义的），过滤纯装饰 panel/label —— 复用 demo/probe_element.py
INTERACTIVE_ROLES = {
    "push button", "toggle button", "menu item", "menu", "check box",
    "radio button", "text", "entry", "combo box", "list item", "list",
    "table cell", "slider", "scroll bar", "spin button", "hyperlink",
    "tab", "tree item", "page tab", "tool button", "split pane",
    "table", "tree", "tree table", "filler", "label", "panel",
}

# 真正「可操作」的角色子集（用于判断节点是否值得分配 ref / interactive_only 过滤）
ACTIONABLE_ROLES = {
    "push button", "toggle button", "menu item", "check box", "radio button",
    "entry", "combo box", "list item", "slider", "scroll bar", "spin button",
    "hyperlink", "tab", "page tab", "tool button", "tree item", "table cell",
}

# 纯噪声角色（无名字时直接丢弃，省 token）
NOISE_ROLES = {"filler", "panel", "unknown", "section", "scroll pane"}

# do_action 时优先选择的动作名（按优先级）—— 复用 demo/action_vs_coord.py
_PREFERRED_ACTIONS = ("click", "press", "activate", "open", "select", "jump")

# ── 总线环境变量的写锁（M-24，见 docs/REVIEW/review_0.1.0.md）────────────────────
# `_atspi()` 与 `sync_bus()` 都在写**进程级** `os.environ["AT_SPI_BUS_ADDRESS"]`
# （libatspi 只在 atspi_init 那一刻读它，没有别处可传，故只能写在这里）。
# 而调用方 `get_ui_tree` / `find_elements` **只挂 @_needs_display、不加屏锁**，可以并发，
# 于是「判 want → 写 env → import」这三步会互相穿插：A 刚写入私有总线、还没 import，
# B 抢进来在沙箱恰好未就绪的瞬间把变量**删掉**，随后 atspi_init 就落到了**宿主总线**，
# 而 `_bound_address` 还被记成 None，之后 sync_bus 判「已对齐」直接 no-op —— 错误不被发现。
# 用 RLock（而非 Lock）：`_atspi()` 会被 `is_available()` / 遍历等多处间接重入。
_BUS_LOCK = threading.RLock()

# ── 子节点读取失败的**限流**日志（M-25，见 docs/REVIEW/review_0.1.0.md）──────────
# 为什么需要：child_count / child_at 吞异常本身是对的（不让异常逃到上层），但历史实现
# **不留任何痕迹** —— 于是 a11y 半失效（应用被 kill / 总线失联）时，表现为「拿到一棵
# 空树」而不是报错，排查成本极高（本项目已在别处两次栽在这类静默降级上）。
# 为什么必须限流：这两个方法在遍历里**每节点调用一次**，真断链时一次 get_ui_tree 可能
# 抛出成千上万次 —— 逐条记会把日志本身变成新的噪声源，反倒盖住「到底哪里断了」。
# 故：前 _CHILD_READ_FAIL_LOG_LIMIT 次逐条记，之后每 100 次给一条汇总。
_CHILD_READ_FAIL_LOG_LIMIT = 20
_child_read_fail_counts: dict[str, int] = {}
_child_read_fail_lock = threading.Lock()


def _log_child_read_failure(func: str, exc: Exception) -> None:
    """子节点读取失败的限流日志（实现说明见上方常量注释）。"""
    with _child_read_fail_lock:
        n = _child_read_fail_counts.get(func, 0) + 1
        _child_read_fail_counts[func] = n
    if n <= _CHILD_READ_FAIL_LOG_LIMIT:
        log.debug("%s 读取失败（第 %d 次），已按空值处理: %s", func, n, exc)
    elif n % 100 == 0:
        log.debug("%s 读取失败已累计 %d 次（此后每 100 次汇总一条）: %s", func, n, exc)

# 总线在进程内切不动时的报错文案（经 `error` 属性暴露给工具层，最终呈现给用户）。
# 要同时说清「发生了什么」与「怎么办」—— 不要抛底层错误：实测那种场景下 libatspi 给的是
# `atspi_error: The application no longer exists`，指向完全不明，排查成本极高。
_BUS_STALE_ERROR = (
    "沙箱 AT-SPI 总线已变化（沙箱重建过），libatspi 无法在进程内切换总线；"
    "重启 Claude Code 可恢复 a11y 能力"
)


class AtspiReader:
    """封装 AT-SPI 读取与元素级操作。所有方法对 gi 异常做防御。"""

    def __init__(self) -> None:
        self._Atspi: Any = None
        self._available: bool | None = None
        self._error: str | None = None
        # 当前 AT-SPI 连接所用的总线地址（None = 宿主会话总线）。用于**检测**沙箱私有总线
        # 的变化。注意「变化」不等于「能切」——libatspi 的 init 是一次性的，且跨总线
        # exit+init 实测会损坏其内部状态，故检测到变化只能标记不可用（见 sync_bus）。
        self._bound_address: str | None = None
        # 总线变了但进程内切不动 → True。仅用于给那条 WARNING 去重：sync_bus 每次工具调用
        # 都会走到，不去重会把日志刷满。只能靠重启进程复位。
        self._bus_stale: bool = False

    # ---------- 总线绑定（a11y 隔离）----------
    def _desired_bus(self) -> str | None:
        """
        当前**应当**连接的 AT-SPI 总线地址。

        isolated 且沙箱就绪 → 沙箱私有总线（宿主 a11y 完全看不到沙箱内应用）；
        其余（real 模式 / 沙箱未起）→ None，即宿主会话总线（旧行为）。
        """
        from ...core import display  # 延迟导入：core 层不反向依赖 backend，此处方向合法
        if display.MANAGER.mode != display.MODE_ISOLATED:
            return None
        if not display.MANAGER.is_sandbox_up():
            return None
        return display.MANAGER.sandbox_at_spi_bus()

    def _apply_bus(self, address: str | None) -> None:
        """把总线地址写进进程环境（libatspi 在 init 时读它）。"""
        if address:
            os.environ["AT_SPI_BUS_ADDRESS"] = address
        else:
            os.environ.pop("AT_SPI_BUS_ADDRESS", None)
        self._bound_address = address

    def sync_bus(self) -> bool:
        """
        把 AT-SPI 连接对齐到当前应连的总线；**发生了首次绑定**才返回 True。

        由 coordinator 在每次沙箱 ensure_started 之后调用（廉价：两次字符串比较）。
        三条分支：
          ① 已对齐 → no-op，返回 False；
          ② **首次绑定**（还没 import 过 Atspi）→ 只写环境变量，下次 `_atspi()` 时按它
             init。这是正常路径，也是唯一能真正生效的时机；
          ③ **已绑定但总线变了**（沙箱重建 / 沙箱消失）→ 切不动，标记 stale + 明确报错。

        为什么第 ③ 条不再走「exit + 重新 init」（那曾是这里的设计）：
        实测证明它救不回来，反而把 reader 弄成**永久失联** —— 跨总线
        `Atspi.exit()` + `Atspi.init()` 会损坏 libatspi 内部状态（`g_hash_table_insert_internal`
        与 `g_object_unref` 断言失败），之后任何访问都报 APPLICATION_GONE；而 exit 之后
        本项目没有任何地方重新 init。故改为保持原状 + 给出可操作的错误，让用户知道
        「重启 Claude Code 即可恢复」，而不是面对一个指向不明的底层错误。

        整段持 `_BUS_LOCK`（M-24）：本函数与 `_atspi()` 都在改写**进程级** `os.environ`，
        而两者可被并发调用（`get_ui_tree`/`find_elements` 不加屏锁）。详见 `_atspi()`。
        """
        with _BUS_LOCK:
            return self._sync_bus_locked()

    def _sync_bus_locked(self) -> bool:
        """`sync_bus()` 的实现体（调用方已持 `_BUS_LOCK`）。"""
        want = self._desired_bus()
        if want == self._bound_address and self._Atspi is not None:
            return False
        # 第 ③ 条：连接已建立且总线要变 —— 进程内切不动（见 docstring）
        if self._Atspi is not None and want != self._bound_address:
            if not self._bus_stale:
                log.warning(
                    "AT-SPI 总线已变化（%s → %s），libatspi 无法在进程内换总线；"
                    "本次会话的 a11y 能力不可用，重启 Claude Code 可恢复",
                    self._bound_address or "(宿主会话总线)",
                    want or "(宿主会话总线)",
                )
                self._bus_stale = True
            self._available = False
            self._error = _BUS_STALE_ERROR
            return False
        # 第 ② 条：首次绑定（或想连的总线恰好不变）
        if want == self._bound_address:
            return False
        self._apply_bus(want)
        log.info("AT-SPI 连接切到总线: %s", want or "(宿主会话总线)")
        return True

    # ---------- 初始化 / 可用性 ----------
    def _atspi(self) -> Any:
        """
        懒加载 Atspi 模块（首次调用触发环境变量配置 + import）。

        整段持 `_BUS_LOCK`（M-24，见 docs/REVIEW/review_0.1.0.md）：这里「判 want → 写
        os.environ → import」是**三步非原子**操作，而环境变量是**进程级**的、`sync_bus()`
        也在写它。并发子 agent 共用同一个 server 进程（`get_ui_tree`/`find_elements` 只挂
        `@_needs_display`、**不加屏锁**，可并发），于是存在这个构型：线程 A 已 `_apply_bus(私有)`
        但尚未 import，线程 B 抢进来、在沙箱**恰好重建/未就绪**的瞬间算出 `want=None`，
        执行 `_apply_bus(None)` 把变量**删掉** → 随后 libatspi 的 `atspi_init()` 落到**宿主
        总线**，而 `_bound_address` 还被写成 None，`sync_bus` 之后判「已对齐」直接 no-op
        —— **错误不会被发现**。这正是本项目最怕的那类静默失效。
        """
        with _BUS_LOCK:
            if self._Atspi is None:
                # 首次使用前对齐总线：沙箱可能刚起来，而 libatspi 只认 init 那一刻的地址
                want = self._desired_bus()
                if want != self._bound_address:
                    self._apply_bus(want)
                self._Atspi = import_atspi()
            return self._Atspi

    def is_available(self) -> bool:
        """AT-SPI 是否可用（能 import 且能拿到 desktop）。结果缓存。"""
        if self._available is not None:
            return self._available
        try:
            atspi = self._atspi()
            desktop = atspi.get_desktop(0)
            # 读一次 child_count 确认 dbus 通路正常
            desktop.get_child_count()
            self._available = True
        except Exception as exc:  # noqa: BLE001
            self._available = False
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("AT-SPI 不可用: %s", self._error)
        return self._available

    @property
    def error(self) -> str | None:
        return self._error

    # ---------- 基础属性读取（防御式）----------
    def role_name(self, obj: Any) -> str:
        """元素角色名（小写），失败返回 'unknown'。复用 demo role_name。"""
        try:
            atspi = self._atspi()
            return atspi.Role.get_name(obj.get_role()).lower()
        except Exception:  # noqa: BLE001
            return "unknown"

    def get_name(self, obj: Any) -> str:
        try:
            return obj.get_name() or ""
        except Exception:  # noqa: BLE001
            return ""

    def get_actions(self, obj: Any) -> list[str]:
        """元素可执行动作名列表。复用 demo get_actions。"""
        actions: list[str] = []
        try:
            n = obj.get_n_actions()
            for i in range(n):
                try:
                    actions.append(obj.get_action_name(i))
                except Exception:  # noqa: BLE001
                    actions.append("?")
        except Exception:  # noqa: BLE001
            pass
        return actions

    def get_states(self, obj: Any) -> list[str]:
        """元素状态名列表（focused/checked/enabled 等）。"""
        states: list[str] = []
        try:
            atspi = self._atspi()
            ss = obj.get_state_set()
            # StateType 枚举遍历不可靠，改用常见状态白名单逐个 contains 检测
            for st_name in ("FOCUSED", "CHECKED", "SELECTED", "ENABLED", "VISIBLE",
                            "ACTIVE", "PRESSED", "EXPANDED", "EDITABLE"):
                st = getattr(atspi.StateType, st_name, None)
                if st is not None:
                    try:
                        if ss.contains(st):
                            states.append(st_name.lower())
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass
        return states

    def get_extents(self, obj: Any, coord: str = "SCREEN") -> Rect | None:
        """
        读元素矩形。coord='SCREEN'（屏幕绝对，可能漂移）或 'WINDOW'（窗口相对，实测可信）。
        复用 demo get_bbox / extents。
        """
        try:
            atspi = self._atspi()
            ctype = atspi.CoordType.SCREEN if coord == "SCREEN" else atspi.CoordType.WINDOW
            e = obj.get_extents(ctype)
            return Rect(x=int(e.x), y=int(e.y), w=int(e.width), h=int(e.height))
        except Exception:  # noqa: BLE001
            return None

    def get_value(self, obj: Any) -> Any:
        """读取元素当前值（文本框内容/滑块值等），尽力而为。"""
        # 1. 文本类：EditableText / Text 接口
        for meth in ("get_text_contents", "get_text"):
            try:
                fn = getattr(obj, meth, None)
                if callable(fn):
                    if meth == "get_text_contents":
                        return fn(0, -1)
                    return fn()
            except Exception:  # noqa: BLE001
                continue
        # 2. 数值类：Value 接口
        for meth in ("get_current_value",):
            try:
                fn = getattr(obj, meth, None)
                if callable(fn):
                    return fn()
            except Exception:  # noqa: BLE001
                continue
        return None

    def get_process_id(self, obj: Any) -> int | None:
        """读元素所属进程 PID（用于按 PID 关联窗口）。"""
        for meth in ("get_process_id",):
            try:
                fn = getattr(obj, meth, None)
                if callable(fn):
                    pid = fn()
                    return int(pid) if pid else None
            except Exception:  # noqa: BLE001
                continue
        return None

    def child_count(self, obj: Any) -> int:
        try:
            return int(obj.get_child_count())
        except Exception as exc:  # noqa: BLE001
            _log_child_read_failure("child_count", exc)
            return 0

    def child_at(self, obj: Any, i: int) -> Any | None:
        try:
            return obj.get_child_at_index(i)
        except Exception as exc:  # noqa: BLE001
            _log_child_read_failure("child_at", exc)
            return None

    def parent(self, obj: Any) -> Any | None:
        try:
            return obj.get_parent()
        except Exception:  # noqa: BLE001
            return None

    # ---------- 应用 / 窗口枚举 ----------
    def desktop(self) -> Any | None:
        try:
            return self._atspi().get_desktop(0)
        except Exception:  # noqa: BLE001
            return None

    def iter_apps(self) -> Iterator[Any]:
        """遍历桌面上的所有应用。"""
        d = self.desktop()
        if d is None:
            return
        for i in range(self.child_count(d)):
            app = self.child_at(d, i)
            if app is not None:
                yield app

    # 注意：这里**刻意不提供 list_apps()**。
    # 历史上它长这样：
    #     for app in self.iter_apps():
    #         if self.child_count(app) == 0:   # ← 致命
    #             continue
    #         names.append(self.get_name(app) or "(无名字)")
    # 其中 child_count(app) 会逼每个应用**惰性构建整棵无障碍树**，是拖崩 GNOME Shell
    # 的直接原因（详见 inject.list_app_names 的长注释与实测事故记录）。
    # 「有哪些应用有窗口」已改由纯 X11 的 backend.list_apps 提供。
    # 此处不再保留 AT-SPI 版实现，以免后人「顺手复用」把风险带回来；
    # 若将来确实需要应用清单，请走 X11 通道，不要遍历 application 节点的 child。

    def find_app(self, keyword: str) -> Any | None:
        """
        按名字关键词（子串，大小写不敏感）找应用。复用 demo find_app。

        安全性：只调 get_name（读 application 节点的名字属性），**不调 child_count**，
        因此不会触发目标应用构建树。代价是 N 次 D-Bus 属性读取，量级可接受。
        """
        kw = keyword.lower()
        for app in self.iter_apps():
            if kw in self.get_name(app).lower():
                return app
        return None

    def top_window_of(self, obj: Any) -> Any | None:
        """从一个元素向上回溯到其所属顶层窗口（role 含 'window'/'frame'/'dialog'）。"""
        cur = obj
        seen = 0
        last = obj
        while cur is not None and seen < 32:
            role = self.role_name(cur)
            last = cur
            if role in ("window", "frame", "dialog", "alert", "file chooser", "layered pane"):
                return cur
            parent = self.parent(cur)
            if parent is None:
                break
            # 到达 application 层停止
            if self.role_name(parent) in ("application", "desktop frame"):
                break
            cur = parent
            seen += 1
        return last

    # 单次操作最多对多少个应用调 child_count（即"触碰"几个应用）。
    # 为何需要这个预算：child_count(app) 会逼该应用构建 a11y 树，对 N 个应用连调
    # 就是 N 次重型构建，累积压力足以打满会话 D-Bus 并拖崩 GNOME Shell
    # （见 inject.list_app_names 的长注释与实测事故）。最优解是按 pid 只碰目标
    # 那一个应用；退化路径必须有上限兜底，不能无上限扫全桌面。
    _APP_TOUCH_BUDGET = 8

    def get_active_window(
        self, active_title: str | None = None, active_pid: int | None = None,
        window_pids_provider: Any = None,
    ) -> Any | None:
        """
        获取活动窗口对应的 AT-SPI 节点。

        实现逻辑（分层，核心目标是**尽量少触碰应用**）：
          1. **PID 优先**（推荐路径）：调用方从 xdotool getactivewindow getwindowpid
             取到 active_pid 传入。这里遍历 application 层时**只读 pid 属性**
             （get_process_id，轻量、不触发建树），命中即锁定该应用，
             再只遍历**它一个**的窗口找标题——把「N 个应用各调一次 child_count」
             降到「1 个」。
          2. **标题退化**：无 pid 或 pid 未命中时，逐个应用找标题匹配的窗口，
             但受 _APP_TOUCH_BUDGET 约束：超预算即停止扫描并告警，
             不再无上限遍历整个桌面。**先用 `window_pids_provider` 给的
             「有窗口的 pid 集合」跳过无窗口应用**（M-23），免得预算被
             gnome-shell/输入法这类 child_count==0 的应用吃光。
          3. fallback：确实没有标题命中时，返回首个「有子窗口」的窗口，
             让调用方至少能拿到东西（与原行为一致）。

        空标题不得参与子串匹配：'' in 任意字符串 恒为 True，会让 gnome-shell 等
        排在前面应用的无名窗口抢先命中（实测 bug：找「另存为」对话框却返回概览界面）。
        空名窗口只可作 fallback。
        """
        # 1) PID 优先：遍历应用只读 pid（不碰 child_count），命中即在该应用内解决
        if active_pid:
            for app in self.iter_apps():
                if self.get_process_id(app) == active_pid:
                    hit, fb = self._scan_app_windows(app, active_title)
                    return hit if hit is not None else fb

        # 2) 标题退化：受应用触碰预算约束，绝不无上限扫全桌面
        fallback = None
        touched = 0
        win_pids = self._resolve_window_pids(window_pids_provider)
        skipped = 0
        for app in self.iter_apps():
            if touched >= self._APP_TOUCH_BUDGET:
                log.warning(
                    "get_active_window 触及应用数达预算(%d)仍未匹配标题 %r，停止扫描"
                    "（已跳过 %d 个无窗口应用）；定位不准时请让调用方传入 active_pid "
                    "走精确定位路径",
                    self._APP_TOUCH_BUDGET, active_title, skipped)
                break
            # M-23（见 docs/REVIEW/review_0.1.0.md）：先用 **X11 侧**已知的「有窗口的 pid」
            # 集合滤掉无窗口应用 —— get_process_id 是纯属性读，不触发建树，**零 a11y 成本**。
            #
            # 历史实现无条件 `touched += 1`，于是 gnome-shell / 输入法 / 注册器这类
            # child_count == 0 的应用会把 8 个预算吃光，目标应用还没轮到就 break
            # （只留一条日志，表现为「找不到活动窗口」）。
            #
            # ⚠️ 刻意**不**按报告原建议「只对 child_count(app) > 0 的应用计数」：
            # 那要先调 child_count 才知道有没有子节点 —— 危险调用一次没少、反而翻倍
            # （判空一次 + _scan_app_windows 里再调一次），等于把唯一那道遍历爆炸防线放宽。
            if win_pids and self.get_process_id(app) not in win_pids:
                skipped += 1
                continue
            touched += 1
            hit, fb = self._scan_app_windows(app, active_title)
            if hit is not None:
                return hit
            if fallback is None and fb is not None:
                fallback = fb
        return fallback

    @staticmethod
    def _resolve_window_pids(provider: Any) -> set[int]:
        """
        调 `window_pids_provider` 取「当前有窗口的应用 pid 集合」（M-23）。

        为什么是**懒加载回调**而不是现成集合：这个集合来自 X11（`wmctrl -lpx`，约 5~10ms），
        而 `get_active_window` 是**最常走的路径**，其中 pid 精确定位那条（路径 1）根本用不到它。
        传回调可保证「用不到就不付钱」；取不到时返回空集，退化为历史行为（不跳过任何应用）。
        """
        if provider is None:
            return set()
        try:
            return set(provider() or ())
        except Exception as exc:  # noqa: BLE001 —— 滤不掉只是少一层优化，绝不能因此让主流程失败
            log.debug("取有窗口应用 pid 集合失败（本次不做预滤）: %s", exc)
            return set()

    def _scan_app_windows(
        self, app: Any, active_title: str | None,
    ) -> tuple[Any | None, Any | None]:
        """
        遍历**单个**应用的窗口，返回 (标题命中的窗口, 首个有子节点的窗口作 fallback)。

        只对一个应用调 child_count —— 这是刻意压到最小的代价（见 _APP_TOUCH_BUDGET）。
        """
        fallback = None
        for w in range(self.child_count(app)):
            win = self.child_at(app, w)
            if win is None:
                continue
            title = self.get_name(win)
            if title and active_title and (
                title == active_title or active_title in title or title in active_title
            ):
                return win, fallback
            if fallback is None and self.child_count(win) > 0:
                fallback = win
        return None, fallback

    # ---------- 树遍历 → UINode ----------
    def build_tree(
        self, root: Any, app_name: str = "", max_depth: int = 8, max_nodes: int = 300,
        include_rect: bool = False, counter: dict[str, Any] | None = None,
    ) -> UINode:
        """
        递归把 AT-SPI 子树转成 UINode 树（带深度/节点数上限，复用 demo walk）。

        include_rect=True 时读 SCREEN 矩形（较慢，仅 element_info/find 用）。

        counter：**可选的外部共享计数器**。不传则本次调用自建（单棵树语义）；传了就跨
        多棵树共享——`backend.get_tree` 正是靠它把「每个窗口各算各的」收敛成「整次调用
        共用 max_nodes」。历史实现每棵树各建一个计数器，于是 max_nodes 实际是「**每窗口**」
        语义：desktop scope 下上限变成 `8 应用 × 全部窗口 × 400`，与工具描述承诺的
        「本次结果上限」不符，且无总量上限（I-4）。

        计数器里的 `truncated` 会被置 True 表示**确有节点被丢弃**——刻意区分于「刚好装满
        max_nodes」（后者不是截断，不该误报）。
        """
        if counter is None:
            counter = {"n": 0, "truncated": False}
        counter.setdefault("truncated", False)
        return self._build(root, app_name, 0, max_depth, max_nodes, counter, include_rect)

    def _build(
        self, obj: Any, app_name: str, depth: int, max_depth: int,
        max_nodes: int, counter: dict[str, Any], include_rect: bool,
    ) -> UINode:
        counter["n"] += 1
        node = UINode(
            role=self.role_name(obj),
            name=self.get_name(obj),
            actions=self.get_actions(obj),
            depth=depth,
            app=app_name,
            native=obj,
        )
        if include_rect:
            node.rect = self.get_extents(obj, "SCREEN")
        # child_count 先算出来复用（原先在 range() 里也是每次展开调一次，非新增开销）。
        # 截断判定需要它：只有「确实还有子节点没展开」才算截断，否则「刚好装到上限」
        # 会被误报成截断。代价是**达到深度上限的节点**多一次 child_count——数量受
        # max_nodes 总量约束，可接受。
        n_children = self.child_count(obj)
        if depth >= max_depth or counter["n"] >= max_nodes:
            if n_children > 0:
                counter["truncated"] = True
            return node
        for i in range(n_children):
            if counter["n"] >= max_nodes:
                counter["truncated"] = True   # 还有兄弟没展开 = 确实截断了
                break
            child = self.child_at(obj, i)
            if child is None:
                continue
            node.children.append(
                self._build(child, app_name, depth + 1, max_depth, max_nodes, counter, include_rect)
            )
        return node

    # ---------- 元素搜索 ----------
    # 遍历熔断预算（访问节点总数上限）。实测坑：Chromium/Electron 应用（Chrome/飞书/QQ 等）
    # 的 a11y 树可达数万节点，全桌面裸搜时逐节点 D-Bus IPC 会把 at-spi2-registryd
    # 和会话总线打满，连带拖死依赖同一总线的 GNOME Shell（表现为系统卡死）。
    # 全桌面搜索预算收紧；指定 root 的搜索给更宽预算。
    _VISIT_BUDGET_DESKTOP = 6000
    _VISIT_BUDGET_SCOPED = 24000

    def find(
        self, text: str | None = None, role: str | None = None, root: Any | None = None,
        interactive_only: bool = True, max_depth: int = 12, limit: int = 40,
        window_pids_provider: Any = None,
    ) -> QueryResult:
        """
        在 root（默认整个桌面）子树内搜索匹配 text（名字子串）/ role（角色子串）的元素。
        复用 demo collect 思路。返回 QueryResult（items 为原生元素对象）。

        三重约束，任一命中都会在 notice 里说明「结果可能不全」——**不静默截断**：
          1. 节点数熔断（见 _VISIT_BUDGET_*）：遍历量上限，防打爆 a11y D-Bus；
          2. **应用数上限**（_APP_TOUCH_BUDGET，仅不带 root 时生效）：每触碰一个应用都会
             逼它构建整棵 a11y 树，这笔一次性成本只被节点预算记作 1，挡不住「触碰 N 个
             应用」。get_tree / get_active_window 早有同类约束，find 原先独缺（I-4②）。
          3. limit：结果条数上限。

        ⚠️ **不带 root 时，先用 `window_pids_provider` 给的「有 X11 窗口的 pid 集合」跳过
        无窗口应用**（与 M-23 在 get_active_window 里的修法同源）。为什么这里必须跳过、
        而不是只调整搜索顺序：gnome-shell / 输入法 / at-spi 注册器这类应用**根本没有客户窗**
        （`wmctrl -l` 只列受 WM 管理的窗口），它们的树却很大 —— 正是 2026-09-14 打崩
        GNOME Shell 的那一类。留在候选里，无论排第几都可能被那 8 个预算名额撞上。
        代价与兜底：漏掉的是「有 a11y 树、但当前不在 WM 窗口清单里」的应用（没设
        `_NET_WM_PID` 的老 Java/SDL 程序、只有弹出菜单在屏上的瞬间）。这不能静默发生，
        故**搜索结果为空且有应用被跳过时**，notice 里写明并给出解法 —— 带 `app=` 重搜走
        的是 scoped 路径（root 已给定），**不受本条限制**。
        """
        results: list[Any] = []
        notice = ""
        scoped = root is not None
        roots = [root] if scoped else list(self.iter_apps())
        budget = self._VISIT_BUDGET_SCOPED if scoped else self._VISIT_BUDGET_DESKTOP
        visited = {"n": 0}
        text_l = text.lower() if text else None
        role_l = role.lower() if role else None
        # scoped 时 root 已经指定，预滤既无必要也无从下手（那是单个应用内部的遍历）
        win_pids = set() if scoped else self._resolve_window_pids(window_pids_provider)
        touched = 0
        skipped_no_window = 0
        for r in roots:
            if not scoped:
                # get_process_id 是**纯属性读**，不触发建树，零 a11y 成本（判据同 M-23）。
                # 刻意**不**按「child_count > 0 才计数」判：那要先调 child_count 才知道有没有
                # 子节点 —— 危险调用一次没省、反而翻倍，等于放宽唯一那道遍历爆炸防线。
                if win_pids and self.get_process_id(r) not in win_pids:
                    skipped_no_window += 1
                    continue
                touched += 1
                if touched > self._APP_TOUCH_BUDGET:
                    notice = (
                        f"⚠️ 搜索已触及应用数上限（{self._APP_TOUCH_BUDGET} 个应用），"
                        f"**结果只覆盖桌面上的部分应用、可能不全**；"
                        f"请带 app= 限定到具体应用后重新搜索"
                    )
                    log.warning("find 触及应用数上限(%d)已截断，结果不完整",
                                self._APP_TOUCH_BUDGET)
                    break
            self._collect(r, text_l, role_l, interactive_only, 0, max_depth, limit,
                          results, visited, budget)
            if len(results) >= limit or visited["n"] >= budget:
                break
        if not scoped and not results and skipped_no_window:
            # 「没搜到」与「没去看」是两回事，必须分开说 —— 否则模型会把后者当成桌面上
            # 真的没有，直接掉到截图兜底那条最贵的路上。
            extra = (f"⚠️ 另有 {skipped_no_window} 个应用因「当前无 X11 窗口」未被搜索"
                     f"（gnome-shell / 输入法 / 守护进程那类，跳过是为避免无谓构建 a11y 树）。"
                     f"若确信目标在其中，请带 app= 指定应用名重新搜索（指定后不受此限制）")
            log.info("find 无结果，本次跳过了 %d 个无窗口应用", skipped_no_window)
            notice = f"{notice}\n{extra}" if notice else extra
        if visited["n"] >= budget and len(results) < limit:
            extra = (f"⚠️ 搜索触发节点数熔断（单次遍历量上限 {budget}），**结果可能不全**；"
                     f"建议加 app/text 限定缩小搜索范围")
            log.warning(
                "find 遍历触发节点数熔断(budget=%d, visited=%d)，结果可能不全；"
                "建议加 app/text 限定缩小搜索范围", budget, visited["n"])
            notice = f"{notice}\n{extra}" if notice else extra
        return QueryResult(items=results[:limit], notice=notice)

    def _collect(
        self, obj: Any, text_l: str | None, role_l: str | None,
        interactive_only: bool, depth: int, max_depth: int, limit: int,
        results: list[Any], visited: dict[str, int], budget: int,
    ) -> None:
        if depth > max_depth or len(results) >= limit or visited["n"] >= budget:
            return
        visited["n"] += 1
        role = self.role_name(obj)
        name = self.get_name(obj)
        match_text = (text_l is None) or (text_l in name.lower())
        match_role = (role_l is None) or (role_l in role)
        actionable = (role in ACTIONABLE_ROLES) or bool(self.get_actions(obj))
        if match_text and match_role and name:
            if (not interactive_only) or actionable or role in INTERACTIVE_ROLES:
                results.append(obj)
        for i in range(self.child_count(obj)):
            if len(results) >= limit or visited["n"] >= budget:
                return
            child = self.child_at(obj, i)
            if child is not None:
                self._collect(child, text_l, role_l, interactive_only,
                              depth + 1, max_depth, limit, results, visited, budget)

    # ---------- 元素详情 ----------
    def make_detail(self, obj: Any, ref: int = 0, app: str = "") -> ElementDetail:
        """原生元素 → ElementDetail（含 value/states）。"""
        return ElementDetail(
            ref=ref, role=self.role_name(obj), name=self.get_name(obj),
            app=app or self._app_name_of(obj), rect=self.get_extents(obj, "SCREEN"),
            actions=self.get_actions(obj), value=self.get_value(obj),
            states=self.get_states(obj),
        )

    def _app_name_of(self, obj: Any) -> str:
        """回溯到 application 层取应用名。"""
        cur = obj
        seen = 0
        while cur is not None and seen < 32:
            parent = self.parent(cur)
            if parent is None or self.role_name(parent) in ("application", "desktop frame"):
                if self.role_name(cur) == "application":
                    return self.get_name(cur)
                # cur 是顶层窗口，其父是 application
                if parent is not None:
                    return self.get_name(parent)
                return ""
            cur = parent
            seen += 1
        return ""

    # ---------- 元素级操作（首选路径）----------
    def do_action(self, obj: Any, action: str | None = None) -> bool:
        """
        元素级操作。复用 demo/action_vs_coord.py 路径A。

        实现逻辑：
          1. 列出元素 actions。
          2. 指定 action 时按名字精确匹配其索引（再退一步做子串匹配）；
             **指定了却匹配不到 → 不执行任何动作**（返回 False，见下方 M-20 说明）。
             未指定 action 时按 _PREFERRED_ACTIONS 优先级选；都没有则取第 0 个。
          3. 调 obj.do_action(index)，返回布尔。
        """
        acts = self.get_actions(obj)
        if not acts:
            return False
        idx = None
        if action:
            for i, nm in enumerate(acts):
                if nm == action:
                    idx = i
                    break
            if idx is None:
                for i, nm in enumerate(acts):
                    if action.lower() in nm.lower():
                        idx = i
                        break
            if idx is None:
                # 指定了动作名却匹配不到 → **不执行任何动作**，返回 False。
                #
                # 为什么不能落到下面那个「取第 0 个」的兜底（M-20，见 docs/REVIEW/review_0.1.0.md）：
                # 调用方给的是**明确的动作名**，匹配不到就说明「这个元素没有这个动作」。
                # 此时执行第 0 个动作 = 做了**另一件语义可能完全相反**的事（delete/remove…），
                # 且返回值只有 bool，调用方无从知道实际执行了什么。
                # 更要命的是它会被 focus() 的退化分支踩中：`do_action(obj, "focus") or
                # do_action(obj, "select")` —— "focus"/"select" 几乎从不作为动作名存在，
                # 于是会落到 _PREFERRED_ACTIONS 命中 "click"，**真的把元素点一下**；
                # 而 coordinator.type_text 在元素级赋值前**无条件**调 backend.focus() 且
                # 不看返回值，故「对只有 click 动作的元素调 type_text」会先误点它一次，
                # 全程无感知 —— 正是本项目最怕的「点到了别的元素还报成功」。
                # 世界观：**「没做」永远比「做了别的」安全**（同 I-1 的「宁可失败不猜」）。
                log.debug("do_action: 指定动作 %r 不在该元素的动作里（仅有 %s），不执行任何动作",
                          action, acts)
                return False
        if idx is None:
            for pref in _PREFERRED_ACTIONS:
                for i, nm in enumerate(acts):
                    if nm == pref:
                        idx = i
                        break
                if idx is not None:
                    break
        if idx is None:
            idx = 0
        try:
            return bool(obj.do_action(idx))
        except Exception as exc:  # noqa: BLE001
            log.debug("do_action 失败: %s", exc)
            return False

    def set_value(self, obj: Any, text: str) -> bool:
        """
        元素级赋值（文本框）。尽力而为，失败返回 False（上层降级到 focus + xdotool type）。

        实现逻辑：优先 EditableText.set_text_contents；其次「真清空 + 插入 + 校验」。

        ️ **语义约定：元素级赋值必须是「替换」**——把框里的内容设成 text。
        键盘注入兜底才是「输入」（遵循 clear_first，默认是追加）。两者语义不同是有意的：
        元素级走的是 EditableText 的「赋值」，键盘走的是「往光标处打字」。

        历史 bug（2026-09-16 修，见 docs/REVIEW/review_0.1.0.md I-2）：路径 2 原实现只
        `set_caret_offset(0)` 就把新文本插到位置 0 —— **旧内容根本没有删除**，结果是
        「新 + 旧」拼接，随后却无条件 `return True`；coordinator 据此回报「元素级
        set_value 成功」，模型以为替换成功，属**静默的数据错误**。
        现改为先 delete_text 真清空、再插入，并用字符数校验结果；任何一步不能确认
        （清空失败 / 读不到字符数 / 长度对不上）一律返回 False 交兜底，**不猜**。
        """
        # 路径1：set_text_contents（AT-SPI 标准的「替换全部内容」）
        try:
            fn = getattr(obj, "set_text_contents", None)
            if callable(fn) and fn(text):
                return True
        except Exception:  # noqa: BLE001
            pass
        # 路径2：手动「清空 → 插入 → 校验」
        insert = getattr(obj, "insert_text", None)
        if not callable(insert):
            return False
        try:
            n = obj.get_character_count()
            if n:
                obj.delete_text(0, n)
        except Exception as exc:  # noqa: BLE001
            # 清不掉就干脆不插：否则会重演「新+旧」拼接。交上层键盘兜底。
            log.debug("set_value 路径2 清空失败，放弃（不猜，交兜底）: %s", exc)
            return False
        try:
            insert(0, text, len(text))
        except Exception as exc:  # noqa: BLE001
            log.debug("set_value 路径2 插入失败: %s", exc)
            return False
        # 校验：清空 + 插入后字符数应等于写入长度。读不到就当作失败——无法确认成功时
        # 返回 False 让上层兜底，好过报告一个可能错的成功。
        try:
            return obj.get_character_count() == len(text)
        except Exception:  # noqa: BLE001
            return False

    def focus(self, obj: Any) -> bool:
        """尝试让元素获得焦点（grab_focus / 选中）。"""
        for meth in ("grab_focus", "set_focus"):
            try:
                fn = getattr(obj, meth, None)
                if callable(fn):
                    return bool(fn())
            except Exception:  # noqa: BLE001
                continue
        # 退化：尝试 select 类 action
        return self.do_action(obj, "focus") or self.do_action(obj, "select")
