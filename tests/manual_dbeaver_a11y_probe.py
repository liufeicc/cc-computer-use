"""
P0-a 探针：DBeaver 的无障碍树到底能不能安全读？（**只读**，不改变被测应用状态）

═══ 为什么要做这个（2026-09-15 实测翻案）═══
项目一直假设「DBeaver 的树不是不存在，而是**一被查询就崩**」，依据是
hs_err_pid338490.log：libatspi → atk-bridge → libswt-pi3-gtk →
gtk_widget_is_sensitive → SIGSEGV。但复查本仓库四次 hs_err 后发现，
其中**三次的崩溃点完全相同、且都不经过 a11y**：

  libc.so.6+0x4798b   —— 反汇编显示是 __run_exit_handlers 在遍历退出回调表
  （`mov 0x10(%rax),%rcx` 比较 flavor 2/3/4，调用方是 `exit+0x1e`），
  si_addr = 0x0，即**进程已经在退出**、在清理阶段摔倒。

那三次的存活时长分别为 20m20s / 9m50s / 22m8s，且每次都对得上「沙箱 Xephyr
先退出」（本次实测：Xephyr 52176 以 rc=1 退出 → DBeaver 01:34:24 崩于 exit）。
**这是「失去 X 连接后退出」的副产物，不是查询把它打崩的。**

真正经 a11y 路径的只有 9/11 那一次（存活仅 22s）。

所以「树一读就崩」这个结论**证据不足**，必须实测。本探针就是这件事：
如果树其实能安全读，DBeaver 就能回到「零坐标元素级操作」——既不需要截图、
也不需要 OCR、更不需要算坐标，这才是能让人机同速的路。

═══ 安全约定（逐条落实 CLAUDE.md 的预算/熔断要求）═══
  - **只针对 DBeaver 一个应用**，绝不做全桌面 a11y 枚举（那是 GNOME Shell
    崩溃事故的根因）。
  - **纯只读**：只调 get_*，绝不调 do_action / set_value，不改变被测应用状态。
  - 查询**按危险性递增**排序，每发一种立刻检查 DBeaver 是否存活；
    一旦死亡立刻停止，并报告**是哪种查询**要了它的命。
  - 定位应用走「按 pid 只读 get_process_id」的轻量路径（项目既有安全模式），
    不对每个应用调 child_count。
  - 每步之间顺带检查 at-spi2-registryd 是否存活（防止连带打崩会话总线）。

用法：
  PYTHONNOUSERSITE=1 "$PY" tests/manual_dbeaver_a11y_probe.py <dbeaver_pid>
退出码 0 = 全部查询通过（树可安全读）；1 = 某步把 DBeaver 打死了。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

# ⚠️ 必须在任何 import gi 之前：conda 自带 GLib 但没有 Atspi typelib，
# 需由 _bootstrap 把 GI_TYPELIB_PATH 指向系统目录
import computer_use_mcp._bootstrap  # noqa: F401  （导入即产生副作用，勿删）

# M-49：`_ok` 是 banner 与退出码的唯一依据，**必须真的会被置位**。
# 它此前只读不写（`global _ok` 声明了却没有任何一处赋值），于是末行恒打印
# 「✅ 全部通过」。当前所有失败路径都提前 `return 1`、走到末行时结论恰好是对的
# （所以这不是活 bug），但只要将来加一条**非致命**的检查，banner 立刻开始说谎 ——
# 而「假绿比红更危险」。故用一个真正置位的 helper 把它变成结构性正确。
_ok = True


def log(msg: str) -> None:
    print(msg, flush=True)


def _fail() -> None:
    """记一次失败（M-49）：banner 与退出码都读 `_ok`，让它真的反映实情。"""
    global _ok
    _ok = False


def alive(pid: int) -> bool:
    """目标进程是否存活（signal 0 只做权限/存在性检查，不真发信号）。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def registry_alive() -> bool:
    """at-spi2-registryd 是否还在（它是全桌面 a11y 的单点，崩了会连累 GNOME Shell）。"""
    p = subprocess.run(["pgrep", "-x", "at-spi2-registr"], capture_output=True, text=True)
    return bool(p.stdout.strip())


class Probe:
    """按危险性递增，对**单个**应用逐项发只读查询，每步做存活检查。"""

    def __init__(self, reader, pid: int) -> None:
        self.r = reader
        self.pid = pid
        self.step_no = 0

    def step(self, desc: str, fn):
        """
        发一次查询并立刻判断后果。

        返回 (是否该继续, 结果)。DBeaver 或 registryd 任一死亡 → 停止整个探针。
        """
        self.step_no += 1
        t0 = time.monotonic()
        err = None
        val = None
        try:
            val = fn()
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        dt = (time.monotonic() - t0) * 1000
        shown = f"{val!r}" if err is None else f"异常 {err}"
        if len(shown) > 120:
            shown = shown[:117] + "…"
        log(f"  [{self.step_no:2d}] {desc:<38} {dt:7.0f}ms  → {shown}")

        if not alive(self.pid):
            log(f"  💥 DBeaver(pid={self.pid}) 在这一步之后死亡 → **凶手就是：{desc}**")
            _fail()
            return False, val
        if not registry_alive():
            log(f"  💥 at-spi2-registryd 在这一步之后消失 → 停止（避免连累桌面）")
            _fail()
            return False, val
        return True, val


