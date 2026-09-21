"""
回归测试集：review 报告 v0.1.0 的 I-7 / I-8 / I-10 三条（2026-09-16）。

为什么单独一个文件：`test_optimizations.py` 已 2000+ 行，这三条又分属「窗口标题读取」
「屏幕坐标原点」「backend 抽象契约」三个互不相干的领域，集中放这里便于按条目号查找。
（顺带记录：`test_optimizations.py` 早已超过项目约定的 800 行上限，按模块拆分是独立的一件事，
不在这里顺手做——半拆比不拆更糟。）

每条失败信息都写清了「修复前会怎样」，因为这三条的共同点就是**修复前不会报错**：
    - I-7  批量取标题在实测的 xdotool 下恒失败，但代码有降级分支兜着，行为「看起来正常」；
    - I-8  本机两屏并集原点恰好是 (0,0)，所有坐标刚好对，换台机器才偏；
    - I-10 `hasattr` 哨兵让别的平台静默少做一件事，不报错、不失败。
所以测试必须**刻意构造出修复前会对的场景**（负原点、含换行的标题、不带 reader 的 backend），
否则它们只是把现状复述一遍。
"""

from __future__ import annotations

import subprocess

import pytest

from computer_use_mcp.backend.linux import grab as grab_mod
from computer_use_mcp.backend.linux.inject import XdotoolInjector
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import ComputerUseError

# tests/ 不是包（无 __init__.py），pytest 会把该目录加进 sys.path，故用平级绝对导入。
# 2026-09-18：原来的 test_optimizations.py 已按被测对象拆成多个文件，共享辅助（_StubBackend
# / _proc）搬到了本目录的 _helpers.py。
from _helpers import _StubBackend


# ========================================================================
# I-7：窗口标题必须逐个取，绝不用 `getwindowname wid1 wid2 …` 批量形式
# ========================================================================
def _patch_run(inj: XdotoolInjector, monkeypatch, titles: dict[str, str],
               calls: list[list[str]] | None = None) -> None:
    """
    替换 injector._run，**忠实模拟实测到的 xdotool 行为**。

    关键在批量形式：本机 xdotool 3.20160805.1 下 `getwindowname w1 w2` **只打印第一个
    窗口的标题**，其余当作未知命令（rc=1）。模拟时只吐首个窗的标题，才能让「修复前」
    真的走进那条会静默串位的分支——若模拟成「批量正常返回多行」，这条测试就测不到东西。
    """
    def fake(args, check=False, timeout=10.0):
        if calls is not None:
            calls.append(list(args))
        wids = args[1:]
        out = (titles.get(wids[0], "") + "\n") if wids else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(inj, "_run", fake)


def test_read_titles_never_batches_multiple_windows(monkeypatch):
    """
    回归（I-7）：多窗时必须逐窗取标题，不得再走批量形式。

    修复前的代价：批量形式既拿不到数据（白起一个进程），又让「行数 != 窗数即降级」这道
    防御**恒被触发**——即每次都要付 N+1 个进程（1 个必然失败的批量 + N 个逐窗）。
    调用方 wait_window 是**轮询**调用（0.25s 一次、最长 10s），所以这是持续开销。
    """
    inj = XdotoolInjector()
    calls: list[list[str]] = []
    _patch_run(inj, monkeypatch, {"11": "Alpha", "22": "Beta", "33": "Gamma"}, calls)

    assert inj._read_titles(["11", "22", "33"]) == ["Alpha", "Beta", "Gamma"]
    assert len(calls) == 3, f"三个窗口应起三次进程，实际：{calls}"
    assert all(len(c) == 2 for c in calls), f"不得出现批量形式（一次带多个 wid）：{calls}"


