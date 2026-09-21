"""工具：get_screen_layout —— 让 LLM 理解几何环境（显示器布局）。"""

from __future__ import annotations

import json
from typing import Any

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 get_screen_layout 工具。"""

    @mcp.tool(
        name="get_screen_layout",
        description=(
            "获取显示器布局：各屏幕名称、分辨率、在虚拟桌面中的偏移(x,y)、是否主屏，"
            "以及虚拟桌面总尺寸。多屏/坐标问题排查时先调它理解几何环境。"
            "返回 JSON 文本。"
        ),
    )
    def get_screen_layout() -> str:
        try:
            layout = coord.screen_layout()
            return json.dumps(layout, ensure_ascii=False, indent=2)
        except ComputerUseError as exc:
            return f"❌ 获取屏幕布局失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "获取屏幕布局失败")
