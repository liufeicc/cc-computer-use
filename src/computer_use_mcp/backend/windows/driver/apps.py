"""
应用与窗口枚举（backend.windows.driver.apps）。

═══ 这一层回答什么 ═══

「现在桌面上有哪些窗口 / 哪些应用 / 目标窗口在哪」—— 即 `Backend` 里
`list_apps` / `list_windows` / `wait_window` / `screen_layout` /
`active_window_title` / `active_window_rect` / `window_screen_pos` 的数据来源。

═══ 与 Linux 侧的关系 ═══

返回**同一套形状**，这样 `tools/` 与 `coordinator/` 不用改：

    list_windows  → [{id, title, pid, x, y, w, h, area}]  按 area 降序
    screen_layout → {monitors: [{name,x,y,w,h,primary}], virtual:(W,H), source}

═══ ⚠️ 一个必须记住的差异 ═══

Linux 侧的 `wmctrl -lpx` 只列**受 WM 管理的客户窗**，天然过滤掉框架/工具窗。
Windows **没有这个概念**：`EnumWindows` 给的是**全部顶层 HWND**，
里面有托盘窗、消息窗、输入法候选窗、以及 `Progman`/`WorkerW` 这类外壳窗。

所以本模块的 `is_usable_window` 过滤不是"锦上添花"，而是**必需的**：
少一道过滤，`list_windows` 会在真机上返回几十个模型看不懂的条目，
把真正的应用窗口淹没掉（与 Linux 侧"i3 框架窗刷屏"是同一类问题，只是那边由 WM 代劳）。
"""

from __future__ import annotations

import time

from . import win32

# 外壳窗口：整屏大小、永远排面积第一，但**不是应用**。
# 与 `dpi_probe._SHELL_CLASSES` 是同一份判断，这里独立一份是因为用途不同
# （那边是"选采样目标"，这边是"列给模型看"），将来也可能各自演化。
_SHELL_CLASSES = frozenset({
    "Progman",                      # 桌面
    "WorkerW",                      # 桌面（壁纸层）
    "Shell_TrayWnd",                # 任务栏
    "Shell_SecondaryTrayWnd",       # 副屏任务栏
    "Windows.UI.Core.CoreWindow",   # 输入体验 / 系统 UI
    "Windows.UI.Composition.DesktopWindowContentBridge",
    "SysShadow",                    # 经典阴影辅助窗
    "TaskListThumbnailWnd",
})


def window_rows(limit: int = 100) -> list[dict]:
    """
    可用窗口清单，形状与 Linux 的 `list_windows` 一致，按面积降序。

    实现逻辑：
      1. `EnumWindows` 取全部顶层窗；
      2. 逐窗 `is_usable_window` 过滤（标题非空 + 面积够 + `WS_VISIBLE` 位）；
      3. 排掉外壳窗（见 `_SHELL_CLASSES`）；
      4. 组装成 dict 并按 `area` 降序。

    ⚠️ 最小化窗口的几何**不可信**（`GetWindowRect` 会给一个历史位置的负坐标矩形）。
    这里**照实返回**它的矩形，同时用 `minimized` 字段标出来 —— 绝不静默丢弃，
    也绝不假装它有个正常坐标：调用方据此决定要不要先还原（见 `ensure_restored`）。
    """
    rows: list[dict] = []
    for hwnd in win32.enum_top_level_windows():
        if not win32.is_usable_window(hwnd):
            continue
        if win32.class_name(hwnd) in _SHELL_CLASSES:
            continue
        rect = win32.window_rect(hwnd)
        if not rect:
            continue
        left, top, right, bottom = rect
        rows.append({
            "id": f"0x{hwnd:x}",
            "title": win32.window_text(hwnd),
            "pid": win32.pid_of_window(hwnd),
            "app": win32.process_basename(win32.pid_of_window(hwnd)) or "",
            "class": win32.class_name(hwnd),
            "x": left, "y": top,
            "w": right - left, "h": bottom - top,
            "area": max(0, right - left) * max(0, bottom - top),
            "minimized": win32.is_iconic(hwnd),
        })
    rows.sort(key=lambda d: d["area"], reverse=True)
    return rows[:max(0, limit)]


def app_names() -> list[str]:
    """
    当前桌面上的应用名（去重、排序）。

    口径是**进程可执行文件名**（去掉目录、`.exe`、转小写），例如 `notepad` / `dbeaver`。
    与 `nodes` 给节点标的 `app` 用同一个来源 —— 两边不一致会让 `find(app=...)`
    "存在但搜不到"，而这是最难查的一类问题（没有报错，只是没结果）。

    刻意**只列有可用窗口的进程**：一个后台进程在桌面上看不见，
    把它列进"应用"会让模型以为可以操作它。
    """
    seen: set[str] = set()
    for row in window_rows(limit=1000):
        if row["app"]:
            seen.add(row["app"])
    return sorted(seen)


