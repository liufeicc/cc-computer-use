"""
尖刀入口（backend.windows.driver.probe）—— 在真机上回答四个问题。

═══ 它要回答什么 ═══

真机 DEMO 是用 **PowerShell + .NET** 验证的概念（`docs/PR/windows-demo/`），
而**真正要发货的是 Python**。两条路之间隔着一层真实的不确定性，本模块就是去把它量掉：

    M0  环境可不可信？       会话是不是交互式（否则后面全是环境假象）
    M1  能不能读到真 UIA 树？ 根元素子级数、子树规模、ControlType 分布
    M2  能不能取到子 HWND？   ★ 定向注入能否成立的唯一前提
    M3  一次调用多贵？        单属性读 vs 整树遍历的延迟（决定要不要加墙钟 deadline）
    M4  能不能在子线程跑？    MCP 工具由 anyio 线程池执行 —— 若 UIA 在非主线程不可用，
                             那必须在**接进 server 之前**知道，否则架构要改

═══ 用法 ═══

    python -m computer_use_mcp.backend.windows.driver.probe --out Z:\\...\\probe.json

默认目标取**当前前台窗口**（不启动任何应用）—— 这样本模块零 subprocess，
也不会被 `tests/test_spawn_env.py` 的 spawn 扫描牵连。

═══ 退出码（调用方据此判断，不靠读 JSON 猜）═══

    0  全部通过
    3  会话不可信（Session 0 / 非 WinSta0）—— **拒绝出结论**
    4  DPI 感知没设上（坐标数据不可信）
    5  跑通了但有自检项没过（如树为空、子 HWND 只下钻了一层）
    2  驱动不可用（缺 uiautomation / 非 Windows）
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from . import _uia, session_info, tree, win32


def _default_point(hwnd: int) -> tuple[int, int] | None:
    """
    在给定窗口里挑一个"M2 值得测"的点：取窗口客户区中心偏上一点。

    为什么不随便挑一个点：若挑到窗口的标题栏或边框，下钻链天然只有一两层，
    那测出来的是"边框没有子窗"这个显然的事实。客户区中心更可能落在真正的内容控件上。
    """
    rect = win32.window_rect(hwnd)
    if not rect:
        return None
    left, top, right, bottom = rect
    if right - left <= 0 or bottom - top <= 0:
        return None
    # 偏上 1/4 处：避开底部的状态栏/工具栏，也避开正中心的可能空洞
    return (left + (right - left) // 2, top + (bottom - top) // 4)


def run(target_hwnd: int | None = None, point: tuple[int, int] | None = None,
        repeat: int = 50, max_depth: int = 6, max_nodes: int = 400) -> dict:
    """跑完整套测量，返回报告 dict（不写文件、不打印——便于单测直接调）。"""
    report: dict = {"schema": 1, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # ---------- M0 环境闸 ----------
    si = session_info.check()
    si["uia_root_child_count"] = session_info.uia_root_child_count()
    report["session"] = si

    # ---------- DPI 顺序（必须先于任何 UIA 对象）----------
    report["dpi"] = _uia.ensure_dpi_awareness()

    # ---------- 驱动能力 ----------
    caps = _uia.capability_dump()
    report["uiautomation"] = caps
    if not caps.get("available"):
        report["verdict"] = "driver_unavailable"
        return report

    # ---------- 选目标窗口 ----------
    hwnd = target_hwnd or win32.foreground_window()
    if not hwnd:
        report["verdict"] = "no_target"
        report["note"] = "没有前台窗口（服务会话的典型症状）"
        return report
    report["target"] = win32.describe(hwnd)
    report["target"]["dpi"] = win32.dpi_for_window(hwnd)

    # ---------- M1 读树 ----------
    #
    # ⚠️ **冷/热两轮都要量**，而且这个区分是必须的：
    # UIA 是跨进程 COM，目标应用的 provider 往往**惰性建树** —— 第一次问它"你有几个孩子"
    # 时它才去构造整棵子树。所以冷启动那一轮可能比之后每一轮贵一个量级，
    # 而"该设多大的 deadline"必须按**冷**的那一轮定（那正是用户第一次调用时的体验）。
    # 这与 Linux 侧 `child_count` 会逼 AT-SPI 目标应用建树是同一类现象，只是那边表现为
    # "可能打崩宿主"，这边表现为"第一下很慢"。
    try:
        element = tree.uia_element_for_hwnd(hwnd)
        cold = tree.walk_tree(element, max_depth=max_depth, max_nodes=max_nodes)
        warm = tree.walk_tree(element, max_depth=max_depth, max_nodes=max_nodes)
        report["tree"] = warm
        report["tree"]["control_type_histogram"] = tree.control_type_histogram(warm["nodes"])
        report["tree"]["unnamed_ratio"] = tree.unnamed_ratio(warm["nodes"])
        report["tree"]["cold_walk_ms"] = cold["elapsed_ms"]
        report["tree"]["cold_count"] = cold["count"]
        report["tree"]["warm_walk_ms"] = warm["elapsed_ms"]
        report["tree"]["cold_over_warm"] = (
            round(cold["elapsed_ms"] / warm["elapsed_ms"], 2) if warm["elapsed_ms"] else None
        )
    except Exception as exc:  # noqa: BLE001
        report["tree"] = {"error": f"{type(exc).__name__}: {exc}"}
        return report

    # ---------- M2 子 HWND 下钻 ----------
    pt = point or _default_point(hwnd)
    if pt:
        report["hwnd_drill"] = win32.deepest_child_at(pt[0], pt[1])
        # 与 UIA 的命中测试交叉验证：两边给出的 NativeWindowHandle 应当一致
        try:
            uia = _uia.import_uia()
            el = uia.ControlFromPoint(pt[0], pt[1])
            report["hwnd_drill"]["uia_control_from_point"] = {
                "name": getattr(el, "Name", None) if el else None,
                "native_hwnd": getattr(el, "NativeWindowHandle", None) if el else None,
            }
        except Exception as exc:  # noqa: BLE001
            report["hwnd_drill"]["uia_control_from_point"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        report["hwnd_drill"] = {"ok": False, "reason": "没能算出测试点（窗口矩形为空）"}

    # ---------- M3 延迟 ----------
    report["latency"] = {
        "timer_baseline": tree.measure_timer_baseline(),
        "attr_reads": tree.measure_attr_latency(element, repeat=repeat),
        "tree_walk_ms": report["tree"]["elapsed_ms"],
        "tree_nodes": report["tree"]["count"],
        # ⚠️ 显式声明这是**库封装**通道：它可能命中库内缓存，**不可**当作跨进程 COM 往返
        "channel": "library（可能命中 uiautomation 内部缓存；不代表原始 COM 往返）",
    }

    # ---------- M4 子线程可用性 ----------
    report["threaded"] = _threaded_check(hwnd)

    report["verdict"] = _verdict(report)
    return report


def _threaded_check(hwnd: int) -> dict:
    """
    在**非主线程**里再读一次树。

    为什么这条可能改变架构：MCP 工具由 anyio 的线程池执行，**不在主线程**。
    UIA 是 COM，而 COM 有 apartment 模型 —— 若库没有为每个线程做初始化，
    在线程池里调用可能直接失败或行为异常。这件事**必须在接进 server 之前**知道：
    真出问题就得改成"所有 UIA 调用固定在一个专用线程里"，那是架构级的改动。
    """
    out: dict = {}
    result: dict = {}

    def work() -> None:
        try:
            el = tree.uia_element_for_hwnd(hwnd)
            kids = el.GetChildren()
            result["ok"] = True
            result["child_count"] = len(kids)
            result["thread_name"] = threading.current_thread().name
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=work, name="uia-thread-check", daemon=True)
    t.start()
    t.join(timeout=30)
    out["ran_off_main_thread"] = not t.is_alive()
    out.update(result)
    if t.is_alive():
        out["note"] = "子线程 30s 没返回 —— UIA 在线程池里可能不可用，需专门处理"
    return out


def _verdict(report: dict) -> str:
    """
    综合判断，给调用方一个**不需要读完整 JSON** 的结论字符串。

    刻意把"环境不可信"排在"能力有问题"前面：在服务会话里，树为空是**环境造成的**，
    把它报成"UIA 读不到树"会指向完全错误的修复方向。
    """
    si = report.get("session", {})
    if not si.get("interactive"):
        return "session_untrusted"
    if not report.get("dpi", {}).get("ok"):
        return "dpi_not_set"
    tr = report.get("tree", {})
    if tr.get("error") or not tr.get("count"):
        return "tree_empty"
    drill = report.get("hwnd_drill", {})
    if not drill.get("ok") or not drill.get("is_child"):
        # 取到的就是顶层框本身 ⇒ 定向注入的前提不成立
        # （那正是 DEMO 里"PostMessage 投给顶层无效"的原因）
        #
        # ⚠️ 判据是 `is_child` 而**不是**"下钻了几层"：实测 `WindowFromPoint` 自己就会下钻，
        #    拿到子窗时 `ChildWindowFromPointEx` 只能再下钻 0 层 —— 用层数判会把
        #    "一下就拿对了"误判成失败（本探针第一版正是这么错的）。
        return "no_child_hwnd"
    return "ok"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windows UIA 驱动层尖刀探针")
    ap.add_argument("--out", required=True, help="结果 JSON 的落盘路径")
    ap.add_argument("--hwnd", type=lambda s: int(s, 0), default=None, help="目标窗口句柄（默认取前台窗口）")
    ap.add_argument("--point", default=None, help="测试点 'x,y'（默认按目标窗口推算）")
    ap.add_argument("--repeat", type=int, default=50, help="延迟测量重复次数")
    ap.add_argument("--max-depth", type=int, default=6)
    ap.add_argument("--max-nodes", type=int, default=400)
    args = ap.parse_args(argv)

    point = None
    if args.point:
        try:
            x, y = (int(v) for v in args.point.split(",", 1))
            point = (x, y)
        except ValueError:
            print(f"❌ --point 格式不对：{args.point!r}（应为 'x,y'）", file=sys.stderr)
            return 2

    try:
        report = run(target_hwnd=args.hwnd, point=point, repeat=args.repeat,
                     max_depth=args.max_depth, max_nodes=args.max_nodes)
    except _uia.WindowsDriverUnavailable as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, default=str)
    except OSError as exc:
        # 写不出去就把结果打到 stdout —— 至少别把这次测量的信息全丢掉
        print(f"⚠️ 写 {args.out} 失败（{exc}），结果改打到 stdout：", file=sys.stderr)
        json.dump(report, sys.stdout, ensure_ascii=False, indent=1, default=str)
        return 5

    verdict = report.get("verdict", "unknown")
    print(f"[probe] verdict = {verdict}")
    print(f"[probe] 结果已写入 {args.out}")
    return {"ok": 0, "session_untrusted": 3, "dpi_not_set": 4}.get(verdict, 5)


if __name__ == "__main__":
    sys.exit(main())
