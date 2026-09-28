"""
平台中立的像素层（backend.screen）—— 截图与点击反馈共用的**唯一**像素工具集。

═══ 为什么单独一层 ═══

「抓一块屏 → 缩放 → 编码 → 算元数据」这一串里，**真正跟平台有关的只有一件事：
抓哪块屏**。其余全是纯 PIL 的活：

  · `clamp_region` / `draw_crosshair` / `changed_fraction` 是纯几何与纯算术；
  · `encode_image` 是缩放 + meta + 编码；
  · `build_click_preview` 是裁剪 + 画准星 + JPEG 编码。

它们原先都在 `backend/linux/` 里，于是 Windows 拿不到——表现为「坐标点击发出去了、
模型拿不到任何反馈」，而反馈正是本项目可信坐标点击的全部依据。

═══ ⚠️ 与 `backend/linux/grab.py` 的分工（别搞混）═══

`linux/grab.py` 保留**全部 X11 专属**的东西：`effective_display()`（抓哪块屏）、
旧版 mss 的换 `os.environ["DISPLAY"]` 兼容路径与它的锁。
本模块只放**两边都成立**的东西。

`linux/grab.py` 会把本模块的 `clamp_region` / `draw_crosshair` / `changed_fraction`
**重导出**（`from ..screen import ...`），这样 `tests/` 里 20+ 处
`monkeypatch.setattr(grab_mod, "<名字>", …)` 全都继续生效。

★ 由此推出一条必须遵守的纪律（违反它会**静默**废掉那些守卫）：
  **Linux 的生产代码继续经 `grab.*` 调用这几个名字**（`grab.draw_crosshair` 这种属性查找），
  只有 Windows 才直接调 `screen.*`。
  反过来写（Linux 改调 `screen.draw_crosshair`）的后果是：补丁**照样 setattr 成功**
  （名字还在），但生产代码不再读它 → 补丁打空 → 用例照绿而零覆盖。
  本项目记录过这一失效模式（拆 mixin 那次两条守卫静默变空集合）。

═══ 为什么 `grab_desktop` 不放在 linux 侧 ═══

它调 `mss.mss()` **不带 `display=`**。在 Linux 上那样写会抓到**宿主**屏（沙箱失效），
所以 Linux 永远不许用它；而 Windows 没有 DISPLAY 这个概念，这就是正确写法。
故它是「抓哪块屏不是问题」的平台的入口，Linux 侧没有调用点。
"""

from __future__ import annotations

import io
from typing import Any, Callable

from ..utils.errors import BackendUnavailableError, ComputerUseError
from .base import ClickPreview

# 点击预览窗的半宽/半高：480x300。**在屏幕边缘会被 `clamp_region` 裁小**——
# 调用方绝不能用这两个常量反推准星位置（见 `build_click_preview` 的注释）。
PREVIEW_HALF_W = 240
PREVIEW_HALF_H = 150


# ── 抓屏（平台中立的那条）────────────────────────────────────────────────

