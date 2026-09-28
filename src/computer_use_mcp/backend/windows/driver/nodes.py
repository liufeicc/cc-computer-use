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

# ── 我们自己定的动作规范名（见模块 docstring 的说明）──────────────────────
ACTION_INVOKE = "invoke"          # → InvokePattern
ACTION_SELECT = "select"          # → SelectionItemPattern
ACTION_EXPAND = "expand"          # → ExpandCollapsePattern.Expand
ACTION_COLLAPSE = "collapse"      # → ExpandCollapsePattern.Collapse
ACTION_SET_VALUE = "set_value"    # → ValuePattern / RangeValuePattern
ACTION_TOGGLE = "toggle"          # → TogglePattern
ACTION_SCROLL = "scroll"          # → ScrollPattern
ACTION_FOCUS = "focus"            # → SetFocus（不是 Pattern，但我们对外的统一动作）

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
# 顺序即**优先级**：一个元素可能同时支持多个 Pattern（比如 ComboBox 既有
# ExpandCollapse 又可能有 Value），`invoke()` 按这个顺序挑第一个能用的。
_PATTERN_ACTIONS: tuple[tuple[str, str], ...] = (
    ("IsInvokePatternAvailable", ACTION_INVOKE),
    ("IsTogglePatternAvailable", ACTION_TOGGLE),
    ("IsSelectionItemPatternAvailable", ACTION_SELECT),
    ("IsExpandCollapsePatternAvailable", ACTION_EXPAND),
    ("IsValuePatternAvailable", ACTION_SET_VALUE),
    ("IsRangeValuePatternAvailable", ACTION_SET_VALUE),
    ("IsScrollPatternAvailable", ACTION_SCROLL),
)


def actions_of(element) -> list[str]:  # noqa: ANN001 —— 库对象，无法标注
    """
    该元素支持的动作（我们自己的规范名）。

    ⚠️ **每个属性单独 try**：UIA 是跨进程调用，某个 Pattern 探测失败（目标应用忙、
    元素刚失效）不该让整个元素丢失动作列表 —— 那会表现成"这个按钮不能点"，
    而实际上只是那一次探测失败。与 `tree._snapshot` 的字段级 try 同一个理由。
    """
    out: list[str] = []
    for attr, action in _PATTERN_ACTIONS:
        try:
            if getattr(element, attr, False):
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
    """
    try:
        v = getattr(element, "GetValuePattern", None)
        if getattr(element, "IsValuePatternAvailable", False) and v is not None:
            return element.GetValuePattern().Value
    except Exception:  # noqa: BLE001
        pass
    try:
        if getattr(element, "IsTextPatternAvailable", False):
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