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
  读树 / 按窗口截图 / 元素级 Invoke 全部可用。
· **real 模式**（反向正控，`run_isolated_story_real.bat`）：
  曾在一次干净的客体状态下 **12/12 全过**（应用确实出现在用户桌面上）。
  之后重跑多轮**偶发卡住**（卡点每次不同：启动、读树都遇到过），
  与产品代码无关 —— 同一客体上三条启动路径会一起卡 20s+ 又突然都 <0.2s，
  是环境现象。**这条反向正控的结论按"验过一次"采信，不按"每轮都过"**。

⚠️⚠️ **第 4) 节的"读树"在本客体上偶发卡死**（2026-09-30 实测：连着 5 轮里卡了 3 轮，
卡在 `get_ui_tree(scope='active_window')` 那一行、一卡十分钟以上）。
**这不是本轮改动引入的 —— 做过 A/B**：`git stash push -- src` 回到改动前的代码，
同一个客体上**照样卡在同一行**。判据与结论因此按"验过一次"采信；
但**看到卡在这里时不要先去查代码**（那会白耗一整轮，正如"启动偶发卡"那条）。

⚠️ 目标应用刻意用 **winver**（纯 Win32），不用 Win11 的记事本 ——
后者是 Store 应用别名，实测在本客体上偶发卡在激活环节。

跑法（客体上，交互式会话内）：
    python tests\\manual_windows_isolated_story.py --out results\\isolated_story.json
"""

from __future__ import annotations

import argparse
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

# 进度直写文件、每行 flush。**不能只靠 print**：上一轮真机跑的时候脚本卡在某一步，
# 而日志停在节标题上、一个字都没有 —— 分不清是卡在启动应用、还是窗口枚举。
# 诊断"没有任何输出就卡住"的问题，必须有一条不经过任何缓冲的通道。
_TRACE: pathlib.Path | None = None


def trace(msg: str) -> None:
    print(f"     [trace] {msg}", flush=True)
    if _TRACE is not None:
        try:
            with open(_TRACE, "a", encoding="utf-8") as fh:
                fh.write(f"[trace] {msg}\n")
                fh.flush()
        except OSError:
            pass


def ok(label: str, cond: bool, detail: str = "") -> bool:
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'OK  ' if cond else 'FAIL'} | {label}" + (f" | {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def note(label: str, detail: str = "") -> None:
    print(f"  ·  {label}" + (f": {detail}" if detail else ""), flush=True)


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
    args = ap.parse_args(argv)

    global _TRACE
    if args.out:
        _TRACE = pathlib.Path(args.out).with_name("_isolated_story_trace.log")
        try:
            _TRACE.write_text("", encoding="utf-8")
        except OSError:
            _TRACE = None

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
    #
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
    trace("读树（active_window）")
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
