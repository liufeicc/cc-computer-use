"""
Windows 的「虚拟屏」（`backend.windows.driver.desktop`）—— 隐藏桌面。

═══ 为什么 Windows 没有 Xephyr，但有一个**够用的对应物** ═══

Linux 的隔离是「另开一块**显示屏**」（Xephyr）—— agent 在那块屏上干活，用户看不见也摸不着。
Windows **没有**第二块显示屏：一个登录会话只有一套输入设备，而且只有「当前输入桌面」
能收到物理按键。

但有**隐藏桌面**（`CreateDesktopW`）：同一会话里的另一张**桌面**。应用起在上面：
  · 用户**看不见**（不在他的桌面上）；
  · 用户的键鼠**碰不到**（物理输入只进当前输入桌面）；
  · 而我们**够得着**—— 真机实测（`tests/manual_windows_hidden_desktop_probe.py`，18/18）：
    跨桌面枚举窗口 ✅、UIA 挂树 ✅、元素级 `Invoke`/`SetValue` ✅、定向键盘 ✅、
    `PrintWindow` ✅，而用户桌面的**前台、指针、窗口集合全程一个都没动**。

═══ ⚠️ 代价（必须一起记住，否则会把它当成万能药）═══

隐藏桌面上**没有输入设备**：`SendInput` 返回 `sent: 0 / GetLastError=5`（实测），
而定向鼠标的四类技法又已证明全失效（`manual_windows_directed_probe.py`）。

**所以隔离模式下，"只能靠坐标点击"的应用（SWT/Java、自绘控件、游戏）做不了。**
它们正好是「没有无障碍树」的那一类 —— 而在隐藏桌面上，元素级是唯一还能用的通道。
这是一条**能力边界**，不是可以绕过去的实现细节：`capabilities()` 与工具返回都要如实说。

═══ ⚠️ 第二个代价：用户看不见，也就**没法介入** ═══

Linux 的沙箱是一块**看得见**的虚拟屏（Xephyr 窗口就摆在宿主上），agent 卡住时用户可以
自己去看一眼、甚至伸手点两下。隐藏桌面上做不到 —— 用户**永远看不到**那扇窗。
所以诊断信息（截图、读树结果）必须经工具回给模型，没有"你自己去看"这条退路。

═══ 命名 ═══

桌面名带 **server 进程号**（`CCCU-<pid>`），与 Linux 侧 Xephyr 的窗口标题同一套理由：
一个用户可能同时开着多个 Claude 会话，各起各的桌面，名字要能分辨归属。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from typing import Any

from . import win32

# 桌面名的前缀；完整名字后面拼本进程 pid（见模块 docstring）
_NAME_PREFIX = "CCCU"

# Windows 桌面的标准访问权限（与 DEMO 探针里验证过的一致）
DESKTOP_ALL_ACCESS = 0x000F01FF
CREATE_UNICODE_ENVIRONMENT = 0x00000400

# 本进程的"目标桌面"状态。**模块级单例**，理由是调用点多而深：
# `apps.window_rows()` 这类底层函数会被十几处（含驱动层内部）调用，把桌面句柄
# 沿着签名一路传下去，等于给每个函数加一个只有在 Windows 上才有意义的参数。
_STATE: dict[str, Any] = {"name": None, "handle": 0, "isolated": False}


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


# ── 会话模式（与 Linux 同一个环境变量、同一套默认值）────────────────────

def display_mode() -> str:
    """
    `"isolated"`（默认）或 `"real"` —— **与 Linux 侧读的是同一个 `CC_CU_DISPLAY_MODE`**。

    刻意复用同一个旋钮而不是新加一个：用户在两个平台上说的是同一件事
    （"要不要隔离"），两个变量名会让人以为它们是两件事。`real` = 回到老行为
    （应用直接开在用户桌面上），与 Linux 的 `real` 语义也一致。

    ⚠️ 解析规则**只认 `"real"`**，其余一切（含空、拼错）都当 `isolated` ——
    默认值必须是**隔离**那一档，拼错一个字母就静默失去隔离是不能接受的。
    """
    return "real" if (os.environ.get("CC_CU_DISPLAY_MODE") or "").strip().lower() == \
        "real" else "isolated"


def desktop_name() -> str:
    """本次会话的隐藏桌面名（带本进程 pid，便于分辨归属）。"""
    return os.environ.get("CC_CU_WINDOWS_DESKTOP") or f"{_NAME_PREFIX}-{os.getpid()}"


# ── 桌面生命周期 ────────────────────────────────────────────────────────

def _create_or_open(name: str) -> int:
    """
    建（或打开）一张桌面，返回句柄；失败返回 0。

    ⚠️ 先 `CreateDesktopW` 再退 `OpenDesktopW`：桌面**同名已存在**时 `CreateDesktop`
    会失败（而不是"返回已有的那个"），而"已经建过"是常态 —— 同一次会话里
    `ensure_isolated()` 可能被调多次（每次启动应用都要确保，幂等比缓存更可靠）。
    """
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


def ensure_isolated() -> dict[str, Any]:
    """
    确保隐藏桌面已就绪，返回 `{"ok", "name", "handle", "created"}`。**幂等**。

    ⚠️ 句柄**只在本进程内有效**（它是个内核对象句柄）：所以状态存在本模块里、
    由本进程的调用方消费，不跨进程传递。多会话各自建各自的桌面（名字带 pid），
    互不影响 —— 这一点比 Linux 侧的 Xephyr 省心：那边还得靠 `-displayfd` 让
    X server 自己挑空闲屏号来避免竞态，桌面名字带 pid 从构造上就不会撞。

    ⚠️ **返回句柄而不只是布尔**：调用方（`spawn` / 枚举）都要用它，
    而"再查一次当前状态"会引入一个"两次查询之间状态变了"的窗口。
    """
    if is_ready():
        return {"ok": True, "name": _STATE["name"], "handle": _STATE["handle"],
                "created": False}
    name = desktop_name()
    h = _create_or_open(name)
    if not h:
        return {"ok": False, "name": name, "handle": 0, "created": False,
                "error": f"CreateDesktopW/OpenDesktopW 失败 err={ctypes.get_last_error()}"}
    _STATE.update({"name": name, "handle": h, "isolated": True})
    return {"ok": True, "name": name, "handle": h, "created": True}


def use_default() -> None:
    """切回「用户自己的桌面」—— `real` 模式与收尾时用。"""
    _STATE.update({"name": None, "handle": 0, "isolated": False})


def is_isolated() -> bool:
    """
    **本次会话是不是隔离模式** —— 看的是**策略**（`CC_CU_DISPLAY_MODE`），
    不是"桌面句柄建出来没有"。

    ⚠️ 这两件事必须分开，且**以策略为准**。它们在某一个窗口期里会不一致：
    隔离模式已生效、但桌面还没（或没能）建出来。此时若按"句柄有没有"来判，
    整条链路会**悄悄退回用户桌面** —— 而那正是用户明确不想要的。
    所以"没建出来"的后果是**什么都看不到**（`enumerate_target` 返回空），
    不是"看到用户的窗口"。

    与 `is_ready()` 的分工就在这里：本函数答"是不是隔离模式"，那个答"准备好了没"。
    """
    return display_mode() == "isolated"


def is_ready() -> bool:
    """隔离桌面是否**已经建出来**（`is_isolated()` 为真时才谈得上）。"""
    return bool(_STATE["isolated"] and _STATE["handle"])


def target_handle() -> int:
    """当前目标桌面的句柄；**0 = 本线程所在桌面**（`EnumDesktopWindows` 的约定）。"""
    return int(_STATE["handle"]) if is_ready() else 0


def target_name() -> str:
    """人读的目标名（给 `SessionProvider.target_label()` 与工具返回值用）。"""
    return str(_STATE["name"]) if is_isolated() else "用户桌面"


def describe() -> dict[str, Any]:
    """状态摘要（自检/诊断用）。"""
    return {"mode": display_mode(), "isolated": is_isolated(),
            "desktop": _STATE["name"], "handle": _STATE["handle"]}


def release() -> None:
    """
    关闭本进程持有的桌面句柄。**不销毁桌面本身**（`CloseDesktop` 只是放下引用）。

    为什么要显式放：句柄是内核对象引用，进程活着就一直占着。而"关掉桌面本身"
    （删掉那张桌面）需要 `DeleteDesktop`（未公开 API），不做 —— 残留的空白桌面
    没有任何窗口，用户看不见也摸不着，代价可以忽略。
    """
    h = _STATE["handle"]
    if h:
        try:
            u = ctypes.windll.user32
            u.CloseDesktop.argtypes = [wintypes.HANDLE]
            u.CloseDesktop.restype = wintypes.BOOL
            u.CloseDesktop(wintypes.HANDLE(h))
        except Exception:  # noqa: BLE001 —— 收尾失败不该把进程退出流程炸掉
            pass
    use_default()


# ── 起进程 ──────────────────────────────────────────────────────────────

def spawn(argv: list[str], env: dict[str, str] | None = None,
          on_desktop: str | None = None) -> dict[str, Any]:
    """
    起一个进程，**放在目标桌面上**（`isolated` 时是隐藏桌面，否则就是用户桌面）。

    返回 `{"ok", "pid", "desktop", "argv", "error"}`。

    ⚠️ **不能用 `subprocess.Popen(..., startupinfo=...)`**：`subprocess.STARTUPINFO`
    上**根本没有 `lpDesktop` 字段**（Python 的 `STARTUPINFO` 只映射了一部分字段）。
    往它上面设属性，Python 只是记到一个普通对象上，`CreateProcess` **从没看到它** ——
    于是"应用起在隐藏桌面上"变成了"起在用户桌面上"，而**一切返回值都是成功的**。
    本项目在 DEMO 阶段正因此被骗过一整轮（探针自报 `desktop='Default'` 才发现）。
    所以这里自己拼 `STARTUPINFOW`。

    ⚠️ 环境块要**自己拼**（`K=V\\0...\\0\\0`）而不是传 `None`：`env=None` 会让子进程
    继承本进程环境，而本项目有一条硬规则 —— 不该继承的东西必须主动剥
    （见 `core/session.py` 的 `env_for`）。调用方传进来的 env 已经是剥过的。

    ⚠️ **不重定向 stdout/stderr**：实测带重定向时 `CreateProcessW` 对本机
    Win11 的 Store 应用别名（`notepad.exe`）会**长时间不返回**（14 分钟以上，
    见 `tests/manual_windows_capture_story.py` 的 `spawn` 看门狗）；不带时正常。
    GUI 应用本来也看不到控制台输出，重定向没有收益。
    """
    if not argv:
        return {"ok": False, "pid": 0, "desktop": None, "argv": argv,
                "error": "argv 为空"}
    if on_desktop is None and is_isolated():
        # ⚠️ 隔离模式但桌面没建出来 → **拒绝**，绝不落到默认桌面（那就是用户桌面）。
        #    调用方（`WindowsBackend.spawn_app`）本就会先 `ensure_isolated()`，
        #    这里是防"有人绕过它直接调"的那一道。
        if not is_ready():
            return {"ok": False, "pid": 0, "desktop": None, "argv": argv,
                    "error": "隔离模式已生效但隔离桌面尚未就绪（请先 ensure_isolated）"}
        on_desktop = _STATE["name"]
    desktop = on_desktop

    k32 = ctypes.windll.kernel32
    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW), ctypes.POINTER(_PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = wintypes.BOOL

    si = _STARTUPINFOW()
    si.cb = ctypes.sizeof(_STARTUPINFOW)
    if desktop:
        si.lpDesktop = desktop
    pi = _PROCESS_INFORMATION()

    src_env = env if env is not None else dict(os.environ)
    envb = ctypes.create_unicode_buffer(
        "".join(f"{k}={v}\0" for k, v in src_env.items() if "=" not in k) + "\0")
    cmdline = subprocess.list2cmdline(argv)
    try:
        ok = bool(k32.CreateProcessW(
            None, cmdline, None, None, False, CREATE_UNICODE_ENVIRONMENT,
            ctypes.cast(envb, ctypes.c_void_p), None,
            ctypes.byref(si), ctypes.byref(pi)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "pid": 0, "desktop": desktop, "argv": argv,
                "error": f"{type(exc).__name__}: {exc}"}
    if not ok:
        return {"ok": False, "pid": 0, "desktop": desktop, "argv": argv,
                "error": f"CreateProcessW 失败 err={ctypes.get_last_error()}"}

    # 句柄不要了：进程已经跑起来，我们只要 pid。
    # ⚠️ 这两个句柄不关就是**句柄泄漏**（每次启动应用漏两个），而它不会表现为
    #    "启动失败"，只会让长会话越跑越慢 —— 属于最难查的那类。
    for h in (pi.hThread, pi.hProcess):
        try:
            k32.CloseHandle(wintypes.HANDLE(h))
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "pid": int(pi.dwProcessId), "desktop": desktop,
            "argv": argv, "error": ""}


# ── 枚举某张桌面上的窗口 ────────────────────────────────────────────────

def enum_windows(hdesk: int = 0, visible_only: bool = True) -> list[int]:
    """
    枚举**指定桌面**上的顶层窗；`hdesk=0` 表示本线程所在桌面。

    ⚠️ 与 `win32.enum_top_level_windows()` 的区别正是本模块存在的理由之一：
    后者走 `EnumWindows`，它**只看调用线程所在的那张桌面** —— 用户桌面上的进程
    用它永远看不到隐藏桌面上的应用，而且**不报任何错**（就是"一个窗口都没有"）。
    """
    out: list[int] = []
    u = ctypes.windll.user32
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):  # noqa: ANN001, ANN202
        if not visible_only or win32.is_window_visible(int(hwnd)):
            out.append(int(hwnd))
        return True

    u.EnumDesktopWindows.argtypes = [wintypes.HANDLE, CB, wintypes.LPARAM]
    u.EnumDesktopWindows.restype = wintypes.BOOL
    u.EnumDesktopWindows(wintypes.HANDLE(hdesk or 0), CB(_cb), 0)
    return out


def enumerate_target(visible_only: bool = True) -> list[int]:
    """枚举**当前目标桌面**上的顶层窗（隔离时是隐藏桌面，否则走原来的那条路）。

    ⚠️ 非隔离时**刻意仍走 `win32.enum_top_level_windows()`** 而不是
    `EnumDesktopWindows(0)`：后者不做"桌面窗口/外壳窗口"的过滤，行为与既有实现
    不完全一致；保持原路径能让 `real` 模式**一字不变**（那是用户明确选的老行为，
    不该被这次改动碰到）。
    """
    if is_isolated():
        # ⚠️ 桌面没建出来时返回**空**，而不是退回 `EnumWindows` —— 后者会列出
        #    **用户桌面的**窗口，那正是隔离要挡掉的东西（见 `is_isolated` 的说明）。
        if not is_ready():
            return []
        return enum_windows(target_handle(), visible_only=visible_only)
    return win32.enum_top_level_windows(visible_only=visible_only)
