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

⚠️ **一条诚实的保留**：探针这 3 个全新进程**一次都没卡过**，而 story 在同一时间段里
卡了 ~50%。「同样的调用序列」是**逐条**相同的，但不是**逐环境**相同的 —— 最大的结构性
差异是**首次碰 UIA 之前进程已经跑了多久**：探针 B 是进程起来 ~2 秒就碰，
story 要先跑完会话检查、用户桌面基线、三组启动对照、launch_app、4c~4e 那一整段（90 秒以上）。
**若哪天有人要继续查**，就从这里入手（把探针 B 改成"先空转 90 秒再碰 UIA"，
或把 story 的 4c~4e 整段挪到读树之后），而不是从头再查一遍。
在没有那个实验之前，**不要**声称"已排除调用序列"以外的更多结论。

═══ ⚠️ 2026-10-02 更新：上面这条"调用序列没问题"**被后续实测收窄了** ═══

`tests/run_iso_ab.bat`（2 轮 plain + 2 轮 prime 交替）跑出来的四轮里，
**三处卡点、两处在 import 期** —— 也就是说本探针量的「隐藏桌面 + 起应用 + 首次挂树」
**只是卡点之一，而且不是最先发生的那一个**：

    plain1  卡在 CoCreateInstance(CUIAutomation)          ← 本探针 B 量的正是这一步
    prime1  卡在模块级 platform.system()（WMI/COM 查询）   ← 在探针 B 那一步**之前**
    plain2  卡在 typing_extensions 的导入（pydantic_core）  ← 也在之前

所以本探针"B 只用 1.32s"的结论**只覆盖了它自己那一段**，不能推广成"story 的调用序列没问题"。
（唯一由这条调查推出的产品改动是平台判定改用 `sys.platform`，见 `core/session.py`；
那一条的理由独立成立：`platform.system()` 在 Windows 上是一次 WMI 查询，
全新进程实测 3.0~4.0 秒，而它在**模块级**。）

⚠️ 别把这条结论读成"以后再卡就别查了"：**再卡时先看栈**（story 现在有看门狗，
会直接告诉你卡在哪一次调用）。若哪天卡点变到**我们自己写的**那些行上，那就是另一回事了。

═══ ⚠️ 2026-10-03 更新：五阶段整批跑了一次，**卡在 D1（干净房间 + DPI V2）** ═══

`tests/run_uia_init_probe.bat` 五个全新进程，**前四个都跑完了**：

    A      干净房间                      首次 UIA **4.69s**（本机历史上 2.52s）
    B0     story 同序（无预热）           首次 ControlFromHandle **2.86s**
    B-idle 先空转 90s                    首次 ControlFromHandle **1.68s**
    B-work 先起杀进程 90s                 ❌ **本轮作废**（见下，`no-window`）
    D1     干净房间 + 先设 DPI V2         **卡住** —— 直到客体整个冻死

★ 两条结论，方向相反，都要记住：

1. **"进程活了多久"不是那个变量**：B-idle（空转 90s）1.68s，比不空转的 B0（2.86s）
   还快。原 docstring 那条"从进程存活时间入手"的线索**至此可以划掉**。
2. **卡点在"干净房间"里也出现过** —— D1 除了 `SetProcessDpiAwarenessContext` 什么都没干。
   这**不能推出"是 DPI V2 的锅"**：同一个 D1 在 2026-09-30 量到的是 **2.76s**，
   而这次整个客体随后完全冻死（见下）。**n=1，不写结论。**

⚠️ **B-work 那一轮是"没测到却看着像测过"**（已修）：它打出的
`RESULT|B.ControlFromHandle(hidden)|ok|0.38s|no-window` —— **0.38 秒这个数字是假的**，
`enumerate_target()` 当时返回 0，所以 `ControlFromHandle` 那一步**根本没执行**。
固定 `sleep(1.5)` 在慢机器上等不到窗口映射。现在改成**轮询等窗口 + 等不到就抛错**，
不再让"没测"伪装成"很快"。

⚠️ **卡死那次是"整台客体冻死"，不是我们的进程单独卡**（这是本轮最有用的运维事实）：
屏幕连续 3 分钟**逐字节相同**（`VBoxManage controlvm screenshotpng` 两次产物大小完全一样），
`VBoxManage guestcontrol run` 连**会话都起不来**（`Error starting guest session
(current status is: starting)`），探针的 `faulthandler` 连栈都写不出来（Z: 上的结果文件
停在卡死前那一行、mtime 不再变）。**判据是这三条一起出现**，任一命中就先去重启客体，
别再从代码里找答案。

