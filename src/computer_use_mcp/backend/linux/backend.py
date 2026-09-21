"""
Linux backend：组合 AT-SPI 读取 + xdotool 注入 + 几何校准 + mss 截图，
实现 backend/base.py 的 Backend 抽象契约。

平台无关的上层（coordinator / tools）只通过 Backend 接口访问，
本类是 Linux/X11 的具体落地。Windows backend 在 Phase 3 另写一份实现同一契约。
"""

from __future__ import annotations

from typing import Any

# ⚠️ M-44：必须 `from ... import _bootstrap` 再用属性访问，**不能** `from ..._bootstrap
# import ATSPI_IMPORT_ERROR`——后者是**值快照**：在导入本模块的这一刻把当时的 None 绑进来，
# 之后 `import_atspi()` 对模块全局的重新赋值不会反映过来（实测确认）。当前恰好无影响
# （下面的 `reader.error or …` 右侧是死支），但 `_bootstrap` 的 docstring 把这两个标志
# 声明为「供 backend 优雅降级」的对外契约，快照会让那份契约悄悄失效。
from ... import _bootstrap
from ...core import geometry
from ...utils.errors import BackendUnavailableError, ComputerUseError
from ...utils.logging import get_logger
from ..base import Backend, ClickPreview, ElementDetail, QueryResult, Rect, TextBlock, UINode
from . import grab, ring
from .atspi import AtspiReader
from .inject import XdotoolInjector
from .ocr import OcrReader

log = get_logger(__name__)


