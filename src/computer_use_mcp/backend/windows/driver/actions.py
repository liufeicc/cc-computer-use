"""
元素级动作与键盘映射（backend.windows.driver.actions）。

═══ 两条上游约束 ═══

**① 元素级动作优先于坐标点击。** 这是本项目在 Linux 侧确立的第一原则，Windows 同理：
UIA 的 `InvokePattern` / `SelectionItemPattern` 是**对元素本身发命令**，零坐标、
不受窗口位置/DPI/遮挡影响。坐标点击永远只是兜底。

**② 按键名必须与 Linux 侧同一套词表。** `core/coordinator/` 与工具层传下来的是
`"ctrl+s"` / `"Return"` / `"alt+F4"` / `"PageDown"` 这类（源自 xdotool 的 keysym 名）。
这里必须认同一批名字 —— 否则 `press_key` 在 Windows 上会变成"要么改 coordinator，
要么静默按错键"。所以 `_VK` 表里同时收 xdotool 的别名（`Return`/`Enter`/`Esc`）。

═══ ⚠️ 扩展键（Extended Key）不是可选项 ═══

方向键、`Delete`、`Home/End`、`PageUp/Down`、右 Ctrl/Alt、Win 键在 PS/2 语义里是
**带 E0 前缀的扩展键**。`SendInput` 必须带上 `KEYEVENTF_EXTENDEDKEY`，
否则这些键会以**小键盘/数字区**的形态被应用收到 —— 表现是"按了方向键但界面没动"，
或者更糟：在记事本里按 Delete 变成了按小键盘的 `.`。
"""

from __future__ import annotations

import ctypes

from . import nodes, win32

# ── 修饰键 ──────────────────────────────────────────────────────────────
_MODIFIERS: dict[str, tuple[int, bool]] = {
    "ctrl": (0x11, False), "control": (0x11, False),
    "shift": (0x10, False),
    "alt": (0x12, False), "meta": (0x12, False),
    "super": (0x5B, True), "win": (0x5B, True), "cmd": (0x5B, True),
}

# ── 具名键 → 虚拟键码（扩展位含义见模块 docstring）──────────────────────
#
# 名字刻意收 xdotool 的写法（Linux 侧传的就是这些），**不要**只收 Windows 的叫法：
# 两端不一致的后果是静默按错键，而不是报错。
_VK: dict[str, tuple[int, bool]] = {
    # 编辑/提交
    "return": (0x0D, False), "enter": (0x0D, False),
    "tab": (0x09, False), "space": (0x20, False),
    "escape": (0x1B, False), "esc": (0x1B, False),
    "backspace": (0x08, False),
    # 下面这些是**扩展键**
    "delete": (0x2E, True), "del": (0x2E, True),
    "insert": (0x2D, True), "ins": (0x2D, True),
    "home": (0x24, True), "end": (0x23, True),
    "pageup": (0x21, True), "pagedown": (0x22, True),
    "prior": (0x21, True), "next": (0x22, True),
    "up": (0x26, True), "down": (0x28, True),
    "left": (0x25, True), "right": (0x27, True),
    # 功能键（依次 0x70..0x7B）
    **{f"f{i}": (0x6F + i, False) for i in range(1, 13)},
}

# 主键：单字符。字母/数字/常见符号在 US 布局上的 VK 恰好等于 ASCII 大写。
def _main_key(token: str) -> tuple[int, bool] | None:
    """
    把一个主键 token 解析成 `(虚拟键码, 是否扩展键)`。

    ⚠️ 单字符走 **`VkKeyScanW`**（交给系统按**当前布局**翻译），而不是自己写
    ASCII→VK 的映射表：后者只在 US 布局上成立，中文/德语/法语布局下会按错键，
    而且**不报错**。`VkKeyScanW` 返回的低字节是 VK、高字节是需要同时按下的修饰位
    （shift = 1, ctrl = 2, alt = 4）—— 这正是"`?` 需要 shift+/"这类事实的权威来源。

    返回 None 表示这个 token 认不出（调用方应当**报错而不是猜**）。
    """
    if len(token) != 1:
        return None
    if token.isascii() and (token.isalnum()):
        # 字母/数字：VK 就是大写 ASCII（这条与布局无关，是 Windows 的约定）
        return (ord(token.upper()), False)
    try:
        u = win32.user32()
        u.VkKeyScanW.argtypes = [win32._WCHAR]
        u.VkKeyScanW.restype = ctypes.c_short
        res = int(u.VkKeyScanW(ord(token)))
    except Exception:  # noqa: BLE001
        return None
    if res == -1 or (res & 0xFF) == 0xFF:
        return None
    return (res & 0xFF, False)


