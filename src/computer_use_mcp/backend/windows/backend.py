"""
Windows 后端（backend.windows.backend）—— `Backend` ABC 的 Windows 实现。

═══ 分层 ═══

本模块是**编排层**：把 ABC 的语义翻译成驱动层（`driver/`）的调用。
真正的平台调用一律在 `driver/` 里（`win32.py` 的 ctypes、`_uia.py` 的 uiautomation、
`nodes.py` 的映射、`apps.py` 的窗口枚举、`actions.py` 的动作）。
这样"换库/换封装"只改驱动层，而 `get_tree` 该返回什么形状这类语义问题留在这里。

═══ ⚠️ 与 Linux 侧最重要的一条差异：没有"打崩宿主"的风险，但换成了延迟 ═══

Linux 侧 `child_count` 会逼目标应用**惰性构建整棵无障碍树**，历史上把宿主 GNOME Shell
打崩过两次 —— 所以那边的预算是**保命闸**。

Windows 的 UIA 是跨进程 COM，**不会崩宿主**，但**遍历很贵**：实测
**13~21 ms/节点**（`docs/PR/windows-driver/README.md` M3），首次访问某个应用还可能
4300 ms（provider 惰性建树）。所以这里的 `max_nodes` 是**时延闸**不是保命闸 ——
`max_nodes=400` 在 Windows 上意味着 5~8 秒。**墙钟 deadline 是必须的**，
不能只靠节点数上限（这一条是从真机测出来的数字推出来的，不是推测）。

⚠️ 2026-09-28 补测：**Chromium 类应用比上面那个估计还慢** —— Edge 实测暖读
26.6 ms/节点、冷读 **36.9 ms/节点**，且"首次碰它"（`ControlFromHandle`，会触发
Chromium 懒建 a11y 树）单独就要 **2.43s**。后者原先落在墙钟预算**之外**，现已纳入
（见 `get_tree` 里"预算先武装"那段注释）。

═══ 本轮已实现 / 未实现（诚实边界）═══

已实现：UIA 能覆盖的一切 —— 读树、搜索、元素级动作、坐标注入、窗口枚举，
以及**抓屏与点击反馈**（`screenshot` / `click_preview` / `change_fraction_since`）。

⚠️ 这三样的平台相关部分**只有「抓哪块屏」这一件事**，而 Windows 没有 DISPLAY 这个概念，
所以像素活全部复用平台中立的 `backend/screen.py`（`mss` 本来就跨平台，Windows 侧走 GDI，
且 `pyproject.toml` 早已声明 mss/pillow 为依赖）。**不要再写"抓屏需要独立的平台 API"**——
那句话是初版的错误判断，实测不成立。

**未实现**：无 —— 23 个抽象方法 + 全部可选能力（含 OCR）都已落地。
OCR 走**系统自带的 `Windows.Media.Ocr`**（选型与实测证据见 `driver/ocr.py`），
不需要随包带 tesseract、也不需要用户装任何东西。

⚠️ 本平台**唯一**的接口不对称：OCR 没有置信度（`SUPPORTS_TEXT_CONFIDENCE = False`），
`min_conf` 不生效。这一条必须**如实转告**模型，不能让它以为自己筛过了。

═══ ★ DPI 感知必须在**构造后端时**就设死（2026-09-28 真机实测的根因）═══

原先感知度是"惰性"设的：只有第一次碰 UIA（`_uia.import_uia()`）才调
`set_dpi_awareness_v2()`。**这在 150% 缩放的机器上会中途翻转坐标空间**，实测数据
（`docs/PR/windows-driver/probe_dpi_flip.py`，客体 1920x1009、dmLogPixels=144）：

    碰 UIA 之前           碰 UIA 之后
    DPI 感知 = 0（未感知）  →  2（PER_MONITOR_AWARE_V2）
    SM_CXSCREEN = 1280x673  →  1920x1009
    同一个窗口的矩形 = (703,95,1230,474) → (1055,143,1846,712)

后果是一条**只在非 100% 缩放机器上出现**的错位：翻转前拿到的坐标（虚拟化空间）
被翻转后的注入按另一个空间解释 —— 端到端 story 里就表现为"点标题栏却点到了桌面、
前台被踢成 Program Manager"，而每一层的返回值都是 ok。

修法：**在 `__init__` 里就把感知度设成 V2**。构造发生在 `get_backend()`，也就是
server 进程启动时（早于任何工具调用），于是整个进程生命周期只有**一个**坐标空间。
`import uiautomation` 那半件事仍保持惰性（它更重，且顺序由 `_uia` 固化）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from ...utils.errors import BackendUnavailableError, ComputerUseError
from ...utils.logging import get_logger
from .. import screen
from ..base import (
    ACTIONABLE_ROLES,
    UINode,
    Backend,
    ClickPreview,
    Element,
    ElementDetail,
    QueryResult,
    Rect,
    TextBlock,
)
from .driver import _uia, actions, apps, capture, desktop, nodes, session_info, win32
from .driver import ocr as ocr_mod

log = get_logger(__name__)

# ── 预算（口径见模块 docstring）──────────────────────────────────────────
#
# ⚠️ 与 Linux 侧的**语义不同**：那边是"碰一个应用会让它建树、可能打崩宿主"的保命闸；
# 这边是"每次跨进程 COM 往返 13~21ms"的**时延闸**。数值沿用同一批常量是为了让工具层
# 的语义（"最多看几个应用/几个节点"）跨平台一致 —— 但**墙钟 deadline 是本平台独有的**，
# 因为只有这边会"节点数没超、时间已经花光"。
_APP_TOUCH_BUDGET = 8
_TREE_APP_BUDGET = 8
# 整次 get_tree 的墙钟上限。按 13~21ms/节点反推：400 节点 ≈ 5~8s，
# 故给到 12s 留出"首次访问 provider 惰性建树"的余量。
#
# ⚠️ 2026-09-28 真机实测（`docs/PR/windows-driver/probe_chromium_scale.py`，Edge）：
# Chromium 类应用**比这个估计慢不少** —— 暖读 26.6 ms/节点、**冷读 36.9 ms/节点**。
# 于是 12s 在它们身上只够约 300~400 节点，真实网页必然被截断。
# 这是**刻意的取舍**：宁可给一棵不完整的树并明说"已触墙钟上限"，
# 也不要让工具调用卡上一分钟（那会把"省往返"这条主线整个吃掉）。
# 故下面 notice 里的数字写的是**实测区间**（13~37），不再只报乐观的那个下界。
_TREE_DEADLINE_S = 12.0
# find 的墙钟上限（搜索比整树遍历更容易失控：它要下钻到叶子）
_FIND_DEADLINE_S = 10.0


class WindowsBackend(Backend):
    """UIA（uiautomation）+ SendInput 的 Windows 后端。"""

    name = "windows"

    # ★ 自带 OCR 引擎**不提供置信度**（`OcrWord` 只有 BoundingRect/Text），
    #   故 `TextBlock.conf` 只能是常数占位、`min_conf` 不生效。
    #   如实声明它 —— 让参数静默变成 no-op 比不支持它更糟。见 base.py 的说明。
    SUPPORTS_TEXT_CONFIDENCE = False

    # ★ 不给范围时**按窗口**截，不抓全屏。
    #   用户桌面上同时开着浏览器/聊天工具/IDE —— 抓全屏会把它们一起送进模型上下文
    #   （隐私 + 准确性双输），而目标应用被别的窗口盖住时更是直接截错东西。
    #   Linux 那边整块 Xephyr 屏就是工作台，所以保持 "screen"（见 base.py 的说明）。
    DEFAULT_CAPTURE_SCOPE = "window"

    def __init__(self) -> None:
        # 刻意**不在 __init__ 里** import uiautomation / 建 COM 对象：
        # 构造发生在 `get_backend()`，而那是任何工具调用都会走的路径；
        # 把重活推迟到真正要用时（首次 `_uia()`），失败面也更容易定位。
        self._uia_mod: Any = None
        self._error: str = ""
        # OCR 引擎惰性创建：首次要枚举语言、挑引擎，不该在每次构造后端时做
        self._ocr_reader: Any = None
        # ⚠️ **但 DPI 感知必须在这一刻就设**，它是唯一不能拖的（`import uiautomation` 那
        #    半件事仍留到 `_uia()`）。它买的是「整个进程生命周期里所有坐标都在同一个
        #    空间」—— 拖到"第一次碰 UIA 才设"会让坐标空间**中途翻转**，
        #    详见模块 docstring 的「DPI 感知必须在构造后端时就设死」。
        self._dpi: dict[str, Any] = _uia.ensure_dpi_awareness()
        # ★ 最近一次由 `spawn_app` 启动的进程号 —— 隔离模式下用它把「当前该操作哪扇窗」
        #   推出来（隐藏桌面上没有前台窗口，见 `apps._target_foreground` 的说明）。
        self._last_spawned_pid: int = 0
        # ⚠️ 还要记**启动的是哪个 exe**：Win11 的 Store 应用别名（`notepad.exe` 就是）
        #   会先起一个 launcher 进程、再拉起真正的应用进程 —— 于是
        #   `CreateProcess` 给的 pid 与**窗口的** pid **不是同一个**（实测：
        #   launch_app 报 pid=5664，窗口的 pid 是 9164）。只按 pid 匹配会静默落到
        #   "面积最大"那条兜底上，而模型的意图明明是"操作我刚启动的那个"。
        self._last_spawned_app: str = ""
        # ★ **隔离桌面也必须在这一刻就建好**，不能等第一次 `launch_app`。
        #
        #   理由是"窗口枚举该看谁"会中途翻转：桌面没建时 `is_isolated()` 为假，
        #   `list_windows` 枚举的是**用户桌面**（看到你开的那些窗）；建好之后同一句
        #   枚举只看**我们的**桌面（空的）。同一次会话里两种答案，而**都不报错** ——
        #   与 DPI 感知那条"坐标空间中途翻转"是同一类失效。
        #   隐藏桌面不弹任何可见的东西，所以没有 Linux 侧"别一开 Claude 就弹个 Xephyr"
        #   那个顾虑，可以放心提前建。
        self._isolated: dict[str, Any] = {}
        if desktop.is_isolated():
            try:
                self._isolated = desktop.ensure_isolated()
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 这里**必须兜住**：构造发生在 `get_backend()`，而它是任何工具调用
                # 都会走的路。构造失败的代价不该是"整个后端起不来"，而应该是
                # "这次没有隔离桌面"—— 后者由 `spawn_app` 在真正要启动应用时
                # **响亮拒绝**（它检查 `ensure_isolated()` 的返回值）。
                # 顺带这也是"在非 Windows 上构造 `WindowsBackend` 不炸"那条既有
                # 纪律（`test_windows_backend_is_instantiable_on_this_platform`）的保证。
                self._isolated = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not self._isolated.get("ok"):
                log.warning("隔离桌面建立失败，本次会话将**没有隔离**：%s",
                            self._isolated.get("error"))

    # ---------- 就绪 ----------

    def _uia(self):  # noqa: ANN202
        if self._uia_mod is None:
            self._uia_mod = _uia.import_uia()
        return self._uia_mod

    def is_available(self) -> bool:
        """
        后端是否可用。

        ⚠️ **不碰 UIA**（不建树、不枚举应用）：本方法会被 `--selftest` 与
        `ensure_available()` 频繁调用，而 Linux 侧的同名方法就因为会触碰 a11y
        而在失败路径上被刻意绕开（见 `server._selftest` 的 M-43 注释）。
        这里只做两件便宜事：驱动库在不在、会话可不可信。
        """
        try:
            _uia.import_uia()
        except _uia.WindowsDriverUnavailable as exc:
            self._error = str(exc)
            return False
        return True

    def ensure_available(self) -> None:
        if not self.is_available():
            raise BackendUnavailableError(
                f"Windows 驱动不可用。请确认：①运行于 Windows；②已 `pip install uiautomation` "
                f"（会连带装 comtypes）。底层错误: {self._error}"
            )

    def liveness(self) -> bool:
        """
        目标会话是否活着 —— **完全不碰 a11y**（与 Linux 侧同名方法同口径）。

        判据是"当前进程是否处在一个**有真桌面的交互式会话**里"：
        服务会话（Session 0）下 UIA 根元素恒为 0、抓屏全黑、前台窗口读不到，
        **而且不报任何错**（真机 DEMO 的血证）。所以这是"这份数据可不可信"的第一道闸，
        必须在会触碰 a11y 之前就能回答。
        """
        try:
            si = session_info.check()
            return bool(si.get("interactive"))
        except Exception:  # noqa: BLE001 —— 判活失败不该把调用方炸掉，返回"不活"即可
            return False

    def sync_at_spi_bus(self) -> None:
        """Windows 上没有 AT-SPI 总线可同步 —— 显式 no-op（不是"忘了实现"）。"""
        return None

    # ---------- 应用 ----------

    def list_apps(self) -> list[str]:
        """当前桌面上的应用名（进程可执行文件名，去重排序）。

        走 X11 侧等价的 Windows 通道 = 枚举顶层窗口取进程名，
        **不触碰 UIA**（同 Linux 侧"list_apps 已改走 X11，零 a11y 成本"的理由）。
        """
        self.ensure_available()
        return apps.app_names()

    # ---------- 树 ----------

    def get_tree(
        self, scope: str = "active_window", app: str | None = None,
        max_depth: int = 8, max_nodes: int = 300,
    ) -> QueryResult:
        """
        读无障碍树，返回 QueryResult（items 为 UINode 森林）。

        scope 语义与 Linux 侧**逐字对齐**：
          - `active_window`：当前前台窗口（默认，最省）；
          - `app`：指定应用的全部窗口；
          - `desktop`：整个桌面的有窗口应用（受 `_TREE_APP_BUDGET` 约束）。

        ⚠️ **非法 scope 一律抛错**（与 Linux 侧同口径）：历史坑是不校验时
        `scope="desktops"` 会静默变成"活动窗口树"、`scope="app"` 不带 `app=` 会静默
        变成"整个桌面"——**两者都不报错，只是给出另一种结果**，模型无从察觉。

        ⚠️ `max_nodes` 是**整次调用**的上限（不是每窗口），跨窗口共享同一个计数，
        且额外受 `_TREE_DEADLINE_S` 墙钟约束（本平台独有，见模块 docstring）。
        任一命中都写进 notice —— **截断必须让模型知道**，否则会把"没返回"
        误读成"界面上没有"。
        """
        if scope not in ("active_window", "app", "desktop"):
            raise ComputerUseError(
                f"未知 scope={scope!r}（仅支持 active_window / app / desktop）")
        if scope == "app" and not app:
            raise ComputerUseError(
                "scope='app' 必须同时给出 app=（应用名关键词）；"
                "否则会退化成受预算限制的整桌面遍历，拿到的不是你要的东西。"
                "想扫整个桌面请显式用 scope='desktop'。")
        self.ensure_available()

        # ⚠️ **墙钟预算必须先武装，再去找根窗口**（2026-09-28 真机实测改）。
        #    原先 deadline 在 `_roots_for` **之后**才算起 —— 而 `_roots_for` 会做
        #    `ControlFromHandle(hwnd)`，Chromium/Electron 的 a11y 是**懒建**的，
        #    第一次碰它可能要好几秒（实测 Edge：整次读树 14.4s，其中 `_roots_for`
        #    与首次遍历各占一截）。于是"总耗时"可以远超预算而 notice 是空的 ——
        #    恰恰在最需要它说话的时候（树最大、最慢）不说。
        deadline = time.monotonic() + _TREE_DEADLINE_S
        roots, notices = self._roots_for(scope, app, deadline=deadline)
        forest: list[UINode] = []
        counter = {"n": 0}

        for label, element in roots:
            if counter["n"] >= max_nodes or time.monotonic() > deadline:
                break
            forest.append(self._build_tree(label, element, 1, max_depth,
                                           max_nodes, counter, deadline))
        # 超时只在这里判一次：`_build_tree` 内部也会按同一个 deadline 提前收手，
        # 故"有没有超"与"停在哪"用的是同一个判据，不会出现两处口径不一致。
        if time.monotonic() > deadline:
            notices.append(
                f"⚠️ 读树已达**墙钟上限**（{_TREE_DEADLINE_S:.0f}s，本平台遍历约 "
                f"13~37ms/节点，Chromium 类应用偏慢）：结果不完整，"
                f"请缩小范围（用 scope='active_window'）")
        if counter["n"] >= max_nodes:
            notices.append(
                f"⚠️ 节点数已达上限（{max_nodes}），树被截断；"
                f"需要更多请调大 max_nodes 或缩小 scope。")
        return QueryResult(items=forest, notice="　".join(notices))

    def _roots_for(self, scope: str, app: str | None,
                   deadline: float | None = None) -> tuple[list[tuple[str, Any]], list[str]]:
        """
        scope → 一组「(应用名, UIA 根元素)」。

        ⚠️ 根元素取**窗口**（`ControlFromHandle`）而不是 UIA 的桌面根：
        本项目在 Linux 侧的主路径就是"先枚举窗口、再按窗口直读"，Windows 侧 DEMO
        也证实了这条（`ControlFromHandle(顶层 HWND)` 能读进整棵子树）。
        走 UIA 桌面根会一次性把所有应用拉起来 —— 那正是 Linux 侧 `desktop` scope
        出事的形状，这边虽然不会崩，但会白白花掉几十秒。
        """
        uia = self._uia()
        notices: list[str] = []

        def out_of_time() -> bool:
            """预算用完了没有（`deadline=None` 表示调用方没给预算，按"没超"处理）。"""
            return deadline is not None and time.monotonic() > deadline

        def deadline_notice(covered: int) -> str:
            return (f"⚠️ 找根窗口时就已用完墙钟预算（{_TREE_DEADLINE_S:.0f}s），"
                    f"只覆盖了前 {covered} 个应用；请用 app= 指定目标。")

        rows = apps.window_rows(limit=1000)

        if scope == "active_window":
            hwnd = self._foreground_hwnd()
            if not hwnd:
                return [], ["没有前台窗口（服务会话的典型症状）—— 改试 scope='app'"]
            return [(self._app_label(hwnd), uia.ControlFromHandle(hwnd))], notices

        if scope == "app":
            needle = (app or "").lower()
            picked = [r for r in rows if needle in (r["app"] or "").lower()]
            if not picked:
                picked = [r for r in rows if needle in (r["title"] or "").lower()]
            if not picked:
                return [], [f"没有名为 {app!r} 的应用或窗口"]
            # ⚠️ 这里也逐窗判预算：`_element_of` 是一次跨进程的 `ControlFromHandle`，
            #    对 Chromium/Electron 这类懒建树的应用，**第一次**就可能花掉几秒。
            out: list[tuple[str, Any]] = []
            for row in picked[:_APP_TOUCH_BUDGET]:
                if out_of_time():
                    notices.append(deadline_notice(len(out)))
                    break
                out.append((row["app"] or row["title"], self._element_of(row)))
            return out, notices

        # desktop：受应用数预算约束
        seen_apps: set[str] = set()
        out = []
        for row in rows:
            key = row["app"] or row["class"]
            if key in seen_apps:
                continue
            if len(seen_apps) >= _TREE_APP_BUDGET:
                notices.append(
                    f"⚠️ 桌面应用数已达上限（{_TREE_APP_BUDGET}），只覆盖了前 "
                    f"{_TREE_APP_BUDGET} 个应用；需要别的应用请用 scope='app' 指定。")
                break
            if out_of_time():
                notices.append(deadline_notice(len(out)))
                break
            seen_apps.add(key)
            out.append((key, self._element_of(row)))
        return out, notices

    def _element_of(self, row: dict) -> Any:  # noqa: ANN401
        """窗口行 → UIA 元素（按 hwnd 直读；这是本项目感知的主路径）。"""
        hwnd = int(row["id"], 16)
        return self._uia().ControlFromHandle(hwnd)

    def _app_label(self, hwnd: int) -> str:
        return win32.process_basename(win32.pid_of_window(hwnd)) or win32.class_name(hwnd)

    def _foreground_hwnd(self) -> int:
        """
        「本次该操作的那扇窗」的句柄。

        ⚠️ 隔离模式下**不是** `GetForegroundWindow()` —— 隐藏桌面上没有前台窗口，
        而那个调用会稳定地返回**用户桌面上**的窗口。拿它当目标，后续的读树/OCR/
        落点证据就全打在用户的窗口上，且因为我们不抢前台，**用户完全感觉不到**。
        （`apps.active_window_*` 已按同一条理由返回 None，这里是同一个判断的另一处。）

        隔离模式下按 `_target_hwnd()` 推导：先认**本进程最近启动的那个应用**的窗口，
        没有就取目标桌面上面积最大的那扇，并在 `notice` 里说明选了谁 ——
        绝不静默猜一个。
        """
        if not desktop.is_isolated():
            return win32.foreground_window()
        hwnd, _why = self._target_hwnd()
        return hwnd

    def _target_hwnd(self) -> tuple[int, str]:
        """隔离模式下的「目标窗口」：(hwnd, 说明)。取不到返回 (0, 原因)。"""
        rows = apps.window_rows(limit=200)
        if not rows:
            return 0, ("隔离桌面上还没有任何窗口 —— 先用 launch_app 启动一个应用"
                       "（agent 自己起的应用才会落在隔离桌面上）")
        if self._last_spawned_pid or self._last_spawned_app:
            mine = [r for r in rows
                    if int(r.get("pid") or 0) == self._last_spawned_pid
                    or (self._last_spawned_app and
                        (r.get("app") or "").lower() == self._last_spawned_app)]
            if mine:
                r = max(mine, key=lambda x: x.get("area") or 0)
                return (int(r["id"], 16),
                        f"最近启动的应用（{self._last_spawned_app or 'pid=' + str(self._last_spawned_pid)}）")
        r = max(rows, key=lambda x: x.get("area") or 0)
        return (int(r["id"], 16),
                f"隔离桌面上面积最大的窗口（{r.get('title') or r.get('app')!r}）"
                f"—— 有多个窗口时建议显式传 app= 指明目标")

    def _build_tree(self, label: str, element: Any, depth: int, max_depth: int,  # noqa: ANN401
                    max_nodes: int, counter: dict, deadline: float) -> UINode:
        """
        递归建 UINode。**边遍历边判两个上限**（节点数 + 墙钟）。

        ⚠️ 与 `tree.walk_tree` 的区别：那个是给尖刀做"一次性快照"用的（返回 dict 列表），
        本方法要产出 `UINode` 并**在遍历中途就能停**。共用的方法是 `_children_of` 的
        纪律：`GetChildren()` 批量取子级（比 `GetFirstChildControl` + `GetNextSiblingControl`
        少一半跨进程往返）。
        """
        counter["n"] += 1
        role = nodes.role_of(self._safe(element, "ControlTypeName"),
                             self._safe(element, "ClassName"))
        node = UINode(
            role=role,
            name=self._safe(element, "Name") or "",
            app=label,
            depth=depth,
            # ⚠️ 传 role：树遍历是**批量**路径，按 `_PROBE_ROLES` 门控（每次 Pattern
            # 探测 ~2.6ms 跨进程调用，400 节点全探要多花约 7 秒，见 nodes 里的成本注释）。
            # 单个元素的追问走 `element_info`，那条**不门控**、给的是事实。
            actions=nodes.actions_of(element, role=role),
            states=nodes.states_of(element),
            value=nodes.value_of(element),
            rect=self._rect_of(element),
            children=[],          # ⚠️ 无默认值：不给的话下面 append 会 AttributeError
            native=element,
        )
        if depth >= max_depth or counter["n"] >= max_nodes or time.monotonic() > deadline:
            return node
        for child in self._children_of(element):
            if counter["n"] >= max_nodes or time.monotonic() > deadline:
                break
            node.children.append(self._build_tree(label, child, depth + 1, max_depth,
                                                  max_nodes, counter, deadline))
        return node

    @staticmethod
    def _children_of(element: Any) -> list:  # noqa: ANN401
        """子级列表。单个节点取子级失败（元素刚失效等）时跳过它，不中断整棵树。"""
        try:
            return list(element.GetChildren() or [])
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _safe(element: Any, attr: str) -> Any:  # noqa: ANN401
        """读一个属性，失败返回 None（字段级容错，同 `tree._snapshot`）。"""
        try:
            return getattr(element, attr, None)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _rect_of(element: Any):  # noqa: ANN401,ANN202
        r = nodes.rect_of(element)
        return Rect(x=r[0], y=r[1], w=r[2] - r[0], h=r[3] - r[1]) if r else None

    # ---------- 搜索 ----------

    def find(
        self, text: str | None = None, role: str | None = None, app: str | None = None,
        interactive_only: bool = True, max_depth: int = 12, limit: int = 40,
    ) -> QueryResult:
        """
        搜索元素，返回 QueryResult（items 为 `Element`）。

        实现逻辑：
          1. 确定搜索根（给了 `app=` 就只在那个应用的窗口里搜，否则全桌面可用窗口）；
          2. 深度优先下钻，逐个比对**名字子串**与**角色子串**（大小写不敏感，与 Linux 侧同口径）；
          3. 命中数达 `limit` 或墙钟超时即停，**两者都写进 notice**。

        ⚠️ `role` 是**子串**匹配而不是等值：调用方可能写 `"button"`（想同时命中
        `push button` / `toggle button`），等值匹配会一个都搜不到。与 Linux 侧
        `atspi.find` 的 `role_l in role` 保持一致。

        ⚠️ 截断必须报：搜索结果被 limit 截断时若不提示，模型会把"没返回"
        读成"界面上没有这个控件"，然后退到截图那条最贵的路。
        """
        self.ensure_available()
        uia = self._uia()
        text_l = (text or "").lower() or None
        role_l = (role or "").lower() or None

        # ⚠️ 与 `get_tree` 同一条纪律：**预算先武装，再去找搜索根**
        #    （`_element_of` 是跨进程的 ControlFromHandle，Chromium 这类懒建树的应用
        #    第一次碰它就可能花掉几秒；放预算外面 = 最慢的那一步不受约束）。
        deadline = time.monotonic() + _FIND_DEADLINE_S

        rows = apps.window_rows(limit=1000)
        if app:
            cand = [r for r in rows
                    if (app.lower() in (r["app"] or "").lower()
                        or app.lower() in (r["title"] or "").lower())]
        else:
            cand = rows[:_APP_TOUCH_BUDGET]
        roots: list[tuple[str, Any]] = []
        for r in cand[:_APP_TOUCH_BUDGET]:
            if time.monotonic() > deadline:
                break
            roots.append((r["app"] or r["title"], self._element_of(r)))

        hits: list[Element] = []
        notices: list[str] = []
        seen = {"n": 0}

        def visit(label: str, element: Any, depth: int) -> None:  # noqa: ANN401
            if len(hits) >= limit or seen["n"] > 24000 or time.monotonic() > deadline:
                return
            for child in self._children_of(element):
                if len(hits) >= limit or time.monotonic() > deadline:
                    return
                seen["n"] += 1
                name = self._safe(child, "Name") or ""
                crole = nodes.role_of(self._safe(child, "ControlTypeName"),
                                      self._safe(child, "ClassName"))
                acts = nodes.actions_of(child, role=crole)
                match_text = (text_l in name.lower()) if text_l else True
                match_role = (role_l in crole) if role_l else True
                actionable = bool(acts) or crole in ACTIONABLE_ROLES
                if match_text and match_role and name and (actionable or not interactive_only):
                    hits.append(Element(
                        ref=0,                      # 由 serializer/coordinator 分配
                        role=crole, name=name, app=label,
                        rect=self._rect_of(child), actions=acts))
                if depth < max_depth:
                    visit(label, child, depth + 1)

        for label, root in roots:
            if len(hits) >= limit or time.monotonic() > deadline:
                break
            try:
                visit(label, root, 1)
            except Exception as exc:  # noqa: BLE001 —— 单个应用失败不该毁掉整次搜索
                notices.append(f"{label}: {type(exc).__name__}")

        if len(hits) >= limit:
            notices.append(f"⚠️ 结果已达上限（{limit} 条），可能还有未列出的匹配。")
        if time.monotonic() > deadline:
            notices.append(
                f"⚠️ 搜索已达墙钟上限（{_FIND_DEADLINE_S:.0f}s），结果不完整；"
                f"请用 app= 缩小范围。")
        return QueryResult(items=hits, notice="　".join(notices))

    # ---------- 元素详情 ----------

    def element_info(self, native: Any) -> ElementDetail:  # noqa: ANN401
        """元素详情。TextBlock（灰区 OCR 块）分支与 Linux 侧同口径：它没有控件语义。"""
        if isinstance(native, TextBlock):
            # TextBlock 是纯数据对象，没有控件语义 —— actions **必须留空**
            # （同 Linux 侧 M-28②：报告"有 click 动作"会把模型引到元素级点击上，
            #   而它对 TextBlock 恒失败，凭空多一轮且让模型怀疑自己用错了 ref）
            return ElementDetail(
                ref=0, role="text-block", name=native.text, app="",
                rect=native.rect, actions=[],
                value=native.text, states=[])
        return ElementDetail(
            ref=0,
            role=nodes.role_of(self._safe(native, "ControlTypeName"),
                               self._safe(native, "ClassName")),
            name=self._safe(native, "Name") or "",
            app="",
            rect=self._rect_of(native),
            actions=nodes.actions_of(native),
            value=nodes.value_of(native),
            states=nodes.states_of(native))

    def is_alive(self, native: Any) -> bool:  # noqa: ANN401
        """
        元素是否还有效。

        ⚠️ 判据是"**还能读到它的一个属性**"，而不是"这个 Python 对象非空"：
        UIA 元素是跨进程代理，目标窗口关掉之后对象照样在，但每次访问都会抛
        `ElementNotAvailable`。轻量读一个属性正是这件事最直接的探针。
        """
        if isinstance(native, TextBlock):
            return True                     # 纯数据对象，不存在"失效"
        try:
            _ = native.ControlTypeName
            return True
        except Exception:  # noqa: BLE001
            return False

    def element_screen_rect(self, native: Any) -> Rect | None:
        """
        元素的屏幕矩形。TextBlock 直接用自带的 rect（**不套用任何校准**）。

        ⚠️ Linux 侧的 `resolve_element_screen_rect` 有一套"GTK 相对坐标漂移校准"，
        那是给 AT-SPI 的产物。Windows 的 UIA `BoundingRectangle` 就是**屏幕绝对物理像素**
        （150% 缩放下实测与注入坐标同一空间，见 `docs/PR/windows-driver/README.md` 第九节），
        **不该也不需要再校准** —— 套上去反而会把正确的坐标改坏。
        """
        if isinstance(native, TextBlock):
            return native.rect
        return self._rect_of(native)

    def window_screen_pos(self, native: Any) -> Rect | None:  # noqa: ANN401
        """元素所属顶层窗口的屏幕矩形。"""
        if isinstance(native, TextBlock):
            return None
        hwnd = self._top_hwnd_of(native)
        if not hwnd:
            return None
        r = win32.window_rect(hwnd)
        return Rect(x=r[0], y=r[1], w=r[2] - r[0], h=r[3] - r[1]) if r else None

    def _top_hwnd_of(self, native: Any) -> int:  # noqa: ANN401
        """
        元素所属的**顶层** HWND。

        ⚠️ 用 UIA 自己的 `GetTopLevelControl`，不自己爬父链：跨进程边界上爬父链
        容易爬到别的应用上（UIA 的父子关系与 Win32 的父子窗不是一回事）。
        """
        try:
            top = native.GetTopLevelControl()
            return int(getattr(top, "NativeWindowHandle", 0) or 0) if top else 0
        except Exception:  # noqa: BLE001
            return 0

    # ---------- 窗口 ----------

    def list_windows(self, limit: int = 100) -> list[dict[str, Any]]:
        return apps.window_rows(limit=limit)

    def wait_window(self, title_contains: str | None = None, window_id: str | None = None,
                    timeout: float = 10.0, poll: float = 0.25) -> dict | None:
        return apps.wait_window(title_contains=title_contains, window_id=window_id,
                                timeout=timeout, poll=poll)

    def screen_layout(self) -> dict[str, Any]:
        return apps.screen_layout()

    def active_window_title(self) -> str | None:
        """本次该操作的窗口标题。

        ⚠️ 隔离模式下走目标窗口推导（`apps.active_window_title` 返回 None 是**对的**，
        但它会让落点证据/落点文字全空）—— 这里补上"我们认定的目标"，
        语义与 Linux 侧的"活动窗口"对齐：都是「这次操作落在哪扇窗」。
        """
        if not desktop.is_isolated():
            return apps.active_window_title()
        hwnd, _why = self._target_hwnd()
        return (win32.window_text(hwnd) or None) if hwnd else None

    def active_window_rect(self) -> Rect | None:
        if not desktop.is_isolated():
            r = apps.active_window_rect()
            return Rect(x=r[0], y=r[1], w=r[2] - r[0], h=r[3] - r[1]) if r else None
        hwnd, _why = self._target_hwnd()
        if not hwnd:
            return None
        r = win32.window_rect(hwnd)
        return Rect(x=r[0], y=r[1], w=r[2] - r[0], h=r[3] - r[1]) if r else None

    # ---------- 元素级动作 ----------

    def focus(self, native: Any) -> bool:  # noqa: ANN401
        try:
            return actions.focus(native)
        except Exception:  # noqa: BLE001
            return False

    def invoke(self, native: Any, action: str | None = None) -> bool:  # noqa: ANN401
        """
        元素级动作（**首选路径**，零坐标）。

        ⚠️ 失败**要如实返回 False**：`coordinator.click` 靠它决定降级到坐标点击。
        这里吞掉异常返回 True 会让调用方以为"元素级成功了"，从而不去降级 —— 结果是
        什么都没发生，而工具报成功。
        """
        try:
            return actions.invoke(native, action)
        except Exception:  # noqa: BLE001
            return False

    def set_value(self, native: Any, text: str) -> bool:  # noqa: ANN401
        try:
            return actions.set_value(native, text)
        except Exception:  # noqa: BLE001
            return False

    # ---------- 坐标注入 ----------

    @staticmethod
    def _window_under_point(x: int, y: int) -> int:
        """
        落点 (x, y) 下的**顶层**窗口句柄；取不到返回 0（= 不抢焦点，别猜一个）。

        ⚠️ 必须 `window_from_point` 之后再 `root_ancestor`：前者给的是**最深**的子窗口
        （可能是个按钮），拿子窗去 `SetForegroundWindow` 是错的（要顶层窗）。
        """
        try:
            return win32.root_ancestor(win32.window_from_point(int(x), int(y)))
        except Exception:  # noqa: BLE001 —— 取不到就当没有，抢焦点本就是尽力而为
            return 0

    def click_at(self, x: int, y: int, button: int = 1, focus_window: bool = True,
                 repeat: int = 1, delay_ms: int = 100) -> bool:
        """
        坐标级点击（**兜底路径**）。

        ⚠️ 双击靠 `repeat=2` + `delay_ms` 交给输入层定时，**不要**让调用方连调两次：
        两次独立调用之间夹着落点证据、OCR、变化对比（实测约 0.5s），
        必然超过系统双击阈值而被认成两次单击 —— X11 侧为此吃过一次亏，Windows 同理。

        ⚠️ `focus_window` 抢的是**落点下的那个窗口**，不是"当前前台"。
        初版写的是 `hwnd = win32.foreground_window()` —— 那是**把当前前台再设一遍**，
        恒等于空操作，与 `base.py` 的契约（"先激活/聚焦**所在窗口**"）不符；
        而这个错误**不会报任何错**，只会让"点击被激活动作吃掉"那类现象继续存在
        （Linux 侧对此的说明见 `linux/inject/pointer.py`）。2026-09-28 真机复核后改正。
        """
        hwnd = self._window_under_point(x, y) if focus_window else 0
        return bool(win32.send_mouse_button(
            x, y, button=button, repeat=repeat, delay_ms=delay_ms,
            focus_window=focus_window, hwnd=hwnd).get("ok"))

    def drag_at(self, x1: int, y1: int, x2: int, y2: int, button: int = 1,
                steps: int = 10, focus_window: bool = True) -> bool:
        """
        坐标级拖拽。**必须插值分步**（原因见 `win32.send_mouse_drag`）。

        失败**不重试**：重试一次拖拽 = 再拖一遍（文件移动两次、画两笔），后果不对称。

        ⚠️ 抢焦点取**起点**下的窗口：拖拽的"意图对象"由起点决定（终点只是落点），
        `describe_point` 也只取起点的落点证据，两处口径一致。
        """
        if focus_window:
            hwnd = self._window_under_point(x1, y1)
            if hwnd:
                win32.set_foreground_window(hwnd)
        return bool(win32.send_mouse_drag(x1, y1, x2, y2, button=button, steps=steps).get("ok"))

    def type_text(self, text: str) -> bool:
        """
        输入文本 —— 走 `KEYEVENTF_UNICODE` 逐字符投递（见 `win32.send_unicode_text`）。

        ⚠️ 与 Linux 侧**最大的不同**：这边**不需要**"非 ASCII 走剪贴板"那套。
        Linux 上 `xdotool type` 遇中文会临时改写全局键盘映射（用户物理键盘整体失灵），
        所以必须绕到剪贴板，还要判目标是不是终端才能选粘贴键。Windows 的
        `KEYEVENTF_UNICODE` 直接投递 UTF-16 码元，**绕开键盘布局**、不改任何全局状态。
        """
        if not text:
            return True
        return bool(win32.send_unicode_text(text).get("ok"))

    def press_key(self, combo: str) -> bool:
        """
        发送快捷键。

        ⚠️ 组合名**认不出就返回 False**，绝不"尽力而为地按一部分"：
        `"ctrl+nosuchkey"` 若被解析成"只按 ctrl"，调用方会看到成功而实际什么都没发生。
        """
        vks = actions.parse_combo(combo)
        if not vks:
            return False
        return bool(win32.send_key_combo(vks).get("ok"))

    # ---------- 本轮仍未实现（诚实报错，不返回空）----------

    def read_text(self, region: tuple[int, int, int, int] | None = None,
                  min_conf: float = 40.0) -> list[TextBlock]:
        """
        OCR 读取一块区域 —— **走系统自带的 `Windows.Media.Ocr`**（不是 tesseract）。

        选型理由与实测证据见 `driver/ocr.py` 的模块 docstring（结论：零安装、无需打包工程、
        隐藏桌面可用）。这里只负责"抓哪一块"：不传 region 时取**活动窗口**的矩形 ——
        与 Linux 侧同口径，因为整屏 OCR 明显更慢。

        ⚠️ `min_conf` **在本平台上不生效**（引擎不提供置信度），见
        `SUPPORTS_TEXT_CONFIDENCE`。这里刻意不抛错也不假装过滤。
        """
        region = region or self._active_window_region()
        if region is None:
            raise ComputerUseError(
                "read_text 需要 region，或至少有一个活动窗口可供推断范围"
            )
        img, origin = screen.grab_desktop(tuple(int(v) for v in region))
        return self.read_text_from_image(img, origin, min_conf=min_conf)

    def read_text_from_image(self, img: Any, origin: tuple[int, int],  # noqa: ANN401
                             min_conf: float = 40.0) -> list[TextBlock]:
        """
        对**已经抓好的一张图**做 OCR（不重新抓屏），坐标以 origin 为图像左上角的屏幕位置。

        复用预览图那次抓屏是刻意的：落点文字/偏差识别应该用**同一块像素**，
        而且要用那张**没画过准星**的原始图（红线横穿文字会降低识别率）。

        ⚠️ 与 Linux 侧的一处**真实差异**，别当成 bug：Windows 引擎不提供置信度，
        故返回的 `TextBlock.conf` 是**常数占位**（见 `driver/ocr.py` 的 `_CONF_PLACEHOLDER`）。
        用 `SUPPORTS_TEXT_CONFIDENCE` 如实声明，调用方据此告诉模型 `min_conf` 不生效。
        """
        try:
            return self._ocr().read_image(img, origin, min_conf=min_conf)
        except BackendUnavailableError:
            raise                       # 缺包/没语言：是环境问题，直说
        except Exception as exc:  # noqa: BLE001
            raise ComputerUseError(f"Windows OCR 失败: {exc}") from exc

    def _ocr(self):  # noqa: ANN202
        """惰性建 `OcrReader`（首次会枚举语言、挑引擎，不该在 `__init__` 里做）。"""
        if self._ocr_reader is None:
            self._ocr_reader = ocr_mod.OcrReader()
        return self._ocr_reader

    def _active_window_region(self) -> tuple[int, int, int, int] | None:
        """活动窗口的 (x,y,w,h)；取不到返回 None。"""
        try:
            r = self.active_window_rect()
        except Exception:  # noqa: BLE001
            return None
        if r is None or r.is_empty():
            return None
        return (int(r.x), int(r.y), int(r.w), int(r.h))

    def describe_point(self, x: int, y: int, with_text: bool = False) -> dict[str, Any]:
        """
        落点证据：这个坐标下面是哪扇窗、底下的控件叫什么。

        ⚠️⚠️ **键名必须与 ABC 契约一致**（`backend/base.py:449-453`）：
        `{"x","y","window_id","window_title","window_rect","text","conf"}`。

        初版这里返回的是 `{"point","window","class","hwnd","control","control_type"}`——
        自造了一套方言。后果是消费方 `core/coordinator/landing.py:358-364` 读
        `ev.get("window_title")` / `ev.get("window_rect")` **恒为 None**，于是
        「落点：窗口「…」 WxH」整行**静默消失**，不报错、不掉任何测试。
        而落点证据正是「`ok=True` 只说明事件发出去了」这句话的解药。
        改法是**这边向契约对齐**，不是改消费方去兼容两种方言——那会让契约变成两份。

        与 Linux 侧的差异（如实记下，不假装）：
          · 落点**文字**（`text`/`conf`）来自 OCR，本平台未实现 → **这两个键不出现**
            （而不是给空串：空串会被读成「那儿没字」）。
          · `with_text=True` 时改报 UIA 的控件名与类型，用 `control`/`control_type` 两个
            **额外**键（Linux 没有对应物，不冲突）。注意 UIA 的 `Name` **不是**像素上
            写着的字，别把它当 OCR 结果读。
          · `window_id` 是 HWND，格式与 Linux 的 X11 窗口 id 一样只作标识用。
        """
        out: dict[str, Any] = {"x": int(x), "y": int(y)}
        try:
            # ⚠️ 必须取**顶层**窗口（`_window_under_point` 里做了 `root_ancestor`）。
            #    2026-09-28 真机实测：直接 `window_from_point` 常拿到**子控件**（编辑区/画布），
            #    子控件的 `GetWindowText` 是**空**的 → `window_title=None`，于是落点那一行
            #    显示成『落点：窗口「?」 839x569』—— 模型据此**分不清自己点在哪扇窗上**，
            #    而落点证据存在的唯一理由就是回答这个问题。
            #    （先前那条真机断言之所以通过，是因为它取的点正好在标题栏上——那属于
            #      非客户区，`WindowFromPoint` 直接返回顶层窗。**采样点挑得好掩盖了缺陷**。）
            hwnd = self._window_under_point(x, y)
            if hwnd:
                out["window_id"] = f"0x{hwnd:x}"
                out["window_title"] = win32.window_text(hwnd) or None
                out["window_rect"] = list(win32.window_rect(hwnd) or []) or None
            else:
                out["window_title"] = None
        except Exception:  # noqa: BLE001 —— 落点证据绝不让主操作失败
            pass
        if with_text:
            try:
                el = self._uia().ControlFromPoint(x, y)
                out["control"] = (getattr(el, "Name", None) or None) if el else None
                out["control_type"] = (str(getattr(el, "ControlTypeName", "") or "")
                                       or None) if el else None
            except Exception:  # noqa: BLE001
                pass
        return out

    # ---------- 抓屏与点击反馈（平台中立的像素活交给 backend/screen.py）----------
    #
    # 为什么这里几乎不用写平台代码：`mss` 本来就是跨平台的（Windows 侧走 GDI），
    # 而 `pyproject.toml` 早已把 mss/pillow 声明成依赖。Linux 侧真正专属的只有
    # **「抓哪块屏」**（`core/display.effective_display()`）与旧版 mss 的换 env 兼容路径
    # —— Windows 没有 DISPLAY 这个概念，那个问题不存在。
    #
    # ⚠️ 本模块的 docstring 里原先写着「抓屏需要一套独立的平台 API（BitBlt/PrintWindow）」，
    #    那句话**是错的**（已改）。真正的分工是 `backend/screen.py` 出像素活、
    #    `backend/linux/grab.py` 出「抓哪块屏」—— 见 screen.py 的模块 docstring。

    def capture_window(self, window_id: str) -> tuple[Any, tuple[int, int]] | None:
        """
        **按窗口渲染**（`PrintWindow` + `PW_RENDERFULLCONTENT`）—— 「只截某一个软件」那条路。

        ⚠️ 与 `screenshot()` 的根本区别，别混：
          · `screenshot(region=…)` 走 `mss` 的**屏幕 DC**，抓到的是「屏幕上那一块」——
            **被别的窗口盖住时，截到的是盖住它的那扇窗**；
          · 本方法让窗口**自己给自己画一张像**，不经过屏幕 DC、**不受遮挡影响**、
            不需要窗口在前台。

        实现在 `driver/capture.py`（含黑图哨兵与窗口状态证据）。这里只负责
        「window_id → hwnd」这一步。

        ⚠️ **id 解析刻意不走 `apps.find_window`**：它只在 `window_rows()` 里找，而
        后者过滤掉了**无标题窗口**与外壳窗 —— 而弹窗菜单、浮层、tooltip 这类
        「正想单独截的界面」恰恰常常没有标题。这里直接认 hwnd，再用
        `is_window` + 有效矩形做校验，把可截范围放宽到「任何一扇真实存在的窗」。
        """
        hwnd = apps.parse_hwnd(window_id)
        if hwnd is None or not win32.is_window(hwnd):
            raise ComputerUseError(
                f"capture_window: 无效的 window_id={window_id!r}"
                f"（可用 list_windows 拿到 id，形如 0x1a2b3c）"
            )
        img, cinfo = capture.print_window(hwnd)
        # ⚠️ `state` 必须在**分支之前**取出来：它只跟着 `cinfo` 走，与抓没抓到无关 ——
        #    而"抓不到"那条分支（要拿状态解释原因）与"抓到了"那条（要拿矩形算原点）
        #    都要用它。真机上第一版把它写在 raise 分支里，于是**成功路径反而
        #    UnboundLocalError**（2026-09-30 真机抓到）。
        state = cinfo.get("state") or {}
        if img is None:
            # 抓不到就**如实报错**，绝不返回黑图 —— 让模型以为"界面就是这样"
            # 是本项目最不能容忍的失败形态，而它在这里的自然语言是「这界面是空白的」。
            raise ComputerUseError(
                f"按窗口截图失败（hwnd=0x{hwnd:x}）："
                f"{cinfo.get('reason') or '未渲染出内容'}"
                f"｜窗口状态={state}｜PrintWindow api_ok={cinfo.get('api_ok')}"
                f" 耗时={cinfo.get('ms')}ms"
            )
        rect = state.get("rect") or [0, 0, 0, 0]
        return img, (int(rect[0]), int(rect[1]))

    def screenshot_window(
        self, window_id: str, max_side: int | None = None,
        fmt: str = "jpeg", quality: int = 85,
    ) -> tuple[bytes, dict[str, Any]] | None:
        """
        按窗口渲染并编码。**比基类多带两样模型要用的东西**，故在这里覆写而不是靠默认实现：

          · `window_title` —— 「我截的是谁」；
          · `nonblack` / `print_ms` / `window_state` —— 「这张图拿得可不可信」
            （全黑 = 抓不到；耗时用来判断偶发卡住是不是 `PrintWindow`）。
        """
        out = super().screenshot_window(window_id, max_side=max_side, fmt=fmt,
                                        quality=quality)
        if out is None:
            return None
        data, meta = out
        hwnd = apps.parse_hwnd(window_id) or 0
        meta["window_title"] = win32.window_text(hwnd) if hwnd else None
        return data, meta

    def spawn_app(self, argv: list[str], env: dict[str, str]) -> dict[str, Any] | None:
        """
        启动应用。**隔离模式下起在隐藏桌面上**（用户看不见、也碰不到），
        `real` 模式下与老行为一致（起在用户桌面）。

        实现逻辑：确保目标桌面就绪 → `desktop.spawn`（自己拼 `STARTUPINFOW` 设
        `lpDesktop`）→ **记下 pid**（隔离模式下它是"该操作哪扇窗"的第一依据，
        因为没有前台窗口可问）。

        ⚠️ 隔离桌面**起不来**时**不静默降级到用户桌面** —— 那正是用户最不想要的
        （他明确选了隔离）。如实返回 `ok=False` 并把原因带上（`CreateDesktopW` 的
        errno 已经说明它为什么不高兴）。
        """
        if desktop.display_mode() != "isolated":
            # ★ `real` 模式**不接管**启动 —— 返回 None 让调用方走原来的
            #   `subprocess.Popen`。这一条是刻意的，不是省事：
            #   ① 用户显式选了 real（"就用老行为"），那条路不该被这次改动碰到；
            #   ② 真机实测 `CreateProcessW` 在**不带 `lpDesktop`** 时会卡住
            #      （2026-09-30 记录，见 `manual_windows_isolated_story.py` 的诊断节）——
            #      带桌面名时正常。既然 real 模式不需要它，就没有理由冒这个风险。
            desktop.use_default()
            return None
        else:
            ready = desktop.ensure_isolated()
            if not ready.get("ok"):
                return {"ok": False, "pid": 0, "target": None, "argv": argv,
                        "error": (f"隔离桌面不可用：{ready.get('error')}。"
                                  f"要直接在用户桌面上启动，请设 "
                                  f"CC_CU_DISPLAY_MODE=real 后重启 Claude Code。")}
        res = desktop.spawn(argv, env)
        if res.get("ok"):
            self._last_spawned_pid = int(res.get("pid") or 0)
            self._last_spawned_app = os.path.basename(argv[0]).lower()
            if self._last_spawned_app.endswith(".exe"):
                self._last_spawned_app = self._last_spawned_app[:-4]
        res["target"] = desktop.target_name() if res.get("ok") else None
        return res

    def active_window_id(self) -> str | None:
        """
        本次该操作的窗口 id；取不到返回 None（与 `list_windows` 的 id 同形状）。

        ⚠️ 隔离模式下走「目标窗口」推导而不是 `GetForegroundWindow()`（理由见
        `_foreground_hwnd`）—— 后者在隔离模式下指的是**用户的**窗口。
        """
        hwnd = self._foreground_hwnd()
        return f"0x{hwnd:x}" if hwnd else None

    def window_for_app(self, app: str) -> str | None:
        """
        按应用名找一个**顶层窗口** id（形状与 `list_windows` 的 `id` 一致）。

        ⚠️ 有多个窗口时取**面积最大**的那个：与应用同进程的常有一堆 10x10 的辅助窗
        （GTK 侧为此吃过一次亏，Windows 的 UWP 外壳窗同理），
        随手取第一个会让"按应用截图"截到一扇看不见的小窗，而**不报任何错**。

        ⚠️ 与 `apps.find_window` 不同之处：这里**不要求窗口有标题**（它走的是
        `window_rows`，会过滤掉无标题窗），而"想单独截的那扇"常常正好没有标题。
        """
        needle = (app or "").strip().lower()
        if not needle:
            return None
        best: tuple[int, int] = (0, 0)      # (面积, hwnd)
        for hwnd in win32.enum_top_level_windows():
            try:
                # app 口径与 `apps.app_names()` / 节点上的 `app` 字段必须**同一来源**，
                # 否则会出现"list_windows 里明明有这个应用，find 它就是找不到"——
                # 那一类问题没有报错、只是没结果，最难查。
                base = (win32.process_basename(hwnd) or "").lower()
                title = (win32.window_text(hwnd) or "").lower()
                if needle not in base and needle not in title:
                    continue
                r = win32.window_rect(hwnd)
                if not r:
                    continue
                area = max(0, r[2] - r[0]) * max(0, r[3] - r[1])
                if area > best[0]:
                    best = (area, hwnd)
            except Exception:  # noqa: BLE001 —— 单个窗口读失败不该让整次查找失败
                continue
        return f"0x{best[1]:x}" if best[1] else None

    def window_rect_of(self, window_id: str) -> Rect | None:
        """
        任意窗口的屏幕矩形（不只是活动窗口）。

        为什么 Windows 侧非要它不可：**隐藏桌面上的应用永远不是「活动窗口」**
        （前台属于用户桌面上的某个窗口）。按 id 找是唯一能拿到它几何的办法 ——
        而按窗口 OCR、落点证据、以及把窗口请到屏幕外再截，都要用这个矩形。
        """
        hwnd = apps.parse_hwnd(window_id)
        if hwnd is None or not win32.is_window(hwnd):
            return None
        r = win32.window_rect(hwnd)
        if not r:
            return None
        return Rect(x=r[0], y=r[1], w=r[2] - r[0], h=r[3] - r[1])

    def screenshot(self, region: tuple[int, int, int, int] | None = None,
                   max_side: int | None = None, fmt: str = "jpeg",
                   quality: int = 85) -> tuple[bytes, dict[str, Any]]:
        """
        截图，返回 (bytes, meta)。灰区兜底。

        Linux 侧的同名方法比这里多一步 `ring.clear_now()`（先把屏上的点击光圈抹掉）——
        光圈是 X11 专属的观感层，Windows 没有，故**这一步不存在**，而不是漏了。

        ⚠️⚠️ **隐藏桌面上这条会失败**（2026-09-28 实测，`:300` 起的隐藏桌面里
        `mss` 的 `BitBlt` 直接抛 `ScreenShotError`）。根因是**屏幕 DC 属于交互桌面**：
        非交互桌面上没有显示设备在渲染，从屏幕 DC 拷贝必然失败。
        **可用的替代品是 `PrintWindow(hwnd, PW_RENDERFULLCONTENT)`**（按窗口渲染，
        不经过屏幕 DC）—— 同一次实测里 `api_ok=True`、灰度极差 249，且拿它喂 OCR
        能认出刚写进去的文字。将来真做"隐藏桌面隔离"时，**抓屏这一路必须换成按窗口
        渲染**，`click_preview` / `change_fraction_since` 同理（它们都建立在同一张图上）。
        同一次实测还发现 `SendInput` 在隐藏桌面上返回 `sent: 0 / GetLastError=5`
        （ERROR_ACCESS_DENIED）—— 即**坐标级注入也不可用**，只有元素级 UIA 动作可用。
        探针与数据：`docs/PR/windows-driver/probe_hidden_desktop_input.py`。
        """
        img, origin = screen.grab_desktop(region)
        return screen.encode_image(img, origin, region=region,
                                   max_side=max_side, fmt=fmt, quality=quality)

    def click_preview(self, x: int, y: int) -> ClickPreview | None:
        """
        抓「点击前」以落点为中心的小图并画准星 —— 它是**四样反馈的总开关**。

        为什么它最要紧：消费方 `core/coordinator/landing.py` 里，本方法返回 None 会
        **连锁掐掉全部四样**反馈（预览图 / 落点文字 / 偏差数值 / 点后变化）。
        故它不是"锦上添花的一张图"，是整套坐标点击可信度的入口。

        平台相关的只有抓屏那一句；裁剪、准星、编码都在 `screen.build_click_preview`
        （它的 docstring 记着「准星像素 = 落点 − 裁剪后原点」为什么不能写死半宽）。
        """
        try:
            x, y = int(x), int(y)
            region = (x - screen.PREVIEW_HALF_W, y - screen.PREVIEW_HALF_H,
                      screen.PREVIEW_HALF_W * 2, screen.PREVIEW_HALF_H * 2)
            img, origin = screen.grab_desktop(region)
            return screen.build_click_preview(img, origin, x, y, region=region)
        except Exception as exc:  # noqa: BLE001 —— 预览图是附加证据，绝不拖垮点击
            log.debug("click_preview 失败（不影响点击）: %s", exc)
            return None

    def change_fraction_since(self, region: tuple[int, int, int, int],
                              before: Any) -> float | None:  # noqa: ANN401
        """
        再抓同一块并与 before（**原始图**，不是画过准星的那张）比较，返回变化像素占比；
        任何失败返回 None —— 证据不得拖垮点击。
        """
        if before is None:
            return None
        try:
            after, _origin = screen.grab_desktop(tuple(int(v) for v in region))
        except Exception as exc:  # noqa: BLE001
            log.debug("点后抓屏失败（跳过变化对比）: %s", exc)
            return None
        try:
            return screen.changed_fraction(before, after)
        except Exception as exc:  # noqa: BLE001
            log.debug("变化对比失败: %s", exc)
            return None
