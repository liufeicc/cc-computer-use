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


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


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
