"""
批量动作（core.coordinator.sequences）—— `act_sequence`，省往返的主力。

一次调用跑完整串步骤，把「模型每轮只能做一件事」摊薄掉。整串是在
`@_exclusive_screen` 下**持屏锁**执行的，故步数 / sleep 秒数 / 截图张数都必须有界
（M-46）—— 没有上限时 `sleep 3600` 能持锁一小时。

`{op:"screenshot"}` 是这套设计里最省的那个：实测任务里三分之二的截图纯粹是
「确认刚才那串动作对不对」，放进同一次调用的最后一步即可省掉整个来回。
"""

from __future__ import annotations

import time
from typing import Any

from ...utils.errors import ComputerUseError, to_friendly_text
from .hooks import _exclusive_screen, _needs_display


class SequenceMixin:
    """`act_sequence` 及其单步执行。"""

    # ================= 批量动作（省往返）=================
    _SEQ_OPS = ("click", "type", "key", "wait", "sleep", "list_windows", "screenshot")

    # 整串 act_sequence 的规模上限（M-46）。为什么需要：整串是在 `@_exclusive_screen`
    # 下**持屏锁**执行的，而此前步数、sleep 秒数、截图张数全都不限 ——
    # `steps=[{"op":"sleep","seconds":3600}]` 能持锁一小时，20 个 screenshot 步骤会把
    # ~20 张 JPEG 塞进同一次响应。以单会话自伤为主（别的会话会立刻拿到 ScreenBusyError
    # 而不是排队），但没有理由留着。数值取「正常批量任务够用、异常输入会被挡下」的量级。
    _SEQ_MAX_STEPS = 50
    _SEQ_MAX_SLEEP = 60.0
    _SEQ_MAX_WAIT = 60.0
    _SEQ_MAX_SCREENSHOTS = 8

    @_needs_display
    @_exclusive_screen
    def act_sequence(self, steps: list[dict], stop_on_error: bool = True) -> dict:
        """
        批量执行一串动作（单次调用完成，显著减少 MCP 往返次数）。

        每个 step 为 dict，op 取值与字段：
          - click : {op,ref?,text?,role?,app?,button?,x?,y?}   （x+y 齐备=裸坐标直点）
          - type  : {op,text,ref?,clear_first?}
          - key   : {op,combo}
          - wait  : {op,title_contains?,window_id?,timeout?}   → wait_window
          - sleep : {op,seconds}
          - list_windows : {op}
          - screenshot   : {op,region?,max_side?}              → 见下方说明
        返回 {ok, steps:[{i,op,ok,message}], stopped_at?}；
        stop_on_error=True 时某步失败即停止后续步骤。

        screenshot 步骤为什么要放进这个串里：以前的流程是「跑一串动作」+「再单独截一次图
        确认」两次调用，后者纯粹为了看一眼结果，却要整整一个来回（实测每轮 30~70 秒，
        而截图本身只要 0.2 秒）。把它作为最后一步，动作与「做完之后长什么样」一次拿到。
        图像不走 JSON，而是挂在返回值的 `_images` 键下（[(bytes, meta), ...]），
        由工具层转成 MCP 的 image content block；调用方序列化前必须 pop 掉它。
        """
        results: list[dict] = []
        images: list[tuple[bytes, dict[str, Any]]] = []
        stopped: int | None = None
        # M-46：步数先在**进循环之前**挡下（整串持屏锁执行，规模必须有界）
        if len(steps or []) > self._SEQ_MAX_STEPS:
            raise ComputerUseError(
                f"一次 act_sequence 最多 {self._SEQ_MAX_STEPS} 步，收到 {len(steps)} 步；"
                f"请拆成多次调用（顺带一提：需要看中间结果做分支判断的步骤本就该拆开）"
            )
        self._seq_shots = 0     # 序列内截图计数（M-46，由 _run_seq_step 自增）
        for i, step in enumerate(steps or []):
            op = (step or {}).get("op")
            try:
                r = self._run_seq_step(op, step or {})
                img = r.pop("_image", None)
                if img is not None:
                    images.append(img)
                ok = bool(r.get("ok", True))
                msg = str(r.get("message", ""))
            except Exception as exc:  # noqa: BLE001
                # M-17：与 tools/ 的其余工具同口径（to_friendly_text）——ComputerUseError
                # 原样给原因（它本就是写给模型看的），其它异常只给「类型 + 原因」、堆栈进
                # 服务端日志。原先直接 str(exc)，等于把内部实现细节（xdotool 退出码原文、
                # gi 的异常串等）原样塞进模型上下文，与别处的口径也不一致。
                ok, msg = False, to_friendly_text(exc, f"步骤 {i}（{op}）执行失败")
            results.append({"i": i, "op": op, "ok": ok, "message": msg})
            if not ok and stop_on_error:
                stopped = i
                break
        all_ok = all(r["ok"] for r in results) and stopped is None
        out: dict = {"ok": all_ok, "steps": results}
        if stopped is not None:
            out["stopped_at"] = stopped
        if images:
            out["_images"] = images
        return out

    def _run_seq_step(self, op: str | None, step: dict) -> dict:
        """执行 act_sequence 的单个 step，返回 {ok,message}。"""
        if op == "click":
            x, y = step.get("x"), step.get("y")
            # M-40：与 tools/action.py::click 同一条校验 —— 只给一个分量时**当场报错**，
            # 别落进元素分支最后回一句「未找到目标元素」（那对「少给一个分量」这个错误
            # 反馈完全指错方向）。act_sequence 是模型最常用的入口，这条更容易被踩。
            if (x is None) != (y is None):
                raise ComputerUseError(
                    f"step[{op}] 的 x 与 y 必须同时给出（或都不给），实际给了 "
                    f"x={x!r} y={y!r}"
                )
            if x is not None and y is not None:
                r = self.click_xy(int(x), int(y), button=int(step.get("button", 1)))
            else:
                r = self.click(ref=step.get("ref"), text=step.get("text"),
                               role=step.get("role"),
                               app=step.get("app"), button=int(step.get("button", 1)))
            return {"ok": r.ok, "message": r.message}
        if op == "type":
            r = self.type_text(text=str(step.get("text", "")), ref=step.get("ref"),
                               clear_first=bool(step.get("clear_first", False)))
            return {"ok": r.ok, "message": r.message}
        if op == "key":
            r = self.press_key(str(step.get("combo", "")))
            return {"ok": r.ok, "message": r.message}
        if op == "wait":
            timeout = float(step.get("timeout", 10.0))
            if timeout > self._SEQ_MAX_WAIT:
                raise ComputerUseError(
                    f"wait.timeout={timeout} 超过单步上限 {self._SEQ_MAX_WAIT}s；"
                    f"整串动作是**持屏锁**执行的，长时间占屏会挡住其它（子）agent"
                )
            info = self.wait_window(title_contains=step.get("title_contains"),
                                    window_id=step.get("window_id"),
                                    timeout=timeout)
            return {"ok": info is not None, "message": f"wait -> {info}"}
        if op == "sleep":
            seconds = float(step.get("seconds", 0.2))
            # M-46：`steps=[{"op":"sleep","seconds":3600}]` 会**持屏锁**睡一小时 ——
            # 以单会话自伤为主（别人会立刻拿到 ScreenBusyError，不会排队），
            # 但锁死一块屏一小时没有正当理由，给个够用的上限。
            if seconds > self._SEQ_MAX_SLEEP:
                raise ComputerUseError(
                    f"sleep.seconds={seconds} 超过单步上限 {self._SEQ_MAX_SLEEP}s；"
                    f"整串动作是**持屏锁**执行的（见 act_sequence 的说明）"
                )
            time.sleep(max(0.0, seconds))
            return {"ok": True, "message": "slept"}
        if op == "list_windows":
            ws = self.list_windows()
            return {"ok": True, "message": f"{len(ws)} windows"}
        if op == "screenshot":
            # M-46：序列内的截图张数也要有上限 —— 每张都会变成响应里的一个 image
            # content block，20 张 JPEG 会把一次响应撑得极大。
            self._seq_shots += 1
            if self._seq_shots > self._SEQ_MAX_SCREENSHOTS:
                raise ComputerUseError(
                    f"一次 act_sequence 内的截图步数超过上限 {self._SEQ_MAX_SCREENSHOTS}；"
                    f"截图很贵（token），确需更多请拆成多次调用"
                )
            # 图像不塞进 message（那是文本），而是以 _image 键回传，由 act_sequence
            # 收集到 _images，最终由工具层转成 MCP 的 image content block。
            reg = step.get("region")
            if reg is not None and len(reg) != 4:
                # M-41 同类：长度不对原先被静默丢成全屏（慢且看错区域）
                raise ComputerUseError(
                    f"screenshot.region 需为 [x, y, w, h] 四个整数，实际给了 {len(reg)} 个"
                )
            reg = tuple(reg) if reg else None
            data, meta = self.screenshot_image(reg, max_side=int(step.get("max_side", 1280)))
            return {"ok": True, "message": f"截图 meta={meta}", "_image": (data, meta)}
        raise ComputerUseError(f"act_sequence 未知 op: {op!r}（支持 {self._SEQ_OPS}）")