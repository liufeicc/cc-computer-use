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

═══ 本轮已实现 / 未实现（诚实边界）═══

已实现：UIA 能覆盖的一切 —— 读树、搜索、元素级动作、坐标注入、窗口枚举，
以及**抓屏与点击反馈**（`screenshot` / `click_preview` / `change_fraction_since`）。

⚠️ 这三样的平台相关部分**只有「抓哪块屏」这一件事**，而 Windows 没有 DISPLAY 这个概念，
所以像素活全部复用平台中立的 `backend/screen.py`（`mss` 本来就跨平台，Windows 侧走 GDI，
且 `pyproject.toml` 早已声明 mss/pillow 为依赖）。**不要再写"抓屏需要独立的平台 API"**——
那句话是初版的错误判断，实测不成立。

**未实现**：`read_text`（OCR）。它抛明确错误而不是返回空 —— 返回空会被读成
"界面上没有文字"，把"功能没做"伪装成"没找到"，是本项目最忌讳的静默。
"""

from __future__ import annotations

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
from .driver import _uia, actions, apps, nodes, session_info, win32

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
_TREE_DEADLINE_S = 12.0
# find 的墙钟上限（搜索比整树遍历更容易失控：它要下钻到叶子）
_FIND_DEADLINE_S = 10.0


class WindowsBackend(Backend):
    """UIA（uiautomation）+ SendInput 的 Windows 后端。"""

    name = "windows"

    def __init__(self) -> None:
        # 刻意**不在 __init__ 里** import uiautomation / 建 COM 对象：
        # 构造发生在 `get_backend()`，而那是任何工具调用都会走的路径；
        # 把重活推迟到真正要用时（首次 `_uia()`），失败面也更容易定位。
        self._uia_mod: Any = None
        self._error: str = ""

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

        roots, notices = self._roots_for(scope, app)
        forest: list[UINode] = []
        counter = {"n": 0}
        deadline = time.monotonic() + _TREE_DEADLINE_S

        for label, element in roots:
            if counter["n"] >= max_nodes:
                break
            forest.append(self._build_tree(label, element, 1, max_depth,
                                           max_nodes, counter, deadline))
            if time.monotonic() > deadline:
                notices.append(
                    f"⚠️ 读树已达**墙钟上限**（{_TREE_DEADLINE_S:.0f}s，本平台遍历约 "
                    f"13~21ms/节点）：结果不完整，请缩小范围（用 scope='active_window'")
                break
        if counter["n"] >= max_nodes:
            notices.append(
                f"⚠️ 节点数已达上限（{max_nodes}），树被截断；"
                f"需要更多请调大 max_nodes 或缩小 scope。")
        return QueryResult(items=forest, notice="　".join(notices))

    def _roots_for(self, scope: str, app: str | None) -> tuple[list[tuple[str, Any]], list[str]]:
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
            picked = picked[:_APP_TOUCH_BUDGET]
            return [(r["app"] or r["title"], self._element_of(r)) for r in picked], notices

        # desktop：受应用数预算约束
        seen_apps: set[str] = set()
        out: list[tuple[str, Any]] = []
        for row in rows:
            key = row["app"] or row["class"]
            if key in seen_apps:
                continue
            if len(seen_apps) >= _TREE_APP_BUDGET:
                notices.append(
                    f"⚠️ 桌面应用数已达上限（{_TREE_APP_BUDGET}），只覆盖了前 "
                    f"{_TREE_APP_BUDGET} 个应用；需要别的应用请用 scope='app' 指定。")
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
        return win32.foreground_window()

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

        if app:
            roots = [(r["app"] or r["title"], self._element_of(r))
                     for r in apps.window_rows(limit=1000)
                     if (app.lower() in (r["app"] or "").lower()
                         or app.lower() in (r["title"] or "").lower())][:_APP_TOUCH_BUDGET]
        else:
            roots = [(r["app"] or r["class"], self._element_of(r))
                     for r in apps.window_rows(limit=_APP_TOUCH_BUDGET)]

        hits: list[Element] = []
        notices: list[str] = []
        deadline = time.monotonic() + _FIND_DEADLINE_S
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
        return apps.active_window_title()

    def active_window_rect(self) -> Rect | None:
        r = apps.active_window_rect()
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

    def click_at(self, x: int, y: int, button: int = 1, focus_window: bool = True,
                 repeat: int = 1, delay_ms: int = 100) -> bool:
        """
        坐标级点击（**兜底路径**）。

        ⚠️ 双击靠 `repeat=2` + `delay_ms` 交给输入层定时，**不要**让调用方连调两次：
        两次独立调用之间夹着落点证据、OCR、变化对比（实测约 0.5s），
        必然超过系统双击阈值而被认成两次单击 —— X11 侧为此吃过一次亏，Windows 同理。
        """
        hwnd = win32.foreground_window() if focus_window else 0
        return bool(win32.send_mouse_button(
            x, y, button=button, repeat=repeat, delay_ms=delay_ms,
            focus_window=focus_window, hwnd=hwnd).get("ok"))

    def drag_at(self, x1: int, y1: int, x2: int, y2: int, button: int = 1,
                steps: int = 10, focus_window: bool = True) -> bool:
        """
        坐标级拖拽。**必须插值分步**（原因见 `win32.send_mouse_drag`）。

        失败**不重试**：重试一次拖拽 = 再拖一遍（文件移动两次、画两笔），后果不对称。
        """
        if focus_window:
            fg = win32.foreground_window()
            if fg:
                win32.set_foreground_window(fg)
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
        **本轮未实现** —— OCR 是独立的下一轮切片。

        引擎已经定了：**tesseract**（复用 Linux 上已调优的那条管线——TSV 解析、上采样、
        PNG `compress_level=1`、`OMP_THREAD_LIMIT=1` 限单线程）。`ocr.py` 的平台耦合只有
        `display.env_for()` 一处，故移植成本不高；它没做，是因为**本轮的边界是
        「把点击反馈这条链闭死」**，而 OCR 会引入一个新的「用户需自行安装」项。

        ⚠️ 抛错而不是返回空列表：返回空会被读成「这个界面上没有文字」，
        把「功能没做」伪装成「没找到」。
        """
        raise BackendUnavailableError(
            "Windows 后端的 read_text（OCR）尚未实现（下一轮的独立切片）。"
            "当前可用的感知通道是 UIA 无障碍树：get_ui_tree / find_element / element_info。"
        )

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
            hwnd = win32.window_from_point(x, y)
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

    def screenshot(self, region: tuple[int, int, int, int] | None = None,
                   max_side: int | None = None, fmt: str = "jpeg",
                   quality: int = 85) -> tuple[bytes, dict[str, Any]]:
        """
        截图，返回 (bytes, meta)。灰区兜底。

        Linux 侧的同名方法比这里多一步 `ring.clear_now()`（先把屏上的点击光圈抹掉）——
        光圈是 X11 专属的观感层，Windows 没有，故**这一步不存在**，而不是漏了。
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
