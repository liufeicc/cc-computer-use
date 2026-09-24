"""
Coordinator 的构造与两道前置检查（core.coordinator.base）。

`CoordinatorBase` 是 MRO 里的第一站，承担三件事：
  - `__init__`：声明**全部**实例字段（跨组的字段集中在一处，便于一眼看清状态）；
  - `_require_sandbox`：注入前的沙箱闸门（B 防线）；
  - `_sandbox_guard`：注入前的用户礼让。
"""

from __future__ import annotations

from ...backend.base import Backend
from ...utils.refs import RefTable
from .. import session
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
        注入前的沙箱闸门（B 防线）：**不通过就抛错拒绝，绝不回落宿主桌面**。

        ⚠️ 判据与错误文案**已搬到** `core/display/provider.py::SessionProviderMixin.require`
        （它依赖 `MODE_ISOLATED` / Xephyr，是**平台专属**逻辑，不该住在平台无关的
        coordinator 里）。这里保留一个转发的理由有二：

          1. 与 `_sandbox_guard` 对称，而后者**必须**留着（见它的 docstring）；
          2. **实例级可打桩** —— 测试对 Coordinator 的方法打桩是本仓既有风格。
             若把调用点直接改成 `session.MANAGER.require(op)`，那些打在实例上的补丁
             会**静默失效**（补丁还在、却不经过它），正是本项目最忌讳的失效形态。
        """
        session.MANAGER.require(op)

    def _sandbox_guard(self) -> str | None:
        """
        注入前礼让：目标环境里用户正在操作时，等一会儿再动。

        ⚠️ 判据已搬到 `SessionProviderMixin.guard`（同 `_require_sandbox` 的理由），
        这里同样是转发。**这个转发不能省**：`tests/test_g_guards.py` 在**实例上**
        把本方法覆盖成 `lambda: None` 来放行礼让，而注入路径（`actions.py`）调的是
        `self._sandbox_guard()`——绕过它，那次覆盖就静默失效了。
        """
        return session.MANAGER.guard()