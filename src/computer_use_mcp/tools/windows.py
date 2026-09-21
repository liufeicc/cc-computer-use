"""工具：list_windows / wait_window —— 窗口甄别与等待（省往返）。"""

from __future__ import annotations

import json
from typing import Any

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 list_windows / wait_window 工具。"""

    @mcp.tool(
        name="list_windows",
        description=(
            "一次列出当前可见窗口：[{id,title,pid,x,y,w,h,area}]，按面积降序"
            "（面积最大者通常是主窗口；未映射的孤儿窗/隐藏辅助窗已被过滤）。"
            "用于甄别同名窗、定位目标窗口 id，替代多次 shell 查询。返回 JSON 文本。"
        ),
    )
    def list_windows(limit: int = 100) -> str:
        try:
            return json.dumps(coord.list_windows(limit=limit), ensure_ascii=False)
        except ComputerUseError as exc:
            return f"❌ 列窗口失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "列窗口失败")

    @mcp.tool(
        name="wait_window",
        description=(
            "等待窗口标题满足条件（server 内部轮询，**单次调用完成**）。"
            "title_contains 忽略大小写；window_id 指定则只盯该窗口，否则盯活动窗口；"
            "超时返回超时提示。用于等页面加载/应用启动，替代外部反复轮询。"
        ),
    )
    def wait_window(
        title_contains: str | None = None,
        window_id: str | None = None,
        timeout: float = 10.0,
    ) -> str:
        try:
            info = coord.wait_window(title_contains=title_contains, window_id=window_id,
                                     timeout=timeout)
            return json.dumps(info, ensure_ascii=False) if info else "⏰ 超时未等到匹配窗口"
        except ComputerUseError as exc:
            return f"❌ 等待窗口失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "等待窗口失败")
