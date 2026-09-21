"""工具：find_element / element_info —— 让 LLM 选元素而非算坐标。"""

from __future__ import annotations

from typing import Any

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 find_element 与 element_info 工具。"""

    @mcp.tool(
        name="find_element",
        description=(
            "按文本/角色/应用搜索可交互元素，返回候选列表（每个含 [ref]）。"
            "用 ref 配合 click/type_text 精确操作，避免坐标估算。"
            "text 为名字子串（如『保存』『是』），role 为角色子串（如 'push button'；"
            "注意 GTK 输入框的 role 是 'text' 而非 'entry'），app 限定应用名关键词。"
            "至少提供 text 或 role 之一。**强烈建议带 app 或 text 限定**："
            "全桌面裸搜很慢，且遍历大型应用（浏览器/Electron）时有节点数熔断，结果可能不全。"
        ),
    )
    def find_element(
        text: str | None = None,
        role: str | None = None,
        app: str | None = None,
        interactive_only: bool = True,
        limit: int = 40,
    ) -> str:
        try:
            elements, notice = coord.find_elements(
                text=text, role=role, app=app,
                interactive_only=interactive_only, limit=limit,
            )
            if elements:
                body = f"找到 {len(elements)} 个候选元素：\n" + "\n".join(
                    el.to_text() for el in elements)
            elif notice:
                # 关键区别：被截断导致的「没找到」**不等于桌面上没有**。若不点明，
                # 模型会据此断定「不存在」，转而走截图这条昂贵得多的路。
                body = ("(在**已覆盖的范围内**未找到匹配元素——本次搜索被截断，"
                        "不代表桌面上没有；请按下方提示收窄范围或调整条件)")
            else:
                body = "(未找到匹配元素。可放宽条件，或先 get_ui_tree 看整体结构)"
            return f"{notice}\n{body}" if notice else body
        except ComputerUseError as exc:
            return f"❌ 搜索失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "搜索失败")

    @mcp.tool(
        name="element_info",
        description=(
            "按 ref 查看单个元素的详情：角色、名字、值、状态、屏幕矩形、可用 actions。"
            "在 click/type 前用它确认元素是否正确、有哪些可执行动作。"
        ),
    )
    def element_info(ref: int) -> str:
        try:
            detail = coord.get_element_info(ref)
            return detail.to_text()
        except ComputerUseError as exc:
            return f"❌ 获取详情失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "获取详情失败")
