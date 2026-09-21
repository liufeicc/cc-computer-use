"""工具：screenshot —— 灰区兜底（自绘控件/游戏/远程桌面无元素树时才用）。"""

from __future__ import annotations

from typing import Any

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error
from ..utils.logging import get_logger

log = get_logger(__name__)


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 screenshot 工具。返回图像内容（FastMCP Image），失败退化为文本说明。"""

    # 优先使用 FastMCP/MCPServer 的 Image 类型（生成标准 ImageContent），兼容 mcp 1.x/2.x
    MCPImage = None
    try:
        from mcp.server.mcpserver import Image as MCPImage  # type: ignore  # mcp 2.x
    except Exception:  # noqa: BLE001
        try:
            from mcp.server.fastmcp import Image as MCPImage  # type: ignore  # mcp 1.x
        except Exception:  # noqa: BLE001
            MCPImage = None  # 退化：返回 base64 文本

    @mcp.tool(
        name="screenshot",
        description=(
            "截图（灰区兜底，**默认不要主动用**）。仅当目标无元素树（自绘控件/游戏/视频/远程桌面）"
            "或 get_ui_tree/find_element 无法定位时才使用——它费 token。"
            "可传 region=[x,y,w,h] 裁剪感兴趣区域；max_side 控制降采样长边（默认1280，省 token）。"
            "inline=false 时不返回图像、只落盘并返回文件路径（更省 token/更快），适合只需存档或"
            "后续自行读取的场景。此时可用 save_path 指定落盘位置——**已存在的文件不会被覆盖**"
            "（会直接报错），需要改路径或留空让本工具自动存到临时目录（**自动路径只保留"
            "最近 5 张、旧的会被回收**，要长期留存必须显式传 save_path）。"
            "优先用 get_ui_tree + click(ref) 完成任务。"
        ),
    )
    def screenshot(
        region: list[int] | None = None,
        max_side: int = 1280,
        inline: bool = True,
        save_path: str | None = None,
    ) -> Any:
        try:
            if region is not None and len(region) != 4:
                # M-41：长度不对原先被静默丢成**全屏** —— 模型以为在看指定区域，
                # 实际看的是整屏（更慢、更费 token，而且判断依据根本不是它要的那块）。
                raise ComputerUseError(
                    f"region 需为 [x, y, w, h] 四个整数，实际给了 {len(region)} 个：{region}"
                )
            reg = tuple(region) if region else None
            if not inline:
                path, meta = coord.screenshot_to_file(reg, max_side=max_side, path=save_path)
                return f"截图已落盘(未内联): path={path} meta={meta}"
            if MCPImage is not None:
                data, meta = coord.screenshot_image(reg, max_side=max_side)
                log.info("screenshot 生成: %s", meta)
                # 格式跟随 backend 实际输出（默认 jpeg：实测编码比 PNG 快 19 倍、小 9 倍）
                img = MCPImage(data=data, format=meta.get("format", "jpeg"))
                # ⚠️ meta 必须跟着图像一起回给模型：**scale**（图像像素→屏幕像素的倍数）
                # 是它把「图上的位置」换算成「屏幕绝对坐标」的唯一依据，缺了就只能猜，
                # 猜错就点偏。历史实现只把 meta 写进日志、图像单独返回，模型拿不到它。
                return [img, f"截图 meta: {meta}"
                            "（屏幕绝对坐标 = origin + 图上坐标 × scale；"
                            "origin 是图像左上角对应的屏幕坐标，全屏时未必是 (0,0)）"]
            # 退化路径：返回 base64 + meta 文本
            b64, meta = coord.screenshot_b64(reg, max_side=max_side)
            return f"截图(base64) meta={meta}\n{b64}"
        except ComputerUseError as exc:
            return f"❌ 截图失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（依赖缺失/参数类型错/内部 bug…）也转成友好文本。
            # 少了这条会穿透到 MCP 层，模型只看到零信息量的
            # "Error executing tool X"（见 utils/errors.to_friendly_text）
            # M-47（折中）：这里由「返回文本」改为**抛 ToolError** —— SDK 会把文本原样
            # 放进 content 并置 is_error=True，客户端/日志这才分得清失败与成功调用
            # （原先一律 isError=false）。ComputerUseError 那条仍是普通文本返回，
            # 那是本项目既定的错误契约（CLAUDE.md：tools 只做参数解析 + 错误转友好文本）。
            raise to_tool_error(exc, "截图失败")