def parse_combo(combo: str) -> list[tuple[int, bool]] | None:
    """
    `"ctrl+shift+t"` → `[(0x11,False), (0x10,False), (0x54,False)]`（**顺序即按下顺序**）。

    解析规则：
      · `+` 分隔，忽略大小写与首尾空白；
      · 修饰键可出现在任意位置，但**统一前置**（Windows 的快捷键语义要求修饰键先按下）；
      · 认不出任何一个 token → 返回 **None**（调用方报错）。

    ⚠️ 刻意**不**返回"尽力而为的部分结果"：`"ctrl+nosuchkey"` 若被解析成"只按 ctrl"，
    调用方会看到一个成功的返回值，而实际什么都没发生 —— 这正是本项目最忌讳的静默。
    """
    if not combo or not combo.strip():
        return None
    tokens = [t.strip().lower() for t in combo.split("+") if t.strip()]
    if not tokens:
        return None

    mods: list[tuple[int, bool]] = []
    mains: list[tuple[int, bool]] = []
    for tok in tokens:
        if tok in _MODIFIERS:
            mods.append(_MODIFIERS[tok])
            continue
        if tok in _VK:
            mains.append(_VK[tok])
            continue
        parsed = _main_key(tok)
        if parsed is None:
            return None
        mains.append(parsed)
    if not mains:
        # 只有修饰键（例如单独按 `ctrl`）是**合法**的 —— 调用方可能就是要按住它。
        return mods or None
    # 去重修饰键但保持顺序（"ctrl+ctrl+s" 不该按下两次 ctrl）
    seen: set[int] = set()
    ordered: list[tuple[int, bool]] = []
    for vk, ext in mods:
        if vk not in seen:
            seen.add(vk)
            ordered.append((vk, ext))
    return ordered + mains


# ── 元素级动作 ──────────────────────────────────────────────────────────

def invoke(element, action: str | None = None) -> bool:  # noqa: ANN001
    """
    对元素执行动作（`action=None` 时按可用 Pattern 自动挑一个）。

    ⚠️ 返回值是 bool 而**不是**"做了就 True"：`coordinator` 靠它决定要不要降级到
    坐标点击。所以每个分支都必须把 UIA 的**真实结果**反映出来 —— 例如
    `ExpandCollapsePattern` 在已经展开的元素上调用不抛异常，但那不代表"刚展开了"。

    ⚠️ **不吞异常**：这里让异常冒到调用方（`backend.invoke` 会转成 False 并记日志）。
    吞在这里的话，"元素已失效"与"这个动作成功了"就无法区分。
    """
    if element is None:
        return False

    wanted = action or None
    # 自动挑：按 nodes 的优先级顺序
    if wanted is None:
        for cand in (nodes.ACTION_INVOKE, nodes.ACTION_TOGGLE, nodes.ACTION_SELECT,
                     nodes.ACTION_EXPAND):
            if cand in nodes.actions_of(element):
                wanted = cand
                break
        if wanted is None:
            return False

    if wanted == nodes.ACTION_INVOKE:
        if not getattr(element, "IsInvokePatternAvailable", False):
            return False
        element.GetInvokePattern().Invoke()
        return True
    if wanted == nodes.ACTION_SELECT:
        if not getattr(element, "IsSelectionItemPatternAvailable", False):
            return False
        element.GetSelectionItemPattern().Select()
        return True
    if wanted == nodes.ACTION_TOGGLE:
        if not getattr(element, "IsTogglePatternAvailable", False):
            return False
        element.GetTogglePattern().Toggle()
        return True
    if wanted in (nodes.ACTION_EXPAND, nodes.ACTION_COLLAPSE):
        if not getattr(element, "IsExpandCollapsePatternAvailable", False):
            return False
        pat = element.GetExpandCollapsePattern()
        if wanted == nodes.ACTION_EXPAND:
            pat.Expand()
        else:
            pat.Collapse()
        return True
    return False


def set_value(element, text: str) -> bool:  # noqa: ANN001
    """
    写元素的值（输入框）。

    ⚠️ 先 `SetFocus` 再写：某些 provider（尤其经典 Win32 编辑框）在非焦点状态下
    会接受 `SetValue` 但**不发通知**，于是紧跟着读回来的值还是旧的 —— 表现成
    "写了但没生效"。聚焦一次的成本约 1ms，换掉一整类难查的静默失败。
    """
    if element is None:
        return False
    try:
        focus(element)
    except Exception:  # noqa: BLE001 —— 聚焦失败不该阻断写值（有些控件不可聚焦但可写）
        pass
    if getattr(element, "IsValuePatternAvailable", False):
        element.GetValuePattern().SetValue(text)
        return True
    if getattr(element, "IsRangeValuePatternAvailable", False):
        element.GetRangeValuePattern().SetValue(float(text))
        return True
    return False


def focus(element) -> bool:  # noqa: ANN001
    """把键盘焦点给该元素（`SetFocus`）。"""
    if element is None:
        return False
    if not getattr(element, "IsKeyboardFocusable", False):
        return False
    element.SetFocus()
    return True


def element_hwnd(element) -> int:  # noqa: ANN001
    """元素所在的**子** HWND（`NativeWindowHandle`）。取不到返回 0。

    用途是"定向注入"那条路（`PostMessage` 只对子 HWND 生效，见 DEMO 的硬约束），
    本模块暂时不用它，但保留出口供后续实现不必再翻一遍驱动层。
    """
    try:
        return int(getattr(element, "NativeWindowHandle", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def focus_window_of(element) -> bool:  # noqa: ANN001
    """
    把元素所属的**顶层窗口**提到前台。

    ⚠️ 走 `GetTopLevelControl` 拿顶层，而不是从子 HWND 上溯 ——
    UIA 已经给了权威答案，自己爬父链在跨进程边界上容易爬错（拿到的还是子窗）。
    """
    try:
        top = element.GetTopLevelControl()
    except Exception:  # noqa: BLE001
        return False
    if top is None:
        return False
    hwnd = element_hwnd(top)
    if not hwnd:
        return False
    return win32.set_foreground_window(hwnd)