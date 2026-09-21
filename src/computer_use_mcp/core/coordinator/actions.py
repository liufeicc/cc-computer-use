"""
操作（core.coordinator.actions）—— 三级降级的执行处。

核心主张（demo 已验证）：**元素级操作优先，坐标点击仅兜底**。
同一个「点击」意图，按以下顺序降级，每级失败才进下一级：

  ① element  ：backend.invoke(native) —— AT-SPI do_action，零坐标、不受焦点/DPI 影响（首选）。
  ② coord    ：geometry 校准出屏幕绝对坐标 → backend.click_at（先聚焦落点窗口）。
  ③ screenshot：连元素都找不到（灰区：自绘/游戏/远程桌面）→ 提示走截图兜底，由 LLM 决策。

本模块的方法**全部**挂 `@_needs_display` + `@_exclusive_screen`（顺序不能反）：
它们都会真的动指针/焦点/键盘。
"""

from __future__ import annotations

from typing import Any

from ...utils.errors import (
    LEVEL_COORD,
    LEVEL_ELEMENT,
    LEVEL_KEY,
    LEVEL_NONE,
    ActionResult,
    ElementNotFoundError,
    InvalidRefError,
)
from .hooks import _exclusive_screen, _needs_display


class ActionMixin:
    """点击 / 输入 / 按键（三级降级）。"""

    # ================= 操作（三级降级）=================
    @_needs_display
    @_exclusive_screen
    def click_xy(
        self, x: int, y: int, button: int = 1, preview: bool | None = None,
        expect: str | None = None,
    ) -> ActionResult:
        """
        裸坐标点击（灰区应用专用：SWT/自绘等无元素树时，LLM 视觉定位后直点）。

        走坐标级通道（先聚焦落点窗口再点），注入前做沙箱礼让检查。
        点击后附带**落点证据**（哪扇窗、底下什么字、点后活动窗口）——裸坐标点击最容易
        点偏，而 xdotool 的返回值只说明「事件发出去了」，不说点中了什么；把这几条信息
        直接报回去，模型就不必再截一张图确认（那是整整一个来回）。

        本方法在坐标点击里给的反馈最全（灰区是坐标点击的主战场），四样东西都来自
        **同一次抓屏**（预览图的原始图）：
          1. 落点文字——压住落点的文字块；
          2. 最近候选——离落点最近的文字块及其偏差像素数；
          3. 期望偏差——`expect` 声明了「我想点什么」时，直接给出精确偏差与**建议改点的坐标**；
          4. 界面变化——点后同区域像素对比（这一下到底有没有被响应）。
        `preview=None` 跟随环境变量（默认抓）；`preview=False` 则连抓都不抓（零开销，
        代价是上述 1~4 全都没有）。
        """
        warn = self._sandbox_guard()
        x, y = int(x), int(y)
        # 点击前取证据：此刻指针本来就要去这儿，无副作用；点完再取就晚了。
        # 先读活动窗口再取落点证据——顺序不能反（describe_point 会挪指针）。沙箱 i3 配置
        # 已**显式** focus_follows_mouse no，挪指针本身不切焦点；此处刻意不依赖那条配置
        # （CC_CU_SANDBOX_WM=none 时沙箱内无 WM，指针位置确实决定键盘去向），故顺序写死。
        # 排查「活动窗口不符」时别往 focus_follows_mouse 上想（D-1/M-8）。
        before_active = self._landing().get("active_window")
        # 预览图（点前抓）：它的**原始图**后面还要喂给 OCR 和点后对比，故整条链路只抓一次
        shot = self._preview_image(x, y, preview)
        # 有裁剪图 → 落点文字由它 OCR 得出（同一份像素复用）；没有（预览被关）→ 退回旧的小块 OCR
        ev = self._point_evidence(x, y, with_text=shot is None)
        aim, aim_text = self._aim_from_preview(x, y, shot, expect)
        if aim.get("landing_text"):
            ev["text"], ev["conf"] = aim["landing_text"], aim.get("landing_conf")
        payload = (shot.data, shot.meta) if shot is not None else None
        try:
            ok = self.backend.click_at(x, y, button=button, focus_window=True)
        except Exception as exc:  # noqa: BLE001
            # 失败恰恰最需要「它本来想点哪、那儿有什么」——评估文字照常给出
            msg = f"坐标点击失败：{exc}"
            if aim_text:
                msg += f"｜{aim_text}"
            return ActionResult(ok=False, level=LEVEL_COORD, message=msg,
                                attempts=[{"level": LEVEL_COORD, "ok": False, "note": str(exc)}],
                                data={"x": x, "y": y, **ev}, preview=payload)
        msg = f"坐标级点击已执行 @({x},{y})"
        landing = self._format_landing(ev, self._landing(self._INJECT_SETTLE), before_active)
        if landing:
            msg += f"｜{landing}"
        if aim_text:
            msg += f"｜{aim_text}"
        change = self._change_from_preview(shot)      # 必须在 settle 之后
        if change:
            msg += f"｜{change}"
        if warn:
            msg += f"；{warn}"
        # 回看记录（含图与结论）：模型事后想核对「刚才点在哪、程序算偏了多少」时取回，
        # 不必重新截图重新估算，也不必盲目再点一次
        self._remember_click(x, y, LEVEL_COORD, shot,
                             note="｜".join(t for t in (aim_text, change) if t))
        return ActionResult(ok=ok, level=LEVEL_COORD, message=msg,
                            attempts=[{"level": LEVEL_COORD, "ok": ok,
                                       "note": f"click_at({x},{y})"}],
                            data={"x": x, "y": y, **ev}, preview=payload)

    @_needs_display
    @_exclusive_screen
    def click(
        self, ref: int | None = None, text: str | None = None, role: str | None = None,
        app: str | None = None, button: int = 1, action: str | None = None,
        preview: bool | None = None,
    ) -> ActionResult:
        """
        点击元素：三级降级。

        参数：ref 优先；否则用 text/role/app 现场找第一个匹配。
        preview：坐标级兜底那一步是否附「点击前准星小图」（元素级成功时无坐标、不附）。
        """
        attempts: list[dict[str, Any]] = []
        # 0. 定位目标
        try:
            native, reloc_note = self._target_native(ref, text, role, app)
        except ElementNotFoundError as exc:
            # M-18①：这里**一级都没走到**（元素级没做、坐标级没点、截图更没截），
            # 故 level 必须是 LEVEL_NONE。原先写 LEVEL_SCREENSHOT 会让 to_text() 打印
            # 「失败（层级=screenshot）」，模型据此以为「截图这条路已经试过且失败」——
            # 而「改用截图」只是**建议**，它已经在 message 与 data["hint"] 里了。
            attempts.append({"level": LEVEL_NONE, "ok": False,
                             "note": f"未找到元素：{exc}"})
            return ActionResult(
                ok=False, level=LEVEL_NONE,
                message=f"未找到目标元素，无法元素级/坐标级点击。{exc}",
                attempts=attempts,
                data={"hint": "改用 screenshot 工具截图，由你视觉判断后再用坐标或其它手段"},
            )

        # 1. 元素级 do_action（首选）
        try:
            ok = self.backend.invoke(native, action)
            attempts.append({"level": LEVEL_ELEMENT, "ok": ok, "note": "do_action"})
            if ok:
                # 元素级本不需要落点证据，但「ref 已失效被重定位」必须回报：
                # 它意味着执行的是**基于旧界面的决策**，模型必须知道并核对（见 _resolve_native）
                msg = "元素级 do_action 成功（零坐标）"
                if reloc_note:
                    msg += f"｜{reloc_note}"
                return ActionResult(ok=True, level=LEVEL_ELEMENT,
                                    message=msg,
                                    attempts=attempts, data={"ref": ref})
        except Exception as exc:  # noqa: BLE001
            attempts.append({"level": LEVEL_ELEMENT, "ok": False, "note": str(exc)})

        # 2. 坐标点击（兜底，先校准）
        warn = self._sandbox_guard()  # 坐标级要动指针/焦点，注入前礼让沙箱内的用户
        try:
            rect = self.backend.element_screen_rect(native)
            if rect is not None and not rect.is_empty():
                cx, cy = rect.center
                # 元素级失败的这条路最容易点偏（坐标是校准出来的），故也回报落点证据。
                # 不做 OCR：目标元素的名字已经知道，缺的只是「这一下打在哪扇窗」——
                # 若落点窗口与元素所属窗口不是同一个，就是打偏了。省掉那 0.3s。
                before_active = self._landing().get("active_window")
                ev = self._point_evidence(cx, cy, with_text=False)
                # 准星小图（点前抓）：这条路的误差模式不是「瞄错目标」而是**校准漂移**
                # （GTK 的 SCREEN 坐标偏移），小图让模型/人一眼看出「这一下到底落在哪」。
                # 不做 OCR 候选评估：目标元素的名字已经知道，缺的只是「到底落在哪」。
                shot = self._preview_image(cx, cy, preview)
                ok = self.backend.click_at(cx, cy, button=button, focus_window=True)
                attempts.append({"level": LEVEL_COORD, "ok": ok,
                                 "note": f"click_at({cx},{cy})"})
                if ok:
                    msg = f"元素级失败，坐标点击已执行 @({cx},{cy})"
                    landing = self._format_landing(ev, self._landing(self._INJECT_SETTLE),
                                                   before_active)
                    if landing:
                        msg += f"｜{landing}"
                    change = self._change_from_preview(shot)   # 必须在 settle 之后
                    if change:
                        msg += f"｜{change}"
                    if reloc_note:
                        msg += f"｜{reloc_note}"
                    if warn:
                        msg += f"；{warn}"
                    self._remember_click(cx, cy, LEVEL_COORD, shot, note=change)
                    return ActionResult(ok=True, level=LEVEL_COORD,
                                        message=msg,
                                        attempts=attempts,
                                        data={"x": cx, "y": cy, "ref": ref, **ev},
                                        preview=(shot.data, shot.meta) if shot else None)
            else:
                attempts.append({"level": LEVEL_COORD, "ok": False, "note": "无可用矩形"})
        except Exception as exc:  # noqa: BLE001
            attempts.append({"level": LEVEL_COORD, "ok": False, "note": str(exc)})

        # 3. 都失败 → 建议截图（同样：截图**没被尝试**，level 只能是 LEVEL_NONE，M-18①）
        return ActionResult(
            ok=False, level=LEVEL_NONE,
            message="元素级与坐标级均失败。建议截图人工/视觉确认，或换 text/role 重新定位。",
            attempts=attempts, data={"ref": ref},
        )

    @_needs_display
    @_exclusive_screen
    def type_text(
        self, text: str, ref: int | None = None, app: str | None = None,
        clear_first: bool = False,
    ) -> ActionResult:
        """
        输入文本：set_value 优先，xdotool type 兜底。

        实现逻辑：
          1. 若给 ref：解析 native，尝试 backend.set_value（元素级，最稳）。
             - 可先 focus 元素再 set_value/输入。
          2. set_value 失败或无 ref：聚焦（若 ref 可定位则聚焦其窗口）后 backend.type_text。
        """
        attempts: list[dict[str, Any]] = []
        native = None
        reloc_note = ""
        if ref is not None:
            try:
                native, reloc_note = self._resolve_native(ref)
            except (InvalidRefError, ElementNotFoundError) as exc:
                attempts.append({"level": LEVEL_ELEMENT, "ok": False, "note": f"ref 解析失败:{exc}"})

        # 1. 元素级 set_value
        if native is not None:
            try:
                # 先聚焦再赋值：元素级 set_value 能成功的前提就是目标已获得焦点。
                # 走 ABC（backend.focus）——原先的 hasattr 哨兵会让别的平台静默丢掉这一步，
                # 表现为「赋值偶发失败」，极难反查（见 base.Backend.focus 的 docstring / I-10）。
                self.backend.focus(native)
                ok = self.backend.set_value(native, text)
                attempts.append({"level": LEVEL_ELEMENT, "ok": ok, "note": "set_value"})
                if ok:
                    msg = "元素级 set_value 成功"
                    if reloc_note:
                        msg += f"｜{reloc_note}"
                    return ActionResult(ok=True, level=LEVEL_ELEMENT,
                                        message=msg,
                                        attempts=attempts, data={"ref": ref})
            except Exception as exc:  # noqa: BLE001
                attempts.append({"level": LEVEL_ELEMENT, "ok": False, "note": str(exc)})

        # 2. 键盘注入兜底
        warn = self._sandbox_guard()  # 键盘注入会发真实按键事件，注入前礼让
        try:
            if clear_first:
                self.backend.press_key("ctrl+a")
                self.backend.press_key("Delete")
            ok = self.backend.type_text(text)
            # 注：backend 内部按文本分流——纯 ASCII 走 xdotool type，
            # 含中文等非 ASCII 走剪贴板粘贴（避免全局键位重映射冻结物理键盘）
            # M-18②：这是**键盘注入**，不是「坐标点击」——原先借用 LEVEL_COORD，会让
            # attempts 的自动分析把两类完全不同的通道混在一起（键盘注入没有落点坐标）。
            attempts.append({"level": LEVEL_KEY, "ok": ok,
                             "note": "键盘注入(xdotool type/剪贴板粘贴)"})
            msg = "键盘注入输入文本" + ("成功" if ok else "失败")
            # 键盘注入没有「落点」可言，风险是**焦点被别的应用抢走**（实测把文本打进过
            # 飞书/Remmina）。回报输入完的活动窗口标题，模型一眼就能看出打偏了，
            # 不必再截图核对。
            landing = self._landing(self._INJECT_SETTLE)
            if landing:
                msg += f"｜{self._format_landing({}, landing)}"
            if reloc_note:
                # 键盘注入虽然不认目标元素，但「原 ref 已失效」这件事仍成立，
                # 模型据此判断自己基于旧界面的决策是否还成立
                msg += f"｜{reloc_note}"
            if warn:
                msg += f"；{warn}"
            return ActionResult(ok=ok, level=LEVEL_KEY if ok else LEVEL_NONE,
                                message=msg,
                                attempts=attempts, data={"ref": ref, **landing})
        except Exception as exc:  # noqa: BLE001
            attempts.append({"level": LEVEL_KEY, "ok": False, "note": str(exc)})
            return ActionResult(ok=False, level=LEVEL_NONE, message=f"输入失败：{exc}",
                                attempts=attempts)

    @_needs_display
    @_exclusive_screen
    def press_key(self, combo: str) -> ActionResult:
        """
        快捷键（全局键盘注入）。

        回报**按键发出后的活动窗口**：快捷键是否生效依赖焦点在谁身上，而焦点随时可能
        被别的应用抢走（实测有过把按键送进用户其它应用的事故）。标题是判断「有没有
        打偏」最便宜的信号，模型据此不必再截图确认。
        """
        warn = self._sandbox_guard()
        before_active = self._landing().get("active_window")
        try:
            ok = self.backend.press_key(combo)
            msg = f"按键 {combo} " + ("已发送" if ok else "失败")
            # ⚠️ 必须等 settle 再读：alt+F4 这类键会关窗/切焦点，立即读到的还是旧窗口
            # （2026-09-15 实测：关掉 Chrome 后仍报「活动窗口：Chrome」，误导判断）。
            landing = self._landing(self._INJECT_SETTLE)
            if landing or before_active:
                msg += f"｜{self._format_landing({}, landing, before_active)}"
            if warn:
                msg += f"；{warn}"
            return ActionResult(ok=ok, level=LEVEL_KEY if ok else LEVEL_NONE,
                                message=msg,
                                data={"combo": combo, **landing})
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, level=LEVEL_NONE, message=f"按键失败：{exc}")