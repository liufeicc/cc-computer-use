"""
UIA 元素 → 本项目 UINode/ElementDetail 的映射（backend.windows.driver.nodes）。

═══ 这一层要解决的核心问题 ═══

`core/serializer.py` 与 `backend/base.py` 的角色词表（`INTERACTIVE_ROLES` /
`ACTIONABLE_ROLES` / `NOISE_ROLES`）是照着 **AT-SPI 的角色名**定的 ——
全是 `"push button"` / `"entry"` / `"list item"` 这种小写串。

Windows 的 UIA 给的是另一套：`ControlTypeName`（`ButtonControl` / `EditControl` …）。
所以本模块的**唯一职责**是把后者翻译成前者，让 `core/` **一行都不用改**。
翻译错了不会报错，只会让某类控件不再分配 ref、不再参与 `interactive_only` 过滤 ——
表现是"模型找不到那个按钮"，而排查时完全看不出是角色翻译的问题。

═══ ★ 为什么必须 ControlType + ClassName 联合判定 ═══

真机 DEMO 的血证：**同一个「按钮」，经典 Win32 应用（走 MSAA 桥）报 `Pane`，
UWP 原生 provider 报 `Button`**。只看 ControlType 的话，前者会被翻译成
`"panel"`（在 `NOISE_ROLES` 里）→ 无名字时**直接被丢弃**，按钮就从树上消失了。

故判定顺序是刻意的：

    1. **先看 `ClassName`**（经典 Win32 控件类名是精确的、且跨版本稳定）
    2. 再看 `ControlType`（UWP/WinUI3 应用靠它，因为类名全是 `Microsoft.UI.Xaml.*`）
    3. 都不命中 → `"unknown"`

反过来（先 ControlType）会让"退化成 Pane 的经典控件"全部翻车。

═══ 关于动作词表 ═══

Linux 侧 `actions` 来自 AT-SPI 的 action 名（**随语言本地化**，例如中文环境是「点击」）。
Windows 侧我们**自己定一套 ASCII 规范名**（见 `ACTION_*` 常量），
理由有二：① 本地化名不可靠，判据不能建立在它上面；② 我们的 `invoke()` 本就要把
动作名映射到 UIA Pattern，多一层本地化名字典纯属自找麻烦。
与 AT-SPI 相同的那些（`click` / `activate` / `press`）保持同名，方便跨平台阅读。
"""

from __future__ import annotations

from typing import Any

from ...base import ACTIONABLE_ROLES
from . import _uia

# ── 我们自己定的动作规范名（见模块 docstring 的说明）──────────────────────
ACTION_INVOKE = "invoke"          # → InvokePattern
ACTION_SELECT = "select"          # → SelectionItemPattern
ACTION_EXPAND = "expand"          # → ExpandCollapsePattern.Expand
ACTION_COLLAPSE = "collapse"      # → ExpandCollapsePattern.Collapse
ACTION_SET_VALUE = "set_value"    # → ValuePattern / RangeValuePattern
ACTION_TOGGLE = "toggle"          # → TogglePattern
ACTION_FOCUS = "focus"            # → SetFocus（不是 Pattern，但我们对外的统一动作）
#
# ⚠️ 刻意**没有** ACTION_SCROLL：`actions.py` 没实现滚动 Pattern 的调用，
#    报出来等于宣称一个我们做不到的动作（滚动有专门的坐标级工具）。
#    将来要加 Element 级滚动，先补 `actions.py` 的调用，再把它加回动作表。

# ── ClassName → 角色（精确、跨版本稳定，**优先于 ControlType**）────────────
#
# 只列"ControlType 可能退化"的那些经典控件。值一律取 AT-SPI 的角色词表，
# 这样 `INTERACTIVE_ROLES` / `ACTIONABLE_ROLES` / `NOISE_ROLES` 原样可用。
_CLASS_ROLES: dict[str, str] = {
    "button": "push button",
    "static": "label",
    "edit": "entry",
    "richedit": "text",
    "richedit20w": "text",
    "richedit50w": "text",
    "richeditd2dpt": "text",          # Win11 记事本的编辑控件（实测就是这个类名）
    "combobox": "combo box",
    "comboboxex32": "combo box",
    "syslistview32": "list",
    "systreeview32": "tree",
    "systabcontrol32": "page tab list",
    "sysheader32": "table column header",
    "systoolbar32": "tool bar",
    "msctls_trackbar32": "slider",
    "msctls_progress32": "progress bar",
    "msctls_statusbar32": "status bar",
    "msctls_updown32": "spin button",
    "scrollbar": "scroll bar",
    "#32770": "dialog",               # 经典 Win32 对话框（winver 就是它）
    "tooltips_class32": "tool tip",
    "syslink": "hyperlink",
    "listbox": "list",
}