def test_read_titles_newline_in_first_title_cannot_shift_others(monkeypatch):
    """
    回归（I-7，关键）：首个窗标题含换行时，后续窗口**绝不能**被串位。

    这是修复前唯一能击穿那道防御的通道：批量形式只输出首个窗的标题，若它恰好含
    「窗数 − 1」个换行，行数就与窗数相撞 -> 判为「没对错位」并放行 -> 返回值变成
    ["第一行", "第二行"]，于是 22 号窗被安上前一个窗口标题的后半截，它自己的真实标题
    ("Beta") 永远看不到。这是**静默错答**，正是那个函数当初要防的事情本身。
    """
    inj = XdotoolInjector()
    _patch_run(inj, monkeypatch, {"11": "第一行\n第二行", "22": "Beta"})

    assert inj._read_titles(["11", "22"]) == ["第一行 第二行", "Beta"], \
        "首窗标题里的换行必须被压成空格留在本窗内，不得溢出成另一个窗口的标题"


# ========================================================================
# I-8：屏幕坐标必须带上「图像原点」，绝不默认 (0,0)
# ========================================================================
def _patch_mss(monkeypatch, monitors: list[dict]) -> None:
    """装一个假 mss：monitors 可控，grab 返回固定尺寸的 BGRA 数据。"""
    import sys
    import types

    class _Sct:
        def __init__(self) -> None:
            self.monitors = monitors

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, mon):
            return types.SimpleNamespace(size=(2, 2), bgra=bytes(2 * 2 * 4))

    fake = types.ModuleType("mss")
    fake.mss = lambda **kw: _Sct()
    monkeypatch.setitem(sys.modules, "mss", fake)


def test_grab_rgb_returns_union_origin_when_no_region(monkeypatch):
    """
    回归（I-8）：全屏抓取必须返回**显示器并集**的原点，而不是默认 (0,0)。

    并集原点不保证是 (0,0)：副屏位于主屏左侧/上方时它是负值。必须用**负原点**来测——
    本机两屏并集原点恰好是 (0,0)，用 (0,0) 测等于什么都没验证（这正是该问题至今潜伏、
    现有测试全绿的原因）。
    """
    _patch_mss(monkeypatch, [{"left": -1920, "top": 0, "width": 3840, "height": 1080}])

    img, origin = grab_mod.grab_rgb()

    assert origin == (-1920, 0), f"应返回并集原点，实际 {origin}"
    assert img.size == (2, 2)


def test_grab_rgb_region_origin_is_the_region(monkeypatch):
    """指定 region 时，原点就是该 region 的左上角（两条分支都从实际使用的矩形取原点）。"""
    _patch_mss(monkeypatch, [{"left": -1920, "top": 0, "width": 3840, "height": 1080}])

    _img, origin = grab_mod.grab_rgb((100, 50, 10, 10))

    assert origin == (100, 50), f"指定 region 时原点应等于 region 左上角，实际 {origin}"


def test_ocr_offsets_by_grab_origin_not_zero(monkeypatch):
    """
    回归（I-8，关键）：**未指定 region**（全屏抓取）时，OCR 坐标必须加上抓屏返回的原点。

    修复前写的是 `off_x = region[0] if region else 0`，等于把「图像原点 = 屏幕原点」当
    默认值。副屏在左/上时并集原点是负的，于是所有文字坐标整体偏一个屏宽，模型按它点击
    **必定点空**——而 OCR 通道的立身之本恰是「坐标是算出来的不是猜的」。
    """
    from PIL import Image

    from computer_use_mcp.backend.linux import ocr as ocr_mod

    img = Image.new("RGB", (32, 32), (255, 255, 255))
    monkeypatch.setattr(ocr_mod, "grab_rgb", lambda region=None: (img, (-1920, 0)))

    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
           "left\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t4\t6\t10\t8\t90.0\t确定\n")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, tsv.encode("utf-8"), b"")

    monkeypatch.setattr(ocr_mod.subprocess, "run", fake_run)

    blocks = ocr_mod.OcrReader().read()

    assert blocks, "应至少识别出一个文本块"
    # _UPSCALE=2：识别坐标 4/6 先除以 2 得 2/3，再加原点 -1920 -> x = -1918
    assert (blocks[0].rect.x, blocks[0].rect.y) == (-1918, 3), \
        f"OCR 坐标必须叠加抓屏原点，实际 {blocks[0].rect}"