def main() -> int:
    global _ok
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        log("用法: manual_dbeaver_a11y_probe.py <dbeaver_pid>")
        return 2
    pid = int(sys.argv[1])
    if not alive(pid):
        log(f" pid={pid} 不存在")
        return 2
    # 安全闸：只允许对 DBeaver 下手，杜绝误传 pid 去戳别的应用。
    # 判据看 comm+args 里是否出现 dbeaver：launch_app 返回的是 launcher 脚本
    # （comm='dbeaver'），而 a11y 节点属于它的子进程 java（comm='java'，
    # args 里含 /usr/share/dbeaver-ce/jre/bin/java）——两者都要放行。
    comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()
    cmdline = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                             capture_output=True, text=True).stdout
    if "dbeaver" not in f"{comm} {cmdline}".lower():
        log(f"❌ pid={pid} 看起来不是 DBeaver（comm={comm!r}），拒绝执行")
        return 2

    log(f"目标: DBeaver pid={pid}")
    log(f"DISPLAY={os.environ.get('DISPLAY')}  registryd存活={registry_alive()}")
    log(f"⚠️ 本探针只读、只针对这一个应用，不做全桌面枚举\n")

    from computer_use_mcp.backend.linux.atspi import AtspiReader

    reader = AtspiReader()
    if not reader.is_available():
        # M-48：`error` 是 @property（atspi.py），**不能当方法调**——
        # 调了会抛 TypeError: 'str' object is not callable，那句本该说清楚
        # 「AT-SPI 为什么不可用」的提示**永远打不出来**，只剩一个误导性的堆栈。
        log(f"❌ AT-SPI 不可用: {reader.error}")
        return 2
    p = Probe(reader, pid)

    # ── 第 0 组：找到 DBeaver 的 application 节点（只读 get_process_id，轻量）──
    log("① 按 pid 定位应用（只读 get_process_id，不碰 child_count）")
    app = None
    ok, apps = p.step("desktop() 取根节点", reader.desktop)
    if not ok:
        return 1

    def _locate():
        nonlocal app
        n = 0
        for a in reader.iter_apps():
            n += 1
            if reader.get_process_id(a) == pid:
                app = a
                return f"命中（扫描了 {n} 个应用）"
        return f"未命中（扫描了 {n} 个应用）"

    ok, _ = p.step("iter_apps() + 逐个只读 pid", _locate)
    if not ok:
        return 1
    if app is None:
        log("❌ 没找到 DBeaver 的 application 节点（AT-SPI 里根本没有它）")
        return 1

    # ── 第 1 组：纯属性读（CLAUDE.md 明确「不触发建树」，应当绝对安全）──
    log("\n② 纯属性读（预期安全：不触发建树）")
    for name, fn in [
        ("get_name(app)", lambda: reader.get_name(app)),
        ("role_name(app)", lambda: reader.role_name(app)),
        ("get_process_id(app)", lambda: reader.get_process_id(app)),
        ("get_states(app)", lambda: reader.get_states(app)),
    ]:
        ok, _ = p.step(name, fn)
        if not ok:
            return 1

    # ── 第 2 组：会**逼目标应用构建整棵 a11y 树**的调用（头号嫌疑人）──
    log("\n③ 触发建树的调用（头号嫌疑人：child_count 会逼 DBeaver 惰性建树）")
    count = 0

    def _child_count():
        nonlocal count
        count = reader.child_count(app)
        return f"child_count={count}"

    ok, _ = p.step("child_count(app)", _child_count)
    if not ok:
        return 1

    for i in range(min(3, max(0, count))):
        ok, _ = p.step(f"child_at(app, {i}) → get_name",
                       lambda i=i: reader.get_name(reader.child_at(app, i)))
        if not ok:
            return 1

    # ── 第 3 组：窗口级遍历（真正干活时会走的路径）──
    log("\n④ 窗口级遍历（实用路径：找顶层窗口并读它的子树）")
    wins = []

    def _top_windows():
        nonlocal wins
        for w in range(reader.child_count(app)):
            node = reader.child_at(app, w)
            if node is not None:
                wins.append(node)
        return f"顶层窗口 {len(wins)} 个"

    ok, _ = p.step("枚举 application 的顶层窗口", _top_windows)
    if not ok:
        return 1

    if wins:
        win = wins[0]
        for name, fn in [
            ("get_name(win)", lambda: reader.get_name(win)),
            ("role_name(win)", lambda: reader.role_name(win)),
            ("get_extents(win, SCREEN)", lambda: reader.get_extents(win, "SCREEN")),
            ("child_count(win)", lambda: reader.child_count(win)),
            ("get_actions(win)", lambda: reader.get_actions(win)),
        ]:
            ok, _ = p.step(name, fn)
            if not ok:
                return 1

    # ─ 第 4 组：项目真正会用的入口（build_tree），这是「能不能用」的最终判据 ──
    log("\n⑤ 项目真实入口：build_tree（能过这关，DBeaver 就能走元素级操作）")
    tree_holder = {}

    def _build():
        nodes = reader.build_tree(scope="active_window", app=None, max_depth=6, max_nodes=200)
        tree_holder["n"] = len(nodes)
        named = [n for n in nodes if n.name]
        return f"节点 {len(nodes)} 个，其中有名字的 {len(named)} 个"

    ok, _ = p.step("build_tree(scope='active_window')", _build)
    if not ok:
        return 1

    log(f"\n{'✅ 全部通过：DBeaver 的 a11y 树**可以安全读**，无需 OCR/截图兜底' if _ok else ' 有失败项'}")
    if tree_holder.get("n"):
        log(f"   （build_tree 取到 {tree_holder['n']} 个节点——P0-a 结论：走元素级路线可行）")
    log(f"   收尾检查：DBeaver 存活={alive(pid)}  registryd存活={registry_alive()}")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())