def grab_desktop(region: tuple[int, int, int, int] | None = None,
                 ) -> tuple[Any, tuple[int, int]]:
    """
    抓本进程所在桌面的像素，返回 `(PIL.Image(RGB), 原点)`。**不换任何 display。**

    ⚠️ 仅给「抓哪块屏不是问题」的平台（Windows：没有 DISPLAY；隐藏桌面隔离靠
    `lpDesktop` 而不是另一块屏）。**Linux 绝不许调它** —— 不带 `display=` 的
    `mss.mss()` 在 Linux 上会抓 `$DISPLAY` 指向的那块屏，沙箱模式下正是**宿主**桌面。

    原点的语义与 `grab_rgb` 完全一致：**图像像素 (0,0) 对应的屏幕绝对坐标**，
    未指定 region 时抓「全部显示器并集」（mss 的 `monitors[0]`，Windows 侧取
    `SM_XVIRTUALSCREEN` 系列），副屏在主屏左/上时它是**负值**。

    失败一律转成 ComputerUseError 体系（同 `grab_rgb` 的理由：mss/PIL 的异常不是
    ComputerUseError，会穿透 tools/ 的 except 逃到 MCP 层，模型只拿到一句零信息量的
    "Error executing tool")。
    """
    try:
        import mss
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailableError(f"抓屏依赖缺失(mss/pillow): {exc}") from exc

    try:
        with mss.mss() as sct:
            if region:
                if int(region[2]) <= 0 or int(region[3]) <= 0:
                    raise ComputerUseError(
                        f"截图区域尺寸非法: region={list(region)}（宽高必须为正）")
                mon = clamp_region(region, sct.monitors[0])
            else:
                mon = sct.monitors[0]      # 全部显示器并集
            origin = (int(mon["left"]), int(mon["top"]))
            raw = sct.grab(mon)
    except ComputerUseError:
        raise                            # 上面主动抛的（区域非法/越界）原样上抛
    except Exception as exc:  # noqa: BLE001
        raise ComputerUseError(
            f"抓屏失败（region={list(region) if region else '全屏'}）: {exc}"
        ) from exc

    try:
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"), origin
    except Exception as exc:  # noqa: BLE001
        raise ComputerUseError(f"抓屏像素解码失败: {exc}") from exc


