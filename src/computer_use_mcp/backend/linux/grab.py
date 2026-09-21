"""
屏幕抓取（backend/linux/grab）—— 截图与 OCR 共用的**唯一**抓屏入口。

为什么要单独一层：截图（backend.screenshot）与 OCR（ocr.OcrReader）都要把某个 display
的像素抓成 PIL 图像，而「抓哪个屏」必须经 core/display.effective_display()（沙箱隔离的
根），且 mss **旧版本**不接受 display 参数、需要临时换 os.environ["DISPLAY"] —— 工具调用
在 anyio 线程池里并发执行，这个 env 交换必须加锁防竞态。这些坑只写一遍，避免两处各自出错。

⚠️ 但「临时换 env」是**不得已的兼容路径**，不是常态（M-38）：os.environ 是有别的读者的
（`display.host_display()` → `user_inside_sandbox` 的礼让检查），而 GRAB_LOCK 只挡得住
grab_rgb 彼此。故首次抓屏会探测一次 `mss.mss(display=…)` 是否可用，可用就**再也不动 env**，
锁也一并省掉；只有旧版 mss 才走那条带副作用的兼容路径。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from ...core import display
from ...utils.errors import BackendUnavailableError, ComputerUseError
from ...utils.logging import get_logger

log = get_logger(__name__)

# mss 旧版本不吃 display 参数时需临时改 os.environ["DISPLAY"]，交换期间必须独占。
GRAB_LOCK = threading.Lock()

# mss 是否接受 display= 参数。**只在首次抓屏时探测一次**（M-38）：
#   - True  → 走无副作用的那条路，根本不动 os.environ，也就不需要 GRAB_LOCK；
#   - False → 只能退回「临时换 env」的旧版本兼容路径（那条路必须持锁）。
# 为什么非要绕开 env 交换：全局 env 有**别的读者**——`display.host_display()` 读它，
# 而它的使用者 `user_inside_sandbox`（注入前的礼让检查）要用它去**宿主屏**上找 Xephyr
# 窗口。GRAB_LOCK 只挡得住 grab_rgb 彼此，挡不住那个读者：一次锁内的 env 交换就足以让
# 并发的礼让检查跑去**沙箱屏**找 Xephyr → 查不到 → 返回 False → **礼让被静默跳过**。
_MSS_TAKES_DISPLAY: bool | None = None


def _probe_display_kwarg(disp: str) -> bool:
    """探测本机 mss 是否接受 `display=` 参数（构造期就抛 TypeError 即为旧版本）。"""
    import mss

    try:
        with mss.mss(display=disp):
            pass
        return True
    except TypeError:
        return False


def _clamp_region(
    region: tuple[int, int, int, int], virtual: dict,
) -> dict[str, int]:
    """
    把 region 与「全部显示器并集」求交，返回 mss 的 monitor dict（M-39）。

    为什么必须 clamp：`describe_point(with_text=True)` 的落点文字探测框是「落点周围
    半宽 160 / 半高 32」，在**屏幕边缘**必然越界（x=50 → left=-110）。mss 对越界矩形
    直接抛 ScreenShotError，被 `_text_near` 吞掉 —— 结果是屏幕边缘的按钮**永远拿不到
    落点文字**，而且失败原因被静默，看起来像「那儿没字」。

    ⚠️ 原点取**裁剪后**的左上角（返回值里的 left/top），不是原始 region 的左上角：
    调用方（OCR / `_text_near`）用「原点 + 图内偏移」换算屏幕坐标，用原始值会让
    边缘那一块的文字坐标整体偏出去。
    """
    vx, vy = int(virtual["left"]), int(virtual["top"])
    vw, vh = int(virtual["width"]), int(virtual["height"])
    x, y, w, h = (int(v) for v in region)

    left = max(x, vx)
    top = max(y, vy)
    right = min(x + w, vx + vw)
    bottom = min(y + h, vy + vh)
    if right <= left or bottom <= top:
        raise ComputerUseError(
            f"截图区域完全落在屏幕之外: region={[x, y, w, h]}，"
            f"屏幕并集=({vx},{vy},{vw}x{vh})"
        )
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def _grab_raw(disp: str, region: tuple[int, int, int, int] | None, use_kwarg: bool):
    """
    在目标屏上抓一块，返回 `(raw, origin)`；把全部 mss 异常收口成 ComputerUseError。

    use_kwarg=True → `mss.mss(display=disp)`（无全局副作用）；
    use_kwarg=False → 依赖调用方已经换好 os.environ["DISPLAY"]（旧版本 mss 兼容路径）。
    """
    import mss

    cm = mss.mss(display=disp) if use_kwarg else mss.mss()
    with cm as sct:
        if region:
            if int(region[2]) <= 0 or int(region[3]) <= 0:
                raise ComputerUseError(
                    f"截图区域尺寸非法: region={list(region)}（宽高必须为正）")
            mon = _clamp_region(region, sct.monitors[0])
        else:
            mon = sct.monitors[0]   # 全部显示器并集
        origin = (int(mon["left"]), int(mon["top"]))
        return sct.grab(mon), origin


def grab_rgb(
    region: tuple[int, int, int, int] | None = None,
) -> tuple[Any, tuple[int, int]]:
    """
    抓取目标屏的一块区域，返回 `(PIL.Image(RGB), 原点)`。

    **原点 = 图像像素 (0,0) 对应的屏幕绝对坐标**，必须与图一起返回（I-8）：
    未指定 region 时抓的是「全部显示器并集」（`mss.monitors[0]`），而并集的 left/top
    **不保证是 (0,0)**——副屏位于主屏**左侧或上方**时它是负值。调用方若默认「图像原点
    = 屏幕原点」，返回的坐标就整体偏一个屏宽，模型据此点击**必定点空**；而 OCR 通道的
    核心卖点恰是「坐标是算出来的不是猜的」。
    把原点**绑在返回值里**（而不是让调用方自己推算）是刻意的：拿得到图就必然拿得到原点，
    不存在「忘了考虑原点」这个选项。本机两屏并集原点恰好是 (0,0)，故这是**潜伏**问题，
    换一台「外接屏在左/上」的机器即暴露。

    实现逻辑：
      1. 目标屏取 display.effective_display()——isolated 模式是 Xephyr 沙箱屏，
         绝不直接读 os.environ["DISPLAY"]（那会抓到用户真实桌面）。
      2. 在锁内完成「换 env → mss 抓屏 → 还原 env」，兼容新旧 mss。
      3. region=(x,y,w,h) 为屏幕绝对坐标；为空则抓全部显示器并集。两种情况都从**实际
         使用的那个 monitor 矩形**取原点，故调用方无需区分自己走的是哪条分支。
      4. BGRA 原始字节转 RGB（不是 BGRX 顺序的话颜色会错）。

    失败一律转成 ComputerUseError 体系（I-5）：本函数是截图与 OCR 两条链路的共同入口，
    而 mss/PIL 的异常（ImportError、ScreenShotError、越界矩形…）都**不是**
    ComputerUseError，会穿透 tools/ 的 except 一路逃到 MCP 层——模型只拿到一句零信息量的
    "Error executing tool X"，既不知道是缺依赖还是参数错。在这里收口，两条链路同时受益。
    """
    try:
        import mss
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailableError(f"抓屏依赖缺失(mss/pillow): {exc}") from exc

    global _MSS_TAKES_DISPLAY
    disp = display.effective_display()
    origin: tuple[int, int] = (0, 0)
    if _MSS_TAKES_DISPLAY is None:
        _MSS_TAKES_DISPLAY = _probe_display_kwarg(disp)
        log.debug("mss 支持 display= 参数：%s（False 时只能退回临时换 os.environ）",
                  _MSS_TAKES_DISPLAY)

    try:
        if _MSS_TAKES_DISPLAY:
            # ✅ 首选路径：不改任何进程级状态 → 不需要锁，也不会干扰礼让检查
            raw, origin = _grab_raw(disp, region, use_kwarg=True)
        else:
            # 兼容路径（旧版 mss）：换 env 期间必须独占（见 GRAB_LOCK 的说明）
            with GRAB_LOCK:
                old = os.environ.get("DISPLAY")
                os.environ["DISPLAY"] = disp
                try:
                    raw, origin = _grab_raw(disp, region, use_kwarg=False)
                finally:
                    if old is None:
                        os.environ.pop("DISPLAY", None)
                    else:
                        os.environ["DISPLAY"] = old
    except ComputerUseError:
        raise                        # 上面主动抛的（区域非法/越界）原样上抛
    except Exception as exc:  # noqa: BLE001
        # ScreenShotError / 屏不存在 / X 连接断开等都在这里收口
        raise ComputerUseError(
            f"抓屏失败（display={disp}, region={list(region) if region else '全屏'}）: {exc}"
        ) from exc
    try:
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception as exc:  # noqa: BLE001
        raise ComputerUseError(f"抓屏像素解码失败: {exc}") from exc
    return img, origin


def draw_crosshair(
    img: Any, px: int, py: int, color: tuple[int, int, int] = (255, 0, 0),
    radius: int = 16, arm: int = 26,
) -> None:
    """
    就地在图上画「十字准星」（红十字 + 小圆环），标出**实际落点**在图中哪个像素。

    两点刻意的设计：
      1. **先画深色描边、再叠红色**：纯红细线在深色/红色 UI 上会消失（灰区应用深色主题
         很常见），而准星一旦看不见，这张图就从「证据」变成「误导」。描边让它在任何
         底色上都可辨。
      2. **只画线，不写字**：图像上的文字要选字体、吃分辨率、还可能被 OCR 当成界面文本；
         落点的坐标本来就在 meta 文本里（更精确、更省 token）。

    为什么画在**图上**而不是屏幕上的光圈（见 ring.py）：光圈是给人看的、随点击闪一下
    就没了；准星是给模型看的、必须与「点击前那一瞬」的像素绑定在一起。

    就地修改（返回 None），不碰编码——编码仍由调用方在**唯一**位点完成。
    """
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    # 深色描边（粗一层），保证在亮底/暗底上都看得见
    d.line([(px - arm, py), (px + arm, py)], fill=(0, 0, 0), width=3)
    d.line([(px, py - arm), (px, py + arm)], fill=(0, 0, 0), width=3)
    d.ellipse([px - radius - 1, py - radius - 1, px + radius + 1, py + radius + 1],
              outline=(0, 0, 0), width=3)
    # 红色主体
    d.line([(px - arm, py), (px + arm, py)], fill=color, width=1)
    d.line([(px, py - arm), (px, py + arm)], fill=color, width=1)
    d.ellipse([px - radius, py - radius, px + radius, py + radius], outline=color, width=1)


def changed_fraction(
    img_a: Any, img_b: Any, threshold: int = 20,
) -> float:
    """
    两张**同尺寸**图之间「变化像素的占比」（0.0~1.0），用于判断点击有没有被界面响应。

    为什么需要它：`ok=True` 只说明「事件发出去了」。同一小块在**点前/点后**各拍一张再比，
    「几乎没变」就是「这一下大概率点空了」的物理证据——它**不需要知道模型想点谁**，
    是对「落点文字/偏差数值」的独立第二信号（两者一起看才不易误判：偏 45px 但界面变了
    = 按钮热区比文字大、其实点中了）。

    判据用「灰度差 > threshold 的像素占比」而非逐像素相等：光标闪烁、抗锯齿、动画这类
    噪声会让逐像素比较永远为「变」，那样这个信号就没用了。

    尺寸不同（屏幕布局在两次抓取之间变了）时抛 ComputerUseError——不是同尺寸的图比较
    没有意义，静默返回 0 会被读成「界面没变」。
    """
    from PIL import ImageChops

    if img_a.size != img_b.size:
        raise ComputerUseError(
            f"变化对比的两张图尺寸不一致: {img_a.size} vs {img_b.size}（屏幕布局变了？）"
        )
    diff = ImageChops.difference(img_a.convert("L"), img_b.convert("L"))
    total = img_a.size[0] * img_a.size[1]
    if total <= 0:
        return 0.0
    hist = diff.histogram()
    changed = sum(hist[threshold + 1:])   # 灰度差 > threshold 视为「变了」
    return changed / total