def find_window(title_contains: str | None = None, window_id: str | None = None) -> dict | None:
    """
    按标题子串或窗口 id 找一个窗口（形状同 `window_rows` 的单项）。

    `window_id` 接受 `"0x1a2b3c"` 或十进制串 —— 两种都认，因为我们的 `id` 字段是
    十六进制而用户（或模型）可能从别处抄来十进制。认不出时返回 None，不猜。
    """
    if window_id:
        target = _parse_hwnd(window_id)
        if target is None:
            return None
        for row in window_rows(limit=1000):
            if _parse_hwnd(row["id"]) == target:
                return row
        return None

    if title_contains:
        needle = title_contains.lower()
        for row in window_rows(limit=1000):
            if needle in (row["title"] or "").lower():
                return row
        return None
    return None


def _parse_hwnd(value: str) -> int | None:
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value, 10)
    except (ValueError, AttributeError):
        return None


def wait_window(title_contains: str | None = None, window_id: str | None = None,
                timeout: float = 10.0, poll: float = 0.25) -> dict | None:
    """
    轮询等窗口出现，超时返回 None。

    ⚠️ **轮询间隔有下限**（0.25s）：Windows 上 `window_rows` 会对每个窗口调
    `process_basename`（一次 `OpenProcess` + `QueryFullProcessImageNameW`），
    全量扫一遍是毫秒级但不是免费的。间隔太小会把 CPU 花在枚举上。
    与 Linux 侧 `wait_window` 同一个口径。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    interval = max(0.05, poll)
    while True:
        hit = find_window(title_contains=title_contains, window_id=window_id)
        if hit is not None:
            return hit
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


# ── 前台 / 几何 ─────────────────────────────────────────────────────────

def active_window_title() -> str | None:
    """
    前台窗口标题。取不到（无前台窗口 / 服务会话）返回 None。

    ⚠️ **空标题归一为 None**：`GetWindowTextW` 对无标题窗口返回空串，而
    "没有前台窗口"与"前台窗口没有标题"是两件事。本项目在 Linux 侧为同一个问题
    专门写过 `window_title()` 做归一（空串 → None），这里保持一致。
    """
    hwnd = win32.foreground_window()
    if not hwnd:
        return None
    text = win32.window_text(hwnd)
    return text or None


def active_window_rect() -> tuple[int, int, int, int] | None:
    """前台窗口矩形。最小化的窗口返回 None —— 那种矩形不可信（见 `window_rows`）。"""
    hwnd = win32.foreground_window()
    if not hwnd or win32.is_iconic(hwnd):
        return None
    return win32.window_rect(hwnd)


def ensure_restored(hwnd: int) -> bool:
    """
    若窗口最小化则还原它，返回"现在是否处于可信任几何的状态"。

    ⚠️ 为什么单列一个函数而不是在 `window_rows` 里顺手还原：
    **列窗口是只读操作，不该改桌面状态**。还原只在真要按坐标操作之前做
    （见 `actions`），这个边界不能糊掉 —— 否则一次 `list_windows` 就会把
    用户最小化的窗口全弹开，那是很讨厌的副作用。
    """
    if not win32.is_window(hwnd):
        return False
    if not win32.is_iconic(hwnd):
        return True
    win32.show_window(hwnd, 9)          # SW_RESTORE
    time.sleep(0.15)                     # 还原是异步的，矩形要等一拍才可信
    return not win32.is_iconic(hwnd)


def screen_layout() -> dict:
    """
    显示器布局：`{monitors: [{name,x,y,w,h,primary}], virtual:(W,H), source}`。

    `virtual` 取 `SM_*VIRTUALSCREEN`（多屏时是把所有屏包起来的外接矩形，
    **可以带非零原点与负坐标**）—— 这正是 `SendInput` 归一化要用的那个空间。

    `source` 如实标注数据来自哪条通道（同 Linux 侧 M-36 的教训：
    标错 source 会在排查多屏问题时指向错误的一层）。
    """
    monitors = win32.enum_monitors()
    metrics = win32.system_metrics()
    virtual = (
        int(metrics.get("SM_CXVIRTUALSCREEN") or 0),
        int(metrics.get("SM_CYVIRTUALSCREEN") or 0),
    )
    if not monitors:
        # 枚举不出显示器时的兜底：拿屏幕度量当唯一一块屏。
        # ⚠️ 这里**必须**把 source 标成 screen_metrics —— 数据确实来自那里，
        #    不是来自 EnumDisplayMonitors（Linux 侧就是标错 source 吃过亏）。
        w = int(metrics.get("SM_CXSCREEN") or 0)
        h = int(metrics.get("SM_CYSCREEN") or 0)
        if w and h:
            monitors = [{"name": "primary", "x": 0, "y": 0, "w": w, "h": h,
                         "primary": True}]
            virtual = virtual if virtual[0] else (w, h)
        return {"monitors": monitors, "virtual": virtual, "source": "screen_metrics"}
    return {"monitors": monitors, "virtual": virtual, "source": "enum_display_monitors"}