"""
Coordinator 的构造与两道前置检查（core.coordinator.base）。

`CoordinatorBase` 是 MRO 里的第一站，承担三件事：
  - `__init__`：声明**全部**实例字段（跨组的字段集中在一处，便于一眼看清状态）；
  - `_require_sandbox`：注入前的沙箱闸门（B 防线）；
  - `_sandbox_guard`：注入前的用户礼让。
"""

from __future__ import annotations

from ...backend.base import Backend
from ...utils.errors import SandboxUnavailableError
from ...utils.refs import RefTable
from .. import display
from ..serializer import TreeSerializer
from .hooks import log


class CoordinatorBase:
    """状态声明与前置安全检查。"""

    def __init__(self, backend: Backend, ref_table: RefTable | None = None) -> None:
        self.backend = backend
        self.refs = ref_table or RefTable()
        self.serializer = TreeSerializer(self.refs)
        # act_sequence 内的截图计数（M-46）。放在实例上而不是每个调用一个局部变量，
        # 是为了让直接调 _run_seq_step 的路径也受同一个上限约束（act_sequence 会重置它）。
        self._seq_shots = 0
        # 同上的**读树**计数：`{op:"ui_tree"}` 每步都会把一整棵树塞进响应，
        # 50 步 × 400 节点足够把一次响应撑爆，故与截图同一套思路单独设限。
        self._seq_trees = 0

    def _require_sandbox(self, op: str) -> None:
        """
        注入前确认沙箱真的在（B 防线）——**不通过就抛错拒绝，绝不回落宿主桌面**。

        实现逻辑：
          1. real 模式：用户显式要求操作真实桌面，本就不隔离 → 放行。
          2. 沙箱在 → 放行。
          3. 沙箱不在 → 先试重建（display.ensure_started 对"曾成功过"的进程允许有限次
             重建，能自愈被回收/崩掉的屏）；仍不在 → 抛 SandboxUnavailableError。

        为什么必须存在：effective_display() 在沙箱不可用时静默回落宿主 DISPLAY，而 LLM
        只看得到"点击成功"，会继续在用户真实桌面上操作。实测这条路真会走到——多会话
        共享同一块沙箱时，建屏方退出会带走屏，另一方的 effective_display 就变成了 :1。
        """
        if display.MANAGER.mode != display.MODE_ISOLATED:
            return
        if display.MANAGER.is_sandbox_up():
            return
        display.MANAGER.ensure_started()  # 曾是好的但屏没了 → 给一次自愈机会
        if display.MANAGER.is_sandbox_up():
            log.warning("沙箱曾失联，已重建成功（op=%s，屏=%s）",
                        op, display.MANAGER.sandbox_display)
            return
        raise SandboxUnavailableError(
            f"[{op}] 隔离沙箱不可用，**已拒绝执行**，以免误操作你的真实桌面。\n"
            f"   可能原因：Xephyr 未安装、启动失败，或虚拟屏被其它会话回收后重建失败。\n"
            f"   处理：稍后重试（会自动尝试重建）；若确实要直接操作真实桌面，请设置 "
            f"CC_CU_DISPLAY_MODE=real 后重启 Claude Code。"
        )

    def _sandbox_guard(self) -> str | None:
        """
        注入前礼让：isolated 模式下若用户正把键鼠伸进沙箱窗口操作，轮询等待其离开。

        返回警告文本（等到超时用户仍在：仍执行注入但明示冲突风险）或 None（无需礼让）。
        只读工具与元素级 do_action/set_value 不碰输入设备，不做此检查。
        """
        if display.MANAGER.mode != display.MODE_ISOLATED:
            return None
        if display.MANAGER.wait_until_user_leaves():
            return None
        return "⚠️ 用户正在沙箱内操作，本次注入可能与其冲突"