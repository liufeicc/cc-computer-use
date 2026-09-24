"""
会话接口层（core.session）的契约守卫。

这层是加 Windows/macOS 支持的**地基**：把原先散落在 `core/` 里的隐式 Linux 假设
收敛成一处显式分支。地基一旦悄悄变形（比如 Linux 分支从"同一个对象"退化成"包装器"），
上层不会报错、只会成片静默失效——所以这里的判据全部盯着**那条不变量本身**，
而不是"接口长什么样"。

判据纪律（本项目的血证：拆 Coordinator 成 mixin 那次，三条反射守卫里两条静默变成
空集合、全绿但零覆盖）：每条断言都要能在**改坏时真的变红**，且不能有空转可能。
"""

from __future__ import annotations

import pytest

from computer_use_mcp.core import display as display_mod
from computer_use_mcp.core import session
from computer_use_mcp.core.session_types import (
    OP_CAPTURE,
    OP_INJECT,
    OP_READ,
    SessionCapabilities,
)


# ==================== 最重要的一条：MANAGER 必须是同一个对象 ====================

def test_session_manager_is_the_same_object_as_display_manager():
    """
    ★ `session.MANAGER` 与 `display.MANAGER` 必须是**同一个对象**，不是包装器。

    为什么这是全层最要紧的不变量：全仓约 55 处测试补丁是
    `monkeypatch.setattr(<MANAGER>, "<成员>", …)` 的形状
    （`tests/_helpers.py` 15 个私有字段、`test_display_lifecycle.py` 21 处、
    `test_f_guards.py` 14 处、`test_h_guards.py` 6 处，还有 test_g_guards 的 M-43 两条）。

    换成"持有 MANAGER 的包装器类"后，`setattr` 会落在**包装器自己**身上、真实 MANAGER
    纹丝不动 —— 于是测试的**前提没生效却不报错**，用例会走错分支、或在错误的假设下
    继续通过。那正是本项目最忌讳的失效形态（判据还在、测的已经不是那件事了）。

    ⚠️ 判据用 `is` 而不是"方法能跑通"：包装器把方法转发得再对，`is` 也是 False。
    """
    assert session.IS_LINUX, "本用例的前提是跑在 Linux 上（CI 与开发机都是）"
    assert session.MANAGER is display_mod.MANAGER, (
        "session.MANAGER 不再是 display.MANAGER 本体 —— 全仓 55 处 monkeypatch 会静默打空。"
        "见 core/display/provider.py 的模块 docstring。"
    )


def test_patching_session_manager_reaches_the_machinery(monkeypatch):
    """
    加固上面那条：不只比对象，还**实际打一次补丁**看它是否生效。

    为什么不能只留 `is` 那条：`is` 挡得住"换成包装器"，但挡不住"将来有人给
    session 加一个 `MANAGER` 的 property/setter 间接层"——那种改动同样会让补丁打偏，
    而 `is` 仍可能是 True。这里直接把会话状态改成 isolated + 沙箱不在，断言
    `capabilities()` 真的反映出那个状态。
    """
    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(session.MANAGER, "is_sandbox_up", lambda: False)

    caps = session.MANAGER.capabilities()
    assert caps.target == "isolated"
    # 沙箱不在 ⇒ 输入/屏幕都没有隔离（虽然注入会被拒绝执行，但那是"拒绝"不是"隔离"）
    assert caps.input_isolated is False
    assert caps.screen_isolated is False
    assert caps.strong_mode is False
    assert caps.notes, "沙箱不在时必须给出说明，不能静默"


# ==================== 能力申报的三态 ====================

def test_capabilities_real_mode_declares_no_isolation(monkeypatch):
    """real 模式：三样隔离全无，且必须**说出来**（不能让人以为还有隔离）。"""
    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_REAL)

    caps = session.MANAGER.capabilities()
    assert caps.target == "real"
    assert (caps.input_isolated, caps.screen_isolated, caps.a11y_isolated) == (False, False, False)
    assert caps.notes and "真实桌面" in caps.notes[0]


def test_capabilities_isolated_and_up_declares_three_way_isolation(monkeypatch):
    """isolated 且沙箱 + 私有总线都在：三样隔离成立，`strong_mode` 为真。"""
    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(session.MANAGER, "is_sandbox_up", lambda: True)
    monkeypatch.setattr(session.MANAGER, "sandbox_at_spi_bus", lambda: "unix:path=/tmp/x/bus")

    caps = session.MANAGER.capabilities()
    assert caps.target == "isolated"
    assert (caps.input_isolated, caps.screen_isolated, caps.a11y_isolated) == (True, True, True)
    assert caps.strong_mode is True


def test_capabilities_reports_a11y_false_on_dead_bus(monkeypatch):
    """
    沙箱在、但私有总线没绑上 ⇒ **a11y 隔离必须报 False**。

    为什么单列一条：这种局面下沙箱内其实没有 a11y 能力（只剩坐标点击 + OCR），
    宿主是安全的——但"宿主安全"不等于"隔离做到了"。报 True 会让用户以为
    无障碍通道也在隔离环境里跑，从而误判 agent 能看到什么。
    """
    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(session.MANAGER, "is_sandbox_up", lambda: True)
    monkeypatch.setattr(session.MANAGER, "sandbox_at_spi_bus",
                        lambda: display_mod._DEAD_AT_SPI_BUS)

    caps = session.MANAGER.capabilities()
    assert caps.a11y_isolated is False
    assert caps.input_isolated is True, "总线死了不影响输入/屏幕隔离，别一并报 False"
    assert caps.notes and "总线" in caps.notes[0]


