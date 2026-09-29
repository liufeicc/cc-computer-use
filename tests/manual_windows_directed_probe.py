# -*- coding: utf-8 -*-
"""定向注入的真机探针（验收脚手架，**不进产品**；跑在 Windows 客体上）。

═══ 这个探针存在的唯一理由 ═══

用户对 Windows 后端的实测反馈是：**操作时会跟用户抢鼠标和焦点**。根因是三处
统一走了 `SendInput` 全局通道（见 `backend/windows/backend.py` 的 `click_at` /
`type_text` / `press_key`），而 `SendInput` 在 Windows 上**没有定向能力** ——
事件进的是全局输入队列，谁在前台谁收到。

Windows 能做到「不抢」的路只有两条，而**它们的可行性只测过一半**：

  ① **UIA 元素级**（`ControlFromPoint` → `InvokePattern`/`SelectionItemPattern`）
     —— DEMO 已实测「目标在后台时依然生效、前台不变」，但那只覆盖了经典 Win32 与
     UWP 的**一两个控件**，没有按窗口类分档量过。
  ② **定向投递**（`PostMessage` 到**子 HWND**）
     —— **键盘**测过（`WM_CHAR` 命中，经典 Win32 与 UWP 两代都验过）；
     **鼠标从没测过**。方案文档明确把它标为「必须由探针或实机定，**不要预设**」。
     值得存疑的理由：不少应用处理鼠标消息时会读**真实光标位置**做 hit-test，
     合成的消息会被丢掉。

本探针把②的鼠标半边补上，并按**窗口类**（不是应用名 —— 产品里最终按键类缓存，
应用版本一变名字就失效）分档，顺带量三件产品要用的事：`NativeWindowHandle` 的可用率、
滚轮/拖拽能不能定向、`PrintWindow` 的黑图率与最坏时延。

═══ 判据纪律 ═══

**一律取外部可观测事实，不看返回值**（返回值只说"调用发出去了"）：

  · 「命中」= 目标应用的内容/状态**真的变了**（读回文本、对话框消失、显示数字变化）；
  · 「不打扰」= 全程 `GetCursorPos` 不变 **且** `GetForegroundWindow` 不变。

⚠️ **「不打扰」这条判据必须先证明它是有效的**：如果本探针压根没有改前台的能力
（`guestcontrol` 起的进程可能被前台锁挡住），那"前台没变"就是恒真的废话。
所以第 1 节先做**对照实验**：确认我能把前台改掉。测不出这个能力，后面所有
「前台没变」的 ✅ 都要降级成「未验证」。这是本仓库踩过的同一类坑（DEMO 里
「探针其实跑在交互桌面上」那次自欺）。

跑法（客体上）：
    python tests\\manual_windows_directed_probe.py --out results\\directed_probe.json

⚠️ 运行期间**请不要动鼠标键盘** —— 「指针没动/前台没变」是本探针的核心判据。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import pathlib
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:  # noqa: BLE001 —— 客体是中文 Windows，不 reconfigure 会在打印中文时炸
    pass

_HERE = pathlib.Path(__file__).resolve()
for _p in _HERE.parents:
    if (_p / "src" / "computer_use_mcp").is_dir():
        sys.path.insert(0, str(_p / "src"))
        break

from computer_use_mcp.backend.windows.driver import _uia, apps, win32  # noqa: E402

# 平台闸门：本探针直接用 user32/gdi32，在别的平台上跑只会得到一堆
# "module has no attribute windll" 的噪声。**响亮地拒绝**比让用户去猜好。
if sys.platform != "win32":
    print("本探针只能在 Windows 客体上运行（它直接调 user32 / gdi32）。")
    print("在 Linux 上跑它没有任何意义 —— 判据全是 Windows 专有的。")
    raise SystemExit(2)

# ══════════════════════════════════════════════════════════════════════════
# 探针自带的 Win32 封装
#
# ⚠️ **刻意不改产品代码**：这些封装在本探针里验证有效之后，才有资格搬进
# `driver/win32.py`。反过来（先改产品再验证）等于把未验证的东西塞进发货代码，
# 而这个探针的结论恰恰是「换台机器/换个应用版本就可能变」的那类。
# ══════════════════════════════════════════════════════════════════════════

_u = ctypes.windll.user32
_u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u.PostMessageW.restype = wintypes.BOOL


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
_u.ScreenToClient.restype = wintypes.BOOL

# 消息常量（只列本探针用到的）
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_MOUSEWHEEL = 0x020A
MK_LBUTTON = 0x0001


def post_message(hwnd: int, msg: int, wp: int = 0, lp: int = 0) -> bool:
    """`PostMessageW` —— 投递（不等待）。返回只代表"排队成功"，**不代表被处理**。"""
    return bool(_u.PostMessageW(wintypes.HWND(hwnd), msg, wp, lp))


def client_point_of(hwnd: int, x: int, y: int) -> tuple[int, int] | None:
    """
    屏幕坐标 → 该窗口的**客户区**坐标。

    ⚠️ 这一步不能省：鼠标消息的 `lParam` 要的是**客户区坐标**，而 UIA 矩形给的是
    **屏幕坐标**。发错坐标空间的表现是「消息投递成功但什么也没点中」——
    静默失败，正是本仓库最忌讳的那种。
    """
    p = _POINT(int(x), int(y))
    if not _u.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(p)):
        return None
    return (p.x, p.y)


def pack_xy(x: int, y: int) -> int:
    """把客户区 (x,y) 打包成 `lParam`（低 16 位 x、高 16 位 y，各自补码）。"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


# ══════════════════════════════════════════════════════════════════════════
# 判据骨架
# ══════════════════════════════════════════════════════════════════════════

_RESULTS: dict[str, dict] = {}


def ok(label: str, cond: bool, detail: str = "") -> bool:
    """记一条判据（打屏 + 进报告）。返回 cond 便于串联。"""
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'✅' if cond else '❌'} {label}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def info(label: str, detail: str) -> None:
    """记一条**观察**（不是判据）——分档数据、耗时、不可用原因都走这里。"""
    _RESULTS[label] = {"ok": None, "detail": detail}
    print(f"  ·  {label}: {detail}")


@dataclass
class Snapshot:
    """一次「打扰」快照：指针位置 + 前台窗口。判据就是两次快照相不相同。"""

    cursor: tuple[int, int] | None
    fg: int
    fg_title: str = ""

    def same_as(self, other: "Snapshot") -> tuple[bool, str]:
        bits = []
        if self.cursor != other.cursor:
            bits.append(f"指针 {self.cursor} → {other.cursor}")
        if self.fg != other.fg:
            bits.append(f"前台 {self.fg_title!r} → {other.fg_title!r}")
        return (not bits), "；".join(bits)


def snap() -> Snapshot:
    h = win32.foreground_window()
    return Snapshot(win32.get_cursor_pos(), int(h or 0),
                    win32.window_text(h) if h else "")


@dataclass
class Target:
    """一个被测目标窗口（按**窗口类**分档，不是按应用名）。"""

    key: str                       # 分档名：classic / uwp / chromium / dialog
    label: str                     # 人读
    launcher: list[str] | None     # 起不来时用什么拉起（None = 只能靠已开着的）
    match: tuple[str, ...]         # 顶层窗类名匹配
    row: dict | None = None
    notes: list[str] = field(default_factory=list)


TARGETS: list[Target] = [
    Target("classic", "经典 Win32（记事本）", ["notepad.exe"], ("Notepad",)),
    Target("dialog", "经典对话框 #32770（winver）", ["winver.exe"], ("#32770",)),
    Target("uwp", "UWP（计算器）", ["calc.exe"], ("ApplicationFrameWindow",)),
    Target("chromium", "Chromium（Edge）", None, ("Chrome_WidgetWin_1",)),
]


def find_target(t: Target, exclude: set[str]) -> dict | None:
    """
    找一个该分档的顶层窗。`exclude` 是探针启动**之前**就存在的窗口 id ——
    只认新出现的，因为探针要的是「我刚起的那个」，而已开着的同名窗会让判据失真。
    """
    for row in apps.window_rows(limit=200):
        if row["id"] in exclude:
            continue
        if row.get("class") in t.match:
            return row
    return None


