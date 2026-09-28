"""
Windows OCR（backend.windows.driver.ocr）—— 系统自带 `Windows.Media.Ocr` 的唯一入口。

═══ 为什么用自带引擎，而不是随包带 tesseract ═══

2026-09-28 真机实测定的案（探针与数据见 `docs/PR/windows-driver/README.md` 第十二节）：

  · **零安装**：中文版 Windows 本来就有 `zh-Hans-CN` 识别语言，不需要管理员、不需要联网；
  · **不需要打包工程**：它是**系统 API**，不是要随包的二进制 —— 这是选它最大的收益；
  · **隐藏桌面上可用**（`CreateDesktopW` + `lpDesktop`，与交互桌面识别结果逐字相同），
    而我们的隔离方案正建立在隐藏桌面上；
  · 耗时 1920x1009 全屏 **≈2.7 s**（文字稀疏）。

⚠️ 一条**文档与实测相反**的记录，别再被它带偏：微软文档说 `Windows.Media.Ocr` 命名空间
「只支持有 package identity 的桌面应用（MSIX 打包）」。实测**不成立** —— 本进程
`Package.Current` 确实抛 `0x80073D54`（没有 identity），但引擎照常创建、照常出中文。
**「文档这么写」不等于「实际会拦」**；这类限制值得花一次探针去撞。

═══ 与本模块打交道的三条硬事实（每一条都踩过）═══

① **`SoftwareBitmap.CreateCopyFromBuffer` 只有 4 参重载**（`buffer, format, w, h`），
   没有带 `BitmapAlphaMode` 的 5 参版 —— 传 5 个参数报 `Invalid parameter count`。
② **`IAsyncOperation` 不是 coroutine**：`asyncio.run(op)` 会报
   `a coroutine was expected`。用 `op.get()`（同步阻塞）最省事，也用不着事件循环。
③ **中文被逐字拆成"词"**：`了解有关此图片的信息` 会变成 6 个各含一字的 word，
   行级 `text` 也是带空格的 `'了 解 有 关 此 图'`。见下面 `_merge_runs`。

④ ★ **`.get()` 在主线程上会炸**：`Cannot call blocking method from single-threaded apartment.`
   根因是 `WindowsBackend` 会 `import uiautomation` → `comtypes` 在 import 时把**主线程**
   初始化为 STA（单线程套间），而 STA 里禁止阻塞等待（会与消息泵死锁）。
   ⚠️ 独立探针能跑通是因为它**没 import uiautomation** —— 典型的「只有集成后才暴露」。
   解法见下面 `_run_on_ocr_thread`：**整段 WinRT 工作**（建引擎/位图/识别）都放到一条
   专属工作线程上执行。为什么是"整段"而不是只搬识别：跨套间传 WinRT 对象要 marshal，
   而它们未必 agile；把一整套都关在同一条线程里，就根本不产生跨套间引用。

═══ 单点纪律（同 `_uia.py`）═══

全仓**只有这里** import `winrt.*`。好处：缺包时错误信息只有一处要维护；
守卫可以断言「import winrt 只出现在本文件」；将来换绑定只改一个文件。
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from typing import Any

from ....utils.errors import BackendUnavailableError
from ...base import Rect, TextBlock

# 引擎无置信度，故 TextBlock.conf 只能给常数。**这个数值本身没有意义**，
# 它的作用只是让 min_conf 的默认阈值（40）不至于把所有块都滤掉 —— 见
# `WindowsBackend.read_text` 与 base.py 的 `SUPPORTS_TEXT_CONFIDENCE`。
_CONF_PLACEHOLDER = 100.0

# 单个 CJK 字符（含 CJK 标点与全角形式）。Windows OCR 把中文逐字切开，靠它认回来。
_CJK_CHAR = re.compile(
    r"^[　-〿㐀-䶿一-鿿豈-﫿＀-￯]$")

# 「CJK 空格 CJK」里的空格是引擎硬加的，要消掉；拉丁词之间的空格是真的，不能动。
_CJK_SPACE = re.compile(
    r"(?<=[　-〿㐀-䶿一-鿿豈-﫿＀-￯]) +"
    r"(?=[　-〿㐀-䶿一-鿿豈-﫿＀-￯])")


class WindowsOcrUnavailable(BackendUnavailableError):
    """自带 OCR 不可用（缺 PyWinRT、非 Windows、或系统没装识别语言）。"""


# ═══════════════════════ 专属工作线程（见模块 docstring ④）═══════════════════════
#
# 为什么必须有它：`uiautomation` → `comtypes` 会把**主线程**初始化成 STA，
# 而 STA 里 PyWinRT 拒绝 `.get()`（阻塞等待会与消息泵死锁）。
# 一条自己起的线程套间是 MTA，`.get()` 在那里合法。
#
# 为什么用**常驻单线程池**而不是每次开一条新线程：引擎要缓存（创建一次要枚举语言、
# 挑引擎），而 WinRT 对象必须在**创建它的那条线程**上使用 —— 缓存只有配常驻线程才成立。

_OCR_TIMEOUT_S = 60.0          # 与 Linux 侧 tesseract 的 _TIMEOUT 同量级
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
# `线程 ident → 引擎`。只有一条工作线程，正常只会有一条记录；
# 万一那条线程死了、池被重建，新线程 ident 不同 ⇒ 自动不复用旧引擎（跨套间用 WinRT 对象是错的）。
_engines: dict[int, Any] = {}


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cc-cu-winocr")
        return _pool


def _reset_pool() -> None:
    """
    丢弃当前池（超时后调用）。旧线程可能仍卡在 OCR 里，故**不 join**、只不再使用它。

    为什么要重置而不是继续共用：单线程池里一个卡死的任务会把后续所有任务永久堵住，
    表现是"第一次超时之后，OCR 再也不工作了"——比超时本身更难查。
    """
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False)
            _pool = None


def _require_winrt() -> dict[str, Any]:
    """
    延迟 import `winrt.*` 并返回需要的名字。**认不出就抛**，不静默降级成"没有识别结果"。

    ⚠️ 缺包是**环境问题**，必须让人看见：静默返回空列表会被读成「这个界面上没有文字」，
    而那正是本项目最忌讳的「把功能没做伪装成没找到」。
    """
    try:
        from winrt.windows.globalization import Language  # noqa: PLC0415
        from winrt.windows.graphics.imaging import (  # noqa: PLC0415
            BitmapPixelFormat, SoftwareBitmap,
        )
        from winrt.windows.media.ocr import OcrEngine  # noqa: PLC0415
        from winrt.windows.storage.streams import Buffer  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 —— 缺包/非 Windows/DLL 加载失败都在这
        raise WindowsOcrUnavailable(
            f"Windows 自带 OCR 不可用（{type(exc).__name__}: {exc}）。"
            f"需要 `pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging "
            f"winrt-Windows.Storage winrt-Windows.Storage.Streams "
            f"winrt-Windows.Globalization winrt-Windows.Foundation "
            f"winrt-Windows.Foundation.Collections`。"
        ) from exc
    return {"Language": Language, "BitmapPixelFormat": BitmapPixelFormat,
            "SoftwareBitmap": SoftwareBitmap, "OcrEngine": OcrEngine, "Buffer": Buffer}


def available_languages() -> list[str]:
    """系统装了哪些 OCR 识别语言（如 `['zh-Hans-CN']`）。取不到返回空列表。"""
    try:
        w = _require_winrt()
    except WindowsOcrUnavailable:
        return []
    try:
        return [lang.language_tag for lang in w["OcrEngine"].available_recognizer_languages]
    except Exception:  # noqa: BLE001
        return []


def is_available() -> bool:
    """本机能不能用（有 PyWinRT + 至少一种识别语言）。**不创建引擎**，便宜。"""
    return bool(available_languages())


class OcrReader:
    """
    `Windows.Media.Ocr` 的薄封装：给定一张 PIL 图，返回 `TextBlock` 列表。

    ⚠️ 所有真实的 WinRT 调用都发生在**专属工作线程**上（见 `_run_on_ocr_thread`），
    本类在主线程上只做参数整理 —— 主线程被 comtypes 占了 STA，碰不得阻塞等待。
    """

    def __init__(self, lang_prefix: str = "zh") -> None:
        # 语言前缀：默认挑中文引擎（本项目的目标场景）。系统只有别的语言时退回
        # `TryCreateFromUserProfileLanguages`，再不行就是没能力（抛）。
        self.lang_prefix = lang_prefix
        self._engine_error: str = ""

    # ---------- 引擎（**只在工作线程上调用**）----------

    def _engine_here(self) -> Any:  # noqa: ANN401
        """取本线程的引擎，没有就建一个。**必须在工作线程上调用。**"""
        tid = threading.get_ident()
        engine = _engines.get(tid)
        if engine is not None:
            return engine
        w = _require_winrt()
        OcrEngine = w["OcrEngine"]  # noqa: N806
        langs: list[Any] = []
        try:
            langs = list(OcrEngine.available_recognizer_languages)
            for lang in langs:
                if lang.language_tag.lower().startswith(self.lang_prefix.lower()):
                    engine = OcrEngine.try_create_from_language(lang)
                    if engine is not None:
                        break
            if engine is None:
                engine = OcrEngine.try_create_from_user_profile_languages()
        except Exception as exc:  # noqa: BLE001
            raise WindowsOcrUnavailable(f"创建 OCR 引擎失败: {exc}") from exc
        if engine is None:
            tags = [lang.language_tag for lang in langs]
            raise WindowsOcrUnavailable(
                f"系统没有可用的 OCR 识别语言（已装：{tags or '无'}）。"
                f"中文识别需要 Windows 装了 zh 的 OCR 能力。"
            )
        _engines.clear()          # 常驻单线程，正常只有一条；避免换线程后累积
        _engines[tid] = engine
        return engine

    def is_available(self) -> bool:
        try:
            self._on_ocr_thread(self._engine_here)
            return True
        except WindowsOcrUnavailable as exc:
            self._engine_error = str(exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self._engine_error = f"{type(exc).__name__}: {exc}"
            return False

    # ---------- 线程桥 ----------

    @staticmethod
    def _on_ocr_thread(fn: Any, *args: Any) -> Any:  # noqa: ANN401
        """
        把 `fn(*args)` 丢到**专属工作线程**上跑并等结果。

        整段 WinRT 工作（建引擎 / 建 SoftwareBitmap / RecognizeAsync / 读结果）都必须
        经这里，理由见模块 docstring ④。返回的是**纯 Python 数据**（`TextBlock`/`Rect`
        都是普通 dataclass），跨线程传没有 COM marshal 的问题。
        """
        fut = _get_pool().submit(fn, *args)
        try:
            return fut.result(timeout=_OCR_TIMEOUT_S)
        except _FuturesTimeout as exc:
            _reset_pool()
            raise WindowsOcrUnavailable(
                f"Windows OCR 超时(>{_OCR_TIMEOUT_S:.0f}s)，区域可能过大"
            ) from exc

    # ---------- 图像 → SoftwareBitmap（**只在工作线程上调用**）----------

    @staticmethod
    def _to_software_bitmap(img: Any) -> Any:  # noqa: ANN401
        """
        PIL 图 → `SoftwareBitmap`，**走内存 buffer 不落盘**。

        ⚠️ 4 个参数，不是 5 个：`CreateCopyFromBuffer` 在本版 PyWinRT 上只有
        `(buffer, format, width, height)` 这一个重载 —— 多传 `BitmapAlphaMode`
        会报 `TypeError: Invalid parameter count`（实测）。
        通道选 RGBA8：源图统一 `convert("RGBA")`，与格式一一对应，不做通道交换。
        """
        w = _require_winrt()
        rgba = img.convert("RGBA")
        data = rgba.tobytes()
        buf = w["Buffer"](len(data))
        buf.length = len(data)
        memoryview(buf)[:] = data
        return w["SoftwareBitmap"].create_copy_from_buffer(
            buf, w["BitmapPixelFormat"].RGBA8, rgba.width, rgba.height)

    # ---------- 识别 ----------

    def read_image(
        self, img: Any, origin: tuple[int, int], min_conf: float = 40.0,  # noqa: ARG002
    ) -> list[TextBlock]:
        """
        对**已经抓好的一张图**做 OCR，坐标以 `origin` 为图像左上角的屏幕位置。

        ⚠️ `min_conf` **在本平台上无效**（引擎不提供置信度）。参数保留只为签名一致；
        调用方要如实告诉模型它没生效 —— 见 `WindowsBackend.SUPPORTS_TEXT_CONFIDENCE`。
        这里刻意**不做**任何"假装过滤"（比如按块面积/长度启发式剔除）：
        那会让模型以为 `min_conf` 起了作用，比不过滤更糟。
        """
        if img is None:
            return []
        return self._on_ocr_thread(self._recognize_blocking, img, origin)

    def _recognize_blocking(self, img: Any, origin: tuple[int, int]) -> list[TextBlock]:  # noqa: ANN401
        """**整段 WinRT 工作**（引擎 → 位图 → 识别 → 转 TextBlock）。只在工作线程上跑。"""
        engine = self._engine_here()
        bitmap = self._to_software_bitmap(img)
        result = engine.recognize_async(bitmap).get()   # STA 之外才允许，见 docstring ④
        return self._blocks_from_result(result, origin)

    @staticmethod
    def _blocks_from_result(result: Any, origin: tuple[int, int]) -> list[TextBlock]:  # noqa: ANN401
        """
        `OcrResult` → `list[TextBlock]`，坐标换算成**屏幕绝对坐标**。

        实现逻辑（`_merge_runs` 是这里唯一有判断力的一步，理由见它的 docstring）：
          1. 逐行遍历 `result.lines`，行内逐词；
          2. 把「连续的单个 CJK 字符」合并成一个块（`_merge_runs`）；
          3. 每个块的矩形取所含词矩形的**并集** —— 合并出来的块的中心才是那段文字的
             中心，若只取第一个词的矩形，点下去会偏到第一个字上；
          4. 加上 `origin`：OCR 给的是**图内像素**，而调用方要的是屏幕绝对坐标
             （与 `grab_rgb` 返回的 origin 同一套语义）。
        """
        ox, oy = int(origin[0]), int(origin[1])
        blocks: list[TextBlock] = []
        try:
            lines = list(result.lines or [])
        except Exception:  # noqa: BLE001
            return blocks

        for line in lines:
            try:
                words = list(line.words or [])
            except Exception:  # noqa: BLE001
                continue
            for text, box in _merge_runs(words):
                if not text:
                    continue
                x, y, w, h = box
                blocks.append(TextBlock(
                    text=text,
                    rect=Rect(int(x) + ox, int(y) + oy, int(w), int(h)),
                    conf=_CONF_PLACEHOLDER,
                ))
        return blocks


def _merge_runs(words: list[Any]) -> list[tuple[str, tuple[float, float, float, float]]]:
    """
    词序列 → `[(文本, 并集矩形), …]`，把「连续的单个 CJK 字符」并成一块。

    ═══ 为什么必须有这一步 ═══

    Windows OCR 把中文**逐字**切成 word：`了解有关此图片的信息` 会变成 6 个各含一字的词，
    且行级 `text` 里也带空格（`'了 解 有 关 此 图'`）。

    若照搬 Linux/tesseract 的「一个词一个 TextBlock」，`get_screen_text` 会输出
    `[5] 了 @ (982,83)`、`[6] 解 @ (999,83)`… —— 模型**读不出「回收站」是一个词**，
    于是找不到该点哪儿。而这正是 `get_screen_text` 存在的唯一理由。

    ═══ 判据取「文本」而不是「几何间距」 ═══

    另一种写法是"两个词框挨得够近就合并"。**不采用**：那个阈值要靠实测调，
    中文标点的间距、不同字号都会让它飘；而"这个 word 是不是单个 CJK 字符"是
    **可从文本直接判定的事实**，不需要任何阈值。

    于是：连续的单个 CJK 字符 → 合成一块（`回收站`）；拉丁词各自成块
    （`Microsoft`、`Edge`）—— 与 tesseract 的行为一致，也和 Linux 侧的口径对齐。
    """
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    run_text: list[str] = []
    run_box: list[float] | None = None

    def flush() -> None:
        nonlocal run_text, run_box
        if run_text and run_box is not None:
            out.append(("".join(run_text), (run_box[0], run_box[1], run_box[2], run_box[3])))
        run_text, run_box = [], None

    for word in words:
        try:
            text = str(word.text or "")
            r = word.bounding_rect
            box = (float(r.x), float(r.y), float(r.width), float(r.height))
        except Exception:  # noqa: BLE001 —— 单个词读失败不该丢掉整行
            continue
        if _CJK_CHAR.match(text):
            run_text.append(text)
            run_box = _union(run_box, box)
            continue
        flush()
        out.append((_CJK_SPACE.sub("", text), box))
    flush()
    return out


def _union(a: list[float] | None, b: tuple[float, float, float, float],
           ) -> list[float]:
    """两个矩形的并集（`None` 表示还没有）。"""
    if a is None:
        return [b[0], b[1], b[2], b[3]]
    x1, y1 = min(a[0], b[0]), min(a[1], b[1])
    x2 = max(a[0] + a[2], b[0] + b[2])
    y2 = max(a[1] + a[3], b[1] + b[3])
    return [x1, y1, x2 - x1, y2 - y1]