"""
DisplayManager 的「会话/隔离 provider」侧面（core.display.provider）。

═══ 这个 mixin 是干什么的 ═══

`core/session.py`（层 3）需要一个平台无关的会话接口，Linux 的实现**就是**
`core/display/` 那套 Xephyr + 私有总线的机器。本 mixin 把那台机器包成
`SessionProvider` 的形状（`kind` / `ensure_ready` / `require` / `guard` /
`capabilities` / `target_label`），其余方法（`env_for` / `app_env` / `describe` /
`stop`）**本来就有**，不在这里重复定义——重复定义会让 MRO 把它们遮蔽掉，
而它们是既有测试的直接打桩目标。

═══ ⚠️ 为什么是 mixin，而不是「一个持有 MANAGER 的包装器类」 ═══

先说结论：**`core/session.py::MANAGER` 必须与 `display.MANAGER` 是同一个对象**。
包装器方案看着更干净（`core/display/` 一行不用动），但它会**静默打空一批既有补丁**：

    monkeypatch.setattr(srv.session.MANAGER, "_mode", MODE_ISOLATED)

`monkeypatch.setattr` 是「先 getattr 确认存在、再 setattr」。对包装器来说，
getattr 可能经 `__getattr__` 转发而成功，setattr 却落在**包装器自己**身上——
于是 `capabilities()` 读到的仍是真实 MANAGER 的旧值，测试前提**没生效但也不报错**。
`tests/test_g_guards.py` 里 M-43 的两条用例正是这个形状（强制 isolated + 沙箱不在，
断言 selftest 走 X11 分支），它们会因此走错分支。

同一对象则任何补丁目标都等价：全仓 55 处 `monkeypatch.setattr(MANAGER, "<成员>", …)`
（`tests/_helpers.py` 15 处私有字段、`test_display_lifecycle.py` 21 处、
`test_f_guards.py` 14 处、`test_h_guards.py` 6 处）全部原样生效。

代价是这个**新文件** + `manager.py` 基类列表多一项——而不是「不碰 core/display/」。
这个取舍是刻意的：**「少改一个文件」不值得换「一批测试静默失效」。**

═══ ⚠️ 新增本 mixin 会打断的守卫（已核对，均不受影响）═══

`tests/test_f_guards.py` 的白名单管的是 `core/display` 的**模块级名字**
（禁 `ensure_started`/`sandbox_at_spi_bus`/…，须保留 `effective_display`/`env_for`/
`app_env`/`start`）。本 mixin 加的是**类方法**，且新名字
（`kind`/`ensure_ready`/`require`/`guard`/`capabilities`/`target_label`）
与那 8 个禁/须保留名一个都不撞。
"""

from __future__ import annotations

from ...utils.errors import SandboxUnavailableError
from ..session_types import OP_INJECT, SessionCapabilities
from .constants import _DEAD_AT_SPI_BUS, MODE_ISOLATED, log


