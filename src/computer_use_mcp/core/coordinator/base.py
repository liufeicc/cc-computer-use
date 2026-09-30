"""
Coordinator 的构造与两道前置检查（core.coordinator.base）。

`CoordinatorBase` 是 MRO 里的第一站，承担三件事：
  - `__init__`：声明**全部**实例字段（跨组的字段集中在一处，便于一眼看清状态）；
  - `_require_sandbox`：注入前的沙箱闸门（B 防线）；
  - `_sandbox_guard`：注入前的用户礼让。
"""

from __future__ import annotations

from ...backend.base import Backend
from ...utils.errors import ComputerUseError
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
        # ★ 本会话**最近一次操作过的窗口**（id 字符串，形如 "0x1a2b3c" 或十进制）。
        #
        # 为什么必须记住它，而不是每次去问「现在是哪个窗口在前台」：
        # 在 Windows 上（`DEFAULT_CAPTURE_SCOPE = "window"`），"不给参数时截哪个窗口"
        # 一旦取成"活动窗口"，就会在 agent 不抢前台的将来**截到用户正在用的那扇窗** ——
        # 用户切回浏览器，agent 调一次 screenshot 就把浏览器画面送进模型上下文，
        # **而且不报任何错**。这是本项目最不能容忍的失败形态（静默 + 泄露用户内容）。
        #
        # 所以「目标窗口」按优先级推导（见 `_capture_target`）：显式指定 > 最近操作过的 >
        # 活动窗口（**且必须在返回值里说明**"无处可推，用了前台窗口"）。
        self._target_window_id: str | None = None

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

    def _require_global_input(self, op: str) -> None:
        """
        ★ 全局注入的闸门：**会动用户真实键鼠的那一类动作**，不通过就抛错拒绝。

        与 `_require_sandbox` 是**两道不同的闸门**，都要挂，别合并：
          · `_require_sandbox`（经 `@_exclusive_screen`）：隔离环境这回在不在 —— 环境问题；
          · 本方法：这个**动作本身**在隔离下有没有通道 —— 方式问题。
        合并的后果是两边共用一份文案，而处置建议完全不同（一个说"重试/修环境"，
        一个说"改用元素级"）——模型照着错的建议做，白耗一轮还找不着北。

        判据住在 `session.MANAGER.require(kind=OP_INJECT_GLOBAL)`（层 3）：只有那个
        平台实现知道"我这儿有没有把输入送进隔离会话的通道"。Linux 的 XTEST 注入进的是
        Xephyr，用户碰不到，故那边这一档是 no-op。

        ⚠️ **调用点必须早于任何副作用**，尤其要早于抓预览图：`click_xy` / `scroll_xy` /
        `drag_xy` 都是先抓一张落点周围的小图再注入，而那张图取自**用户的屏幕** ——
        既然这一下注定要被拒绝，就不该先把用户屏幕上的内容读进模型上下文。
        """
        session.MANAGER.require(op, kind=session.OP_INJECT_GLOBAL)

    def _global_input_note(self) -> str | None:
        """
        放行时那句**响亮提醒**（`None` = 没什么要说的）。

        存在的理由：`CC_CU_ALLOW_GLOBAL_INPUT=1` 是用户显式授权"动我的键鼠"，但模型
        看不到环境变量 —— 它只看到"点击成功"。结果里不带这句话，模型就会以为这一下
        落在隔离环境里，继续按那个假设决策（比如"我点的是沙箱，不用担心碰到用户的东西"）。
        """
        try:
            return session.MANAGER.global_input_note()
        except Exception as exc:  # noqa: BLE001 —— 附加文案绝不拖垮主操作
            log.debug("global_input_note 失败（忽略）: %s", exc)
            return None

    # ---------- 目标窗口（「这次操作的是哪一扇窗」）----------

    def _remember_target_window(self, window_id: str | None) -> None:
        """
        记下「本会话最近操作过的窗口」。**取不到就什么都不做**（不覆盖已有的值）。

        为什么要记：见 `__init__` 里 `_target_window_id` 那段说明 ——
        「不给参数时截哪个窗口」不能取"当前前台"，那在 agent 不抢前台之后
        会变成"用户正在用的那扇窗"。
        """
        if window_id:
            self._target_window_id = str(window_id)

    def _capture_target(
        self, scope: str = "auto", window_id: str | None = None,
        app: str | None = None,
    ) -> tuple[str, str | None, str]:
        """
        决定**这次截图/OCR 该覆盖哪儿**，返回 `(模式, 窗口 id 或 None, 说明)`。

        模式取值只有两个：`"screen"`（整屏/整块区域）与 `"window"`（某一扇窗）。

        解析顺序（**顺序本身就是设计**，别调换）：

          1. `scope="screen"` → 整屏。用户明确要的，照办。
          2. `scope="window"` 或 `scope="auto"` 且本平台默认按窗口
             （`backend.DEFAULT_CAPTURE_SCOPE`）：
             a. 给了 `window_id` → 用它；
             b. 给了 `app` → 经 `backend.window_for_app()` 找它的窗口；
             c. 本会话**最近操作过**的窗口（`_target_window_id`）；
             d. 兜底：活动窗口 —— **且说明里必须写明这一点**。

        ⚠️ 第 d 步为什么要"写明"：它是唯一可能**截到用户自己窗口**的分支。
        在 agent 不抢前台的形态下，前台窗口属于**用户**。静默用它就是"悄悄把用户的
        浏览器画面读进模型上下文"，而模型只会以为"这就是当前界面"。
        写明之后，模型至少知道这条信息来路可疑、可以显式传 window_id 重来。

        ⚠️ 平台不支持按窗口渲染时（`capture_window` 返回 None）**不在这里降级**：
        本方法只决定"要哪扇窗"，怎么拿到图由调用方按平台能力决定（见 `screens.py`
        的 `_grab`）—— 把两件事混在一起，会让"平台没这能力"与"这次抓失败了"
        看起来一模一样。
        """
        want_window = (scope == "window") or (
            scope == "auto" and self.backend.DEFAULT_CAPTURE_SCOPE == "window")
        if scope == "screen" or not want_window:
            return "screen", None, ""

        if window_id:
            return "window", str(window_id), f"按指定窗口 {window_id}"

        if app:
            wid = None
            try:
                wid = self.backend.window_for_app(app)
            except Exception as exc:  # noqa: BLE001 —— 找窗失败不该把整次调用炸掉
                log.debug("window_for_app(%r) 失败: %s", app, exc)
            if wid:
                return "window", str(wid), f"按应用 {app!r} 找到窗口 {wid}"
            raise ComputerUseError(
                f"找不到应用 {app!r} 的窗口。可用 list_windows 查看当前有哪些窗口，"
                f"或直接传 window_id。"
            )

        if self._target_window_id:
            return ("window", self._target_window_id,
                    f"按本会话最近操作过的窗口 {self._target_window_id}")

        r = None
        try:
            r = self.backend.active_window_rect()
        except Exception as exc:  # noqa: BLE001
            log.debug("active_window_rect 失败: %s", exc)
        if r is not None and not r.is_empty():
            title = None
            try:
                title = self.backend.active_window_title()
            except Exception:  # noqa: BLE001
                title = None
            wid_fb = None
            try:
                wid_fb = self.backend.active_window_id()
            except Exception as exc:  # noqa: BLE001
                log.debug("active_window_id 失败: %s", exc)
            return ("window", wid_fb,
                    f"⚠️ 没有可推断的目标窗口，**退用了当前前台窗口**"
                    f"（{title or '未知'}）——那可能正是**用户在用的窗口**。"
                    f"要确定截哪一扇，请显式传 window_id 或 app，"
                    f"或先对目标做一次操作（click/type_text）让本会话记住它。")
        # 空桌面等取不到窗口的场景：如实退回整屏，并说明原因
        return "screen", None, "取不到任何窗口（空桌面？），已退回整屏"
