"""
OCR 文本层（backend/linux/ocr）—— 灰区应用的「元素树替代品」。

问题：SWT/Java、自绘控件、游戏、远程桌面这类应用没有无障碍树（或树一读就崩），本项目
的三级降级只能落到「截图 + 坐标点击」。而截图路线最贵的一环**不在工具侧**（截图只要
0.2s），而在**模型侧**：它得看图、估算像素位置、再把图像坐标换算回屏幕坐标。实测一次
DBeaver 任务 8.3 分钟里，工具只占 21 秒，其余 478 秒全花在这个循环上。

本模块把「屏幕上有哪些字、各自在哪」直接变成**文本**：模型读到 `[ref] 文字 @ (x,y)`
就能点，既不用看图、也不用估算坐标（坐标是识别时算出来的，不是猜的）。

实测耗时（1600x1000，psm=11，chi_sim+eng）：
  - 全屏密集文字界面：8~10s
  - 一个对话框大小的区域（约 700x250）：2~3s
故**默认只识别活动窗口那块区域**，而不是全屏——见 coordinator.get_screen_text。

实现选择：直接 subprocess 调 `tesseract` 命令行，而不是 pytesseract——
  - 少一个 Python 依赖，PyInstaller 不必加 hidden-import；
  - `--psm 11`（稀疏文本）正合 GUI 界面；
  - `tsv` 输出自带每个词的 left/top/width/height/conf，正是我们要的坐标。
与 xdotool / Xephyr 一样，tesseract 属于**运行时需系统提供**的 OS 级依赖
（`sudo apt install tesseract-ocr tesseract-ocr-chi-sim`），不内嵌进冻结产物。

已知限制（实测查明，别误判成参数没调好）：
  - **文字紧邻深色图标时，整行识别会崩**。实测 zenity 对话框：「确认删除该文件吗？」
    旁边有个深色问号图标时被识别成 "wissen?"（conf 31）；把图标裁掉、只留正文行后
    变成「确认/该/文件/吗/?」，几乎全对。
    根因是 tesseract 的**行级**识别把图标与文字一起看，不是对比度问题——实测加灰度、
    自动对比度、二值化、换 psm(4/6/11/12/13)、放大 1~3 倍，含图标时一律失败。
    **绕过办法**：把 `region` 收窄到只含文字的那一小块，再调一次（模型可先用第一次
    的结果拿到该块的 bbox，再据此收窄重试）。
  - 纯文本场景（菜单、按钮、列表项、标题）识别准确率高，不受此影响。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import time

from ...core import display
from ...utils.logging import get_logger
from ..base import Rect, TextBlock
from .grab import grab_rgb

log = get_logger(__name__)

# 识别语言：中文场景默认简中+英文。用 CC_CU_OCR_LANG 可换（如纯英文界面用 eng 更快）。
ENV_OCR_LANG = "CC_CU_OCR_LANG"
DEFAULT_LANG = "chi_sim+eng"

# --psm 11 = 稀疏文本：不假设版面结构，适合 GUI（菜单/按钮/表格混杂）
_PSM = "11"
_TIMEOUT = 60.0

# 低于此置信度的词直接丢弃（GUI 上常有一像素噪点被识别成怪字符）
_MIN_CONF = 40.0

# 把图放大再识别：小字号中文（12~14px）在 1x 下错误率明显偏高。
# 实测放大 2x 的代价约 +20% 耗时，换来显著更准，划算。
_UPSCALE = 2


def _upscale_image(img):
    """
    放大 OCR 输入图（倍率 `_UPSCALE`）。

    **必须显式指定 LANCZOS**（M-37）：不指定时 Pillow 默认用 BICUBIC，而 2x **放大**
    小字号文字时 BICUBIC 会明显发糊——糊掉的笔画正是 tesseract 最容易认错的东西。
    LANCZOS 边缘更锐，与 `backend.screenshot` 缩小时的选择是同一套理由。
    """
    from PIL import Image as _PILImage

    return img.resize((img.width * _UPSCALE, img.height * _UPSCALE), _PILImage.LANCZOS)


class OcrReader:
    """用 tesseract 读取屏幕上的一块区域，返回带屏幕坐标的文本块。"""

    def __init__(self) -> None:
        self.lang = os.environ.get(ENV_OCR_LANG, DEFAULT_LANG).strip() or DEFAULT_LANG
        self._exe = shutil.which("tesseract")
        self.error: str | None = None if self._exe else "未安装 tesseract（sudo apt install tesseract-ocr tesseract-ocr-chi-sim）"

    def is_available(self) -> bool:
        return self._exe is not None

    def read(
        self, region: tuple[int, int, int, int] | None = None, min_conf: float = _MIN_CONF,
    ) -> list[TextBlock]:
        """
        识别一块区域，返回按「从上到下、从左到右」排好序的文本块。

        实现逻辑：
          1. 抓屏（grab_rgb，经 display.effective_display，沙箱安全）。它同时返回**原点**
             ——图像像素 (0,0) 对应的屏幕绝对坐标。全屏抓的是显示器并集，其原点不保证是
             (0,0)，所以原点一律用返回值、绝不硬编码（见 grab_rgb 的 docstring / I-8）。
          2. 放大 _UPSCALE 倍后编码 PNG 交给 tesseract（stdin 直传，不落临时文件）。
          3. 解析 TSV：只取 level==5 的行（词级），每行带 left/top/width/height/conf。
          4. 把同一 (block, par, line) 的词合并成「行」——这样 '忽略' 才是一个块，
             而不是被切成单字，模型也才好按语义点。
          5. union 出整行的包围盒，换算回原始坐标（除以放大倍数）并加上**原点**。
          6. 过滤低置信度与空白文本，按 (top, left) 排序返回。
        """
        if not self.is_available():
            raise RuntimeError(self.error or "tesseract 不可用")

        t0 = time.monotonic()
        # 原点由抓屏一并给出（I-8）：**不要**再写 `region[0] if region else 0`——
        # 那是把「图像原点 = 屏幕原点」当默认，而全屏抓的是显示器**并集**，副屏在
        # 主屏左/上时并集原点是负值，硬编码 0 会让所有文字坐标偏一个屏宽。
        img, (off_x, off_y) = grab_rgb(region)

        blocks = self.read_image(img, (off_x, off_y), min_conf=min_conf)
        log.info("OCR region=%s 用时 %.2fs，识别 %d 块文本",
                 region, time.monotonic() - t0, len(blocks))
        return blocks

    def read_image(
        self, img, origin: tuple[int, int], min_conf: float = _MIN_CONF,
    ) -> list[TextBlock]:
        """
        识别**已经抓好的一张 PIL 图**（不再抓屏），坐标以 `origin` 为图像左上角的屏幕位置。

        为什么要有这个入口：坐标点击时会先为「点击预览图」抓一次屏（480x300 裁剪）。
        接着要读落点文字/候选文字时，若再按 region 抓一次，就是同一份像素抓两遍；
        更关键的是——**预览图上已经画了准星**，红线横穿文字会让识别变差。故把「原始图」
        留在内存里、直接喂给这里：一次抓屏，两处复用。

        实现逻辑与 `read` 的后半段完全一致（放大 → PNG → tesseract --psm 11 → 解析 TSV →
        坐标除以放大倍数并加原点）。
        """
        if not self.is_available():
            raise RuntimeError(self.error or "tesseract 不可用")

        t0 = time.monotonic()
        off_x, off_y = int(origin[0]), int(origin[1])
        if _UPSCALE != 1:
            img = _upscale_image(img)
        buf = io.BytesIO()
        img.save(buf, format="PNG")   # OCR 输入要无损，这里不能省成 JPEG

        cmd = [self._exe, "stdin", "stdout", "--psm", _PSM, "-l", self.lang, "tsv"]
        try:
            # 走 env_for 而非继承 os.environ：tesseract 也是系统二进制，而产物里打包了
            # 它需要的同名库（libjpeg.so.8 / libpng16.so.16 / libtiff.so.6 / libwebp.so.7
            # / libz.so.1，SONAME 精确命中）—— 不剥就会让它用产物内那份图片解码库。
            p = subprocess.run(cmd, input=buf.getvalue(), capture_output=True,
                               timeout=_TIMEOUT, env=display.env_for())
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"tesseract 超时(>{_TIMEOUT}s)，区域可能过大") from exc
        if p.returncode != 0:
            raise RuntimeError(
                f"tesseract 失败(rc={p.returncode}): {p.stderr.decode('utf-8', 'replace')[:200]}"
            )

        blocks = self._parse_tsv(p.stdout.decode("utf-8", "replace"),
                                 off_x, off_y, min_conf)
        log.info("OCR(已抓图 %sx%s) 用时 %.2fs，识别 %d 块文本",
                 img.width, img.height, time.monotonic() - t0, len(blocks))
        return blocks

    @staticmethod
    def _parse_tsv(tsv: str, off_x: int, off_y: int, min_conf: float) -> list[TextBlock]:
        """
        解析 tesseract 的 TSV 输出，按行聚合后再按「大间隙」切块。

        TSV 列序：level page_num block_num par_num line_num word_num left top width height conf text
        只关心 level==5（词）。

        处理逻辑：
          1. 收集词级记录（过滤低置信度与空白）。
          2. 按 tesseract 的 (block,par,line) 分组，组内按 left 排序。
          3. **组内再按横向间隙切块**：tesseract 会把同一视觉行里的所有东西并成一行，
             于是「一个图标 + 右侧一段文字」会被并成一块，返回的坐标落在两者中间——
             点下去两头都不着。间隙超过 1.2 倍行高即视为两块。
             （注意：中文常被切成单字，字间间隙极小，不会误切。）
          4. 坐标要 (1) 除以放大倍数、(2) 加**原点偏移**（抓屏返回的图像原点，不是
             硬编码的 0，也不是调用方传的 region——全屏抓取时 region 为空、并集原点
             未必是 (0,0)），才是屏幕绝对坐标。
        """
        words: list[tuple] = []
        for raw in tsv.splitlines()[1:]:          # 跳过表头
            cols = raw.split("\t")
            if len(cols) < 12 or cols[0] != "5":
                continue
            text = cols[11].strip()
            if not text:
                continue
            try:
                conf = float(cols[10])
                left, top = int(cols[6]), int(cols[7])
                w, h = int(cols[8]), int(cols[9])
                key = (int(cols[2]), int(cols[3]), int(cols[4]))
            except ValueError:
                continue
            if conf < min_conf:
                continue
            words.append((key, left, top, w, h, conf, text))

        groups: dict[tuple, list[tuple]] = {}
        for w in words:
            groups.setdefault(w[0], []).append(w)

        out: list[TextBlock] = []
        for ws in groups.values():
            ws.sort(key=lambda x: x[1])           # 按 left 排
            chunks: list[list[tuple]] = [[ws[0]]]
            for prev, cur in zip(ws, ws[1:]):
                gap = cur[1] - (prev[1] + prev[3])
                line_h = max(prev[4], cur[4])
                if gap > max(8, int(1.2 * line_h)):
                    chunks.append([cur])
                else:
                    chunks[-1].append(cur)
            for ch in chunks:
                text = _join_words([c[6] for c in ch])
                if not text.strip():
                    continue
                x0 = min(c[1] for c in ch)
                y0 = min(c[2] for c in ch)
                x1 = max(c[1] + c[3] for c in ch)
                y1 = max(c[2] + c[4] for c in ch)
                out.append(TextBlock(
                    text=text,
                    rect=Rect(x=x0 // _UPSCALE + off_x, y=y0 // _UPSCALE + off_y,
                              w=(x1 - x0) // _UPSCALE, h=(y1 - y0) // _UPSCALE),
                    conf=max(c[5] for c in ch),
                ))
        out.sort(key=lambda b: (b.rect.y, b.rect.x))
        return out


def _join_words(words: list[str]) -> str:
    """
    把一行里的词拼成整串。

    中文场景的关键细节：tesseract 常把中文按字切开（"忽" "略"），若一律用空格连，
    会得到 "忽 略" 这种模型不好匹配、人也不好读的结果。故：**两侧都是非 ASCII 字符时
    不加空格**，其余情况（英文单词之间）才加。
    """
    out = ""
    for w in words:
        if not out:
            out = w
            continue
        ascii_prev = out[-1].isascii()
        ascii_cur = w[0].isascii()
        out += (" " if (ascii_prev and ascii_cur) else "") + w
    return out