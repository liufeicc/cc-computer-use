"""
按窗口渲染（`backend.windows.driver.capture`）—— 「只截某一个软件」那条路。

═══ 为什么必须按窗口渲染，而不是裁一块屏幕 ═══

Linux 侧的截图是「抓一块**屏幕**区域」：那在沙箱里完全够用，因为整块 Xephyr 屏
就是我们的工作台，上面除了目标应用什么都没有。

**Windows 上没有这样的屏** —— 用户的桌面上同时开着浏览器、聊天工具、IDE。
裁一块矩形只能得到「屏幕上那一块」，于是：

  · **被别的窗口盖住时，截到的是盖住它的那扇窗**（OCR 因此会读到完全无关的内容）；
  · 目标窗口挪到屏幕外一部分时，裁出来的图是残缺的；
  · 用户桌面上的其它窗口内容会**一起进模型上下文**（隐私与准确性双输）。

`PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT)` 让**窗口自己给自己画一张像**：
不经过屏幕 DC、**不受遮挡影响**、不需要窗口在前台。真机实测非黑占比
0.94~1.00（`docs/PR/windows-driver/README.md`），隐藏桌面上同样可用
（`tests/manual_windows_hidden_desktop_probe.py`）。

═══ ⚠️ 黑图哨兵：判「抓不到」必须带窗口状态证据 ═══

`PrintWindow` **返回真不等于拿到了内容** —— 隐藏桌面/服务会话下它返回 `api_ok=True`
却给一张全黑图（本项目在 DEMO 阶段正是被这个骗过一次）。所以必须验内容。

但**不能只按"暗"判失败**：深色主题的界面本来就大片近黑，纯像素判据会把正常窗口
冤枉成失败。这里用的是**两条一起**：

  1. **近黑像素占比接近 100%**（阈值 `_BLACK_FRACTION_FAIL`）——
     真失败的 PrintWindow 给的是**全零缓冲**，一个非零像素都没有；
     而深色主题至少还有标题栏文字、边框、光标，非黑占比不会真的到 0。
  2. **窗口状态**（`IsIconic` / `IsWindowVisible` / 矩形是否合法）——
     它把「为什么抓不到」变成模型能行动的一句话（"窗口最小化了"），
     而不是一句"抓不到"。

⚠️ **还有一类失败哨兵拦不住：最小化的窗口**。实测它**不黑**，而是画出一张
237x39 的标题栏细条（非黑占比 1.0）—— 模型会把那张细条当成窗口的全部内容。
故 `print_window` 在动手画之前先查 `IsIconic`（见那里的注释）。

⚠️ 阈值刻意取"**几乎全黑**"而不是"不够亮"：本次判据的误判方向**不对称** ——
把一个正常窗口判成失败，模型会去走更贵的坐标路径（浪费但不出错）；
把一张全黑图当成"界面就是这样"，模型会基于**空白**做决策（那才是真出错）。
所以宁可严一点点，但也不能严到冤枉深色主题。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from . import win32

# `PrintWindow` 的第二个标志。PW_RENDERFULLCONTENT = 0x2（Win 8.1+）：
# **不带它的话 DWM 合成的内容画不出来**（Win10/11 上表现为大面积黑或只有边框）。
PW_RENDERFULLCONTENT = 0x00000002

# 近黑判定与失败阈值（见模块 docstring 的「黑图哨兵」）
_NEAR_BLACK = 8
_BLACK_FRACTION_FAIL = 0.999


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def window_state(hwnd: int) -> dict[str, Any]:
    """
    窗口的**状态证据** —— 用来把「抓不到」翻译成一句模型能行动的话。

    只读几个属性，**不碰 a11y 树**（`child_count` 那类调用会逼目标应用惰性建树，
    对 Chrome/Electron 是能拖垮桌面的操作，见 CLAUDE.md）。
    """
    st: dict[str, Any] = {"hwnd": hwnd, "minimized": None, "visible": None,
                          "ws_visible": None, "rect": None}
    try:
        st["minimized"] = bool(win32.is_iconic(hwnd))
    except Exception:  # noqa: BLE001
        pass
    try:
        st["visible"] = bool(win32.is_window_visible(hwnd))
    except Exception:  # noqa: BLE001
        pass
    try:
        st["ws_visible"] = bool(win32.has_ws_visible(hwnd))
    except Exception:  # noqa: BLE001
        pass
    try:
        r = win32.window_rect(hwnd)
        st["rect"] = list(r) if r else None
    except Exception:  # noqa: BLE001
        pass
    return st


def black_reason(state: dict[str, Any]) -> str:
    """按窗口状态给出「为什么抓不到」的可行动说明。状态正常时返回空串。"""
    if state.get("minimized"):
        return ("窗口处于**最小化**状态 —— 系统不会为它渲染内容。"
                "可先把它还原（`launch_app` 起的应用由我们控制，通常不该最小化）。")
    if state.get("visible") is False or state.get("ws_visible") is False:
        return "窗口**不可见**（已隐藏）—— 系统不会为它渲染内容。"
    r = state.get("rect")
    if not r or (r[2] - r[0]) <= 0 or (r[3] - r[1]) <= 0:
        return f"窗口矩形非法或为空（{r}）—— 没有可渲染的画布。"
    return ""


def print_window(hwnd: int) -> tuple[Any | None, dict[str, Any]]:
    """
    让 `hwnd` 自己渲染一张图，返回 `(PIL.Image | None, info)`。

    `info` 里的键（**都要原样带进 meta 给模型看**）：
      · `ok`         —— 拿到了**有内容**的图；
      · `nonblack`   —— 非黑像素占比（越低越可疑，0.0 就是一张全零图）；
      · `ms`         —— `PrintWindow` 这一次调用的墙钟耗时；
      · `state`      —— 窗口状态证据（见 `window_state`）；
      · `reason`     —— `ok=False` 时给模型的一句话。

    ⚠️ **`ms` 要量出来**：已知 `PrintWindow` 在某些 Chromium/GPU 路径上会**阻塞**，
    而它在我们的进程里是同步调用、杀不掉。量出来才能判断"偶发卡住"是不是它。
    """
    info: dict[str, Any] = {"hwnd": hwnd, "ok": False, "api_ok": False,
                            "ms": None, "nonblack": None, "reason": ""}
    state = window_state(hwnd)
    info["state"] = state

    if not hwnd or not win32.is_window(hwnd):
        info["reason"] = f"hwnd 无效（0x{hwnd:x}）"
        return None, info

    # ★★ 最小化必须在**画之前**就判掉 ★★
    #
    # 2026-09-30 真机实测：最小化的窗口 `PrintWindow` **不会失败、也不给黑图** ——
    # 它老老实实画出一张 **237x39 的标题栏细条**（非黑占比 1.0，哨兵完全拦不住）。
    # 后果比黑图更坏：模型拿到一张"有内容的图"，会以为**那就是这个窗口的界面**，
    # 据此做出完全错误的判断，而且无从察觉。
    #
    # 所以这里不靠像素判据，靠**窗口状态**（`IsIconic`）：它是系统给出的、
    # 与渲染无关的事实。判掉之后返回的原因是可行动的（"先还原它"）。
    if state.get("minimized"):
        info["reason"] = black_reason(state)
        return None, info

    r = state.get("rect")
    if not r:
        info["reason"] = "取不到窗口矩形"
        return None, info
    w, h = int(r[2] - r[0]), int(r[3] - r[1])
    if w <= 0 or h <= 0:
        info["reason"] = f"窗口矩形非法 {w}x{h}"
        return None, info

    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    # 用窗口 DC 建兼容位图：这样位图的**尺寸与色深**天然与目标窗口一致，
    # 省掉一套"猜像素格式"的逻辑（与 DEMO 探针里验证过的写法相同）。
    hdc_win = u.GetWindowDC(ctypes.c_void_p(hwnd))
    memdc = g.CreateCompatibleDC(ctypes.c_void_p(hdc_win))
    bmp = g.CreateCompatibleBitmap(ctypes.c_void_p(hdc_win), w, h)
    g.SelectObject(ctypes.c_void_p(memdc), ctypes.c_void_p(bmp))

    try:
        t0 = time.perf_counter()
        info["api_ok"] = bool(u.PrintWindow(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(memdc), PW_RENDERFULLCONTENT))
        info["ms"] = round((time.perf_counter() - t0) * 1000, 1)

        from PIL import Image
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h     # 负 = 自上而下，省一次翻转
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(ctypes.c_void_p(memdc), ctypes.c_void_p(bmp), 0, h, buf,
                    ctypes.byref(bmi), 0)
        # "BGRX"：32 位 DIB 是 BGRA 顺序，而第四字节（alpha）在 PrintWindow 的结果里
        # 不可信（常常是 0），故显式丢弃它 —— 当成 RGBA 会得到一张全透明图。
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1).copy()
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"取像素失败: {exc}"
        return None, info
    finally:
        g.DeleteObject(ctypes.c_void_p(bmp))
        g.DeleteDC(ctypes.c_void_p(memdc))
        u.ReleaseDC(ctypes.c_void_p(hwnd), ctypes.c_void_p(hdc_win))

    # ── 黑图哨兵（判据见模块 docstring）──
    small = img.convert("L").resize((160, 100))
    px = list(small.getdata())
    dark = sum(1 for v in px if v <= _NEAR_BLACK)
    nonblack = 1.0 - dark / max(1, len(px))
    info["nonblack"] = round(nonblack, 4)

    if nonblack <= (1.0 - _BLACK_FRACTION_FAIL):
        info["reason"] = (
            black_reason(state)
            or "整张图是全黑的（一个非零像素都没有）—— 这个窗口没有渲染出内容。"
               "常见于 DirectX/GPU 直绘的界面，可改用 region 裁屏并接受「可能被遮挡」。"
        )
        return None, info

    info["ok"] = True
    return img, info


def encode_window(hwnd: int, max_side: int | None = None,
                  fmt: str = "jpeg", quality: int = 85) -> tuple[bytes, dict[str, Any]]:
    """
    按窗口渲染并编码，供 `Backend.screenshot_window` 直接返回。

    ⚠️ `origin` 必须是**窗口左上角的屏幕坐标**，与 `screen.grab_desktop` 的原点语义
    逐字一致 —— 这样「屏幕坐标 = origin + 图上坐标 × scale」这条换算规则在两个通道
    （按窗口 / 裁屏）上**通用**，模型不必分辨自己在看哪一种。
    scale 恒为 1（本进程是 PMv2，UIA 矩形与物理像素同空间），但仍按
    `screen.encode_image` 的同一套组装，免得两条路的 meta 形状漂移。
    """
    from ..screen import encode_image
    from ...utils.errors import ComputerUseError

    img, cinfo = print_window(hwnd)
    state = cinfo.get("state") or {}
    rect = state.get("rect")
    origin = (int(rect[0]), int(rect[1])) if rect else (0, 0)

    if img is None:
        # 抓不到就**如实报错**，绝不返回黑图 —— 让模型以为"界面就是这样"是本项目
        # 最不能容忍的失败形态（见 base.Backend.screenshot 的同类说明）。
        raise ComputerUseError(
            f"按窗口截图失败（hwnd=0x{hwnd:x}）：{cinfo.get('reason') or '未渲染出内容'}"
            f"｜窗口状态={state}｜PrintWindow api_ok={cinfo.get('api_ok')}"
            f" 耗时={cinfo.get('ms')}ms"
        )

    data, meta = encode_image(img, origin, region=None, max_side=max_side,
                              fmt=fmt, quality=quality)
    # ★ `capture` 字段让模型分辨自己拿到的是哪一种图 —— 「按窗口渲染」能看见被遮挡
    #   的内容，「裁屏」看到的可能是遮挡者。两者对同一处像素的解释完全不同。
    meta["capture"] = "print_window"
    meta["window_id"] = f"0x{int(hwnd):x}"
    meta["window_title"] = win32.window_text(hwnd)
    meta["nonblack"] = cinfo.get("nonblack")
    meta["print_ms"] = cinfo.get("ms")
    meta["window_state"] = state
    return data, meta
