"""
Win32 的 ctypes 绑定（backend.windows.driver.win32）。

═══ 两条纪律 ═══

**① 一律惰性绑定。** 本模块在 **Linux 上 import 也必须成功**——CI 跑在 Linux，
而驱动层的守卫测试要 import 它。所以 `ctypes.WinDLL("user32")` 全部藏在函数里，
模块顶层只有结构体定义与常量。

**② 驱动层零 subprocess。** 平台调用走 ctypes，不 spawn 外部命令。理由有两条：
真实原因是要低延迟（每个窗口操作 spawn 一次进程，几百毫秒就没了，而 UIA 的一次属性读
是个位数毫秒）；副带好处是 `tests/test_spawn_env.py` 那条「每个 spawn 点的 env= 必须
源自 env_for/app_env」的结构守卫**在本包上恒真**，不会因为我们引入新 spawn 点而需要加豁免。

═══ 注：为什么不用 pywin32 / comtypes 的封装 ═══

我们要的 Win32 面很窄（就下面这些函数），而 `uiautomation` 已经把 UIA 那半边包好了。
再引一个 Win32 封装库等于为十几个函数拉一串依赖进冻结产物 —— 与本项目
「刻意不引 pytesseract、直接调 tesseract 命令行」的依赖观一致。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

# ── 常量（只列我们用到的）──────────────────────────────────────────────────

CWP_ALL = 0x0000
CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004

GWL_STYLE = -16
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000

# DPI 感知上下文（SetProcessDpiAwarenessContext）
DPI_AWARENESS_CONTEXT_UNAWARE = -1
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = -3
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

# GetProcessDpiAwareness（shcore）
PROCESS_DPI_AWARENESS_UNAWARE = 0
PROCESS_DPI_AWARENESS_SYSTEM_AWARE = 1
PROCESS_DPI_AWARENESS_PER_MONITOR_AWARE = 2

# 屏幕度量（GetSystemMetrics）
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

# 枚举当前显示设置（EnumDisplaySettingsW）
ENUM_CURRENT_SETTINGS = 0xFFFFFFFF

# SendInput
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
# 键盘
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# ═══ 定宽别名：为什么不能直接用 `wintypes.DWORD` ═══
#
# `ctypes.wintypes` 在**非 Windows** 上把所有名字都建在 `c_long`/`c_ulong` 之上
# （`DWORD = c_ulong`、`LONG = c_long`）。而 Linux 是 **LP64**（这两个 8 字节）、
# Windows 是 **LLP64**（4 字节）—— 于是同一份结构体定义在两个平台上会布局成不同大小。
#
# 后果不只是"Linux 上算错"（我们本来也不在 Linux 上调这些 API），而是：
# **唯一能在 CI 里自动验证结构布局的地方恰恰是 Linux**。布局一旦随平台漂移，
# `sizeof` 断言就只能在真机上跑 —— 而真机验证正是我们要避免的那件事。
#
# 所以凡是「希望布局跨平台一致、从而能被 Linux 上的守卫守住」的结构体，
# 一律用下面这几个写死宽度的别名，**不要**用 `wintypes` 里那些。
# （既有那些只用标量入参、不构成结构体布局的地方不受影响，无需改动。）
_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_LONG = ctypes.c_int32

# ⚠️ `WCHAR` 同样要钉死：Windows 的 `wchar_t` 是 **2** 字节，而 Linux 的 `ctypes.c_wchar`
# 是 **4** 字节。`DEVMODEW` 里有两个 32 元素的字符数组（`dmDeviceName` / `dmFormName`），
# 用 `c_wchar` 会让整个结构体大出 128 字节 —— 而 `dmPelsWidth` 正是本探针用来判定
# 「坐标有没有被虚拟化」的**绝对参照物**，读错它的后果是整份结论反向。
# 用定宽 `c_uint16` 数组 + `_wchars_to_str` 解码，布局就与 Windows 逐字节一致了。
_WCHAR = ctypes.c_uint16


def _wchars_to_str(arr) -> str:  # noqa: ANN001 —— ctypes 数组，类型没法标
    """把定宽 UTF-16 数组读成 str（到 NUL 为止）。只用于设备名这类 BMP 文本。"""
    return "".join(chr(c) for c in arr if c != 0)

# ⚠️ `ctypes.wintypes` 在非 Windows 上还少一个 `ULONG_PTR`（那个名字只在
# `sys.platform == 'win32'` 分支里定义）。语义：指针宽度整数。
ULONG_PTR = getattr(wintypes, "ULONG_PTR", None) or (
    ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class POINTL(ctypes.Structure):
    """
    `POINTL`。

    ⚠️ 这里刻意用 `c_int32` 而**不是** `c_long`：Windows 是 LLP64（`long` = 4 字节），
    Linux 是 LP64（`long` = 8 字节）。用 `c_long` 的话，同一份定义在 Linux 上会布局成
    另一个大小 —— 而本模块的硬纪律是「在 Linux 上 import 也必须成功」，CI 也跑在 Linux，
    于是**唯一能自动验证结构布局的地方恰恰是 Linux**。写死 `c_int32` 后布局在两个平台上
    一致，`sizeof` 断言才能真的在 CI 里跑（见 `test_windows_driver_guards.py`）。
    """
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# ── 惰性 DLL 句柄 ─────────────────────────────────────────────────────────
#
# 用 `ctypes.WinDLL` 而不是 `ctypes.windll.<x>`：后者在 Linux 上**根本不存在**
# 这个属性（`ctypes` 在非 Windows 上没有 `windll`），写成 `ctypes.windll.user32`
# 会让本模块在 Linux 上 import 就炸 —— 而 CI 在 Linux 上要 import 它。
_cache: dict[str, ctypes.CDLL] = {}


def _dll(name: str) -> ctypes.CDLL:
    """按名字取 DLL 句柄（进程内缓存）。非 Windows 上会抛 OSError，由调用方处理。"""
    if name not in _cache:
        _cache[name] = ctypes.WinDLL(name)      # type: ignore[attr-defined]
    return _cache[name]


def user32() -> ctypes.CDLL:
    return _dll("user32")


def kernel32() -> ctypes.CDLL:
    return _dll("kernel32")


def shcore() -> ctypes.CDLL:
    """Windows 8.1+ 的 shcore.dll（`GetProcessDpiAwareness` 在这里）。"""
    return _dll("shcore")


# ── 窗口站 / 桌面 ─────────────────────────────────────────────────────────

def process_window_station_name() -> str:
    """
    当前进程所在**窗口站**的名字。

    判据用途：交互式会话是 `WinSta0`；服务会话是 `Service-0x0-xxxx$`。
    实测这两者下 UIA 的表现天差地别（服务会话里根元素子级恒为 0、抓屏全黑、
    前台窗口读不到），**而且不报任何错** —— 所以这是"这份数据可不可信"的第一道闸。
    """
    u = user32()
    u.GetProcessWindowStation.restype = wintypes.HANDLE
    h = u.GetProcessWindowStation()
    return _user_object_name(h)


def thread_desktop_name() -> str:
    """当前线程所在**桌面**的名字（交互式会话里通常是 `Default`）。"""
    u = user32()
    u.GetThreadDesktop.restype = wintypes.HANDLE
    u.GetThreadDesktop.argtypes = [wintypes.DWORD]
    k = kernel32()
    k.GetCurrentThreadId.restype = wintypes.DWORD
    h = u.GetThreadDesktop(k.GetCurrentThreadId())
    return _user_object_name(h)


def _user_object_name(handle) -> str:
    """`GetUserObjectInformationW(h, UOI_NAME=2, …)`。"""
    u = user32()
    buf = ctypes.create_unicode_buffer(256)
    need = wintypes.DWORD(0)
    ok = u.GetUserObjectInformationW(handle, 2, buf, ctypes.sizeof(buf), ctypes.byref(need))
    return buf.value if ok else ""


# ── 会话号 ───────────────────────────────────────────────────────────────

def session_id_of_process(pid: int) -> int | None:
    """`ProcessIdToSessionId`。0 = 服务会话（**没有真桌面**），1+ = 交互式会话。"""
    k = kernel32()
    sid = wintypes.DWORD(0)
    ok = k.ProcessIdToSessionId(wintypes.DWORD(pid), ctypes.byref(sid))
    return int(sid.value) if ok else None


def current_process_id() -> int:
    k = kernel32()
    k.GetCurrentProcessId.restype = wintypes.DWORD
    return int(k.GetCurrentProcessId())


# ── DPI ──────────────────────────────────────────────────────────────────

def set_dpi_awareness_v2() -> dict:
    """
    把进程的 DPI 感知设为 `PER_MONITOR_AWARE_V2`。**必须在任何 UIA 对象建立之前调。**

    ═══ 为什么这一步是 Windows 支持的头号前置 ═══

    UIA 的 `BoundingRectangle` 返回**物理像素**，而 `SendInput` 的坐标是**按调用进程的
    DPI 感知度解释**的。进程若不是 DPI 感知的，Windows 会对它做 DPI 虚拟化 ——
    同一个数字在两边含义不同，于是**二次缩放、点击偏几十像素**，
    且只在非 100% 缩放的机器上出现（"在 4K 笔记本上偏、在外接 1080p 上却正常"）。

    ═══ ⚠️ 顺序陷阱（实测过，会静默失效）═══

    `uiautomation` 在**模块导入时**就执行 `SetProcessDpiAwareness(PerMonitorDpiAware)`
    （旧 shcore API，值 2，**不是** V2）。而 Windows 只允许设一次感知度，**后设的失败**。
    所以本函数必须在 `import uiautomation` **之前**调用——`_uia.import_uia()` 里把这条
    顺序固化了下来，不要绕过它直接 import。

    返回 dict 而不是 bool：调用结果要进 JSON 报告（`ok=False` 时整份坐标数据都不可信）。
    """
    # 首选新 API（SetProcessDpiAwarenessContext，可指定 V2）
    try:
        u = user32()
        u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        ok = bool(u.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
        err = 0 if ok else ctypes.get_last_error()
        return {"api": "SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)",
                "ok": ok, "err": err,
                "note": "" if ok else "失败通常意味着感知度已被更早的调用设死"}
    except Exception as exc:  # noqa: BLE001 —— 老系统上没有这个导出
        return {"api": "SetProcessDpiAwarenessContext", "ok": False, "err": str(exc)}


def dpi_awareness_now() -> dict:
    """读回当前进程的 DPI 感知度（用于验证上面那步真的生效了）。"""
    out: dict = {}
    try:
        p = shcore()
        val = ctypes.c_int(0)
        hr = p.GetProcessDpiAwareness(None, ctypes.byref(val))
        out["shcore_GetProcessDpiAwareness"] = {"hr": hr, "value": int(val.value)}
    except Exception as exc:  # noqa: BLE001
        out["shcore_GetProcessDpiAwareness"] = {"error": str(exc)}
    try:
        u = user32()
        u.GetDpiForSystem.restype = wintypes.UINT
        out["GetDpiForSystem"] = int(u.GetDpiForSystem())
    except Exception as exc:  # noqa: BLE001
        out["GetDpiForSystem"] = {"error": str(exc)}
    return out


def dpi_for_window(hwnd: int) -> int | None:
    try:
        u = user32()
        u.GetDpiForWindow.argtypes = [wintypes.HWND]
        u.GetDpiForWindow.restype = wintypes.UINT
        return int(u.GetDpiForWindow(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return None


# ── ★ 坐标空间：DPI 虚拟化的判定 ──────────────────────────────────────────
#
# ═══ 这一节要回答的唯一问题 ═══
#
# **「UIA 给的坐标」与「我们注入的坐标」是不是同一个空间？**
#
# 若不是，表现是「在 100% 缩放的开发机上一切正常，在 4K 笔记本上点偏几十像素」——
# 一个在开发机上**永远重现不了**、且没有任何报错的故障。
#
# ═══ 为什么不能靠"自己跟自己比"来判定 ═══
#
# 非 DPI 感知的进程会被 Windows **虚拟化**：它眼里的一切坐标都被按缩放比除过了。
# 而 `GetSystemMetrics` / `GetCursorPos` / `SetCursorPos` / `SendInput` / `UIA 矩形`
# **全都经过同一层虚拟化** —— 于是"绕过一圈回来对得上"这个判据**恒真**，
# 哪怕实际上全偏了 1.5 倍。这类判据是坏尺子：它测不出任何东西，却永远绿。
#
# 所以本节的判据**必须锚在一个绝对参照物上**：
#   · `EnumDisplaySettingsW` 读的是**显示驱动**里的模式表，**从不虚拟化**
#     （`dmPelsWidth/Height` 永远是物理像素）
#   · `GetSystemMetrics(SM_CXSCREEN)` 对非感知进程**会**被虚拟化
# 两者一比即知：相等 ⇒ 没被虚拟化；差一个缩放比 ⇒ 被虚拟化了。
#
# 另一条独立的、同样非循环的判据是**感知度上下文本身**
# （`AreDpiAwarenessContextsEqual`）——它比 shcore 的 `GetProcessDpiAwareness` 强，
# 因为后者只能报出 "PER_MONITOR"(2)，**分不出 V1 与 V2**。


class _DEVMODE_PRINT(ctypes.Structure):
    _fields_ = [("dmOrientation", ctypes.c_short), ("dmPaperSize", ctypes.c_short),
                ("dmPaperLength", ctypes.c_short), ("dmPaperWidth", ctypes.c_short),
                ("dmScale", ctypes.c_short), ("dmCopies", ctypes.c_short),
                ("dmDefaultSource", ctypes.c_short), ("dmPrintQuality", ctypes.c_short)]


class _DEVMODE_DISPLAY(ctypes.Structure):
    _fields_ = [("dmPosition", POINTL),
                ("dmDisplayOrientation", _DWORD),
                ("dmDisplayFixedOutput", _DWORD)]


class _DEVMODE_U1(ctypes.Union):
    """头文件里那个匿名并集：打印口径与显示口径共用同一段空间（两者都是 16 字节）。"""

    _fields_ = [("print", _DEVMODE_PRINT), ("display", _DEVMODE_DISPLAY)]


class _DEVMODE_U2(ctypes.Union):
    _fields_ = [("dmDisplayFlags", _DWORD), ("dmNup", _DWORD)]


class DEVMODEW(ctypes.Structure):
    """
    `DEVMODEW`（EnumDisplaySettings 的输出结构）。

    ⚠️ 字段顺序与并集布局必须与 Windows 头文件逐字对应——这是**二进制布局契约**，
    错一位后面读到的 `dmPelsWidth` 就是垃圾值，而且不会有任何报错。
    故本探针把 `dmSize` 一并记进报告（Windows 要求调用方填对 `dmSize`，
    填错时 `EnumDisplaySettingsW` 直接返回 0 —— 那是我们能拿到的最早的失败信号）。

    ⚠️ `_DEVMODE_*` 四个类型**必须定义在本类之前**：ctypes 在 `_fields_` 里引用的是
    已存在的类型对象，没有 Python 那种前向引用。写成字符串名会静默产生一个
    **错误布局**，而不是报错。
    """

    _fields_ = [
        ("dmDeviceName", _WCHAR * 32),
        ("dmSpecVersion", _WORD),
        ("dmDriverVersion", _WORD),
        ("dmSize", _WORD),
        ("dmDriverExtra", _WORD),
        ("dmFields", _DWORD),
        ("u1", _DEVMODE_U1),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", _WCHAR * 32),
        ("dmLogPixels", _WORD),           # ← 这一项就是 DPI（老 API 里就这么存）
        ("dmBitsPerPel", _DWORD),
        ("dmPelsWidth", _DWORD),          # ← ★ 物理宽度，不随调用方感知度变化
        ("dmPelsHeight", _DWORD),         # ← ★ 物理高度
        ("u2", _DEVMODE_U2),
        ("dmDisplayFrequency", _DWORD),
        ("dmICMMethod", _DWORD),
        ("dmICMIntent", _DWORD),
        ("dmMediaType", _DWORD),
        ("dmDitherType", _DWORD),
        ("dmReserved1", _DWORD),
        ("dmReserved2", _DWORD),
        ("dmPanningWidth", _DWORD),
        ("dmPanningHeight", _DWORD),
    ]


def system_metrics() -> dict:
    """
    一组 `GetSystemMetrics` 取值。**其中屏幕宽高对非 DPI 感知进程是被虚拟化的**
    —— 这正是它作为判据的价值（见本节开头的说明）。
    """
    u = user32()
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int
    names = {
        "SM_CXSCREEN": SM_CXSCREEN, "SM_CYSCREEN": SM_CYSCREEN,
        "SM_XVIRTUALSCREEN": SM_XVIRTUALSCREEN, "SM_YVIRTUALSCREEN": SM_YVIRTUALSCREEN,
        "SM_CXVIRTUALSCREEN": SM_CXVIRTUALSCREEN, "SM_CYVIRTUALSCREEN": SM_CYVIRTUALSCREEN,
        "SM_CMONITORS": SM_CMONITORS,
    }
    out: dict = {}
    for name, idx in names.items():
        try:
            out[name] = int(u.GetSystemMetrics(idx))
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}
    return out


def current_display_mode(device: str | None = None) -> dict:
    """
    当前显示模式（`EnumDisplaySettingsW(NULL, ENUM_CURRENT_SETTINGS, …)`）。

    ★ 这是本节里**唯一不随调用方 DPI 感知度变化的**屏幕尺寸来源，
    故它是判定「坐标有没有被虚拟化」的绝对参照物。
    """
    u = user32()
    u.EnumDisplaySettingsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.POINTER(DEVMODEW)]
    u.EnumDisplaySettingsW.restype = wintypes.BOOL
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    ok = u.EnumDisplaySettingsW(device, ENUM_CURRENT_SETTINGS, ctypes.byref(dm))
    if not ok:
        return {"ok": False, "dmSize_used": dm.dmSize,
                "err": ctypes.get_last_error(),
                "note": "返回 0 —— 多半是 dmSize 填错或没有该显示设备"}
    return {
        "ok": True,
        "dmSize_used": dm.dmSize,
        "device": _wchars_to_str(dm.dmDeviceName),
        "dmPelsWidth": int(dm.dmPelsWidth),
        "dmPelsHeight": int(dm.dmPelsHeight),
        "dmLogPixels": int(dm.dmLogPixels),
        "dmBitsPerPel": int(dm.dmBitsPerPel),
        "dmDisplayFrequency": int(dm.dmDisplayFrequency),
        "dmPosition": [int(dm.u1.display.dmPosition.x), int(dm.u1.display.dmPosition.y)],
    }


def thread_dpi_awareness_context() -> int:
    """当前线程的 DPI 感知上下文（伪句柄，只用于比较，不要当普通指针解引用）。"""
    u = user32()
    u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    return int(u.GetThreadDpiAwarenessContext() or 0)


def window_dpi_awareness_context(hwnd: int) -> int | None:
    """某个窗口（= 它的进程）的 DPI 感知上下文。用于判断**目标应用**的感知档位。"""
    try:
        u = user32()
        u.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
        u.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        return int(u.GetWindowDpiAwarenessContext(wintypes.HWND(hwnd)) or 0)
    except Exception:  # noqa: BLE001
        return None


def _context_value(offset: int) -> ctypes.c_void_p:
    """
    DPI 感知上下文常量 → `c_void_p`。

    ⚠️ 这些是**负数**（UNAWARE=-1 … PER_MONITOR_AWARE_V2=-4），在 64 位上是
    符号扩展的伪句柄。直接 `c_void_p(-1)` 会得到 `0xFFFFFFFFFFFFFFFF`，
    这与 Windows 内部表示一致；**不要**改成 `c_void_p(0xFFFFFFFF)`。
    """
    return ctypes.c_void_p(offset)


def describe_awareness_context(ctx: int | None) -> dict:
    """
    把一个 DPI 感知上下文翻译成可读名字 —— **能分出 V1 与 V2**。

    ⚠️ 为什么不用 `shcore!GetProcessDpiAwareness`（`dpi_awareness_now` 里那个）：
    它只返回值 2 = `PER_MONITOR_AWARE`，**V1 与 V2 都报 2**。而 V1 与 V2 在
    "非客户区是否随 DPI 缩放"等行为上并不相同 —— 我们要的恰恰是 V2。
    只有 `AreDpiAwarenessContextsEqual` 能区分它们。
    """
    if not ctx:
        return {"known": False, "reason": "拿不到上下文句柄"}
    try:
        u = user32()
        u.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
        u.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        u.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "reason": f"{type(exc).__name__}: {exc}"}

    candidates = {
        "unaware": DPI_AWARENESS_CONTEXT_UNAWARE,
        "system_aware": DPI_AWARENESS_CONTEXT_SYSTEM_AWARE,
        "per_monitor_aware_v1": DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE,
        "per_monitor_aware_v2": DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2,
        "unaware_gdiscaled": -5,
    }
    matched: list[str] = []
    for name, val in candidates.items():
        try:
            if u.AreDpiAwarenessContextsEqual(ctypes.c_void_p(ctx), _context_value(val)):
                matched.append(name)
        except Exception:  # noqa: BLE001 —— 老系统上没有 -5 这个常量
            pass
    try:
        value = int(u.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(ctx)))
    except Exception as exc:  # noqa: BLE001
        value = {"error": str(exc)}
    return {"known": True, "matched": matched, "awareness_value": value}


# ── 注入通道的坐标回环 ───────────────────────────────────────────────────
#
# ⚠️ 下面两个都是**会被虚拟化**的 API（与 UIA 矩形同一层）。所以它们**不能**用来
# 判定"坐标空间对不对"——见本节开头的说明。它们的用途是**回环自检**：
# 走一遍真实注入路径（挪指针 → 读回 → 问那个点下面是哪个窗口），
# 能抓到"感知度没问题但注入路径本身写错了"这类 bug（归一化取整、虚拟桌面偏移等）。

def get_cursor_pos() -> tuple[int, int] | None:
    u = user32()
    p = POINT()
    if not u.GetCursorPos(ctypes.byref(p)):
        return None
    return (int(p.x), int(p.y))


def set_cursor_pos(x: int, y: int) -> bool:
    """把鼠标指针挪到 (x, y)。坐标按**调用进程的 DPI 感知度**解释。"""
    u = user32()
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.SetCursorPos.restype = wintypes.BOOL
    return bool(u.SetCursorPos(int(x), int(y)))


class MOUSEINPUT(ctypes.Structure):
    # dx/dy 用定宽别名而不是 wintypes.LONG，理由见文件上方的「定宽别名」说明。
    _fields_ = [("dx", _LONG), ("dy", _LONG),
                ("mouseData", _DWORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", _WORD), ("wScan", _WORD), ("dwFlags", _DWORD),
                ("time", _DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", _DWORD), ("wParamL", _WORD), ("wParamH", _WORD)]


class _INPUT_U(ctypes.Union):
    """
    ⚠️ 三个成员**都要列**，即使我们只用其中两个。

    `INPUT` 的 `cbSize` 必须等于 `sizeof(INPUT)`，而 union 的大小取**最大成员**。
    漏掉 `mi` 会让 `sizeof` 变小 → `SendInput` **返回 0 且不报错**（它只是拒绝执行），
    表现成"注入静默无效"。在 x64 上 `MOUSEINPUT`(32) 是最大的那个，所以加 `ki`/`hi`
    不改变总大小 —— 有专门的守卫钉住 `sizeof(INPUT) == 40`（Linux 上就能跑）。
    """

    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", _DWORD), ("u", _INPUT_U)]


def send_input_move_absolute(x: int, y: int) -> dict:
    """
    用 **`SendInput` + `MOUSEEVENTF_ABSOLUTE`** 把指针挪到屏幕坐标 (x, y)。

    这是生产路径上真正会用的那条（`uiautomation` 的点击底层就是 `SendInput`），
    所以要验的不是 `SetCursorPos` 而是它。两者的坐标解释规则相同，但这条多一层
    **归一化**：绝对坐标要先映射到 0~65535，多显示器还要 `MOUSEEVENTF_VIRTUALDESK`。
    那一层取整/漏 flag 正是"看着成功、偏几十像素"的另一处来源，故一并量出来。
    """
    metrics = system_metrics()
    vw = metrics.get("SM_CXVIRTUALSCREEN")
    vh = metrics.get("SM_CYVIRTUALSCREEN")
    vx = metrics.get("SM_XVIRTUALSCREEN")
    vy = metrics.get("SM_YVIRTUALSCREEN")
    if not all(isinstance(v, int) and v > 0 for v in (vw, vh)):
        return {"ok": False, "reason": "虚拟桌面度量为空（服务会话的典型症状）"}

    # Windows 的归一化分母是 (边长 - 1)，不是边长 —— 差这一格会在屏幕最右/最下
    # 产生一像素的系统性偏差，且只在边界处可见。
    norm_x = int(round((x - vx) * 65535 / max(vw - 1, 1)))
    norm_y = int(round((y - vy) * 65535 / max(vh - 1, 1)))

    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)

    u = user32()
    u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u.SendInput.restype = wintypes.UINT
    sent = int(u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)))
    return {
        "ok": sent == 1,
        "sent_count": sent,
        "input_struct_size": ctypes.sizeof(INPUT),
        "normalized": [norm_x, norm_y],
        "virtual_desktop": [vx, vy, vw, vh],
    }


def _send_inputs(inputs: list[INPUT]) -> dict:
    """
    一次 `SendInput` 提交**一串**事件。返回 `{"ok", "sent", "size"}`。

    ⚠️ 为什么必须一次提交而不是循环单发：`SendInput` 原子地插入事件流，
    分成 N 次调用时，**别的进程的输入有机会插进我们的事件之间**（用户碰一下鼠标、
    输入法弹个候选窗），那样"按下 A 再按下 B"就可能变成"A、别人的事件、B"。
    按键组合（Ctrl+S）尤其怕这个 —— 中间插入的松开事件会把组合拆散。
    """
    if not inputs:
        return {"ok": True, "sent": 0}
    arr = (INPUT * len(inputs))(*inputs)
    u = user32()
    u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    u.SendInput.restype = wintypes.UINT
    sent = int(u.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))
    return {"ok": sent == len(inputs), "sent": sent,
            "expected": len(inputs), "size": ctypes.sizeof(INPUT)}


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def send_unicode_text(text: str) -> dict:
    """
    用 `KEYEVENTF_UNICODE` **按字符**投递一段文本。

    ═══ 为什么这条路比 Linux 侧干净得多 ═══

    Linux 上 `xdotool type` 遇到非 ASCII 会**临时改写全局键盘映射**
    （XChangeKeyboardMapping）—— 输入期间用户物理键盘整体失灵，还要跟输入法抢 XKB。
    所以 Linux 侧非 ASCII 必须绕到剪贴板，而剪贴板又要判"目标是不是终端"才能选粘贴键。

    Windows 这边不存在这些问题：`KEYEVENTF_UNICODE` 直接把 `wScan` 当成 UTF-16 码元投递，
    **完全绕开键盘布局**（不需要目标机器装了对应布局，也不会改任何全局状态）。
    代价是它走的是"文本输入"而非"按键"，**不触发快捷键** —— 这正是我们要的：
    输入文本就该只输入文本。

    ⚠️ 用 UTF-16 **码元**逐位投递而不是 `ord(char)`：`ord()` 对 BMP 之外的字符
    （emoji 等）返回 >0xFFFF 的码点，而 `wScan` 只有 16 位，会被静默截断成别的字。
    Python 的 `utf-16-le` 编码正好把它拆成代理对，逐码元发出去就是对的。
    """
    payload = text.encode("utf-16-le")
    inputs: list[INPUT] = []
    for i in range(0, len(payload), 2):
        code = payload[i] | (payload[i + 1] << 8)
        inputs.append(_key_input(scan=code, flags=KEYEVENTF_UNICODE))
        inputs.append(_key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return _send_inputs(inputs)


def send_key_combo(vks: list[tuple[int, bool]]) -> dict:
    """
    按下并松开一组键。入参是 `[(虚拟键码, 是否扩展键), …]`，**顺序即按下顺序**。

    实现逻辑（顺序是有讲究的）：
      1. 修饰键与主键**一并按下**（按给定顺序）；
      2. **逆序**全部松开 —— 松开顺序反了会让某些应用看到"修饰键还按着"的中间态，
         表现为快捷键偶发失灵（与 Linux 侧"先松主键再松修饰键"是同一个道理）。
    """
    downs = [_key_input(vk=vk, flags=(KEYEVENTF_EXTENDEDKEY if ext else 0))
             for vk, ext in vks]
    ups = [_key_input(vk=vk, flags=KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0))
           for vk, ext in reversed(vks)]
    return _send_inputs(downs + ups)


def send_mouse_button(x: int, y: int, button: int = 1, repeat: int = 1,
                      delay_ms: int = 100, focus_window: bool = False,
                      hwnd: int = 0) -> dict:
    """
    移动指针到 `(x, y)` 并点击（可重复 = 双击/三击）。

    ═══ ⚠️ 双击的间隔为什么必须在这里控 ═══

    X11 侧为同一件事吃过大亏（当时的实现是"调用方连调两次 click"，
    两次之间夹着落点证据、OCR、变化对比，约 0.5s，必然被认成两次单击）。
    Windows 完全一样：**间隔必须交给输入层**。这里一次 `SendInput` 提交
    `down/up` × repeat，中间用**服务端定时**而不是调用方往返 —— 调用方往返的间隔
    不可控，而系统的双击阈值（默认约 500ms）是可预测的。

    ⚠️ `delay_ms` 仍然是"提交后 sleep"而非"事件间隔"：`SendInput` 数组里没有时间戳
    （`time=0` 表示用系统时间），所以同一批事件的间隔由**系统处理速度**决定，
    通常远小于阈值。分多批提交并 sleep 才是真正可控的间隔 —— 本函数按批提交，
    每批之间 sleep `delay_ms`，这样"两次点击"之间的间隔就是**我们说了算**。

    `focus_window` 为真时先 `SetForegroundWindow`：坐标点击本身不需要前台，
    但**某些应用会把非前台窗口的点击当成"激活"**而不触发按钮（与 Linux 侧
    "合成点击被激活窗口吃掉"同一类现象）。故默认不抢焦点，由调用方声明。
    """
    if focus_window and hwnd:
        set_foreground_window(hwnd)
        time.sleep(0.05)

    down_flag, up_flag = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS[1])
    vx, vy, vw, vh, _, _ = _virtual_metrics()
    if vw <= 0 or vh <= 0:
        return {"ok": False, "reason": "虚拟桌面度量为空（服务会话的典型症状）"}
    norm_x = int(round((x - vx) * 65535 / max(vw - 1, 1)))
    norm_y = int(round((y - vy) * 65535 / max(vh - 1, 1)))
    move_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    batches: list[dict] = []
    for i in range(max(1, repeat)):
        if i:
            time.sleep(max(0.0, delay_ms) / 1000.0)
        inp_move = INPUT(type=INPUT_MOUSE)
        inp_move.u.mi = MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0,
                                   dwFlags=move_flags, time=0, dwExtraInfo=0)
        inputs = [inp_move]
        for flag in (down_flag, up_flag):
            one = INPUT(type=INPUT_MOUSE)
            one.u.mi = MOUSEINPUT(dx=norm_x, dy=norm_y, mouseData=0,
                                  dwFlags=flag | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                                  time=0, dwExtraInfo=0)
            inputs.append(one)
        batches.append(_send_inputs(inputs))
    return {"ok": all(b.get("ok") for b in batches), "batches": batches,
            "repeat": repeat, "pointer": [x, y]}


def send_mouse_drag(x1: int, y1: int, x2: int, y2: int, button: int = 1,
                    steps: int = 10, hold: float = 0.01) -> dict:
    """
    从 `(x1,y1)` 按住拖到 `(x2,y2)`，中间**插值出多步移动**。

    ⚠️ 必须插值（同 Linux 侧的实测教训）：只发一个"从 A 到 B"的移动事件时，
    很多应用判定不出"按住拖动"（拖放目标不激活、画布不跟随、滚动条回弹），
    表现是"拖了但没反应"而 `SendInput` 返回值完全正常。

    ⚠️ 失败**不重试**：重试一次拖拽 = 再拖一遍（文件被移动两次、画布多画一笔）。
    后果不对称，所以宁可报失败让调用方决定。

    步数上限由调用方（`backend.base` 的常量）控制 —— 这里只保证 >= 1。
    """
    down_flag, up_flag = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS[1])
    vx, vy, vw, vh, _, _ = _virtual_metrics()
    if vw <= 0 or vh <= 0:
        return {"ok": False, "reason": "虚拟桌面度量为空"}
    move_flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK

    def _norm(px: int, py: int) -> tuple[int, int]:
        return (int(round((px - vx) * 65535 / max(vw - 1, 1))),
                int(round((py - vy) * 65535 / max(vh - 1, 1))))

    n = max(1, steps)
    seq: list[tuple[int, int, int]] = []          # (dx, dy, 额外 flag)
    nx, ny = _norm(x1, y1)
    seq.append((nx, ny, 0))
    seq.append((nx, ny, down_flag))
    for i in range(1, n + 1):
        px = x1 + (x2 - x1) * i // n
        py = y1 + (y2 - y1) * i // n
        dx, dy = _norm(px, py)
        seq.append((dx, dy, 0))
    nx2, ny2 = _norm(x2, y2)
    seq.append((nx2, ny2, up_flag))

    # 逐事件提交 + 短 sleep：这里**不能**整串一次提交 —— 一次提交会被系统在
    # 同一时刻处理完，中间没有"按住"的持续时间，与不插值没区别。
    ok = True
    for dx, dy, extra in seq:
        inp = INPUT(type=INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=0,
                              dwFlags=move_flags | extra, time=0, dwExtraInfo=0)
        if not _send_inputs([inp]).get("ok"):
            ok = False
            break
        if hold > 0:
            time.sleep(hold)
    return {"ok": ok, "steps": n, "from": [x1, y1], "to": [x2, y2],
            "events": len(seq), "button": button}


_BUTTON_FLAGS: dict[int, tuple[int, int]] = {
    1: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    2: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    3: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
}


def _virtual_metrics() -> tuple[int, int, int, int, int, int]:
    """`(x, y, w, h, screen_w, screen_h)` —— 归一化坐标要用前四个。"""
    m = system_metrics()
    return (int(m.get("SM_XVIRTUALSCREEN") or 0), int(m.get("SM_YVIRTUALSCREEN") or 0),
            int(m.get("SM_CXVIRTUALSCREEN") or 0), int(m.get("SM_CYVIRTUALSCREEN") or 0),
            int(m.get("SM_CXSCREEN") or 0), int(m.get("SM_CYSCREEN") or 0))


# ── 顶层窗口枚举 ─────────────────────────────────────────────────────────

def enum_top_level_windows(visible_only: bool = True) -> list[int]:
    """
    枚举顶层窗口（`EnumWindows`）。

    ⚠️ 与 Linux 侧的 `wmctrl -lpx` 不是一回事：`wmctrl` 那种"只列受 WM 管理的窗口"
    的概念在 Windows 上不存在，这里拿到的是**所有顶层 HWND**（含工具窗、托盘窗）。
    调用方必须自己按"有标题 + 有面积 + WS_VISIBLE 位"过滤 —— 本函数的定位是
    **提供原始集合**，过滤策略留在一处（`describe`/`is_usable_window` 那边）。
    """
    u = user32()
    found: list[int] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):  # noqa: ANN001
        try:
            if not visible_only or u.IsWindowVisible(hwnd):
                found.append(int(hwnd))
        except Exception:  # noqa: BLE001 —— 枚举期间窗口消失是常态，跳过即可
            pass
        return True

    u.EnumWindows.argtypes = [proc_type, wintypes.LPARAM]
    u.EnumWindows.restype = wintypes.BOOL
    u.EnumWindows(proc_type(_cb), 0)
    return found


def is_usable_window(hwnd: int, min_side: int = 40) -> bool:
    """
    过滤掉托盘/工具/消息窗，留下"用户能看见的那个框"。

    三条一起用（任一条单独都不够）：
      · 有非空标题 —— 工具窗与消息窗通常没有；
      · 面积够大 —— 图标级窗口（16x16）不是应用主窗；
      · `WS_VISIBLE` **风格位**而不是 `IsWindowVisible` —— 后者沿父链上溯，
        桌面窗口不可见时会把所有子窗一律报 False（服务会话里正是这样）。
    """
    rect = window_rect(hwnd)
    if not rect:
        return False
    left, top, right, bottom = rect
    if right - left < min_side or bottom - top < min_side:
        return False
    if not window_text(hwnd):
        return False
    return has_ws_visible(hwnd)


# ── 进程名（应用身份）────────────────────────────────────────────────────
#
# Linux 侧的应用名来自 `wmctrl -lpx` 的 WM_CLASS（AT-SPI 的 application name 与它基本对齐）。
# Windows 上没有对应物，最接近的是**进程可执行文件名**：它稳定、人读、且与
# "用户眼里这是哪个应用"一致（notepad / dbeaver / chrome）。

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_image_name(pid: int) -> str | None:
    """
    `QueryFullProcessImageNameW` → 完整映像路径（如 `C:\\Windows\\System32\\notepad.exe`）。

    用 `PROCESS_QUERY_LIMITED_INFORMATION` 而不是 `PROCESS_QUERY_INFORMATION`：
    前者在**受保护/更高完整性级别**的进程上也能成功，而后者会被拒绝。
    拿不到时返回 None —— **不**返回"未知"之类的占位串，那会让调用方以为拿到了名字。
    """
    if not pid:
        return None
    try:
        k = kernel32()
        k.OpenProcess.argtypes = [_DWORD, wintypes.BOOL, _DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            k.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, _DWORD, ctypes.c_wchar_p, ctypes.POINTER(_DWORD)]
            k.QueryFullProcessImageNameW.restype = wintypes.BOOL
            buf = ctypes.create_unicode_buffer(1024)
            size = _DWORD(1024)
            ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value if ok else None
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return None


def process_basename(pid: int) -> str | None:
    """
    进程名（去目录、去 `.exe`、转小写）—— 即我们对外报的**应用名**。

    为什么统一小写去扩展名：用户在 `find(app="Notepad")` 里写的、以及树上标的，
    必须是同一个串；大小写与扩展名差异会造成"存在但搜不到"这类难查的问题。
    """
    path = process_image_name(pid)
    if not path:
        return None
    base = path.replace("/", "\\").rsplit("\\", 1)[-1]
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base.lower() or None


# ── 显示器枚举 ───────────────────────────────────────────────────────────

MONITORINFOF_PRIMARY = 0x1
CCHDEVICENAME = 32


class MONITORINFOEXW(ctypes.Structure):
    """`MONITORINFOEXW`（`GetMonitorInfoW` 的输出，带设备名）。"""

    _fields_ = [
        ("cbSize", _DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", _DWORD),
        ("szDevice", _WCHAR * CCHDEVICENAME),
    ]


def enum_monitors() -> list[dict]:
    """
    枚举显示器：`[{name, x, y, w, h, primary}]`。

    ⚠️ 用 `rcMonitor`（**整块屏**）而不是 `rcWork`（去掉任务栏的可用区）：
    这是给"模型判断坐标落在哪块屏"用的，任务栏那块也是屏的一部分；
    拿 `rcWork` 会让最下沿的点击坐标被判成"不在任何屏上"。

    `cm == 0` 时返回空列表并**由调用方决定怎么降级** —— 不在这里悄悄编一块假屏出来。
    """
    u = user32()
    out: list[dict] = []
    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
        ctypes.POINTER(RECT), wintypes.LPARAM)

    def _cb(hmon, _hdc, _lprect, _lparam):  # noqa: ANN001
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        try:
            u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
            u.GetMonitorInfoW.restype = wintypes.BOOL
            if u.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                out.append({
                    "name": _wchars_to_str(info.szDevice),
                    "x": int(r.left), "y": int(r.top),
                    "w": int(r.right - r.left), "h": int(r.bottom - r.top),
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                })
        except Exception:  # noqa: BLE001 —— 单块屏取失败不该丢掉其余
            pass
        return True

    u.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p,
                                      monitor_enum_proc, wintypes.LPARAM]
    u.EnumDisplayMonitors.restype = wintypes.BOOL
    u.EnumDisplayMonitors(None, None, monitor_enum_proc(_cb), 0)
    return out


# ── 置前（焦点）──────────────────────────────────────────────────────────

def set_foreground_window(hwnd: int) -> bool:
    """
    `SetForegroundWindow`。

    ⚠️ Windows **故意**限制谁能调用成功：只有当前拥有前台窗口的进程、或收到过用户输入的
    进程才能把窗口提到前台（防"弹窗劫持"）。所以它**会失败**，而且失败是设计行为不是 bug。
    调用方必须能接受 False 并继续（坐标点击本身不需要前台，只有键盘输入才需要）。
    """
    try:
        u = user32()
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        return bool(u.SetForegroundWindow(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def is_iconic(hwnd: int) -> bool:
    """窗口是否最小化（`IsIconic`）。

    最小化的窗口 `GetWindowRect` 会返回一个**历史位置的负坐标矩形**，
    拿它算点击坐标必然打偏 —— 所以调用方要先判这个，再决定要不要还原。
    """
    try:
        return bool(user32().IsIconic(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def show_window(hwnd: int, cmd: int = 9) -> bool:
    """`ShowWindow`（默认 SW_RESTORE=9）—— 把最小化的窗口还原，之后矩形才可信。"""
    try:
        u = user32()
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        return bool(u.ShowWindow(wintypes.HWND(hwnd), cmd))
    except Exception:  # noqa: BLE001
        return False


# ── 窗口查询 ─────────────────────────────────────────────────────────────

def class_name(hwnd: int) -> str:
    u = user32()
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
    return buf.value


def window_text(hwnd: int) -> str:
    u = user32()
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
    return buf.value


def pid_of_window(hwnd: int) -> int:
    u = user32()
    pid = wintypes.DWORD(0)
    u.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    u = user32()
    r = RECT()
    if not u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def is_window_visible(hwnd: int) -> bool:
    return bool(user32().IsWindowVisible(wintypes.HWND(hwnd)))


def style_of(hwnd: int) -> int:
    """
    取窗口的 `GWL_STYLE` 原始值。

    ⚠️ 为什么不能只看 `IsWindowVisible()`：它**沿父链上溯** —— 只要桌面窗口本身不可见
    （服务会话的桌面就是），所有子窗口一律报 False，看起来像"窗口没显示"。
    实测在服务会话里，记事本主窗的 `WS_VISIBLE` 位其实是**置位**的。
    判"这个窗口自己有没有被标记为可见"要看 style 位。
    """
    u = user32()
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = ctypes.c_long
    return int(u.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE))


def has_ws_visible(hwnd: int) -> bool:
    return bool(style_of(hwnd) & WS_VISIBLE)


def foreground_window() -> int:
    u = user32()
    u.GetForegroundWindow.restype = wintypes.HWND
    return int(u.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    return bool(user32().IsWindow(wintypes.HWND(hwnd)))


def describe(hwnd: int) -> dict:
    """一个窗口的完整快照（诊断与 JSON 报告用）。"""
    rect = window_rect(hwnd)
    return {
        "hwnd": f"0x{hwnd:x}" if hwnd else "0x0",
        "class": class_name(hwnd),
        "text": window_text(hwnd),
        "pid": pid_of_window(hwnd),
        "rect": list(rect) if rect else None,
        "is_visible": is_window_visible(hwnd),
        "ws_visible": has_ws_visible(hwnd),
    }


# ── ★ 取点下最深子 HWND（定向注入的唯一前提）─────────────────────────────

def root_ancestor(hwnd: int) -> int:
    """取顶层祖先（`GetAncestor(hwnd, GA_ROOT=2)`）——用于判断"这是不是个子窗"。"""
    u = user32()
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetAncestor.restype = wintypes.HWND
    return int(u.GetAncestor(wintypes.HWND(hwnd), 2) or 0)


def window_from_point(x: int, y: int) -> int:
    u = user32()
    u.WindowFromPoint.argtypes = [POINT]
    u.WindowFromPoint.restype = wintypes.HWND
    return int(u.WindowFromPoint(POINT(x, y)) or 0)


def _child_from_point_ex(parent: int, x: int, y: int, flags: int) -> int:
    u = user32()
    u.ChildWindowFromPointEx.argtypes = [wintypes.HWND, POINT, wintypes.UINT]
    u.ChildWindowFromPointEx.restype = wintypes.HWND
    return int(u.ChildWindowFromPointEx(wintypes.HWND(parent), POINT(x, y), flags) or 0)


def deepest_child_at(x: int, y: int, max_depth: int = 16) -> dict:
    """
    屏幕坐标 → 该点下**最深**的子 HWND。返回 `{"ok", "chain", "deepest", "reason"}`。

    ═══ 为什么必须有这一步 ═══

    真机 DEMO 实测：`PostMessage` 把 `WM_CHAR` 投给**顶层主窗口**时，
    经典 Win32 对话框与 UWP 应用**都不生效**；投给**子 HWND** 才生效
    （经典应用是具体的控件窗如 `REDIT50W`；UWP 是 `ApplicationFrameWindow` 下的
    `Windows.UI.Core.CoreWindow`）。补上 `WM_KEYDOWN/WM_KEYUP` 也救不回来。

    所以「定向注入」这条路成立与否，**完全取决于能不能取到子 HWND**。

    ⚠️ 实测补充（2026-09-24，Win11 记事本）：`WindowFromPoint` **自己就已经下钻**了 ——
    它返回的是该点下最深的那扇窗（实测直接拿到 `RichEditD2DPT`，而不是顶层 `Notepad`），
    此时 `ChildWindowFromPointEx` 自然只能再下钻 0 层。所以**不能拿"下钻层数"当判据**：
    那会把"一下就拿到了正确的子窗"误判成"取不到子窗"。
    真正的判据是 **`deepest` 不等于它的顶层祖先**（见返回值里的 `is_child`）。

    ═══ 算法 ═══

    `WindowFromPoint` 拿到最外层，然后 `ChildWindowFromPointEx`
    （`SKIPINVISIBLE | SKIPTRANSPARENT`）逐层下钻，直到子窗不再变化为止。
    每一层都记录下来（`chain`），因为**下钻链本身就是证据**：如果链只有一层，
    说明这条路在该应用上不成立，得另想办法。

    `max_depth` 是防御性的：窗口层级理论上可以很深（自绘控件套娃），
    加个上限免得在异常构型上死循环。同时用 `seen` 集合兜住"子窗等于自己"的环。
    """
    hwnd = window_from_point(x, y)
    if not hwnd:
        return {"ok": False, "reason": "WindowFromPoint 返回空（点在桌面或无窗口处）",
                "chain": [], "deepest": None}

    flags = CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT
    chain: list[dict] = []
    seen: set[int] = set()
    cur = hwnd
    while cur and cur not in seen and len(chain) < max_depth:
        seen.add(cur)
        chain.append(describe(cur))
        child = _child_from_point_ex(cur, x, y, flags)
        if not child or child == cur:
            break
        cur = child

    deepest = chain[-1] if chain else None
    top = root_ancestor(hwnd)
    top_desc = describe(top) if top else None
    return {
        "ok": True,
        "point": [x, y],
        "chain": chain,
        "deepest": deepest,
        "depth": len(chain),
        # ★ 真正的判据（不是层数）：取到的是**子窗**而不是顶层框。
        #   定向注入能不能成立看的就是这一条（DEMO：投给顶层无效、投给子窗有效）。
        "top_level": top_desc,
        "is_child": bool(deepest and top and deepest["hwnd"] != top_desc["hwnd"]),
        # 自检：下钻链上每一环都必须**真的包含这个点**，否则说明下钻逻辑错了
        # （ChildWindowFromPointEx 在边缘像素/坐标换算上的行为与直觉不同）
        "self_check": {
            "point_inside_all": all(_contains(c.get("rect"), x, y) for c in chain),
            "single_pid": len({c.get("pid") for c in chain}) == 1,
        },
    }


def _contains(rect: list[int] | None, x: int, y: int) -> bool:
    if not rect:
        return False
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom
