# -*- coding: utf-8 -*-
"""定向注入的真机探针（验收脚手架，**不进产品**；跑在 Windows 客体上）。

═══ 这个探针存在的唯一理由 ═══

用户对 Windows 后端的实测反馈是：**操作时会跟用户抢鼠标和焦点**。根因是三处
统一走了 `SendInput` 全局通道（见 `backend/windows/backend.py` 的 `click_at` /
`type_text` / `press_key`），而 `SendInput` 在 Windows 上**没有定向能力** ——
事件进的是全局输入队列，谁在前台谁收到。

Windows 能做到「不抢」的路只有两条，而**它们的可行性只测过一半**：

  ① **UIA 元素级**（`ControlFromPoint`/`ControlFromHandle` → `InvokePattern`）
     —— DEMO 已实测「目标在后台时依然生效、前台不变」，但那只覆盖了经典 Win32 与
     UWP 的**一两个控件**，没有按窗口类分档量过。
  ② **定向投递**（`PostMessage`/`SendMessage` 到**子 HWND**）
     —— **键盘**测过（`WM_CHAR` 命中，经典 Win32 与 UWP 两代都验过）；
     **鼠标从没测过**。方案文档明确把它标为「必须由探针或实机定，**不要预设**」。
     值得存疑的理由：不少应用处理鼠标消息时会读**真实光标位置**做 hit-test，
     合成的消息会被丢掉；主题化的按钮（UxTheme）对合成点击尤其挑剔。

═══ 判据纪律 ═══

**一律取外部可观测事实，不看返回值**（返回值只说"调用发出去了"）：

  · 「命中」= 目标应用的状态**真的变了**（对话框消失、读回的文本出现）；
  · 「不打扰」= 操作前后 `GetCursorPos` 不变 **且** `GetForegroundWindow` 不变。

⚠️ **「不打扰」这条判据必须先证明它是有效的**：如果本探针压根没有改前台的能力，
那"前台没变"就是恒真的废话。所以第 2 节先做**对照实验**：确认我能把前台改掉。
这是本仓库踩过的同一类坑（DEMO 里「探针其实跑在交互桌面上」那次自欺）。

═══ 为什么鼠标那一节要用**多个全新对话框** ═══

「点中了」唯一的强证据是**对话框消失**——而它一旦消失就没法再测下一种技法。
所以每种技法都**重新拉起一个 winver** 来测（`fresh_dialog`）。同理，前台/后台两个
条件是**分别**测的：把这两个变量混在一起，就分不清失败是"技法不对"还是"目标是后台"。

跑法（客体上）：
    python tests\\manual_windows_directed_probe.py --out results\\directed_probe.json

⚠️ 运行期间**请不要动鼠标键盘** —— 「指针没动/前台没变」是本探针的核心判据。

═══ 2026-09-29 真机结论（三轮迭代后）═══

| 通道 | 目标在后台时可用？ | 会不会打扰 |
|---|---|---|
| UIA 元素级 `InvokePattern.Invoke` | ✅ **可用** | ✅ 不打扰（前后台两种都测了） |
| UIA `ValuePattern.SetValue` | ✅ 写进去了（读回命中） | ⚠️ **Win11 记事本会自己激活到前台** |
| 定向键盘 `PostMessage(WM_CHAR)` → 子 HWND | ✅ 可用（DEMO 结论复现） | ✅ 不打扰 |
| **定向鼠标**（PostMessage/SendMessage→子窗、`BM_CLICK`、PostMessage→顶层） | ❌ **全失效** | ✅（因为压根没生效） |
| 定向鼠标 `BM_CLICK`（目标**前台**时） | ✅ 生效 | —（目标本身是前台，判据不适用） |

**「对话框特有」这条推测已被推翻**（2026-09-30 补测，第六节 b）：
早先只在对话框按钮上测过鼠标定向，当时猜「对话框要求自己处于活动状态才响应」。
补设了第六节 b —— 在**记事本主窗口的编辑区**（`RichEditD2DPT`）上点一刀再看光标行号：
**光标没动**（字符落在第 0 行，不在点击处），同时前台被记事本自己抢走了。
所以**主窗口上的定向鼠标同样不成立**，不是对话框的特例。

⚠️ 这条与「不打扰」那条结论是**分开**读的：鼠标消息确实没生效（不打扰是"因为什么都没发生"），
而 UIA `SetValue` / `Invoke` 是**真的生效了**并且（除记事本 SetValue 外）不打扰。
两者不能互相外推 —— 一个是"能用但不打扰"，一个是"根本不能用"。

⚠️ **每条"不打扰"的结论都依赖于对照实验先通过**（见 `control_experiment`）：
真机实测里有一轮**本进程完全没有前台权力**（设谁都设不动），那一轮所有
「前台没变」都是废话。**更隐蔽的一次是 2026-09-30 的复跑**：那一轮对照实验**通过了**，
但环境里 Edge 在反复自行抢前台，于是`鼠标[…]点中了（对话框消失）`一度**假通过** ——
重跑（先把 Edge 杀掉）后六种鼠标技法**全部如实报失败**。所以
**「点中了」这条判据在有后台噪声时也会假阳性**：噪声窗口把对话框顶掉，看起来就像被点掉了。
稳的做法是：跑之前先确认 `0b) 前台稳定性诊断` 是干净的。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import pathlib
import subprocess
import sys
import time
import traceback
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
_u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u.SendMessageW.restype = wintypes.LPARAM

WINDOW_TEXT = _u.GetWindowTextW


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
_u.ScreenToClient.restype = wintypes.BOOL

# 消息常量（只列本探针用到的）
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_MOUSEWHEEL, WM_SETFOCUS = 0x020A, 0x0007
BM_CLICK, BM_GETCHECK = 0x00F5, 0x00F0
MK_LBUTTON = 0x0001


def post_message(hwnd: int, msg: int, wp: int = 0, lp: int = 0) -> bool:
    """`PostMessageW` —— 投递（不等待）。返回只代表"排队成功"，**不代表被处理**。"""
    return bool(_u.PostMessageW(wintypes.HWND(hwnd), msg, wp, lp))


def send_message(hwnd: int, msg: int, wp: int = 0, lp: int = 0) -> int:
    """`SendMessageW` —— 同步投递（等对方处理完）。返回对方的返回值。"""
    return int(_u.SendMessageW(wintypes.HWND(hwnd), msg, wp, lp))


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


def hwnd_of(desc: dict | None) -> int:
    """
    从 `win32.describe()` 的返回值里取回**整数**句柄。

    ⚠️ 这里有个坑值得单独写一个函数：`describe()["hwnd"]` 是 **`"0x1234"` 这样的
    字符串**（诊断报告里给人看的），而探针后面所有 Win32 调用要的是 int。
    首轮真机实测就栽在这儿：`hex(deepest)` 抛 `TypeError: 'str' object cannot be
    interpreted as an integer` —— 探针当场崩，一整轮白跑。
    """
    if not desc:
        return 0
    h = desc.get("hwnd")
    if isinstance(h, int):
        return h
    try:
        return int(str(h), 16)
    except (TypeError, ValueError):
        return 0


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


def guarded(label: str, fn, *a, **kw):  # noqa: ANN001, ANN201, ANN401
    """
    跑一节测试；崩了只记一条观察，**不中断整轮**。正常时把 `fn` 的返回值原样传回。

    ⚠️ 这条不是"防御性编程"，是拿实际代价换来的：首轮真机跑里
    `keyboard_route` 抛了个 `TypeError`（`describe()["hwnd"]` 是字符串不是 int），
    整个探针当场死掉 —— 而排在它后面的**鼠标定向那一节（本轮的核心未知量）
    一个字都没跑到**。一轮真机要 6 分钟，这种"前面崩了后面白瞎"的浪费必须堵住。
    """
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001
        info(f"⚠️ {label} 崩了", f"{type(exc).__name__}: {exc} —— 这一节的结论记为**未覆盖**")
        traceback.print_exc()
        return None


# ══════════════════════════════════════════════════════════════════════════
# 前台基线：判「不打扰」之前，**必须**先有一个被验证过是稳定的起点
# ══════════════════════════════════════════════════════════════════════════

def _desktop_hwnd() -> int:
    return win32.root_ancestor(win32.window_from_point(5, 5))


def sample_foreground(seconds: float = 1.6, step: float = 0.4) -> list[tuple]:
    """在一段时间里连续采样前台（hwnd, 标题），用来判断"它到底稳不稳"。"""
    out = []
    for _ in range(max(1, int(seconds / step))):
        h = win32.foreground_window()
        out.append((int(h or 0), win32.window_text(h) if h else ""))
        time.sleep(step)
    return out


def is_stable(seconds: float = 1.6, step: float = 0.4) -> tuple[bool, str]:
    s = sample_foreground(seconds, step)
    titles = [t for _h, t in s]
    return (len(set(titles)) == 1), " → ".join(dict.fromkeys(titles))


def stable_baseline(avoid: int = 0) -> Snapshot | None:
    """
    建立一个**被验证过是稳定的**中性前台，作为「不打扰」判据的起点。

    ═══ 为什么非要有这一步（第二轮真机实测的教训）═══

    第一版直接把「把前台让给桌面」当成基线，然后前后对比。结果 8 条「不打扰」
    判据**全部**失败，而失败的那句话一模一样：

        'Program Manager' → 'settings.json - Notepad'

    中间我们只做了 `SetValue` / `Invoke` 这些元素级动作 —— 它们**根本不该动前台**。
    真相是那条基线本身是**过渡态**：把前台让给桌面之后，系统会在随后一两秒把前台
    还给上一个真实应用（这里是 Win11 记事本，它还会自己激活一次）。
    拿过渡态当基线，后面所有 ✅/❌ 都没有意义 —— 这正是本仓库反复记录的
    「判据本身失效」那一类，比结论错更危险，因为它看起来是有结论的。

    所以：**先设、再连续采样验证、稳了才用它**；不稳就换下一个候选。

    返回 None 表示当前环境**连一个稳定的中性前台都找不到** —— 此时所有「不打扰」
    结论都必须记为**未验证**，而不是"通过"或"不通过"。
    """
    candidates: list[tuple[str, int]] = []
    desk = _desktop_hwnd()
    if desk and desk != avoid:
        candidates.append(("桌面", desk))
    for row in apps.window_rows(limit=50):
        h = int(row["id"], 16)
        if h != avoid and h not in {c[1] for c in candidates}:
            candidates.append((f"窗口 {row.get('title')!r}", h))

    for label, hwnd in candidates[:4]:
        win32.set_foreground_window(hwnd)
        time.sleep(0.4)
        stable, seq = is_stable()
        info(f"基线候选[{label}]", f"稳定={stable} 轨迹={seq}")
        if stable:
            cur = snap()
            if cur.fg == hwnd:
                info("选定基线", f"{label} hwnd={hex(hwnd)}")
                return cur
    return None


# 本轮「本进程有没有前台权力」——由 `control_experiment` 写入，供 `judge_quiet` 消费。
# ⚠️ 它是**跨小节共享的判据前提**，不是某一节的局部状态：拿不到前台权力时，
#    「前台没变」是恒真的废话，那一轮所有 ✅ 都不能采信。
_HAS_FOREGROUND_POWER: bool | None = None


def judge_quiet(label: str, baseline: Snapshot | None) -> None:  # noqa: ANN001
    """
    收尾时判「不打扰」：**基线不稳、或本轮没有前台权力，就不下结论**。

    ⚠️ 刻意不把这两种情况记成 ❌，也不记成 ✅ —— 那是**未验证**。
    本项目最忌讳的失效形态就是拿"没测到"冒充"测到了没问题"。

    ⚠️ 两个方向的处理**不对称**（这是刻意的）：
      · 判 **✅**（前台没变）时，必须先证明本进程**有能力**改前台 —— 否则那是废话；
      · 判 **❌**（前台变了）时**不需要**这个前提 —— 变都变了，说明确实有事发生。
    """
    after = snap()
    if baseline is None:
        info(f"{label}:不打扰用户", "**未验证** —— 本环境找不到稳定的中性前台基线，"
                                    "前后对比无意义（见 stable_baseline 的说明）")
        return
    same, why = baseline.same_as(after)
    if same and _HAS_FOREGROUND_POWER is not True:
        info(f"{label}:不打扰用户",
             "**未验证** —— 本轮「本进程有前台权力」那条对照实验没过，"
             "「前台没变」在此前提下是恒真判据，不能当结论（见 control_experiment）")
        return
    ok(f"{label}:不打扰用户", same, why or f"与基线一致（{baseline.fg_title!r}）")


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


# ══════════════════════════════════════════════════════════════════════════
# 目标发现
# ══════════════════════════════════════════════════════════════════════════

# 一个"看起来是真的窗口"的最小尺寸。⚠️ 必须过滤，否则 UWP 应用会先冒出一个
# 202x56 @(0,0) 的占位/隐藏窗，后面的命中测试全打在桌面上 —— 首轮实测就踩到了，
# 表现为「算出来的点下面居然是回收站图标」。
_MIN_W, _MIN_H = 200, 150


def _plausible(row: dict) -> bool:
    return (row.get("w", 0) >= _MIN_W and row.get("h", 0) >= _MIN_H
            and not row.get("minimized"))


@dataclass
class Target:
    """一个被测目标窗口（按**窗口类**分档，不是按应用名）。"""

    key: str                       # 分档名：classic / uwp / chromium
    label: str                     # 人读
    launcher: list[str] | None     # 起不来时用什么拉起（None = 只能靠已开着的）
    classes: tuple[str, ...]       # 顶层窗类名匹配
    apps: tuple[str, ...] = ()     # 进程名匹配（Win11 记事本这类已经 UWP 化，类名会变）
    row: dict | None = None
    notes: list[str] = field(default_factory=list)


TARGETS: list[Target] = [
    Target("classic", "经典 Win32（记事本）", ["notepad.exe"], ("Notepad",), ("notepad",)),
    Target("uwp", "UWP（计算器）", ["calc.exe"], ("ApplicationFrameWindow",), ("calculator",)),
    # Edge 的 exe 不在 PATH 上（实测 `msedge.exe` → WinError 2），故给完整路径。
    # 命中不了就记为「未覆盖」——Chromium 是本探针最想覆盖的一档（合成消息在那类
    # 应用上最可能被 hit-test 丢掉），所以值得多写一个候选路径。
    Target("chromium", "Chromium（Edge）",
           [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
           ("Chrome_WidgetWin_1",), ("msedge",)),
]


def find_target(t: Target, exclude: set[str]) -> dict | None:
    """
    找一个该分档的顶层窗。`exclude` 是探针启动**之前**就存在的窗口 id ——
    只认新出现的，因为探针要的是「我刚起的那个」，而已开着的同名窗会让判据失真。
    """
    for row in apps.window_rows(limit=200):
        if row["id"] in exclude or not _plausible(row):
            continue
        if row.get("class") in t.classes or (row.get("app") or "").lower() in t.apps:
            return row
    return None


def ensure_target(t: Target, exclude: set[str], launch: bool) -> dict | None:
    """先找已存在的；没有就按 `launcher` 拉起并轮询等待（**不固定 sleep**）。"""
    row = find_target(t, exclude)
    if row or not launch or not t.launcher:
        return row
    print(f"  → 拉起 {t.launcher}")
    exe = next((p for p in t.launcher if "\\" not in p or pathlib.Path(p).exists()),
               t.launcher[0])
    if "\\" in exe and not pathlib.Path(exe).exists():
        info(f"{t.key}:启动失败", f"候选路径都不存在：{t.launcher} —— **未覆盖**")
        return None
    try:
        subprocess.Popen([exe])  # noqa: S603
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:启动失败", f"{type(exc).__name__}: {exc} —— **未覆盖**")
        return None
    # 轮询：固定 sleep 在慢机器上会偶发失败，而失败会被误读成"不支持"。
    # 给足 40s：Win11 上 Store 应用（记事本/计算器）冷启动明显慢于经典 Win32。
    for _ in range(80):
        time.sleep(0.5)
        row = find_target(t, exclude)
        if row:
            return row
    return None


def kill(exe: str) -> None:
    subprocess.run(["taskkill", "/F", "/IM", exe],  # noqa: S603,S607
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def fresh_dialog(how: str = "winver", timeout: float = 15.0) -> dict | None:
    """
    拉起一个**全新的** `#32770` 对话框并返回它的窗口行。

    为什么每次都要新的：鼠标那一节判「点中了」的唯一强证据是**对话框消失**，
    而它一旦消失就没法再测下一种技法。同理也保证了每种技法面对的是**同样干净**的
    初始状态（不会因为前一种技法改了什么而互相污染）。

    `how` 有两种来源，**刻意不止一种**：
      · `winver`  —— 系统自带的经典对话框；
      · `msgbox`  —— PowerShell/WinForms 弹的消息框。
    第二种存在的理由：若只在一种来源上测出"鼠标定向不成立"，分不清是
    **"这条路整体不成立"**还是**"这台机器上那个特定程序有防护"**。
    多一个来源才配得上"此技法不成立"这句话。
    """
    before = {r["id"] for r in apps.window_rows(limit=200)}
    if how == "msgbox":
        cmd = ("Add-Type -AssemblyName System.Windows.Forms; "
               "[void][System.Windows.Forms.MessageBox]::Show('cccu-probe','cccu')")
        subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd])  # noqa: S603,S607
    else:
        subprocess.Popen(["winver.exe"])  # noqa: S603,S607
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        for row in apps.window_rows(limit=200):
            if row["id"] not in before and row.get("class") == "#32770":
                return row
    return None


def close_dialogs() -> None:
    """关掉本探针可能留下的对话框宿主进程。"""
    kill("winver.exe")
    kill("powershell.exe")
    time.sleep(0.4)


def dialog_button(dialog_hwnd: int, want: str | None = None) -> tuple[int, int, int, str] | None:
    """
    在对话框里找一个可见按钮，返回 `(子HWND, 中心点屏幕x, 中心点屏幕y, 文字)`。

    ⚠️ 用 `FindWindowExW` 枚举而非 UIA：UIA 的 `ControlFromPoint` 依赖点命中，
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
        text = win32.window_text(h) or ""
        if not (r and win32.is_window_visible(h) and (r[2] - r[0]) > 10):
            continue
        if want and want not in text:
            continue
        return (h, (r[0] + r[2]) // 2, (r[1] + r[3]) // 2, text)
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

    win32.set_foreground_window(desk)
    time.sleep(0.4)

    global _HAS_FOREGROUND_POWER
    _HAS_FOREGROUND_POWER = bool(can_lose or can_take)
    return ok("对照实验:本进程有前台权力", _HAS_FOREGROUND_POWER,
              "否则「前台没变」是恒真判据，本探针的「不打扰」结论全部失效"
              "（本轮所有 ✅ 都会被降级成「未验证」；❌ 不受影响）")


# ══════════════════════════════════════════════════════════════════════════
# ② UIA 路线（首选）：按名字找到按钮 → Invoke
# ══════════════════════════════════════════════════════════════════════════

def _walk(ctrl, depth: int = 0, max_depth: int = 8, out: list | None = None) -> list:  # noqa: ANN001
    """
    递归收集一棵 UIA 子树的元素（带上限）。

    ⚠️ `max_depth` 与 `out` 上限都要有：Chromium/Electron 的树可以是几万节点，
    无界遍历会把探测跑成"卡住"。本项目在 Linux 侧有同类熔断（访问节点预算）。
    """
    if out is None:
        out = []
    if depth > max_depth or len(out) > 800:
        return out
    out.append(ctrl)
    kids = []
    try:
        kids = list(ctrl.GetChildren())
    except Exception:  # noqa: BLE001 —— 有些 provider 不给 GetChildren
        try:
            k = ctrl.GetFirstChildControl()
            while k is not None:
                kids.append(k)
                k = k.GetNextSiblingControl()
        except Exception:  # noqa: BLE001
            kids = []
    for k in kids:
        _walk(k, depth + 1, max_depth, out)
    return out


def uia_invoke_button(t: Target, row: dict, anchor_fg: bool) -> None:
    """
    走 UIA：按**名字**在窗口里找到按钮 → `InvokePattern.Invoke()`。

    ⚠️ 为什么这条是**首选**：它是本项目在 Linux 侧确立的第一原则（元素级零坐标）。
    DEMO 已实测「目标在后台时元素级动作依然生效、前台不变」，比消息路线可靠——
    消息路线还要赌「子 HWND 存在 + 应用自己处理消息 + lParam 是客户区坐标」。

    `anchor_fg`：把目标窗口设为前台之后再 Invoke（测"前台时会不会额外抢一下"），
    还是**保持后台**（测"后台能不能干活且不抢"）。两种都要测：
    产品的默认形态是后者，但前者能分辨"技法不对"与"后台不行"。
    """
    tag = "前台" if anchor_fg else "后台"
    print(f"\n=== ② UIA 路线 · {t.label} · 目标{tag} ===")
    hwnd = int(row["id"], 16)
    if anchor_fg:
        win32.set_foreground_window(hwnd)
        time.sleep(0.5)
        ok(f"{t.key}:目标确实在前台({tag})", snap().fg == hwnd, f"前台={snap().fg_title!r}")
        baseline = snap()
    else:
        # 基线必须**验证过稳定**才算数 —— 见 stable_baseline 的长注释
        baseline = stable_baseline(avoid=hwnd)
    info(f"{t.key}:UIA 起点前台({tag})", (baseline.fg_title if baseline else "<无稳定基线>"))

    try:
        uia = _uia.import_uia()
        root = uia.ControlFromHandle(hwnd)
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:ControlFromHandle({tag})", f"失败 {type(exc).__name__}: {exc}")
        return
    if root is None:
        info(f"{t.key}:ControlFromHandle({tag})", "返回 None")
        return

    nodes = _walk(root)
    buttons = [n for n in nodes if _control_type_name(n) == "ButtonControl"]
    names = [getattr(b, "Name", "") for b in buttons][:8]
    info(f"{t.key}:树规模({tag})", f"{len(nodes)} 个节点，其中按钮 {len(buttons)} 个：{names}")
    target_btn = next((b for b in buttons
                       if (getattr(b, "Name", "") or "").strip() in ("确定", "OK", "是", "Yes")),
                      None)
    if target_btn is None:
        # ⚠️ 记 **info 而不是失败**：算盘上没有「确定」按钮不代表 UIA 这条路不行 ——
        #    那只是这个应用没有可判定的目标。把它记成 ❌ 就等于用"没测到"冒充"测出来不行"。
        info(f"{t.key}:UIA 找不到「确定」按钮({tag})",
             f"**未覆盖**（该窗口的按钮是：{names}）—— 判据只对对话框类窗口成立")
        return
    ok(f"{t.key}:UIA 找得到「确定」按钮({tag})", True, f"名字={getattr(target_btn, 'Name', '')!r}")

    invoked = False
    exc_note = ""
    try:
        target_btn.GetInvokePattern().Invoke()
        invoked = True
    except Exception as exc:  # noqa: BLE001
        exc_note = f"{type(exc).__name__}: {exc}"
    time.sleep(1.2)
    gone = not win32.is_window(hwnd)

    # ★ 判据取**外部可观测事实**（窗口消失），**不取有没有抛异常**。
    #   真机实测抓到的反例：`Invoke()` 抛 `COMError(灾难性故障)`，而对话框**确实关了** ——
    #   那是 UIA 的经典形态：动作生效后目标窗户被销毁，调用方手上的 COM 对象随之失效，
    #   于是这次调用「报错」了。若拿异常当判据，就会把一次**成功**记成失败，
    #   进而让上层降级到坐标点击 —— 那正是我们要避免的"抢鼠标"路径。
    if exc_note and gone:
        info(f"{t.key}:Invoke 抛了异常但动作其实成功({tag})",
             f"{exc_note} —— 目标随动作销毁导致 COM 对象失效，属**已知形态**，不是失败")
    ok(f"{t.key}:UIA Invoke 生效（对话框消失）({tag})", gone,
       "对话框已消失" if gone else f"对话框还在 → Invoke 没生效（{exc_note or '未抛异常'}）")
    # ⚠️ 当**目标本身就是基线**（前台那一档）时，"前台变了"是**目标关闭的正常后果**：
    #    Windows 在活动窗口消失后必然挑一个继任者。那不是我们打扰用户，是系统行为，
    #    故这一档刻意**不判**「不打扰」，只判"没有抢走用户别的东西"（指针没动）。
    if anchor_fg:
        info(f"{t.key}:不打扰用户({tag})",
             "**不适用** —— 基线就是目标窗口，它被关掉后前台必然易主（系统行为，非打扰）")
        info(f"{t.key}:指针未被扰动({tag})", f"指针={snap().cursor}")
    else:
        judge_quiet(f"{t.key}[UIA Invoke/{tag}]", baseline)
    if win32.is_window(hwnd):
        close_dialogs()


def _control_type_name(ctrl) -> str:  # noqa: ANN001
    try:
        return str(getattr(ctrl, "ControlTypeName", "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ══════════════════════════════════════════════════════════════════════════
# ③ 消息路线 · 键盘（对照组：这条路已在 DEMO 里验过）
# ══════════════════════════════════════════════════════════════════════════

def keyboard_route(row: dict, token: str, read_back) -> None:  # noqa: ANN001
    """
    在记事本里投 `WM_CHAR` 到**子 HWND**，然后**读回文档内容**判命中。

    ⚠️ 判据必须是读回，不能看 `PostMessage` 的返回值：那个返回值只说"排进队列了"。
    读的通道（UIA `ValuePattern`）与写的通道（消息投递）完全独立，这才算数。
    """
    print("\n=== ③ 消息路线 · 键盘（对照组）===")
    drill = win32.deepest_child_at(row["x"] + row["w"] // 2, row["y"] + row["h"] // 2)
    deepest = hwnd_of(drill.get("deepest"))
    info("子 HWND 下钻", f"深度={drill.get('depth')} 最深={hex(deepest) if deepest else None} "
                         f"类名={(drill.get('deepest') or {}).get('class')} "
                         f"是子窗={drill.get('is_child')}")
    if not deepest:
        ok("键盘:能取到子 HWND（定向投递的前提）", False, "取不到 → 这条路不成立")
        return

    # 基线必须**验证过稳定**：投递本身不该动前台，动的只能是环境噪声（见 stable_baseline）
    baseline = stable_baseline(avoid=int(row["id"], 16))
    for ch in token:
        post_message(deepest, WM_CHAR, ord(ch), 0)
        time.sleep(0.05)
    time.sleep(1.0)

    text = read_back()
    ok("键盘:内容真的进了目标应用", token in text,
       f"读回含 {token!r}" if token in text else f"读回={text[:60]!r}")
    judge_quiet("键盘[定向投递]", baseline)


# ══════════════════════════════════════════════════════════════════════════
# ④ 消息路线 · 鼠标（**本探针的核心未知量**）
# ══════════════════════════════════════════════════════════════════════════

def mouse_tech(name: str, fn, fg: bool, source: str = "winver") -> None:  # noqa: ANN001
    """
    在一个**全新对话框**上试一种鼠标技法，判据是「对话框消失」+「不打扰」。

    ⚠️ 每次都用新对话框是刻意的：对话框一消失就没法再测，而且新的能保证每种技法
    面对同样干净的初始状态（互相不污染）。

    `source` 是对话框来源（`winver` / `msgbox`）。**两种都要试**：只在一种来源上
    得出"此技法不成立"，分不清是「这条路整体不行」还是「那个特定程序有防护」。
    """
    tag = ("前台" if fg else "后台") + ("" if source == "winver" else f"/{source}")
    print(f"\n=== ④ 鼠标技法 · {name} · 目标{tag} ===")
    dlg = fresh_dialog(how=source)
    if not dlg:
        info(f"鼠标[{name}/{tag}]", "拉不起 winver 对话框，**未覆盖**")
        return
    hwnd = int(dlg["id"], 16)
    btn = dialog_button(hwnd, want="确定")
    if not btn:
        info(f"鼠标[{name}/{tag}]", "对话框里找不到「确定」按钮，**未覆盖**")
        kill("winver.exe")
        return
    bh, bx, by, caption = btn
    info(f"鼠标[{name}/{tag}]:目标按钮", f"文字={caption!r} 子HWND={hex(bh)} 屏幕点=({bx},{by})")

    if fg:
        win32.set_foreground_window(hwnd)
        time.sleep(0.5)
        baseline = snap()
    else:
        # 基线必须验证过稳定；且**必须避开目标窗口**（它稍后会被关掉，
        # 若拿它当基线，"前台变了"就只是它关闭的正常后果，不是我们打扰了用户）
        baseline = stable_baseline(avoid=hwnd)

    detail = fn(hwnd, bh, bx, by)
    time.sleep(1.2)

    gone = not win32.is_window(hwnd)
    if not gone:
        # 复检一次：万一是"关得慢"而不是"没点中"，判据会冤枉这条技法。
        # 代价 2 秒，换掉一整类假阴性。
        time.sleep(2.0)
        gone = not win32.is_window(hwnd)
    ok(f"鼠标[{name}/{tag}]:点中了（对话框消失）", gone,
       detail + ("｜对话框已消失" if gone else "｜对话框还在（含 2s 复检）→ 此技法**不成立**"))
    if fg:
        info(f"鼠标[{name}/{tag}]:不打扰用户",
             "**不适用** —— 基线就是目标窗口，它被关掉后前台必然易主（系统行为，非打扰）")
    else:
        judge_quiet(f"鼠标[{name}/{tag}]", baseline)
    if win32.is_window(hwnd):
        close_dialogs()


def uia_write_text(t: Target, row: dict, token: str, read_back) -> None:  # noqa: ANN001
    """
    走 UIA：往目标窗口的编辑区**直接写值**（`ValuePattern.SetValue`），目标保持**后台**。

    ⚠️ 为什么这条是产品的主线：`type_text(ref=...)` 的最优路径就是它 —— 零坐标、
    零键盘事件、不碰你任何东西。DEMO 已实测它在后台可用，这里把它在**记事本**上
    再固化一遍（记事本是最常见的灰区测试对象，且读回通道与写入通道完全独立）。
    """
    print(f"\n=== ②b UIA 路线 · 写值（{t.label}，目标后台）===")
    hwnd = int(row["id"], 16)
    baseline = stable_baseline(avoid=hwnd)
    info(f"{t.key}:UIA 写值起点前台", (baseline.fg_title if baseline else "<无稳定基线>"))

    try:
        uia = _uia.import_uia()
        root = uia.ControlFromHandle(hwnd)
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:UIA 写值", f"ControlFromHandle 失败 {type(exc).__name__}: {exc}")
        return

    nodes = _walk(root)
    # 找一个支持 ValuePattern 的元素（文档/编辑框）
    edit = None
    for n in nodes:
        try:
            if n.GetPattern(_uia.resolve_pattern("ValuePattern")) is not None:
                edit = n
                break
        except Exception:  # noqa: BLE001
            continue
    if edit is None:
        info(f"{t.key}:UIA 写值", f"**未覆盖** —— {len(nodes)} 个节点里没有支持 ValuePattern 的")
        return

    wrote = False
    try:
        edit.GetValuePattern().SetValue(token)
        wrote = True
    except Exception as exc:  # noqa: BLE001
        info(f"{t.key}:UIA 写值异常", f"{type(exc).__name__}: {exc}")
    time.sleep(1.0)

    text = read_back()
    back = token in text
    ok(f"{t.key}:UIA 写值生效（读回命中）", wrote and back,
       f"读回含 {token!r}" if back else f"读回={text[:60]!r}")
    judge_quiet(f"{t.key}[UIA 写值]", baseline)


def _tech_post_to_button(dlg: int, bh: int, bx: int, by: int) -> str:  # noqa: ARG001
    """投 `WM_MOUSEMOVE`+`WM_LBUTTONDOWN/UP` 给**按钮子窗**，`lParam` = 客户区坐标。"""
    c = client_point_of(bh, bx, by)
    if c is None:
        return "ScreenToClient 失败"
    lp = pack_xy(*c)
    post_message(bh, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.08)
    post_message(bh, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    post_message(bh, WM_LBUTTONUP, 0, lp)
    return f"PostMessage 给子窗(客户区={c})"


def _tech_send_to_button(dlg: int, bh: int, bx: int, by: int) -> str:  # noqa: ARG001
    """与上同，但用 `SendMessage`（同步，等对方处理完）。"""
    c = client_point_of(bh, bx, by)
    if c is None:
        return "ScreenToClient 失败"
    lp = pack_xy(*c)
    send_message(bh, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    send_message(bh, WM_LBUTTONUP, 0, lp)
    return f"SendMessage 给子窗(客户区={c})"


def _tech_bm_click(dlg: int, bh: int, bx: int, by: int) -> str:  # noqa: ARG001
    """
    `BM_CLICK` —— **按钮控件自己的**"我被按了"消息。

    为什么值得单列：主题化（UxTheme）的按钮对**合成鼠标消息**历来挑剔（它要的是
    "真光标在我身上"那种鼠标消息），而 `BM_CLICK` 是控件级别的语义消息，
    绕过命中测试。若这条通，至少"按按钮"这类最常见的坐标点击有了不抢焦点的办法。
    """
    send_message(bh, BM_CLICK, 0, 0)
    return "SendMessage(BM_CLICK) 给按钮子窗"


def _tech_post_to_dialog(dlg: int, bh: int, bx: int, by: int) -> str:  # noqa: ARG001
    """
    投给**对话框本身**（顶层），`lParam` 是**屏幕坐标**。

    这是"父窗自己派发到子控件"的那条路 —— 有些应用（尤其非 Win32 框架）只认这条。
    与投给子窗是**两种不同的语义**，所以分开测。
    """
    lp = pack_xy(bx, by)
    post_message(dlg, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.08)
    post_message(dlg, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    post_message(dlg, WM_LBUTTONUP, 0, lp)
    return f"PostMessage 给顶层对话框(屏幕坐标={bx},{by})"


# ══════════════════════════════════════════════════════════════════════════
# ⑤ PrintWindow：黑图率与最坏时延（Phase 1 的前置情报）
# ══════════════════════════════════════════════════════════════════════════

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
# ⑥b **主窗口**（非对话框）上的定向点击 —— 决定"灰区软件能不能不打扰"
#
# ═══ 为什么必须单独测这一节 ═══
#
# 第五节在**对话框按钮**上测出「定向鼠标四种技法全失效」，而推测的机制是
# 「对话框要求自己处于活动状态才响应按钮点击」。若真是这样，那是**对话框特有**的性质，
# 主窗口上的按钮未必如此。
#
# 这件事直接决定产品形态：
#   · 有元素树的软件 → 走元素级，本来就不打扰（已证）；
#   · **没有元素树的软件**（SWT/Java、自绘控件、游戏）→ 只能坐标点击。
#     其中"整个界面是一块渲染面、没有子 HWND"的那类（SWT 就是）连**定向目标都找不到**，
#     而"主窗口里真有子控件"的那类还有机会。
#
# 所以这一节回答两个问题：
#   ① 桌面上的主窗口里，有多少**根本没有子 HWND**（= 定向投递从构造上就没法谈）？
#   ② 对**有**子控件的主窗口，同一套消息技法能不能生效？
# ══════════════════════════════════════════════════════════════════════════

def _enum_children(hwnd: int, cap: int = 40) -> list[dict]:  # noqa: ANN001
    """枚举一个窗口的直接子窗（`FindWindowExW` 逐个取兄弟），带上限。"""
    u = ctypes.windll.user32
    u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowExW.restype = wintypes.HWND
    out: list[dict] = []
    cur = wintypes.HWND(0)
    while len(out) < cap:
        cur = u.FindWindowExW(wintypes.HWND(hwnd), cur, None, None)
        if not cur:
            break
        h = int(cur)
        out.append({"hwnd": h, "class": win32.class_name(h),
                    "text": win32.window_text(h) or "",
                    "rect": win32.window_rect(h)})
    return out


def child_hwnd_census() -> None:
    """
    统计桌面上主窗口的**子 HWND 拥有率**。

    这是本探针里最可泛化的一条观察：定向投递（`PostMessage` 到子窗）的**前提是
    目标有子 HWND**。整块自绘界面的应用（SWT/Java、Electron 的渲染面）没有子窗，
    那么"不抢焦点地点击"在那类应用上**从构造上就不成立** —— 无论消息参数写得多对。
    """
    print("\n=== 6b-① 子 HWND 普查（定向投递的前提）===")
    total = with_child = 0
    rows: list[str] = []
    for row in apps.window_rows(limit=60):
        if row.get("class") == "#32770":        # 对话框另算
            continue
        total += 1
        kids = _enum_children(int(row["id"], 16))
        if kids:
            with_child += 1
            if len(rows) < 8:
                rows.append(f"{row.get('app')}/{row.get('class')}: "
                            f"{[k['class'] for k in kids[:5]]}")
    info("主窗口子 HWND 普查", f"共 {total} 个主窗口，其中 {with_child} 个有子 HWND"
                              f"（{with_child}/{max(total, 1)}）")
    for r in rows:
        info("  有子窗的例子", r)
    if total:
        ok("桌面上存在有子 HWND 的主窗口（存在可定向的目标）", with_child > 0,
           f"{with_child}/{total}")
    # ⚠️ 刻意**不**断言"没有子窗的主窗口一定不能定向" —— 那需要拿一个真的无子窗应用
    #    （SWT/DBeaver 这类）来测，本机没装。这里只报事实，不做外推。


def main_window_mouse(row: dict, read_back) -> None:  # noqa: ANN001
    """
    在**主窗口**（不是对话框）上测定向鼠标：点击编辑区把光标挪到中间，再打字。

    ═══ 判据为什么这样设计 ═══

    主窗口上的点击**没有**"对话框消失"那种干脆的证据，所以要自己造一个确定性的：
      1. 先把正文设成 `0123456789` 并确认；
      2. 用 `EM_SETSEL(0,0)` 把**光标复位到开头**（这是**准备**，不是被测行为）；
      3. 定向投递一次点击，落在编辑区**水平中点**；
      4. 再投一个字符 `X`；
      5. 读回：落在中间（`01234X56789`）说明**点击真的生效了**；
         落在开头（`X0123456789`）说明点击**没生效**（打字是上次已证可用的能力）。

    这个设计的好处是**两半都有独立已知的答案**：键盘投递与读回都已被前面的小节证过，
    所以结果的差异只能归因于那一次点击。若 `EM_SETSEL` 不被该控件支持，就**记为未覆盖**
    而不是判失败 —— 那说明判据搭不起来，不是"技法不行"。
    """
    print(f"\n=== 6b-② 主窗口定向点击 · {row.get('app')}/{row.get('class')} ===")
    hwnd = int(row["id"], 16)
    try:
        from computer_use_mcp.backend.windows.driver import _uia as _u  # noqa: PLC0415
        uia = _u.import_uia()
        root = uia.ControlFromHandle(hwnd)
    except Exception as exc:  # noqa: BLE001
        info("主窗口定向点击", f"取不到 UIA 根 {type(exc).__name__}: {exc} —— **未覆盖**")
        return

    # 找到那个支持 ValuePattern 的编辑区（同时拿到它的子 HWND）
    edit_el = None
    for n in _walk(root):
        try:
            if n.GetPattern(_uia.resolve_pattern("ValuePattern")) is not None:
                edit_el = n
                break
        except Exception:  # noqa: BLE001
            continue
    if edit_el is None:
        info("主窗口定向点击", "该主窗口没有支持 ValuePattern 的编辑区 —— **未覆盖**")
        return
    edit_hwnd = int(getattr(edit_el, "NativeWindowHandle", 0) or 0)
    info("主窗口定向点击:编辑区", f"子HWND={hex(edit_hwnd) if edit_hwnd else '0(无)'} "
                                  f"类名={getattr(edit_el, 'ClassName', '?')}")
    if not edit_hwnd:
        info("主窗口定向点击", "编辑区没有自己的 HWND（XAML/自绘）→ **定向投递无从谈起**")
        return

    EM_SETSEL = 0x00B1
    LINES = [f"{i:02d}AAAAAAAA" for i in range(20)]     # 20 行，够铺满编辑区

    def set_text(text: str) -> None:
        edit_el.GetValuePattern().SetValue(text)
        time.sleep(0.4)

    def caret_to_start() -> None:
        send_message(edit_hwnd, EM_SETSEL, 0, 0)
        time.sleep(0.2)

    def line_of_x() -> tuple[int | None, str]:
        """X 落在第几行（`None` = 没找到）。"""
        text = read_back().replace("\r\n", "\n")
        for i, ln in enumerate(text.split("\n")):
            if "X" in ln:
                return i, ln
        return None, text[:60]

    # ═══ 判据怎么搭 ═══
    #
    # ⚠️ 第一版把落点定在编辑区**垂直中点**、正文只有一行 —— 结果字符落到了**末尾**。
    #    那不是"点击没生效"：RichEdit 里**点在正文下方**本来就会把光标放到文末。
    #    判据因此分不清"点中了但位置是文末"与"根本没点中"。实测代价是一轮白跑。
    #
    # 现在改成**多行**（20 行铺满），落点取垂直中点：
    #   · 点击没生效 → 光标停在开头（EM_SETSEL 复位过）→ X 落在**第 0 行**；
    #   · 点击生效   → 光标落在**中间某行** → X 落在第 8~12 行一带。
    #   **「不是第 0 行、也不是最后一行」**就是判据 —— 它对 y 的像素级精度不敏感。
    set_text("\r\n".join(LINES))
    caret_to_start()
    baseline = stable_baseline(avoid=hwnd)
    info("主窗口定向点击:起点前台", baseline.fg_title if baseline else "<无稳定基线>")

    # 对照：不点击，直接打字 → 必须落在第 0 行（证明 EM_SETSEL 与读回都可靠）
    caret_to_start()
    post_message(edit_hwnd, WM_CHAR, ord("C"), 0)
    time.sleep(0.7)
    ctrl_line, ctrl_ln = line_of_x()
    ok("主窗口定向点击[对照]:不点击时落在第 0 行（判据搭得起来）",
       ctrl_line == 0, f"行号={ctrl_line} 该行={ctrl_ln!r}")

    # 被测：定向点击编辑区中点，再打字
    set_text("\r\n".join(LINES))
    caret_to_start()
    r = win32.window_rect(edit_hwnd)
    mx, my = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
    c = client_point_of(edit_hwnd, mx, my)
    if c is None:
        info("主窗口定向点击", "ScreenToClient 失败 —— **未覆盖**")
        return
    info("主窗口定向点击:落点", f"屏幕=({mx},{my}) 客户区={c}")
    lp = pack_xy(*c)
    post_message(edit_hwnd, WM_MOUSEMOVE, 0, lp)
    time.sleep(0.08)
    post_message(edit_hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    time.sleep(0.08)
    post_message(edit_hwnd, WM_LBUTTONUP, 0, lp)
    time.sleep(0.5)
    post_message(edit_hwnd, WM_CHAR, ord("X"), 0)
    time.sleep(0.8)
    line, ln = line_of_x()

    moved = line is not None and 0 < line < len(LINES) - 1
    ok("主窗口定向点击:点击生效（字符落在中间行而不是第 0 行）", moved,
       f"行号={line} 该行={ln!r} —— " + ("光标确实被点击挪走了" if moved
                                          else "光标没动 → 主窗口上的定向鼠标也不成立"))
    judge_quiet("主窗口定向点击", baseline)


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
    cx, cy = row["x"] + row["w"] // 2, row["y"] + row["h"] // 2
    drill = win32.deepest_child_at(cx, cy)
    deepest = hwnd_of(drill.get("deepest"))
    if not deepest:
        info("滚轮/拖拽", "取不到子 HWND，无从投递")
        return
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
    lp = pack_xy(*client_point_of(deepest, cx, cy) or (0, 0))
    for msg, wp in ((WM_LBUTTONDOWN, MK_LBUTTON), (WM_MOUSEMOVE, MK_LBUTTON), (WM_LBUTTONUP, 0)):
        post_message(deepest, msg, wp, lp)
        time.sleep(0.08)
    time.sleep(0.4)
    info("拖拽:消息序列", "Down→Move→Up 已投递（起点=终点，仅验证**投得出去**，不判命中）")


# ══════════════════════════════════════════════════════════════════════════

def main() -> int:  # noqa: C901
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

    # 清场：探针要的是"新出现的"窗口，旧的不算。
    # ⚠️ **notepad 也一起清掉**：第二轮实测里它反复自己激活（前台轨迹里出现
    # `'Program Manager' → 'settings.json - Notepad'`），是污染「不打扰」判据的
    # 噪声源。它只在需要它的那一节才被拉起，那一节跑完立刻再关掉。
    for exe in ("notepad.exe", "winver.exe"):
        kill(exe)
    time.sleep(1.5)
    pre_existing = {r["id"] for r in apps.window_rows(limit=200)}
    print(f"  启动前已有 {len(pre_existing)} 个顶层窗")

    # 前台基线诊断 —— 先搞清楚"谁是噪声"，否则后面所有对比都不可信
    print("\n=== 0b) 前台稳定性诊断（判据有效性的前提）===")
    stable, seq = is_stable(seconds=3.2, step=0.4)
    info("静止 3.2s 的前台轨迹", f"稳定={stable}｜{seq}")
    ok("环境里没有窗口反复自行激活（基线可测）", stable,
       "否则「不打扰」的前后对比会被噪声污染，必须先把噪声源关掉再测")
    b = guarded("建立稳定基线", stable_baseline)
    info("稳定基线", f"{b.fg_title!r} hwnd={hex(b.fg)}" if b else "**找不到**")

    if not args.no_launch:
        print("\n=== 1) 发现目标（按窗口类分档）===")
    found: list[tuple[Target, dict]] = []
    for t in TARGETS:
        row = ensure_target(t, pre_existing, not args.no_launch)
        if row:
            found.append((t, row))
            info(f"目标[{t.key}]", f"{t.label} → {row.get('title')!r} class={row.get('class')} "
                                   f"app={row.get('app')} {row['w']}x{row['h']} "
                                   f"@({row['x']},{row['y']})")
        else:
            info(f"目标[{t.key}]", f"{t.label} → **未找到**（未覆盖，不静默跳过）")

    if not found:
        print("  ⛔ 一个目标都没有，无法测试")
        return 5

    dlg_t = Target("dialog", "经典对话框 #32770", None, ("#32770",), ())

    # ① 对照实验（判据有效性的前提）
    guarded("对照实验", control_experiment, found[0][1])

    # ② UIA · 按钮 Invoke，**目标后台**（产品的默认形态，最要紧的那一格）
    dlg_bg = guarded("拉新鲜对话框(后台)", fresh_dialog)
    if dlg_bg:
        guarded("UIA Invoke(后台)", uia_invoke_button, dlg_t, dlg_bg, False)
    else:
        info("UIA 后台 Invoke", "拉不起 winver，**未覆盖**")

    # ③ UIA · 按钮 Invoke，**目标前台**（对照：分辨"技法不对"与"后台不行"）
    dlg_fg = guarded("拉新鲜对话框(前台)", fresh_dialog)
    if dlg_fg:
        guarded("UIA Invoke(前台)", uia_invoke_button, dlg_t, dlg_fg, True)
    else:
        info("UIA 前台 Invoke", "拉不起 winver，**未覆盖**")

    # ④ 鼠标定向 —— **本探针的核心未知量**（四种技法，各用一个全新对话框）
    #    刻意排在记事本之前：这一节对"谁是前台"最敏感，窗口越少噪声越小。
    print("\n=== 4) 消息路线 · 鼠标（本探针的核心未知量）===")
    for name, fn in (("PostMessage→子窗", _tech_post_to_button),
                     ("SendMessage→子窗", _tech_send_to_button),
                     ("BM_CLICK→按钮", _tech_bm_click),
                     ("PostMessage→顶层对话框", _tech_post_to_dialog)):
        guarded(f"鼠标[{name}]", mouse_tech, name, fn, False)
    guarded("鼠标[BM_CLICK/前台]", mouse_tech, "BM_CLICK→按钮", _tech_bm_click, True)
    # ⚠️ 换一个对话框**来源**再测一遍最可疑的那条：只在 winver 上得出"不成立"，
    #    分不清是"这条路整体不成立"还是"那个程序有防护"。多一个来源才配得上结论。
    for name, fn in (("BM_CLICK→按钮", _tech_bm_click),
                     ("PostMessage→子窗", _tech_post_to_button)):
        guarded(f"鼠标[{name}/msgbox]", mouse_tech, name, fn, False, "msgbox")

    # ⑤ 记事本：UIA 写值 + 键盘定向 + 滚轮/拖拽（记事本只在这一段里存在）
    classic = next((r for t, r in found if t.key == "classic"), None)
    if classic:
        def read_notepad() -> str:
            """读回记事本正文（复用 UIA，与任何写入通道都**完全独立**）。"""
            try:
                from computer_use_mcp.backend.base import get_backend  # noqa: PLC0415
                res = get_backend().get_tree(scope="app", app="notepad",
                                             max_depth=8, max_nodes=300)
            except Exception as exc:  # noqa: BLE001
                return f"<读树失败 {type(exc).__name__}: {exc}>"
            stack, val = list(res.items), ""
            while stack:
                n = stack.pop()
                if n.role in ("text", "entry", "document") and not val:
                    try:
                        val = get_backend().element_info(n.native).value or ""
                    except Exception:  # noqa: BLE001
                        pass
                stack.extend(n.children)
            return val

        guarded("UIA 写值", uia_write_text,
                Target("classic", "经典 Win32（记事本）", None, ("Notepad",), ()),
                classic, "UIAWRITE", read_notepad)
        guarded("键盘定向", keyboard_route, classic, "DIRECTED", read_notepad)
        # 6b：主窗口（非对话框）上的定向点击 —— 决定"灰区软件能不能不打扰"
        guarded("主窗口定向点击", main_window_mouse, classic, read_notepad)
        guarded("滚轮/拖拽", wheel_and_drag_probe, classic)
    else:
        info("记事本三项", "没有经典 Win32 目标，**未覆盖**")

    # 6b-① 子 HWND 普查（定向投递的前提，放在最前面跑，受环境噪声影响最小）
    guarded("子 HWND 普查", child_hwnd_census)

    # ⑥ PrintWindow（黑图率 + 最坏时延）
    for t, row in found:
        guarded(f"PrintWindow[{t.key}]", print_window_route, t, row)

    print("\n=== ⑦ 收尾 ===")
    for exe in ("notepad.exe", "winver.exe"):
        kill(exe)

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
