# -*- coding: utf-8 -*-
"""隐藏桌面「能不能当 Windows 的虚拟屏」的真机探针（验收脚手架，**不进产品**）。

═══ 它回答的唯一问题 ═══

用户要的是「**像 Linux 一样，把软件起在另一块屏里**」：不跟用户抢鼠标焦点，
截图只拿目标窗口、且不受最小化影响。

Linux 的答案是 Xephyr（另一块**显示**）。Windows 上**没有**第二块显示 ——
一个登录会话只有一套输入设备，而且只有「当前输入桌面」能收到物理按键。
所以能对应的只有 **隐藏桌面**（`CreateDesktopW`，同一会话里的另一张**桌面**）。

前面已经实测过（`probe_hidden_desktop_input.py`）：在隐藏桌面**内部**跑一个进程时，
UIA 读树 ✅、`ValuePattern` 写值 ✅、`PrintWindow` ✅、OCR ✅，
但 `SendInput` ❌（error 5）、`mss` 整屏抓屏 ❌。

**⚠️ 那只回答了「进程在隐藏桌面上时能干什么」，没回答真正要紧的那个问题：**

    我们的 MCP server 跑在**用户的默认桌面**上（Claude Code 在哪儿它就在哪儿）。
    它能不能**跨桌面**看见并操作隐藏桌面上的应用？

如果能 —— 那就是 Linux+Xephyr 的等价物：应用起在看不见的地方，server 留在原地操控它，
用户完全不受影响。如果不能 —— 隐藏桌面这条路就只能靠「把 server 也搬进隐藏桌面」，
那是完全不同的部署形态（等于装成服务），代价大得多。

**本探针就是去量「跨桌面」这一条**，而且全程从**用户桌面**这一侧发起。

═══ 判据纪律 ═══

一律取**外部可观测事实**，不看返回值：
  · 「看得见」= 窗口真的被枚举出来 / UIA 真的拿到了元素，不是"函数没抛异常"；
  · 「操作生效」= 目标应用的状态**真的变了**（对话框消失、文本读回命中）；
  · 「不打扰」= 用户桌面的 `GetForegroundWindow` 与 `GetCursorPos` 全程不变。

⚠️ 「不打扰」同样**必须先证明判据有效**（本仓库踩过：没有前台权力时"前台没变"是废话），
所以第 0 节先做对照实验。

跑法（客体上，交互式会话内）：
    python tests\\manual_windows_hidden_desktop_probe.py --out results\\hidden_desktop_probe.json

═══ 2026-09-30 真机结论：**跨桌面全部成立**（18 条判据全过，`EXITCODE=0`）═══

| 判据 | 结果 |
|---|---|
| `EnumWindows`（本桌面）看不到隐藏桌面的窗口 | ✅ 交集 0 个（预期如此） |
| `EnumDesktopWindows(hdesk)` 看得到 | ✅ 11~13 个 |
| **UIA 从用户桌面 `ControlFromHandle` 挂上隐藏桌面的窗口** | ✅ **挂得上** |
| **跨桌面元素级 `Invoke`（对话框的「确定」）** | ✅ **对话框真的消失了** |
| **跨桌面 `ValuePattern.SetValue`** | ✅ 读回命中（`HIDOPS<pid>`） |
| **跨桌面定向键盘 `PostMessage(WM_CHAR)` → 子 HWND** | ✅ 读回命中 |
| **跨桌面 `PrintWindow`（按窗口渲染）** | ✅ 非黑 1.0000 / 0.9784，791x569 / 861x752 |
| 用户桌面：前台没变 / 指针没动 / 窗口集合没变 | ✅✅✅ |

**所以：MCP server 不需要搬进隐藏桌面。** 它留在用户会话里，就能把应用起在
看不见的桌面上、读它的树、点它的按钮、往里打字、只截它那一扇窗 ——
**用户桌面的前台、指针、窗口集合全程一个都没动**。

对应到用户的两条诉求：
  · 「不抢鼠标和焦点」→ 隐藏桌面 + 元素级/定向通道，成立（**但见下面的边界**）；
  · 「单独截某个软件、不受最小化影响」→ `PrintWindow` 按窗口渲染，成立
    （不受遮挡；最小化与否由我们控制，因为我们自己决定要不要最小化它）。

═══ ⚠️ 边界（必须一起记住，否则会把它当成万能药）═══

1. **没有元素树的应用（SWT/Java、自绘控件、游戏）在隐藏桌面上做不了** ——
   只能坐标点击，而隐藏桌面上 `SendInput` 不可用（error 5），`PostMessage` 鼠标又
   已被证明**四种技法全失效**（见 `manual_windows_directed_probe.py`）。
   即：隐藏桌面只对「**有元素树**」的应用成立，对灰区应用是死路。
2. **桌面堆只有 768KB**（`SharedSection`），本探针只起了 2 个应用，**未做多应用压测**。
3. 隐藏桌面上没有真正的用户，**某些应用可能不在不可见时绘制**（本探针里记事本与
   winver 都正常出图，但这不等于所有应用都行）。

═══ ⚠️ 判据层面的一课（本探针第二版踩的，比结论更值钱）═══

第二版把「指针没动」判成 ❌：基线取到 `(640,336)`、收尾读到 `(960,504)`，看着像指针
跳到了屏幕正中。**而那个进程当时是 150% 缩放（144 DPI），640×1.5 = 960、336×1.5 = 504
—— 指针一动没动，翻转的是坐标系**：设 DPI 感知之前 `GetCursorPos` 返回**虚拟化坐标**，
之后返回**物理坐标**。

**教训：跨坐标系比较任何坐标都是无效判据。** 现在 DPI 感知提到**最前面**（第 0 节），
先固定坐标系、再取基线。另外补了一条**反证**：在没有控制台的 `pythonw.exe` 里
做同一个调用，读数同样差 1.5 倍 —— 证明这不是"控制台窗口重排把指针拉回去了"，
判据刻意写成**比例**而非相等，因为只有比例能区分「坐标系翻转」与「真的挪了指针」。
（同一类坑本项目在 2026-09-28 也踩过一次，那次是坐标空间在 UIA 与注入之间翻转。）
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import pathlib
import subprocess
import sys
import time
from ctypes import wintypes

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_HERE = pathlib.Path(__file__).resolve()
_REPO = next((p for p in _HERE.parents if (p / "src" / "computer_use_mcp").is_dir()), None)
if _REPO is not None:
    sys.path.insert(0, str(_REPO / "src"))

DESKTOP_NAME = "CCCUOpsProbe"
_RESULTS: dict[str, dict] = {}


def ok(label: str, cond: bool, detail: str = "") -> bool:
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'OK  ' if cond else 'FAIL'} | {label}" + (f" | {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def note(label: str, detail: str = "") -> None:
    """非判据的观察（不计入成败，但进报告）。"""
    print(f"  ·  {label}" + (f": {detail}" if detail else ""), flush=True)


# ══════════════════════ ctypes：桌面与进程创建 ══════════════════════

class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


DESKTOP_ALL_ACCESS = 0x000F01FF
CREATE_UNICODE_ENVIRONMENT = 0x00000400


def create_desktop(name: str) -> int:
    """建（或打开）一张桌面，返回句柄（0 = 失败）。**句柄只在本进程内有效**。"""
    u = ctypes.windll.user32
    u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    u.CreateDesktopW.restype = wintypes.HANDLE
    h = u.CreateDesktopW(name, None, None, 0, DESKTOP_ALL_ACCESS, None)
    if not h:
        u.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL,
                                   wintypes.DWORD]
        u.OpenDesktopW.restype = wintypes.HANDLE
        h = u.OpenDesktopW(name, 0, False, DESKTOP_ALL_ACCESS)
    return int(h or 0)


def spawn_on_desktop(argv: list[str], desktop: str) -> int:
    """
    把进程放到指定桌面上起，**不等待**（等待会让探针卡在子进程生命周期上）。

    ⚠️ `subprocess.STARTUPINFO` 上**根本没有 `lpDesktop` 字段** —— 早期探针往它上面
    设属性，Python 只是记到了一个普通对象上，`CreateProcess` 从没看到它，
    于是整场探针其实跑在**交互桌面**上（典型静默失效）。所以这里自己拼 `STARTUPINFOW`。
    """
    k32 = ctypes.windll.kernel32
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = wintypes.BOOL

    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    si.lpDesktop = desktop
    pi = _PROCESS_INFORMATION()
    env = dict(os.environ)
    envb = ctypes.create_unicode_buffer("".join(f"{k}={v}\0" for k, v in env.items()) + "\0")
    ok_spawn = k32.CreateProcessW(
        None, subprocess.list2cmdline(argv), None, None, False,
        CREATE_UNICODE_ENVIRONMENT, ctypes.cast(envb, ctypes.c_void_p), None,
        ctypes.byref(si), ctypes.byref(pi))
    if not ok_spawn:
        note("CreateProcessW 失败", f"err={ctypes.get_last_error()}")
        return 0
    return int(pi.dwProcessId)


def enum_desktop_windows(hdesk: int) -> list[int]:
    """
    枚举**某张指定桌面**上的顶层窗（跨桌面看窗口的唯一正路）。

    ⚠️ 对比：`EnumWindows` 只枚举**调用线程所在桌面**的窗 —— 用户桌面上的进程用它
    永远看不到隐藏桌面。这正是本探针要区分的那一点。
    """
    out: list[int] = []
    u = ctypes.windll.user32
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):  # noqa: ANN001
        out.append(int(hwnd))
        return True

    u.EnumDesktopWindows.argtypes = [wintypes.HANDLE, CB, wintypes.LPARAM]
    u.EnumDesktopWindows.restype = wintypes.BOOL
    u.EnumDesktopWindows(wintypes.HANDLE(hdesk), CB(_cb), 0)
    return out


def enum_windows_own_desktop() -> list[int]:
    """枚举**本线程所在桌面**的顶层窗（对照用 —— 预期看不到隐藏桌面）。"""
    from computer_use_mcp.backend.windows.driver import win32
    return win32.enum_top_level_windows(visible_only=False)


def cursor_pos() -> tuple[int, int]:
    u = ctypes.windll.user32
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    p = POINT()
    u.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def foreground() -> tuple[int, str]:
    from computer_use_mcp.backend.windows.driver import win32
    h = win32.foreground_window()
    return (h, win32.window_text(h) if h else "")


def snapshot_user_desktop() -> dict:
    from computer_use_mcp.backend.windows.driver import win32
    fg_hwnd, fg_title = foreground()
    return {"fg_hwnd": fg_hwnd, "fg_title": fg_title, "cursor": cursor_pos(),
            "windows": sorted(enum_windows_own_desktop()),
            "top": len(win32.enum_top_level_windows(visible_only=True))}


# ★ 指针/前台的**轨迹**：只在前后各取一次时，只能说"变了"，说不出"哪一步变的"。
#   本探针第一版就栽在这 —— 指针从中部跳到屏幕正中，而所有"看起来该动指针"的步骤
#   （UIA / PostMessage / PrintWindow）一个个看都不像。把每个阶段都记一笔，
#   才能把「谁知道动了指针」缩到具体一步。非判据，但进报告。
_TRAIL: list[dict] = []


def mark(label: str) -> None:
    fg_hwnd, fg_title = foreground()
    cur = cursor_pos()
    _TRAIL.append({"step": label, "cursor": list(cur), "fg": fg_title})
    print(f"     [mark] {label}: 指针={cur} 前台={fg_title!r}", flush=True)


def print_window(hwnd: int):  # noqa: ANN202
    """`PrintWindow(hwnd, PW_RENDERFULLCONTENT)` → (PIL 图, 说明)。按窗口渲染，不走屏幕 DC。"""
    from PIL import Image

    from computer_use_mcp.backend.windows.driver import win32
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    r = win32.window_rect(hwnd)
    if not r:
        return None, "取不到窗口矩形"
    w, h = max(1, r[2] - r[0]), max(1, r[3] - r[1])
    hdc = u.GetWindowDC(ctypes.c_void_p(hwnd))
    memdc = g.CreateCompatibleDC(ctypes.c_void_p(hdc))
    bmp = g.CreateCompatibleBitmap(ctypes.c_void_p(hdc), w, h)
    g.SelectObject(ctypes.c_void_p(memdc), ctypes.c_void_p(bmp))
    api_ok = bool(u.PrintWindow(ctypes.c_void_p(hwnd), ctypes.c_void_p(memdc), 2))

    class _BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bmi = _BMIH()
    bmi.biSize = ctypes.sizeof(_BMIH)
    bmi.biWidth, bmi.biHeight = w, -h
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    g.GetDIBits(ctypes.c_void_p(memdc), ctypes.c_void_p(bmp), 0, h, buf,
                ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    g.DeleteObject(ctypes.c_void_p(bmp))
    g.DeleteDC(ctypes.c_void_p(memdc))
    u.ReleaseDC(ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc))
    px = list(img.convert("L").resize((160, 100)).getdata())
    nonblack = 1.0 - sum(1 for v in px if v <= 8) / max(1, len(px))
    return img, f"api_ok={api_ok} {w}x{h} 非黑={nonblack:.4f}"


# ══════════════════════════════ 主流程 ══════════════════════════════

def find_window_by_class(hdesk: int, cls: str) -> int:
    """在隐藏桌面上按类名找一个有面积的顶层窗。"""
    from computer_use_mcp.backend.windows.driver import win32
    for hwnd in enum_desktop_windows(hdesk):
        try:
            if win32.class_name(hwnd) != cls:
                continue
            r = win32.window_rect(hwnd)
            if r and (r[2] - r[0]) > 80 and (r[3] - r[1]) > 40:
                return hwnd
        except Exception:  # noqa: BLE001
            continue
    return 0


def wait_for_class(hdesk: int, cls: str, timeout: float = 20.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = find_window_by_class(hdesk, cls)
        if h:
            return h
        time.sleep(0.5)
    return 0


# ── UIA 遍历辅助（跨桌面挂上来的元素，用法与普通元素完全一样）──────────

def walk_uia(node, depth: int = 0, limit: int = 400):  # noqa: ANN001, ANN202
    """深度优先遍历 UIA 子树，产出 `(元素, 深度)`。**任何一层抛异常都不该中断整场**。"""
    if node is None or depth > 40 or limit <= 0:
        return
    yield node, depth
    try:
        children = list(node.GetChildren())
    except Exception:  # noqa: BLE001
        return
    for ch in children:
        yield from walk_uia(ch, depth + 1, limit - 1)


def find_button(root):  # noqa: ANN001, ANN202
    """在子树里找第一个按钮（Invoke 判据要用）。"""
    from computer_use_mcp.backend.windows.driver import _uia
    uia = _uia.import_uia()
    try:
        want = uia.ControlType.ButtonControl
    except Exception:  # noqa: BLE001
        want = None
    for node, _d in walk_uia(root):
        try:
            if want is not None and node.ControlType == want:
                return node
        except Exception:  # noqa: BLE001
            continue
    return None


def find_value_node(root):  # noqa: ANN001, ANN202
    """
    在子树里找第一个**有 ValuePattern** 的元素（记事本的编辑区）。

    ⚠️ 判据是「`GetValuePattern()` 拿得到且 `.Value` 是字符串」，不是看角色名 ——
    角色名是翻译层的产物，而这里要的是**通道本身在不在**（跨桌面时这点尤其要问清楚）。
    """
    for node, _d in walk_uia(root):
        try:
            vp = node.GetValuePattern()
            if isinstance(vp.Value, str):
                return node
        except Exception:  # noqa: BLE001
            continue
    return None


def read_value(root) -> str:  # noqa: ANN001
    node = find_value_node(root)
    if node is None:
        return ""
    try:
        return str(node.GetValuePattern().Value or "")
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    print("=== 0) 就绪 ===", flush=True)
    from computer_use_mcp.backend.windows.driver import apps, session_info, win32

    sess = session_info.check()
    if not ok("会话可信（有真桌面）", bool(sess.get("interactive")),
              f"session={sess.get('session_id')} 窗口站={sess.get('window_station')}"):
        return 3

    # ★★ DPI 感知必须在**任何坐标读数之前**设好 ★★
    #
    # 本项目为这件事吃过一次亏，而本文件的第二版**又原样踩了一遍**：进程在设 DPI 感知
    # *之前*是"感知不到 DPI"的，`GetCursorPos` 于是返回**被虚拟化过**的坐标；
    # 设完 PMv2 之后，同一个物理位置返回**物理坐标**。两次读数一对比，看起来指针
    # "从 (640,336) 跳到了 (960,504)" —— 而 640×1.5 = 960、336×1.5 = 504 恰好成立，
    # 即**指针一动没动**，翻转的是**坐标系**（该机是 150% 缩放）。
    #
    # 教训（判据层面，比结论更值钱）：**跨坐标系比较任何坐标都是无效判据**。
    # 所以基线必须在设完 DPI 感知之后取 —— 先定坐标系，再开始量。
    from computer_use_mcp.backend.windows.driver import _uia as _uia_mod
    dpi_report = _uia_mod.ensure_dpi_awareness()
    note("DPI 感知（坐标空间的前提）", json.dumps(dpi_report, ensure_ascii=False)[:200])
    sys_dpi = (dpi_report.get("after") or {}).get("GetDpiForSystem")
    note("系统 DPI", f"{sys_dpi}（144 = 150% 缩放；虚拟化坐标与物理坐标差 1.5 倍）")

    before = snapshot_user_desktop()
    mark("0) 开场（坐标系已固定）")
    note("用户桌面：前台", f"{before['fg_title']!r} 指针={before['cursor']} "
                          f"顶层窗={before['top']} 个")

    # ── 0b) 对照实验：本进程有没有改前台的权力（否则"前台没变"是废话）──
    print("\n=== 0b) 对照实验：本进程有前台权力吗 ===", flush=True)
    anchor = None
    for r in apps.window_rows(limit=50):
        if r.get("app") not in ("explorer", "progman") and r.get("w", 0) > 200:
            anchor = int(r["id"], 16)
            break
    power = False
    if anchor:
        win32.set_foreground_window(anchor)
        time.sleep(0.5)
        power = foreground()[0] == anchor
        win32.set_foreground_window(before["fg_hwnd"])
        time.sleep(0.4)
    ok("对照实验:本进程有前台权力", power,
       "否则「前台没变」是恒真判据，本轮所有不打扰结论都失效")
    if not power:
        note("警告", "本轮拿不到前台权力 —— 下面「不打扰」的结论一律降级看待")

    # ── 1) 建隐藏桌面，把两个应用起进去 ──
    print("\n=== 1) 建隐藏桌面并起应用（全部从用户桌面这一侧发起）===", flush=True)
    subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"],  # noqa: S603,S607
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["taskkill", "/F", "/IM", "winver.exe"],  # noqa: S603,S607
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(1.0)

    mark("1a) 建桌面之前")
    hdesk = create_desktop(DESKTOP_NAME)
    if not ok("建出隐藏桌面", hdesk != 0, f"hdesk=0x{hdesk:x} 名字={DESKTOP_NAME}"):
        return 5

    pid_np = spawn_on_desktop(["notepad.exe"], DESKTOP_NAME)
    pid_wv = spawn_on_desktop(["winver.exe"], DESKTOP_NAME)
    mark("1b) 应用已投进隐藏桌面")
    ok("应用被投进隐藏桌面（CreateProcess 成功）",
       pid_np != 0 or pid_wv != 0, f"notepad pid={pid_np} winver pid={pid_wv}")

    hwnd_np = wait_for_class(hdesk, "Notepad") or wait_for_class(hdesk, "Notepad")
    mark("1c) 隐藏桌面窗口已就位")
    hwnd_dlg = wait_for_class(hdesk, "#32770")
    note("隐藏桌面上的窗口", f"Notepad=0x{hwnd_np:x} 对话框=0x{hwnd_dlg:x}")

    # ── 2) ★ 跨桌面可见性（本探针的核心）──
    print("\n=== 2) ★ 用户桌面上的进程，能不能看见隐藏桌面的窗口 ===", flush=True)
    own = set(enum_windows_own_desktop())
    theirs = set(enum_desktop_windows(hdesk))
    note("本桌面枚举到的顶层窗数", str(len(own)))
    note("隐藏桌面枚举到的顶层窗数", str(len(theirs)))
    ok("EnumWindows（本桌面）看不到隐藏桌面的窗口", not (own & theirs),
       f"交集 {len(own & theirs)} 个 —— 这是预期行为，故普通窗口枚举必须换路")
    ok("EnumDesktopWindows(hdesk) 看得到隐藏桌面的窗口", bool(theirs),
       f"{len(theirs)} 个：{sorted('0x%x' % h for h in theirs)[:6]}")

    # 2b-0) 确认**初始化 UIA 那条链**不会动指针。
    #       坐标系自第 0 节起就没变过，所以这里的比较是有效的（这正是 A 那一步的意义）。
    from computer_use_mcp.backend.windows.driver import _uia
    mark("2b-0a) import_uia 之前")
    import uiautomation  # noqa: PLC0415,F401
    mark("2b-0b) import uiautomation 之后")
    cur_a = tuple(_TRAIL[-2]["cursor"])
    cur_b = tuple(_TRAIL[-1]["cursor"])
    ok("初始化 UIA 不动用户指针", cur_a == cur_b, f"{cur_a} → {cur_b}")

    # 2b-1) ★ 给上面那条结论**补一个反证**：同一个 API 调用，在**没有控制台**的
    #       进程里（pythonw）读数同样变了 —— 证明这**不是**控制台窗口重排导致的
    #       （console 确实有把指针拉回窗口内的行为，那是一条真实存在的可能解释）。
    #       判据刻意取**比例**而不是相等：原点与读数都变 1.5 倍 ⇒ 坐标系翻转；
    #       只变一边 ⇒ 才是真的挪了指针。这样写，判据本身就能区分这两种世界。
    php = pathlib.Path(os.environ.get("TEMP", ".")) / f"cccu_dpi_{os.getpid()}.json"
    src = (
        "import ctypes,json,sys,pathlib\n"
        "k=ctypes.windll.kernel32\n"
        "class P(ctypes.Structure):_fields_=[('x',ctypes.c_long),('y',ctypes.c_long)]\n"
        "def cur():\n"
        "    p=P();ctypes.windll.user32.GetCursorPos(ctypes.byref(p));return [p.x,p.y]\n"
        "c0=cur()\n"
        "u=ctypes.windll.user32\n"
        "u.SetProcessDpiAwarenessContext.argtypes=[ctypes.c_void_p]\n"
        "u.SetProcessDpiAwarenessContext.restype=ctypes.c_bool\n"
        "api=bool(u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))\n"
        "c1=cur()\n"
        "con=bool(k.GetConsoleWindow())\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps("
        "{'api_ok':api,'before':c0,'after':c1,'has_console':con}),encoding='utf-8')\n"
    )
    try:
        pw = pathlib.Path(sys.executable).with_name("pythonw.exe")
        if not pw.exists():
            pw = pathlib.Path(sys.executable)
        subprocess.run([str(pw), "-c", src, str(php)], timeout=60, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info2 = json.loads(php.read_text(encoding="utf-8")) if php.exists() else {}
        note("无控制台进程里设 DPI 感知", json.dumps(info2, ensure_ascii=False))
        b0, b1 = info2.get("before") or [0, 0], info2.get("after") or [0, 0]
        ratio_ok = (b0[0] and b1[0] and abs(b1[0] / b0[0] - 1.5) < 0.02)
        same = b0 == b1
        ok("★ 无控制台进程里的读数变化也是坐标系翻转（比例 1.5，非指针移动）",
           bool(same or ratio_ok),
           f"has_console={info2.get('has_console')} {b0} → {b1} "
           f"（比 {round(b1[0]/b0[0], 3) if b0[0] else 'n/a'}；"
           f"1.5 = 虚拟化↔物理，指针未动）")
    except Exception as exc:  # noqa: BLE001
        note("无控制台对照实验没跑成", f"{type(exc).__name__}: {str(exc)[:100]}")

    uia = _uia.import_uia()
    mark("2b) UIA 库已初始化")
    ctrl = None
    if hwnd_dlg or hwnd_np:
        target_hwnd = hwnd_dlg or hwnd_np
        try:
            ctrl = uia.ControlFromHandle(target_hwnd)
            ok("★ UIA 从用户桌面能挂上隐藏桌面的窗口（ControlFromHandle）",
               ctrl is not None, f"hwnd=0x{target_hwnd:x}")
        except Exception as exc:  # noqa: BLE001
            ok("★ UIA 从用户桌面能挂上隐藏桌面的窗口（ControlFromHandle）", False,
               f"{type(exc).__name__}: {str(exc)[:120]}")
    else:
        ok("★ UIA 从用户桌面能挂上隐藏桌面的窗口（ControlFromHandle）", False,
           "隐藏桌面上没找到可用的目标窗口（应用没起来）")

    # ── 3) 跨桌面元素级操作 ──
    print("\n=== 3) 跨桌面元素级操作（Invoke / SetValue）===", flush=True)
    ctrl_dlg = ctrl_np = None
    if ctrl is not None:
        if hwnd_dlg:
            try:
                ctrl_dlg = uia.ControlFromHandle(hwnd_dlg)
                note("对话框子树节点数", str(sum(1 for _ in walk_uia(ctrl_dlg))))
            except Exception as exc:  # noqa: BLE001
                note("挂对话框失败", f"{type(exc).__name__}: {str(exc)[:100]}")
        # 3a-0) ★ 先抓图（对话框还活着），再谈 Invoke
        print("\n=== 3a-0) 跨桌面 PrintWindow（按窗口渲染，必须在 Invoke 之前）===",
              flush=True)
        for tag, h in (("notepad", hwnd_np), ("dialog", hwnd_dlg)):
            if not h:
                continue
            try:
                img, desc = print_window(h)
                ok(f"★ 跨桌面 PrintWindow 出图[{tag}]",
                   img is not None and "非黑=0.0000" not in desc, desc)
            except Exception as exc:  # noqa: BLE001
                ok(f"★ 跨桌面 PrintWindow 出图[{tag}]", False,
                   f"{type(exc).__name__}: {str(exc)[:100]}")

        # 3a) 找对话框上的按钮并 Invoke（判据 = 对话框真的消失）
        btn = find_button(ctrl_dlg) if ctrl_dlg is not None else None
        if btn is not None:
            ok("跨桌面找到对话框的按钮元素", True, f"名字={getattr(btn, 'Name', '')!r}")
            try:
                btn.GetInvokePattern().Invoke()
            except Exception as exc:  # noqa: BLE001
                # 动作生效后目标窗被销毁、手里的 COM 对象随之失效，于是这次调用"报错"。
                # **不能拿异常当失败判据**（真机抓到过反例：抛 COMError 而对话框确实关了）。
                note("Invoke 抛异常（可能是生效后对象失效）",
                     f"{type(exc).__name__}: {str(exc)[:80]}")
            time.sleep(2.0)
            gone = find_window_by_class(hdesk, "#32770") == 0
            ok("★ 跨桌面 Invoke 生效（对话框消失）", gone,
               "对话框确实消失了" if gone else "对话框还在（含 2s 复检）")
        else:
            note("对话框上没找到按钮元素", "跳过 Invoke 判据")

        # 3b) 记事本 SetValue（读回是独立通道，判据不看返回值）
        if hwnd_np:
            try:
                ctrl_np = uia.ControlFromHandle(hwnd_np)
            except Exception:  # noqa: BLE001
                ctrl_np = None
            tok = f"HIDOPS{os.getpid()}"
            node = find_value_node(ctrl_np) if ctrl_np is not None else None
            wrote = False
            if node is not None:
                try:
                    node.GetValuePattern().SetValue(tok)
                    wrote = True
                except Exception as exc:  # noqa: BLE001
                    note("SetValue 抛异常", f"{type(exc).__name__}: {str(exc)[:80]}")
            time.sleep(1.0)
            rb = read_value(ctrl_np) if ctrl_np is not None else ""
            ok("★ 跨桌面 SetValue 生效（读回命中）", tok in rb,
               f"写入={wrote} 读回={rb[:40]!r}")

        # ── 4) 跨桌面定向键盘（PostMessage 不进输入队列，理应跨得过去）──
        print("\n=== 4) 跨桌面定向键盘（PostMessage WM_CHAR）===", flush=True)
        if ctrl_np is not None:
            u = ctypes.windll.user32
            u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_void_p, ctypes.c_void_p]
            u.PostMessageW.restype = ctypes.c_int
            # 目标：编辑区自己所在的那个子 HWND（UIA 的 NativeWindowHandle）
            child = 0
            node = find_value_node(ctrl_np)
            try:
                child = int(getattr(node, "NativeWindowHandle", 0) or 0)
            except Exception:  # noqa: BLE001
                child = 0
            tok2 = f"KEY{os.getpid()}"
            posted = 0
            if child:
                for ch in tok2:
                    posted += int(u.PostMessageW(ctypes.c_void_p(child), 0x0102,
                                                 ctypes.c_void_p(ord(ch)),
                                                 ctypes.c_void_p(0)))
            time.sleep(1.0)
            rb2 = read_value(ctrl_np)
            ok("★ 跨桌面定向键盘生效", bool(tok2 in rb2),
               f"投递={posted} 子窗=0x{child:x} 读回={rb2[:60]!r}")
        else:
            note("跳过定向键盘", "记事本上没挂到可写元素")

        # ── 5) 跨桌面按窗口渲染（截图那条）──
        # ⚠️ 顺序要紧：**必须在 3a) 的 Invoke 之前抓对话框** —— Invoke 会把对话框关掉，
        #    之后再抓只会得到「取不到窗口矩形」。探针第一版把本节排在 3) 后面，
        #    于是白白丢了一条判据，还看起来像"PrintWindow 对对话框不成立"。

    # ── 6) 用户桌面有没有被打扰 ──
    print("\n=== 6) 用户桌面有没有被打扰 ===", flush=True)
    mark("6) 收尾前")
    after = snapshot_user_desktop()
    if power:
        ok("用户桌面:前台没变", before["fg_title"] == after["fg_title"],
           f"{before['fg_title']!r} → {after['fg_title']!r}")
        ok("用户桌面:指针没动", before["cursor"] == after["cursor"],
           f"{before['cursor']} → {after['cursor']}")
        ok("用户桌面:窗口集合没变（应用没跑到用户桌面上）",
           before["windows"] == after["windows"],
           f"前 {len(before['windows'])} 个 → 后 {len(after['windows'])} 个")
    else:
        note("跳过「不打扰」判据", "本轮没有前台权力，判据恒真、无意义")

    # ── 7) 收尾 ──
    print("\n=== 7) 收尾 ===", flush=True)
    subprocess.run(["taskkill", "/F", "/IM", "notepad.exe"],  # noqa: S603,S607
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(["taskkill", "/F", "/IM", "winver.exe"],  # noqa: S603,S607
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(1.0)

    if _TRAIL:
        print("\n=== 指针/前台轨迹（用来定位是哪一步动的）===", flush=True)
        for row in _TRAIL:
            print(f"  {row['step']:<28} 指针={tuple(row['cursor'])} 前台={row['fg']!r}",
                  flush=True)
        cur0 = tuple(_TRAIL[0]["cursor"])
        for row in _TRAIL[1:]:
            if tuple(row["cursor"]) != cur0:
                print(f"  ★ 指针在「{row['step']}」这一步前后发生了变化："
                      f"{cur0} → {tuple(row['cursor'])}", flush=True)
                cur0 = tuple(row["cursor"])

    failed = [k for k, v in _RESULTS.items() if not v["ok"]]
    print(f"\n[probe] 判据 {len(_RESULTS)} 条，失败 {len(failed)} 条", flush=True)
    for k in failed:
        print(f"  ❌ {k} — {_RESULTS[k]['detail']}", flush=True)
    if args.out:
        p = pathlib.Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"results": _RESULTS, "failed": failed, "trail": _TRAIL,
                                "dpi": dpi_report, "system_dpi": sys_dpi},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  报告已写入 {p}", flush=True)
    return 0 if not failed else 5


if __name__ == "__main__":
    raise SystemExit(main())
