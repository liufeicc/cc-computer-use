"""
find() 无窗口应用预滤的**真机验证**：预滤不会把该搜到的应用滤掉。

═══ 为什么单开一个真机探针 ═══
`tests/test_atspi_guards.py` 那组用例全在假对象上跑（pid 是自己编的），因此有一条
关键前提**单测根本测不到**：

    `window_pids()` 的 pid 来自 X11 的 `_NET_WM_PID`（`wmctrl -lpx`），
    而预滤比较的另一侧来自 AT-SPI 的 `get_process_id(app)`。

这两个 pid **必须指向同一个进程**，预滤才成立。只要有任何一条路径让它们错开
（Flatpak/Snap 那类启动器把窗口 pid 记在父进程、a11y 树注册在子进程；或应用根本
不设 `_NET_WM_PID`），预滤就会把**正在用的那个应用整个跳过** —— 表现恰是「按 text
找不到元素」，而这正是本次改动想修的症状，等于用一种失效替换另一种失效。

所以本脚本不测逻辑（逻辑有单测），只钉这条**跨通道一致性**前提：
  ① 沙箱内起的 zenity，其 pid 在 wmctrl 侧与 AT-SPI 侧**都对得上**；
  ② 不带 app= 的全桌面 find 仍能搜到它的按钮（预滤没把它滤掉）；
  ③ 元素级点击真的把它点掉（搜到的确实是它，不是同名别的东西）；
  ④ 盘点漏网面：桌面上「有窗口但 a11y 侧无对应应用」的 pid 有几个。

═══ 安全设计 ═══
遍历 a11y 本身有风险（见 CLAUDE.md「child_count 是全项目最危险的单次调用」），故
**沿用 manual_a11y_isolation.py 的安全闸**：先断言 reader 绑在私有总线、且看不到宿主
gnome-shell，才继续 find 遍历。任何一条不过就退出，绝不在未隔离的情况下遍历。

用法：
  PYTHONNOUSERSITE=1 "$PY" tests/manual_find_prefilter_probe.py
退出码 0 = 全过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import computer_use_mcp._bootstrap  # noqa: F401  （必须在 import gi 之前）

_ok = True
TITLE = "预滤探针"


def log(msg: str) -> None:
    print(msg, flush=True)


def check(cond: bool, msg: str) -> bool:
    global _ok
    log(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _ok = False
    return cond


def main() -> int:
    if os.environ.get("CC_CU_DISPLAY_MODE", "").lower() == "real":
        log("real 模式下本探针会遍历宿主桌面，拒绝运行（默认 isolated 即可）")
        return 2

    from computer_use_mcp.backend.base import get_backend
    from computer_use_mcp.core import display
    from computer_use_mcp.core.coordinator import Coordinator

    log("① 起沙箱（会一并起私有 AT-SPI 总线）")
    display.MANAGER.ensure_started()
    bus = display.MANAGER.at_spi_bus()
    if not check(bus is not None, f"私有 AT-SPI 总线已就绪: {bus}"):
        display.MANAGER.stop()
        return 1

    backend = get_backend()
    coord = Coordinator(backend=backend)
    backend.sync_at_spi_bus()

    log("② 【安全闸】reader 必须绑在私有总线上，否则绝不遍历")
    bound = backend.reader._bound_address          # noqa: SLF001  验证用
    if not check(bound == bus, f"reader 绑定 = {bound}（应为私有总线）"):
        log("  ⚠️ reader 没绑到私有总线 → 未隔离，退出（不在宿主总线上遍历）")
        display.MANAGER.stop()
        return 1
    names = [backend.reader.get_name(a) for a in backend.reader.iter_apps()]
    check("gnome-shell" not in names, f"私有总线上看不到宿主应用：{names}")
    if "gnome-shell" in names:
        log("  ⚠️ 隔离没成立 → 退出，不冒险遍历")
        display.MANAGER.stop()
        return 1

    log("③ 沙箱内起 zenity（按钮标签写死，判据与系统语言无关）")
    ok_label, no_label = "确认预滤", "取消预滤"
    subprocess.run(["pkill", "-9", "-x", "zenity"], capture_output=True)   # 精确匹配，勿用 -f
    info = coord.launch_app(
        f"zenity --question --title {TITLE} --text 预滤探针"
        f" --ok-label={ok_label} --cancel-label={no_label}", settle=2.0)
    app_pid = info["pid"]
    log(f"   zenity pid={app_pid} display={info['display']}")
    bus_dir = os.path.dirname(bus.split("unix:path=", 1)[1]) if bus else None

    try:
        log("④ 等它上树并跑不带 app= 的全桌面 find（轮询，不用固定 sleep —— I-17）")
        found, notice = [], ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            found, notice = coord.find_elements(text=ok_label, role=None, app=None, limit=5)
            if found:
                break
            time.sleep(0.4)
        check(bool(found), f"全桌面 find 搜到按钮：{[(e.name, e.app) for e in found]}")
        # 有结果时**不该**出现「无 X11 窗口未搜索」那条提示（它会误导模型怀疑结果不全）
        check("无 X11 窗口" not in (notice or ""), f"命中结果时无跳过提示：{notice!r}")

        log("⑤ 跨通道 pid 一致性（预滤成立的真正前提）")
        win_pids = backend.window_pids()
        a11y_pids = {backend.reader.get_process_id(a) for a in backend.reader.iter_apps()}
        a11y_pids.discard(None)
        check(app_pid in win_pids, f"wmctrl 侧认得该 pid：{sorted(win_pids)}")
        check(app_pid in a11y_pids, f"AT-SPI 侧认得该 pid：{sorted(a11y_pids)}")

        orphan = sorted(p for p in win_pids if p not in a11y_pids)
        log(f"⑥ 漏网面盘点：有窗口但 a11y 侧无对应应用的 pid = {orphan}")
        check(app_pid not in orphan, "本次这个沙箱内应用不该落在漏网面里")

        log("⑦ 元素级点击收尾")
        if found:
            r = coord.click(ref=found[0].ref)
            check(r.ok, f"click(ref={found[0].ref}) → ok={r.ok} level={r.level}")
            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                alive = bool(subprocess.run(["pgrep", "-x", "zenity"],
                                            capture_output=True).stdout.strip())
                if not alive:
                    break
                time.sleep(0.3)
            check(not alive, "点掉后 zenity 已退出（搜到并点的确实是它）")

        return 0 if _ok else 1
    finally:
        subprocess.run(["pkill", "-9", "-x", "zenity"], capture_output=True)
        display.MANAGER.stop()
        # 回收检查必须排在 stop() **之后**（放在 try 里时总线目录还在，判据恒红）
        check(not bus_dir or not os.path.exists(bus_dir),
              f"本次的私有总线目录已回收（{bus_dir}）")


if __name__ == "__main__":
    sys.exit(main())