def test_screenshot_meta_carries_origin(monkeypatch):
    """
    回归（I-8）：截图 meta 必须带 origin。

    模型是按「屏幕绝对坐标 = origin + 图上坐标 x scale」换算落点的，而 tools 的提示语
    也正是这么承诺的。缺了 origin，全屏截图在并集原点非零时换算全错——而模型无从察觉。
    """
    from PIL import Image

    from computer_use_mcp.backend.linux import backend as be_mod

    img = Image.new("RGB", (40, 20), (0, 0, 0))
    monkeypatch.setattr(be_mod.grab, "grab_rgb", lambda region=None: (img, (-1920, 0)))

    _data, meta = be_mod.LinuxBackend().screenshot(max_side=20)

    assert meta["origin"] == [-1920, 0], f"meta 必须带图像原点，实际 {meta}"
    assert meta["resized"] == [20, 10] and meta["scale"] == 2.0


# ========================================================================
# I-10：is_alive / focus 必须走 ABC，不得 hasattr 摸 backend.reader
# ========================================================================
def test_backend_abc_declares_is_alive_and_focus():
    """
    回归（I-10）：这两项能力必须登记在 ABC 上。

    登记的意义不是「多两个方法」，而是让**别的平台**必须对它们表态：漏登记时上层那两个
    `hasattr(backend, "reader")` 哨兵会让 Windows backend 静默少做聚焦、静默退化存活判定，
    且**没有任何测试会失败**。做成抽象方法后，漏实现直接 ImportError/TypeError。
    """
    from computer_use_mcp.backend.base import Backend

    missing = {"is_alive", "focus"} - set(Backend.__abstractmethods__)
    assert not missing, f"这些能力必须登记为抽象方法，否则别的平台会静默失去它们：{missing}"


def test_native_alive_delegates_to_abc(monkeypatch):
    """回归（I-10）：ref 存活判定走 backend.is_alive，不再摸 backend.reader。"""
    backend = _StubBackend()
    assert not hasattr(backend, "reader"), "本测试的前提：桩不得有 reader 属性"
    coord = Coordinator(backend=backend)
    seen: list = []
    backend.is_alive = lambda n: (seen.append(n), True)[1]

    assert coord._native_alive(object()) is True
    assert len(seen) == 1, "必须经 ABC 判定，而不是绕过它去摸实现细节"


def test_type_text_focuses_through_abc_without_reader_attribute(monkeypatch):
    """
    回归（I-10，关键）：`type_text(ref=...)` 的「先聚焦」必须经 ABC 完成。

    修复前的写法是 `if hasattr(backend, "reader"): backend.reader.focus(native)`。
    这里刻意用一个**不带 reader 属性**的 backend：修复前它拿不到这一步聚焦，修复后照样
    会调 `backend.focus`。聚焦是元素级赋值能成功的前提，静默丢掉它表现为「赋值偶发失败」，
    在真实桌面上极难反查到是少了 focus。
    """
    backend = _StubBackend()
    assert not hasattr(backend, "reader"), "本测试的前提：桩不得有 reader 属性"
    backend.is_alive = lambda n: True
    coord = Coordinator(backend=backend)
    native = object()
    ref = coord.refs.register(native, role="text", name="输入框", app="gedit")

    coord.type_text("hi", ref=ref)

    assert backend.focused == [native], \
        "必须经 ABC 聚焦，且作用在同一个 native 上（不能是重定位到的别的元素）"


def test_stub_focus_is_not_polluted_into_calls():
    """桩的 focus 单独记账：塞进 calls 会打乱既有「第 N 个调用是什么」类断言。"""
    backend = _StubBackend()
    backend.focus(object())
    assert backend.focused and backend.calls == []


def test_zero_size_region_still_raises_after_origin_change():
    """I-8 改了 grab_rgb 的返回值，但错误契约（I-5）不能被顺带破坏。"""
    with pytest.raises(ComputerUseError) as ei:
        grab_mod.grab_rgb((10, 10, 0, 0))
    assert "尺寸非法" in str(ei.value)