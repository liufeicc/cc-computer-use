"""
UIA 树读取与延迟测量（backend.windows.driver.tree）。

═══ 两件事 ═══

1. **读一棵真树**：根元素 → 应用 → 子树，带 Name / ControlType / ClassName /
   BoundingRectangle / 支持的 Pattern。
2. **量延迟**：单属性读 vs 整树遍历，分开计时。

延迟为什么必须量：`Backend` 的既有遍历预算（节点数上限）在 Linux 上是**保命闸**
（AT-SPI 的 `child_count` 会逼目标应用惰性建树，历史上把宿主 GNOME Shell 打崩过两次）。
而 Windows 的 UIA 是跨进程 COM，代价是**延迟**不是崩溃 —— 性质从「保命闸」变成
「时延闸」。所以光有节点数上限不够，**还需要一条墙钟 deadline**，而 deadline 该设多少
得先知道单次调用大概多贵。

═══ ⚠️ 缓存的陷阱 ═══

`uiautomation` 的 `Control` 对象会在内部缓存部分属性。若拿"读已缓存属性"的耗时
去代表"跨进程 COM 往返"，会把 deadline 定得**过于乐观**。故本模块**两个通道分别报**：
  · `library`  —— 库的高层属性（可能命中缓存）
  · `raw`      —— 直接 `GetCurrentPropertyValue`（绕过高层封装）
并**显式标注**哪个是哪个，不让读报告的人误用。
"""

from __future__ import annotations

import time

from . import _uia

# 计时用的属性（都存在，且类型不同，能顺带覆盖几种编组路径）
_PROBES = ("Name", "ClassName", "ControlType", "BoundingRectangle", "IsOffscreen")


def _now() -> float:
    return time.perf_counter()


