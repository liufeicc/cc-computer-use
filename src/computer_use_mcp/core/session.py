"""
会话与隔离的统一接口层（core.session）—— 层 3。

═══ 为什么需要这一层 ═══

本项目的 `core/` 里**到处都是隐式的 Linux 假设**：`grep platform.system` 一条都搜不到，
但 `core/display/`（Xephyr 虚拟屏 + 私有 AT-SPI 总线）是 Linux 硬写，
`core/coordinator/` 又直接 import 它。加 Windows/macOS 时，每加一个平台就要在 `core/`
里做一次「考古式」的逐处修改。

本模块把那套隐式假设收敛成**一处显式分支**：平台差异只允许出现在
  1. `backend/<platform>/` 之内，
  2. `core/session.py` 的这个分支，
  3. `backend/base.py` 的 `get_backend()` 工厂。
第三处出现 `platform.system()` 就是 bug，由 `tests/test_platform_module_boundary.py` 兜住。

═══ 三层的分工（别混）═══

    层 1  backend/base.py::Backend     「这个平台**能**做什么」——能力，进程级固定
    层 2  backend/common/              「哪些逻辑与平台无关」——纯函数
    层 3  本模块 :: SessionProvider    「这次操作**落在哪个会话**、**代价是什么**」——策略，会变

同一个 Windows 后端既可能跑在「用户桌面 + 进程白名单」模式，也可能跑在「隐藏桌面」模式，
所以「隔离策略」不能塞进 `Backend`——那会把平台能力与会话策略搅在一起。

═══ 本轮的实现面 ═══

只做了 Linux（Xephyr 那套，**同一个对象**，见下）与 `NoIsolationSession`（非 Linux 的
诚实兜底）。Windows/macOS 的具体 provider 在后续轮次填，接口形状已经容得下。

⚠️ `SessionCapabilities` / `SessionProvider` 定义在 `core/session_types.py`（叶模块），
这里只做再导出 —— 原因（循环导入与 import 顺序依赖）见那个模块的 docstring。
"""

from __future__ import annotations

import platform
from typing import Any

from .session_types import (  # noqa: F401  —— 再导出，调用方一律 `from .. import session`
    OP_CAPTURE,
    OP_INJECT,
    OP_READ,
    SessionCapabilities,
    SessionProvider,
)

# 平台判定：全仓**只有本文件与 backend/base.py** 允许出现 `platform.system()`。
# 其它模块（含 `_bootstrap.py`）要判断平台，一律消费本常量——这样「平台分支在哪」
# 永远只有一个答案，而不是靠 grep 去找散落的 `if platform`。
IS_LINUX = platform.system().lower() == "linux"


class NoIsolationSession:
    """
    无隔离会话：`real` 模式的实现，也是「该平台尚无隔离实现」时的**诚实兜底**。

    ⚠️ 它的 `capabilities()` 把三项隔离全报 False、并把 `isolation` 记进
    `unimplemented` —— 这不是偷懒，是**本模块存在的意义**：
    在没有隔离的平台上假装有隔离，会让用户以为「agent 在用另一个桌面操作」，
    而实际上它正在动他的鼠标。宁可少一项能力，不可报错状态。
    """

    unimplemented: tuple[str, ...] = ("isolation",)

    def kind(self) -> str:
        return "real"

    def capabilities(self) -> SessionCapabilities:
        return SessionCapabilities(
            target="real",
            input_isolated=False,
            screen_isolated=False,
            a11y_isolated=False,
            strong_mode=False,
            unimplemented=self.unimplemented,
            notes=("本平台暂无隔离实现：agent 的操作直接落在你正在使用的桌面上。",),
        )

    def ensure_ready(self) -> dict:
        # 没有要准备的东西——不启虚拟屏、不建总线。返回 describe() 让调用方有个
        # 统一的「准备结果摘要」形状可以用。
        return self.describe()

    def is_ready(self) -> bool:
        # 没有隔离环境要等，任何时候都能干活。⚠️ 注意这不等于"隔离好了"——
        # 本平台根本没有隔离，那件事由 capabilities() 如实申报（三项全 False）。
        return True

    def require(self, op: str, *, kind: str = OP_INJECT) -> None:
        # 不隔离 ⇒ 没有可把的闸门。**但这不是"无所谓"**：真实代价已经由
        # capabilities() 如实申报出去，调用方（工具描述 / 模型）据此决定要不要做。
        # 在这里再抛错只会让本平台整体不可用，而用户可能正是要这样用。
        return None

    def guard(self) -> str | None:
        # 不隔离 ⇒ 用户就在同一个环境里，「礼让」无从谈起（没有"另一个桌面"可退让）。
        return None

    def env_for(self, display: str | None = None) -> dict[str, str]:
        """不换目标屏，但仍要**剥掉** Linux 专属的残留变量。

        ⚠️ 这里刻意不走 `dict(os.environ)` 原样返回：本项目的子进程环境出口有一条
        硬规则——不该继承的东西必须主动剥（`AT_SPI_BUS_ADDRESS` 与冻结产物的
        `LD_LIBRARY_PATH` 都出过实测事故）。在非 Linux 上它们通常本就不存在，
        但「通常不存在」不是不剥的理由（可能是从别的会话继承来的）。
        """
        import os

        env = dict(os.environ)
        for key in ("AT_SPI_BUS_ADDRESS", "GI_TYPELIB_PATH"):
            env.pop(key, None)
        return env

    def app_env(self, display: str | None = None) -> dict[str, str]:
        """启动应用用的 env。本实现下与 `env_for` 相同——**只多一件事**那条规则里，
        这里"多的那件事"是空的（没有要注入的目标会话变量）。"""
        return self.env_for(display)

    def target_label(self) -> str:
        return "real"

    def describe(self) -> dict:
        caps = self.capabilities()
        return {
            "kind": self.kind(),
            "target": caps.target,
            "input_isolated": caps.input_isolated,
            "screen_isolated": caps.screen_isolated,
            "a11y_isolated": caps.a11y_isolated,
            "unimplemented": list(caps.unimplemented),
        }

    def stop(self) -> None:
        return None


def _make_manager() -> SessionProvider:
    """挑本次会话的 provider —— **全仓唯一的平台分支点之一**。

    ⚠️ Linux 分支返回的是 `display.MANAGER` **本体**，不是包装器。
    理由（完整版见 `core/display/provider.py` 的模块 docstring）：
    包装器会让 `monkeypatch.setattr(MANAGER, "<成员>", …)` 这类补丁落到包装器自己身上，
    而真实 MANAGER 没变 —— **前提没生效却不报错**，全仓 55 处打桩点会静默失效。
    """
    if IS_LINUX:
        from . import display  # 延迟到函数内：非 Linux 上完全不碰 X11 那套

        return display.MANAGER
    return NoIsolationSession()


# 模块级单例。`MANAGER` 这个名字是刻意保留的——coordinator 与 server 原本写的是
# `display.MANAGER`，改用 session 后仍叫 `MANAGER`，语义（"本次会话的那一个"）不变。
MANAGER: SessionProvider = _make_manager()


def capabilities() -> SessionCapabilities:
    """本次会话的隔离能力申报（`MANAGER.capabilities()` 的便捷入口）。"""
    return MANAGER.capabilities()


def describe() -> dict[str, Any]:
    """状态摘要（`--selftest` / 诊断用）。"""
    return MANAGER.describe()