def ensure_target(t: Target, exclude: set[str], launch: bool) -> dict | None:
    """先找已存在的；没有就按 `launcher` 拉起并轮询等待（**不固定 sleep**）。"""
    row = find_target(t, exclude)
    if row or not launch or not t.launcher:
        return row
    print(f"  → 拉起 {t.launcher}")
    try:
        subprocess.Popen(t.launcher)  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:启动失败", f"{type(exc).__name__}: {exc}")
        return None
    # 轮询：固定 sleep 在慢机器上会偶发失败，而失败会被误读成"不支持"
    for _ in range(40):
        time.sleep(0.5)
        row = find_target(t, exclude)
        if row:
            return row
    return None


# ══════════════════════════════════════════════════════════════════════════
# ① 对照实验：证明「不打扰」这条判据是有效的
# ══════════════════════════════════════════════════════════════════════════

def control_experiment(anchor: dict) -> bool:
    """
    先证明**探针有能力改变前台**，否则后面所有「前台没变」都是恒真的废话。

    做法：把桌面设为前台，再试着把 `anchor` 窗口提上来；能提上来即说明这个进程
    有前台权力。全程记录，最后复位。
    """
    print("\n=== ① 对照实验：本进程能不能改前台？（判据有效性的前提）===")
    desk = win32.root_ancestor(win32.window_from_point(5, 5))
    before = snap()
    win32.set_foreground_window(desk)
    time.sleep(0.6)
    mid = snap()
    can_lose = mid.fg != before.fg
    info("能否把前台让给桌面", f"{before.fg_title!r} → {mid.fg_title!r}")

    hwnd = int(anchor["id"], 16)
    win32.set_foreground_window(hwnd)
    time.sleep(0.6)
    after = snap()
    can_take = after.fg == hwnd
    info("能否把目标窗口提上来", f"→ {after.fg_title!r}")

    # 复位（并把复位算进"探针自己的行为"，不污染后续判据）
    win32.set_foreground_window(desk)
    time.sleep(0.4)
    ok("对照实验:本进程有前台权力", can_lose or can_take,
       "否则「前台没变」是恒真判据，本探针的「不打扰」结论全部失效")
    return can_lose or can_take


# ══════════════════════════════════════════════════════════════════════════
# ② UIA 路线（首选）：ControlFromPoint → Pattern
# ══════════════════════════════════════════════════════════════════════════

def uia_route(t: Target, row: dict) -> None:
    """
    走 UIA：`ControlFromPoint(x,y)` 拿元素 → 看它有哪些 Pattern → 调 `Invoke`。

    ⚠️ 为什么这条是**首选**：它是本项目在 Linux 侧确立的第一原则（元素级零坐标）。
    DEMO 已实测「目标在后台时元素级动作依然生效、前台不变」。比消息路线可靠——
    消息路线还要赌「子 HWND 存在 + 应用自己处理消息 + lParam 是客户区坐标」。
    """
    from computer_use_mcp.backend.windows.driver import nodes  # noqa: PLC0415

    print(f"\n=== ② UIA 路线 · {t.label} ===")
    hwnd = int(row["id"], 16)
    cx, cy = row["x"] + row["w"] // 2, row["y"] + row["h"] // 2
    before = snap()

    try:
        uia = _uia.import_uia()
        elem = uia.ControlFromPoint(cx, cy)
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:ControlFromPoint", f"失败 {type(exc).__name__}: {exc}")
        return
    if elem is None:
        info(f"{t.key}:ControlFromPoint", "返回 None（该点下没有 UIA 元素）")
        return

    # 关键数据：这个控件的 NativeWindowHandle 有没有值（决定"ref → 定向键盘"能不能成立）
    sub_hwnd = 0
    try:
        sub_hwnd = int(getattr(elem, "NativeWindowHandle", 0) or 0)
    except Exception:  # noqa: BLE001
        pass
    acts = []
    try:
        acts = nodes.actions_of(elem)
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:actions_of", f"失败 {type(exc).__name__}: {exc}")
    info(f"{t.key}:元素", f"类型={getattr(elem, 'ControlTypeName', '?')} "
                          f"名字={getattr(elem, 'Name', '')!r} "
                          f"子HWND={hex(sub_hwnd) if sub_hwnd else '0(无)'} "
                          f"动作={acts}")

    uia_ok = bool(acts)
    ok(f"{t.key}:UIA 能给出可用动作（不抢焦点操作的前提）", uia_ok,
       f"actions={acts}" if uia_ok else "该控件没有可直接调用的 Pattern（自绘/只读）")

    # 「不打扰」判据：ControlFromPoint 是命中测试，不该动指针也不该动前台
    after = snap()
    same, why = before.same_as(after)
    ok(f"{t.key}:UIA 命中测试不打扰用户", same, why or "指针与前台均未变")


