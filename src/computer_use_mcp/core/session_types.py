"""
会话能力的**纯类型**（core.session_types）。

═══ 为什么单独一个叶模块，而不是直接写在 core/session.py 里 ═══

`core/session.py` 要 import `core.display`（挑 Linux 的 provider 实现），
而 `core/display/provider.py` 又需要构造 `SessionCapabilities` —— 两个文件互相 import
就成环，**而且是否爆炸取决于 import 顺序**：

    import core.display  → provider.py → core.session → (模块级) _make_manager()
                                                            → from . import display
                                                            → 拿到**半初始化**的模块，
                                                              display.MANAGER 还不存在

「取决于谁先被 import」正是本项目最忌讳的那类 bug（碰巧对/碰巧炸）。本模块只依赖
stdlib，两边都 import 它，环从构造上就不存在——与 `backend/base.py` 的
`BUTTON_NUMBERS`/角色词表被上提是同一种做法。

本模块**只放类型与常量**，不放任何实现、不 import 任何项目内模块。
`core/session.py` 会把这里的名字原样再导出，调用方一律 `from .. import session` 取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SessionCapabilities:
    """
    本次会话的隔离能力申报。

    ⚠️ **这是一份要原样上报给模型与用户的契约，不是内部开关。**
    用户问「agent 到底在哪儿操作、会不会动我的电脑」，答案就是这里。

    ═══ 为什么不是一个 `bool is_isolated` ═══

    隔离**不是一个维度**：三个平台的强度不均匀，而且不均匀在**不同的维度**上。

      · Linux：Xephyr 买到输入 + 屏幕隔离，但**买不到无障碍隔离**——AT-SPI 走会话总线，
        沙箱内外共用同一个 registryd，历史上把宿主 GNOME Shell 打崩过两次
        （详见 CLAUDE.md），要靠另起一条私有总线才补上。
      · Windows：隐藏桌面三样全买（已实测）；而「进程域沙箱」**只买得到无障碍隔离**，
        坐标点击兜底仍会动真实指针。
      · macOS：`CGVirtualDisplay` 只买屏幕隔离，**输入隔离买不到**——
        `CGEventPostToPid` 覆盖不了全部应用，兜底必然抢指针。

    一个布尔值表达不了「屏幕隔离了但输入没隔离」，而**那恰恰是 macOS 的真实状态**。
    把一个买不到的维度报成做到了，用户就会在 agent 动他鼠标的时候以为它待在另一个桌面里
    ——这正是本项目最不能容忍的静默失效。

    ═══ 申报原则 ═══

    **宁可保守（说"没有"而实际有），绝不乐观。**
    保守的代价是用户少用一项能力；乐观的代价是他对「自己的电脑正在被怎么用」判断错误。
    """

    target: str
    """操作落在哪儿。

    'isolated' —— 隔离的会话目标（另一块屏 / 另一块桌面 / 另一台机器）
    'real'     —— 用户本人的环境（他显式要求如此，或该平台尚未实现隔离）
    """

    input_isolated: bool
    """**用户的指针与键盘是否完全不受影响。**

    这一项最要紧：它决定「agent 干活时用户能否继续用自己的电脑」。
    判据是**存在性**而非概率——只要还有**任何一条**会动真实指针 / 抢真实焦点的
    回退路径（哪怕它只在元素级失败时才走），就必须申报 False。
    """

    screen_isolated: bool
    """用户屏幕上会不会冒出我们启动/操作的窗口。"""

    a11y_isolated: bool
    """无障碍流量是否与用户的应用隔离。

    Linux 上这条最要命（见类 docstring）；Windows/macOS 上 UIA/AX 都是**按目标进程**
    访问的跨进程调用，不存在「一条总线把所有应用连起来」的结构，故天然为真
    ——但那是**另一回事**，叙述时别和 Linux 的私有总线混为一谈。
    """

    strong_mode: bool
    """是否启用了本平台能提供的**最强**隔离形态。

    用来区分「Windows 进程域沙箱」与「Windows 隐藏桌面」——两者 `target` 都是
    'isolated'，强度却差一大截。用户想知道「隔离到什么程度」时看的就是这一项。
    """

    unimplemented: tuple[str, ...] = ()
    """本平台**尚未实现**的能力名（如 'click_ring'）。

    非空时必须报到工具描述与自检输出里：少了某项能力**不是 bug**，
    但让用户以为它是 bug 则是我们的问题。
    """

    notes: tuple[str, ...] = ()
    """本次会话特有的限制说明（人读，直接给模型/用户看）。

    例：「目标应用以管理员运行，完整性级别高于本进程，已拒绝注入」。
    """


# ── op 的「种类」：闸门据此决定怎么把关 ─────────────────────────────────────────
#
# 为什么除了 op 名还要有种类：`require(op)` 的 op（如 'click_xy'）只够写日志，
# 判据需要的是「这次操作会碰到什么」。而这件事**无法从方法名可靠推出**——
# 加第 24 个注入方法时忘登记就静默降级。
#
# 故种类由**装饰器位置**给出（挂了 `_exclusive_screen` 的即会动输入/屏），
# derive 而非 enumerate：既有守卫 `test_sandbox_gate_applies_to_all_injection_methods`
# 已经在反射枚举这件事了，不存在"忘了登记"的口子。

OP_INJECT = "inject"
"""会动输入设备或屏幕状态。**默认值**——最保守的那一档。"""

OP_CAPTURE = "capture"
"""抓屏 / 读像素（会碰屏，但不改屏状态）。"""

OP_READ = "read"
"""只读遍历，不改任何状态。"""


class SessionProvider(Protocol):
    """
    层 3：会话与隔离策略。`core/` 与 `tools/` 只认这个 Protocol。

    ═══ 为什么它不是 `Backend` 的一部分 ═══

    `Backend` 回答「这个平台**能**做什么」（能力，进程级，随平台固定）；
    本 Protocol 回答「这一次操作**落在哪个会话**、**代价是什么**」（策略，会变）。

    同一个 Windows 后端，既可能跑在「用户桌面 + 进程白名单」模式，也可能跑在
    「隐藏桌面」模式——把它塞进 `Backend` 会让平台能力与会话策略搅成一团。

    ═══ 实现方（本轮） ═══

    · Linux：`core/display/` 的 `DisplayManager` 本体（经 `SessionProviderMixin`），
      **不是包装器**——`core/session.py::MANAGER` 直接指向同一个对象，理由见那里的注释。
    · 其它平台：`NoIsolationSession`（如实申报"不隔离"）。
    """

    def kind(self) -> str:
        """本次生效的模式名：'isolated' | 'real'。"""

    def capabilities(self) -> SessionCapabilities:
        """当前能力申报。

        **必须每次现算**，不能缓存：沙箱可能已被回收或重建、用户可能刚把键鼠伸进来、
        目标应用可能刚被提权——能力会变。
        """

    def ensure_ready(self) -> dict:
        """惰性准备（原先的 `DisplayManager.ensure_started`）。

        **唯一被 server 正常路径使用的入口**（经 Coordinator 的 `_needs_display`）。
        进程启动时**不**做任何准备：MCP server 是会话启动即常驻拉起的 stdio 进程，
        在那里准备会导致「整场没用过本工具也起了个虚拟屏」。
        返回值是给人看的准备结果摘要。
        """

    def is_ready(self) -> bool:
        """会话是否**已经就绪**。

        语义是「此刻能不能安全地正常干活」，**不是**「隔离强不强」：
          · real 模式 → 恒 True（用户显式要求操作真实环境，没有"要等的东西"）；
          · isolated 且隔离环境已就位 → True；
          · isolated 但隔离环境没起来 → False（此时 `require()` 会拒绝注入，
            且**对 Linux 而言，触碰 a11y 会落到宿主总线上**——那正是能把 GNOME Shell
            打崩的那条路，故自检必须据此绕开 AT-SPI）。

        与 `capabilities()` 的区别：那个回答「隔离到什么程度」（给用户/模型看），
        这个回答「现在能不能用」（给自检与闸门看）。前者是契约，后者是状态。
        """

    def require(self, op: str, *, kind: str = OP_INJECT) -> None:
        """闸门：当前状态不足以安全执行 `op` 时**抛错拒绝**，绝不静默降级。

        这条不能省：静默回落会让 LLM 只看到「操作成功」，然后继续在用户真实环境里
        点下去。`op` 只用于日志/文案；`kind` 才是判据的输入。
        """

    def guard(self) -> str | None:
        """注入前的**礼让**：用户此刻正在目标环境里操作时，等一会儿再动。

        与 `require` 的区别：`require` 是「能不能做」（不满足就抛错拒绝），
        `guard` 是「现在做合不合适」（不满足就等，超时后带警告继续）。
        返回警告文本或 None。
        """

    def env_for(self, display: str | None = None) -> dict[str, str]:
        """**所有**子进程的环境出口。

        职责：换到目标会话、并**主动剥掉**不该被继承的变量。
        （Linux 侧还要剥 `AT_SPI_BUS_ADDRESS` 与冻结产物的 `LD_LIBRARY_PATH`，
        理由见 `core/display/__init__.py` —— 那两条都是实测踩出来的。）
        """

    def app_env(self, display: str | None = None) -> dict[str, str]:
        """专给**启动应用**用：在 `env_for()` 之上**只多一件事**——
        注入「让应用进到目标会话」所需的变量（Linux 是私有总线地址；
        Windows 隐藏桌面是 `lpDesktop`；macOS 是虚拟显示器的关联）。

        ⚠️ 这条边界不许合并进 `env_for()`：`env_for` 被一堆无关子进程共用，
        往里塞 a11y 开关既污染它们、也说不清意图。
        """

    def target_label(self) -> str:
        """人读的目标名（`env["DISPLAY"]` 的跨平台替身，如 ':99' / 'real'）。"""

    def describe(self) -> dict:
        """状态摘要（`--selftest` 与诊断用）。"""

    def stop(self) -> None:
        """幂等回收。"""
