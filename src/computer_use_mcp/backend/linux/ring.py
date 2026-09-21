"""
点击光圈（backend/linux/ring）—— 坐标点击时在屏幕上闪一个红圈，**给人看**。

和其他落点反馈的分工：`click_preview` 给的准星图是**给模型看**的（跟着点击结果返回）；
这里的光圈是**给人看**的——用户（或看沙箱窗口的人）能一眼看到「agent 刚才点了哪儿」。
它纯属可视化，不参与任何判断。

实现要点与理由（改前先读）：

1. **进程内线程 + 队列，不起子进程**。
   - python-xlib 的 `Display` **不是线程安全的**（一次连接上的请求/应答与内部事件队列
     没有锁），所以全部 X 流量必须关进**单一 owner 线程**；调用方只投递指令。
   - X 协议保证：**连接断开时 server 释放该连接的全部资源**——包括进程被 SIGKILL 的
     情形（内核关 socket）。所以光圈窗口随进程死亡自动消失，**不需要 atexit，也不会
     在用户桌面上留下孤儿窗口**。若改用常驻子进程，就得自己处理孤儿回收（对照
     `core/display` 里私有总线那套 `_sweep_stale_buses` 的工作量，能省则省）。
   - 顺带：不新增任何 subprocess spawn 点（`tests/test_spawn_env.py` 因此零改动），
     也避开「冻结产物自 spawn 要保留 _internal 库路径」与 `env_for()` 剥库路径的冲突。

2. **画圈时机是「点击之前」**：`injector.click_at` 是单个 xdotool 进程串
   `windowactivate --sync → mousemove → click`，几十到上百毫秒；先投递画圈（微秒级入队），
   圈先亮、点击随后落下，视觉上是「这里要点 → 点了」。放在点击之后则有两个坏处：
   点击抛异常时人完全看不到痕迹（而失败恰恰最需要知道它想点哪），且点完界面可能已变。

3. **必须实色，不能半透明**：沙箱 Xephyr 里 i3 **不做合成**，32bpp ARGB 窗口没有
   composer 接管、alpha 会被忽略（结果是脏块）。故一律实色红。

4. **点击穿透靠 SHAPE 的 Input 形状置空**（本机宿主 mutter 与 Xephyr 实测均为 SHAPE 1.1）。
   拿不到 SHAPE 时退化为「四条细矩形拼成的取景括号」——**刻意留出中心**，故即使窗口
   自己不穿透，落在中心的点击也不会被它挡住（光圈是在点击前画的，挡到就等于制造新 bug）。

5. **绝不抛、绝不拖慢点击**：入队是 `put_nowait`（队列满就丢弃并记 debug）；任何异常只
   记 debug 并**按 display 熔断**（连续 2 次失败才放弃该屏，成功一次即清零）。为什么按
   屏记账而不是一个全局 bool：沙箱屏号是自动分配、重建后会变（`Xephyr -displayfd`），
   全局熔断会被一次早已无关的故障永久锁死；而「一次连接失败」往往只是沙箱重建竞态，
   第一次失败就永久关掉功能属于**静默能力消失**，正是本项目最忌讳的失效形态。

6. **`event_mask=0` 且从不选事件**：python-xlib 的 Display 有内部事件队列，若选了掩码却
   从不消费，长驻连接的事件队列会**无界增长**（慢性泄漏）。将来若有人为了做淡出动画
   而顺手加个 StructureNotifyMask，请同时消费事件——否则这就是个坑。

7. **连接缓存按屏，且新增 key 时关掉旧 key**：一个进程一生只可能针对一块屏点击；屏号
   变了说明沙箱重建了，旧连接留着只会泄漏。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

from ...utils.logging import get_logger

log = get_logger(__name__)

# 总开关：CC_CU_CLICK_RING=0（或 false/no/off）关掉光圈。默认开。
ENV_RING = "CC_CU_CLICK_RING"

# 圈的尺寸（外径）与线宽。**纯观感参数**：这是画给人看的，只求「一眼看到点了哪儿」，
# 不承担任何度量职责（要量偏差看准星图/程序算的偏差值）。初版 100 太大——圈本身占掉
# 100x100 的信息，反而盖住落点周围正是人想看的那些字与控件；48 足够醒目又不喧宾夺主。
# ⚠️ 改这里要顺手看 `tests/test_e2e_zenity.py::test_ring_visible_has_hole_and_autoclears`
#（它按尺寸换算采样点），故那边是**从本模块读 _SIZE/_THICK**，而不是写死字面量。
_SIZE = 48
_THICK = 4
# 圈的自毁时间。原为 0.4s —— **实测人根本看不到**：探针在落点周围抓纯红像素，0.4s 只
# 采得到 12 帧（实际存活 0.35s），而人眼当时多半正看着别处（Claude Code 界面），一眨眼
# 就错过；48px 的细环在 1600x1000 屏上本就小。1.5s 是「扫一眼能看清」与「不碍事」的折中。
# 圈变久之后，两处会把它拍进去的地方都**必须**有 clear_now 兜底：`screenshot()` 开头，
# 以及 `change_fraction_since()` 点后抓屏之前——后者若漏掉，536 个圈像素会凭空算成
# ~0.37% 的「界面变化」，与 0.5% 的「无变化」阈值只差一点点，属于侥幸不误报而非设计保证。
_TTL = 1.5
# worker 的 tick：决定 clear_now 的最坏等待与自毁精度
_TICK = 0.05
# 红色像素值（TrueColor 24/32 下的 0xRRGGBB）
_RED = 0xFF0000
# 同一屏连续失败多少次后熔断
_MAX_FAILS = 2

_QUEUE: queue.Queue = queue.Queue(maxsize=4)
_WORKER: threading.Thread | None = None
_START_LOCK = threading.Lock()
_CONNS: dict[str, Any] = {}          # display → Xlib Display（**仅 worker 线程读写**）
_FAILS: dict[str, int] = {}
_DISABLED: set[str] = set()
_SHAPE_OK: dict[str, bool] = {}      # 该屏能否用 SHAPE（缓存，别每次点击都问一遍扩展表）
_VISIBLE = False                     # 屏上是否正有一个圈（worker 写、clear_now 读）
_VISIBLE_LOCK = threading.Lock()


def enabled() -> bool:
    """光圈总开关（环境变量默认开）。"""
    raw = os.environ.get(ENV_RING)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def show(x: int, y: int, ttl: float = _TTL) -> None:
    """
    请求在屏幕 (x,y) 处闪一个红圈（给人看）。**永不抛、永不阻塞**。

    ⚠️ 目标屏在这里（**调用线程**）解析并随指令一起入队：绝不能让 worker 线程稍后再查
    `display.effective_display()`——沙箱可能在这期间重建/消失，那样圈会画到**宿主屏**上，
    而调用方毫不知情（本项目的沙箱隔离原则：任何「稍后再取 display」都是隐患）。
    """
    if not enabled():
        return
    try:
        from ...core import display as display_mod

        disp = display_mod.effective_display()
        if not disp or disp in _DISABLED:
            return
        _ensure_worker()
        _QUEUE.put_nowait(("show", disp, int(x), int(y), float(ttl)))
    except queue.Full:
        log.debug("光圈队列已满，丢弃本次（宁可看不见圈，也不让点击变慢）")
    except Exception as exc:  # noqa: BLE001
        log.debug("光圈请求失败（不影响点击）: %s", exc)


def clear_now(timeout: float = 0.05) -> None:
    """
    若屏上正有圈，等它消失再返回（截图前调用，免得把残圈拍进图里）。

    没圈时只是一次布尔判断、零成本；有圈时最多等 `timeout`（worker 的 tick 是 50ms）。
    """
    with _VISIBLE_LOCK:
        if not _VISIBLE:
            return
    done = threading.Event()
    try:
        _QUEUE.put_nowait(("clear", done))
    except queue.Full:
        return
    done.wait(timeout)


def status() -> dict[str, Any]:
    """当前状态摘要（selftest / 排查用）：各屏是否可用、是否被熔断。"""
    return {
        "enabled": enabled(),
        "disabled": sorted(_DISABLED),
        "fails": dict(_FAILS),
        "shape": dict(_SHAPE_OK),
        "connections": sorted(_CONNS),
    }


def _ensure_worker() -> None:
    """懒启动 worker 线程（只启动一次；持锁的临界区极小）。"""
    global _WORKER
    if _WORKER is not None and _WORKER.is_alive():
        return
    with _START_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker, name="cc-cu-ring", daemon=True)
        _WORKER.start()


def _worker() -> None:
    """
    光圈线程主循环：**唯一**接触 X 的地方（python-xlib 的 Display 非线程安全）。

    每轮取一条指令（最多等 _TICK），并顺带处理「到点自毁」。用短 tick 轮询而不是
    `queue.get()` 无限阻塞，是为了让自毁与 clear_now 都能在 50ms 内被响应。
    """
    deadline = 0.0
    while True:
        timeout = _TICK if deadline == 0.0 else min(_TICK, max(0.0, deadline - time.monotonic()))
        try:
            item = _QUEUE.get(timeout=timeout)
        except queue.Empty:
            item = None
        if item is not None:
            kind = item[0]
            if kind == "show":
                _, disp, x, y, ttl = item
                _destroy_all()                    # 同屏最多一个圈（连点表现为「圈在跳」）
                _draw(disp, x, y)
                deadline = time.monotonic() + ttl
            elif kind == "clear":
                _destroy_all()
                deadline = 0.0
                try:
                    item[1].set()
                except Exception:  # noqa: BLE001
                    pass
        if deadline and time.monotonic() >= deadline:
            _destroy_all()
            deadline = 0.0


# --------------------------------------------------------------------------
# 以下函数**只在 worker 线程里**被调用
# --------------------------------------------------------------------------
_WINDOWS: list[Any] = []      # 当前屏上属于我们的窗口（通常是 1 个，括号模式是 4 个）


def _set_visible(flag: bool) -> None:
    global _VISIBLE
    with _VISIBLE_LOCK:
        _VISIBLE = flag


def _destroy_all() -> None:
    """销毁当前所有光圈窗口（worker 线程内调用）。"""
    global _WINDOWS
    if not _WINDOWS:
        _set_visible(False)
        return
    for w in _WINDOWS:
        try:
            w.destroy()
        except Exception as exc:  # noqa: BLE001
            log.debug("光圈销毁失败（忽略）: %s", exc)
    _WINDOWS = []
    _set_visible(False)
    for disp, conn in list(_CONNS.items()):
        try:
            conn.flush()          # 立刻生效；**不用 sync()**（那是等一个往返）
        except Exception:  # noqa: BLE001
            _drop_conn(disp)


def _drop_conn(disp: str) -> None:
    conn = _CONNS.pop(disp, None)
    _SHAPE_OK.pop(disp, None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _note_failure(disp: str, what: str, exc: Exception) -> None:
    """失败记账：按屏累计，连续 _MAX_FAILS 次后熔断该屏（成功一次即清零）。"""
    _drop_conn(disp)
    n = _FAILS.get(disp, 0) + 1
    _FAILS[disp] = n
    log.debug("光圈 %s 失败(%d/%d) display=%s: %s", what, n, _MAX_FAILS, disp, exc)
    if n >= _MAX_FAILS:
        _DISABLED.add(disp)
        log.warning("光圈在 display=%s 上连续失败 %d 次，本屏停止绘制（不影响任何操作）",
                    disp, n)


def _ensure_conn(disp: str):
    """
    取（或建立）目标屏的 X 连接。懒 import `Xlib`：缺包时只有光圈不可用，点击照常。

    新增一个屏的连接时**关掉其它屏的**：一个进程一生只可能针对一块屏点击，
    旧屏号（沙箱重建过）留着只会泄漏连接。
    """
    conn = _CONNS.get(disp)
    if conn is not None:
        return conn
    for other in list(_CONNS):
        if other != disp:
            _drop_conn(other)
    from Xlib import display as xdisplay   # 懒 import：缺 Xlib 只影响光圈

    conn = xdisplay.Display(disp)
    _CONNS[disp] = conn
    return conn


def _ring_rects(size: int, thick: int) -> list[dict[str, int]]:
    """
    用一组 1 像素高的**扫描线矩形**近似一个圆环（X11 的 SHAPE 只能接受矩形集合）。

    只画环带（外半径 size/2、内半径 = 外半径 − thick），中间留空——实测宿主与沙箱都能
    正确渲染，且中心无像素（e2e 的判据正是「环带有红、中心无红、四角无红」）。
    """
    import math

    half = size // 2
    r_out, r_in = half, max(1, half - thick)
    rects: list[dict[str, int]] = []
    for dy in range(-r_out, r_out + 1):
        y = half + dy
        rem_out = r_out * r_out - dy * dy
        if rem_out < 0:
            continue
        ho = int(math.sqrt(rem_out))
        rem_in = r_in * r_in - dy * dy
        hi = int(math.sqrt(rem_in)) if rem_in > 0 else -1
        if hi < 0:
            rects.append({"x": half - ho, "y": y, "width": ho * 2 + 1, "height": 1})
        else:
            w = ho - hi
            if w > 0:
                rects.append({"x": half - ho, "y": y, "width": w, "height": 1})
                rects.append({"x": half + hi + 1, "y": y, "width": w, "height": 1})
    return rects


def _draw(disp: str, x: int, y: int) -> None:
    """在 (x,y) 画圈（worker 线程内调用）。失败只记账，不影响调用方。"""
    global _WINDOWS
    try:
        from Xlib import X
        from Xlib.ext import shape

        conn = _ensure_conn(disp)
        root = conn.screen().root
        depth = conn.screen().root_depth
        half = _SIZE // 2
        use_shape = _SHAPE_OK.get(disp)
        if use_shape is None:
            use_shape = bool(conn.has_extension("SHAPE"))
            _SHAPE_OK[disp] = use_shape
        if use_shape:
            w = root.create_window(x - half, y - half, _SIZE, _SIZE, 0, depth,
                                   X.InputOutput, X.CopyFromParent,
                                   background_pixel=_RED, override_redirect=1, event_mask=0)
            w.shape_rectangles(shape.SO.Set, shape.SK.Bounding, 0, 0, 0,
                               _ring_rects(_SIZE, _THICK))
            # Input 形状置空 = 该窗口**完全不接收**指针事件 → 它下面的窗口照常收到这次点击
            w.shape_rectangles(shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
            w.map()
            _WINDOWS = [w]
        else:
            # 降级：四条细矩形拼「取景括号」，尺寸与 SHAPE 版一致（同一观感，不因缺扩展而变形）。
            # 竖向两条只画到 ±gap，**刻意在中心留出 2*gap 见方的空档**——光圈是在点击前画的，
            # 中心必须永远不被自己的窗口挡住（挡住自己点的那一下就是制造新 bug）。
            gap = half - _THICK
            specs = [(x - half, y - half, _SIZE, _THICK),               # 上
                     (x - half, y + half - _THICK, _SIZE, _THICK),      # 下
                     (x - half, y - gap, _THICK, gap * 2),              # 左
                     (x + half - _THICK, y - gap, _THICK, gap * 2)]     # 右
            wins = []
            for wx, wy, ww, wh in specs:
                win = root.create_window(wx, wy, ww, wh, 0, depth, X.InputOutput,
                                         X.CopyFromParent, background_pixel=_RED,
                                         override_redirect=1, event_mask=0)
                win.map()
                wins.append(win)
            _WINDOWS = wins
        conn.flush()               # 立刻上屏；**不用 sync()**（等往返会把圈拖到点击之后）
        _FAILS[disp] = 0           # 成功一次即清零
        _set_visible(True)
    except Exception as exc:  # noqa: BLE001
        _note_failure(disp, "绘制", exc)
        _WINDOWS = []
        _set_visible(False)