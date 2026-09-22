"""工具：click / type_text / press_key —— 执行层（三级降级）。"""

from __future__ import annotations

from typing import Any

from ..core.coordinator import Coordinator
from ..utils import blocks
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def _dump_temp(data: bytes, meta: dict) -> tuple[str, dict]:
    """
    把图像字节落到临时文件（仅在拿不到 MCP Image 类型的退化路径上用）。

    与 `screenshot(inline=false)` 的自动路径同属一类泄漏：写一次留一个，没人回收。
    故走 `utils/temps` 的同一套约定（文件名带 pid 归属 + LRU 裁剪），判据与理由
    见 `utils/blocks.dump_temp_image` 的 docstring。
    """
    return blocks.dump_temp_image(data, meta, "cc-cu-seq"), meta


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 click / type_text / press_key 工具。"""

    @mcp.tool(
        name="click",
        description=(
            "点击一个元素。优先传 ref（来自 get_ui_tree/find_element/get_screen_text）；"
            "也可传 text(+app) 现场匹配第一个元素。"
            "执行采用三级降级：①元素级 do_action（零坐标，首选）→ ②校准坐标点击（兜底）→ "
            "③都失败则提示改用 screenshot。返回结果会标明实际生效的层级。"
            "**坐标级点击会回报「落点证据」**（落在哪扇窗、底下什么字、点后活动窗口）、"
            "**一张点击前抓的准星小图**（红十字 = 你刚才点的那个像素，未缩放、"
            "图上 1 像素 = 屏幕 1 像素），以及**程序算好的偏差数值**："
            "最近文字块离落点多远；若你传了 expect（如 expect=\"保存\"），还会直接给出"
            "「未命中，偏差 (+17,-42) 共45px，建议改点其中心 (517,258)」——按建议坐标重试即可，"
            "**不必再截图估算**。另有「点后界面变化」百分比作为命中参考（无变化 = 大概率点空）。"
            "preview=false 可关掉这套画面反馈（更省 token，代价是失去位置依据）。"
            "灰区应用（无元素树，如 SWT/自绘控件）可传裸坐标 x+y（屏幕绝对坐标，"
            "可先用 get_screen_text 读出坐标）直接坐标级点击，无需再借道 shell。"
        ),
    )
    def click(
        ref: int | None = None,
        text: str | None = None,
        role: str | None = None,
        app: str | None = None,
        button: int = 1,
        action: str | None = None,
        x: int | None = None,
        y: int | None = None,
        preview: bool = True,
        expect: str | None = None,
    ) -> Any:
        """
        点击（三级降级）。

        ⚠️ 返回注解必须是 `Any` 而**不是 `str`**（实测 mcp 2.2.0）：注解为 `str` 时 SDK 会
        在注册期生成 outputSchema 并在调用返回时 `validate_python` 校验，而**带预览图的那次
        点击返回的是 content block 列表**（文本 + Image + meta 文本）→ ValidationError；
        症状是「平时全绿、一开启预览就崩」，极难反查。`Any` 时 output_schema 为 None，
        走 content 展开路径，列表才能正常返回。
        无图时仍返回纯字符串（保持既有语义与断言不变）。
        """
        try:
            # M-40：x/y **只给一个**时必须当场报错。历史行为是落进下面的元素分支，
            # 最终回一句「未找到目标元素…建议改用 screenshot」——对「少给一个分量」这个
            # 错误**反馈完全指错方向**，模型会去排查元素树、白多一个来回。
            if (x is None) != (y is None):
                raise ComputerUseError(
                    f"x 与 y 必须同时给出（或都不给）；只给了 "
                    f"{'x=' + str(x) if x is not None else 'y=' + str(y)}。"
                    f"坐标点击需要完整的一对屏幕绝对坐标。"
                )
            if x is not None and y is not None:
                if ref is not None or text is not None or role is not None or action is not None:
                    # 原先静默优先 x/y，模型无从知道自己给的 ref/action 被忽略了。
                    raise ComputerUseError(
                        "x/y（裸坐标点击）与 ref/text/role/action（元素级点击）不能混用；"
                        "请二选一——元素级优先，只在灰区应用（无元素树）才用裸坐标。"
                    )
                result = coord.click_xy(x, y, button=button, preview=preview, expect=expect)
            else:
                result = coord.click(ref=ref, text=text, role=role, app=app,
                                     button=button, action=action, preview=preview)
        except ComputerUseError as exc:
            return f"❌ 点击失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "点击失败")
        # 预览图挂在 ActionResult.preview（bytes）——**不能进 data**（会被 to_text() repr 进
        # 上下文），故在这里单独取出、转成 MCP Image content block（文本在前、图与 meta 在后）。
        images = [result.preview] if result.preview else []
        return blocks.image_blocks(
            result.to_text(), images, caption="点击预览 meta", family="cc-cu-click",
            note="红十字中心 = 实际落点 (x,y) 的屏幕绝对坐标；本图**未缩放**，"
                 "图上 1 像素 = 屏幕 1 像素，屏幕坐标 = origin + 图上坐标",
        )

    @mcp.tool(
        name="type_text",
        description=(
            "输入文本。若给 ref（文本框/输入区），优先用元素级 set_value（最稳，不受焦点影响）；"
            "否则/失败时降级为键盘注入（xdotool type）。clear_first=true 先全选删除原内容再输入。"
            "建议：先 click(ref) 聚焦目标输入框，再 type_text。"
            "键盘注入会回报**输入后的活动窗口标题**——焦点被别的应用抢走时一眼可见，"
            "不必再截图核对。"
        ),
    )
    def type_text(
        text: str,
        ref: int | None = None,
        app: str | None = None,
        clear_first: bool = False,
    ) -> str:
        try:
            result = coord.type_text(text=text, ref=ref, app=app, clear_first=clear_first)
            return result.to_text()
        except ComputerUseError as exc:
            return f"❌ 输入失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "输入失败")

    @mcp.tool(
        name="press_key",
        description=(
            "发送快捷键（全局键盘注入）。combo 形如 'ctrl+s'、'alt+F4'、'Return'、'Tab'、"
            "'ctrl+shift+t'。修饰键用 ctrl/alt/shift/super；常见别名（PageDown/Enter/Esc/"
            "方向键等）会自动归一为 xdotool keysym；键名不识别会明确报错而非静默无操作。"
            "会回报**按键后的活动窗口标题**（快捷键生效与否取决于焦点在谁身上）。"
        ),
    )
    def press_key(combo: str) -> str:
        try:
            result = coord.press_key(combo)
            return result.to_text()
        except ComputerUseError as exc:
            return f"❌ 按键失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "按键失败")

    @mcp.tool(
        name="get_last_click_image",
        description=(
            "回看**最近一次坐标点击**的准星小图与结论（index=-1 最近，-2 上上次）。"
            "⭐ 什么时候用它：坐标点击之后**界面没有预期反应**（没弹窗、没跳转、没变化）时，"
            "先用它看清「刚才到底点在哪、程序算的偏差是多少、建议改点哪个坐标」，再据此修正重试；"
            "**不要盲目重复点击**（实测那是最容易把界面点花的做法），也不必重新截图重新估算。"
            "返回里含：落点坐标、当时程序算出的偏差与建议坐标、点后界面变化百分比。"
            "记录只保留本会话内最近 8 次坐标点击；元素级点击零坐标、不产生记录。"
        ),
    )
    def get_last_click_image(index: int = -1) -> Any:
        try:
            result = coord.get_last_click_image(index=index)
        except ComputerUseError as exc:
            return f"❌ 回看失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            raise to_tool_error(exc, "回看失败")
        images = [result.preview] if result.preview else []
        if not images and result.ok:
            # 有记录但没图（那次 preview 被关掉）：说清楚，别让模型以为工具坏了
            return (result.to_text()
                    + "｜注意：该次点击未保存预览图（当时 preview=false 或抓图失败）")
        return blocks.image_blocks(
            result.to_text(), images, caption="点击回看 meta", family="cc-cu-click",
            note="红十字中心 = 当时**实际点击**的像素，屏幕绝对坐标 = origin + 图上坐标"
                 "（本图未缩放，图上 1 像素 = 屏幕 1 像素）",
        )

    @mcp.tool(
        name="act_sequence",
        description=(
            "一次调用批量执行一串动作——**省往返的关键工具**。"
            "⭐ 判据：**凡是下一步不依赖上一步结果的连续动作，一律合并成一次 act_sequence**；"
            "每多一次单独调用，就多一个「模型思考 + 读结果」的来回（实测每轮 30~70 秒，"
            "而工具本身只要零点几秒——**慢的是往返，不是操作**）。"
            "只有**需要看结果做分支判断**时才拆成多次调用。"
            "steps 每项含 op 字段：click{ref?,text?,role?,app?,button?,x?,y?} / "
            "double_click{x,y,button?} / "
            "scroll{direction?,amount?,x?,y?} / "
            "drag{from_x,from_y,to_x,to_y,button?,steps?} / "
            "type{text,ref?,clear_first?} / key{combo} / wait{title_contains?,window_id?,timeout?} / "
            "sleep{seconds} / list_windows{} / ui_tree{scope?,app?,interactive_only?,max_nodes?} / "
            "screenshot{region?,max_side?}。"
            "两种「把观察也折进来」的写法，都是省掉一次单独调用："
            "① **ui_tree 放最后** → 同一次调用里拿到「这一串做完之后界面的结构」，"
            "看结构优先用它（文本、便宜）；"
            "② screenshot → 确实需要看像素时才用（贵）。"
            "**click 的 text 可给候选名列表**（如 [\"保存\",\"Save\"]）：按顺序找哪个名字真的"
            "存在于界面，**命中即停**——不确定目标叫什么时用它，省掉「试一个、失败再来一次」"
            "的整个来回。注意它只解决「名字不确定」；「点了但弹出的对话框不是我要的」这类"
            "仍需你自己看结果判断。"
            "stop_on_error=true 时某步失败即停止后续。"
            "返回每步 ok/message 的 JSON；点击类步骤的 message 里带**落点证据**"
            "（落在哪扇窗、底下什么字、点后活动窗口），据此判断有没有点偏，不必再截图确认。"
        ),
    )
    def act_sequence(steps: list[dict], stop_on_error: bool = True) -> Any:
        import json

        # 与 screenshot 工具同样的 v1/v2 兼容导入（MCPServer.Image / FastMCP.Image）
        MCPImage = None
        try:
            from mcp.server.mcpserver import Image as MCPImage  # type: ignore  # mcp 2.x
        except Exception:  # noqa: BLE001
            try:
                from mcp.server.fastmcp import Image as MCPImage  # type: ignore  # mcp 1.x
            except Exception:  # noqa: BLE001
                MCPImage = None

        try:
            res = coord.act_sequence(steps, stop_on_error=stop_on_error)
        except ComputerUseError as exc:
            return f"❌ 批量动作失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "批量动作失败")

        # 截图步骤的图像挂在 _images 下（bytes，无法 JSON 序列化）：先取出再序列化，
        # 然后作为额外的 image content block 追加——一次调用同时拿到「步骤日志 + 图像」。
        images: list = res.pop("_images", [])
        text = json.dumps(res, ensure_ascii=False)
        if not images:
            return text
        if MCPImage is None:
            # 退化路径：拿不到 Image 类型时把图落盘，至少给出文件路径（比丢掉强）
            paths = []
            for data, meta in images:
                p, m = _dump_temp(data, meta)
                paths.append(f"{p} (meta={m})")
            return text + "\n截图已落盘: " + "; ".join(paths)
        blocks: list[Any] = [text]
        for data, meta in images:
            blocks.append(MCPImage(data=data, format=meta.get("format", "jpeg")))
            blocks.append(f"截图 meta: {meta}"
                          "（屏幕绝对坐标 = origin + 图上坐标 × scale；"
                          "origin 是图像左上角对应的屏幕坐标，全屏时未必是 (0,0)）")
        return blocks
