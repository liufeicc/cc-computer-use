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
from .hooks import _exclusive_screen, _needs_display, log


class ScreenMixin:
    """屏幕布局、截图、OCR 文本层。"""

    # ================= 抓图通道：按窗口优先，裁屏兜底 =================

    def _window_capture_supported(self) -> bool:
        """本平台有没有「按窗口渲染」这条通道（基类默认实现返回 None = 没有）。

        ⚠️ 判据取**类方法是不是被覆写过**，而不是 `hasattr` / `try: ... except`：
        本仓库明文否定带默认值的哨兵写法（I-10）——它会让"新平台忘了实现"
        静默退化成另一种行为。
        """
        from ...backend.base import Backend
        return (type(self.backend).capture_window is not Backend.capture_window)

    def _grab(self, region: tuple[int, int, int, int] | None, scope: str,
              window_id: str | None, app: str | None,
              max_side: int, fmt: str) -> tuple[bytes, dict[str, Any]]:
        """
        抓一张图，返回 `(bytes, meta)`。**截图与 OCR 共用这一条**（口径一致）。

        三层优先级：
          1. 显式 `region` → 就按那块区域裁屏（用户明确要的，照办）；
          2. 平台支持按窗口渲染 → **让那扇窗自己画一张**（不受遮挡影响、不含别人的窗口）；
          3. 否则退化为「按窗口矩形裁屏」，并在 meta 里**如实标注** `capture="region_crop"`
             与"可能被遮挡"——模型必须能分辨自己在看哪一种，否则会把遮挡者的内容
             当成目标窗口的内容。
        """
        if region is not None:
            data, meta = self.backend.screenshot(region, max_side=max_side, fmt=fmt)
            meta.setdefault("capture", "region_crop")
            return data, meta

        mode, wid, note = self._capture_target(scope, window_id, app)
        if mode == "screen":
            data, meta = self.backend.screenshot(None, max_side=max_side, fmt=fmt)
            meta.setdefault("capture", "screen")
            if note:
                meta["target_note"] = note
            return data, meta

        if self._window_capture_supported():
            wid = wid or self.backend.active_window_id()
            if wid:
                data, meta = self.backend.screenshot_window(
                    wid, max_side=max_side, fmt=fmt)
                self._remember_target_window(wid)
                if note:
                    meta["target_note"] = note
                return data, meta

        # 退化：按窗口矩形裁屏。**必须标明**，因为这时看到的是"屏幕上那块"，
        # 被遮挡时内容属于盖住它的窗口。
        rect = None
        if wid:
            try:
                rect = self.backend.window_rect_of(wid)
            except Exception as exc:  # noqa: BLE001
                log.debug("window_rect_of(%s) 失败: %s", wid, exc)
        rect = rect or self.backend.active_window_rect()
        if rect is None or rect.is_empty():
            data, meta = self.backend.screenshot(None, max_side=max_side, fmt=fmt)
            meta["capture"] = "screen"
            meta["target_note"] = (note + "｜" if note else "") + "取不到窗口矩形，已退回整屏"
            return data, meta
        data, meta = self.backend.screenshot(rect.to_tuple(), max_side=max_side, fmt=fmt)
        meta["capture"] = "region_crop"
        meta["window_id"] = wid
        meta["target_note"] = ((note + "｜") if note else "") + (
            "本平台不支持按窗口渲染，这张是**屏幕上那块区域**的裁剪 —— "
            "目标窗口若被别的窗口挡住，图里就是**遮挡者**的内容。")
        return data, meta

    @_needs_display
    def screen_layout(self) -> dict[str, Any]:
        return self.backend.screen_layout()

    @_needs_display
    @_exclusive_screen
    def screenshot_b64(
        self, region: tuple[int, int, int, int] | None = None, max_side: int = 1280,
        scope: str = "auto", window_id: str | None = None, app: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """
        截图并返回 (base64, meta)。灰区兜底。

        为省 token，长边超过 max_side 时按比例降采样。
        """
        png, meta = self.screenshot_image(region, max_side, scope=scope,
                                          window_id=window_id, app=app)
        return base64.b64encode(png).decode("ascii"), meta

    @_needs_display
    @_exclusive_screen
    def screenshot_image(
        self, region: tuple[int, int, int, int] | None = None, max_side: int = 1280,
        fmt: str = "jpeg", scope: str = "auto",
        window_id: str | None = None, app: str | None = None,
    ) -> tuple[bytes, dict[str, Any]]:
        """
        截图并返回 (图像 bytes, meta)。灰区兜底。

        `scope`（不给范围时截哪儿）：
          · `"auto"`（默认）—— 跟随**本平台的工作台形态**（`backend.DEFAULT_CAPTURE_SCOPE`）：
            Linux 是整块沙箱屏（那儿只有我们的应用），Windows 是**目标窗口**
            （用户桌面上还有别的东西，抓全屏会把它们一起送进模型上下文）。
          · `"window"` / `"screen"` —— 显式指定，覆盖上面的默认。
        目标窗口的来源见 `_capture_target`：显式 `window_id` / `app` >
        本会话最近操作过的窗口 > 活动窗口（**且会注明**）。

        缩放与编码**都在 backend 内一次性完成**（抓屏 → 缩放 → 编一次）。
        历史坑：这里曾是「backend 全尺寸编码 → 这里 Image.open 解码 → resize → 再编码」，
        同一张图压两遍、全尺寸那一次纯属浪费（实测 266ms）。现已下沉到 backend。
        """
        return self._grab(region, scope, window_id, app, max_side, fmt)

    @_needs_display
    @_exclusive_screen
    def screenshot_to_file(
        self, region: tuple[int, int, int, int] | None = None, max_side: int = 1280,
        path: str | None = None, scope: str = "auto",
        window_id: str | None = None, app: str | None = None,
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
        data, meta = self.screenshot_image(region, max_side, scope=scope,
                                           window_id=window_id, app=app)
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
        window_id: str | None = None, app: str | None = None,
    ) -> str:
        """
        OCR 读取屏幕文本，返回「[ref] 文字 @ (x,y) 」的紧凑列表——灰区应用的**元素树替代品**。

        为什么需要它：SWT/Java、自绘控件、游戏、远程桌面没有无障碍树（或树一读就崩），
        三级降级只能落到「截图 + 坐标点击」。而那条路最贵的**不在工具侧**（截图 0.2s），
        在**模型侧**：看图 → 估像素位置 → 换算回屏幕坐标，实测每轮 30~70s。本工具让模型
        直接读到带坐标的文本，既能点(ref 或 x,y)又不用看图。

        scope 决定默认识别范围（都是为了避免全屏 OCR 的 8~10s 开销）：
          - 'window'（默认）：**目标窗口**。对话框/菜单场景最快（2~3s）。
          - 'screen'：整屏。文字密集时 8~10s，仅在确实要看全局时用。
        传了 region 则一律以 region 为准。

        ⚠️ 「目标窗口」不等于「活动窗口」：它按 `window_id` > `app` >
        本会话最近操作过的窗口 > 活动窗口 的顺序推导（见 `_capture_target`）。
        最后那一档**会在返回里注明**——它可能正是**用户在用的窗口**。

        ⚠️ 平台支持「按窗口渲染」时（Windows），OCR 喂的是**那扇窗自己画的图**：
        **目标被别的窗口盖住也照样读得对**（老实现是"截屏幕上那块矩形"，
        被遮挡时会读到遮挡者上的字，而且完全不报错）。

        ⚠️ 坐标是**快照**：界面变化后（切窗、弹新对话框）请重新调用，不要复用旧 ref。

        ⚠️ **本会话可能没有坐标级通道**（Windows 隔离模式）：那里的 ref 与坐标**只能读**，
        按它们点击会被闸门拒绝。返回里会**当场说明**这一点（见下方的 `readonly` 分支），
        绝不留给模型自己撞 —— 撞了之后它只会以为是自己点偏了，然后换个坐标重试。
        """
        reg = tuple(region) if region and len(region) == 4 else None
        # ★ 本会话的坐标/ref 能不能点。判据在 `_coordinate_input_unavailable`，
        #   它读的是**能力申报**而不是"是不是隔离模式"——Linux 的 Xephyr 有坐标通道，
        #   那边一切照旧（详见那个方法的 docstring）。
        readonly = self._coordinate_input_unavailable()
        scope_used = "region"
        notes: list[str] = []
        blocks: list[Any]
        if reg is not None:
            blocks = self.backend.read_text(reg, min_conf=min_conf)
        else:
            mode, wid, note = self._capture_target(scope, window_id, app)
            if note:
                notes.append(note)
            img_got = None
            if mode == "window" and self._window_capture_supported():
                wid = wid or self.backend.active_window_id()
                if wid:
                    try:
                        img_got = self.backend.capture_window(wid)
                    except Exception as exc:  # noqa: BLE001
                        # 抓不到就退回裁屏 —— OCR 值得把这条退路留着（它不同于截图：
                        # 这里要的是**文字**，而裁屏拿到遮挡者的字至少还能看出不对）。
                        notes.append(f"按窗口渲染失败，已改为裁屏幕上那块区域：{exc}")
                    else:
                        self._remember_target_window(wid)
            if img_got is not None:
                img, origin = img_got
                scope_used = f"window:{wid}"
                blocks = self.backend.read_text_from_image(img, origin, min_conf=min_conf)
            else:
                if mode == "window":
                    rect = None
                    if wid:
                        try:
                            rect = self.backend.window_rect_of(wid)
                        except Exception as exc:  # noqa: BLE001
                            log.debug("window_rect_of(%s) 失败: %s", wid, exc)
                    rect = rect or self.backend.active_window_rect()
                    if rect is not None and not rect.is_empty():
                        reg, scope_used = rect.to_tuple(), "window"
                    else:
                        scope_used = "screen"   # 取不到窗口（如空桌面）→ 如实退回全屏
                else:
                    scope_used = "screen"
                blocks = self.backend.read_text(reg, min_conf=min_conf)
        # ⚠️ `min_conf` 在有些平台**不生效**（Windows 自带 OCR 没有置信度）。
        #    不能让它静默变成 no-op —— 模型会以为自己筛掉了低质量结果。
        #    支持时这段是空串，不占输出。
        conf_note = self.backend.min_conf_notice(min_conf)
        if not blocks:
            return ("(未识别到文本。可能：该区域确实没有文字、OCR 引擎缺语言包，"
                    "或语言包缺中文——见 CLAUDE.md 的 OCR 依赖说明)"
                    + (f"\n{conf_note}" if conf_note else "")
                    + (("\n" + "｜".join(notes)) if notes else ""))

        # ★ 坐标点不了的会话，把代价**说在 ref 前面**（而不是只在末尾纠正那句承诺）：
        #   模型读到一张带 ref 的清单，第一反应就是"点它"。这句话必须在它形成那个
        #   计划**之前**出现，否则它就是"先照着做、撞了再回来读"——而那正是要避免的往返。
        #   ⚠️ 单独成段而不是混进 `notes` 那个 `｜` 长串里：那一串已经够长了，
        #   而这条是**本会话最重要的一句**，混进去等于让它淹没在噪音里。
        warn = ""
        if readonly:
            warn = (
                "⚠️ 本会话**没有坐标级通道**（隔离模式：程序起在一张你看不见的桌面上，"
                "本进程发出的鼠标事件只会落到**用户**的桌面上，所以这一下会被闸门拒绝）。"
                "下面的 ref 与坐标**只能用来读，点不了**（click / scroll / drag 都不行）。\n"
                "   替代：改用元素级 —— get_ui_tree / find_element 拿 ref 再 click(ref=...)，"
                "零坐标、也不碰用户键鼠。\n"
                "   若这个应用**没有无障碍树**（SWT/Java、自绘控件、游戏、远程桌面）："
                "隔离模式下**做不了**，请让用户设 CC_CU_DISPLAY_MODE=real 后重启 Claude Code。"
            )

        truncated = len(blocks) > max_items
        shown = blocks[:max_items]
        out: list[str] = []
        for b in shown:
            ref = self.refs.register(b, role="text-block", name=b.text, app="ocr")
            r = b.rect
            out.append(f"[{ref}] {b.text}  @ ({r.x},{r.y}) {r.w}x{r.h}")
        header = (f"识别范围={scope_used} region={list(reg) if reg else '全屏'}，"
                  f"共 {len(blocks)} 块文本" + (f"，已截断显示前 {max_items} 块" if truncated else "")
                  + (f"\n{conf_note}" if conf_note else "")
                  + (("\n" + "｜".join(notes)) if notes else ""))
        # ⚠️ `warn` 前后各留一个空行：它要挡住的是"看到 ref 就想点"这个动作，
        #    挤在行文里等于没写。无 `warn` 时这条分支与原文**逐字节相同**。
        head = f"{header}\n\n{warn}\n\n" if warn else f"{header}\n"
        body = "\n".join(out)
        # ⚠️ 末尾这段是**承诺**，本会话兑现不了就不能照样写：`readonly` 时原文那句
        #    「可直接 click(x=..,y=..)；也可 click(ref=N)」是假的（见上方 `readonly` 的来源）。
        #    留着的代价不是"少说了一句"，而是**主动误导** —— 模型照着做、被拒、
        #    然后归因成自己点偏了。
        if readonly:
            howto = ("（坐标已是**屏幕绝对坐标**，但本会话**没有坐标级通道**："
                     "上面这些 ref/坐标只能读，点不了。要操作请改用元素级 ref。"
                     "文字是快照，界面变了请重新识别）")
        else:
            howto = ("（坐标已是**屏幕绝对坐标**，可直接 click(x=..,y=..)；"
                     "也可 click(ref=N) 由服务端代点，避免抄错。"
                     "文字是快照，界面变了请重新识别）")
        return f"{head}{body}\n\n{howto}"
