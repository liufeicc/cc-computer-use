"""
DPI / 坐标空间专项探针（backend.windows.driver.dpi_probe）。

═══ 它回答的唯一问题 ═══

**「UIA 给的坐标」与「我们注入的坐标」是不是**同一个空间**？**

这是整个 Windows 支持里**头号风险**，也是驱动层尖刀跑完第一轮后**唯一既没验证、
也没排除**的那条（`docs/PR/windows-driver/README.md` 第八节）。它之所以危险，是因为
它的失败**完全静默**：进程若不是 DPI 感知的，一切照常"成功"，只是每一个坐标都被
Windows 悄悄按缩放比除过了 —— 于是**在 100% 缩放的开发机上一切正常，
在 150% 的 4K 笔记本上点偏几十像素**，且没有任何报错指向这里。

═══ ★ 为什么判据不能是"绕过一圈回来对得上" ═══

这是本模块最要紧的一条设计约束，不写清楚就会被后来者改成一个坏尺子。

非 DPI 感知的进程会被 Windows **虚拟化**。而下面这些**全都经过同一层虚拟化**：

    UIA 的 BoundingRectangle · GetSystemMetrics · GetCursorPos
    SetCursorPos · SendInput

于是「拿 UIA 的矩形中心去挪指针、再读回来问那个点下面是哪个窗口」这一串
**天然自洽**，哪怕实际全偏了 1.5 倍 —— 因为每一环都用了同一个被缩放过的坐标系。
这个判据**恒真**，测不出任何东西，却永远绿。本项目在别处吃够过这类亏
（拆 `Coordinator` 那次三条反射守卫里两条静默变成空集合），故本模块的判据
**一律锚在绝对参照物上**：

  ① `EnumDisplaySettingsW` 读的是**显示驱动**里的模式表，**从不虚拟化**；
     而 `GetSystemMetrics(SM_CXSCREEN)` 对非感知进程**会**被虚拟化。
     两者一比即知（`_virtualization_verdict`）。
  ② **DPI 感知上下文本身**（`AreDpiAwarenessContextsEqual`）。它比 shcore 的
     `GetProcessDpiAwareness` 强，因为后者只能报出 "PER_MONITOR"(2)，
     **分不出 V1 与 V2** —— 而我们要的恰恰是 V2。

回环（挪指针 → 读回 → 问窗口）**仍然保留，但它的定位是"抓实现 bug"而不是
"判定坐标空间"**：它能抓到归一化取整错、虚拟桌面偏移漏 flag 这类问题，
**判不了**空间对不对。报告里这两类结论分开列，别混读。

═══ 退出码（调用方据此判断，不靠读 JSON 猜）═══

    0  坐标空间一致，可以放心按 UIA 矩形注入
    3  会话不可信（Session 0 / 非 WinSta0）—— 别读数据，先解决会话问题
    4  DPI 感知没设上 —— 坐标数据不可信
    5  跑通了但有自检项没过（UIA 自相矛盾 / 注入有偏移 / 不是 per-monitor aware）
    6  ★ **坐标被虚拟化** —— UIA 矩形与注入坐标不在同一空间，必须加变换层
    2  驱动不可用（缺 uiautomation / 非 Windows）
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import _uia, session_info, win32

# 每个窗口取几个采样点。取得少是刻意的：窗口内的再多点也是同一套坐标换算，
# 点多了只是把同一份证据重复贴几遍，白涨报告体积。
_SAMPLES_PER_WINDOW = 2
# 最多看几个窗口。同理——坐标空间是**进程级**属性，换个窗口不会变；
# 多测几个只是为了覆盖"不同 DPI 感知档位的目标应用"这类差异。
_MAX_WINDOWS = 3


# ── 判据 ①：虚拟化（绝对参照物法）────────────────────────────────────────

def _virtualization_verdict(metrics: dict, mode: dict) -> dict:
    """
    用**唯一不随调用方感知度变化**的来源，判定坐标有没有被虚拟化。

    实现逻辑：
      · `dmPelsWidth/Height`（来自 `EnumDisplaySettingsW`，读显示驱动的模式表）
        永远是**物理像素**；
      · `SM_CXSCREEN/SM_CYSCREEN`（`GetSystemMetrics`）在非 DPI 感知进程里
        会被按缩放比**除小**。
      · 两者相等 ⇒ 没被虚拟化；差一个整数比 ⇒ 被虚拟化了，比值就是缩放比。

    ⚠️ `implied_scaling_percent` 只在 `virtualized` 为真时有意义。
    相等时算出的是 100%，那是"没有缩放"的正确表述，不是缺省值。
    """
    pels_w = mode.get("dmPelsWidth")
    pels_h = mode.get("dmPelsHeight")
    sm_w = metrics.get("SM_CXSCREEN")
    sm_h = metrics.get("SM_CYSCREEN")

    if not all(isinstance(v, int) for v in (pels_w, pels_h, sm_w, sm_h)):
        return {
            "ok": False,
            "reason": "拿不到可比对的度量（物理模式或系统度量缺失）",
            "enum_display_settings": mode,
            "system_metrics": metrics,
        }

    virtualized = (pels_w != sm_w) or (pels_h != sm_h)
    ratio = round(pels_w / sm_w, 4) if sm_w and sm_h and pels_w and pels_h else None
    return {
        "ok": True,
        "physical_pels": [pels_w, pels_h],
        "system_metrics_says": [sm_w, sm_h],
        "virtualized": virtualized,
        "implied_scaling_percent": round(ratio * 100) if ratio else None,
        "note": (
            "★ 坐标被虚拟化：一切来自 UIA 的坐标都要按这个比例变换后才能注入"
            if virtualized else
            "物理像素与系统度量一致 ⇒ 进程没有被 DPI 虚拟化"
        ),
    }


# ── 判据 ②：UIA 自洽（点 → 元素 → 矩形，矩形必须含住那个点）──────────────

def _rect_of(element) -> tuple[int, int, int, int] | None:  # noqa: ANN001
    """
    从 uiautomation 元素取 `BoundingRectangle`，统一成 `(l, t, r, b)` 整数元组。

    ⚠️ 两种形状都认：库把 `BoundingRectangle` 定义成一个具名元组式的 `Rect`
    （有 `.left/.top/.right/.bottom`），但也可能在某些版本上退化成普通元组。
    写死一种的话，换版本时会得到 `AttributeError` —— 那还算好的；
    更坏的是取到空值然后被判成"这个元素没有矩形"，把一个库问题记成目标应用的问题。
    """
    try:
        rect = element.BoundingRectangle
    except Exception:  # noqa: BLE001
        return None
    if rect is None:
        return None
    try:
        if hasattr(rect, "left"):
            return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        seq = tuple(rect)
        if len(seq) >= 4:
            return (int(seq[0]), int(seq[1]), int(seq[2]), int(seq[3]))
    except Exception:  # noqa: BLE001
        return None
    return None


def _contains(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    left, top, right, bottom = rect
    return left <= x < right and top <= y < bottom


def _sample_points(rect: tuple[int, int, int, int], count: int) -> list[tuple[int, int]]:
    """
    在窗口矩形里取采样点。**刻意避开边缘**：边框/阴影区常落在没有子窗的位置，
    拿那里做命中测试，量到的是"边框没有子窗"这个显然的事实，与坐标空间无关。
    取 1/4、1/2 两个横向位置、纵向取 40% 处（避开标题栏与底部状态栏）。
    """
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w <= 8 or h <= 8:
        return []
    y = top + int(h * 0.40)
    fractions = (0.25, 0.50)[:max(1, count)]
    return [(left + int(w * f), y) for f in fractions]


def _probe_point(uia, x: int, y: int, move_cursor: bool) -> dict:
    """
    对一个屏幕坐标走一遍完整链路，把每一步的结果都留下来。

    ═══ 为什么要用「元素自己矩形的中心」再走一遍 ═══

    光验「UIA 矩形含住查询点」还不够 —— 那只说明 UIA 自己前后一致。
    真正要验的是**从 UIA 矩形算出来的那个点，注入发射出去之后是不是还落在这个元素上**，
    因为生产路径就是这样：读 UIA 矩形 → 算中心 → 注入点击。

    故流程是：
      ① 拿查询点问 UIA → 得到元素与其矩形（矩形必须含住查询点）
      ② 取该矩形的**中心**作为"真正要点的那个点"
      ③ 问 Win32：在查询点与在中心点上，最深处是不是**同一个控件**（两条独立通道互证）
      ④ 挪指针到中心点 → 读回 → 再问一次"指针下面是谁"

    ③④ 都**判不了坐标空间**（见模块 docstring：整条链共用同一层虚拟化，自洽是必然的），
    它们抓的是实现 bug；判坐标空间的是 ①② 与报告里的 `virtualization`/`awareness`。

    ═══ ⚠️ 遮挡 ═══

    查询点可能被**别的窗口盖住**（z 序更高）。那不是坐标问题，但会让"下面是谁"的比对
    得出一个看似失败的答案。所以本函数把遮挡如实记下来（`occluded`），
    **交给调用方决定要不要采信** —— 绝不在这里悄悄跳过，否则报告会显得一切正常，
    而实际上有一半采样点根本没测到东西。
    """
    out: dict = {"point": [x, y]}

    # ---- ① UIA 自洽 + ② 取元素矩形中心 ----
    rect = None
    try:
        el = uia.ControlFromPoint(x, y)
        rect = _rect_of(el) if el is not None else None
        out["uia"] = {
            "name": (getattr(el, "Name", None) or None) if el is not None else None,
            "control_type": (str(getattr(el, "ControlTypeName", "") or "") or None)
            if el is not None else None,
            "native_hwnd": (int(getattr(el, "NativeWindowHandle", 0) or 0))
            if el is not None else None,
            "rect": list(rect) if rect else None,
            # ★ 判据 A：UIA 自己得前后一致
            "point_inside_rect": _contains(rect, x, y) if rect else None,
        }
    except Exception as exc:  # noqa: BLE001
        out["uia"] = {"error": f"{type(exc).__name__}: {exc}"}

    if not rect:
        out["note"] = "该点下 UIA 没给出矩形，后面的比对无法进行"
        return out

    center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    out["element_center"] = list(center)

    # ---- ③ 两条独立通道：查询点 vs 矩形中心，最深处是不是同一个控件 ----
    try:
        h_at_query = win32.window_from_point(x, y)
        h_at_center = win32.window_from_point(center[0], center[1])
        out["win32"] = {
            "hwnd_at_query_point": f"0x{h_at_query:x}" if h_at_query else "0x0",
            "class_at_query_point": win32.class_name(h_at_query) if h_at_query else None,
            "hwnd_at_element_center": f"0x{h_at_center:x}" if h_at_center else "0x0",
            "class_at_element_center": win32.class_name(h_at_center) if h_at_center else None,
            # ★ 判据 B：从 UIA 矩形算出的中心，Win32 看过去也该是同一个控件
            "same_hwnd": bool(h_at_query and h_at_query == h_at_center),
            "same_root": bool(
                h_at_query and h_at_center
                and win32.root_ancestor(h_at_query) == win32.root_ancestor(h_at_center)),
        }
        # 遮挡：中心点上最深的那个控件，与查询点上的**根窗**是否同一个顶层窗口。
        # 不同 ⇒ 中心点被别的顶层窗口盖住了 ⇒ 判据 B 这条不能采信。
        out["occluded"] = not out["win32"]["same_root"]
    except Exception as exc:  # noqa: BLE001
        out["win32"] = {"error": f"{type(exc).__name__}: {exc}"}
        out["occluded"] = None

    # ---- ④ 注入回环（会改全局指针；放最后）----
    if move_cursor:
        out["injection"] = _injection_roundtrip(center[0], center[1])
    return out


def _injection_roundtrip(x: int, y: int) -> dict:
    """
    真的走一遍注入通道：挪指针 → 读回 → 问那个点下面是谁。

    ⚠️ 这一步**判不了坐标空间对不对**（见模块 docstring：整条链共用同一层虚拟化，
    自洽是必然的）。它判的是**实现 bug**：
      · 归一化取整写错（分母该是 `边长-1` 而写了 `边长`，会在屏幕最右/最下差 1px）；
      · 多显示器时漏了 `MOUSEEVENTF_VIRTUALDESK`（坐标会被当成相对主屏）；
      · `INPUT` 结构体布局写错 —— `cbSize` 不对时 `SendInput` **返回 0 且不报错**。

    判据取「挪之前那个点上是谁」与「挪之后指针下面是**谁**」是否一致，而不是"挪之前
    挪之后是同一个工作空间" —— 后者恒真（自己跟自己比），是坏尺子。
    """
    out: dict = {"target": [x, y], "hwnd_before": None}

    try:
        before = win32.window_from_point(x, y)
        out["hwnd_before"] = f"0x{before:x}" if before else "0x0"
    except Exception as exc:  # noqa: BLE001
        out["hwnd_before_error"] = f"{type(exc).__name__}: {exc}"

    try:
        out["set_cursor_pos_ok"] = win32.set_cursor_pos(x, y)
        got = win32.get_cursor_pos()
        out["cursor_after_set"] = list(got) if got else None
        out["set_delta"] = [got[0] - x, got[1] - y] if got else None
        if got:
            hwnd = win32.window_from_point(got[0], got[1])
            out["hwnd_at_cursor"] = f"0x{hwnd:x}" if hwnd else "0x0"
            out["cursor_hits_same_hwnd"] = bool(
                hwnd and out["hwnd_before"] == f"0x{hwnd:x}")
    except Exception as exc:  # noqa: BLE001
        out["set_cursor_error"] = f"{type(exc).__name__}: {exc}"

    try:
        sent = win32.send_input_move_absolute(x, y)
        out["send_input"] = sent
        if sent.get("ok"):
            got = win32.get_cursor_pos()
            out["cursor_after_sendinput"] = list(got) if got else None
            out["sendinput_delta"] = [got[0] - x, got[1] - y] if got else None
            if got:
                hwnd = win32.window_from_point(got[0], got[1])
                out["sendinput_hits_same_hwnd"] = bool(
                    hwnd and out["hwnd_before"] == f"0x{hwnd:x}")
    except Exception as exc:  # noqa: BLE001
        out["send_input"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


# ── 主流程 ───────────────────────────────────────────────────────────────

def run(sample_windows: int = _MAX_WINDOWS, move_cursor: bool = True) -> dict:
    """跑完整套测量，返回报告 dict（不写文件、不打印——便于单测直接调）。"""
    report: dict = {"schema": 1, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # ---------- 环境闸 ----------
    report["session"] = session_info.check()

    # ---------- DPI 前置（必须先于任何 UIA 对象）----------
    report["dpi_setup"] = _uia.ensure_dpi_awareness()

    # ---------- ★ 判据 ②：感知度上下文（能分 V1 / V2）----------
    report["awareness"] = win32.describe_awareness_context(
        win32.thread_dpi_awareness_context())

    # ---------- ★ 判据 ①：虚拟化（绝对参照物）----------
    report["virtualization"] = _virtualization_verdict(
        win32.system_metrics(), win32.current_display_mode())

    caps = _uia.capability_dump()
    report["uiautomation"] = caps
    if not caps.get("available"):
        report["verdict"] = "driver_unavailable"
        return report
    uia = _uia.import_uia()

    # ---------- 逐窗口回环 ----------
    original_cursor = win32.get_cursor_pos()
    report["windows"] = []
    try:
        report["windows"] = _probe_windows(uia, sample_windows, move_cursor)
    finally:
        # 把指针放回原处：这在别人的机器上跑时是基本礼貌（探针挪了全局指针），
        # 放 finally 里是为了"中途抛异常也还回去"。
        if move_cursor and original_cursor:
            try:
                win32.set_cursor_pos(*original_cursor)
            except Exception:  # noqa: BLE001
                pass
    report["cursor_restored_to"] = list(original_cursor) if original_cursor else None

    report["verdict"] = _verdict(report)
    return report


def _probe_windows(uia, limit: int, move_cursor: bool) -> list[dict]:
    """
    挑几个有代表性的顶层窗口，各取若干采样点跑一遍回环。

    挑选顺序：**前台窗口最先**（它必然没被遮挡，采样最干净），再按面积从大到小补几个
    真实的**应用**窗口。面积排序这个启发式与 Linux 侧 `window_screen_pos_by_pid`
    取最大窗同一个理由：小窗多半是工具窗/辅助窗。

    ⚠️ 但面积排序在 Windows 上会先撞上**外壳窗口**——`Progman`（桌面）、
    `Windows.UI.Core.CoreWindow`（输入体验）都是整屏大小，永远排在最前，
    而它们恰恰不是应用。故 `_SHELL_CLASSES` 把它们排除掉；不排的话，
    报告里会通篇在测桌面窗口，看起来"测了好几个窗口"，实则一个应用都没碰到。
    """
    seen: list[int] = []
    fg = win32.foreground_window()
    if fg and win32.is_usable_window(fg):
        seen.append(fg)

    candidates = [
        h for h in win32.enum_top_level_windows()
        if win32.is_usable_window(h)
        and win32.class_name(h) not in _SHELL_CLASSES
        and h not in seen
    ]
    candidates.sort(key=_area, reverse=True)
    seen.extend(candidates)

    out: list[dict] = []
    for hwnd in seen[:max(0, limit)]:
        rect = win32.window_rect(hwnd)
        entry: dict = {
            "window": win32.describe(hwnd),
            "is_foreground": hwnd == fg,
            "dpi_for_window": win32.dpi_for_window(hwnd),
            # 目标应用**自己**的感知档位：与我们的不同时，UIA 要给的是"物理矩形"，
            # 而这正是历史上"二次缩放"发生的地方，故单独记一笔。
            "window_dpi_awareness": win32.describe_awareness_context(
                win32.window_dpi_awareness_context(hwnd)),
            "samples": [],
        }
        if rect:
            for (x, y) in _sample_points(rect, _SAMPLES_PER_WINDOW):
                entry["samples"].append(_probe_point(uia, x, y, move_cursor))
        out.append(entry)
    return out


# 整屏大小的外壳窗口：它们永远排面积第一，但**不是应用**。
_SHELL_CLASSES = frozenset({
    "Progman",                      # 桌面
    "WorkerW",                      # 桌面（壁纸层）
    "Shell_TrayWnd",                # 任务栏
    "Windows.UI.Core.CoreWindow",   # 输入体验 / 系统 UI
    "Windows.UI.Composition.DesktopWindowContentBridge",
})


def _area(hwnd: int) -> int:
    rect = win32.window_rect(hwnd)
    if not rect:
        return 0
    left, top, right, bottom = rect
    return max(0, right - left) * max(0, bottom - top)


def _verdict(report: dict) -> str:
    """
    综合判断，给调用方一个**不需要读完整 JSON**的结论字符串。

    判据顺序是刻意的：**环境 → 前置 → 空间 → 实现**。
    「会话不可信」必须排在一切前面 —— 在服务会话里，什么都是空的/错的，
    把它报成"坐标不一致"会指向完全错误的修复方向（真机 DEMO 为此多花了三轮）。
    """
    if not report.get("session", {}).get("interactive"):
        return "session_untrusted"
    if not report.get("dpi_setup", {}).get("ok"):
        return "dpi_not_set"

    matched = report.get("awareness", {}).get("matched") or []
    if not any(m in ("per_monitor_aware_v1", "per_monitor_aware_v2") for m in matched):
        # 系统感知或不感知都会导致虚拟化 —— 这条与下面的 virtualized 是同一件事的
        # 两个侧面（一个是"为什么"，一个是"实际有没有发生"），两条都留着互为印证。
        return "not_per_monitor_aware"

    virt = report.get("virtualization", {})
    if virt.get("virtualized"):
        return "coordinates_virtualized"

    for win in report.get("windows", []):
        for sample in win.get("samples", []):
            uia_part = sample.get("uia", {})
            if uia_part.get("point_inside_rect") is False:
                # UIA 自己前后矛盾（拿一个点去问，回来的矩形不含这个点）——
                # 只可能是坐标空间被换过，绝不可能是"这个元素本来就偏"。
                return "uia_self_inconsistent"

            # ⚠️ 遮挡点**不参与**判定：中心点被别的窗口盖住时，"下面是谁"的比对
            # 本来就该是另一个窗口，那不是坐标问题。跳过是**如实**的，不是放水——
            # 每个采样点的 `occluded` 都记在报告里，覆盖率一眼可见。
            if sample.get("occluded"):
                continue

            win_part = sample.get("win32", {})
            if win_part.get("same_root") is False:
                # 查询点与"由 UIA 矩形算出的中心"落在**不同的顶层窗口**上 ⇒
                # 矩形与坐标查询不在同一空间。这是最直接的坏消息。
                #
                # ⚠️ 判据刻意用 `same_root` 而**不是** `same_hwnd`：
                # 一个 UIA 元素可以**横跨多个 HWND**。实测（150% 下的 winver 对话框）：
                # 查询点落在对话框架本身（`#32770`），而矩形中心落在对话框里的静态文本
                # 子窗（`Static`）—— 两者 HWND 不同但同属一个顶层窗口，**完全正常**。
                # 第一版拿 `same_hwnd` 当判据，于是把一个正确的界面判成了失败。
                # 判"坐标空间对不对"要的是**顶层窗口**这个粒度，不是子控件。
                return "uia_rect_mismatch"

            inj = sample.get("injection", {})
            for key in ("set_delta", "sendinput_delta"):
                delta = inj.get(key)
                if delta and any(abs(v) > 1 for v in delta):
                    # 允许 1px：归一化到 0~65535 必然有取整，多显示器边界上尤其如此。
                    # 超过 1px 就不是取整了，是那一层映射写错了。
                    return "injection_offset"
            for key in ("cursor_hits_same_hwnd", "sendinput_hits_same_hwnd"):
                if inj.get(key) is False:
                    return "injection_hit_mismatch"
    return "ok"


_EXIT_CODES = {
    "ok": 0,
    "session_untrusted": 3,
    "dpi_not_set": 4,
    "coordinates_virtualized": 6,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windows DPI / 坐标空间专项探针")
    ap.add_argument("--out", required=True, help="结果 JSON 的落盘路径")
    ap.add_argument("--windows", type=int, default=_MAX_WINDOWS,
                    help=f"最多测几个顶层窗口（默认 {_MAX_WINDOWS}）")
    ap.add_argument("--no-move-cursor", action="store_true",
                    help="不挪指针（只做只读部分；注入回环整段跳过）")
    args = ap.parse_args(argv)

    try:
        report = run(sample_windows=args.windows, move_cursor=not args.no_move_cursor)
    except _uia.WindowsDriverUnavailable as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, default=str)
    except OSError as exc:
        # 写不出去就把结果打到 stdout —— 至少别把这次测量的信息全丢掉
        print(f"⚠️ 写 {args.out} 失败（{exc}），结果改打到 stdout：", file=sys.stderr)
        json.dump(report, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 5

    verdict = report.get("verdict", "unknown")
    virt = report.get("virtualization", {})
    print(f"[dpi_probe] verdict = {verdict}")
    print(f"[dpi_probe] 物理像素 = {virt.get('physical_pels')} ｜ "
          f"系统度量 = {virt.get('system_metrics_says')} ｜ "
          f"虚拟化 = {virt.get('virtualized')} ｜ 隐含缩放 = {virt.get('implied_scaling_percent')}%")
    print(f"[dpi_probe] 感知度 = {report.get('awareness', {}).get('matched')}")
    print(f"[dpi_probe] 结果已写入 {args.out}")
    return _EXIT_CODES.get(verdict, 5)


if __name__ == "__main__":
    sys.exit(main())
