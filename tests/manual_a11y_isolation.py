"""
a11y 隔离的**集成验证**：沙箱内应用的树可以被读，而宿主完全不受影响。

═══ 为什么需要它 ═══
虚拟屏**不隔离 AT-SPI**（a11y 走会话 D-Bus）。2026-09-15 实测事故：对沙箱内应用做
a11y 遍历，把宿主 GNOME Shell 打崩 —— 离线解 core 确证是 gnome-shell **自己**的
atk-bridge 在对已释放的 GObject 做 `g_object_ref`（use-after-free），崩在主循环线程。

本脚本验证**私有 AT-SPI 总线**把这个缺口补上了：沙箱应用与 MCP 的 reader 都连私有
总线，宿主那条总线上完全看不到它们。

═══ 安全设计（重要）═══
遍历 a11y 本身是有风险的（见上），所以本脚本**在开始遍历之前先断言隔离成立**：
  ① 私有总线确实起来了；
  ② reader 绑定的地址**就是**私有总线（而不是宿主总线）；
  ③ 私有总线上**看不到宿主应用**（gnome-shell / gsd-* 这些）。
三条都过了才继续读树 —— 任何一条不过就直接退出，绝不在未隔离的情况下遍历。

用法：
  PYTHONNOUSERSITE=1 "$PY" tests/manual_a11y_isolation.py
退出码 0 = 全过。
"""

from __future__ import annotations

import os
import sys

import computer_use_mcp._bootstrap  # noqa: F401  （必须在 import gi 之前）

_ok = True


def log(msg: str) -> None:
    print(msg, flush=True)


def check(cond: bool, msg: str) -> bool:
    global _ok
    log(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _ok = False
    return cond


def main() -> int:
    from computer_use_mcp.core import display
    from computer_use_mcp.core.coordinator import Coordinator
    from computer_use_mcp.backend.base import get_backend

    log("① 起沙箱（会一并起私有 AT-SPI 总线）")
    display.MANAGER.ensure_started()
    bus = display.MANAGER.at_spi_bus()
    check(bus is not None, f"私有 AT-SPI 总线已就绪: {bus}")
    if not bus:
        log("  私有总线没起来 → 应用会走死地址（安全但无 a11y），本验证无法继续")
        display.MANAGER.stop()
        return 1
    check(bus.startswith("unix:path=/tmp/"), "总线路径在我们的临时目录里（不碰 /run/user/*）")

    backend = get_backend()
    coord = Coordinator(backend=backend)

    log("\n② 在沙箱内起一个 GTK3 应用")
    coord.launch_app("evince", settle=4)
    info = coord.wait_window(title_contains="evince", timeout=15) or \
        coord.wait_window(title_contains="文档", timeout=5)
    log(f"   {info}")
    check(info is not None, "应用窗口已出现")

    log("\n③ 【安全闸】确认 reader 连的是私有总线、且看不到宿主应用")
    backend.sync_at_spi_bus()
    bound = backend.reader._bound_address          # noqa: SLF001  验证用
    check(bound == bus, f"reader 绑定 = {bound}（应为私有总线）")
    if bound != bus:
        log("  ⚠️ reader 没绑到私有总线 —— 绝不能在未隔离的情况下遍历，退出")
        display.MANAGER.stop()
        return 1
    host_names = [backend.reader.get_name(a) for a in backend.reader.iter_apps()]
    log(f"   私有总线上可见应用: {host_names}")
    check("gnome-shell" not in host_names, "看不到宿主 gnome-shell（隔离成立）")
    check(any("evince" in n for n in host_names), "能看到沙箱内的 evince")
    if "gnome-shell" in host_names:
        log("  ⚠️ 隔离没成立 —— 退出，不冒险遍历")
        display.MANAGER.stop()
        return 1

    log("\n④ 隔离已确认 → 放心读沙箱内应用的树")
    ok, tree = True, ""
    try:
        tree = coord.get_ui_tree(scope="app", app="evince", max_nodes=80)
    except Exception as exc:  # noqa: BLE001
        ok = False
        log(f"   读树异常: {type(exc).__name__}: {exc}")
    lines = [ln for ln in tree.splitlines() if ln.strip()]
    check(ok and len(lines) > 0, f"读到树（{len(lines)} 行）")
    for ln in lines[:8]:
        log("     " + ln[:100])

    log("\n⑤ 收尾")
    # M-56③：判据必须收窄到「**本次这条**总线目录是否消失」。
    # 原先用的是全局 `glob.glob("/tmp/cc-cu-at-spi-*")` —— 那会被**别的会话**留下的
    # 残留带红（2026-09-16 实测：本次跑出的目录正常回收、目录总数没增长，却因另一组
    # 01:03 的历史残留报 ❌）。用 bus 变量里已经有的那条路径，判据才只关乎本次。
    bus_dir = os.path.dirname(bus.split("unix:path=", 1)[1]) if bus else None
    display.MANAGER.stop()
    if bus_dir:
        check(not os.path.exists(bus_dir), f"本次的私有总线目录已回收（{bus_dir}）")
    else:
        log("   （未拿到总线路径，跳过目录回收检查）")
    log("\n" + ("✅ 全过：a11y 已隔离，沙箱内可读树、宿主不受影响" if _ok else "❌ 有失败项"))
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())