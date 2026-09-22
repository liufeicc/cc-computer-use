"""
批量动作（core.coordinator.sequences）—— `act_sequence`，省往返的主力。

一次调用跑完整串步骤，把「模型每轮只能做一件事」摊薄掉。整串是在
`@_exclusive_screen` 下**持屏锁**执行的，故步数 / sleep 秒数 / 截图张数都必须有界
（M-46）—— 没有上限时 `sleep 3600` 能持锁一小时。

`{op:"screenshot"}` 是这套设计里最省的那个：实测任务里三分之二的截图纯粹是
「确认刚才那串动作对不对」，放进同一次调用的最后一步即可省掉整个来回。

`{op:"ui_tree"}` 补的是同一类浪费的**另一半**：`screenshot` 管「看像素」，
它管「看结构」。「点开菜单 → 读菜单里有什么」原先也只能拆成两次调用。两者都是
「把下一步的观察折进这一次调用」，区别只在看的是结构（文本、便宜）还是像素（贵）。

`click` 的 `text` 支持**候选名列表**（`["保存","Save"]`，命中即停），解决「不确定
目标叫什么」——理由见 `_pick_existing_name` 的 docstring，那里也写清了它的能力边界。
"""

from __future__ import annotations

import time
from typing import Any

from ...backend.base import BUTTON_NUMBERS
from ...utils.errors import ComputerUseError, ElementNotFoundError, to_friendly_text
from .hooks import _exclusive_screen, _needs_display


def _seq_button(value: Any, op: str) -> int:
    """op 里的鼠标按键名 → X11 按钮号；非法值**当场报错**（不静默当左键）。"""
    name = str(value if value is not None else "left").strip().lower()
    if name not in BUTTON_NUMBERS:
        raise ComputerUseError(
            f"{op}.button 只能是 left / middle / right，实际给了 {value!r}"
        )
    return BUTTON_NUMBERS[name]