class SessionProviderMixin:
    """把 `DisplayManager` 适配成 `core.session_types.SessionProvider`。

    ⚠️ 本类的每个方法都必须用 `self.<方法>()` 调用 `DisplayManager` 自己的方法
    （而**不是**在别处缓存绑定方法）：既有测试靠替换 `MANAGER` 上的**实例属性**来打桩，
    调用时查属性才吃得到那些补丁。
    """

    # ---------- 身份 ----------

    def kind(self) -> str:
        """本次生效的模式名（'isolated' | 'real'）——即 `DisplayManager.mode`。"""
        return self.mode

    def target_label(self) -> str:
        """人读的目标名。Linux 上就是 DISPLAY 串（如 ':99'），与 `env["DISPLAY"]` 一致。"""
        return self.effective_display()

    # ---------- 准备 ----------

    def ensure_ready(self) -> dict:
        """惰性准备（= `ensure_started`）。

        保留一个改名后的入口而不是让调用方直接用 `ensure_started`，是为了让
        `core/` 只依赖 session 的形状——将来 Windows 的 provider 没有「start」这个概念
        （它要么已经在隔离环境里，要么本来就在真实桌面），但仍必须提供同一个入口。
        """
        return self.ensure_started()

    # ---------- 闸门 ----------

    def is_ready(self) -> bool:
        """会话是否已经就绪（见 `SessionProvider.is_ready` 的语义说明）。

        real 模式恒 True；isolated 模式下等于「沙箱在不在」。
        自检用它来决定「可不可以安全地触碰 AT-SPI」——isolated 且沙箱没起来时，
        那次触碰会落到宿主总线，而那是能把宿主 GNOME Shell 打崩的路（实测过两次）。
        """
        if self.mode != MODE_ISOLATED:
            return True
        return self.is_sandbox_up()

    def require(self, op: str, *, kind: str = OP_INJECT) -> None:
        """
        注入前确认沙箱真的在（B 防线）——**不通过就抛错拒绝，绝不回落宿主桌面**。

        实现逻辑（逐字搬自 `core/coordinator/base.py::_require_sandbox`，
        行为与错误文案一字未改）：
          1. real 模式：用户显式要求操作真实桌面，本就不隔离 → 放行。
          2. 沙箱在 → 放行。
          3. 沙箱不在 → 先试重建（对"曾成功过"的进程允许有限次重建，能自愈被回收/
             崩掉的屏）；仍不在 → 抛 SandboxUnavailableError。

        为什么必须存在：`effective_display()` 在沙箱不可用时**静默回落宿主 DISPLAY**，
        而 LLM 只看得到"点击成功"，会继续在用户真实桌面上操作。实测这条路真会走到——
        多会话共享同一块沙箱时，建屏方退出会带走屏，另一方的 effective_display 就变成了 :1。

        `kind` 在本实现里不参与判据（Linux 的闸门粒度就是「沙箱在不在」，与操作种类无关），
        但签名必须收它——Windows 侧判据会按种类分档（只读免检、注入要过白名单/UIPI）。
        """
        if self.mode != MODE_ISOLATED:
            return
        if self.is_sandbox_up():
            return
        self.ensure_started()  # 曾是好的但屏没了 → 给一次自愈机会
        if self.is_sandbox_up():
            log.warning("沙箱曾失联，已重建成功（op=%s，屏=%s）", op, self.sandbox_display)
            return
        raise SandboxUnavailableError(
            f"[{op}] 隔离沙箱不可用，**已拒绝执行**，以免误操作你的真实桌面。\n"
            f"   可能原因：Xephyr 未安装、启动失败，或虚拟屏被其它会话回收后重建失败。\n"
            f"   处理：稍后重试（会自动尝试重建）；若确实要直接操作真实桌面，请设置 "
            f"CC_CU_DISPLAY_MODE=real 后重启 Claude Code。"
        )

    def guard(self) -> str | None:
        """
        注入前礼让：isolated 模式下若用户正把键鼠伸进沙箱窗口操作，轮询等待其离开。

        返回警告文本（等到超时用户仍在：仍执行注入但明示冲突风险）或 None（无需礼让）。
        只读工具与元素级 do_action/set_value 不碰输入设备，不做此检查。
        （逐字搬自 `core/coordinator/base.py::_sandbox_guard`。）
        """
        if self.mode != MODE_ISOLATED:
            return None
        if self.wait_until_user_leaves():
            return None
        return "⚠️ 用户正在沙箱内操作，本次注入可能与其冲突"

    # ---------- 能力申报 ----------

    def capabilities(self) -> SessionCapabilities:
        """
        本次会话的隔离能力申报。**每次现算**，不缓存。

        三态（对应 Linux 侧真实存在的三种局面）：

          1. real 模式 —— 用户显式要求直接操作真实桌面。三样隔离全无，如实申报。
          2. isolated 且沙箱在 —— Xephyr 买了输入 + 屏幕隔离；a11y 隔离另看私有总线
             是否真的绑上了（起不来时 `app_env` 会退回**死地址**，此时沙箱内没有
             a11y 能力但宿主是安全的——那不算"隔离成功"，故申报 False）。
          3. isolated 但沙箱不在 —— 此时 `require()` 会拒绝一切注入，`effective_display()`
             会回落宿主。**输入隔离必须报 False**：虽然注入被拦住了，但拦截是"拒绝执行"
             而不是"在别处执行"，用户若不知道这一点就无法理解为什么工具报错。
        """
        isolated = self.mode == MODE_ISOLATED
        if not isolated:
            return SessionCapabilities(
                target="real",
                input_isolated=False,
                screen_isolated=False,
                a11y_isolated=False,
                strong_mode=False,
                notes=("real 模式：操作直接落在你的真实桌面上（用户显式要求）",),
            )

        up = self.is_sandbox_up()
        if not up:
            return SessionCapabilities(
                target="isolated",
                input_isolated=False,
                screen_isolated=False,
                a11y_isolated=False,
                strong_mode=False,
                notes=(
                    "隔离沙箱未就绪：注入会被**拒绝执行**（不会回落你的真实桌面）。",
                    "稍后重试会在后台尝试重建沙箱。",
                ),
            )

        bus = self.sandbox_at_spi_bus()
        return SessionCapabilities(
            target="isolated",
            input_isolated=True,
            screen_isolated=True,
            a11y_isolated=bus != _DEAD_AT_SPI_BUS,
            strong_mode=True,
            notes=() if bus != _DEAD_AT_SPI_BUS else (
                "沙箱的私有无障碍总线未绑定：沙箱内没有 a11y 能力（宿主不受影响），"
                "只剩坐标点击与 OCR 两条通道。",
            ),
        )