# ── ControlTypeName → 角色（UWP / WinUI3 走这条）──────────────────────────
_CONTROL_TYPE_ROLES: dict[str, str] = {
    "ButtonControl": "push button",
    "SplitButtonControl": "push button",
    "CheckBoxControl": "check box",
    "RadioButtonControl": "radio button",
    "ComboBoxControl": "combo box",
    "EditControl": "entry",
    "DocumentControl": "text",
    "TextControl": "label",
    "ListControl": "list",
    "ListItemControl": "list item",
    "MenuControl": "menu",
    "MenuBarControl": "menu bar",
    "MenuItemControl": "menu item",
    "TreeControl": "tree",
    "TreeItemControl": "tree item",
    "TabControl": "page tab list",
    "TabItemControl": "page tab",
    "ToolBarControl": "tool bar",
    "StatusBarControl": "status bar",
    "ProgressBarControl": "progress bar",
    "SliderControl": "slider",
    "SpinnerControl": "spin button",
    "ScrollBarControl": "scroll bar",
    "HyperlinkControl": "hyperlink",
    "ImageControl": "image",
    "TableControl": "table",
    "DataGridControl": "table",
    "DataItemControl": "table cell",
    "HeaderControl": "table column header",
    "HeaderItemControl": "table column header",
    "WindowControl": "frame",
    "PaneControl": "panel",
    "GroupControl": "panel",
    "CustomControl": "unknown",
    "SeparatorControl": "separator",
    "CalendarControl": "calendar",
    "TitleBarControl": "panel",
    "ThumbControl": "panel",
    "ToolTipControl": "tool tip",
}


def role_of(control_type: str | None, class_name: str | None) -> str:
    """
    判定一个元素的角色（**先 ClassName 后 ControlType**，理由见模块 docstring）。

    返回 AT-SPI 风格的小写角色名；都认不出时返回 `"unknown"`
    （它在 `NOISE_ROLES` 里，无名字时会被 serializer 丢掉 —— 这正是我们要的：
    不认识的容器不该出现在给模型的树里）。
    """
    cls = (class_name or "").strip().lower()
    if cls in _CLASS_ROLES:
        return _CLASS_ROLES[cls]
    ct = (control_type or "").strip()
    if ct in _CONTROL_TYPE_ROLES:
        return _CONTROL_TYPE_ROLES[ct]
    return "unknown"


# ── UIA Pattern → 我们的动作名 ───────────────────────────────────────────
#
# ⚠️⚠️ 这里**只能**用 `PatternId` 的成员名，且必须经 `_uia.resolve_pattern()` 拿 id，
#     再走 `element.GetPattern(id)`。**绝不要**写回
#     `getattr(element, "IsInvokePatternAvailable", False)` —— 那条路真机实测**恒为 False**：
#
#       · `uiautomation.Control` 根本没有 `IsXxxPatternAvailable` 这类属性，
#         而 `getattr(x, name, 默认值)` 让「属性不存在」与「属性为 False」**无法区分** ——
#         名字写错不报错、只静默给默认值。于是所有元素的动作表都是空的，
#         元素级 do_action（本项目的第一原则）整条路是死的，而**单测全绿**：
#         桩对象恰好长着我臆想出来的那个属性，守卫在拿虚构验证虚构。
#
#       · `GetInvokePattern()` 那些 getter 也不能用：它们定义在**控制类型子类**上而不在
#         基类（`ButtonControl` 有 `GetInvokePattern`，**没有** `GetSelectionItemPattern`），
#         `getattr` 会直接抛 AttributeError。唯一通用的入口是基类的 `GetPattern(patternId)`。
#
# 顺序即**优先级**：一个元素可能同时支持多个 Pattern（ComboBox 既有 ExpandCollapse
# 又可能有 Value），`invoke()` 按这个顺序挑第一个能用的。
#
# 为什么**不列** ScrollPattern：全仓没有任何代码按动作名分支（`actions` 只被
# `serializer` 当布尔信号 + 显示文本），而 `actions.py` 也**没有**实现滚动 Pattern 的
# 调用 —— 报出来等于宣称一个我们做不到的动作。滚动有专门的坐标级工具。
_PATTERN_ACTIONS: tuple[tuple[str, str], ...] = (
    ("InvokePattern", ACTION_INVOKE),
    ("TogglePattern", ACTION_TOGGLE),
    ("SelectionItemPattern", ACTION_SELECT),
    ("ExpandCollapsePattern", ACTION_EXPAND),
    ("ValuePattern", ACTION_SET_VALUE),
    ("RangeValuePattern", ACTION_SET_VALUE),
)

