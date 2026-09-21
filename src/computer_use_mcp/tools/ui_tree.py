"""工具：get_ui_tree —— 核心感知，替代截图。"""

from __future__ import annotations

from typing import Any, Literal

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 get_ui_tree 工具到 FastMCP 实例。"""

    @mcp.tool(
        name="get_ui_tree",
        description=(
            "读取桌面无障碍元素树（结构化文本），这是感知屏幕的首选方式，**不要默认用 screenshot**。"
            "返回紧凑文本树，每个可操作元素带 [ref] 编号，后续用 click(ref)/type_text(ref) 精确操作，"
            "无需估算坐标。scope=active_window 最省 token（默认）；找特定应用用 scope=app+app=名字关键词；"
            "整桌面用 scope=desktop（大，慎用）。interactive_only=true 只列可操作元素，进一步省 token。"
        ),
    )
    def get_ui_tree(
        # Literal 而非裸 str（M-26/M-41）：非法 scope 过去会被静默降级成「活动窗口树」
        # 或「整桌面树」——模型拿到的是**另一种**结果而不是报错，会据此做出错误判断。
        # 用 Literal 后由 pydantic 在**参数校验阶段**直接拒绝，且错误原文会把取值集合
        # 列全（实测 mcp 2.2.0 + pydantic 2.13：「Input should be 'active_window',
        # 'app' or 'desktop'」），模型可自查；schema 只是多了 enum，纯增量。
        scope: Literal["active_window", "app", "desktop"] = "active_window",
        app: str | None = None,
        interactive_only: bool = False,
        max_nodes: int = 400,
    ) -> str:
        try:
            return coord.get_ui_tree(
                scope=scope, app=app, interactive_only=interactive_only, max_nodes=max_nodes,
            )
        except ComputerUseError as exc:
            return f"❌ 读取失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "读取失败")