跑法（客体上，交互式会话内；宿主侧用 `tests/run_windows_probe.sh` 驱动）：
    python tests\\manual_windows_uia_init_probe.py --only a
    python tests\\manual_windows_uia_init_probe.py --only b
"""

from __future__ import annotations

import faulthandler
import os
import pathlib
import subprocess
import sys
import threading
import time

_HERE = pathlib.Path(__file__).resolve()
_REPO = next((p for p in _HERE.parents if (p / "src" / "computer_use_mcp").is_dir()), None)
if _REPO is not None:
    sys.path.insert(0, str(_REPO / "src"))

# 结果直写文件、每行 flush —— 与 story 同一条理由：卡住时也要留下已经走过的证据。
_OUT = _HERE.parent.parent / "docs" / "PR" / "windows-driver" / "results" / \
    "_uia_init_probe.txt"

_SLOW = 5.0        # 超过这个秒数就标出来（**只是标注，不是判据**）

# 卡死自证：探针的每一步正常都是毫秒级，90s 没动就肯定不对了（详见 main() 里的说明）。
_HANG_DUMP_S = 90.0


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


def _wait_rows(desktop, timeout: float):  # noqa: ANN001, ANN202
    """
    轮询等目标桌面上出现窗口，返回那次枚举结果（可能为空 ⇒ 调用方**必须**当失败处理）。

    为什么不是固定 sleep：本客体会周期性整体变慢，`spawn` 返回 pid 只说明进程起来了，
    **窗口映射到桌面**还要更久。固定 1.5s 在慢的时候会等不到（实测踩到过）。
    """
    deadline = time.time() + timeout
    rows = []
    while time.time() < deadline:
        rows = desktop.enumerate_target()
        if rows:
            return rows
        time.sleep(0.5)
    return rows


def phase_b() -> None:
    """
    与 story **同序**：建隐藏桌面 → 在它上面起应用 → **本进程的首次 UIA 调用**
    就是 `ControlFromHandle` 那扇窗。

    ⚠️ 这里刻意走产品代码（`driver.desktop`）而不是自己调 `CreateDesktopW`：
    要复现的是**我们实际会走到的那条路径**，不是"一个同样语义的手写版本"。
    """
    say("--- 阶段 B：隐藏桌面 + 它上面的应用 + 首次 UIA（= story 的顺序）---")
    from computer_use_mcp.backend.windows.driver import desktop

    # ★ 碰 UIA **之前**记一次线程数：story 里那段 4b 会用 `_timed()` 起**可抛弃的
    #   守护线程**（超时就不 join 了，线程留在那儿跑完为止），所以"碰 UIA 时进程里有
    #   几个在跑的线程"是一个真实存在、且 story 与探针**不同**的变量。
    #   记下来是为了让"线程数"这个假设**可证伪**，而不是靠猜。
    say(f"B.threads_before_uia={threading.active_count()}")

    timed("B.ensure_isolated", lambda: desktop.ensure_isolated().get("name"))
    timed("B.spawn_winver", lambda: desktop.spawn(["winver.exe"], dict(os.environ)).get("pid"))
    # ⚠️ **必须轮询等窗口出现，不能固定 sleep 一个魔数**：2026-10-02 的实测里
    #    `--work-before 90` 那一轮的 `enumerate_target` 返回了 **0**，于是核心那条
    #    `ControlFromHandle` 走的是 "no-window" 分支、**根本没碰 UIA** —— 而它照样
    #    打出一行 `RESULT|…|ok|0.38s|no-window`。**0.38s 看着像一个测出来的数字**，
    #    其实什么都没测到。这正是本仓库最忌讳的那种判据：**通过与否取决于被测对象
    #    坏没坏**（这里更糟，是"没测"伪装成"很快"）。
    #    所以：等窗口 + 等不到就**报错**，绝不返回一个像测量结果的数。
    rows = _wait_rows(desktop, 20.0)
    timed("B.enumerate_target", lambda: len(rows))

    def _first_uia_on_hidden():  # noqa: ANN202
        if not rows:
            raise RuntimeError(
                f"隐藏桌面上等 {20.0:.0f}s 也没等到窗口 —— 本条**没测到任何东西**，"
                f"不要把它读成「很快」")
        import uiautomation
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


def _idle(seconds: float) -> None:
    """纯空转 —— 只抬高"进程已经活了多久"这一个变量，不做任何别的事。"""
    say(f"--- 预热 A：纯空转 {seconds:.0f}s（只抬高进程存活时间）---")
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(5.0)
        say(f"  idle {time.time() - t0:.0f}s threads={threading.active_count()}")
    say("预热 A 结束")


def _work(seconds: float) -> None:
    """
    做**与 story 同类**的事 ~N 秒（起/杀应用、枚举窗口），但**不碰 UIA**。

    这是在复制 story 与探针之间**唯一的、成规模的结构性差异**：story 在首次碰 UIA 之前
    已经跑了 90 秒以上的活（会话检查 → 桌面基线 → 三组启动对照 → launch_app → 4c~4e），
    而探针 B 是进程起来 ~2 秒就碰。

    ⚠️ 刻意**复刻 story 那个 `_timed()` 的"可抛弃守护线程"形状**（超时不 join、线程留着
    跑完）：那是进程内累积的状态，也正是"进程跑了多久"这个变量里最难用 sleep 模拟的部分。
    """
    say(f"--- 预热 B：做 story 同类的活 ~{seconds:.0f}s（不碰 UIA）---")
    from computer_use_mcp.backend.windows.driver import desktop

    desktop.ensure_isolated()
    t0 = time.time()
    rounds = 0
    while time.time() - t0 < seconds:
        rounds += 1
        _fire_and_forget(lambda: subprocess.Popen(["winver.exe"]))  # noqa: S603
        time.sleep(0.4)
        _fire_and_forget(lambda: subprocess.run(  # noqa: S603,S607
            ["taskkill", "/F", "/IM", "winver.exe"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False))
        _fire_and_forget(lambda: len(desktop.enumerate_target()))
    say(f"预热 B 结束：{rounds} 轮，threads={threading.active_count()}")


def _fire_and_forget(fn) -> None:  # noqa: ANN001
    """
    在**守护线程**里跑 `fn` 且**不等它** —— 复刻 story 里 `_timed()` 超时后的形状。

    ⚠️ 用 `daemon=True` 是为了不给进程退出添麻烦，但这**不能**让"线程还在跑"这件事消失：
    它照样在执行里，照样可能持着锁/句柄。这正是我们要不要证伪的东西。
    """
    threading.Thread(target=lambda: _swallow(fn), daemon=True).start()


def _swallow(fn) -> None:  # noqa: ANN001
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


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
    ap.add_argument("--idle-before", type=float, default=0.0,
                    help="碰 UIA 之前纯空转 N 秒（只抬高进程存活时间）")
    ap.add_argument("--work-before", type=float, default=0.0,
                    help="碰 UIA 之前做 story 同类的活 N 秒（起/杀应用、枚举窗口，不碰 UIA）")
    # ★ 单变量对照：story 在碰 UIA 之前会先 `_uia.ensure_dpi_awareness()` 把进程设成
    #   PER_MONITOR_AWARE_V2（产品代码，`WindowsBackend.__init__` 里就调），
    #   而探针 A/B 从没设过它 —— 这是"story 卡、探针不卡"之间**最纯粹的一个差异**。
    #   带上它 = 只改这一个变量。
    ap.add_argument("--set-dpi", action="store_true",
                    help="碰 UIA 之前先设 PER_MONITOR_AWARE_V2（复刻 story 的那一步）")
    args = ap.parse_args(argv)

    try:
        if args.only == "a":
            _OUT.unlink()
    except OSError:
        pass

    # ★ 卡死自证（与 story 同一条纪律：卡住时必须留下"卡在哪一次调用"这条事实）。
    #   本探针**不设看门狗退出** —— 它跑的次数少、且我们正想看它卡（卡住本身是数据）。
    #   但要有个比故事里短的阈值：探针的正常一步是毫秒级，90s 没动就肯定不对了。
    faulthandler.dump_traceback_later(_HANG_DUMP_S, repeat=True, exit=False)

    say(f"=== only={args.only} pid={os.getpid()} "
        f"idle_before={args.idle_before:.0f} work_before={args.work_before:.0f} ===")
    say(f"py={sys.version.split()[0]} exe={sys.executable}")
    say(f"coinit_flags={getattr(sys, 'coinit_flags', 'unset')!r} "
        f"（unset = 由 comtypes/第一个用 COM 的线程决定公寓）")
    say(f"threads_at_start={threading.active_count()}")

    # ⚠️ 预热必须在**碰 UIA 之前**，否则它就不是在抬高那个变量了。
    if args.set_dpi:
        from computer_use_mcp.backend.windows.driver import win32
        r = win32.set_dpi_awareness_v2()
        say(f"已设 DPI={r}（复刻 story 的 `_uia.ensure_dpi_awareness()`）")
    if args.idle_before > 0:
        _idle(args.idle_before)
    if args.work_before > 0:
        _work(args.work_before)

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
    say("带预热时 B 慢 -> '首次碰 UIA 之前进程已经跑了多久'就是那个变量")
    say("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