# 哪些角色**值得**探 Pattern（2026-09-28 真机量的成本决定的）。
#
# 为什么需要门控：每次 `GetPattern` 是一次跨进程调用，冷路径实测 **~2.6 ms**，
# 而每个元素要探 6 个。记事本 55 个节点里只有 36% 是这类角色 —— 全探会让
# 400 节点的树多花约 7 秒，而整棵树的墙钟预算只有 12 秒。
#
# 门控的语义是**「这些角色才承载动作」**，不是"省点时间"：容器/标签类角色
# （panel / filler / label / separator…）上的 Pattern 不是模型会去执行的动作。
# ⚠️ 它是**有损**的：若某个容器角色其实带 InvokePattern，树里不会显示。
# 代价可控的理由是 `element_info()` 与 `invoke()` 对**单个元素**一律真探、不走门控 ——
# 模型对某个具体元素有疑问时，那条路给的是事实而不是门控的近似。
_PROBE_ROLES: frozenset[str] = frozenset(ACTIONABLE_ROLES) | {
    "text", "entry", "list", "tree", "table", "document", "calendar",
}


def _pattern_id(name: str) -> int:
    """
    `PatternId` 成员名 → 数值，按名缓存。

    ⚠️ 认不出**抛异常**（见 `_uia.resolve_pattern`）—— 这是这一层唯一的诊断出口：
    `getattr` 的默认值会把"名字写错"变成"这张表是空的"，而抛出来才是响亮的。
    """
    if name not in _PATTERN_ID_CACHE:
        _PATTERN_ID_CACHE[name] = _uia.resolve_pattern(name)
    return _PATTERN_ID_CACHE[name]


_PATTERN_ID_CACHE: dict[str, int] = {}


def pattern_available(element, pattern_name: str) -> bool:  # noqa: ANN001
    """
    该元素是否支持某个 Pattern（`pattern_name` 是 `PatternId` 的成员名）。

    `GetPattern` 内部已 catch COMError、不支持时返回 None，故这里只需判空。
    异常仍让它冒出去 —— "元素已失效"与"不支持该 Pattern"是两件事。
    """
    return element.GetPattern(_pattern_id(pattern_name)) is not None


def actions_of(element, role: str | None = None, probe: bool = True) -> list[str]:  # noqa: ANN001
    """
    该元素支持的动作（我们自己的规范名）。

    `role` 给了就按 `_PROBE_ROLES` 门控（树遍历走这条，见常量处的成本说明）；
    不给或 `probe=False` 的语义不同 —— **`element_info`/`invoke` 一律传 `probe=True`
    且不传 role**，因为那是对单个元素的追问，必须是事实。

    ⚠️ 每个 Pattern 单独 try：UIA 是跨进程调用，某个 Pattern 探测失败（目标应用忙、
    元素刚失效）不该让整个元素丢失动作列表 —— 那会表现成"这个按钮不能点"，
    而实际上只是那一次探测失败。与 `tree._snapshot` 的字段级 try 同一个理由。
    """
    if role is not None and probe and role not in _PROBE_ROLES:
        probe = False
    out: list[str] = []
    if probe:
        # ⚠️ 常量解析必须在循环**之外**（`_pattern_id` 认不出名字会抛）：
        #    放进 try 里就会被兜底吞掉 —— 于是"名字写错了"再次退化成"这张动作表是空的"，
        #    正是 2026-09-28 那个 bug 的同一形状。
        #    这一层要的是：**常量错 → 响亮地炸；单次探测失败 → 静默跳过**。两者分开。
        resolved = [(action, _pattern_id(name)) for name, action in _PATTERN_ACTIONS]
        for action, pid in resolved:
            try:
                if element.GetPattern(pid) is not None:
                    out.append(action)
            except Exception:  # noqa: BLE001
                pass
        # ExpandCollapse 支持时补上 collapse（同一 Pattern 的反向操作）
        if ACTION_EXPAND in out:
            out.append(ACTION_COLLAPSE)
    try:
        if getattr(element, "IsKeyboardFocusable", False):
            out.append(ACTION_FOCUS)
    except Exception:  # noqa: BLE001
        pass
    return out