class SequenceMixin:
    """`act_sequence` 及其单步执行。"""

    # ================= 批量动作（省往返）=================
    _SEQ_OPS = ("click", "double_click", "type", "key", "wait", "sleep", "list_windows",
                "ui_tree", "scroll", "drag", "screenshot")

    # 整串 act_sequence 的规模上限（M-46）。为什么需要：整串是在 `@_exclusive_screen`
    # 下**持屏锁**执行的，而此前步数、sleep 秒数、截图张数全都不限 ——
    # `steps=[{"op":"sleep","seconds":3600}]` 能持锁一小时，20 个 screenshot 步骤会把
    # ~20 张 JPEG 塞进同一次响应。以单会话自伤为主（别的会话会立刻拿到 ScreenBusyError
    # 而不是排队），但没有理由留着。数值取「正常批量任务够用、异常输入会被挡下」的量级。
    _SEQ_MAX_STEPS = 50
    _SEQ_MAX_SLEEP = 60.0
    _SEQ_MAX_WAIT = 60.0
    _SEQ_MAX_SCREENSHOTS = 8
    # 序列内**读树**步数上限。树是文本（比截图便宜），但它一步就是几百行，
    # 5 棵已经是「一次调用里连读五屏」的量级，再多就该拆调用重新规划了。
    _SEQ_MAX_TREES = 5
    # 序列内读树的节点上限。默认值刻意小于 ui_tree 工具的 400：序列里读树是为了
    # 「看一眼刚操作完的界面长什么样」，不是全量分析，几百行会把响应撑得很难读。
    _SEQ_TREE_DEFAULT_NODES = 150
    _SEQ_TREE_MAX_NODES = 400

    @_needs_display
    @_exclusive_screen
    def act_sequence(self, steps: list[dict], stop_on_error: bool = True) -> dict:
        """
        批量执行一串动作（单次调用完成，显著减少 MCP 往返次数）。

        每个 step 为 dict，op 取值与字段：
          - click : {op,ref?,text?,role?,app?,button?,x?,y?}   （x+y 齐备=裸坐标直点）
                     text 可给**候选名列表**：依次找哪个名字真的存在，**命中即停**，见下方说明
          - double_click : {op,x,y,button?}   → 双击。**必须给 x/y**（元素级没有双击语义）
          - scroll : {op,direction?,amount?,x?,y?}   → 滚动。direction=up/down，
                     amount=刻度数（一格 = 一次滚轮事件）；x/y 是作用点，不给用活动窗口中心
          - drag   : {op,from_x,from_y,to_x,to_y,button?,steps?}   → 拖拽（起点→终点）
          - type  : {op,text,ref?,clear_first?}
          - key   : {op,combo}
          - wait  : {op,title_contains?,window_id?,timeout?}   → wait_window
          - sleep : {op,seconds}
          - list_windows : {op}
          - ui_tree : {op,scope?,app?,interactive_only?,max_nodes?} → get_ui_tree
          - screenshot   : {op,region?,max_side?}              → 见下方说明
        返回 {ok, steps:[{i,op,ok,message}], stopped_at?}；
        stop_on_error=True 时某步失败即停止后续步骤。

        screenshot 步骤为什么要放进这个串里：以前的流程是「跑一串动作」+「再单独截一次图
        确认」两次调用，后者纯粹为了看一眼结果，却要整整一个来回（实测每轮 30~70 秒，
        而截图本身只要 0.2 秒）。把它作为最后一步，动作与「做完之后长什么样」一次拿到。
        图像不走 JSON，而是挂在返回值的 `_images` 键下（[(bytes, meta), ...]），
        由工具层转成 MCP 的 image content block；调用方序列化前必须 pop 掉它。

        ui_tree 步骤解决的是同一类浪费的**另一半**：「点开菜单 → 读菜单里有什么」以前
        也只能拆成两次调用。它与 screenshot 的分工是——**看结构用 ui_tree（文本、便宜），
        看像素才用 screenshot（贵）**；灰区应用没有元素树，只有 screenshot 这条路。
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
        self._seq_trees = 0     # 序列内读树计数（同 M-46 思路，由 _run_seq_step 自增）
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
                return {"ok": r.ok, "message": r.message}
            ref, text = step.get("ref"), step.get("text")
            hit: str | None = None
            if isinstance(text, (list, tuple)):
                # 候选名列表：见 _pick_existing_name 的 docstring（为何是「命中即停」）
                ref, hit = self._pick_existing_name(text, step.get("role"), step.get("app"))
                text = hit
            r = self.click(ref=ref, text=text, role=step.get("role"),
                           app=step.get("app"), button=int(step.get("button", 1)))
            # 命中的是哪个名字必须回给模型：它本来就不确定名字，不告诉它就只能靠再读一次
            # 树去猜——那正是本机制要消掉的那个来回。
            msg = f"{r.message}｜候选名命中「{hit}」" if hit else r.message
            return {"ok": r.ok, "message": msg}
        if op == "double_click":
            # 与独立工具 double_click 同源。**双击必须给 x/y**：元素级动作没有双击
            # 语义，所以这里不存在「用 ref 也能双击」的空间——早报错比让模型以为
            # 传了 ref 就行要好。
            x, y = step.get("x"), step.get("y")
            if x is None or y is None:
                raise ComputerUseError(
                    "double_click 必须给完整的 x 与 y（双击只能走坐标级：元素级只有"
                    "「激活/单击」，没有双击这个语义；可先用 element_info 取元素矩形的中心）"
                )
            r = self.click_xy(int(x), int(y), button=_seq_button(step.get("button"), op),
                              clicks=2)
            return {"ok": r.ok, "message": r.message}
        if op == "scroll":
            r = self.scroll_xy(direction=str(step.get("direction", "down")),
                               amount=int(step.get("amount", 5)),
                               x=step.get("x"), y=step.get("y"))
            return {"ok": r.ok, "message": r.message}
        if op == "drag":
            need = ("from_x", "from_y", "to_x", "to_y")
            missing = [k for k in need if step.get(k) is None]
            if missing:
                raise ComputerUseError(
                    f"drag 需要起点与终点四个坐标，缺少 {missing}；"
                    f"形如 {{op:'drag', from_x:100, from_y:200, to_x:400, to_y:200}}"
                )
            r = self.drag_xy(int(step["from_x"]), int(step["from_y"]),
                             int(step["to_x"]), int(step["to_y"]),
                             button=_seq_button(step.get("button"), op),
                             steps=int(step.get("steps", 10)))
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
        if op == "ui_tree":
            # 与截图同源的浪费：「点开菜单 → 读菜单里有什么」原先也得拆成两次调用。
            # 树走 message（它就是文本，天然 JSON 可序列化），故不必像截图那样单开
            # `_image` 旁路；代价是它直接进模型上下文，**节点上限因此更要收紧**。
            self._seq_trees += 1
            if self._seq_trees > self._SEQ_MAX_TREES:
                raise ComputerUseError(
                    f"一次 act_sequence 内的读树步数超过上限 {self._SEQ_MAX_TREES}；"
                    f"一次调用连读这么多屏已经超出「顺手确认」的范畴，"
                    f"请拆成多次调用重新规划"
                )
            max_nodes = int(step.get("max_nodes", self._SEQ_TREE_DEFAULT_NODES))
            if max_nodes > self._SEQ_TREE_MAX_NODES:
                # 与 screenshot.region（M-41）同口径：越界的参数**当场报错**，
                # 不静默截断成另一个值——模型以为自己要了多少就该是多少。
                raise ComputerUseError(
                    f"ui_tree.max_nodes={max_nodes} 超过序列内上限 "
                    f"{self._SEQ_TREE_MAX_NODES}（工具 get_ui_tree 的默认值）；"
                    f"确需整棵大树请单独调用 get_ui_tree"
                )
            scope = step.get("scope", "active_window")
            if scope not in ("active_window", "app", "desktop"):
                # 与 tools/ui_tree.py 的 Literal 同口径（M-26/M-41）：非法 scope 静默
                # 降级成别的范围，模型拿到的是**另一种**结果却不自知。
                raise ComputerUseError(
                    f'ui_tree.scope 只能是 "active_window" / "app" / "desktop"，'
                    f"实际给了 {scope!r}"
                )
            tree = self.get_ui_tree(
                scope=scope, app=step.get("app"),
                interactive_only=bool(step.get("interactive_only", False)),
                max_nodes=max_nodes,
            )
            return {"ok": True, "message": tree}
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

    def _pick_existing_name(
        self, names: Any, role: str | None, app: str | None,
    ) -> tuple[int, str]:
        """
        候选名列表 → 第一个**真的存在于元素树**的名字，返回 (ref, 该名字)。

        解决的是哪种失败：模型不确定目标叫什么（中文界面「保存」/ 英文界面「Save」/
        某些应用叫「另存为」）。原先只能先点一个、失败了再开一次调用试下一个 —— 每个
        候选一次往返（实测 30~70 秒），而它本可以在一张「纸条」里写完。

        为什么必须是「命中即停」，而不是「失败就继续试下一个」—— 这是本方法存在的
        全部理由，别改回去：`stop_on_error=false` 那种语义在这里恰恰是**错的**。
        中文界面上第一个候选「保存」已经点中、对话框都关了，程序若还接着去找「Save」，
        运气好是白搜一次，运气差就点到别的窗口上。候选列表表达的是「同一个意图的几种
        写法」，所以找到了就不该再看后面的。

        为什么判定标准只能是「找不找得到」：那是程序**自己**能确定的事实（在树文本里
        做字符串匹配），不需要任何判断力，所以放在服务端做是安全的。反过来，「点了保存、
        弹出『文件已存在，是否覆盖』」这类「执行了但不对」判定不了，只能把结果交回模型。
        故本机制**只**覆盖「名字不确定」这一类，别指望它替代分支判断。

        为什么命中后返回 ref 而不是名字：调用方接着 click(ref=...)，走的是 `_resolve_native`
        的快路径，**不会再搜一次树**（走名字则要重搜）。
        """
        tried: list[str] = []
        for name in names:
            try:
                native, _ = self._target_native(None, str(name), role, app)
            except ElementNotFoundError:
                tried.append(str(name))
                continue
            ref = self.refs.register(native, role=role or "", name=str(name), app=app or "")
            return ref, str(name)
        # 试过哪些名字要**全部**列出：只报最后一个会让模型以为只搜了一次，
        # 据此判断「这个界面是英文的」就是错的。
        raise ElementNotFoundError(
            f"候选名 {tried} 在元素树里一个都找不到（已按顺序全部搜过）。"
            f"要么换成界面里真实存在的名字，要么先读树/get_screen_text 看清它到底叫什么"
        )