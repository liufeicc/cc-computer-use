"""
工具：double_click / scroll / drag —— 三个独立的鼠标动作。

**为什么它们是独立工具而不是 act_sequence 的 op**：这三者都是「模型想主动做一个动作」
时的一等公民（尤其 scrolling 是灰区应用里翻看屏幕外内容的唯一手段），藏在批量工具的
op 列表里模型根本发现不了。它们同时**也**接进了 act_sequence（见 sequences.py），
两处并存：独立工具负责「能被发现」，op 负责「批量时不掉回慢路径」。

⚠️ 三者的共同点：**全是坐标级动作**，因而都带落点证据（拖拽只取起点——见 drag_xy）。
双击更是只能如此：无障碍元素级动作没有「双击」这个语义。
"""

from __future__ import annotations

from typing import Any, Literal

from ..backend.base import BUTTON_NUMBERS
from ..core.coordinator import Coordinator
from ..utils import blocks
from ..utils.errors import ComputerUseError, to_tool_error

# 鼠标按键名 → X11 按钮号。参数收**语义名**而非数字：X11 里 2 是中键、3 是右键，
# 让模型记这种编号纯属制造出错机会；且 Literal 会在**参数校验阶段**就拒绝非法取值并把
# 可选集合列给模型（与 get_ui_tree 的 scope 同口径，M-26/M-41），不静默降级。
# 映射表本身放在 backend/base.py：act_sequence 的 op 也要用同一份，见那里的说明。
_BUTTONS = BUTTON_NUMBERS


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 double_click / scroll / drag 三个工具。"""

    @mcp.tool(
        name="double_click",
        description=(
            "双击（**坐标级**）。双击只能走坐标通道——无障碍元素级动作只有「激活/单击」，"
            "没有双击这个语义。所以要给屏幕绝对坐标（可用 get_screen_text 读出，或先 "
            "get_ui_tree 找到元素后用 element_info 取其矩形中心）。"
            "两次点击的间隔由服务端定时（100ms，远小于系统约 400ms 的双击阈值），"
            "与证据采集开销无关，因此**必定构成双击**；"
            "⚠️ 不要用两次 click 来代替——那两次调用之间隔着分钟级往返，系统只会认成两次单击。"
            "button 支持 left/middle/right。返回落点证据 + 点击前准星小图 + 点后界面变化百分比。"
        ),
    )
    def double_click(
        x: int, y: int, button: Literal["left", "middle", "right"] = "left",
    ) -> Any:
        """
        双击。返回注解必须是 `Any`——带预览图时返回的是 content block 列表而非 str，
        注解为 `str` 会让 SDK 生成 outputSchema 并校验失败（详见 tools/action.py::click）。
        """
        try:
            result = coord.click_xy(int(x), int(y), button=_BUTTONS[button], clicks=2)
        except ComputerUseError as exc:
            return f"❌ 双击失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            raise to_tool_error(exc, "双击失败")
        images = [result.preview] if result.preview else []
        return blocks.image_blocks(
            result.to_text(), images, caption="双击落点 meta", family="cc-cu-dblclick",
            note="红十字中心 = 双击落点 (x,y) 的屏幕绝对坐标；本图**未缩放**，"
                 "图上 1 像素 = 屏幕 1 像素，屏幕坐标 = origin + 图上坐标",
        )

    @mcp.tool(
        name="scroll",
        description=(
            "滚动。`amount` 是**刻度数**（一次滚轮事件 = 一格，没有半格）；direction 为 up/down。"
            "⚠️ **滚多远无法预知**：一格滚多少内容由目标应用决定（GTK 约 3 行、浏览器按比例、"
            "画布应用按像素），所以别指望一次滚到位——正确用法是「滚一下 → 看结果 → 不够再滚」，"
            "把 scroll 与 ui_tree/screenshot 放进**同一次 act_sequence** 最省时间。"
            "x/y 是滚动的**作用点**（滚轮事件由 X 发给指针下那个窗口，所以在哪儿滚决定了滚谁）；"
            "不给则用活动窗口中心。返回「点后界面变化」百分比——"
            "⚠️ 没变化**不等于滚动失败**，也可能是已经滚到内容尽头。"
        ),
    )
    def scroll(
        direction: Literal["up", "down"] = "down",
        amount: int = 5,
        x: int | None = None,
        y: int | None = None,
    ) -> Any:
        try:
            result = coord.scroll_xy(direction=direction, amount=amount, x=x, y=y)
        except ComputerUseError as exc:
            return f"❌ 滚动失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            raise to_tool_error(exc, "滚动失败")
        images = [result.preview] if result.preview else []
        return blocks.image_blocks(
            result.to_text(), images, caption="滚动作用点 meta", family="cc-cu-scroll",
            note="红十字中心 = 滚动作用点 (x,y) 的屏幕绝对坐标；本图**未缩放**",
        )

    @mcp.tool(
        name="drag",
        description=(
            "拖拽：从 (from_x,from_y) 按住、拖到 (to_x,to_y) 松开。起终点都是屏幕绝对坐标。"
            "中间会自动插值出**一串连续移动**——只发一个移动事件时，很多应用判定不出"
            "「按住拖动」，表现是「拖了但没反应」而底层还报成功。"
            "button 支持 left/middle/right。返回**起点**的落点证据与点后界面变化百分比"
            "（拖拽最典型的失败就是抓错起点：抓到别的控件、或没抓住手柄）。"
        ),
    )
    def drag(
        from_x: int, from_y: int, to_x: int, to_y: int,
        button: Literal["left", "middle", "right"] = "left",
        steps: int = 10,
    ) -> Any:
        try:
            result = coord.drag_xy(from_x, from_y, to_x, to_y,
                                   button=_BUTTONS[button], steps=steps)
        except ComputerUseError as exc:
            return f"❌ 拖拽失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            raise to_tool_error(exc, "拖拽失败")
        images = [result.preview] if result.preview else []
        return blocks.image_blocks(
            result.to_text(), images, caption="拖拽起点 meta", family="cc-cu-drag",
            note="红十字中心 = 拖拽**起点** (from_x,from_y) 的屏幕绝对坐标；本图**未缩放**",
        )