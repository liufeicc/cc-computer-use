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

Linux（Xephyr 那套，**同一个对象**，见下）、Windows（隐藏桌面，见
`WindowsDesktopSession`），以及其它平台的 `NoIsolationSession`（诚实兜底）。

⚠️ `SessionCapabilities` / `SessionProvider` 定义在 `core/session_types.py`（叶模块），
这里只做再导出 —— 原因（循环导入与 import 顺序依赖）见那个模块的 docstring。
"""

from __future__ import annotations

import sys
from typing import Any

from .session_types import (  # noqa: F401  —— 再导出，调用方一律 `from .. import session`
    COORDINATE_INPUT,
    OP_CAPTURE,
    OP_INJECT,
    OP_INJECT_GLOBAL,
    OP_READ,
    SessionCapabilities,
    SessionProvider,
    display_mode_from_env,
    global_input_allowed,
    global_input_note,
    global_input_refusal,
)
from ..utils.errors import GlobalInputRefusedError

# 平台判定：全仓**只有本文件与 backend/base.py** 允许做平台分支。
# 其它模块（含 `_bootstrap.py`）要判断平台，一律消费本常量——这样「平台分支在哪」
# 永远只有一个答案，而不是靠 grep 去找散落的 `if platform`。
#
# ⚠️ 用 `sys.platform`（编译期常量）而**不是** `platform.system()` —— 后者在 Windows 上
# 是一次 **WMI 查询**（`platform.py::_get_machine_win32` → `_wmi_query` → `_wmi.pyd` 的 COM 调用）。
# 实测（2026-10-02，本客体，Python 3.12.10）：全新进程里它要 **3.0~4.0 秒**；更糟的是
# 真机 story 里栈直接停在这一行**超过 90 秒**（看门狗打出来的）——与 UIA 那次
# `CoCreateInstance` 是同一种「本进程首次 COM 激活偶发阻塞数分钟」的客体病态。
# 而这两行是**模块级**语句：每一个 import 到 `core.session` 的进程都要先付这笔钱，
# MCP server 冷启动也不例外。`sys.platform` 不可能阻塞，且对本项目要回答的问题完全等价。
IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"


def _stripped_env() -> dict[str, str]:
    """本平台不换目标会话，但仍要**剥掉** Linux 专属的残留变量。

    ⚠️ 刻意不走 `dict(os.environ)` 原样返回：本项目的子进程环境出口有一条硬规则
    ——不该继承的东西必须主动剥（`AT_SPI_BUS_ADDRESS` 与冻结产物的 `LD_LIBRARY_PATH`
    都出过实测事故）。在非 Linux 上它们通常本就不存在，但「通常不存在」不是不剥的
    理由（可能是从别的会话继承来的）。

    抽成模块级函数而不是复制两份：`NoIsolationSession` 与 `WindowsDesktopSession`
    的这条规则**必须逐字相同** —— 复制一份就迟早有一份改漏，而漏掉的那次不会有任何症状，
    只会在某台机器上把宿主的 a11y 总线地址带进沙箱应用里。
    """
    import os

    env = dict(os.environ)
    for key in ("AT_SPI_BUS_ADDRESS", "GI_TYPELIB_PATH"):
        env.pop(key, None)
    return env


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

    def global_input_note(self) -> str | None:
        """
        恒为 None。

        ⚠️ 这**不是**"少写了一句提醒"：本实现下压根没有隔离，agent 的每一步都落在
        用户自己的桌面上 —— 那件事已经由 `capabilities()` 的三项全 False **一次性**
        申报过了。每次注入结果里再喊一遍，只会让这句话变成每次响的背景噪音，
        而模型的注意力是有限的：它会在最该注意的那一次也忽略掉。
        """
        return None

    def startup_caveat(self) -> str | None:
        """
        恒为 None：本实现下"没有隔离"这件事已经由 `capabilities()` 三项全 False 申报过，
        本平台也没有其它**稳定**的能力边界要说（见 Protocol 对"必须稳"的说明）。
        """
        return None

    def env_for(self, display: str | None = None) -> dict[str, str]:
        """不换目标屏，但仍要剥掉 Linux 专属残留（见 `_stripped_env`）。"""
        return _stripped_env()

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


class WindowsDesktopSession:
    """
    Windows 隐藏桌面会话（层 3）—— 与 `NoIsolationSession` 的关键区别是**真的有隔离**。

    ⚠️ **本类刻意不 import 任何 backend**，只读两个环境变量：
    `CC_CU_DISPLAY_MODE`（模式）与 `CC_CU_ALLOW_GLOBAL_INPUT`（是否显式放开全局注入）。
    两条判据都由 `core/session_types.py` 里的**纯函数**给出，而
    `backend/windows/driver/desktop.py` 用的是**同一个函数** —— 所以不存在"两份真值"。
    要是反过来去 import `backend.windows.driver.desktop` 拿状态，会同时踩两个坑：
    ① `core/` 不许 import `backend.<平台>.*`（`test_platform_module_boundary.py` 兜住）；
    ② 成环 —— session 挑 provider 的时候就要认识 desktop 的状态。

    ═══ 三项隔离在 Windows 上分别是什么 ═══

      · `screen_isolated` —— 应用起在隐藏桌面上（`CreateDesktopW`），用户屏幕上不会
        冒出任何窗口。**已真机验证**（`manual_windows_isolated_story.py` 13/13）。
      · `a11y_isolated` —— UIA 是**按目标进程**的跨进程调用，不存在"一条总线把所有应用
        连起来"的结构（Linux 的 AT-SPI 才是那样，所以那边要靠私有总线补）。天然为真。
      · `input_isolated` —— **取决于策略**，见下。这一项是本类存在的理由。

    ═══ `input_isolated` 为什么是 True（而不是保守地报 False）═══

    `SessionCapabilities.input_isolated` 的判据是「**存在性**而非概率：只要还有任何一条
    会动真实指针/抢真实焦点的回退路径，就必须申报 False」。隔离模式下坐标点击/键盘注入
    的那些路径**现在是拒绝执行**（`require` + backend 的同一条判据），而不是"存在但危险"
    —— 入口被封死了，判据因此为真。

    ⚠️ 但用户把 `CC_CU_ALLOW_GLOBAL_INPUT=1` 打开之后，那些入口**真的会执行**，
    于是立刻退回 False，并改用 `notes` 大声说明。这不是"我们不实现"，而是
    **"本平台实现不了、只能靠用户授权绕过"** —— 报成 False 才不会让用户以为
    他待在另一个桌面里。宁可保守（少用一项能力），绝不乐观。

    ⚠️ `input_isolated` 与 `unimplemented` 是**两个轴**：严格档下前者为 True（用户安全）
    而后者列着 `coordinate_input`（我们做不了这件事）；放开档下两者一起翻转。
    别以为其中一个写反了 —— 没有"能力又全、又安全"的那一档，那正是这条能力边界的含义。
    """

    def kind(self) -> str:
        return display_mode_from_env()

    def _isolated(self) -> bool:
        return display_mode_from_env() == "isolated"

    def capabilities(self) -> SessionCapabilities:
        iso = self._isolated()
        # ⚠️ 每次现算（不缓存）：用户可能刚改过环境变量，而"能力申报"是给用户/模型看的
        #    契约，读到旧值等于告诉他一个已经不成立的现状。
        allowed = global_input_allowed()
        notes: list[str] = []
        if iso:
            notes.append(
                "隔离模式（Windows 隐藏桌面）：agent 启动的应用开在一张你看不见的桌面上，"
                "你的键鼠、前台、屏幕上的窗口都不受影响。"
                "代价是**你看不到 agent 在操作什么** —— 要确认界面请让它回截图。"
            )
            if allowed:
                notes.append(
                    "⚠️ CC_CU_ALLOW_GLOBAL_INPUT=1 已开启：坐标级点击/键盘注入会绕过闸门，"
                    "**真的动你的鼠标、键盘与前台焦点**。要恢复隔离请去掉这个变量并重启。"
                )
            else:
                notes.append(
                    "坐标级点击/滚动/拖拽/键盘注入在隔离模式下**默认被拒绝**"
                    "（隐藏桌面上没有输入通道，硬发只会打到你的桌面上）。"
                    "请改用元素级（ref / set_value）；没有无障碍树的应用在隔离模式下不支持。"
                )
        return SessionCapabilities(
            target="isolated" if iso else "real",
            input_isolated=iso and not allowed,
            screen_isolated=iso,
            a11y_isolated=iso,
            # Windows 能提供的最强隔离形态就是隐藏桌面（没有更强的），故 isolated 即 strong。
            strong_mode=iso,
            # ⚠️ 这两个 `unimplemented` 说的是**两个不同的轴**，别以为哪一个写反了：
            #   · 严格档：用户的键鼠**绝对安全**（input_isolated=True），代价是
            #     「坐标级/键盘注入」这一项能力在隔离会话里**没有** —— 报的是**我们做不了什么**；
            #   · 放开档：能力有了（故 unimplemented 为空），代价是用户的安全没了
            #     （input_isolated=False）—— 报的是**他失去了什么**。
            #   两个轴各自如实，合起来才是一句完整的话：没有"能力又全、又安全"的那一档。
            unimplemented=(COORDINATE_INPUT,) if (iso and not allowed) else (),
            notes=tuple(notes),
        )

    def ensure_ready(self) -> dict:
        """
        没有要"等"的东西 —— 隐藏桌面由 backend 在**构造时**建好
        （见 `WindowsBackend.__init__`；比惰性建更可靠，理由写在那里）。

        返回 `describe()` 让调用方拿到与别的 provider 同形状的"准备结果摘要"。
        """
        return self.describe()

    def is_ready(self) -> bool:
        """
        恒为 True。**理由必须讲清，否则它看起来像一句敷衍**：

        Linux 那边这个方法是「沙箱起来了没」—— 因为 Linux 的 a11y 会在沙箱未起时
        落到**宿主总线**上（能把 GNOME Shell 打崩，所以自检必须据此绕开 AT-SPI）。
        Windows 上不存在那条路：UIA 按目标进程访问，没有"共享一条总线"的结构。

        那"隔离桌面没建出来"谁来管？两处**它真正的消费点**：
          · `spawn_app` —— 拒绝启动并如实报错（绝不落到用户桌面上）；
          · `enumerate_target` —— 返回空，而不是退回 `EnumWindows` 去看用户的窗口。
        这两个才是判据的落点；在这里再答一次只会多一份会过期的副本。
        """
        return True

    def require(self, op: str, *, kind: str = OP_INJECT) -> None:
        """
        闸门：**按操作种类分档**（`kind` 这个参数就是为这一档设计的）。

        · `kind=OP_INJECT`（默认，所有挂 `_exclusive_screen` 的方法）→ **放行**。
          判据不在这里：隔离模式下任何会碰真实输入的动作都已经被 `OP_INJECT_GLOBAL`
          那一档按住；而元素级 `Invoke`/`SetValue` 是跨进程消息，根本不碰输入设备。
          在这里按"沙箱在不在"再拦一次，只会把元素级也一起误杀。
        · `kind=OP_INJECT_GLOBAL` → 隔离模式下**拒绝**（除非用户显式放开）。

        ⚠️ 与 Linux 的差别是刻意的：那边只有"沙箱在不在"一个粒度，因为 XTEST 注入进的是
        虚拟屏、用户碰不到；这边"送进隔离会话的通道**不存在**"，所以拦的是动作本身。
        """
        if kind != OP_INJECT_GLOBAL:
            return
        why = global_input_refusal(op)
        if why:
            raise GlobalInputRefusedError(why)

    def guard(self) -> str | None:
        # 隔离模式下用户**碰不到**隐藏桌面，没有"礼让"可言（他不可能正在里面操作）。
        # real 模式下用户就在同一个桌面上——但那时他自己要的就是这个，也不需要"等"。
        return None

    def global_input_note(self) -> str | None:
        """放行时那句**响亮提醒**（隔离模式下被显式放开才会有内容）。判据在纯函数里。"""
        return global_input_note()

    def startup_caveat(self) -> str | None:
        """
        ★ 隔离模式下**整场不变**的能力边界 —— 只由环境变量推出来，与运行时状态无关。

        这正是它可以写进 `instructions`（模型开工前就读到的那段）的全部理由：那边要的
        是"整场都成立的事实"，而本方法读的两个环境变量进程生命周期内不会变。

        ⚠️ 别把它改成读"隔离桌面建出来没有"之类的东西：那会随状态变，而写进
        `instructions` 的话没人会再回头修正它 —— 模型会把一句过期的话当既定事实用一整场。
        """
        if not self._isolated():
            return None
        if global_input_allowed():
            return ("本会话运行在**隔离模式**（Windows 隐藏桌面）下，且 "
                    "CC_CU_ALLOW_GLOBAL_INPUT=1 已开启：坐标级点击/滚动/拖拽/键盘注入"
                    "**会真的动用户的鼠标、键盘与前台焦点**。能用元素级（ref / set_value）"
                    "就优先用元素级。")
        return ("本会话运行在**隔离模式**（Windows 隐藏桌面）下：你启动的应用开在一张"
                "用户看不见的桌面上，用户的键鼠与前台不受影响。"
                "代价是**坐标级点击/滚动/拖拽/键盘注入会被拒绝**——隐藏桌面上没有输入"
                "通道，硬发只会打到用户的桌面上。请一律走元素级：get_ui_tree / "
                "find_element 拿 ref，再 click(ref=...) / type_text(ref=...)。"
                "对没有无障碍树的应用（SWT/Java、自绘控件、游戏），隔离模式下**不支持**，"
                "需要用户改 CC_CU_DISPLAY_MODE=real。")

    def env_for(self, display: str | None = None) -> dict[str, str]:
        """不换目标会话（Windows 靠 `lpDesktop` 而非环境变量），但仍要剥掉 Linux 残留。"""
        return _stripped_env()

    def app_env(self, display: str | None = None) -> dict[str, str]:
        """启动应用用的 env。本实现下与 `env_for` 相同。

        ⚠️ 这**不是**"忘了实现"：Windows 把应用放进隔离桌面的机制是
        `STARTUPINFOW.lpDesktop`（一个**进程启动参数**，不是环境变量），
        故这一层没有要多注入的东西 —— 实现落在 `driver/desktop.py::spawn`。
        """
        return _stripped_env()

    def target_label(self) -> str:
        return display_mode_from_env()

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
        # 桌面句柄由 backend 自己放（`desktop.release()`）—— 那边才知道它建了什么。
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
    if IS_WINDOWS:
        # ⚠️ 这里**不 import** `backend.windows.*`：本类只读环境变量，见它的 docstring。
        return WindowsDesktopSession()
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
