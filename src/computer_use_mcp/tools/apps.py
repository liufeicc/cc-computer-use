"""工具：launch_app —— 在目标 display 启动应用（隔离沙箱的应用入口正路）。"""

from __future__ import annotations

import json
from typing import Any

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 launch_app 工具。"""

    @mcp.tool(
        name="launch_app",
        description=(
            "在目标 display 启动应用：isolated 模式即放进 Xephyr 沙箱（把应用放上虚拟屏的"
            "**唯一正路**，宿主桌面不受影响）；real 模式即普通启动。command 用 shlex 解析"
            "（如 'zenity --entry --title=T'、'gedit'），返回 {pid,command,display,argv} 的"
            "JSON（argv 是**实际执行**的命令行）。"
            "启动后配合 wait_window 等窗口出现，再 get_ui_tree/click 操作。"
            "已知的单实例应用（gnome-terminal、gedit 等）会自动补上它们各自的『去单实例』"
            "参数（如 --disable-factory / --standalone），否则它们的窗口会开到宿主桌面上"
            "那个已存在的实例旁——这类应用只改 DISPLAY 是搬不动的，返回的 argv 里能看到"
            "补了什么。"
        ),
    )
    def launch_app(command: str, settle: float = 2.0) -> str:
        try:
            return json.dumps(coord.launch_app(command, settle=settle), ensure_ascii=False)
        except ComputerUseError as exc:
            return f"❌ 启动失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "启动失败")
