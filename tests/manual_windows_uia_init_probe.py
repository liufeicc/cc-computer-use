# -*- coding: utf-8 -*-
"""「一次性阻塞操作在客体上到底多慢」的对照探针（跑在 Windows 客体上，不进 pytest）。

═══ 它回答什么 ═══

真机 story 偶发卡死（一卡十分钟以上）。`faulthandler` 抓到的栈是
**`uiautomation` 的惰性单例初始化 → `comtypes` → `CoCreateInstance(CUIAutomation)`**；
同一轮里 `taskkill` 也卡过 90 秒以上。

两种解释的处置**完全相反**：

  A) **客体环境病态**（虚拟化 + 杀软 + vboxsf 让一次性阻塞操作偶尔变得极慢）
     → 不是我们的 bug，如实记录即可；
  B) **我们自己的调用序列**（先建隐藏桌面、再往上面起应用，之后再初始化 UIA）
     → 那要改的是产品代码。

判据设计（**每个阶段各起一个全新进程**，而不是在一个进程里按顺序跑）：

  · `--only=b` —— 与 story 同序：建隐藏桌面 → 在它上面起应用 → **本进程的首次 UIA 调用
    就是 `ControlFromHandle` 那扇窗**。这一步慢 = 我们的调用序列有问题。
  · `--only=a` —— 干净房间：进程刚起来，除了 import 什么都没干，量普通子进程调用
    与本进程的首次 UIA 调用。这一步也慢 = 客体本身病态。

  ⚠️⚠️ **两阶段必须各起一个进程**（第一版把它们串在同一个进程里，于是 a 先把 UIA
  单例预热了、b 量的根本不是"首次初始化"—— **探针本身把待测的那一步消掉了**）。
  这是这类对照最容易踩的坑：验证"首次调用慢不慢"，就得保证它真的是首次。
  · `--only=c` —— 收尾复量（同一进程内再量一次，看有没有"越跑越糟"）。

⚠️ **只量、不断言**：本探针不判成败，输出是给人看的数字 + 一段判读提示。
   把"多少秒算病态"写死成阈值，只会在换机器时变成一个假警报源。
⚠️ 输出**只走 ASCII 并落文件**：中文经 guestcontrol 的管道回来看必是乱码。

═══ 实测结论（2026-09-30，本客体）═══

    A（干净房间）      subprocess 0.26s / 0.13s，**首次 UIA 初始化 + 读根 2.52s**
    B（story 同序）    建桌面 0.01s、起应用 0.04s、**首次 ControlFromHandle 1.32s**
    C（复用）          subprocess 0.21s、读根 2.59s

**结论：调用序列本身没问题** —— story 那条路在这里 1.32 秒就跑完了。

所以 story 偶发卡死（栈停在 `CoCreateInstance`，一卡十分钟以上）**是客体当时的状态**，
不是我们的代码。旁证有两条，都在**同一轮 story 里**：`taskkill` 卡过 90s 以上、
早期版本里 `CreateProcessW` 卡过 20s 以上 —— 而同一台客体在探针里做同样的事只要 0.2s。
要复现得等它自己再犯，不能靠改代码去"修"。

⚠️ 别把这条结论读成"以后再卡就别查了"：**再卡时先看栈**（story 现在有看门狗，
会直接告诉你卡在哪一次调用）。若哪天卡点变到**我们自己写的**那些行上，那就是另一回事了。

跑法（客体上，交互式会话内；宿主侧用 `tests/run_windows_probe.sh` 驱动）：
    python tests\\manual_windows_uia_init_probe.py --only a
    python tests\\manual_windows_uia_init_probe.py --only b
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve()
_REPO = next((p for p in _HERE.parents if (p / "src" / "computer_use_mcp").is_dir()), None)
if _REPO is not None:
    sys.path.insert(0, str(_REPO / "src"))

# 结果直写文件、每行 flush —— 与 story 同一条理由：卡住时也要留下已经走过的证据。
_OUT = _HERE.parent.parent / "docs" / "PR" / "windows-driver" / "results" / \
    "_uia_init_probe.txt"

_SLOW = 5.0        # 超过这个秒数就标出来（**只是标注，不是判据**）


def say(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(_OUT, "a", encoding="ascii", errors="replace") as fh:
            fh.write(line + "\n")
            fh.flush()
    except OSError:
        pass


def timed(label: str, fn):  # noqa: ANN001, ANN201
    t0 = time.time()
    try:
        out = fn()
        dt = time.time() - t0
        say(f"RESULT|{label}|{'SLOW' if dt >= _SLOW else 'ok'}|{dt:.2f}s|{out}")
    except Exception as exc:  # noqa: BLE001
        dt = time.time() - t0
        say(f"RESULT|{label}|ERR|{dt:.2f}s|{type(exc).__name__}: {str(exc)[:120]}")
    return


def _spawn_cost() -> int:
    """一次普通子进程往返 —— 与 story 里 `kill()` 走的是同一条路。"""
    return int(subprocess.run(["cmd", "/c", "ver"], capture_output=True,  # noqa: S603
                              timeout=120).returncode)


def _uia_root() -> str:
    """首次碰 UIA 的真实路径之一（内部就会建那个惰性单例）。"""
    import uiautomation
    return uiautomation.GetRootControl().ControlTypeName


def phase_a() -> None:
    """干净房间：除了 import 什么都没干，首次 UIA 调用就在这里。"""
    say("--- 阶段 A：干净房间（本进程首次 UIA 就在这里）---")
    timed("A.subprocess#1", _spawn_cost)
    timed("A.subprocess#2", _spawn_cost)
    timed("A.uia_init+root", _uia_root)


def phase_b() -> None:
    """
    与 story **同序**：建隐藏桌面 → 在它上面起应用 → **本进程的首次 UIA 调用**
    就是 `ControlFromHandle` 那扇窗。

    ⚠️ 这里刻意走产品代码（`driver.desktop`）而不是自己调 `CreateDesktopW`：
    要复现的是**我们实际会走到的那条路径**，不是"一个同样语义的手写版本"。
    """
    say("--- 阶段 B：隐藏桌面 + 它上面的应用 + 首次 UIA（= story 的顺序）---")
    from computer_use_mcp.backend.windows.driver import desktop

    timed("B.ensure_isolated", lambda: desktop.ensure_isolated().get("name"))
    timed("B.spawn_winver", lambda: desktop.spawn(["winver.exe"], dict(os.environ)).get("pid"))
    time.sleep(1.5)
    timed("B.enumerate_target", lambda: len(desktop.enumerate_target()))

    def _first_uia_on_hidden():  # noqa: ANN202
        import uiautomation
        rows = desktop.enumerate_target()
        if not rows:
            return "no-window"
        return type(uiautomation.ControlFromHandle(int(rows[0]))).__name__
    # ★ 这一条是**整个探针的核心**：它慢就说明我们的调用序列有问题。
    timed("B.ControlFromHandle(hidden)：含惰性单例初始化", _first_uia_on_hidden)

    try:
        import subprocess as _sp
        _sp.run(["taskkill", "/F", "/IM", "winver.exe"],  # noqa: S603,S607
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, check=False, timeout=60)
    except Exception:  # noqa: BLE001
        pass
    desktop.release()


def phase_c() -> None:
    """同一进程内再量一次：看有没有"越跑越糟"（UIA 已预热，这一条量的是复用量级）。"""
    say("--- 阶段 C：同进程内复量（有没有越跑越糟）---")
    timed("C.subprocess", _spawn_cost)
    timed("C.uia_root", _uia_root)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("a", "b", "c", "all"), default="all",
                    help="只跑某一阶段（a/b 建议各起一个进程，见模块 docstring）")
    args = ap.parse_args(argv)

    try:
        if args.only == "a":
            _OUT.unlink()
    except OSError:
        pass

    say(f"=== only={args.only} pid={os.getpid()} ===")
    say(f"py={sys.version.split()[0]} exe={sys.executable}")
    say(f"coinit_flags={getattr(sys, 'coinit_flags', 'unset')!r} "
        f"（unset = 由 comtypes/第一个用 COM 的线程决定公寓）")

    if args.only in ("a", "all"):
        phase_a()
    if args.only in ("b", "all"):
        phase_b()
    if args.only in ("c", "all"):
        phase_c()

    say("--- 判读 ---")
    say("A 就慢        -> 客体环境病态（与我们的调用序列无关）")
    say("只有 B 慢     -> 我们的调用序列（隐藏桌面/上面的应用）与 UIA 初始化有交互")
    say("C 比 A 慢很多 -> 越跑越糟（最值得改的一种）")
    say("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
