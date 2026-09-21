"""工具：get_screen_text —— 灰区应用的「元素树替代品」（OCR 出带坐标的文本）。"""

from __future__ import annotations

from typing import Any, Literal

from ..core.coordinator import Coordinator
from ..utils.errors import ComputerUseError, to_friendly_text, to_tool_error


def register(mcp: Any, coord: Coordinator) -> None:
    """注册 get_screen_text 工具。"""

    @mcp.tool(
        name="get_screen_text",
        description=(
            "读取屏幕上的文字及其坐标，返回紧凑文本列表（每行 `[ref] 文字 @ (x,y) 宽x高`）。"
            "**灰区应用（无元素树：SWT/Java、自绘控件、游戏、远程桌面）首选感知方式**——"
            "它比 screenshot 省得多：你读文字就能定位，不需要看图、也不需要从图像里估算像素"
            "位置再换算回屏幕坐标（那正是灰区操作慢和点偏的主因）。拿到结果后可直接 "
            "click(x=..,y=..)，或 click(ref=N) 由服务端代点（避免抄错坐标）。"
            "scope='window'（默认）只识别当前活动窗口那块区域，2~3 秒；scope='screen' 整屏，"
            "文字密集时要 8~10 秒，非必要别用。也可传 region=[x,y,w,h] 自己指定区域。"
            "注意：坐标是**快照**，界面变化（切窗/弹新对话框）后请重新调用，不要复用旧 ref。"
            "⚠️ 已知限制：**文字紧邻深色图标时那一行会识别失败**（实测「确认删除该文件吗？」"
            "挨着问号图标时被认成乱码）。若发现某行文字明显不对，把 region 收窄到只含该文字"
            "的小块再调一次即可（可先用本次返回的该块 bbox 定位）。"
        ),
    )
    def get_screen_text(
        region: list[int] | None = None,
        # M-41：收成 Literal，非法值由 pydantic 在**参数校验阶段**直接打回（mcp 2.x 会把
        # 这类错误原文回给模型）。原先写 `str`，拼错（如 "scren"）会被静默降级成 window，
        # 模型拿到的是「另一块区域」的结果而完全无从察觉。
        scope: Literal["window", "screen"] = "window",
        min_conf: float = 40.0,
        max_items: int = 150,
    ) -> str:
        try:
            if region is not None and len(region) != 4:
                # M-41：`region` 长度不对（含传 3 个数的常见笔误）原先被**静默丢成全屏** ——
                # 那不只是慢（全屏 OCR 8~10s，对话框只要 0.3s），更糟的是模型以为自己在看
                # 指定区域，实际看的是整屏。响亮报错比默默换一块区域安全得多。
                raise ComputerUseError(
                    f"region 需为 [x, y, w, h] 四个整数，实际给了 {len(region)} 个：{region}"
                )
            reg = tuple(region) if region else None
            return coord.get_screen_text(region=reg, scope=scope,
                                         min_conf=min_conf, max_items=max_items)
        except ComputerUseError as exc:
            return f"❌ 读取屏幕文本失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            # 非预期异常（tesseract 缺失/超时等）与其余工具同口径：类型 + 原因回给模型，
            # 堆栈只进服务端日志；并抛 ToolError 让协议层带上 is_error（M-47）。
            # ⚠️ 这里原先既没接 to_friendly_text（I-5 漏网的一个点），给用户的也是裸
            # `{exc}`；补上后与别处的文案一致，也顺带保住了 tesseract 那条安装提示。
            raise to_tool_error(exc, "OCR 失败（若提示未安装：sudo apt install "
                                     "tesseract-ocr tesseract-ocr-chi-sim）")