def clamp_region(
    region: tuple[int, int, int, int], virtual: dict,
) -> dict[str, int]:
    """
    把 region 与「全部显示器并集」求交，返回 mss 的 monitor dict。

    为什么必须 clamp：点击预览的窗口是「落点周围半宽 240 / 半高 150」，在**屏幕边缘**
    必然越界（x=50 → left=-190）。mss 对越界矩形直接抛 ScreenShotError，被上层吞掉 ——
    结果是屏幕边缘的按钮**永远拿不到预览图与落点文字**，而且失败原因被静默，
    看起来像「那儿没东西」。

    ⚠️ 原点取**裁剪后**的左上角（返回值里的 left/top），不是原始 region 的左上角：
    调用方用「原点 + 图内偏移」换算屏幕坐标，用原始值会让边缘那一块的坐标整体偏出去。
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


# ── 编码与元数据 ─────────────────────────────────────────────────────────

def encode_image(
    img: Any, origin: tuple[int, int],
    region: tuple[int, int, int, int] | None = None,
    max_side: int | None = None, fmt: str = "jpeg", quality: int = 85,
) -> tuple[bytes, dict[str, Any]]:
    """
    `screenshot` 的**尾部**：按 `max_side` 缩放 → 组装 meta → 编码**一次**。

    ⚠️ 编码只做一次（修历史坑）：旧实现是「backend 全尺寸 PNG 编码 → coordinator 再
    Image.open 解码 → 缩放 → 重新编码」，同一张图被压两遍，且全尺寸那一次完全白费
    （实测 3840x1200 下就是 266ms）。故顺序固定为**抓屏 → 缩放 → 编一次**。

    为什么默认 JPEG 而非 PNG（实测 3840x1200）：JPEG(q=70) 编码 14ms / 334KB，
    PNG(level=1) 266ms / 3065KB —— 快 19 倍、小 9 倍。截图是给人/模型看的，不需要
    无损。若确有需要可传 fmt="png"。

    meta 里的 `origin` 与 `scale` 是**模型换算屏幕坐标的唯一依据**
    （`屏幕坐标 = origin + 图上坐标 × scale`），缺一个模型就只能猜。
    """
    from PIL import Image

    orig_w, orig_h = img.size
    meta: dict[str, Any] = {
        "format": fmt,
        "region": list(region) if region else None,
        # ⚠️ origin = 图像像素 (0,0) 对应的**屏幕绝对坐标**。全屏抓取抓的是显示器并集，
        # 其原点不保证是 (0,0)——副屏在主屏左/上时是负值。模型要靠
        # 「屏幕坐标 = origin + 图上坐标 × scale」换算，缺了 origin 就会整体偏一个屏宽。
        "origin": [int(origin[0]), int(origin[1])],
        "source_size": [orig_w, orig_h],
    }
    if max_side and max(orig_w, orig_h) > max_side:
        ratio = max_side / max(orig_w, orig_h)
        new_size = (max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio)))
        # LANCZOS：缩小时保留文字边缘锐度，缩小后模型仍能看清小字
        img = img.resize(new_size, Image.LANCZOS)
        meta["resized"] = [img.width, img.height]
        # ⚠️ scale 是「图像像素 → 屏幕像素」的倍数（屏幕坐标 = 图上坐标 × scale）。
        # 少了它，模型只能**猜**这张图缩了多少，据此算出的落点必然偏——实测踩到过。
        meta["scale"] = orig_w / img.width
    meta["width"], meta["height"] = img.size

    buf = io.BytesIO()
    if fmt == "png":
        # 去掉 optimize=True（数百毫秒纯 CPU 浪费且肉眼无差异），低压缩等级快一个量级
        img.save(buf, format="PNG", compress_level=1)
    else:
        img.save(buf, format="JPEG", quality=quality)
    data = buf.getvalue()
    meta["bytes"] = len(data)
    return data, meta


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


def build_click_preview(
    img: Any, origin: tuple[int, int], x: int, y: int,
    region: tuple[int, int, int, int] | None = None,
    annotate: Callable[[Any, int, int], None] | None = None,
) -> ClickPreview | None:
    """
    已抓好的整块像素 → 点击预览（`ClickPreview`）。落点在裁剪图外时返回 None。

    ⚠️ `region` 必须传：消费方 `core/coordinator/landing.py` 的
    `_change_from_preview` 拿 `meta["region"]` 去**再抓同一块**做点前后对比，
    缺了它那一行反馈直接返回空串（不报错、静默少一样证据）。

    `annotate(img, px, py)` 是画准星的函数，默认 `draw_crosshair`。
    ⚠️ 参数化它的唯一理由是**保住 Linux 侧的补丁点**：`linux/backend.py` 传
    `grab.draw_crosshair`（属性查找），于是 `monkeypatch.setattr(grab_mod,
    "draw_crosshair", …)` 仍然打在真正被调用的那个函数上。若这里写死
    `draw_crosshair`，那些补丁会静默打空（见模块 docstring 的纪律）。

    实现逻辑（每一步都有失败保护——预览图是**附加证据**，拿不到绝不能让点击失败）：
      1. **准星像素 = 落点 − 裁剪后原点**（★ 最容易写错的一行）：`clamp_region`
         在屏幕边缘会改变原点，若图省事写死 `PREVIEW_HALF_W`，则**越靠屏幕边缘的按钮
         准星指得越离谱**——而这个功能存在的唯一理由就是「位置对不对」，那就是主动
         误导级 bug。
      2. 落点不在返回图内（极端越界）→ 返回 None：宁可不发图，也不发一张指错的图。
      3. 准星画在**副本**上，原始图随 ClickPreview 一起返回：同一块像素后面还要用于
         OCR（红线横穿文字会降低识别率）与「点前后对比」（红线会被误当变化）。
      4. `subsampling=0`（4:4:4）是必须的：默认的 4:2:0 会把 1px 细红线的色度糊掉，
         模型看到一团粉雾、反而看不清十字中心落在哪个像素。
    """
    if img is None:
        return None
    px, py = int(x) - int(origin[0]), int(y) - int(origin[1])
    if not (0 <= px < img.width and 0 <= py < img.height):
        return None
    annotated = img.copy()          # 准星只画在副本上（原始图留给 OCR / 像素对比）
    (annotate or draw_crosshair)(annotated, px, py)
    buf = io.BytesIO()
    annotated.save(buf, format="JPEG", quality=85, subsampling=0)
    data = buf.getvalue()
    meta: dict[str, Any] = {
        "kind": "click_preview", "format": "jpeg",
        "x": int(x), "y": int(y),
        "region": [int(v) for v in region] if region else None,
        "origin": [int(origin[0]), int(origin[1])],
        "crosshair": [px, py],          # 十字中心在**图上**的像素位置
        "scale": 1,                     # 未缩放：图上 1 像素 = 屏幕 1 像素
        "width": img.width, "height": img.height, "bytes": len(data),
    }
    return ClickPreview(data=data, meta=meta, image=img)


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