# ==================== 闸门语义 ====================

def test_require_blocks_when_isolated_and_sandbox_down(monkeypatch):
    """isolated + 沙箱不在 ⇒ **抛错拒绝**（绝不静默回落宿主桌面）。"""
    from computer_use_mcp.utils.errors import SandboxUnavailableError

    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(session.MANAGER, "is_sandbox_up", lambda: False)
    monkeypatch.setattr(session.MANAGER, "ensure_started", lambda: {"stub": True})

    with pytest.raises(SandboxUnavailableError) as ei:
        session.MANAGER.require("click_xy")
    assert "已拒绝执行" in str(ei.value)
    assert "click_xy" in str(ei.value), "错误文案必须带上是哪个 op（否则排查时不知道谁被拦了）"


def test_require_passes_in_real_mode(monkeypatch):
    """real 模式：用户显式要求操作真实桌面，闸门放行。"""
    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_REAL)
    session.MANAGER.require("click_xy")   # 不抛即通过


def test_is_ready_semantics(monkeypatch):
    """`is_ready` 回答「现在能不能用」：real 恒真；isolated 看沙箱在不在。"""
    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_REAL)
    assert session.MANAGER.is_ready() is True

    monkeypatch.setattr(session.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(session.MANAGER, "is_sandbox_up", lambda: False)
    assert session.MANAGER.is_ready() is False

    monkeypatch.setattr(session.MANAGER, "is_sandbox_up", lambda: True)
    assert session.MANAGER.is_ready() is True


# ==================== 常量与默认值（防被改松）====================

def test_op_kinds_are_distinct():
    """
    三个 op 种类必须两两不同。

    若有人把它们写重（比如都把 OP_READ 赋成 "inject"），闸门就再也分不出"只读"与
    "会动输入"——而那种改动**不会有任何测试变红**，只会让只读操作也去抢屏锁。
    """
    assert len({OP_INJECT, OP_CAPTURE, OP_READ}) == 3


def test_require_defaults_to_inject():
    """
    `require` 的 `kind` 默认值必须是 `OP_INJECT`（最保守那一档）。

    为什么钉默认值：`@_exclusive_screen` 的调用点不传 `kind`，靠的就是这个默认值。
    改成 `OP_READ` 会让所有注入方法**默认走只读档**，闸门形同虚设——而且没有任何
    调用点需要改，静默生效。
    """
    import inspect

    sig = inspect.signature(session.SessionProvider.require)
    assert sig.parameters["kind"].default == OP_INJECT


def test_capabilities_is_frozen_and_defaults_are_conservative():
    """
    `SessionCapabilities` 必须不可变，且可选字段默认「最保守」。

    不可变的理由：它是要上报给用户的契约快照。可变的话，某处代码顺手改一个字段
    就能让"已经报出去的隔离状态"变成另一回事，而调用方毫不知情。
    """
    caps = SessionCapabilities(target="real", input_isolated=False, screen_isolated=False,
                               a11y_isolated=False, strong_mode=False)
    with pytest.raises(Exception):        # frozen dataclass → FrozenInstanceError
        caps.input_isolated = True        # type: ignore[misc]
    assert caps.unimplemented == ()
    assert caps.notes == ()


def test_linux_backend_actually_implements_liveness():
    """
    `Backend.liveness()` 的默认值是 `False`（说不清就不声称可用）——所以 Linux 侧
    **必须真的重写它**，否则自检会在"沙箱没起来"这条分支上永远判死。

    这条守的是「默认值把实现吃掉」：有了默认实现之后，谁把 `LinuxBackend.liveness`
    删掉都不会报错，只是自检从此说假话 —— 而"少了一个方法"是重构时最容易顺手删的东西。
    """
    from computer_use_mcp.backend.base import Backend
    from computer_use_mcp.backend.linux.backend import LinuxBackend

    assert LinuxBackend.liveness is not Backend.liveness, (
        "LinuxBackend 没有重写 liveness —— 它会继承默认值 False，自检会永远判死"
    )
    # 行为判据（比"是不是同一个函数对象"强）：它必须**真的去问 X11 通道**。
    # 只判 `is not` 的话，把实现改成 `return super().liveness()` 照样能通过 ——
    # 而那恰恰是"实现被悄悄摘掉"最常见的写法。
    class _StubInjector:
        def __init__(self, v): self.v = v
        def probe_liveness(self): return self.v

    be = LinuxBackend.__new__(LinuxBackend)
    for value in (True, False):
        be.injector = _StubInjector(value)
        assert be.liveness() is value, (
            "liveness() 没有转发到 injector.probe_liveness() —— 判活通道已失效"
        )
    # 正控：默认实现确实返回 False。传一个裸 object 即可 —— 默认实现不碰 self，
    # 而抽象基类本身无法实例化（这正是"必须有默认实现"那条约束的来源）。
    assert Backend.liveness(object()) is False, (
        "默认值不再是 False —— 那么上面那条『两边不同』的判据就证明不了什么了"
    )
