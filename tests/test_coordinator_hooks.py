"""
Coordinator 的装饰器守卫、屏独占锁与沙箱闸门（拆分自 test_optimizations.py）。

本文件是「漏挂装饰器 = 静默失效」这一类风险**唯一**的兜底，判据必须是反射枚举
（见 `_coordinator_methods` 的说明：不能用 `vars(Coordinator)`）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from computer_use_mcp.backend.base import Backend, QueryResult, Rect, TextBlock
from computer_use_mcp.backend.linux import inject as inject_mod
from computer_use_mcp.backend.linux.inject import XdotoolInjector
from computer_use_mcp.core import display as display_mod
from computer_use_mcp.core import screen_lock
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import (
    LEVEL_ELEMENT,
    BackendUnavailableError,
    ComputerUseError,
    InjectionError,
    InvalidRefError,
    SandboxUnavailableError,
    ScreenBusyError,
    to_friendly_text,
)

# 注：拆分（2026-09-18）后各文件共用**同一份 import 头**，其中未用到的名字无害。
# 统一的好处是不会漏项——按需裁剪时漏掉一个 import，报错点会离真正的原因很远。

from _helpers import _StubBackend, _proc, _reset_display  # noqa: E402




def test_coordinator_ctor_is_side_effect_free(monkeypatch):
    """构造 Coordinator 不应触发沙箱启动（server 启动路径用它，一触即弹窗就白改了）。"""
    hits: list[int] = []
    monkeypatch.setattr(display_mod.MANAGER, "ensure_started",
                        lambda: (hits.append(1), {"ok": True})[1])
    Coordinator(backend=_StubBackend())
    assert hits == []





def test_first_tool_call_triggers_lazy_start(monkeypatch):
    """首次对外方法调用才触发惰性启动。"""
    hits: list[int] = []
    monkeypatch.setattr(display_mod.MANAGER, "ensure_started",
                        lambda: (hits.append(1), {"ok": True})[1])
    # ⚠️ 必须把**沙箱闸门**一并放行：本用例测的是「_needs_display 钩子有没有被触发」，
    # 而 click_xy 之后还会过 _require_sandbox（isolated 且沙箱不在 → 拒绝执行并抛错）。
    # 单测模式下 conftest 强制 real，闸门自然放行；**e2e 模式下 mode=isolated**，于是本
    # 用例实际变成了「依赖当时恰好有沙箱在跑」——旧的文件布局里 test_e2e_zenity 模块的
    # module 级 fixture 先起了沙箱，纯属文件名字母序的侥幸；2026-09-18 按模块拆文件后
    # 它排在 e2e 之前，立刻失败。本用例与闸门无关，显式声明前提，别依赖环境。
    monkeypatch.setattr(display_mod.MANAGER, "is_sandbox_up", lambda: True)
    coord = Coordinator(backend=_StubBackend())
    coord.click_xy(1, 2)
    assert hits == [1]





def test_screen_layout_passes_through_backend_result():
    """
    screen_layout 冒烟：coordinator 必须**原样透传** backend 的结果（I-14，从 e2e 迁来）。

    它原本是 tests/test_e2e_zenity.py 里的 `test_type_and_key_smoke`，但函数体从头到尾
    只有 `screen_layout()` —— 没有 press_key、没有 type_text，名与 docstring（「键盘注入
    冒烟」）跟实际做的事完全无关，还占着 `CC_CU_E2E=1` 的一个 e2e 名额。放到这里更合适：
    它根本不需要桌面。

    迁过来后**不能照抄**原来那句 `assert "monitors" in res` —— backend 已经是桩了，
    `{}` 里断言 "monitors" 只会失败，而就算给桩塞上 monitors，那也是「断言桩返回了桩」，
    恒真的空断言。所以这里断言 coordinator 层的真实契约：不重算、不裁剪、原样透传。
    """
    backend = _StubBackend()
    marker = {
        "monitors": [{"left": -1920, "top": 0, "width": 1920, "height": 1080},
                     {"left": 0, "top": 0, "width": 1920, "height": 1080}],
        "size": [3840, 1080],
    }
    backend.screen_layout = lambda: marker

    assert Coordinator(backend=backend).screen_layout() == marker





def _hooks_of(name: str) -> tuple:
    """取 Coordinator 对外方法 name 上的装饰器标记（自外向内）；没有则空元组。

    走 `getattr(Coordinator, ...)`（属性查找）而不是 `vars()` —— 后者看不到 mixin
    继承来的方法，见 `_coordinator_methods` 的说明。
    """
    return tuple(getattr(getattr(Coordinator, name, None), "_cc_hooks", ()))





def _coordinator_methods() -> dict:
    """
    枚举 Coordinator 的**全部**方法，**含 mixin 继承来的**。

    ⚠️ 为什么不能再用 `vars(Coordinator)`（2026-09-18 拆包时实测到的事实）：
    Coordinator 现在是 `class Coordinator(CoordinatorBase, LandingMixin, ...)` 的
    mixin 组合，而 `vars(cls)` 只看**本类自己的 `__dict__`** —— 继承来的方法一个都
    看不到。拆包当天实测的后果：本文件三条防漏测试里，**两条静默变成空集合、全绿但
    零覆盖**（`missing` 恒为空），只有 `stale` 那条因为「豁免名单全成了死登记」而变红。
    也就是说，若无那条副作用式的检查，这次拆包会把三道守卫**全部废掉且不报任何错**
    —— 正是本项目反复强调的那类「判据松了却看不出来」的失效。

    实现：遍历 `Coordinator.__mro__`，按「基类 → 派生类」顺序写入，于是派生类的同名
    覆盖最后生效（与属性查找语义一致）。加新的 mixin 时本函数自动覆盖，无需再改。
    """
    out: dict = {}
    for klass in reversed(Coordinator.__mro__):
        out.update(vars(klass))
    return out





def test_every_public_coordinator_method_is_lazy_hooked():
    """
    结构性防漏：Coordinator 的**每个**对外方法都要挂 @_needs_display。

    理由：漏挂不会报错，只会让该工具静默回落宿主桌面（最危险的那类 bug）。新增对外
    方法时本测试会直接失败，逼你补钩子或显式登记豁免。

    ️ 判据是装饰器自己打的 `_cc_hooks` 标记，**不是** `hasattr(obj, "__wrapped__")`
    （I-11，见 docs/REVIEW/review_0.1.0.md）：两个装饰器都用 functools.wraps，留下的
    痕迹完全一样，旧判据只能分辨「一个装饰器都没挂」，分辨不了「挂错了一个」——实测
    只挂 @_exclusive_screen 的方法照样判通过，而漏挂 _needs_display 的后果（a11y 连接
    不对齐沙箱私有总线）正是本测试唯一的兜底职责。
    """
    exempt: set[str] = {
        # 只读**本进程内存**里的点击回看缓存（landing._CLICK_LOG），零屏幕交互：
        # 它既不读 a11y、也不碰注入，挂上钩子反而会让「看一眼上次点了哪」这种纯查内存
        # 的操作去拉起 Xephyr 沙箱（无端弹窗 + 白等启动）。与只读豁免同族。
        "get_last_click_image",
    }
    missing = [
        name for name, obj in _coordinator_methods().items()
        if not name.startswith("_") and callable(obj)
        and "needs_display" not in _hooks_of(name) and name not in exempt
    ]
    assert not missing, f"这些对外方法未挂 @_needs_display：{missing}"

    # 判据自身不能退化成「看有没有标记」：标记是装饰器打的，若两个装饰器打同一个标记，
    # 上面那条又等于什么都没测。这里正面钉住「两个标记能区分开」。
    assert _hooks_of("click_xy") == ("needs_display", "exclusive_screen"), \
        "click_xy 同时挂了两个装饰器，标记必须能分辨出它俩"





def test_exclusive_hook_is_always_inside_needs_display():
    """
    结构性防漏：挂了 @_exclusive_screen 的方法必须**同时**挂 @_needs_display，
    且 exclusive 必须在内侧（I-11）。

    顺序不是洁癖：`_exclusive_screen` 会抢屏锁，而 `_needs_display` 负责启动沙箱。
    顺序反了（或漏挂 needs_display），首次调用就会在**持锁状态下等 Xephyr 启动**
    （最长 10s），把并发的其它 agent 白白挡在门外；更糟的是沙箱还没起来，
    `_require_sandbox` 会直接把这次调用拒掉。
    """
    bad = []
    for name, obj in _coordinator_methods().items():
        if name.startswith("_") or not callable(obj):
            continue
        hooks = _hooks_of(name)
        if "exclusive_screen" not in hooks:
            continue
        if "needs_display" not in hooks or hooks.index("needs_display") > hooks.index("exclusive_screen"):
            bad.append((name, hooks))
    assert not bad, f"这些方法的 @_exclusive_screen 未挂在 @_needs_display 内侧：{bad}"





# ---------- 屏独占锁：并发子 agent 互斥，且必须「快速失败」而不是排队 ----------
def test_screen_lock_fast_fail_not_queue():
    """
    **另一个线程**拿不到时必须**立即**失败，不能排队等——排队会把「基于旧界面做的决策」
    延迟执行。（同线程重入是允许的，见 test_screen_lock_reentrant_same_thread）
    """
    import threading
    import time as _time
    lock = screen_lock.ScreenLock()
    assert lock.acquire("click_xy") is True

    got: list[bool] = []
    elapsed: list[float] = []

    def other_thread():
        t0 = _time.monotonic()
        got.append(lock.acquire("type_text"))
        elapsed.append(_time.monotonic() - t0)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(2.0)

    assert got == [False], "已被其它线程占用时必须返回 False"
    assert elapsed and elapsed[0] < 0.2, f"必须立即返回而非阻塞等待（实测 {elapsed}s）"
    lock.release()
    assert lock.acquire("type_text") is True   # 释放后别人能拿
    lock.release()





def test_screen_lock_reentrant_same_thread():
    """act_sequence 持锁跑整串，其中每步又进装饰器 → 同线程必须能重入。"""
    lock = screen_lock.ScreenLock()
    assert lock.acquire("act_sequence") is True
    assert lock.acquire("click_xy") is True      # 重入
    lock.release()                                # 内层释放
    assert lock.holder_info().startswith("「act_sequence」"), "内层释放不该清掉持有者"
    lock.release()                                # 外层释放
    assert lock.acquire("other") is True          # 真正释放后别人能拿
    lock.release()





def test_busy_message_tells_llm_to_wait_and_resense():
    """回给 LLM 的提示必须包含「等待」与「重新感知」，否则它会直接重放上一步。"""
    lock = screen_lock.ScreenLock()
    lock.acquire("click_xy")
    msg = lock.busy_message("type_text")
    lock.release()
    assert "已拒绝" in msg and "等待" in msg and "get_ui_tree" in msg





def test_concurrent_injection_gets_busy_error(monkeypatch):
    """两个线程同时注入：一个成功，另一个拿到 ScreenBusyError（提示屏被占用）。"""
    import threading
    import time as _time
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)  # 跳过沙箱闸门

    backend = _StubBackend()
    orig = backend.click_at

    def slow_click(x, y, **k):
        _time.sleep(0.4)
        return orig(x, y, **k)

    backend.click_at = slow_click
    coord = Coordinator(backend=backend)
    errors: list[str] = []

    def worker(px):
        try:
            coord.click_xy(px, 1)
        except ScreenBusyError as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=worker, args=(1,)); t1.start()
    _time.sleep(0.15)  # 确保 t1 已持锁进入注入
    t2 = threading.Thread(target=worker, args=(2,)); t2.start()
    t1.join(5); t2.join(5)

    assert len(errors) == 1, f"应恰好有一个被拒，实际 {len(errors)}"
    assert "已拒绝" in errors[0]
    assert [c for c in backend.calls if c[0] == "click"] == [("click", 1, 1)], \
        "被拒的那次绝不能真的点到屏上"





def test_act_sequence_holds_lock_for_whole_sequence(monkeypatch):
    """整串 act_sequence 期间屏被独占，别的 agent 不能在中间插进来。"""
    import threading
    import time as _time
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)

    coord = Coordinator(backend=_StubBackend())
    busy: list[str] = []
    done: list[bool] = []

    def seq():
        coord.act_sequence([{"op": "sleep", "seconds": 0.4},
                            {"op": "key", "combo": "ctrl+a"}])
        done.append(True)

    def intruder():
        _time.sleep(0.15)
        try:
            coord.click_xy(3, 4)
        except ScreenBusyError as exc:
            busy.append(str(exc))

    t1 = threading.Thread(target=seq); t1.start()
    t2 = threading.Thread(target=intruder); t2.start()
    t1.join(5); t2.join(5)
    assert done == [True]
    assert len(busy) == 1, "序列执行期间插入的调用应被拒"





# ---------- 沙箱闸门（B 防线）：绝不静默打到真实桌面 ----------
def test_require_sandbox_rejects_when_isolated_and_down(monkeypatch):
    """
    isolated 且沙箱不可用 → 抛 SandboxUnavailableError，**不执行注入**。
    这是本改动最核心的一条：以前这里会静默回落到用户真实桌面。
    """
    _reset_display(monkeypatch)
    monkeypatch.setattr(display_mod.shutil, "which", lambda name: None)  # 模拟没装 Xephyr
    coord = Coordinator(backend=_StubBackend())

    with pytest.raises(SandboxUnavailableError) as ei:
        coord.click_xy(10, 20)
    assert "已拒绝执行" in str(ei.value)
    assert coord.backend.calls == [], "被拒时绝不能落到 backend 上"





def test_require_sandbox_allows_real_mode(monkeypatch):
    """real 模式是用户显式要求操作真实桌面 → 放行（不算「偷偷」）。"""
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    coord = Coordinator(backend=_StubBackend())
    assert coord.click_xy(10, 20).ok is True





def test_require_sandbox_allows_when_up(monkeypatch):
    """沙箱就绪 → 放行。"""
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(display_mod.MANAGER, "is_sandbox_up", lambda: True)
    coord = Coordinator(backend=_StubBackend())
    assert coord.click_xy(10, 20).ok is True





# @_exclusive_screen 的**显式豁免登记**（I-12，见 docs/REVIEW/review_0.1.0.md）。
#
# 为什么是「枚举全部 + 登记例外」而不是历史那份 9 个方法名的清单：后者是**常量集合**，
# 只校验清单里那几个 —— 新增第 10 个注入类方法忘了挂装饰器，测试**不会失败**（它自己
# 的 docstring 就明说「漏挂 → 该工具既不互斥、也会静默打到真实桌面」）。实测那份清单还
# **当场就漏了一个**：`get_screen_text` 明明挂着装饰器却不在清单里，把它的装饰器摘掉
# 照样全绿。同一文件里 `test_every_public_coordinator_method_is_lazy_hooked` 用的反射
# 枚举才是这里该有的写法。
#
# 豁免的都是**只读**方法（不改屏状态）：加锁会让并发的感知类调用白白互斥。
# `wait_window` 还带 10s 轮询，持锁会白挡别人一整个轮询周期。
_EXCLUSIVE_EXEMPT: dict[str, str] = {
    "get_ui_tree": "只读 a11y 遍历，不改屏状态",
    "find_elements": "只读 a11y 搜索，不改屏状态",
    "get_element_info": "只读单个元素详情，不改屏状态",
    "list_windows": "只读 X11 窗口列表，不改屏状态",
    "wait_window": "只读轮询（最长 10s）——持锁会把别人白挡一整个轮询周期",
    "screen_layout": "只读显示器布局，不改屏状态",
    "get_last_click_image": "只读本进程内存里的点击回看缓存（landing._CLICK_LOG），"
                            "既不碰屏也不碰 a11y，加锁只会白挡并发的真实操作",
}





def test_sandbox_gate_applies_to_all_injection_methods():
    """
    结构性防漏：所有会改变屏状态/受并发影响的方法都必须挂 @_exclusive_screen
    （漏挂 → 该工具既不互斥、也会静默打到真实桌面）。

    判据是**反射枚举** Coordinator 的全部对外方法，而不是手写清单（I-12）。
    """
    missing = [
        name for name, obj in _coordinator_methods().items()
        if not name.startswith("_") and callable(obj)
        and "exclusive_screen" not in _hooks_of(name)
        and name not in _EXCLUSIVE_EXEMPT
    ]
    assert not missing, (
        f"这些对外方法未挂 @_exclusive_screen（新方法请补装饰器；确实只读请在 "
        f"_EXCLUSIVE_EXEMPT 里登记并写明理由）：{missing}"
    )

    # 豁免名单本身也要被守：① 登记了却其实挂着锁 —— 等于悄悄放弃了这条覆盖；
    # ② 登记了一个不存在的方法（改名/删除后没同步）—— 一条死的豁免会误导后来者。
    redundant = [n for n in _EXCLUSIVE_EXEMPT if "exclusive_screen" in _hooks_of(n)]
    assert not redundant, f"这些方法已挂 @_exclusive_screen，不该再登记为豁免：{redundant}"
    stale = [n for n in _EXCLUSIVE_EXEMPT if n not in _coordinator_methods()]
    assert not stale, f"这些豁免登记指向不存在的方法（改名/删除后请同步）：{stale}"





# ---------- RefTable 并发安全 ----------
def test_ref_table_concurrent_register_no_duplicate_refs():
    """
    并发 register 不得串号：无锁时 `ref = _next; _next += 1` 非原子，
    两个线程可能拿到同一个 ref 号，表现为「按 ref 点击却点到别的元素」。
    """
    import threading
    from computer_use_mcp.utils.refs import RefTable

    table = RefTable()
    refs: list[int] = []
    guard = threading.Lock()

    def worker(base):
        local = []
        for i in range(200):
            local.append(table.register(object(), name=f"{base}-{i}"))
        with guard:
            refs.extend(local)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(refs) == 1200
    assert len(set(refs)) == 1200, "ref 必须全局唯一（出现重复即串号）"