def states_of(element) -> list[str]:  # noqa: ANN001
    """
    该元素的状态（人读文本，进 `ElementDetail.to_text` / `UINode.states`）。

    刻意报**事实**而不是判断：`disabled` / `offscreen` / `focused` / `selected` /
    `expanded` / `collapsed`。让模型自己决定这些意味着什么 ——
    服务端替它下结论（比如"不可点"）在边界情况下会误导。
    """
    out: list[str] = []

    def flag(attr: str, label: str, invert: bool = False) -> None:
        try:
            v = getattr(element, attr, None)
            if v is None:
                return
            v = bool(v)
            if invert:
                v = not v
            if v:
                out.append(label)
        except Exception:  # noqa: BLE001
            pass

    flag("IsEnabled", "disabled", invert=True)
    flag("IsOffscreen", "offscreen")
    flag("HasKeyboardFocus", "focused")
    flag("IsSelected", "selected")

    try:
        # ExpandCollapseState：0=Collapsed,1=Expanded,2=PartiallyExpanded,3=LeafNode
        st = getattr(element, "ExpandCollapseState", None)
        if st == 0:
            out.append("collapsed")
        elif st == 1:
            out.append("expanded")
    except Exception:  # noqa: BLE001
        pass

    try:
        # ToggleState：0=Off,1=On,2=Indeterminate
        ts = getattr(element, "ToggleState", None)
        if ts == 1:
            out.append("checked")
        elif ts == 0 and "check box" in str(getattr(element, "ControlTypeName", "")):
            out.append("unchecked")
    except Exception:  # noqa: BLE001
        pass
    return out


def value_of(element) -> Any:  # noqa: ANN001
    """
    取元素的值（输入框内容等）。取不到返回 `None` —— **不要**返回空串：
    "没有值"与"值是空字符串"是两件事，模型据此判断"这个框是空的"还是"这个控件没有值"。

    ⚠️ 判定走 `GetPattern(id)`，**不是** `getattr(element, "IsValuePatternAvailable", …)` ——
    后者在本库上恒为假（见 `_PATTERN_ACTIONS` 处的长注释）。
    """
    try:
        if element.GetPattern(_pattern_id("ValuePattern")) is not None:
            return element.GetValuePattern().Value
    except Exception:  # noqa: BLE001
        pass
    try:
        if element.GetPattern(_pattern_id("TextPattern")) is not None:
            return element.GetTextPattern().DocumentRange.GetText(-1)
    except Exception:  # noqa: BLE001
        pass
    try:
        return getattr(element, "Name", None)
    except Exception:  # noqa: BLE001
        return None


def rect_of(element) -> tuple[int, int, int, int] | None:  # noqa: ANN001
    """
    `BoundingRectangle` → `(l, t, r, b)`。

    ⚠️ 两种形状都认：库把它定义成具名元组式的 `Rect`（有 `.left` 等），
    但换版本可能退化成普通序列。写死一种的话会得到 `AttributeError`（还算好的），
    更坏的是取到空值后被当成"这元素没有矩形"——把库的问题记成应用的问题。
    """
    try:
        r = element.BoundingRectangle
    except Exception:  # noqa: BLE001
        return None
    if r is None:
        return None
    try:
        if hasattr(r, "left"):
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        seq = tuple(r)
        if len(seq) >= 4:
            return (int(seq[0]), int(seq[1]), int(seq[2]), int(seq[3]))
    except Exception:  # noqa: BLE001
        return None
    return None