"""
截图与灰区感知（core.coordinator.screens）—— 三级降级里的第三级。

`get_screen_text`（OCR）是灰区（无无障碍树）应用的**元素树替代品**：它把屏幕变成
「[ref] 文字 @ (x,y)」的文本列表，模型读文字即可定位，坐标是**算出来的不是猜的**。

这条路最贵的**不在工具侧**（截图只要 0.2s），在**模型侧**：看图 → 估像素位置 →
换算回屏幕坐标，实测一次 DBeaver 任务 8.3 分钟里工具只占 21 秒。所以本模块的目标
始终是「让模型少看图」：OCR 文本优先，截图兜底。

本组方法都会抓屏/改屏状态，故挂 `@_exclusive_screen`（`screenshot_*` 抓屏时若与他人
并发，画面可能被对方的注入动作改变，读到的就不是一个一致的状态）。
"""

from __future__ import annotations

import base64
import os
from typing import Any

from ...utils import temps
from ...utils.errors import ComputerUseError
from .hooks import _exclusive_screen, _needs_display


class ScreenMixin:
    """屏幕布局、截图、OCR 文本层。"""

    @_needs_display
    def screen_layout(self) -> dict[str, Any]:
        return self.backend.screen_layout()

    @_needs_display
    @_exclusive_screen
    def screenshot_b64(
        self, region: tuple[int, int, int, int] | None = None, max_side: int = 1280,
    ) -> tuple[str, dict[str, Any]]:
        """
        截图并返回 (base64, meta)。灰区兜底。

        为省 token，长边超过 max_side 时按比例降采样。
        """
        png, meta = self.screenshot_image(region, max_side)
        return base64.b64encode(png).decode("ascii"), meta

    @_needs_display
    @_exclusive_screen
    def screenshot_image(
        self, region: tuple[int, int, int, int] | None = None, max_side: int = 1280,
        fmt: str = "jpeg",
    ) -> tuple[bytes, dict[str, Any]]:
        """
        截图并返回 (图像 bytes, meta)。灰区兜底。

        缩放与编码**都在 backend 内一次性完成**（抓屏 → 缩放 → 编一次）。
        历史坑：这里曾是「backend 全尺寸编码 → 这里 Image.open 解码 → resize → 再编码」，
        同一张图压两遍、全尺寸那一次纯属浪费（实测 266ms）。现已下沉到 backend。
        """
        return self.backend.screenshot(region, max_side=max_side, fmt=fmt)

    @_needs_display
    @_exclusive_screen
    def screenshot_to_file(
        self, region: tuple[int, int, int, int] | None = None, max_side: int = 1280,
        path: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        截图并落盘，返回 (文件路径, meta)。inline=False 时使用，省 token/turn 时间。

        path 为空则自动生成临时文件（后缀跟随实际编码格式，默认 .jpg），**且只保留最近
        5 张**（LRU 自动回收，见 `utils/temps`）；要长期留存就显式传 `save_path`。

        ⚠️ **本方法永不覆盖已存在的文件**（C-1）。path 由模型直接给出，写的是**宿主真实
        文件系统**——本项目其余能力（点击/输入/启应用）都只动 Xephyr 沙箱内的屏，唯独这里
        落到宿主磁盘，代价不对称：模型误猜一个路径（如把 ~/.bashrc 当成存放目录）即整段
        覆盖用户文件，且写入的是图像二进制、不可逆。故此处宁可失败，也不猜。
        需要覆盖时由调用方自己先删除目标文件——不给 overwrite 开关，避免「一句 true 就
        把破坏性操作变得顺手」。
        """
        data, meta = self.screenshot_image(region, max_side)
        auto = not path
        if auto:
            # 自动路径：文件名带本进程 pid（归属标记），写完后按 LRU 裁剪。
            # 为什么必须回收：模型只要用过一次 inline=false，这张图就永久留在 /tmp，
            # 而它自己不会清理。实测手工清掉过 118 个，全历次会话累积。
            # 为什么不靠 atexit：会话被强杀时 atexit 不跑，那正是残留的主要来源；
            # 改成「每次写入时裁剪」才是有界的。判据与命名见 utils/temps 的模块 docstring。
            suffix = ".png" if meta.get("format") == "png" else ".jpg"
            path = temps.new_temp_image("cc-cu-shot", suffix)
        elif os.path.exists(path):
            raise ComputerUseError(
                f"目标文件已存在，本工具不覆盖：{path}\n"
                f"   请改用一个不存在的路径，或把 save_path 留空（将自动存到临时目录）。"
            )
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as exc:
            # 父目录不存在 / 无权限等：转成友好文案。否则 OSError 不是 ComputerUseError，
            # 会穿透工具层的 except 跑到 MCP 层，模型只拿到一句无信息量的
            # "Error executing tool screenshot"，既不知道是路径错还是依赖缺失。
            raise ComputerUseError(
                f"写入失败：{path}（{exc.strerror or exc}）——请确认目录存在且有写权限"
            ) from exc
        if auto:
            # 写在**后面**才回收：万一写入失败，就不该顺手删掉上一张还能看的图。
            # protect=path 显式钉住「刚交给模型的这张不许删」，不依赖 mtime 排序打平时的运气。
            temps.prune_temp_images("cc-cu-shot", protect=path)
        meta["path"] = path
        return path, meta

    # ================= 灰区感知：OCR 文本层 =================
    @_needs_display
    @_exclusive_screen
    def get_screen_text(
        self, region: tuple[int, int, int, int] | None = None, scope: str = "window",
        min_conf: float = 40.0, max_items: int = 150,
    ) -> str:
        """
        OCR 读取屏幕文本，返回「[ref] 文字 @ (x,y) 」的紧凑列表——灰区应用的**元素树替代品**。

        为什么需要它：SWT/Java、自绘控件、游戏、远程桌面没有无障碍树（或树一读就崩），
        三级降级只能落到「截图 + 坐标点击」。而那条路最贵的**不在工具侧**（截图 0.2s），
        在**模型侧**：看图 → 估像素位置 → 换算回屏幕坐标，实测每轮 30~70s。本工具让模型
        直接读到带坐标的文本，既能点(ref 或 x,y)又不用看图。

        scope 决定默认识别范围（都是为了避免全屏 OCR 的 8~10s 开销）：
          - 'window'（默认）：当前**活动窗口**那块矩形。对话框/菜单场景最快（2~3s）。
          - 'screen'：整屏。文字密集时 8~10s，仅在确实要看全局时用。
        传了 region 则一律以 region 为准。

        ⚠️ 坐标是**快照**：界面变化后（切窗、弹新对话框）请重新调用，不要复用旧 ref。
        """
        reg = tuple(region) if region and len(region) == 4 else None
        scope_used = "region"
        if reg is None:
            if scope == "screen":
                scope_used = "screen"
            else:
                r = self.backend.active_window_rect()
                if r is not None and not r.is_empty():
                    reg, scope_used = r.to_tuple(), "window"
                else:
                    scope_used = "screen"   # 取不到活动窗口（如空桌面）→ 退回全屏

        blocks = self.backend.read_text(reg, min_conf=min_conf)
        if not blocks:
            return ("(未识别到文本。可能：该区域确实没有文字、tesseract 未安装，"
                    "或语言包缺中文——见 CLAUDE.md 的 OCR 依赖说明)")

        truncated = len(blocks) > max_items
        shown = blocks[:max_items]
        out: list[str] = []
        for b in shown:
            ref = self.refs.register(b, role="text-block", name=b.text, app="ocr")
            r = b.rect
            out.append(f"[{ref}] {b.text}  @ ({r.x},{r.y}) {r.w}x{r.h}")
        header = (f"识别范围={scope_used} region={list(reg) if reg else '全屏'}，"
                  f"共 {len(blocks)} 块文本" + (f"，已截断显示前 {max_items} 块" if truncated else ""))
        body = "\n".join(out)
        return (f"{header}\n{body}\n\n"
                f"（坐标已是**屏幕绝对坐标**，可直接 click(x=..,y=..)；"
                f"也可 click(ref=N) 由服务端代点，避免抄错。文字是快照，界面变了请重新识别）")