# ══════════════════════════════════════════════════════════════════════════
# ③ 消息路线 · 键盘（对照组：这条路已在 DEMO 里验过）
# ══════════════════════════════════════════════════════════════════════════

def _drill(x: int, y: int) -> dict:
    """取点下最深子 HWND（定向投递的唯一前提）。"""
    return win32.deepest_child_at(x, y)


def keyboard_route(row: dict, token: str, read_back) -> None:
    """
    在记事本里投 `WM_CHAR` 到**子 HWND**，然后**读回文档内容**判命中。

    ⚠️ 判据必须是读回，不能看 `PostMessage` 的返回值：那个返回值只说"排进队列了"。
    读的通道（UIA `ValuePattern`）与写的通道（消息投递）完全独立，这才算数。
    """
    print("\n=== ③ 消息路线 · 键盘（对照组）===")
    hwnd = int(row["id"], 16)
    drill = _drill(row["x"] + row["w"] // 2, row["y"] + row["h"] // 2)
    deepest = (drill.get("deepest") or {}).get("hwnd")
    info("子 HWND 下钻", f"深度={drill.get('depth')} 最深={hex(deepest) if deepest else None} "
                         f"类名={(drill.get('deepest') or {}).get('class')} "
                         f"是子窗={drill.get('is_child')}")
    if not deepest:
        ok("键盘:能取到子 HWND（定向投递的前提）", False, "取不到 → 这条路不成立")
        return

    before = snap()
    for ch in token:
        post_message(deepest, WM_CHAR, ord(ch), 0)
        time.sleep(0.05)
    time.sleep(1.0)
    after = snap()
    same, why = before.same_as(after)
    ok("键盘:投递不打扰用户", same, why or "指针与前台均未变")

    text = read_back()
    ok("键盘:内容真的进了目标应用", token in text,
       f"读回={text[:60]!r}" if token not in text else f"读回含 {token!r}")


# ══════════════════════════════════════════════════════════════════════════
# ④ 消息路线 · 鼠标（**本探针的核心未知量**）
# ══════════════════════════════════════════════════════════════════════════

def mouse_route_on_dialog(row: dict) -> None:
    """
    在 `#32770` 对话框上点「确定」——**判据是对话框真的消失**。

    为什么挑对话框：它的按钮是经典 Win32 控件，且"点中"有**极强且不可伪装**的
    外部证据（整扇窗没了）。其他应用上的点击判据都软得多（要说清"菜单展开了"
    得再加一层观测）。
    """
    print("\n=== ④ 消息路线 · 鼠标（对话框「确定」按钮）===")
    hwnd = int(row["id"], 16)
    target = _find_button(hwnd)
    if not target:
        info("鼠标:对话框按钮", "没找到可点的按钮，跳过（**如实记为未覆盖**）")
        return
    btn_hwnd, (bx, by), caption = target
    info("鼠标:目标按钮", f"文字={caption!r} 子HWND={hex(btn_hwnd)} 屏幕点=({bx},{by})")

    client = client_point_of(btn_hwnd, bx, by)
    if client is None:
        info("鼠标:客户区换算", "ScreenToClient 失败 —— 坐标空间无法对齐，跳过")
        return
    info("鼠标:客户区坐标", f"屏幕=({bx},{by}) → 客户区={client}")

    before = snap()
    lp = pack_xy(*client)
    # 先 MOVE 再 DOWN/UP：只发 DOWN 时不少控件不做 hit-test
    post_message(btn_hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.08)
    post_message(btn_hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    post_message(btn_hwnd, WM_LBUTTONUP, 0, lp)
    time.sleep(1.2)
    after = snap()
    same, why = before.same_as(after)
    ok("鼠标:投递不打扰用户", same, why or "指针与前台均未变")

    gone = not win32.is_window(hwnd)
    ok("鼠标:对话框真的被点掉了（定向鼠标成立）", gone,
       "对话框已消失" if gone else "对话框还在 → 这条应用类上定向鼠标**不成立**")


def _find_button(dialog_hwnd: int) -> tuple[int, tuple[int, int], str] | None:
    """
    在对话框里找一个可见按钮，返回 `(子HWND, 屏幕中心点, 按钮文字)`。

    ⚠️ 用 `FindWindowExW` 枚举而非 UIA：UIA 的 `ControlFromPoint` 依赖点时命中，
    而我们要的是"随便找一个能点的按钮"。**枚举出来的是真的 Win32 子窗**，
    正好也是定向投递要的目标。
    """
    u = ctypes.windll.user32
    u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowExW.restype = wintypes.HWND
    cur = wintypes.HWND(0)
    while True:
        cur = u.FindWindowExW(wintypes.HWND(dialog_hwnd), cur, "Button", None)
        if not cur:
            return None
        h = int(cur)
        r = win32.window_rect(h)
        if r and win32.is_window_visible(h) and (r[2] - r[0]) > 10:
            return (h, ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2), win32.window_text(h) or "")


# ══════════════════════════════════════════════════════════════════════════
# ⑤ PrintWindow：黑图率与最坏时延（Phase 1 的前置情报）
# ══════════════════════════════════════════════════════════════════════════

def print_window_route(t: Target, row: dict) -> None:
    """
    对目标窗口做一次 `PrintWindow(PW_RENDERFULLCONTENT)`，量**非黑占比**与**耗时**。

    两件事都要量：
      · **黑图率** —— 抓不到时必须能识别出来（不然模型会把黑图当成"界面就是黑的"）；
      · **最坏时延** —— 已知它在 Chromium/GPU 路径上可能**挂住**，而它在我们的进程里
        同步执行、杀不掉。最坏值不可接受的话，方案里得换 `PW_CLIENTONLY` 或走退化路径。
    """
    print(f"\n=== ⑤ PrintWindow · {t.label} ===")
    hwnd = int(row["id"], 16)
    u, g = _gdi()
    w, h = row["w"], row["h"]
    if w <= 0 or h <= 0:
        info(f"{t.key}:PrintWindow", "窗口矩形非法，跳过")
        return

    t0 = time.perf_counter()
    hdc = u.GetWindowDC(wintypes.HWND(hwnd))
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mem, bmp)
    got = bool(u.PrintWindow(wintypes.HWND(hwnd), mem, 2))  # 2 = PW_RENDERFULLCONTENT
    dt = time.perf_counter() - t0
    dark = _dark_fraction(mem, bmp, w, h, g)
    g.DeleteObject(bmp)
    g.DeleteDC(mem)
    u.ReleaseDC(wintypes.HWND(hwnd), hdc)

    info(f"{t.key}:PrintWindow", f"api_ok={got} 耗时={dt:.2f}s 非黑占比={(1 - dark):.4f}")
    ok(f"{t.key}:PrintWindow 出图（非黑占比 > 0.2）", (1 - dark) > 0.2,
       f"非黑={(1 - dark):.4f}，越低越可疑（全黑 = 抓不到，不能当成「界面就是黑的」）")
    if dt > 1.0:
        info(f"{t.key}:PrintWindow 慢", f"{dt:.2f}s —— 它在我们的进程里同步执行、杀不掉，要留意")


