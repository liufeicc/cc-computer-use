"""
感知（core.coordinator.awareness）—— 读树、搜元素、解析 ref。

这一组全是**只读**操作：不改变屏状态，因而**不挂** `@_exclusive_screen`（在
`tests/test_optimizations.py` 的 `_EXCLUSIVE_EXEMPT` 里显式登记了理由）。但它们
**必须挂** `@_needs_display` —— `get_ui_tree` 默认 `scope=active_window` 会经
`inject.active_window_title/pid` 读 X11：沙箱未启时读到的是**宿主**活动窗口，而随后的
click 落在沙箱，感知与操作分属两个 display，极隐蔽。
"""

from __future__ import annotations

from typing import Any

from ...backend.base import Element, ElementDetail, TextBlock
from ...utils.errors import ComputerUseError, ElementNotFoundError, InvalidRefError
from .hooks import _needs_display, log


class AwarenessMixin:
    """读树 / 搜索 / 元素详情 / ref 解析。"""

    # ================= 感知 =================
    @_needs_display
    def get_ui_tree(
        self, scope: str = "active_window", app: str | None = None,
        interactive_only: bool = False, max_nodes: int = 400,
    ) -> str:
        """读树 + 序列化（分配 ref）。返回紧凑文本。"""
        res = self.backend.get_tree(scope=scope, app=app, max_nodes=max(50, max_nodes))
        text = self.serializer.serialize(
            res.items, interactive_only=interactive_only, max_nodes=max_nodes,
            register_refs=True,
        )
        # 截断/降级说明**必须**放在最前面：它是「结果不完整」的警告，放在末尾容易被
        # 长树淹没。见 backend.base.QueryResult 的说明（I-4 + I-9）。
        return f"{res.notice}\n{text}" if res.notice else text

    @_needs_display
    def find_elements(
        self, text: str | None = None, role: str | None = None, app: str | None = None,
        interactive_only: bool = True, limit: int = 40,
    ) -> tuple[list[Element], str]:
        """
        搜索元素并注册 ref，返回 (Element 候选列表, 结果完整性说明)。

        说明非空 = 搜索被截断（应用数/节点数达上限），**必须**回给模型——它若把
        「截断」当成「桌面上没有」，就会绕去截图那条昂贵得多的路（见 QueryResult）。
        """
        if not text and not role:
            raise ComputerUseError("find_element 需至少提供 text 或 role 之一")
        res = self.backend.find(text=text, role=role, app=app,
                                interactive_only=interactive_only, limit=limit)
        out: list[Element] = []
        for nv in res.items:
            ref = self.refs.register(nv, role=role or "", name=text or "", app=app or "")
            el = self.backend.element_info(nv)
            el.ref = ref
            # 用真实读到的 role/name 覆盖（注册时可能只有查询词）
            self.refs.get(ref).meta.update(role=el.role, name=el.name, app=el.app)
            out.append(Element(ref=ref, role=el.role, name=el.name, app=el.app,
                               rect=el.rect, actions=el.actions))
        return out, res.notice

    @_needs_display
    def get_element_info(self, ref: int) -> ElementDetail:
        """按 ref 取元素详情。"""
        native, _ = self._resolve_native(ref)   # 只读操作：发生重定位时无需回报
        detail = self.backend.element_info(native)
        detail.ref = ref
        return detail

    # ================= ref 解析 =================
    def _resolve_native(self, ref: int) -> tuple[Any, str]:
        """
        按 ref 取「活的」原生元素对象，返回 (native, 重定位备注)。

        实现逻辑：
          1. RefTable 取 entry；无 → InvalidRefError。
          2. 校验 payload 是否仍有效（尝试读角色名，gi 对已销毁元素会抛异常或返回空）。
             - 有效 → 直接返回，备注为空串。
          3. 失效则尝试用 meta(role+name+app) 重新定位：
             - 命中 0 个 → InvalidRefError（界面已变，请重新感知）
             - 命中 >1 个 → InvalidRefError（**不猜**，理由见下）
             - 恰好 1 个 → 放行，但返回一条备注，由调用方回报给模型

        **为什么命中多个必须拒绝**（2026-09-16 修，见 review_0.1.0.md I-1）：
          重定位的触发条件是「原 native 已死」，即**界面已经变了**，而调用方的决策是基于
          旧界面做出的——这与 screen_lock 拒绝「排队执行旧决策」是同一条理由，本就不该
          盲目执行。而 (role, name, app) 只是**弱身份**：「确定」「保存」在每个对话框里都
          同名，limit=1 会静默选中**第一个**匹配（可能是另一个窗口里的另一个按钮），随后
          invoke 成功、回报「元素级 do_action 成功」——正是本项目最忌讳的「打偏了却报成功」。
          命中多个恰恰说明身份根本不唯一，此时**宁可让模型重新感知，也不猜**。

        **已知残留（本次未覆盖）**：「恰好唯一匹配、但目标已变」仍挡不住——例如对话框 A
          关闭后同 app 弹出对话框 B、两者各有一个唯一的「确定」。根治需要更强的身份证据
          （如矩形邻近），而那要求 serializer 在注册 ref 时读矩形（当前为性能刻意不读）。
          故唯一命中时仍放行，但**必须**把备注回报给调用方，让模型有机会核对。
        """
        entry = self.refs.get(ref)
        if entry is None:
            raise InvalidRefError(f"ref={ref} 不存在或已过期，请重新调用 get_ui_tree/find_element")
        native = entry.payload
        if self._native_alive(native):
            return native, ""
        # 失效 → 重定位（保守策略，见 docstring）
        meta = entry.meta
        log.info("ref=%s 失效，尝试按 meta 重定位: %s", ref, meta)
        self.refs.remove(ref)
        name, role, app = meta.get("name"), meta.get("role"), meta.get("app")
        # limit=2 只为「判断是否唯一」：find 在凑满 limit 时即提前停止，代价与 limit=1 相当
        matches = self.backend.find(text=name or None, role=role or None,
                                    app=app or None, limit=2).items
        if not matches:
            raise InvalidRefError(
                f"ref={ref} 已失效且无法重定位（界面可能已变化），请重新 get_ui_tree"
            )
        if len(matches) > 1:
            raise InvalidRefError(
                f"ref={ref} 已失效，按 (role={role}, name={name}, app={app}) 重定位到多个"
                f"候选（≥2 个），无法确定是哪一个——**本工具不猜**。\n"
                f"   请重新 get_ui_tree 确认界面现状后，用新的 ref 操作。"
            )
        return matches[0], (
            f"⚠️ 原 ref={ref} 的元素已失效，本操作实际落在按 (role={role}, name={name}, "
            f"app={app}) 重定位到的新元素上——**可能已不是同一个目标**，请核对结果"
        )

    def _native_alive(self, native: Any) -> bool:
        """
        检测原生元素是否仍可用。

        TextBlock（OCR 文本块）直接判活：它是个纯数据对象，不存在"被销毁"这回事，
        若走下面的 role_name 分支会抛异常 → 被误判失效 → 触发按 meta 重定位（一条
        无意义的 AT-SPI 搜索）。代价是 OCR 坐标是**快照**，界面变了它会过期——
        见 get_screen_text 的说明，调用方需在界面变化后重新识别。
        """
        if isinstance(native, TextBlock):
            return True
        try:
            # 走 ABC（backend.is_alive）而不是 `hasattr(backend, "reader")` 摸 Linux 私有属性：
            # 后者会让别的平台**静默失去**这项能力，而它是 ref 失效判定与重定位的唯一入口。
            # 见 base.Backend.is_alive 的 docstring（I-10）。
            return self.backend.is_alive(native)
        except Exception:  # noqa: BLE001
            return False

    def _target_native(
        self, ref: int | None, text: str | None, role: str | None, app: str | None,
    ) -> tuple[Any, str]:
        """
        根据 ref 或 text/role/app 解析出目标原生元素，返回 (native, 重定位备注)。

        找不到抛 ElementNotFoundError。备注非空表示「原 ref 已失效、本操作落在了按 meta
        重定位到的新元素上」，调用方**必须**把它拼进回报给模型的消息里（见 _resolve_native）。
        走 text/role/app 现场匹配时没有「旧目标」可言，备注恒为空串。
        """
        if ref is not None:
            return self._resolve_native(ref)
        if text or role:
            matches = self.backend.find(text=text, role=role, app=app, limit=1).items
            if matches:
                return matches[0], ""
        raise ElementNotFoundError(
            "未提供 ref，且按 text/role/app 未匹配到元素"
        )