class LinuxBackend(Backend):
    """AT-SPI + xdotool 实现。"""

    name = "linux-atspi-xdotool"

    def __init__(self) -> None:
        self.reader = AtspiReader()
        self.injector = XdotoolInjector()
        self.ocr = OcrReader()   # 灰区感知通道（tesseract），不可用时只在调用时报错

    # ---------- 可用性 ----------
    def is_available(self) -> bool:
        """AT-SPI 可读即视为可用（xdotool 缺失只影响坐标兜底，不阻断元素级主路径）。"""
        return self.reader.is_available()

    def ensure_available(self) -> None:
        if not self.is_available():
            raise BackendUnavailableError(
                "AT-SPI 不可用。请确认：①运行于 Linux/X11；②已开启无障碍开关 "
                "`gsettings set org.gnome.desktop.interface toolkit-accessibility true`；"
                f"③已安装 pygobject 与系统 Atspi typelib。底层错误: {self.reader.error or _bootstrap.ATSPI_IMPORT_ERROR}"
            )

    # ---------- 应用 / 树 ----------
    def list_apps(self) -> list[str]:
        """
        有窗口的应用名列表。

        走 X11（WM_CLASS）而非 AT-SPI —— 理由见 inject.list_app_names 的长注释：
        AT-SPI 版要对每个 application 节点调 get_child_count()，会逼 Chrome/Electron
        等重型应用惰性构建整棵无障碍树，把 at-spi2-registryd 与会话 D-Bus 打满，
        连带拖崩 GNOME Shell（实测事故）。

        注意：这里**不再退回 AT-SPI 实现**，哪怕 X11 通道不可用也只返回空列表——
        退回路径正是本次要根除的风险源，宁可给出空结果也不重新引入崩溃可能。
        """
        return self.injector.list_app_names()

    # 'desktop' / 无 app 的 'app' scope 会遍历所有应用，而**每碰一个应用就要构建它的
    # a11y 树**——必须设上限，否则等同于「把整个桌面的无障碍树一次性全拉起来」。
    # 数值取小：宁可少返回并告警，也不要为了「全」而把宿主的 GNOME Shell 拖崩。
    _TREE_APP_BUDGET = 8
    # 单个应用的窗口数上限：极端情况（多标签浏览器/IDE）一个应用能开出几十扇窗，
    # 每扇窗都是一次完整 build_tree。与节点上限配合，避免「一个应用吃光全部预算」。
    _TREE_WINDOW_BUDGET = 20

    def get_tree(
        self, scope: str = "active_window", app: str | None = None,
        max_depth: int = 8, max_nodes: int = 300,
    ) -> QueryResult:
        """
        读无障碍树，返回 QueryResult（items 为 UINode 森林）。

        scope:
          - 'active_window'：当前活动窗口（默认，最省 token）。走 pid 精确定位，
            只触碰 1 个应用。
          - 'app'：指定 app（名字关键词）的全部窗口。
          - 'desktop'：整个桌面所有有窗口应用（最大，**慎用**，受 _TREE_APP_BUDGET
            约束只覆盖前 N 个应用）。

        ⚠️ **max_nodes 是「整次调用」的节点上限，不是「每窗口」**（I-4 修）：
        本方法只建**一个** counter 并传给所有 build_tree，跨应用、跨窗口共享。
        历史实现让每棵树各建一个计数器，于是 desktop scope 的实际上限是
        `8 应用 × 全部窗口 × max_nodes`——无总量上限，且与工具描述承诺的语义不符。

        三重上限（应用数/窗口数/节点数）任一命中都会写进 notice，由 coordinator 拼进
        工具输出——**模型必须知道结果被截断了**，否则会把「没返回」误读成「桌面上没有」。

        性能与安全（血泪教训）：碰一个应用 = 让该应用构建它的整棵 a11y 树。
        Chrome/Electron 等重型应用单棵树上万节点，`desktop` scope 会把它们的树全部
        拉起来，瞬间打满 at-spi2-registryd 与会话 D-Bus，**连带拖崩 GNOME Shell**。
        需要范围请优先用 'app' 或 'active_window'，别用 'desktop' 探路。

        ⚠️ **非法 scope 一律抛 ComputerUseError**（M-26，见 docs/REVIEW/review_0.1.0.md）：
        历史实现不校验，于是 `scope="desktops"`（拼错）静默变成**活动窗口树**、
        `scope="app"` 不带 `app=` 静默变成**受 8 应用预算限制的整桌面遍历** ——
        两者都不是报错，而是给出**另一种**结果，模型无从察觉自己拿到的是别的 scope 的东西。
        工具层另有 `Literal[...]` 做第一道拦截；这里这道是守 ABC 契约，防别的调用方绕过工具层。
        """
        if scope not in ("active_window", "app", "desktop"):
            raise ComputerUseError(
                f"未知 scope={scope!r}（仅支持 active_window / app / desktop）"
            )
        if scope == "app" and not app:
            raise ComputerUseError(
                "scope='app' 必须同时给出 app=（应用名关键词）；"
                "否则会退化成受预算限制的整桌面遍历，拿到的不是你要的东西。"
                "想扫整个桌面请显式用 scope='desktop'。"
            )
        self.ensure_available()
        nodes: list[UINode] = []
        notices: list[str] = []
        # 一个 counter 贯穿本次调用的所有应用与窗口 —— 这才是「整次调用」的节点上限
        counter: dict[str, Any] = {"n": 0, "truncated": False}

        def _collect_windows(app_node: Any) -> None:
            """把一个应用的窗口逐个转成 UINode 树，共用 counter。"""
            aname = self.reader.get_name(app_node)
            n_win = self.reader.child_count(app_node)
            if n_win > self._TREE_WINDOW_BUDGET:
                notices.append(
                    f"⚠️ 应用「{aname}」窗口数达上限（{self._TREE_WINDOW_BUDGET}），"
                    f"只展开了前 {self._TREE_WINDOW_BUDGET} 扇窗口"
                )
                log.warning("get_tree: 应用 %s 窗口数 %d 超上限 %d，已截断",
                            aname, n_win, self._TREE_WINDOW_BUDGET)
            for w in range(min(n_win, self._TREE_WINDOW_BUDGET)):
                if counter["n"] >= max_nodes:
                    return
                win = self.reader.child_at(app_node, w)
                if win is not None:
                    nodes.append(
                        self.reader.build_tree(win, aname, max_depth, max_nodes,
                                               counter=counter))

        if scope == "desktop":
            # 注：这里原先是 `scope == "desktop" or (scope == "app" and not app)`。
            # 后半句在 M-26 加了前置校验后已**不可达**（那种组合在函数开头就抛了），
            # 故删掉以免误导——它看着像是在「兜住 app 缺失」，实际是静默改变语义的那条路。
            count = 0
            for a in self.reader.iter_apps():
                if count >= self._TREE_APP_BUDGET:
                    notices.append(
                        f"⚠️ 已触及应用数上限（{self._TREE_APP_BUDGET} 个），"
                        f"**返回的只是桌面上的部分应用**；请改用 app= 收窄范围"
                    )
                    log.warning(
                        "get_tree(scope=%s) 触及应用数达上限(%d)已截断，返回的只是"
                        "部分应用；请改用 app= 收窄范围", scope, self._TREE_APP_BUDGET)
                    break
                # child_count(a) 本身就是「触碰」（会触发该应用建树），故先计数再调用
                count += 1
                if self.reader.child_count(a) == 0:
                    continue
                _collect_windows(a)
                if counter["n"] >= max_nodes:
                    break
        elif scope == "app" and app:
            target = self.reader.find_app(app)
            if target is not None:
                _collect_windows(target)
        else:  # active_window
            # 同时取 title 与 pid：get_active_window 优先用 pid 精确定位应用，
            # 避免为找活动窗口而遍历桌面上每个应用的窗口（见其 docstring）。
            title = self.injector.active_window_title()
            pid = self.injector.active_window_pid()
            # window_pids_provider 是**懒加载**的（M-23）：上面的 pid 精确定位用不到它，
            # 只有退化到「逐个应用找标题」时才需要，故传方法本身而不是现算一个集合。
            win = self.reader.get_active_window(
                active_title=title, active_pid=pid, window_pids_provider=self.window_pids)
            if win is not None:
                nodes.append(self.reader.build_tree(win, self.reader._app_name_of(win),
                                                    max_depth, max_nodes, counter=counter))

        if counter["truncated"]:
            notices.append(
                f"⚠️ 节点数已达上限（max_nodes={max_nodes}），**这棵树不完整**；"
                f"请收窄 scope（用 app= 或 active_window）或调大 max_nodes"
            )
        return QueryResult(items=nodes, notice="\n".join(notices))

    # ---------- 搜索 / 详情 ----------
    def find(
        self, text: str | None = None, role: str | None = None, app: str | None = None,
        interactive_only: bool = True, max_depth: int = 12, limit: int = 40,
    ) -> QueryResult:
        """
        搜索元素，返回 QueryResult（items 为原生元素对象）。

        notice 会一路透传到工具输出：搜索结果被截断（应用数/节点数达上限）时，
        模型必须知道「这只是部分结果」，否则会把截断误读成「桌面上没有」。
        """
        self.ensure_available()
        root = self.reader.find_app(app) if app else None
        # 传**方法**而不是集合：带 app= 的搜索走 scoped 路径，预滤用不到，
        # 现取就白付一次 wmctrl（判据同 window_pids 的 docstring）。
        return self.reader.find(text=text, role=role, root=root,
                                interactive_only=interactive_only,
                                max_depth=max_depth, limit=limit,
                                window_pids_provider=self.window_pids)

    def element_info(self, native: Any) -> ElementDetail:
        """
        元素详情。

        TextBlock（OCR 文本块）不是无障碍对象，读不到 role/actions/states，
        这里就地构造一份等价详情（role 固定为 'text-block'），使 element_info(ref)
        对灰区应用的 ref 也能正常工作，而不是抛异常。

        ⚠️ `actions` **必须留空**（M-28②）：此处历史实现填了 `["click"]`，但 `invoke()`
        对 TextBlock **恒返回 False**（它只是个「文字 + 坐标」，没有控件语义）。模型看到
        「有 click 动作」就会去走元素级点击，拿到的是「元素级失败，坐标点击已执行」——
        凭空多一轮，还会怀疑是不是自己用错了 ref。**报不出的能力就不要报**。
        """
        if isinstance(native, TextBlock):
            return ElementDetail(ref=0, role="text-block", name=native.text,
                                 rect=native.rect,
                                 value=native.text,
                                 states=[f"ocr-conf={native.conf:.0f}"])
        self.ensure_available()
        return self.reader.make_detail(native)

    # ---------- 元素级操作（首选）----------
    def is_alive(self, native: Any) -> bool:
        """
        见 `Backend.is_alive`。

        AT-SPI 侧的判据：能读到角色名、且不是 `""` / `unknown` / `invalid` 即算活。
        `role_name` 自己会把异常吞成 `"unknown"`，这里再包一层 try 是兜它之外的意外
        （例如 native 根本不是 Accessible —— 那属于「用不了」，同样该判 False 而非抛出）。

        TextBlock 不走这里：调用方 `coordinator._native_alive` 已直接判活（它是纯数据
        对象，不存在被销毁），理由见 `Backend.is_alive` 的 docstring。
        """
        try:
            return self.reader.role_name(native) not in ("", "unknown", "invalid")
        except Exception:  # noqa: BLE001
            return False

    def focus(self, native: Any) -> bool:
        """
        见 `Backend.focus`。转发到 `AtspiReader.focus`（grab_focus / set_focus，
        退化到 focus / select 类 action）。

        TextBlock 返回 False：OCR 文本块没有可聚焦的控件（它只是个坐标+文字），
        返回 False 不会中断流程——`type_text` 只是「尝试先聚焦」，赋值成功与否由
        随后的 `set_value` 决定。
        """
        if isinstance(native, TextBlock):
            return False
        self.ensure_available()
        return self.reader.focus(native)

    def invoke(self, native: Any, action: str | None = None) -> bool:
        """
        元素级操作。

        TextBlock 没有元素级动作（OCR 只认得出字，认不出控件语义），一律返回 False ——
        这样 coordinator 会**正常降级到坐标点击**（它自带的 rect 就是屏幕绝对坐标），
        正是灰区该走的路径。
        """
        if isinstance(native, TextBlock):
            return False
        self.ensure_available()
        return self.reader.do_action(native, action)

    def set_value(self, native: Any, text: str) -> bool:
        """元素级赋值；TextBlock 无此能力 → False，由 coordinator 降级为键盘注入。"""
        if isinstance(native, TextBlock):
            return False
        self.ensure_available()
        return self.reader.set_value(native, text)

    # ---------- 几何 / 坐标兜底 ----------
    def window_screen_pos(self, native: Any) -> Rect | None:
        """
        元素所属顶层窗口的屏幕绝对矩形（坐标校准用）。

        实现逻辑：
          1. 回溯到顶层窗口节点。
          2. 取窗口标题 + 进程 PID，用 xdotool 按 PID 精确查窗口几何（demo 已验证最稳）。
          3. 退化：用窗口节点自身的 SCREEN 矩形。
        """
        return self.window_screen_pos_with_quality(native)[0]

    def window_screen_pos_with_quality(self, native: Any) -> tuple[Rect | None, str]:
        """
        见 `Backend.window_screen_pos_with_quality`（M-16 观察点）。

        与 window_screen_pos 同一条链路，只是把「按 pid 查窗口」的匹配质量一并带出来
        ——顶层窗口无名时那份基准是不可信的，见 base 里的说明。
        """
        self.ensure_available()
        top = self.reader.top_window_of(native)
        if top is None:
            return None, "none"
        title = self.reader.get_name(top)
        pid = self.reader.get_process_id(top) or self.reader.get_process_id(native)
        if pid:
            rect, quality = self.injector.window_screen_pos_by_pid_match(pid, title or None)
            if rect is not None:
                return rect, quality
        # 退化：窗口 SCREEN 矩形（同样说不清可信度）
        return self.reader.get_extents(top, "SCREEN"), "unknown"

    def element_screen_rect(self, native: Any) -> Rect | None:
        """
        元素屏幕绝对矩形。

        TextBlock 走快路径：它的 rect 是 OCR 阶段就算好的**屏幕绝对坐标**，
        不需要 geometry 校准（那套校准是给 GTK 的 SCREEN/WINDOW 相对坐标漂移用的，
        对 OCR 结果不适用、也不该套用）。
        """
        if isinstance(native, TextBlock):
            return native.rect
        self.ensure_available()
        screen = self.reader.get_extents(native, "SCREEN")
        rel = self.reader.get_extents(native, "WINDOW")
        win, quality = self.window_screen_pos_with_quality(native)
        rect, source = geometry.resolve_element_screen_rect(screen, win, rel)
        log.debug("element_screen_rect source=%s rect=%s quality=%s", source, rect, quality)
        # M-16 观察点：source='calibrated' 意味着「SCREEN 与窗口矩形对不上，故改用校准值」，
        # 而 quality='pid_fuzzy' 意味着「这份窗口基准是退化成『该 pid 面积最大的窗口』得来的」。
        # 两者同时成立时，一个很可能的情形是：元素其实在**无名弹层**里（顶层窗口无名 →
        # 基准取到了应用主窗口），于是校准出来的坐标整体偏移。这条路径历史上是**完全沉默**的，
        # 排查「坐标点偏」时无从下手，故在此留下一处可诊断的痕迹。
        # 刻意只记日志、不改决策：修正校准策略需要真实无名弹窗的实测证据（见 REVIEW M-16）。
        if source == "calibrated" and quality == "pid_fuzzy":
            log.warning(
                "坐标校准基准可疑：SCREEN(%s) 与窗口(%s) 不相交，故改用「窗口原点+相对坐标」"
                "校准，但该窗口是按 pid 取面积最大者（顶层窗口无标题或标题未命中）得来的——"
                "若该元素位于弹出菜单/下拉浮层等独立无名顶层窗内，基准会错成应用主窗口，"
                "校准坐标将整体偏移。元素相对矩形=%s", screen, win, rel,
            )
        return rect

    def click_at(self, x: int, y: int, button: int = 1, focus_window: bool = True) -> bool:
        """
        坐标点击（兜底）。focus_window=True 时先聚焦落点所在窗口（修复 demo 路径B）。

        点击**之前**先点亮一个红圈（`ring.show`，给人看）：它是纯可视化，入队即返回
        （微秒级），随后的 xdotool 注入（含 `--sync` 的聚焦，几十~上百毫秒）之后才真正
        落下点击——视觉上正是「这里要点 → 点了」。放在注入之后就晚了：注入抛异常时
        人看不到任何痕迹，而失败恰恰最需要知道它想点哪。
        ⚠️ 本方法是**全部坐标点击的唯一漏斗**，光圈挂在这里一处即全覆盖（新增坐标路径
        不可能漏画）；元素级 do_action 不经过这里，故天然不画（它零坐标、不存在「瞄哪」）。
        """
        if not self.injector.is_available():
            from ...utils.errors import InjectionError
            raise InjectionError("xdotool 不可用，无法执行坐标点击")
        ring.show(int(x), int(y))          # 永不抛、永不阻塞，失败只是看不见圈
        wid = None
        if focus_window:
            wid = self.injector.window_id_under(x, y)
        return self.injector.click_at(x, y, button=button, focus_wid=wid)

    # ---------- 键盘 ----------
    def type_text(self, text: str) -> bool:
        if not self.injector.is_available():
            from ...utils.errors import InjectionError
            raise InjectionError("xdotool 不可用，无法输入文本")
        return self.injector.type_text(text)

    def press_key(self, combo: str) -> bool:
        if not self.injector.is_available():
            from ...utils.errors import InjectionError
            raise InjectionError("xdotool 不可用，无法发送按键")
        return self.injector.press_key(combo)

    # ---------- 窗口清单 / 等待 ----------
    def window_pids(self) -> set[int]:
        """
        当前「有窗口的应用」pid 集合（走 X11 `wmctrl -lpx`，**零 a11y 成本**）。

        用途（M-23，见 docs/REVIEW/review_0.1.0.md）：`AtspiReader.get_active_window`
        的标题退化路径据此跳过无窗口应用 —— gnome-shell、输入法、注册器这类应用的
        `child_count` 恒为 0，却会**无条件吃掉** `_APP_TOUCH_BUDGET`，导致目标应用还没
        轮到就 break（表现为「找不到活动窗口」，只留一条日志）。
        `AtspiReader.find` 的全桌面路径同样据此预滤（同一个预算被同一批应用吃光的问题，
        表现是「按 text 找不到元素」）。

        接口做成零参方法、按**回调**传给 reader：精确定位路径根本用不到它，
        传集合会白付一次 `wmctrl`（约 5~10ms ×每次 get_tree），传方法则按需才取。
        """
        try:
            return {w["pid"] for w in self.injector.list_windows(limit=200) if w.get("pid")}
        except Exception as exc:  # noqa: BLE001 —— 滤不掉只是少层优化，绝不能拖垮主流程
            log.debug("取有窗口应用 pid 集合失败（本次不做预滤）: %s", exc)
            return set()

    def list_windows(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.injector.list_windows(limit=limit)

    def wait_window(
        self, title_contains: str | None = None, window_id: str | None = None,
        timeout: float = 10.0, poll: float = 0.25,
    ) -> dict | None:
        return self.injector.wait_window(title_contains=title_contains, window_id=window_id,
                                         timeout=timeout, poll=poll)

    # ---------- 屏幕布局 ----------
    def screen_layout(self) -> dict[str, Any]:
        return self.injector.screen_layout()

    # ---------- 截图兜底 ----------
    def screenshot(
        self, region: tuple[int, int, int, int] | None = None,
        max_side: int | None = None, fmt: str = "jpeg", quality: int = 85,
    ) -> tuple[bytes, dict[str, Any]]:
        """
        截图，返回 (bytes, meta)。灰区兜底。

        ⚠️ 编码**只做一次**（修历史坑）：旧实现是「backend 全尺寸 PNG 编码 → coordinator
        再 Image.open 解码 → 缩放 → 重新编码」，同一张图被压两遍，且全尺寸那一次完全白费
        （实测 3840x1200 下就是 266ms）。现在把缩放提到编码之前：抓屏 → 缩放 → 编一次。

        为什么默认 JPEG 而非 PNG（实测 3840x1200）：JPEG(q=70) 编码 14ms / 334KB，
        PNG(level=1) 266ms / 3065KB —— 快 19 倍、小 9 倍。截图是给人/模型看的，不需要
        无损。若确有需要可传 fmt="png"。
        """
        try:
            import io

            from PIL import Image
        except Exception as exc:  # noqa: BLE001
            raise BackendUnavailableError(f"截图依赖缺失(mss/pillow): {exc}") from exc

        # 先把可能还在屏上的点击光圈抹掉：`act_sequence` 里「click 步紧接 screenshot 步」
        # 只隔 ~250ms，而圈的寿命是 1.5s——不清理就会把红圈拍进图里，模型可能把它当成
        # 界面元素。无圈时这只是一次布尔判断，零成本。
        ring.clear_now()
        img, origin = grab.grab_rgb(region)
        orig_w, orig_h = img.size
        meta: dict[str, Any] = {
            "format": fmt, "region": list(region) if region else None,
            # ⚠️ origin = 图像像素 (0,0) 对应的**屏幕绝对坐标**（I-8）。全屏抓取抓的是
            # 显示器并集，其原点不保证是 (0,0)——副屏在主屏左/上时是负值。模型要靠
            # 「屏幕坐标 = origin + 图上坐标 × scale」换算，缺了 origin 就会整体偏一个屏宽。
            "origin": [origin[0], origin[1]],
            "source_size": [orig_w, orig_h],
        }
        if max_side and max(orig_w, orig_h) > max_side:
            ratio = max_side / max(orig_w, orig_h)
            new_size = (max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio)))
            # LANCZOS：缩小时保留文字边缘锐度，缩小后模型仍能看清小字
            img = img.resize(new_size, Image.LANCZOS)
            meta["resized"] = [img.width, img.height]
            # ⚠️ scale 是「图像像素 → 屏幕像素」的倍数（屏幕坐标 = 图上坐标 × scale）。
            # 少了它，模型只能**猜**这张图缩了多少，据此算出的落点必然偏——实测踩到过。
            meta["scale"] = orig_w / img.width
        meta["width"], meta["height"] = img.size

        buf = io.BytesIO()
        if fmt == "png":
            # 去掉 optimize=True（数百毫秒纯 CPU 浪费且肉眼无差异），低压缩等级快一个量级
            img.save(buf, format="PNG", compress_level=1)
        else:
            img.save(buf, format="JPEG", quality=quality)
        png = buf.getvalue()
        meta["bytes"] = len(png)
        return png, meta

    # ---------- 点击预览图（给模型看的「我瞄到哪了」）----------
    # 以落点为中心裁剪的半宽/半高（480x300）。为什么是这个尺寸：足够容纳「目标 + 周围一圈
    # 邻居」（判断瞄偏了多少往往要靠旁边的按钮做参照），而 token 只有整屏的 1/8（~190）。
    _PREVIEW_HALF_W = 240
    _PREVIEW_HALF_H = 150

    def click_preview(self, x: int, y: int) -> ClickPreview | None:
        """
        抓「点击前」以落点为中心的小图，画上准星，返回 `ClickPreview`。

        实现逻辑（每一步都有失败保护——预览图是**附加证据**，拿不到绝不能让点击失败）：
          1. region = 以 (x,y) 为中心 480x300；`grab_rgb` 内部会把它与屏幕并集求交
             （`_clamp_region`），故屏幕边缘的点击也不会越界报错。
          2. **准星像素 = 落点 − 裁剪后原点**（★ 本方法最容易写错的一行）：`_clamp_region`
             在屏幕边缘会改变原点，若图省事写死 `HALF_W`，则**越靠屏幕边缘的按钮准星指得
             越离谱**——而这个功能存在的唯一理由就是「位置对不对」，那就是主动误导级 bug。
          3. 落点不在返回图内（极端越界）→ 返回 None：宁可不发图，也不发一张指错的图。
          4. 准星画在**副本**上，原始图随 ClickPreview 一起返回：同一块像素后面还要用于
             OCR（红线横穿文字会降低识别率）与「点前后对比」（红线会被误当变化）。
          5. 编码 **只做一次**：抓 → 画 → 存 JPEG。**不缩放**（scale 恒为 1），故 meta 里
             的 `crosshair` 就是屏幕坐标换算的直接依据，模型不需要任何乘法。
             `subsampling=0`（4:4:4）是必须的：默认的 4:2:0 会把 1px 细红线的色度糊掉，
             模型看到一团粉雾、反而看不清十字中心落在哪个像素。
        """
        try:
            import io
        except Exception as exc:  # noqa: BLE001
            log.debug("click_preview: 依赖缺失 %s", exc)   # pragma: no cover - 环境级问题
            return None
        try:
            x, y = int(x), int(y)
            region = (x - self._PREVIEW_HALF_W, y - self._PREVIEW_HALF_H,
                      self._PREVIEW_HALF_W * 2, self._PREVIEW_HALF_H * 2)
            img, origin = grab.grab_rgb(region)
            px, py = x - origin[0], y - origin[1]
            if not (0 <= px < img.width and 0 <= py < img.height):
                log.debug("click_preview: 落点 (%s,%s) 不在裁剪图内 origin=%s size=%s",
                          x, y, origin, img.size)
                return None
            annotated = img.copy()          # 准星只画在副本上（原始图留给 OCR / 像素对比）
            grab.draw_crosshair(annotated, px, py)
            buf = io.BytesIO()
            annotated.save(buf, format="JPEG", quality=85, subsampling=0)
            data = buf.getvalue()
            meta: dict[str, Any] = {
                "kind": "click_preview", "format": "jpeg",
                "x": x, "y": y,
                "region": [region[0], region[1], region[2], region[3]],
                "origin": [origin[0], origin[1]],
                "crosshair": [px, py],          # 十字中心在**图上**的像素位置
                "scale": 1,                     # 未缩放：图上 1 像素 = 屏幕 1 像素
                "width": img.width, "height": img.height, "bytes": len(data),
            }
            return ClickPreview(data=data, meta=meta, image=img)
        except Exception as exc:  # noqa: BLE001
            log.debug("click_preview 失败（不影响点击）: %s", exc)
            return None

    def change_fraction_since(self, region: tuple[int, int, int, int], before: Any) -> float | None:
        """
        再抓同一块并与 before 比较，返回变化像素占比；任何失败返回 None（证据不得拖垮点击）。

        为什么对比的是 `before`（**原始图**，不是发给模型的那张）：发给模型的成品上画了
        红十字准星，拿它当参照会让**每次点击都凭空多出几个百分点的「变化」**——那正好是
        本信号要分辨的东西，绝不能被自己画的标记污染。
        """
        if before is None:
            return None
        # 先抹掉可能还在屏上的点击光圈（与 `screenshot()` 开头同源、同理由）：圈的寿命是
        # 1.5s，而本方法在点击后 ~0.2s 就被调用——圈**必然还在**。它是在点击之后才画的，
        # 拿它去跟点击前的 `before` 比，会凭空多出 536 个变化像素（约 0.37%），而「无变化」
        # 的阈值是 0.5%：眼下只是**侥幸**不误报，圈再大一点、裁剪区再小一点就会翻车。
        # 无圈时这只是一次布尔判断，零成本。
        ring.clear_now()
        try:
            after, _origin = grab.grab_rgb(tuple(int(v) for v in region))   # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            log.debug("点后抓屏失败（跳过变化对比）: %s", exc)
            return None
        try:
            return grab.changed_fraction(before, after)
        except Exception as exc:  # noqa: BLE001
            log.debug("变化对比失败: %s", exc)
            return None

    def read_text_from_image(self, img: Any, origin: tuple[int, int],
                             min_conf: float = 40.0) -> list[TextBlock]:
        """对已抓好的图做 OCR（复用预览图那次抓屏，不重复抓同一块像素）。"""
        return self.ocr.read_image(img, origin, min_conf=min_conf)

    # ---------- 灰区感知：OCR 文本层 ----------
    def active_window_rect(self) -> Rect | None:
        """当前活动窗口的屏幕绝对矩形（OCR 默认区域用它，避免全屏 8~10s 的开销）。"""
        wid = self.injector.active_window_id()
        if not wid:
            return None
        return self.injector.window_geometry(wid)

    def read_text(
        self, region: tuple[int, int, int, int] | None = None, min_conf: float = 40.0,
    ) -> list[TextBlock]:
        """OCR 读取一块区域，返回带屏幕坐标的文本块（灰区应用的感知通道）。"""
        return self.ocr.read(region, min_conf=min_conf)

    def sync_at_spi_bus(self) -> None:
        """
        把 a11y 连接对齐到沙箱私有 AT-SPI 总线（由 coordinator 在 ensure_started 后调用）。

        为什么需要：虚拟屏**不隔离 AT-SPI**（a11y 走会话 D-Bus）。若不切换，沙箱内应用的
        a11y 流量会落到宿主总线上，连带把宿主 GNOME Shell 打崩（2026-09-15 事故）。
        """
        self.reader.sync_bus()

    # ---------- 落点证据（省掉「再截一张图确认」的整个来回）----------
    def active_window_title(self) -> str | None:
        """当前活动窗口标题（纯 X11 一次调用，约 10ms）。"""
        return self.injector.active_window_title()

    def describe_point(self, x: int, y: int, with_text: bool = False) -> dict[str, Any]:
        """
        回报 (x,y) 处的落点证据：哪扇窗、底下什么字。

        实现逻辑（每一步都可失败，失败只是该项为 None，绝不抛出——落点证据是附加信息，
        拿不到不能让点击本身失败）：
          1. window_id_under(x,y)：内部会 mousemove 到该点再读 getmouselocation 的
             WINDOW 字段。**副作用要注意**：指针被挪到 (x,y)。调用方一律在点击**之前**
             取证据，此时指针本来也要去那儿，故无害。
          2. 由 wid 取标题与几何（两次 xdotool 调用）。标题是判断「有没有打偏」的主信号。
          3. with_text=True 时再 OCR 落点周围一小块，挑出**覆盖落点或离它最近**的那块文字
             ——那正是「我本来要点的那个东西叫什么」。
             ⚠️ 只抓 320x64 的小块，不用整屏（整屏 OCR 实测 8~10s，小块约 0.2~0.4s）。
        """
        out: dict[str, Any] = {
            "x": int(x), "y": int(y), "window_id": None, "window_title": None,
            "window_rect": None, "text": None, "conf": None,
        }
        try:
            wid = self.injector.window_id_under(int(x), int(y))
        except Exception as exc:  # noqa: BLE001
            log.debug("describe_point: 取落点窗口失败 %s", exc)
            wid = None
        if wid:
            out["window_id"] = wid
            try:
                out["window_title"] = self.injector.window_title(wid)
            except Exception as exc:  # noqa: BLE001
                log.debug("describe_point: 取窗口标题失败 %s", exc)
            try:
                r = self.injector.window_geometry(wid)
                out["window_rect"] = list(r.to_tuple()) if r else None
            except Exception as exc:  # noqa: BLE001
                log.debug("describe_point: 取窗口几何失败 %s", exc)

        if with_text:
            blk = self._text_near(int(x), int(y))
            if blk is not None:
                out["text"], out["conf"] = blk.text, blk.conf
        return out

    # 落点文字探测框的半宽/半高（像素）。取这么小是为了让 tesseract 只处理一行字，
    # 既快（~0.3s）又准（框越大越容易把旁边的图标并进同一行，正是 OCR 已知的失败模式）。
    _POINT_TEXT_HALF_W = 160
    _POINT_TEXT_HALF_H = 32

    def _text_near(self, x: int, y: int) -> TextBlock | None:
        """
        在落点周围的一小块里 OCR，返回覆盖该点（或离它最近）的文本块。

        挑选规则：优先「矩形包含落点」的块；没有则取中心距落点最近、且在
        _POINT_TEXT_HALF_H 以内的块（阈值防住「框里只有远处的无关文字」被误当成落点文字）。
        """
        box = (x - self._POINT_TEXT_HALF_W, y - self._POINT_TEXT_HALF_H,
               self._POINT_TEXT_HALF_W * 2, self._POINT_TEXT_HALF_H * 2)
        try:
            blocks = self.read_text(box)
        except Exception as exc:  # noqa: BLE001
            log.debug("_text_near: OCR 失败 %s", exc)
            return None
        if not blocks:
            return None
        containing = [b for b in blocks if b.rect.x <= x <= b.rect.x + b.rect.w
                      and b.rect.y <= y <= b.rect.y + b.rect.h]
        if containing:
            return containing[0]
        best, best_d = None, None
        for b in blocks:
            cx, cy = b.rect.center
            d = (cx - x) ** 2 + (cy - y) ** 2
            if best_d is None or d < best_d:
                best, best_d = b, d
        if best is not None and best_d is not None and best_d <= self._POINT_TEXT_HALF_H ** 2:
            return best
        return None