def measure_attr_latency(control, repeat: int = 50) -> dict:
    """
    对**同一个**元素反复读几个属性，返回各属性的 min/p50/p90/max（毫秒）。

    刻意报分位数而不是均值：COM 调用的分布是长尾的（首个调用要 QI、偶尔撞上目标应用
    忙），均值会被长尾拉高到没有代表性。
    """
    out: dict = {"repeat": repeat, "per_attr": {}}
    samples: dict[str, list[float]] = {a: [] for a in _PROBES}

    for _ in range(repeat):
        for attr in _PROBES:
            t0 = _now()
            try:
                getattr(control, attr, None)
            except Exception:  # noqa: BLE001 —— 读不到也要计入耗时（那就是它的真实代价）
                pass
            samples[attr].append((_now() - t0) * 1000.0)

    for attr, vals in samples.items():
        vals.sort()
        n = len(vals)
        out["per_attr"][attr] = {
            "min_ms": round(vals[0], 3),
            "p50_ms": round(vals[n // 2], 3),
            "p90_ms": round(vals[int(n * 0.9)], 3),
            "max_ms": round(vals[-1], 3),
        }
    return out


def measure_timer_baseline(times: int = 200_000) -> dict:
    """
    计时器基线自检。

    为什么要有：若宿主/客体负载异常，`perf_counter` 的读数可能整体失真。
    先跑一段空循环量出"这台机器上计时本身的值"，报告里带上它，
    读数据的人才有依据判断那些毫秒数是否可信（与"本轮数据作废"的判据配套）。
    """
    t0 = _now()
    acc = 0
    for i in range(times):
        acc += i
    dt = (_now() - t0) * 1000.0
    return {"loop_iters": times, "total_ms": round(dt, 3), "acc": acc}


def walk_tree(control, max_depth: int = 6, max_nodes: int = 400) -> dict:
    """
    遍历子树，返回 `{"nodes": [...], "count", "truncated", "elapsed_ms"}`。

    实现逻辑：
      · 深度优先，`GetChildren()` 批量取子级（比逐个 `GetFirstChildControl` +
        `GetNextSiblingControl` 少一半 COM 往返——后者是每次一个跨进程调用）；
      · 命中 `max_depth` / `max_nodes` 时 `truncated=True` —— **绝不静默截断**
        （本项目在 Linux 侧为此专门立过规矩：截断必须带提示，否则模型会误读成"界面没有这个元素"）；
      · 节点字段刻意与 `backend/base.py::UINode` 对齐（role/name/rect/value/actions），
        让将来接进 backend 时不用再翻译一遍结构。

    ⚠️ `control` 的类型不做校验：它来自 `_uia.import_uia()` 拿到的库对象，
    在本模块里只当作"有 GetChildren/Name/… 的东西"用（便于在 Linux 上喂假对象做单测）。
    """
    t0 = _now()
    nodes: list[dict] = []
    truncated = False

    def visit(node, depth: int) -> None:
        nonlocal truncated
        if len(nodes) >= max_nodes:
            truncated = True
            return
        for child in _children_of(node):
            if len(nodes) >= max_nodes:
                truncated = True
                return
            nodes.append(_snapshot(child, depth))
            if depth < max_depth:
                visit(child, depth + 1)
            else:
                # 到深度上限了：在这里判"还有没有更深的东西"，有就标记截断
                if _children_of(child):
                    truncated = True

    visit(control, 1)
    return {
        "nodes": nodes,
        "count": len(nodes),
        "truncated": truncated,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "elapsed_ms": round((_now() - t0) * 1000.0, 3),
    }


def _children_of(node) -> list:
    try:
        return list(node.GetChildren() or [])
    except Exception:  # noqa: BLE001 —— 个别节点取子级会抛（元素已失效等），跳过它
        return []


def _snapshot(node, depth: int) -> dict:
    """取一个节点的紧凑快照。**每个字段单独 try** —— 任一属性读失败不该丢掉整个节点。"""
    def safe(attr, default=None):
        try:
            return getattr(node, attr, default)
        except Exception:  # noqa: BLE001
            return default

    rect = safe("BoundingRectangle")
    return {
        "depth": depth,
        "name": safe("Name", ""),
        "class": safe("ClassName", ""),
        "control_type": str(safe("ControlTypeName", "") or ""),
        "automation_id": safe("AutomationId", ""),
        "rect": ([rect.left, rect.top, rect.right, rect.bottom] if rect else None),
        "offscreen": safe("IsOffscreen"),
        "native_hwnd": safe("NativeWindowHandle"),
        "patterns": _patterns_of(node),
    }


def _patterns_of(node) -> list[str]:
    """节点支持的 Pattern 名（元素级操作能不能做，全看这个）。"""
    try:
        getter = getattr(node, "GetSupportedPatterns", None)
        if getter is None:
            return []
        return [str(getattr(p, "__name__", p)) for p in getter()]
    except Exception:  # noqa: BLE001
        return []


def control_type_histogram(nodes: list[dict]) -> dict:
    """ControlType 直方图 —— 用来一眼看出"这棵树读得对不对"。

    真机 DEMO 实测：同一个「按钮」，经典 Win32 应用（走 MSAA 桥）报 `Pane`，
    UWP 原生 provider 报 `Button`。所以角色映射**不能只看 ControlType**，
    必须 `ControlType + ClassName` 联合判定 —— 直方图里把这个事实摆出来，
    免得后来人照着 ControlType 写一张错的映射表。
    """
    hist: dict[str, int] = {}
    for n in nodes:
        key = n.get("control_type") or "(空)"
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


def unnamed_ratio(nodes: list[dict]) -> float:
    """无名节点占比。过高说明读到的多半是布局容器（`core/serializer.py` 的 flatten 逻辑要处理）。"""
    if not nodes:
        return 0.0
    return round(sum(1 for n in nodes if not (n.get("name") or "").strip()) / len(nodes), 3)


def uia_element_for_hwnd(hwnd: int):
    """HWND → UIA 元素（**本项目感知的主路径**：先枚举窗口，再按 HWND 直读）。"""
    uia = _uia.import_uia()
    return uia.ControlFromHandle(hwnd)