def _gdi():  # noqa: ANN202
    """
    `user32` / `gdi32` 句柄，**并把 argtypes 设全**。

    ⚠️ 不设 argtypes 在 64 位下会**静默截断** HDC/HBITMAP 一类的句柄（ctypes 默认按
    `c_int` 传），表现是 `PrintWindow` 返回 0 或直接崩 —— 而这类错误在探针里会被
    误读成「该应用不支持按窗口渲染」，把探针自身的 bug 记成产品结论。
    """
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    u.GetWindowDC.argtypes, u.GetWindowDC.restype = [wintypes.HWND], wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    u.PrintWindow.restype = wintypes.BOOL
    g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteDC.argtypes = [wintypes.HDC]
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    g.GetDIBits.restype = ctypes.c_int
    return u, g


def _dark_fraction(mem_dc: int, bmp: int, w: int, h: int, g) -> float:  # noqa: ANN001
    """采样统计「近黑」像素占比（每 8 像素取一个，够看且快）。"""
    class _BMI(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bmi = _BMI()
    bmi.biSize = ctypes.sizeof(_BMI)
    bmi.biWidth = w
    bmi.biHeight = -h                      # 负高度 = 自顶向下，省一次翻转
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    if not g.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
        return 1.0
    raw = buf.raw
    dark = total = 0
    for i in range(0, len(raw) - 4, 32):   # 每 8 像素采一个
        b, gg, r = raw[i], raw[i + 1], raw[i + 2]
        total += 1
        if r < 16 and gg < 16 and b < 16:
            dark += 1
    return (dark / total) if total else 1.0


# ══════════════════════════════════════════════════════════════════════════
# ⑥ 滚轮 / 拖拽：有没有定向手段（决定它们在严格模式下是否必须禁用）
# ══════════════════════════════════════════════════════════════════════════

def wheel_and_drag_probe(row: dict) -> None:
    """
    滚轮与拖拽在严格模式下**很可能只能禁用** —— 这一节只是把"试过了、不行"记下来，
    免得后人再从零想一遍。判据刻意弱（这里没有强的外部证据可用），
    所以结论一律记为**观察**而不是判据。
    """
    print("\n=== ⑥ 滚轮 / 拖拽的定向可行性 ===")
    hwnd = int(row["id"], 16)
    cx, cy = row["x"] + row["w"] // 2, row["y"] + row["h"] // 2
    drill = _drill(cx, cy)
    deepest = (drill.get("deepest") or {}).get("hwnd")
    if not deepest:
        info("滚轮/拖拽", "取不到子 HWND，无从投递")
        return
    client = client_point_of(deepest, cx, cy) or (0, 0)
    before = snap()
    # WM_MOUSEWHEEL 的 lParam 是**屏幕坐标**（与其它鼠标消息不同！），wParam 高位是 delta
    post_message(deepest, WM_MOUSEWHEEL, (0xFF88 << 16) | 0, pack_xy(cx, cy))
    time.sleep(0.6)
    after = snap()
    same, why = before.same_as(after)
    info("滚轮:WM_MOUSEWHEEL 投递", f"投出去了；不打扰={same}（{why or '未变'}）")
    info("滚轮:命中判据", "**没有可靠的外部判据** —— 本探针不下结论，"
                          "要看它是否生效必须在具体应用上另设观测（未做）")
    info("拖拽:定向", "未测 —— 需要 Down→Move→Up 序列且命中判据更难；"
                      "首期按「只能走全局」处理（严格模式禁用）")
    # 拖拽的序列形式在下面按"是否可投递"记一笔，不判命中
    lp = pack_xy(*client)
    for msg, wp in ((WM_LBUTTONDOWN, MK_LBUTTON), (WM_MOUSEMOVE, MK_LBUTTON), (WM_LBUTTONUP, 0)):
        post_message(deepest, msg, wp, lp)
        time.sleep(0.08)
    time.sleep(0.4)
    info("拖拽:消息序列", "Down→Move→Up 已投递（起点=终点，仅验证**投得出去**，不判命中）")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="把完整报告写成 JSON（UTF-8）")
    ap.add_argument("--no-launch", action="store_true", help="不主动拉起应用，只测已开着的")
    args = ap.parse_args()

    print("=== 0) 就绪 ===")
    try:
        from computer_use_mcp.backend.windows.driver import session_info  # noqa: PLC0415
        sess = session_info.check()
        ok("会话可信（有真桌面）", bool(sess.get("interactive")),
           f"session={sess.get('session_id')} 窗口站={sess.get('window_station')} "
           f"桌面={sess.get('desktop')}")
        if not sess.get("interactive"):
            print("  ⛔ 不在交互式会话里，后面所有结论都不可信，直接退出")
            return 3
    except Exception as exc:  # noqa: BLE001
        info("会话自检", f"失败 {type(exc).__name__}: {exc}")

    try:
        print(f"  uiautomation 能力: {json.dumps(_uia.capability_dump(), ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001
        info("uiautomation", f"不可用 {type(exc).__name__}: {exc}")

    # 清场：探针要的是"新出现的"窗口，旧的不算
    for exe in ("notepad.exe", "winver.exe"):
        subprocess.run(["taskkill", "/F", "/IM", exe],  # noqa: S603,S607
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(1.0)
    pre_existing = {r["id"] for r in apps.window_rows(limit=200)}
    print(f"  启动前已有 {len(pre_existing)} 个顶层窗")

    print("\n=== 1) 发现目标（按窗口类分档）===")
    launch = not args.no_launch
    found: list[tuple[Target, dict]] = []
    for t in TARGETS:
        row = ensure_target(t, pre_existing, launch)
        if row:
            found.append((t, row))
            info(f"目标[{t.key}]", f"{t.label} → {row.get('title')!r} class={row.get('class')} "
                                   f"{row['w']}x{row['h']} @({row['x']},{row['y']})")
        else:
            info(f"目标[{t.key}]", f"{t.label} → **未找到**（未覆盖，不静默跳过）")

    if not found:
        print("  ⛔ 一个目标都没有，无法测试")
        return 5

    # ② 对照实验（用任意一个目标做锚点）
    anchor = found[0][1]
    control_experiment(anchor)

    # ③ 逐档测 UIA 与 PrintWindow
    for t, row in found:
        uia_route(t, row)
        print_window_route(t, row)

    # ④ 键盘（记事本）
    classic = next((r for t, r in found if t.key == "classic"), None)
    if classic:
        from computer_use_mcp.backend.base import get_backend  # noqa: PLC0415
        backend = get_backend()

        def read_notepad() -> str:
            """读回记事本正文（复用 UIA，与投递通道完全独立）。"""
            try:
                res = backend.get_tree(scope="app", app="notepad", max_depth=8, max_nodes=300)
            except Exception as exc:  # noqa: BLE001
                return f"<读树失败 {type(exc).__name__}: {exc}>"
            stack, val = list(res.items), ""
            while stack:
                n = stack.pop()
                if n.role in ("text", "entry", "document") and not val:
                    try:
                        val = backend.element_info(n.native).value or ""
                    except Exception:  # noqa: BLE001
                        pass
                stack.extend(n.children)
            return val

        keyboard_route(classic, "DIRECTED", read_notepad)
        wheel_and_drag_probe(classic)
    else:
        info("键盘/滚轮/拖拽", "没有经典 Win32 目标，**未覆盖**")

    # ⑤ 鼠标（对话框）
    dialog = next((r for t, r in found if t.key == "dialog"), None)
    if dialog:
        mouse_route_on_dialog(dialog)
    else:
        info("鼠标:定向", "没有 #32770 对话框目标，**未覆盖**（这是本探针的核心未知量）")

    print("\n=== ⑦ 收尾 ===")
    for exe in ("notepad.exe", "winver.exe"):
        subprocess.run(["taskkill", "/F", "/IM", exe],  # noqa: S603,S607
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    failed = [k for k, v in _RESULTS.items() if v["ok"] is False]
    graded = [k for k, v in _RESULTS.items() if v["ok"] is not None]
    print(f"\n[probe] 判据 {len(graded)} 条（另有 {len(_RESULTS) - len(graded)} 条非判据的观察），"
          f"失败 {len(failed)} 条")
    for k in failed:
        print(f"  ❌ {k} — {_RESULTS[k]['detail']}")

    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"results": _RESULTS, "failed": failed},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  报告已写入 {out}")

    # 退出码：0 全过 / 5 有失败 / 3 会话不可信（与既有探针同一套约定）
    return 5 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
