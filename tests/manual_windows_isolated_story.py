# -*- coding: utf-8 -*-
"""「应用起在隔离桌面上」的真机 story（验收脚手架，跑在 Windows 客体上，不进 pytest）。

═══ 它验收什么 ═══

用户的原话是「能不能像我 LINUX 一样，**在虚拟屏中启动软件**」。这条故事把那条链路钉死：

  1. `launch_app` 启动的应用**落在隔离桌面上** —— 判据是**同一进程里的两次枚举**：
     `win32.enum_top_level_windows()`（= 用户桌面）**看不到**它，
     而 `apps.window_rows()`（= 目标桌面，隔离模式下是隐藏桌面）**看得到**。
     这一对判据是本故事的核心：只看前者会得到"没启动"，只看后者会得到"启动了"，
     **只有两条一起看**才证明它"启动了、但不在你的桌面上"。
  2. 用户的桌面**没被改动**：窗口数量不变、前台不变、指针不动。
  3. 起了之后**照样能操作**：读树 → 元素级写值 → 读回命中 → 截图。
  4. ★ **坐标级/键盘注入被拒绝，且用户的指针与前台真的一动不动**（4c 节）——
     这一条回答的是用户原话的后半句「**操作软件时会跟用户抢夺鼠标和焦点**」。
     判据取**异常类型** `GlobalInputRefusedError` 而不是"抛了异常"：隐藏桌面上
     SendInput 本身也会失败，两者都表现为"抛了异常"，只有认类型才分得开。
  5. 能力申报与会话级边界说明按实际策略给出（4d），且开关打开时判据**真的翻转**（4e）。
  6. `real` 模式下退回老行为（应用起在用户桌面）—— 反向正控，
     否则"隔离生效"与"枚举写死了"看起来一模一样。

⚠️ 本故事断言的核心判据里有一条与 **DPI 坐标系**有关：取指针基线必须在
设完 DPI 感知之后（见 `manual_windows_hidden_desktop_probe.py` 记录的那一课）。

═══ 验收状态（2026-09-30）═══

· **isolated 模式**：**全过**（`EXITCODE=0`）——
  应用起在隔离桌面上、用户桌面窗口数不变、指针与前台不动、
  读树 / 按窗口截图 / 元素级 Invoke 全部可用；4c/4d/4e 三节（默认不碰用户键鼠）也全过。
· **real 模式**（反向正控，`run_isolated_story_real.bat`）：
  曾在一次干净的客体状态下 **12/12 全过**（应用确实出现在用户桌面上）。
  之后重跑多轮**偶发卡住**（卡点每次不同：启动、读树都遇到过），
  与产品代码无关 —— 同一客体上三条启动路径会一起卡 20s+ 又突然都 <0.2s，
  是环境现象。**这条反向正控的结论按"验过一次"采信，不按"每轮都过"**。

═══ ⚠️ 本客体偶发卡死：卡点**已经不止一处**，根因仍未查明 ═══

2026-10-02 第一轮把它钉到了 `get_ui_tree` 里的
`uiautomation.instance() → comtypes → CoCreateInstance(CUIAutomation)`
（即 `ControlFromHandle(hwnd)` 里**本进程第一次碰 UIA** 的那一下）。
**2026-10-02 的 A/B 重复实验（`tests/run_iso_ab.bat`，2 轮 plain + 2 轮 prime）
把这个结论收窄了：那只是卡点之一，不是全部。**

四轮实测（客体时刻，同一进程序列、同一会话）：

    plain1  01:55-01:58  卡在 `CoCreateInstance(CUIAutomation)`（读树那一步）  ← 原来那个点
    prime1  01:58-02:01  卡在 **模块级 `platform.system()`**（`core/session.py`）← 新点，在 import 期
    plain2  02:01-02:05  卡在 **`typing_extensions` 的导入**（`pydantic_core` 链上）← 新点，也在 import 期
    prime2  02:05-02:06  ❌ **本轮无效（我们自己的锅，见下）**

⚠️ **prime2 那一轮是废的，而且是本仓库自己造成的**：那次运行期间对 `src/` 改了一半
（`backend/base.py` 写成 `sys.platform` 却没补 `import sys`），于是它当场
`NameError: name 'sys' is not defined` —— **判据的失败取决于被测对象坏没坏**，
正是本项目反复吃过的那类假红。静态守卫 `test_no_undefined_global_names_in_src` 抓住了它。
记在这里是为了下一个人别把这轮当成"prime 又失败了一次"的论据。

★ **由此得到一条更重要的结论：`--prime-uia` 那个问题至今没有答案。**
四轮里**没有任何一轮真正执行到"预热"那一步** —— prime1 与 plain2 都在 **import 期**
就卡住了（预热代码在 `main()` 里、import 之后），prime2 则是我自己弄坏的。
所以 2026-10-02 那条 n=1 线索（"预热那轮整条跑通了"）**既没被证实、也没被推翻**。
上一版这里写过"预热不解决任何事"——**那句话是错的，已删**。

★ **新观察（还没有定论）**：三处卡点里**有两处是"本进程第一次做 COM 激活"**
（UIA 的 `CoCreateInstance`、WMI 的 `_wmi_query`），第三处是共享盘上的导入。
这与"这台客体会周期性整体变慢"那条独立观察方向一致（裸 import 测到过 50s、
`Z:` 读比 `C:` 慢约 3 倍），但**样本太少，写成结论就是过度推断**。

**已排除**（同一台机器、同一时段、逐条对照出来的）：

  · **不是脚本放在共享文件夹的问题**：把 `src/`+`tests/` 拷到客体本地盘（`C:` 根下）
    再跑，照样卡在同一行。
  · **不是"后面的活把它搞坏的"**：`--tree-early`（把首次碰 UIA 提到 4b/2/3 之前）
    仍然卡 —— 所以原因在这三节**之前**就已经存在（后来的 import 期卡点印证了这一点）。
  · **不是单纯"进程活了很久"**：探针 `--idle-before 90` 是 2.47s。
  · **不是 story 式的起/杀进程**：探针 `--work-before 90` 到了 18s（明显变慢）**但没卡死**。
  · **不是单独设 DPI V2**：`--set-dpi` 的干净房间是 2.76s。

**已修（唯一一处由这条调查直接推出的产品改动，且理由独立成立）**：
`core/session.py` 与 `backend/base.py` 的平台判定从 `platform.system()` 改成 `sys.platform`。
前者在 Windows 上是一次 **WMI/COM 查询**，全新进程实测 **3.0~4.0 秒**，而它是**模块级**语句
（每个 import 到 `core.session` 的进程都要付，MCP server 冷启动也算）。
`sys.platform` 是编译期常量、不可能阻塞，且对本项目要回答的问题完全等价。

⚠️ 所以**再遇到卡住时不要先去翻代码**（那正是前几轮白耗掉的那些轮次）。本故事带
**看门狗**：150s 没有新输出就打印全部线程栈、写一条 FAIL、并带退出码 5 结束 ——
卡住这件事会**自己说出卡在哪一次调用**，宿主也拿得到明确结论而不是等满 900s。
先看那条栈：**卡点仍在我们自己的行上**才需要往下查。
（本轮四次卡死里，三次卡在**库代码**上、一次是我们自己半截编辑造成的 —— 没有一次
卡在本故事的业务逻辑上。）

⚠️ 另一条独立观察（同一台机器）：**这台客体会周期性地整体变慢** —— 裸 import 一个模块
测到过 50s、`Z:`（VirtualBox 共享文件夹）的读比 `C:` 慢约 3 倍、单次 append 到 `Z:` 约 37ms。
上面的"卡死"很可能只是这个分布的长尾，但**没有证据**，别写成结论。

⚠️ 目标应用刻意用 **winver**（纯 Win32），不用 Win11 的记事本 ——
后者是 Store 应用别名，实测在本客体上偶发卡在激活环节。

═══ 两个**排查用**开关（正常验收别带）═══

  · `--tree-early`：把「读树」提到 4b/2/3 之前 —— 用来判断"是不是后面那几节搞坏的"。
  · `--prime-uia`：读树前先 `GetRootControl()` 一次 —— 用来把"首次碰 UIA"与
    "碰的是哪一扇窗"劈成两种不同的可能。

跑法（客体上，交互式会话内）：
    python tests\\manual_windows_isolated_story.py --out results\\isolated_story.json
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_HERE = pathlib.Path(__file__).resolve()
_REPO = next((p for p in _HERE.parents if (p / "src" / "computer_use_mcp").is_dir()), None)
if _REPO is not None:
    sys.path.insert(0, str(_REPO / "src"))

_SPAWN_TIMEOUT = 25.0
_RESULTS: dict[str, dict] = {}

# 卡死多久之后打印全部线程栈（见 main() 里的 `faulthandler.dump_traceback_later`）。
# 取 90s：正常一步是毫秒级，而"慢但正常"的那几档（首次挂 UIA 树 4.3s、Edge 冷读 2.4s）
# 都远在它之下 —— 到点还没动，基本就是真卡住了。
_HANG_DUMP_S = 90.0

# 卡死多久之后**带着失败退出**（见 `_start_watchdog`）。
# ⚠️ 必须大于故事里所有合法的长等待，否则会把"正常但慢"误判成卡死：
#    最长的是 4b 那三个 20s 的启动对照与各处 `wait_for(..., 30.0)` —— 150s 有 5 倍余量。
_HANG_ABORT_S = 150.0

# 进度直写文件、每行 flush。**不能只靠 print**：上一轮真机跑的时候脚本卡在某一步，
# 而日志停在节标题上、一个字都没有 —— 分不清是卡在启动应用、还是窗口枚举。
# 诊断"没有任何输出就卡住"的问题，必须有一条不经过任何缓冲的通道。
_TRACE: pathlib.Path | None = None

# ★ 心跳：看门狗据此判断"还在推进"还是"卡住了"（见 `_start_watchdog`）。
#   任何一条输出（trace / ok / note）都算一次心跳 —— 判据是"**有没有新信息**"，
#   而不是"进程死没死"，因为后者在卡住时同样是"活着"。
_HEART: dict[str, float] = {"at": 0.0, "what": "启动"}


def _beat(what: str) -> None:
    _HEART["at"] = time.monotonic()
    _HEART["what"] = what


def trace(msg: str) -> None:
    _beat(f"trace: {msg}")
    print(f"     [trace] {msg}", flush=True)
    if _TRACE is not None:
        try:
            with open(_TRACE, "a", encoding="utf-8") as fh:
                fh.write(f"[trace] {msg}\n")
                fh.flush()
        except OSError:
            pass


def ok(label: str, cond: bool, detail: str = "") -> bool:
    _beat(f"ok: {label}")
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'OK  ' if cond else 'FAIL'} | {label}" + (f" | {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def note(label: str, detail: str = "") -> None:
    _beat(f"note: {label}")
    print(f"  ·  {label}" + (f": {detail}" if detail else ""), flush=True)


def _start_watchdog(limit: float) -> None:
    """
    ★ 卡死看门狗：超过 `limit` 秒没有新输出，就**打印全部线程栈并退出**。

    ═══ 为什么必须有它 ═══

    2026-09-30 实测：本故事会**偶发卡死**，一卡十分钟以上。而没有看门狗时，这一轮的代价是
    「宿主白等满 900s 超时 + 日志停在半截、一个字的原因都没有」—— 排查一次要花一整晚，
    而且**卡点每次都不同**（启动过、读树过），光看"停在哪一行"根本推不出结论。

    有了它，同样的卡死会变成：**3 分钟后一条 FAIL + 一份完整的线程栈**，
    直接指出卡在哪一次调用（实测定到 `uiautomation` 的惰性单例
    `CoCreateInstance(CUIAutomation)`）。省下的不是几秒钟，是一整轮排查。

    ⚠️ `limit` 必须**大于所有合法的长等待**，否则它会把"正常但慢"误判成卡死：
    本故事里最长的是 4b 那三个 20s 的启动对照，以及各处 `wait_for(..., 30.0)`。
    取 150s 给了 5 倍余量。

    ⚠️ 退出用 `os._exit` 而不是 `sys.exit`：卡住的线程可能正持着 GIL，
    正常退出路径（atexit / 线程 join）会**卡在退出本身**，让看门狗的初衷落空。
    `os._exit` 是立刻终结进程 —— 日志已经写进文件了，不需要优雅退出。

    ⚠️ 栈打印走 `faulthandler`，它**直接写 fd、不经过任何缓冲**：
    哪怕随后进程被强杀，这几行也已经落盘（这正是能拿到证据的原因）。
    """
    def _loop() -> None:
        while True:
            time.sleep(5.0)
            idle = time.monotonic() - _HEART["at"]
            if idle < limit:
                continue
            msg = (f"\n{'!' * 70}\n"
                   f"[watchdog] 卡死：已 {idle:.0f}s 没有任何新输出"
                   f"（上一条：{_HEART['what']}）。下面是全部线程栈。\n"
                   f"{'!' * 70}")
            print(msg, flush=True)
            if _TRACE is not None:
                try:
                    with open(_TRACE, "a", encoding="utf-8") as fh:
                        fh.write(msg + "\n")
                        fh.flush()
                except OSError:
                    pass
            faulthandler.dump_traceback()      # 全部线程，直写 fd
            _RESULTS["看门狗：卡死（见线程栈）"] = {
                "ok": False, "detail": f"{idle:.0f}s 无输出；上一条={_HEART['what']}"}
            # ⚠️ 退出码 5 = 有失败项（与 story 自身的约定一致），
            #    让宿主那侧的 `run_windows_probe.sh` 走"❌ 探针有失败项"分支而不是"超时"。
            os._exit(5)

    threading.Thread(target=_loop, name="cc-cu-watchdog", daemon=True).start()


def wait_for(pred, timeout: float, interval: float = 0.4):  # noqa: ANN001, ANN202
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(interval)
    return None


def kill(names: str) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/IM", names],  # noqa: S603,S607
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, timeout=15)
    except subprocess.TimeoutExpired:
        note("taskkill 超时", names)


def user_desktop_windows(**kw):  # noqa: ANN003
    """**用户桌面**上的顶层窗 —— 走 `EnumWindows`（只看本线程所在桌面）。

    ⚠️ 这里刻意**不用** `apps.window_rows()`：那个走的是"目标桌面"，
    隔离模式下是隐藏桌面 —— 用它就量不到"应用有没有跑到用户桌面上"。
    """
    from computer_use_mcp.backend.windows.driver import win32
    return win32.enum_top_level_windows(**kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    # ★ 实验开关：把「读树」（本客体上会卡死的那一步）提到 4b 那串 spawn 对照**之前**。
    #   它只做一件事——让**首次**碰 UIA 发生在 4b 的进程折腾之前。于是同一台机器、
    #   同一天、同一份代码跑两遍（带/不带这个开关）就能判定：
    #     带开关不卡、不带卡  → 是 4b 那串"起进程/杀进程"把 UIA 初始化搞坏的；
    #     两遍都卡            → 原因在更早的地方（会话检查/桌面基线/launch_app）。
    #   ⚠️ 判据要落在**首次**碰 UIA 上：先读一次会把单例预热，后面那次自然就好了
    #   （这正是本实验要利用的性质，不是缺陷）。
    ap.add_argument("--tree-early", action="store_true",
                    help="在 4b 之前先读一次树（判定卡死是否由 4b 的进程折腾引起）")
    # ★ 实验开关：在真正读树**之前**先用另一个 UIA 调用把单例建起来。
    #   它把"卡在 CoCreateInstance"这一个现象**劈成两种可能**：
    #     · 这里就卡 → 是「本进程首次碰 UIA」这件事本身（与碰的是哪扇窗无关）；
    #     · 这里不卡、随后读树卡 → 卡的是 `ControlFromHandle(那一扇窗)`，
    #       即**那个窗口的 provider** 有问题（那是完全不同的另一条线索）。
    ap.add_argument("--prime-uia", action="store_true",
                    help="读树前先用 GetRootControl() 预热 UIA 单例（劈开两种卡法）")
    args = ap.parse_args(argv)

    global _TRACE
    if args.out:
        _TRACE = pathlib.Path(args.out).with_name("_isolated_story_trace.log")
        try:
            _TRACE.write_text("", encoding="utf-8")
        except OSError:
            _TRACE = None

    # ★★ 卡死自证两件套（为什么必须有，见 `_start_watchdog` 的 docstring）：
    #    ① `dump_traceback_later` —— 90s 没推进就打印全部线程栈（**但不杀进程**），
    #       拿到"卡在哪一次调用"这条事实；
    #    ② 看门狗 —— 150s 没推进就打印栈并**带着退出码 5 结束**，
    #       让宿主拿到明确结论而不是白等满 900s。
    #    ⚠️ 两者都要：① 保证"哪怕进程随后被强杀也留下证据"，② 保证"这一轮不会白等"。
    faulthandler.dump_traceback_later(_HANG_DUMP_S, repeat=True, exit=False)
    _beat("武装看门狗")
    _start_watchdog(_HANG_ABORT_S)
    trace(f"已武装卡死自证：{_HANG_DUMP_S:.0f}s 打栈 / {_HANG_ABORT_S:.0f}s 带码退出")

    print("=== 0) 就绪 ===", flush=True)
    trace("起步")
    from computer_use_mcp.backend.base import Backend  # noqa: F401
    from computer_use_mcp.backend.windows.driver import (apps, desktop, session_info,
                                                         win32)
    from computer_use_mcp.core.coordinator import Coordinator

    si = session_info.check()
    if not ok("会话可信（有真桌面）", bool(si.get("interactive")),
              f"session={si.get('session_id')} 窗口站={si.get('window_station')}"):
        return 3

    # 坐标系先固定（跨阶段比较指针坐标才有意义）
    from computer_use_mcp.backend.windows.driver import _uia
    _uia.ensure_dpi_awareness()
    note("显示模式", f"CC_CU_DISPLAY_MODE={os.environ.get('CC_CU_DISPLAY_MODE')!r}"
                     f" → {desktop.display_mode()!r}")

    # 清场：杀掉可能还开着的目标应用。
    #   ⚠️ 目标应用刻意用 **winver**（纯 Win32）而不是记事本：Win11 的 `notepad.exe`
    #      是 **Store 应用别名**，启动要走 app-activation 机制 —— 实测它在本客体上
    #      **偶发卡住**（`CreateProcess`/`Popen` 三条路一起卡 20s 以上，
    #      见下面的诊断节）。同一个现象在 `manual_windows_capture_story.py` 里
    #      也遇到过（当时记成"重定向导致"，现在看**归因是错的**：
    #      不带重定向同样会卡，而是这个应用本身偶发卡）。
    #      仓库里其它探针（隐藏桌面、定向注入）用的都是 winver，一直很稳。
    kill("winver.exe")
    time.sleep(1.2)

    user_before = set(user_desktop_windows())
    cur0 = win32.get_cursor_pos() or (0, 0)
    fg0 = win32.foreground_window()
    title0 = win32.window_text(fg0) if fg0 else ""
    note("用户桌面基线", f"顶层窗={len(user_before)} 个，指针={cur0} 前台={title0!r}")

    # ── 1) 隔离桌面必须在**后端构造时**就已就绪 ──
    print("\n=== 1) 隔离桌面就绪（构造后端时就该建好）===", flush=True)
    from computer_use_mcp.backend.base import get_backend
    backend = get_backend()
    info = desktop.describe()
    note("桌面状态", json.dumps(info, ensure_ascii=False))
    _m = desktop.display_mode()
    ok("隔离桌面状态与会话模式一致（后端构造时就定下来，不是等第一次启动应用）",
       (bool(info.get("isolated")) == (_m == "isolated")),
       f"mode={_m!r} isolated={info.get('isolated')} desktop={info.get('desktop')!r}")

    # ★ 用户桌面上的窗口在隔离模式下**不该**被列出；real 模式下**就该**被列出
    #   —— 这一对是同一个判据的两个方向，只测一边等于没测。
    rows_target = apps.window_rows(limit=200)
    overlap = set(int(r["id"], 16) for r in rows_target) & user_before
    note("目标桌面上的窗口", f"{len(rows_target)} 个，与用户桌面交集 {len(overlap)} 个")
    if desktop.display_mode() == "isolated":
        ok("★ 目标桌面枚举**看不到用户桌面的窗口**（隔离模式）", not overlap,
           f"目标桌面 {len(rows_target)} 个，交集 {len(overlap)} 个"
           f"（隔离模式下应为 0：我们还没启动过任何应用）")
    else:
        ok("★ 目标桌面枚举**看得到用户桌面的窗口**（real 模式）",
           len(rows_target) > 0,
           f"目标桌面 {len(rows_target)} 个，交集 {len(overlap)} 个"
           f"（real 模式下\"目标桌面\"就是用户桌面，必须看得到）")

    # ── 4b) 诊断：`CreateProcessW` 不带桌面名时会不会卡？──
    # ⚠️ 这一节是**诚实记录一个尚未查明的现象**，不是验收判据：
    #   2026-09-30 实测：**同一个客体上，三条启动路径会一起卡**（`Popen` 带/不带
    #   重定向、`desktop.spawn` 带/不带桌面名，全部超过 20s 不返回），而换一轮跑
    #   又全部 <0.2s 返回。所以卡的原因**不在我们的代码**，而在客体当时的某种状态
    #   （嫌疑最大的是 Win11 `notepad.exe` 这个 **Store 应用别名**的激活机制；
    #   已把故事里的目标应用换成纯 Win32 的 `winver.exe`）。
    #   ⚠️ 早期版本文档里把它归因成"`Popen` 带 DEVNULL 重定向导致"——**那是错的**，
    #   同一轮里不带重定向的 `Popen` 也一样卡。
    #   产品侧照旧避开（`spawn_app` 在 real 模式下返回 None，走老路径），
    #   但这条**尚未查明的环境现象**必须留着，免得下次又按错误归因去改代码。
    print("\n=== 4b) 诊断：不带桌面名的 CreateProcessW 会不会卡 ===", flush=True)
    trace("诊断：准备 spawn 对照")

    def _timed(fn, budget: float) -> tuple[bool, float]:  # noqa: ANN001
        """跑 `fn`，最多等 `budget` 秒；返回 (是否按时返回, 耗时)。"""
        done: list[bool] = []
        t0 = time.time()

        def _run() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                note("对照实验抛异常", f"{type(exc).__name__}: {str(exc)[:80]}")
            done.append(True)

        th = threading.Thread(target=_run, daemon=True)
        th.start()
        th.join(budget)
        return bool(done), round(time.time() - t0, 2)

    desktop.use_default()
    ok_pop, dt_pop = _timed(
        lambda: subprocess.Popen(["winver.exe"],  # noqa: S603
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 start_new_session=True), 20.0)
    note("对照：subprocess.Popen（**带 DEVNULL 重定向**，= launch_app 走的那条）",
         f"按时返回={ok_pop} 耗时={dt_pop}s")
    kill("winver.exe")
    ok_pop2, dt_pop2 = _timed(
        lambda: subprocess.Popen(["winver.exe"]), 20.0)  # noqa: S603
    note("对照：subprocess.Popen（不带重定向）", f"按时返回={ok_pop2} 耗时={dt_pop2}s")
    kill("winver.exe")
    ok_ds, dt_ds = _timed(
        lambda: desktop.spawn(["winver.exe"], dict(os.environ)), 20.0)
    note("对照：desktop.spawn（不带桌面名）", f"按时返回={ok_ds} 耗时={dt_ds}s")
    _RESULTS["诊断:三条启动路径的时延（环境偶发卡，已记录、非判据）"] = {
        "ok": True,          # ★ 这条**不判成败** —— 它是记录，不是验收
        "detail": f"Popen+DEVNULL 返回={ok_pop}({dt_pop}s) / "
                  f"Popen 裸 返回={ok_pop2}({dt_pop2}s) / "
                  f"desktop.spawn 返回={ok_ds}({dt_ds}s)"
                  f"（三条**一起**卡 = 环境现象，不是某条 API 的问题）"
                  f"；产品侧 real 模式已避开这条路（spawn_app 返回 None）"}
    kill("winver.exe")

    # ── 2) launch_app 把应用放进隔离桌面 ──
    print("\n=== 2) launch_app 启动应用 ===", flush=True)
    coord = Coordinator(backend)
    trace("建好 Coordinator，准备 launch_app")
    try:
        res = coord.launch_app("winver.exe", settle=1.0)
        trace(f"launch_app 返回：{res}")
    except Exception as exc:  # noqa: BLE001
        ok("launch_app 成功", False, f"{type(exc).__name__}: {exc}")
        return 5
    ok("launch_app 成功", bool(res.get("pid")), json.dumps(res, ensure_ascii=False)[:200])
    note("launch_app 回报的落地位置", repr(res.get("display")))

    trace("等记事本出现在目标桌面上")
    rows = wait_for(lambda: [r for r in apps.window_rows(limit=200)
                             if r["app"] == "winver"], 25.0)
    trace(f"目标桌面上的记事本：{len(rows) if rows else 0} 个")
    if not ok("★ 目标桌面上**看得到**刚启动的记事本", bool(rows), f"{rows}"):
        return 5
    hwnd = int(rows[0]["id"], 16)
    note("目标窗口", f"{rows[0]['id']} {rows[0]['title']!r} "
                     f"{rows[0]['w']}x{rows[0]['h']}")

    # ── 3) ★ 核心：用户桌面**看不到**它 ──
    print("\n=== 3) ★ 用户桌面看不到它 ===", flush=True)
    trace("取用户桌面快照（对照）")
    user_after = set(user_desktop_windows())
    trace(f"用户桌面 {len(user_before)} → {len(user_after)}")
    # ★ 这一对判据**必须按模式分档**：同一个事实（应用在不在用户桌面上）
    #   在两种模式下**应当相反**。只断一个方向等于没测 —— 一个"永远查不到"
    #   的实现也能过隔离那一侧。
    if desktop.display_mode() == "isolated":
        ok("★ 用户桌面的窗口数量没变（应用没跑到你桌面上）",
           len(user_after) == len(user_before),
           f"{len(user_before)} → {len(user_after)}；新增 "
           f"{[hex(h) for h in (user_after - user_before)][:5]}")
        ok("★ 那扇窗**不在**用户桌面的枚举结果里", hwnd not in user_after,
           f"hwnd=0x{hwnd:x} 在用户桌面枚举中={hwnd in user_after}")
    else:
        ok("★ real 模式下那扇窗**就在**用户桌面的枚举结果里（反向正控）",
           hwnd in user_after,
           f"hwnd=0x{hwnd:x} 在用户桌面枚举中={hwnd in user_after}；"
           f"新增 {len(user_after - user_before)} 个窗口")

    cur1 = win32.get_cursor_pos() or (0, 0)
    fg1 = win32.foreground_window()
    if desktop.display_mode() == "isolated":
        ok("指针没动", tuple(cur0) == tuple(cur1), f"{tuple(cur0)} → {tuple(cur1)}")
        ok("前台没变", fg0 == fg1,
           f"{title0!r} → {win32.window_text(fg1) if fg1 else ''!r}")
    else:
        # real 模式下新应用**会**跳到你面前（那正是用户选 real 的代价，如实记下）
        note("real 模式下前台", f"{title0!r} → "
             f"{win32.window_text(fg1) if fg1 else ''!r}（可能被新窗口抢走，预期如此）")

    # ★ `--tree-early`：在这里把树读一次 —— 让**首次**碰 UIA 发生在这一段之前。
    #   4b 那串"起进程/杀进程"的对照跑在 section 2 之前（文件里在它上面），
    #   而这个位置在它**之后**、又在 4c~4e 之前，正好把两段进程折腾都隔在一边。
    #   拿它和默认跑法比：带开关不卡 = 卡死由之前那串进程折腾引起。
    if args.tree_early:
        print("\n=== 3.5) ★ 提前读树（首次碰 UIA 就发生在这里）===", flush=True)
        trace("提前读树：目标窗口 id")
        backend.active_window_id()
        trace("提前读树：list_windows")
        backend.list_windows(limit=50)
        trace("提前读树：get_ui_tree(active_window) ← 本进程首次碰 UIA")
        _early = coord.get_ui_tree(scope="active_window", max_nodes=200)
        trace(f"提前读树完成：{len(str(_early))} 字符")
        ok("★ 提前读树成功（首次碰 UIA 发生在 4c~4e 与 4b 之外）", bool(_early),
           f"{len(str(_early))} 字符")

    # ── 4c) ★ 坐标级/键盘注入被**拒绝**，而且指针真的没动 ──
    #
    # ⚠️ 这一节（以及 4d/4e）**刻意排在下一节的「读树」之前**：那一步在本客体上
    #    **偶发卡死**（实测连着 5 轮卡 3 轮，一卡十分钟以上；已用 `git stash -- src`
    #    做过 A/B，与产品代码无关）。排在前面的好处是：卡住时**本节结论已经打出来了**
    #    —— 判据要落在最不容易被环境吃掉的位置上。
    #
    # 这一节回答的是用户的另一半原话：「**操作软件时会跟用户抢夺鼠标和焦点**」。
    # 4) 那一节证明了"起得来、元素级能用"，但**没有**证明"坐标级不会打到用户身上" ——
    # 而在 Windows 上那是结构性风险：MCP server 进程自己跑在用户桌面上，
    # 它发出的 SendInput 只可能落到当前输入桌面 = 用户的桌面。
    #
    # ⚠️ 判据刻意取**异常类型** `GlobalInputRefusedError` 而不是"抛了异常"：
    #    在隐藏桌面上 SendInput 本身也会失败（error 5），于是"闸门拦住了"与
    #    "闸门没拦、只是注入失败"都表现为"抛了异常"，只有认类型才分得开。
    print("\n=== 4c) ★ 隔离模式下坐标级/键盘注入被拒绝 ===", flush=True)
    from computer_use_mcp.utils.errors import GlobalInputRefusedError

    cur_before_gate = win32.get_cursor_pos() or (0, 0)
    fg_before_gate = win32.foreground_window()

    # 每一个都会动用户键鼠的入口都要被拦住（**逐个**验，不抽样）
    _gated_calls = [
        ("click_xy 坐标点击", lambda: coord.click_xy(100, 100, preview=False)),
        ("double_click 连击", lambda: coord.click_xy(100, 100, clicks=2, preview=False)),
        ("scroll_xy 滚动", lambda: coord.scroll_xy("down", 3, x=100, y=100)),
        ("drag_xy 拖拽", lambda: coord.drag_xy(100, 100, 300, 300)),
        ("press_key 快捷键", lambda: coord.press_key("ctrl+s")),
        ("type_text 键盘注入（无 ref，走兜底）", lambda: coord.type_text("ccc-probe")),
        ("backend.click_at（绕过协调层的那条）", lambda: backend.click_at(100, 100)),
        ("backend.type_text（同上）", lambda: backend.type_text("ccc-probe")),
    ]
    refused: list[str] = []
    leaked: list[str] = []
    detail: list[str] = []
    for label, call in _gated_calls:
        try:
            call()
        except GlobalInputRefusedError as exc:
            refused.append(label)
            detail.append(f"{label}: {str(exc).splitlines()[0][:60]}")
        except Exception as exc:  # noqa: BLE001 —— 别的异常 = 闸门没拦住（或坏在别处）
            leaked.append(f"{label} → {type(exc).__name__}: {str(exc)[:80]}")
        else:
            leaked.append(f"{label} → **居然成功了**")
    ok("★ 八个会动用户键鼠的入口**全部**被闸门拒绝", not leaked,
       f"拦下 {len(refused)}/{len(_gated_calls)}；漏网={leaked}")

    _msg = ""
    try:
        coord.click_xy(100, 100, preview=False)
    except GlobalInputRefusedError as exc:
        _msg = str(exc)
    ok("★ 拒绝文案给出**出路**（元素级 / real 模式 / 显式放开三条）",
       ("元素级" in _msg) and ("CC_CU_ALLOW_GLOBAL_INPUT=1" in _msg)
       and ("CC_CU_DISPLAY_MODE=real" in _msg),
       _msg.splitlines()[0][:80] if _msg else "（没拿到文案）")

    # ★★ 这一节真正的判据：**用户的指针与前台一点没动**。
    #    前面那些调用若有一条真的发出去，指针必然已飞到 (100,100)。
    cur_after_gate = win32.get_cursor_pos() or (0, 0)
    fg_after_gate = win32.foreground_window()
    ok("★★ 连试八次之后，**用户的指针仍然一动不动**",
       tuple(cur_before_gate) == tuple(cur_after_gate),
       f"{tuple(cur_before_gate)} → {tuple(cur_after_gate)}")
    ok("★★ 用户的**前台窗口也没变**", fg_before_gate == fg_after_gate,
       f"0x{fg_before_gate:x} → 0x{fg_after_gate:x}")

    # ── 4d) 能力申报：按实际策略如实说 ──
    print("\n=== 4d) 能力申报与会话级边界说明 ===", flush=True)
    from computer_use_mcp.core import session as session_mod
    caps = session_mod.MANAGER.capabilities()
    ok("★ capabilities 申报 input_isolated=True（隔离模式下没有任何路径会碰用户的键鼠）",
       bool(caps.input_isolated), f"input_isolated={caps.input_isolated} caps={caps}")
    ok("★ 同一次申报里列出「做不到的事」（coordinate_input），不假装什么都能干",
       "coordinate_input" in caps.unimplemented, f"unimplemented={caps.unimplemented}")
    caveat = session_mod.MANAGER.startup_caveat() or ""
    ok("★ 会话级边界说明非空且给出路（它会被拼进 MCP instructions，模型开工前就读到）",
       ("元素级" in caveat) and ("ref" in caveat), caveat[:100])

    # ── 4e) 反向正控：开关打开时，判据**确实会翻转** ──
    #
    # ⚠️ 刻意**不去真的注入一次**：那一下会落在客体用户的真实桌面上（点到什么不可控）。
    #    所以反向正控只验到"判据翻转"这一层（provider 放行 + 提醒文案出现），
    #    不验"事件真的发出去了" —— 后者由 `manual_windows_directed_probe.py` 那批
    #    已在**受控坐标**上量过。
    print("\n=== 4e) 反向正控：CC_CU_ALLOW_GLOBAL_INPUT=1 时判据翻转 ===", flush=True)
    os.environ["CC_CU_ALLOW_GLOBAL_INPUT"] = "1"
    try:
        flipped = False
        try:
            session_mod.MANAGER.require("probe", kind=session_mod.OP_INJECT_GLOBAL)
            flipped = True
        except GlobalInputRefusedError:
            flipped = False
        ok("★ 打开开关后 provider **不再拒绝**（证明上面那批「拦住」不是恒定行为）",
           flipped, f"require 放行={flipped}")
        note_text = session_mod.MANAGER.global_input_note() or ""
        ok("★ 且会给出**响亮的提醒**（结果文案里要带这一句）",
           ("全局注入" in note_text) and ("ALLOW_GLOBAL_INPUT" in note_text)
           and ("键" in note_text), note_text[:100])
        caps_on = session_mod.MANAGER.capabilities()
        ok("★ 此时 capabilities 立刻改口申报 input_isolated=False（不缓存、不撒谎）",
           caps_on.input_isolated is False,
           f"input_isolated={caps_on.input_isolated} unimplemented={caps_on.unimplemented}")
    finally:
        os.environ.pop("CC_CU_ALLOW_GLOBAL_INPUT", None)

    # ── 4) 起了之后照样能操作 ──
    print("\n=== 4) 隔离桌面上的应用照样能操作 ===", flush=True)
    # ⚠️ 读树这一步在本客体上**偶发卡死**（实测一卡十分钟以上）。卡住时只知道"卡在
    #    读树里"是**不够**的 —— 它内部有好几步跨进程 UIA 调用。所以把子步骤也 trace 出来，
    #    配合 faulthandler 的栈快照，才能指到具体是哪一次调用。
    # ★ `--prime-uia`：读树之前先用**另一个** UIA 调用建单例（劈开两种卡法，见参数说明）。
    if args.prime_uia:
        trace("预热 UIA：import + GetRootControl()（本进程首次碰 UIA 就在这里）")
        _uia.import_uia().GetRootControl()
        trace("预热 UIA 完成 ✔")
        ok("★ 预热 UIA（GetRootControl）没卡", True, "首次碰 UIA 走的是根元素")

    trace("读树 1/3：目标窗口 id")
    _hwnd_hint = backend.active_window_id()
    trace(f"读树 1/3 返回 {_hwnd_hint!r}")
    trace("读树 2/3：list_windows")
    _rows = backend.list_windows(limit=50)
    trace(f"读树 2/3 返回 {len(_rows)} 行")
    trace("读树 3/3：get_ui_tree(active_window)")
    tree = coord.get_ui_tree(scope="active_window", max_nodes=200)
    trace(f"读树返回 {len(str(tree))} 字符")
    text = tree if isinstance(tree, str) else str(tree)
    ok("★ 读得到树（active_window 在隔离模式下指向我们启动的那个应用）",
       "[ref" in text or "push button" in text or "text" in text, text[:160])

    uia = _uia.import_uia()

    def walk(node, depth=0, limit=400):  # noqa: ANN001, ANN202
        if node is None or depth > 30 or limit <= 0:
            return
        yield node, depth
        try:
            kids = list(node.GetChildren())
        except Exception:  # noqa: BLE001
            return
        for ch in kids:
            yield from walk(ch, depth + 1, limit - 1)

    # ⚠️ 截图要排在**元素级 Invoke 之前**：Invoke 会把 winver 对话框关掉，
    #    之后再按同一个 hwnd 截图只会拿到"窗口无效"（实测踩到，白跑一轮）。
    data, meta = backend.screenshot_window(f"0x{hwnd:x}")
    ok("★ 按窗口截图拿得到（隔离桌面上 PrintWindow 可用）", bool(data),
       f"{len(data)} bytes capture={meta.get('capture')}")

    tok = f"ISO{os.getpid()}"
    ctrl = wait_for(lambda: uia.ControlFromHandle(hwnd), 30.0)
    btn = wait_for(lambda: next(
        (nd for nd, _d in walk(ctrl)
         if getattr(nd, "ControlType", None) == uia.ControlType.ButtonControl
         and (getattr(nd, "Name", "") or "").strip() in ("确定", "OK")), None), 30.0)
    invoked = False
    if btn is not None:
        try:
            btn.GetInvokePattern().Invoke()      # ★ 元素级：零坐标、不抢前台
            invoked = True
        except Exception as exc:  # noqa: BLE001
            # 动作生效后目标窗被销毁、手里的 COM 对象随之失效，于是这次调用"报错"。
            # **不能拿异常当失败判据**（真机抓到过反例：抛 COMError 而对话框确实关了）。
            note("Invoke 抛异常（可能是生效后对象失效）",
                 f"{type(exc).__name__}: {str(exc)[:80]}")
    gone = wait_for(lambda: not win32.is_window(hwnd), 10.0) is not None
    ok("★ 元素级 Invoke 生效（对话框真的关了，零坐标）", gone,
       f"找到按钮={btn is not None} Invoke 返回={invoked} 窗口已消失={gone}")

    # ── 5) ★ 反向正控：**同一个故事在 real 模式下必须给出相反的结论** ──
    #
    # ⚠️ 这一节刻意**不在进程内切模式**：显示模式是**会话级**决定（与 Linux 侧
    # `CC_CU_DISPLAY_MODE` 同一条约定，改它要重启 Claude Code），进程内切没有意义
    # —— 第一版就是这么写的，结果 `spawn_app` 按环境变量又重新建了隔离桌面，
    # 判据红得莫名其妙（而那不是 bug，是那段代码在测一件 API 不支持的事）。
    # 正确做法是**换个环境变量重跑一遍本故事**（`run_isolated_story_real.bat`）。
    print("\n=== 5) 模式自检（反向正控靠换环境变量重跑）===", flush=True)
    mode = desktop.display_mode()
    if mode == "real":
        # ⚠️ 这里**重新起一个**应用来验，而不是去找上面那个 ——
        #    第 4 节已经把它的对话框 Invoke 关掉了（那是元素级操作生效的证据），
        #    再按旧 hwnd 找只会得到空（实测踩到，白跑一轮）。
        subprocess.Popen(["winver.exe"])                     # noqa: S603
        got_user = wait_for(
            lambda: [r for r in apps.window_rows(limit=200)
                     if r["app"] == "winver"
                     and int(r["id"], 16) in user_desktop_windows()], 25.0)
        ok("★ real 模式下应用**确实出现在用户桌面上**（证明隔离那侧的判据不是恒真）",
           bool(got_user),
           f"用户桌面上的 winver 窗={[r['id'] for r in (got_user or [])][:5]}")
        ok("real 模式下目标桌面==用户桌面（枚举口径没写死）",
           not desktop.is_isolated(), f"isolated={desktop.is_isolated()}")

        # ★ real 模式下全局注入**不该**被拒绝（否则 4c 那批"拦住"就成了一条恒真的判据，
        #   分不清是"闸门按策略工作"还是"闸门一律拒绝"）。
        from computer_use_mcp.core import session as session_mod
        from computer_use_mcp.utils.errors import GlobalInputRefusedError as _GIR
        allowed = False
        try:
            session_mod.MANAGER.require("probe", kind=session_mod.OP_INJECT_GLOBAL)
            allowed = True
        except _GIR:
            allowed = False
        ok("★ real 模式下 provider **放行**全局注入（反向正控：闸门按策略分档，不是恒拒）",
           allowed, f"require 放行={allowed}")
        caps_real = session_mod.MANAGER.capabilities()
        ok("real 模式下 capabilities 如实申报 target=real 且 input_isolated=False",
           caps_real.target == "real" and caps_real.input_isolated is False,
           f"target={caps_real.target} input_isolated={caps_real.input_isolated}")
        ok("real 模式下没有会话级边界说明（用户自己选的老行为，不该天天提醒）",
           session_mod.MANAGER.startup_caveat() is None,
           repr(session_mod.MANAGER.startup_caveat()))
    else:
        note("本次跑的是 isolated 模式",
             "real 那一侧请用 run_isolated_story_real.bat 再跑一遍")

    # ── 6) 收尾 ──
    print("\n=== 6) 收尾 ===", flush=True)
    # 撤掉卡死自证：故事跑完了还让它继续每 90s 打一份栈，只会把日志淹掉。
    faulthandler.cancel_dump_traceback_later()
    kill("winver.exe")
    desktop.release()

    failed = [k for k, v in _RESULTS.items() if not v["ok"]]
    print(f"\n[story] 判据 {len(_RESULTS)} 条，失败 {len(failed)} 条", flush=True)
    for k in failed:
        print(f"  ❌ {k} — {_RESULTS[k]['detail']}", flush=True)
    if args.out:
        p = pathlib.Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"results": _RESULTS, "failed": failed,
                                 "desktop": info}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"  报告已写入 {p}", flush=True)
    return 0 if not failed else 5


if __name__ == "__main__":
    raise SystemExit(main())
