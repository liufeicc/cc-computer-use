"""
MCP 图像 content block 的共享助手（utils.blocks）。

**为什么单独一层**：把「bytes + meta」变成模型能看的 Image content block，需要三件事
——① 兼容导入 `MCPImage`（mcp 2.x 在 `mcp.server.mcpserver`、1.x 在 `mcp.server.fastmcp`，
都拿不到时退化为落盘 + 文本）；② `_images` 私有键必须先 pop 再序列化（bytes 不能进 JSON）；
③ 每个图像块后面都要跟一段 **meta 文本**（origin/scale 是把图上位置换算回屏幕绝对坐标的
唯一依据，缺了模型只能猜、猜错就点偏）。

这套动作在仓库里已经手写了三份（tools/screenshot.py、tools/action.py 的 act_sequence、
utils/errors.py 的 ToolError 同族导入）。再加第四份就是给「三份已经漂移」埋第四份，
故抽到这里；本模块**只做拼接**，不碰抓屏/编码（那是 backend 的职责）。
"""

from __future__ import annotations

from typing import Any

from . import temps

# MCPImage 兼容导入（与 server.py / tools/screenshot.py 同一套判据）
try:  # mcp 2.x
    from mcp.server.mcpserver import Image as MCPImage  # type: ignore
except Exception:  # noqa: BLE001
    try:  # mcp 1.x
        from mcp.server.fastmcp import Image as MCPImage  # type: ignore
    except Exception:  # noqa: BLE001
        MCPImage = None  # 退化：改为落盘 + 给出文件路径


def dump_temp_image(data: bytes, meta: dict[str, Any], family: str) -> str:
    """
    把图像字节落到临时文件，返回路径（仅在拿不到 MCP Image 类型的退化路径上用）。

    自动落盘的每一张都是潜在泄漏（写一次留一个、没人回收），故沿用 `utils/temps` 的
    同一套约定：文件名带 **pid 归属** + 写入后按 LRU 只留最近 5 张（且只回收本进程
    归属或主人已死的文件——/tmp 是全局的，按前缀无差别裁剪等于误杀别的会话）。
    """
    suffix = ".png" if meta.get("format") == "png" else ".jpg"
    path = temps.new_temp_image(family, suffix)
    with open(path, "wb") as f:
        f.write(data)
    temps.prune_temp_images(family, protect=path)
    return path


def image_blocks(
    text: str, images: list[tuple[bytes, dict[str, Any]]], caption: str = "截图 meta",
    family: str = "cc-cu-img", note: str | None = None,
) -> Any:
    """
    把「文本日志 + 一组图像」拼成 MCP 的 content block 列表。

    返回约定（与 tools/screenshot.py、act_sequence 既有顺序一致，历史实现已按此对齐）：
      顺序：**文本在前**，其后每张图跟两个块——`MCPImage` 与一段 meta 文本。
      - 无图 → 直接返回 `text`（保持既有「click 无图时返回纯字符串」的断言不破）；
      - `MCPImage is None` → 退化为落盘 + 文本，至少给出文件路径（比丢掉强）。

    `note` 是 meta 之后那句**换算说明**：缺省即截图的通用说法（与历史文本逐字一致），
    点击预览图传自己的说法（十字中心=落点、未缩放）。这段文字不是装饰——它是模型把
    「图上的位置」换算成「屏幕绝对坐标」的唯一依据，缺了就只能猜、猜错就点偏。
    """
    if not images:
        return text
    if MCPImage is None:
        paths = [f"{dump_temp_image(d, m, family)} (meta={m})" for d, m in images]
        return text + f"\n图像已落盘({family}): " + "; ".join(paths)
    suffix = note or ("屏幕绝对坐标 = origin + 图上坐标 × scale；"
                      "origin 是图像左上角对应的屏幕坐标，全屏时未必是 (0,0)")
    blocks: list[Any] = [text]
    for data, meta in images:
        blocks.append(MCPImage(data=data, format=meta.get("format", "jpeg")))
        blocks.append(f"{caption}: {meta}（{suffix}）")
    return blocks