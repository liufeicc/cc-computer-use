"""
平台抽象接口（Backend ABC）+ 跨层数据类。

设计原则：
  - 上层（core/coordinator、tools）只依赖本模块的抽象与数据类，与具体平台无关。
  - Linux（AT-SPI + xdotool）实现一份；Windows（UIA + SendInput）第二阶段实现。
  - 所有几何统一用屏幕绝对坐标（Rect），相对→绝对的校准在 core/geometry 完成。
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Rect:
    """屏幕矩形（绝对坐标，像素）。"""

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    @property
    def center(self) -> tuple[int, int]:
        """矩形中心点 (cx, cy)。"""
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def area(self) -> int:
        return self.w * self.h

    def is_empty(self) -> bool:
        """
        尺寸非法 / 读矩形失败（**不是**「面积很小」的意思，M-19）。

        ⚠️ 判据是 `w <= 0 or h <= 0`，所以 **1x1 的隐形辅助窗口不算空**——它的
        中心点仍是合法坐标，只是多半点不出东西。这里刻意不改成 `<= 1`：本方法同时
        被当作「能不能算出中心点去注入」的闸门（`coordinator.click` / `get_screen_text`
        的区域兜底）与「这个 SCREEN 矩形可不可信」的判据（`geometry.screen_rect_is_suspicious`），
        把 1x1 判空会在**后者**连带改变校准决策；要过滤极小窗口请在窗口枚举那一层做
        （`inject.list_windows` / `window_screen_pos_by_pid` 取面积最大者，已天然排除）。
        """
        return self.w <= 0 or self.h <= 0

    def to_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def __str__(self) -> str:
        return f"({self.x},{self.y},{self.w}x{self.h})"


@dataclass
class UINode:
    """无障碍树上的一个节点（用于序列化整棵树）。"""

    role: str = ""
    name: str = ""
    rect: Rect | None = None
    actions: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    value: Any = None
    ref: int | None = None
    app: str = ""
    depth: int = 0
    children: list["UINode"] = field(default_factory=list)
    # 原生平台对象（如 AT-SPI Accessible）。不参与文本序列化，仅供 ref 注册时
    # 把 ref 映射到「活对象」，以便后续 do_action 直接操作。同进程内有效。
    native: Any = None

    # 注意（M-22，见 docs/REVIEW/review_0.1.0.md）：这里曾有一个 `interactive` property，
    # 实现为 `bool(self.actions)`，**与 serializer 的 is_actionable 是两套判定且已漂移**
    # （后者多一条 `role in ACTIONABLE_ROLES`，是前者的超集）。它全仓零调用方，已删除。
    # ⚠️ 若将来要合并这两套判定，方向必须是「让 UINode 委托 serializer」，**不能反过来**
    # 让 serializer 复用这个弱判据 —— 那会静默丢掉 role 分支，导致大面积元素不再分配 ref。


@dataclass
class TextBlock:
    """
    OCR 识别出的一块屏幕文本（灰区应用的「元素」替身）。

    为什么要有它：SWT/Java、自绘控件、游戏、远程桌面没有无障碍树（或树一读就崩），
    只能靠截图让模型自己找按钮——而模型从图像里估坐标既慢又易错。OCR 直接把
    「文字 + 屏幕绝对坐标」交给模型，等价于给灰区应用补了一张「元素表」。

    它不是无障碍对象，因此：
      - element_screen_rect() 直接返回自带的 rect（识别时就算好了，不需要校准）；
      - invoke() / set_value() 一律返回 False —— 它没有元素级动作，让 coordinator
        正常降级到坐标点击，这正是我们要的路径。
    """

    text: str
    rect: Rect
    conf: float = 0.0


@dataclass
class ClickPreview:
    """
    一次坐标点击的「预览图」三件套。

    为什么要把**原始图**也带上（`image`）：同一块像素在这一刻会被用三次——
      ① 编码成带准星的 JPEG 发给模型（`data`/`meta`）；
      ② 做 OCR（读落点文字 / 最近候选 / 期望目标的位置）；
      ③ 作为「点前参照图」，与点后同一块再抓一次做像素对比（判断界面有没有响应）。
    ② 若改用「按 region 再抓一次」，就是同一份像素抓两遍；③ 若用 ① 那张（**画过准星**），
    红十字会被当成「变化」计入，让每次点击都凭空多出几个百分点。故原始图必须留在内存里，
    但它**只在本进程内流转**，绝不进 meta（meta 是要拼进模型上下文的）。
    """

    data: bytes                 # 带准星的 JPEG（给模型看）
    meta: dict[str, Any]        # 附带元信息（region/origin/crosshair/scale…）
    image: Any = None           # 原始 PIL 图（**不含准星**，仅进程内复用）


@dataclass
class Element:
    """轻量候选元素（find_element 返回，含 ref 供后续操作）。"""

    ref: int
    role: str
    name: str
    app: str = ""
    rect: Rect | None = None
    actions: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        r = str(self.rect) if self.rect else "(无矩形)"
        a = ",".join(self.actions) if self.actions else "(无动作)"
        return f"[{self.ref}] {self.role} | {self.name} | app={self.app} | rect={r} | actions={a}"


@dataclass
class ElementDetail(Element):
    """元素详情（element_info 返回，补充 value/states）。"""

    value: Any = None
    states: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        base = super().to_text()
        return f"{base}\n     value={self.value!r} states={','.join(self.states) or '(无)'}"


@dataclass
class QueryResult:
    """
    带「结果完整性说明」的查询返回（get_tree / find 统一用它）。

    为什么需要这个字段（I-4 + I-9，见 docs/REVIEW/review_0.1.0.md）：
      backend 的遍历有**三重上限**——节点数、触及的应用数、窗口数。命中任一上限，
      返回的结果就是**不完整的**。历史实现只 `log.warning`，模型看不到，于是它会把
      「被截断」误读成「桌面上没有这个东西」，转而走截图这条昂贵得多的路（实测截图
      是会话成本的大头）。而项目的明文要求是「截断**必带提示**，不静默截断」。

    故上限触发必须**随返回值一起上去**，由 coordinator 拼进工具输出。

    字段：
      items:  实际结果（UINode 森林 或 原生元素列表）
      notice: 非空 = 结果不完整/被降级，**必须**原样回给模型（含「下一步该怎么做」）
    """

    items: list[Any] = field(default_factory=list)
    notice: str = ""


class Backend(ABC):
    """平台后端抽象契约。"""

    name: str = "abstract"

    @abstractmethod
    def is_available(self) -> bool:
        """当前平台/会话是否可用（AT-SPI 能加载、无障碍开关已开等）。"""

    @abstractmethod
    def list_apps(self) -> list[str]:
        """列出有窗口的应用名。"""

    @abstractmethod
    def get_tree(
        self, scope: str = "active_window", app: str | None = None,
        max_depth: int = 8, max_nodes: int = 300,
    ) -> QueryResult:
        """
        读取无障碍树，返回 QueryResult（items 为 UINode 森林；scope: active_window/app/desktop）。

        ️ `max_nodes` 是**整次调用**的节点上限（不是每窗口）：命中即停止展开，并在
        `notice` 里说明——模型必须知道结果不完整，不能把「被截断」当成「没有」。

        ⚠️ `scope` 只接受 `active_window` / `app` / `desktop`；非法值或
        `scope="app"` 未给 `app` 时**抛 `ComputerUseError`**（M-26）——历史实现会静默
        换成另一种 scope 的结果，模型拿到的东西与它要的不是一回事却无从察觉。
        """

    @abstractmethod
    def find(
        self, text: str | None = None, role: str | None = None,
        app: str | None = None, interactive_only: bool = True,
        max_depth: int = 10, limit: int = 40,
    ) -> QueryResult:
        """
        搜索元素，返回 QueryResult（items 为平台原生元素对象，由 coordinator 注册 ref）。

        ⚠️ 不带 `app` 时会遍历桌面上多个应用，受应用数/节点数上限约束，命中即在
        `notice` 里说明「结果只覆盖了部分应用」。
        """

    @abstractmethod
    def element_info(self, native: Any) -> ElementDetail:
        """读取单个原生元素的详情（不含 ref，ref 由上层填）。"""

    @abstractmethod
    def is_alive(self, native: Any) -> bool:
        """
        原生元素是否仍可寻址（=「这个 ref 还有效吗」）。

        为什么登记在 ABC 而不是让上层自己摸实现细节：平台无关层原本写的是
            role = backend.reader.role_name(native) if hasattr(backend, "reader") else None
        ——`reader` 是 Linux 实现的**私有属性**，而 `hasattr` 哨兵会让别的平台**静默失去**
        这项能力（不报错、不失败，只是「悄悄不做」），偏偏它正是 ref 失效判定与「按 meta
        重定位」的唯一入口。登记成抽象方法后，任何 backend 都必须对这个问题表态。

        语义刻意做成「存活」而不是暴露 AT-SPI 的 `role_name`：上层要的本来就是「这个元素
        还能不能用」，而「role」是 AT-SPI 特有的概念，不该泄漏进平台无关层（那正是本 ABC
        存在的意义）。返回 False 表示「用不了」，上层据此决定是否尝试重定位。

        ⚠️ TextBlock（OCR 文本块）是**纯数据对象**、不存在「被销毁」，调用方
        （`coordinator._native_alive`）会**直接判活、不经过本方法**。那是一条与平台无关的
        数据模型约定，所以刻意不放进任何 backend 实现里——否则换个平台就要重写一遍。
        """

    @abstractmethod
    def focus(self, native: Any) -> bool:
        """
        尝试让元素获得焦点；返回是否成功（后端不支持时返回 False）。

        用途：`type_text` 在元素级赋值**之前**先聚焦。这不是可有可无的修饰——元素级
        `set_value` 能成功的前提就是目标已获得焦点（实测：不聚焦时赋值会落到别处或无效）。
        同样因为它，绝不能再用 `hasattr(backend, "reader")` 兜：那样换平台后这步会
        **静默消失**，表现为「赋值偶发失败」这种极难反查的现象。
        """

    @abstractmethod
    def invoke(self, native: Any, action: str | None = None) -> bool:
        """元素级操作（首选）：action 为空时自动选 click/activate/press。"""

    @abstractmethod
    def set_value(self, native: Any, text: str) -> bool:
        """元素级赋值（文本框等）。"""

    @abstractmethod
    def element_screen_rect(self, native: Any) -> Rect | None:
        """元素屏幕绝对矩形（已校准），供坐标兜底使用。"""

    @abstractmethod
    def click_at(self, x: int, y: int, button: int = 1, focus_window: bool = True) -> bool:
        """坐标点击（兜底）。focus_window=True 时先激活/聚焦所在窗口。"""

    @abstractmethod
    def type_text(self, text: str) -> bool:
        """键盘输入文本。"""

    @abstractmethod
    def press_key(self, combo: str) -> bool:
        """快捷键，如 'ctrl+s'、'Return'。"""

    @abstractmethod
    def list_windows(self, limit: int = 100) -> list[dict[str, Any]]:
        """列出可见窗口 [{id,title,pid,x,y,w,h,area}]（按面积降序），用于窗口甄别/定位。"""

    @abstractmethod
    def wait_window(
        self, title_contains: str | None = None, window_id: str | None = None,
        timeout: float = 10.0, poll: float = 0.25,
    ) -> dict | None:
        """等待窗口标题满足条件（内部轮询）；超时返回 None。"""

    @abstractmethod
    def window_screen_pos(self, native: Any) -> Rect | None:
        """元素所属顶层窗口的屏幕矩形（坐标校准用）。"""

    def window_screen_pos_with_quality(self, native: Any) -> tuple[Rect | None, str]:
        """
        同 window_screen_pos，但额外回报「这个校准基准有多可信」（M-16 观察点）。

        为什么需要（M-16）：坐标校准的基准 = 元素所属**顶层窗口**的屏幕矩形。而按 pid 查
        窗口几何时，若该顶层窗口**没有标题**（弹出菜单 / 下拉浮层 / tooltip 的顶层窗通常无名），
        就只能退化成「该 pid 下面积最大的窗口」——那多半是应用主窗口，不是浮层本身。于是
        「窗口原点 + 元素相对坐标」算出来的绝对坐标整体偏移，而**当前的代码路径对此完全沉默**
        （`geometry.resolve_element_screen_rect` 照样返回 source='calibrated'）。

        本方法把这份不确定性显式暴露出来，供调用方在**几何确实可疑时**记一条可诊断的告警；
        不改任何校准决策——改校准策略需要真实无名弹窗的实测证据，不能凭推测动（见 REVIEW M-16）。

        返回 (rect, quality)，quality ∈ {"title", "pid_fuzzy", "unknown", "none"}；
        默认实现标 "unknown"：别的平台没有这个退化路径，不必实现。
        """
        return self.window_screen_pos(native), "unknown"

    @abstractmethod
    def screen_layout(self) -> dict[str, Any]:
        """显示器布局 + 尺寸（+ DPI 若可得）。"""

    @abstractmethod
    def screenshot(
        self, region: tuple[int, int, int, int] | None = None,
        max_side: int | None = None, fmt: str = "jpeg", quality: int = 85,
    ) -> tuple[bytes, dict[str, Any]]:
        """
        截图，返回 (图像字节, meta)。region=(x,y,w,h) 为空则全屏。

        max_side：长边上限，超出则**先缩放后编码**（None=不缩放）。必须是先缩放再编码
        ——历史实现是「全尺寸编码一次 → 解码 → 缩放 → 再编码」，同一张图压两遍，
        全尺寸那一次纯属浪费（实测 266ms）。
        fmt：'jpeg'（默认）或 'png'。JPEG 实测比 PNG 快 19 倍、体积小 9 倍，截图不需要无损。
        meta：含 width/height/bytes/format/resized/**origin**；被缩放时含 scale。
              **origin** = 图像像素 (0,0) 对应的屏幕绝对坐标（全屏抓的是显示器
              并集，其原点不保证是 (0,0)——副屏在主屏左/上时为负值），**scale** =
              图像像素 → 屏幕像素的倍数。两者合起来才是完整换算：
              屏幕坐标 = origin + 图上坐标 × scale。缺 origin 会整体偏一个屏宽。
        """

    @abstractmethod
    def active_window_rect(self) -> Rect | None:
        """
        当前活动窗口的屏幕绝对矩形；拿不到返回 None。

        用途：给「按窗口范围感知」的工具（OCR 取默认区域、区域截图）定边界——
        全屏 OCR 实测 8~10s，而一个对话框大小的区域只要 2~3s。
        """

    @abstractmethod
    def read_text(
        self, region: tuple[int, int, int, int] | None = None, min_conf: float = 40.0,
    ) -> list[TextBlock]:
        """
        OCR 读取一块区域的文本（灰区应用的感知通道），返回带屏幕坐标的文本块。

        region=(x,y,w,h) 屏幕绝对坐标；为空则由实现选默认范围（通常是活动窗口）。
        """

    @abstractmethod
    def active_window_title(self) -> str | None:
        """
        当前活动窗口标题；拿不到返回 None。

        用途：坐标点击/键盘注入之后回报「这一下打到哪个窗口去了」——
        焦点可能被别的应用抢走（实测有过把文本打进飞书/Remmina 的事故），
        标题是判断「有没有打偏」最便宜的信号（纯 X11 一次调用，约 10ms）。
        """

    @abstractmethod
    def describe_point(self, x: int, y: int, with_text: bool = False) -> dict[str, Any]:
        """
        回报屏幕坐标 (x,y) 处的「落点证据」：这是哪扇窗、底下写着什么字。

        为什么需要它：坐标级点击/键盘注入的返回值**只能说明「事件发出去了」**——
        xdotool 不关心你点到了什么，`ok=True` 不等于「点中了想要的东西」。模型为此
        只能再截一张图确认，而那是整整一个来回（实测每轮 30~70 秒，工具本身只占 0.2 秒）。
        本方法把「模型本来要从截图里看的那几条信息」直接以文本报回去，省掉该来回。

        with_text=True 时额外 OCR 落点周围一小块，给出「落点文字」（目标按钮的标签）；
        它比整屏 OCR 便宜得多（只抓 300x60 上下），代价约 0.2~0.4 秒。

        返回（键缺失/取不到时为 None，不抛异常——落点证据是**附加信息**，
        拿不到也不能让点击本身失败）：
          {"x","y","window_id","window_title","window_rect","text","conf"}
        """

    def click_preview(self, x: int, y: int) -> ClickPreview | None:
        """
        以落点 (x,y) 为中心抓一张**点击前**的小图，作为「我瞄到哪了」的证据。

        为什么要有它：坐标点击的误差不是「执行偏差」（xdotool 指哪打哪），而是
        **「选的坐标不是想点的目标」**——而这件事只有画面能说清。落点文字（describe_point）
        覆盖得了「有字的目标」，覆盖不了纯图标/自绘控件；小图是给模型补的那只眼睛。

        ⚠️ 必须**在点击之前**调用：点完弹窗可能已关，画面上的证据反而丢了（与
        describe_point 的「点前取证据」同一条纪律）。

        为什么是「小裁剪」而不是整屏截图：整屏一张约 1500 token，会把本项目刻意消灭的
        「看图 → 估坐标」那条最贵回路重新引回来；480x300 只有 ~190 token，且**不缩放**
        （scale 恒为 1，图上 1 像素 = 屏幕 1 像素，模型零换算）。

        返回 `ClickPreview`（带准星的 JPEG + meta + 原始图）；meta 含 `{kind, format, x, y,
        region, origin, crosshair, scale, width, height, bytes}`——**origin 与 crosshair
        是模型换算屏幕坐标的依据**，`image` 则供本进程内做 OCR 与点前后对比。

        这是**可选能力**：默认实现返回 None（该平台没有这项能力，coordinator 正常少发一张
        图）。之所以不做成 @abstractmethod：`_StubBackend` 等测试替身与未实现的平台
        backend 会因缺少该方法而**无法实例化**（几十个用例当场全红），而「Linux 实现被悄悄
        删成默认 no-op」这一风险由测试 `test_linux_backend_actually_implements_click_preview`
        兜住。
        """
        return None

    def change_fraction_since(self, region: tuple[int, int, int, int], before: Any) -> float | None:
        """
        **再抓一次** `region` 并与 `before` 逐像素比较，返回「变化像素占比」（0.0~1.0）。

        用途：判断这一点到底有没有被界面响应——`ok=True` 只说明「事件发出去了」。
        它是「落点文字 / 偏差数值」之外的**独立第二信号**（不需要知道模型想点谁）：
        偏 45px 但界面变了 = 按钮热区比文字大、其实点中了；偏 45px 且毫无变化 = 该修坐标了。

        默认返回 None（平台没有该能力，coordinator 跳过这条证据）。
        """
        return None

    def read_text_from_image(self, img: Any, origin: tuple[int, int],
                             min_conf: float = 40.0) -> list[TextBlock]:
        """
        对**已经抓好的一张图**做 OCR（不重新抓屏），坐标以 origin 为图像左上角的屏幕位置。

        为什么需要：坐标点击已经为预览图抓过一次屏，落点文字/候选识别应当复用那张**原始**图
        （预览成品上画了准星，红线横穿文字会让 tesseract 变差），而不是再抓一次同一块像素。
        默认实现返回空列表（无 OCR 能力的平台据此正常降级为「没有文字线索」）。
        """
        return []

    def sync_at_spi_bus(self) -> None:
        """
        把 a11y 连接对齐到当前应连的 AT-SPI 总线（沙箱私有总线）。

        默认 no-op：只有 Linux 沙箱需要它（虚拟屏**不隔离** AT-SPI —— a11y 走会话
        D-Bus，共用宿主 registryd，遍历会打崩宿主 GNOME Shell；见 core/display.py）。
        Windows backend 没有这个概念，继承本默认实现即可。
        """


def get_backend() -> Backend:
    """
    平台工厂：返回当前平台对应的 backend 实例。

    实现逻辑：
      - Linux → LinuxBackend（AT-SPI + xdotool）。
      - 其它平台 → 抛 BackendUnavailableError（Windows 第二阶段实现）。
    """
    from ..utils.errors import BackendUnavailableError

    system = platform.system().lower()
    if system == "linux":
        from .linux.backend import LinuxBackend

        return LinuxBackend()
    raise BackendUnavailableError(
        f"当前平台 '{system}' 暂不支持（Phase 1 仅 Linux；Windows 在 Phase 3）"
    )
