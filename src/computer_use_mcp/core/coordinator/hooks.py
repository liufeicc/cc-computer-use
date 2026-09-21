"""
Coordinator 的两个装饰器与它们的顺序标记（core.coordinator.hooks）。

这两个装饰器是**全局性约定**的落点，不是普通工具函数：
  - `@_needs_display`：惰性沙箱启动 + a11y 连接对齐（**每个**对外方法都必须挂）；
  - `@_exclusive_screen`：屏独占锁 + 沙箱闸门（注入类方法才挂，且必须在 needs_display **内侧**）。

漏挂不会报错，只会静默回落宿主桌面 / 静默丢互斥——所以有两条结构性守卫测试盯着
（`tests/test_optimizations.py` 的三个 test_*hook* / *gate*）。改这里的任何约定，
同步改那三条测试的判据。
"""

from __future__ import annotations

import functools
from typing import Any

from ...utils.errors import SandboxUnavailableError, ScreenBusyError
from ...utils.logging import get_logger
from .. import display, screen_lock

# ⚠️ 用**固定名字**取 logger（而不是 `__name__`）：本包拆分自单文件
# `core/coordinator.py`，固定名字让所有子模块**共用同一个 logger 对象**（日志名与
# 拆分前一致），也让针对 `coordinator.log` 的 monkeypatch 对全部子模块生效。
log = get_logger("computer_use_mcp.core.coordinator")


def _mark(wrapper, name: str, fn) -> Any:
    """
    给装饰器产物打标记 `_cc_hooks`，**按「自外向内」记录装饰器顺序**。

    为什么需要它（I-11，见 docs/REVIEW/review_0.1.0.md）：两个装饰器都用
    `functools.wraps`，于是在方法身上留下的痕迹**完全一样**（都只有 `__wrapped__`）。
    而结构性防漏测试原先的判据正是 `not hasattr(obj, "__wrapped__")`，它只能分辨
    「一个装饰器都没挂」，分辨不了「挂错了一个」——实测：只挂 `@_exclusive_screen`、
    完全没挂 `@_needs_display` 的方法，照样判通过。而漏挂 `_needs_display` 的后果
    （a11y 连接不对齐沙箱私有总线）恰恰是 CLAUDE.md 点名的「最危险的那类静默失效」，
    这条测试又是它唯一的兜底。

    为什么是一个元组而不是两个布尔标记：方法上**同时**需要「挂了哪些」和「谁在外」
    两条信息。`@_exclusive_screen` 必须挂在 `@_needs_display` **内侧**（先启沙箱再抢
    屏锁，否则首次调用会在持锁状态下等 Xephyr 启动、把别的 agent 白白挡在门外），
    这条顺序要求在 docstring 里写着、却一直没有任何测试守着。用元组把顺序直接编码进
    标记，两条信息一次拿全，测试里 `hooks[0]` 就是最外层那个装饰器。

    用 `getattr(fn, ...)` 显式读取内层标记，而不依赖 `functools.wraps` 会复制
    `__dict__` 这一实现细节 —— 后者是「碰巧对」：换个写法（或有人给 wraps 传了别的
    updated）就静默失效。
    """
    wrapper._cc_hooks = (name,) + tuple(getattr(fn, "_cc_hooks", ()))
    return wrapper


def _needs_display(fn):
    """
    惰性沙箱启动的挂载点（实现见 core/display.DisplayManager.ensure_started）。

    为什么钩在这里：MCP server 由 Claude Code 在**会话启动时就常驻拉起**（stdio 进程）。
    若在 server.main() 里启沙箱，则每次开 Claude 都弹出一个虚拟屏——哪怕整场会话一次
    都没用过本工具。而 tools/ 只做参数解析、**所有**工具调用都收敛到 Coordinator 的
    对外方法，故这里是「用户真的用到了本 MCP」的唯一入口，且新增工具不会漏挂。

    必须覆盖到**看起来只读**的方法：get_ui_tree 默认 scope=active_window 会经
    inject.active_window_title/active_window_pid 读 X11，若沙箱尚未启动就会读到宿主的
    活动窗口，而随后的 click 落在沙箱——感知与操作分属两个 display，是极隐蔽的不一致。

    不吞异常也不加 try：ensure_started 自身只告警不抛（启动失败即回落宿主并告警一次），
    保证「沙箱问题」不会让工具整体不可用。
    """
    @functools.wraps(fn)
    def wrapper(self: "Any", *args: Any, **kwargs: Any) -> Any:
        display.MANAGER.ensure_started()
        # 沙箱起来后把 a11y 连接对齐到沙箱私有 AT-SPI 总线（顺序不能反：总线随沙箱建，
        # 且 libatspi 的 init 是一次性的，必须赶在第一次真正用 a11y 之前）。
        # 廉价：两次字符串比较；非 Linux backend 是 no-op。
        try:
            self.backend.sync_at_spi_bus()
        except Exception as exc:  # noqa: BLE001
            log.debug("sync_at_spi_bus 失败（忽略，不影响主流程）: %s", exc)
        return fn(self, *args, **kwargs)

    return _mark(wrapper, "needs_display", fn)


def _exclusive_screen(fn):
    """
    屏独占 + 沙箱闸门（并发子 agent 互斥 / 绝不静默打到真实桌面）。

    两个职责，都在**注入真正落地之前**把关：

    ① 屏独占（选项1）：同一 Claude 会话的主 agent 与所有子 agent 共用同一个 MCP server
       进程，因而共用一块虚拟屏、一个 X11 指针/焦点、一份剪贴板与一张 ref 表。两个子
       agent 同时操作会互相破坏。这里用**非阻塞**抢锁，抢不到就抛 ScreenBusyError ——
       **不排队**。理由见 ScreenBusyError 的 docstring：排队会把"基于旧界面做的决策"
       延迟执行，产出"看起来成功实则打偏"的结果。
       锁可重入（同线程）：act_sequence 持锁跑完整串步骤，其中每步又进这里。

    ② 沙箱闸门（方案B）：isolated 模式下沙箱不在就拒绝注入，见 _require_sandbox。
       少了这一层，effective_display() 会**静默回落宿主桌面**，而 LLM 只看得到
       "操作成功"，会继续在你的真实桌面上点下去。

    装饰器顺序要求：必须挂在 @_needs_display **内侧**（写在它下面），
    即先确保沙箱启动、再抢屏锁——否则首次调用会在持锁状态下等 Xephyr 启动（最长 10s），
    把其它 agent 白白挡在门外。
    """
    @functools.wraps(fn)
    def wrapper(self: "Any", *args: Any, **kwargs: Any) -> Any:
        op = fn.__name__
        lock = screen_lock.SCREEN_LOCK
        if not lock.acquire(op):
            raise ScreenBusyError(lock.busy_message(op))
        try:
            self._require_sandbox(op)
            return fn(self, *args, **kwargs)
        finally:
            lock.release()

    return _mark(wrapper, "exclusive_screen", fn)