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

import time
from typing import Any

from ...utils.errors import (
    LEVEL_COORD,
    LEVEL_ELEMENT,
    LEVEL_KEY,
    LEVEL_NONE,
    ActionResult,
    ComputerUseError,
    ElementNotFoundError,
    InvalidRefError,
)
from .hooks import _exclusive_screen, _needs_display

# 连击次数的中文名：回报给模型时用它（「坐标级双击已执行」比「坐标级 2 连击已执行」好读）
_CLICK_NAMES = {1: "点击", 2: "双击", 3: "三击"}


class ActionMixin:
    """点击 / 输入 / 按键（三级降级）。"""

    # 一次滚动最多多少格。滚轮同样走**持屏锁**的坐标通道：`amount=100000` 会让 xdotool
    # 用 --repeat 发十万次滚轮事件（几十秒起步），把屏锁白白占死。
    _SCROLL_MAX_AMOUNT = 200
    # 拖拽插值步数上限。步与步之间还有 hold 秒停顿（见 injector.drag），步数过多会拖成秒级。
    _DRAG_MAX_STEPS = 200

    # ================= 操作（三级降级）=================
    @_needs_display
    @_exclusive_screen
    def click_xy(
        self, x: int, y: int, button: int = 1, preview: bool | None = None,
        expect: str | None = None, clicks: int = 1,
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

        `clicks>1` 即连击（双击/三击，供 `double_click` 工具使用）。**双击只能走这条
        坐标级通道**——AT-SPI 的 do_action 只有 activate/click，没有"双击"这个动作，
        所以元素级再精准也表达不了它。连击的**间隔由注入层定时**（xdotool --repeat/
        --delay），与这里的落点证据、OCR、变化对比等开销彻底解耦；本方法自身仍然照常
        做证据链（双击比单击更怕点偏，反馈一个都不能少）。
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
        action_name = _CLICK_NAMES.get(clicks, f"{clicks}连击")
        try:
            ok = self.backend.click_at(x, y, button=button, focus_window=True,
                                       repeat=int(clicks))
        except Exception as exc:  # noqa: BLE001
            # 失败恰恰最需要「它本来想点哪、那儿有什么」——评估文字照常给出
            msg = f"坐标{action_name}失败：{exc}"
            if aim_text:
                msg += f"｜{aim_text}"
            return ActionResult(ok=False, level=LEVEL_COORD, message=msg,
                                attempts=[{"level": LEVEL_COORD, "ok": False, "note": str(exc)}],
                                data={"x": x, "y": y, **ev}, preview=payload)
        msg = f"坐标级{action_name}已执行 @({x},{y})"
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

    # ================= 滚动 / 拖拽（裸坐标动作）=================
    @_needs_display
    @_exclusive_screen
    def scroll_xy(
        self, direction: str = "down", amount: int = 5,
        x: int | None = None, y: int | None = None,
        preview: bool | None = None,
    ) -> ActionResult:
        """
        滚动：把指针移到作用点，发 `amount` 格滚轮事件（一格 = 一次 button 4/5）。

        几个刻意的取舍：
          1. **作用点必须明确**：滚轮事件由 X 发给**指针下那个窗口**，所以「在哪儿滚」
             决定滚谁。给了 x/y 就用它，否则取**活动窗口中心**——绝不沿用「指针恰好在哪」
             （那是上一次操作留下的位置，不可预测，排查起来也毫无线索）。
          2. 上滚=button 4、下滚=button 5；`amount` 格交给注入层的 `--repeat` 一次发出。
          3. **不聚焦窗口**（focus_window=False）：滚动是在看内容，抢焦点纯属副作用。
          4. 反馈只给**界面变化百分比**：滚动没有「落点」可言（指针只是作用点），而
             「内容到底动没动」正是这一下唯一需要确认的事，也正好是不依赖意图的客观信号。

        ⚠️ **滚多远不可预知，别指望一次到位**：一格滚多少内容由**应用**决定（GTK 约 3 行、
        浏览器按比例、画布应用按像素），故 `amount` 只能给量级。正确用法是「滚一下 →
        看结果 → 不够再滚」，配合 act_sequence 的 ui_tree/screenshot 步骤一次提交。
        """
        word = (direction or "down").strip().lower()
        if word not in ("up", "down"):
            raise ComputerUseError(
                f"scroll 的 direction 只能是 'up' 或 'down'，实际给了 {direction!r}"
            )
        amount = int(amount)
        if amount < 1:
            raise ComputerUseError(f"scroll 的 amount 至少为 1 格，实际给了 {amount}")
        if amount > self._SCROLL_MAX_AMOUNT:
            raise ComputerUseError(
                f"scroll 的 amount={amount} 超过上限 {self._SCROLL_MAX_AMOUNT} 格；"
                f"滚动是持屏锁执行的坐标动作，一次滚这么多会长时间占屏"
            )
        x, y, src = self._scroll_point(x, y)
        warn = self._sandbox_guard()
        # 预览图（注入前抓）：它的**原始图**后面要做点后对比，故与 click_xy 一样只抓一次
        shot = self._preview_image(x, y, preview)
        try:
            ok = self.backend.click_at(x, y, button=4 if word == "up" else 5,
                                       focus_window=False, repeat=amount)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(
                ok=False, level=LEVEL_COORD, message=f"滚动失败：{exc}",
                attempts=[{"level": LEVEL_COORD, "ok": False, "note": str(exc)}],
                data={"x": x, "y": y, "direction": word, "amount": amount},
            )
        msg = (f"已向{'上' if word == 'up' else '下'}滚动 {amount} 格 @({x},{y})"
               f"（作用点：{src}）")
        time.sleep(self._INJECT_SETTLE)          # 给应用时间处理滚轮事件，再做点后对比
        change = self._change_from_preview(shot)
        if change:
            msg += f"｜{change}"
        else:
            # 「没变化」**不等于滚失败**（可能已到列表尽头）。不写清楚的话，模型会把
            # 它读成失败而反复重试——而重试同样不会有变化，于是白白耗尽轮数。
            msg += "｜未检出界面变化（可能已到内容尽头，或该处本就无内容可滚）"
        if warn:
            msg += f"；{warn}"
        return ActionResult(
            ok=ok, level=LEVEL_COORD, message=msg,
            attempts=[{"level": LEVEL_COORD, "ok": ok,
                       "note": f"scroll {word} x{amount} @({x},{y})"}],
            data={"x": x, "y": y, "direction": word, "amount": amount},
            preview=(shot.data, shot.meta) if shot else None,
        )

    def _scroll_point(self, x: int | None, y: int | None) -> tuple[int, int, str]:
        """滚动作用点：显式坐标优先，否则活动窗口中心。返回 (x, y, 来源说明)。"""
        if x is not None and y is not None:
            return int(x), int(y), "指定坐标"
        rect = self.backend.active_window_rect()
        if rect is not None and not rect.is_empty():
            cx, cy = rect.center
            return cx, cy, "活动窗口中心"
        raise ComputerUseError(
            "scroll 需要 x/y 指明「在哪儿滚」，或至少有一个可用的活动窗口；"
            "当前既未给坐标、也取不到活动窗口矩形"
        )

    @_needs_display
    @_exclusive_screen
    def drag_xy(
        self, x1: int, y1: int, x2: int, y2: int, button: int = 1,
        steps: int = 10, preview: bool | None = None,
    ) -> ActionResult:
        """
        拖拽：从 (x1,y1) 按下、拖到 (x2,y2) 抬起。

        实现逻辑：
          1. 起点**落点证据**（注入前抓）——拖拽最典型的失败是**抓错起点**（抓到别的控件、
             或没抓住手柄），而终点由参数给定、不会有歧义，故证据只取起点。
          2. 预览图（起点）→ 注入 → 点后变化对比：与 click_xy 同一套，且复用同一次抓屏。
          3. 插值与节奏控制在 `injector.drag`（必须插值、必须拼成一条命令，理由见那里）。

        ⚠️ 失败**不重试**（与 click_xy 的 M-34 分支不同）：重试一次拖拽 = 对同一目标
        再拖一遍（文件移动两次、画两笔），后果不对称，故直接报失败让模型重新感知。
        """
        steps = int(steps)
        if steps < 1:
            raise ComputerUseError(f"drag 的 steps（插值步数）至少为 1，实际给了 {steps}")
        if steps > self._DRAG_MAX_STEPS:
            raise ComputerUseError(
                f"drag 的 steps={steps} 超过上限 {self._DRAG_MAX_STEPS}；"
                f"步数过多会让这次拖拽拖成秒级（每步之间还有停顿）"
            )
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        if (x1, y1) == (x2, y2):
            raise ComputerUseError("drag 的起点与终点相同，构不成拖拽（按住又原地松开）")
        warn = self._sandbox_guard()
        before_active = self._landing().get("active_window")
        shot = self._preview_image(x1, y1, preview)
        # 起点的落点文字由裁剪预览图 OCR 得出（同一份像素复用）；没有预览图时退回小块 OCR
        ev = self._point_evidence(x1, y1, with_text=shot is None)
        try:
            ok = self.backend.drag_at(x1, y1, x2, y2, button=button, steps=steps)
        except Exception as exc:  # noqa: BLE001
            msg = f"拖拽失败：{exc}"
            landing = self._format_landing(ev, {}, before_active)
            if landing:
                msg += f"｜{landing}"
            return ActionResult(
                ok=False, level=LEVEL_COORD, message=msg,
                attempts=[{"level": LEVEL_COORD, "ok": False, "note": str(exc)}],
                data={"x1": x1, "y1": y1, "x2": x2, "y2": y2, **ev},
            )
        msg = f"已拖拽 ({x1},{y1}) → ({x2},{y2})"
        landing = self._format_landing(ev, self._landing(self._INJECT_SETTLE), before_active)
        if landing:
            msg += f"｜{landing}"
        change = self._change_from_preview(shot)     # 必须在 settle 之后
        if change:
            msg += f"｜{change}"
        if warn:
            msg += f"；{warn}"
        return ActionResult(
            ok=ok, level=LEVEL_COORD, message=msg,
            attempts=[{"level": LEVEL_COORD, "ok": ok,
                       "note": f"drag ({x1},{y1})->({x2},{y2}) steps={steps}"}],
            data={"x1": x1, "y1": y1, "x2": x2, "y2": y2, **ev},
            preview=(shot.data, shot.meta) if shot else None,
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