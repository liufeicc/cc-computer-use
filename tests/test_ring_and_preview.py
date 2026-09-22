# -*- coding: utf-8 -*-
"""
点击预览图（准星小图）与光圈 —— Step 1 / Step 4 的回归集。

本文件的共同契约（改这块前先读）：
  1. **预览图绝不让主操作失败**：抓图出错只是少一张图，点击必须照常执行；
  2. **准星像素必须用「裁剪后的原点」反算**（`px = x - origin[0]`）：`_clamp_region`
     在屏幕边缘会改变原点，写死半宽 = 越靠边缘的按钮准星指得越离谱 —— 而这个功能
     存在的唯一理由就是「位置对不对」，指错就是主动误导；
  3. **bytes 绝不进 `ActionResult.data`**：`to_text()` 会把 data 逐项拼进模型上下文，
     JPEG 字节进去就是几十 KB 的 `b'\\xff\\xd8...'`；
  4. tools 层 `click` 的返回注解必须是 `Any`：注解为 `str` 时 mcp 2.x 会生成
     outputSchema 并校验，而带图那次返回的是 content block 列表 → ValidationError。
"""

from __future__ import annotations

import os
import time
import sys

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.backend.base import Backend, ClickPreview, Rect, TextBlock  # noqa: E402
from computer_use_mcp.backend.linux import backend as be_mod  # noqa: E402
from computer_use_mcp.backend.linux import grab as grab_mod  # noqa: E402
from computer_use_mcp.core import display as display_mod  # noqa: E402
from computer_use_mcp.core.coordinator import Coordinator  # noqa: E402
from computer_use_mcp.utils import blocks as blocks_mod  # noqa: E402
from computer_use_mcp.utils import temps  # noqa: E402
from computer_use_mcp.utils.errors import ActionResult  # noqa: E402

from _helpers import _StubBackend  # noqa: E402


class _FakeMCP:
    """最小 MCP 桩：`@mcp.tool(...)` 原样返回函数，并把注册结果收起来供调用。"""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, name=None, description=None):
        def deco(fn):
            self.tools[name] = fn
            return fn
        return deco


def _coord(monkeypatch, backend=None) -> Coordinator:
    """real 模式下的 coordinator（单测不启沙箱，见 conftest）。"""
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    return Coordinator(backend=backend or _StubBackend())


def _click_tool(monkeypatch, coord):
    from computer_use_mcp.tools import action as action_mod

    mcp = _FakeMCP()
    action_mod.register(mcp, coord)
    return mcp.tools["click"]


# ==================== ABC 契约 ====================

def test_click_preview_is_optional_not_abstract():
    """
    `click_preview` 必须是**非抽象**方法：做成 @abstractmethod 会让 `_StubBackend` 等
    测试替身与未实现的平台 backend 当场 `TypeError: Can't instantiate abstract class`
    （几十个用例全红）；而「Linux 实现被悄悄删成默认 no-op」这一 I-10 式风险由本文件
    的另一条用例（实现必须覆盖默认）兜住，不靠把方法抽象化。
    """
    assert "click_preview" not in Backend.__abstractmethods__, sorted(Backend.__abstractmethods__)
    assert not getattr(Backend.click_preview, "__isabstractmethod__", False), \
        "click_preview 不能是抽象方法（否则测试替身与未实现平台无法实例化）"


def test_linux_backend_actually_implements_click_preview():
    """Linux 实现必须**真的覆盖**默认 no-op，否则能力静默消失（全绿但没图）。"""
    assert be_mod.LinuxBackend.click_preview is not Backend.click_preview
    # 同族的两个可选能力也一样：被悄悄删成默认 no-op 时，评估/变化对比会无声消失
    assert be_mod.LinuxBackend.read_text_from_image is not Backend.read_text_from_image
    assert be_mod.LinuxBackend.change_fraction_since is not Backend.change_fraction_since


def test_build_sh_collects_xlib():
    """
    打包必须收进 Xlib 的**动态**子模块：`Xlib.ext.shape` 不出现在任何静态 import 图里
    （由 `Xlib.display` 按扩展名动态导入），漏了的表现是「开发态一切正常、**只有冻结产物**
    里没有光圈」——正是最该用结构性守卫挡住的那类漏网。
    """
    import pathlib

    build = (pathlib.Path(__file__).resolve().parent.parent / "build.sh").read_text(
        encoding="utf-8")
    assert "--collect-submodules Xlib" in build, "build.sh 必须收进 Xlib 的子模块"
    assert "--hidden-import Xlib.ext.shape" in build, "shape 扩展是动态导入，必须显式钉死"


# ==================== 准星几何（最高危的一行）====================

def test_crosshair_uses_clamped_origin_not_half_width(monkeypatch):
    """
    ★ 屏边场景：请求 region 的左上是 (-240,-150)，而实际裁剪后 origin 是 (0,0)。

    正确算法给出 px = x - origin[0] = 0；历史最可能写错的形式是写死半宽（240）——
    那样准星会落在图中央，**指着一个完全不是落点的位置**。这条用例就是钉死这个差别。
    """
    got: dict = {}
    drawn_on: dict = {}

    monkeypatch.setattr(grab_mod, "draw_crosshair",
                        lambda img, px, py: (got.update(px=px, py=py), drawn_on.update(img=img)))
    monkeypatch.setattr(grab_mod, "grab_rgb",
                        lambda region=None: (Image.new("RGB", (240, 150)), (0, 0)))

    shot = be_mod.LinuxBackend().click_preview(0, 0)
    assert shot is not None

    assert (got["px"], got["py"]) == (0, 0), f"准星必须用裁剪后原点反算，实际 {got}"
    assert shot.meta["crosshair"] == [0, 0]
    assert shot.meta["origin"] == [0, 0]
    assert shot.meta["scale"] == 1 and shot.meta["kind"] == "click_preview"
    assert shot.data[:2] == b"\xff\xd8", "必须是 JPEG（快且小）"
    # 准星必须画在**副本**上：原始图随后要喂 OCR 与点前后对比，被红线污染就失去价值
    assert drawn_on["img"] is not shot.image, "准星画到了原始图上（后续 OCR/对比会被污染）"
    assert shot.image is not None and shot.image.size == (240, 150)


def test_crosshair_offsets_by_origin_in_the_middle_of_the_screen(monkeypatch):
    """屏中央（无裁剪）时：px 就是请求的半宽，仍须由 origin 推出而非写死。"""
    got: dict = {}
    monkeypatch.setattr(grab_mod, "draw_crosshair",
                        lambda img, px, py: got.update(px=px, py=py))
    monkeypatch.setattr(grab_mod, "grab_rgb",
                        lambda region=None: (Image.new("RGB", (480, 300)), (260, 400)))

    shot = be_mod.LinuxBackend().click_preview(500, 550)

    assert shot is not None
    assert (got["px"], got["py"]) == (240, 150), got
    assert shot.meta["region"] == [260, 400, 480, 300]
    assert shot.meta["origin"] == [260, 400]


def test_click_preview_returns_none_when_point_outside_crop(monkeypatch):
    """落点不在返回图内时**宁可不发图**，也不发一张指错位置的图。"""
    monkeypatch.setattr(grab_mod, "grab_rgb",
                        lambda region=None: (Image.new("RGB", (10, 10)), (5000, 5000)))
    assert be_mod.LinuxBackend().click_preview(0, 0) is None


def test_click_preview_never_raises(monkeypatch):
    """抓屏/编码任何环节出错都只返回 None（附加证据不得变成新的失败点）。"""
    def boom(region=None):
        raise RuntimeError("屏幕没了")

    monkeypatch.setattr(grab_mod, "grab_rgb", boom)
    assert be_mod.LinuxBackend().click_preview(10, 20) is None


# ==================== bytes 不进上下文 ====================

def test_preview_bytes_never_enter_action_result_data():
    """`to_text()` 是给模型看的：图像字节只能待在 `preview` 字段里，不能进 `data`。"""
    r = ActionResult(ok=True, level="coord", message="已点击",
                     data={"x": 1, "y": 2},
                     preview=(b"\xff\xd8" + b"x" * 5000, {"format": "jpeg"}))
    text = r.to_text()
    assert "preview" not in text and "\xff" not in text
    assert len(text) < 300, f"文本里混进了图像字节：{text[:200]}"


# ==================== 开关与降级 ====================

def test_preview_flag_priority_param_env_default(monkeypatch):
    """
    优先级：工具参数 > 环境变量 > 默认开。

    留意语义差别——**参数决定这次抓不抓**（preview=False 时连抓都不抓，零成本），
    环境变量是全局兜底（CC_CU_CLICK_PREVIEW=0 整体关掉）。
    """
    seen: list = []
    backend = _StubBackend()
    backend.click_preview = lambda x, y: (seen.append((x, y)), None)[1]
    coord = _coord(monkeypatch, backend)

    monkeypatch.delenv("CC_CU_CLICK_PREVIEW", raising=False)
    coord.click_xy(1, 2)
    assert seen == [(1, 2)], "默认应当抓图"

    seen.clear()
    coord.click_xy(1, 2, preview=False)
    assert seen == [], "显式 preview=False 时不该抓"

    monkeypatch.setenv("CC_CU_CLICK_PREVIEW", "0")
    coord.click_xy(1, 2)
    assert seen == [], "环境变量关闭时不抓"

    coord.click_xy(1, 2, preview=True)
    assert seen == [(1, 2)], "工具参数优先于环境变量"


def test_preview_failure_never_breaks_click(monkeypatch):
    """取图抛异常时，点击必须照常执行且结果 ok（与落点证据同一条纪律）。"""
    backend = _StubBackend()

    def boom(x, y):
        raise RuntimeError("X 连接断了")

    backend.click_preview = boom
    coord = _coord(monkeypatch, backend)

    r = coord.click_xy(10, 20)
    assert r.ok and r.preview is None, r.message
    assert backend.calls[-1] == ("click", 10, 20), "点击本身必须照常执行"


def test_element_level_click_has_no_preview(monkeypatch):
    """元素级 do_action 零坐标，本就不存在「瞄到哪」——不该抓图（也不该多一次后台开销）。"""
    seen: list = []
    backend = _StubBackend()
    backend.invoke = lambda n, action=None: True
    backend.click_preview = lambda x, y: (seen.append((x, y)), None)[1]
    coord = _coord(monkeypatch, backend)

    blk = TextBlock(text="保存", rect=Rect(100, 100, 30, 20), conf=90)
    ref = coord.refs.register(blk, role="text-block", name="保存", app="ocr")
    r = coord.click(ref=ref)

    assert r.ok and r.level == "element", r.message
    assert r.preview is None and seen == [], "元素级不该抓预览图"


def test_coord_fallback_click_also_captures_preview(monkeypatch):
    """元素级失败 → 坐标兜底那条路**要**抓图（误差模式是校准漂移，图是唯一直接证据）。"""
    seen: list = []
    backend = _StubBackend()
    backend.invoke = lambda n, action=None: False
    backend.element_screen_rect = lambda n: Rect(200, 200, 40, 20)
    backend.click_preview = lambda x, y: (
        seen.append((x, y)),
        ClickPreview(data=b"\xff\xd8jpeg", meta={"format": "jpeg"}, image=object()),
    )[1]
    coord = _coord(monkeypatch, backend)

    blk = TextBlock(text="保存", rect=Rect(200, 200, 40, 20), conf=90)
    ref = coord.refs.register(blk, role="text-block", name="保存", app="ocr")
    r = coord.click(ref=ref)

    assert r.ok and r.level == "coord", r.message
    assert seen == [(220, 210)], f"坐标兜底要抓图（元素中心），实际 {seen}"
    assert r.preview is not None


# ==================== tools 层 ====================

def test_click_tool_return_annotation_is_not_str():
    """
    `-> str` 会让 mcp 2.x 生成 outputSchema 并对返回做 pydantic 校验；带预览图的那次
    返回 content block 列表 → ValidationError，且**只在带图时**发生（平时全绿），
    是最难反查的一类崩法。这里把注解钉死。
    """
    coord = Coordinator(backend=_StubBackend())
    from computer_use_mcp.tools import action as action_mod

    mcp = _FakeMCP()
    action_mod.register(mcp, coord)
    ann = mcp.tools["click"].__annotations__.get("return")
    assert str(ann) in ("typing.Any", "Any"), f"click 返回注解应为 Any，实际 {ann}"


def test_click_tool_returns_text_only_when_no_preview(monkeypatch):
    """没有图时仍返回纯字符串（保持既有语义与断言不变）。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: None
    coord = _coord(monkeypatch, backend)
    click = _click_tool(monkeypatch, coord)

    out = click(x=10, y=20, preview=True)
    assert isinstance(out, str) and "坐标级点击已执行" in out, out


def test_click_tool_appends_image_and_meta_blocks(monkeypatch):
    """有图时返回 [文本, MCPImage, meta 文本]：**文本在前**，meta 必须说明十字的含义。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: ClickPreview(
        data=b"\xff\xd8fake-jpeg",
        meta={"kind": "click_preview", "format": "jpeg", "x": x, "y": y,
              "origin": [0, 0], "crosshair": [240, 150], "scale": 1, "width": 480, "height": 300},
        image=object())
    coord = _coord(monkeypatch, backend)
    click = _click_tool(monkeypatch, coord)

    out = click(x=10, y=20, preview=True)

    if blocks_mod.MCPImage is None:      # 环境拿不到 Image 类型 → 退化落盘路径
        assert isinstance(out, str) and "cc-cu-click-" in out, out
        return
    assert isinstance(out, list) and len(out) == 3, out
    assert isinstance(out[0], str) and "坐标级点击已执行" in out[0]
    meta_text = out[2]
    assert "红十字中心" in meta_text and "屏幕 1 像素" in meta_text, meta_text
    assert "scale" in meta_text and "origin" in meta_text, meta_text


def test_click_tool_falls_back_to_disk_without_image_type(monkeypatch, tmp_path):
    """拿不到 MCP Image 类型时退化落盘，家族名 `cc-cu-click`（有 pid 归属 + LRU 回收）。"""
    monkeypatch.setattr(blocks_mod, "MCPImage", None)
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    backend = _StubBackend()
    backend.click_preview = lambda x, y: ClickPreview(
        data=b"\xff\xd8fake", meta={"kind": "click_preview", "format": "jpeg", "x": x, "y": y},
        image=object())
    coord = _coord(monkeypatch, backend)
    click = _click_tool(monkeypatch, coord)

    out = click(x=10, y=20)

    assert isinstance(out, str), out
    assert "cc-cu-click-" in out, out
    assert list(tmp_path.glob("cc-cu-click-*")), "退化路径必须真的落盘"


# ==================== 点击评估：程序算偏差 ====================

def _blk(text, x, y, w, h, conf=90.0):
    return TextBlock(text=text, rect=Rect(x, y, w, h), conf=conf)


def test_assess_aim_landing_text_and_nearest_offset():
    """
    纯函数判据（合成文本块，"保存" 框 (500,240)-(560,276) 中心 (530,258)）：
      - 落点 (520,300) 在框外 → 未命中，dx = 520-530 = -10（偏左），dy = 300-258 = +42（偏下）；
      - 落点若落在某块**内部** → 该块就是落点文字（取面积最小者 = 最具体的那块）。
    """
    blocks = [_blk("取消", 400, 250, 52, 22), _blk("保存", 500, 240, 60, 36),
              _blk("帮助", 600, 248, 50, 22)]

    a = Coordinator._assess_aim(520, 300, blocks, None)
    assert a["landing_text"] is None, "落点在所有文字块之外"
    assert a["nearest"]["text"] == "保存" and a["nearest"]["center"] == [530, 258]
    assert (a["nearest"]["dx"], a["nearest"]["dy"]) == (-10, 42)
    assert a["nearest"]["dist"] == int(round((10 ** 2 + 42 ** 2) ** 0.5))

    a2 = Coordinator._assess_aim(510, 258, blocks, None)
    assert a2["landing_text"] == "保存" and a2["landing_conf"] == 90.0

    # 远处无关文字不该被指认为「最近候选」（阈值防误导）
    a3 = Coordinator._assess_aim(3000, 3000, blocks, None)
    assert a3["nearest"] is None


def test_assess_aim_expect_hit_miss_and_not_found():
    """`expect` 把启发式变成**精确偏差 + 可直接采用的建议坐标**。"""
    blocks = [_blk("取消", 400, 250, 52, 22), _blk("保存", 500, 240, 60, 36)]

    hit = Coordinator._assess_aim(530, 258, blocks, "保存")["expect"]
    assert hit["found"] and hit["hit"], hit          # 落点在该块内 = 命中

    miss = Coordinator._assess_aim(520, 300, blocks, "保存")["expect"]
    assert miss["found"] and not miss["hit"], miss
    assert (miss["dx"], miss["dy"]) == (-10, 42)
    assert miss["suggest"] == [530, 258], "建议坐标必须是该块中心（可直接照抄去点）"

    absent = Coordinator._assess_aim(520, 300, blocks, "提交")["expect"]
    assert absent["found"] is False and absent["query"] == "提交", absent

    # 多个块命中同一关键词时取**最近**的那一个
    dup = [_blk("确定", 100, 100, 40, 20), _blk("确定", 520, 296, 40, 20)]
    near = Coordinator._assess_aim(520, 300, dup, "确定")["expect"]
    assert near["suggest"] == [540, 306], near


def test_format_aim_reports_suggestion_and_facts_only():
    blocks = [_blk("保存", 500, 240, 60, 36)]
    text = Coordinator._format_aim(
        Coordinator._assess_aim(520, 300, blocks, "保存"))
    assert "未命中" in text and "偏差 (-10,+42)" in text and "建议改点其中心 (530,258)" in text, text
    assert Coordinator._format_aim({}) == ""


def test_format_change_three_buckets_and_none():
    f = Coordinator._format_change
    assert f(None) == ""
    assert "无（0.3%" in f(0.003)
    assert "轻微（2.0%" in f(0.02)
    assert "明显（12.0%" in f(0.12)
    assert "作参考不作断言" in f(0.02), "措辞必须只报事实（慢界面可能滞后）"


def _shot_with_image(img=None):
    return ClickPreview(data=b"\xff\xd8x", meta={"kind": "click_preview", "format": "jpeg",
                                                 "origin": [0, 0], "region": [0, 0, 480, 300]},
                        image=img if img is not None else object())


def test_click_xy_prints_deviation_and_change(monkeypatch):
    """
    接线的总体判据：消息里要同时出现「期望…未命中…建议改点其中心」与「点后界面变化」，
    且**变化对比发生在 settle 之后**（否则读到的是变化前的屏幕，等于白比一次）。
    """
    order: list[str] = []
    backend = _StubBackend()
    sentinel_img = object()
    backend.click_preview = lambda x, y: _shot_with_image(sentinel_img)
    blocks = [_blk("保存", 500, 240, 60, 36)]

    def fake_ocr(img, origin, min_conf=40.0):
        order.append("ocr")
        assert img is sentinel_img, "OCR 必须用**原始图**（未画准星那张）"
        return blocks

    backend.read_text_from_image = fake_ocr

    def fake_change(region, before):
        order.append("change")
        assert before is sentinel_img, "变化对比必须拿原始图当参照（准星会被误当变化）"
        return 0.003

    backend.change_fraction_since = fake_change
    coord = _coord(monkeypatch, backend)

    seen_settle: list[float] = []
    orig_landing = coord._landing

    def spy_landing(settle: float = 0.0):
        seen_settle.append(settle)
        return orig_landing(settle)

    monkeypatch.setattr(coord, "_landing", spy_landing)

    r = coord.click_xy(520, 300, expect="保存")

    assert r.ok, r.message
    assert "期望「保存」：未命中" in r.message and "建议改点其中心 (530,258)" in r.message, r.message
    assert "点后界面变化：无（0.3%" in r.message, r.message
    assert order == ["ocr", "change"], order
    assert seen_settle[-1] == Coordinator._INJECT_SETTLE, "变化对比必须在 settle 之后"


def test_click_xy_aim_failure_never_breaks_click(monkeypatch):
    """评估链任何一环失败都只是「少一段文字」，点击必须照常成功。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: _shot_with_image()

    def boom(*a, **k):
        raise RuntimeError("tesseract 挂了")

    backend.read_text_from_image = boom
    backend.change_fraction_since = boom
    coord = _coord(monkeypatch, backend)

    r = coord.click_xy(10, 20, expect="保存")
    assert r.ok and "坐标级点击已执行" in r.message, r.message
    assert backend.calls[-1] == ("click", 10, 20), "点击本身必须照常执行"


def test_click_xy_without_preview_skips_ocr_and_change(monkeypatch):
    """preview=False（连抓都不抓）时：不做 OCR、不做变化对比，退回旧的落点文字通道。"""
    seen: list[str] = []
    backend = _StubBackend()
    backend.click_preview = lambda x, y: None
    backend.read_text_from_image = lambda *a, **k: seen.append("ocr") or []
    backend.change_fraction_since = lambda *a, **k: seen.append("change") or 0.0
    coord = _coord(monkeypatch, backend)

    r = coord.click_xy(10, 20, preview=False)

    assert r.ok and r.preview is None, r.message
    assert seen == [], seen
    # 没有裁剪图时，落点文字退回 describe_point(with_text=True)（历史路径）
    assert ("describe_point", 10, 20, True) in backend.calls, backend.calls


# ==================== 回看通道 ====================

@pytest.fixture(autouse=True)
def _clear_click_log():
    """回看缓存是**模块级**的（进程内单例）：用例之间必须清干净，否则相互串味。"""
    from computer_use_mcp.core.coordinator import landing as landing_mod

    def _clear():
        with landing_mod._CLICK_LOG_LOCK:
            landing_mod._CLICK_LOG.clear()

    _clear()
    yield
    _clear()


def test_review_empty_cache_says_so(monkeypatch):
    """没有记录时**明确说明**，不静默返回空（模型的下一步判断依赖这句话）。"""
    coord = _coord(monkeypatch)
    r = coord.get_last_click_image()
    assert not r.ok and "尚无坐标点击记录" in r.message, r.message
    assert r.data["count"] == 0


def test_review_returns_last_and_previous_clicks(monkeypatch):
    """index=-1 取最近一次、-2 取上上次；越界时报告总条数。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: _shot_with_image()
    coord = _coord(monkeypatch, backend)

    coord.click_xy(100, 200)
    coord.click_xy(300, 400)

    last = coord.get_last_click_image()
    assert last.ok and (last.data["x"], last.data["y"]) == (300, 400), last.message
    assert last.data["count"] == 2 and last.data["has_preview"] is True
    assert last.preview is not None and last.preview[1]["kind"] == "click_preview"

    prev = coord.get_last_click_image(index=-2)
    assert (prev.data["x"], prev.data["y"]) == (100, 200), prev.message

    over = coord.get_last_click_image(index=-9)
    assert not over.ok and "索引越界" in over.message and "共 2 条" in over.message, over.message


def test_review_keeps_records_without_image(monkeypatch):
    """preview=false 的那次也要留记录（只是没图）——否则模型会以为「点了但记录凭空消失」。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: None
    coord = _coord(monkeypatch, backend)

    coord.click_xy(50, 60, preview=False)
    r = coord.get_last_click_image()

    assert r.ok and r.preview is None, r.message
    assert r.data["has_preview"] is False, r.data
    assert (r.data["x"], r.data["y"]) == (50, 60)


def test_sequence_clicks_are_recorded_but_not_attached(monkeypatch):
    """
    `act_sequence` 里的坐标点击**只记录、不附图**：序列中间每张图模型都无处反应
    （逐张 attach 是 token 反模式），但回看通道照样能取到——两者解耦正是这么设计的。
    """
    backend = _StubBackend()
    backend.click_preview = lambda x, y: _shot_with_image()
    coord = _coord(monkeypatch, backend)

    res = coord.act_sequence([{"op": "click", "x": 11, "y": 22}], stop_on_error=True)

    assert "_images" not in res or not res["_images"], "序列不该把预览图塞进响应"
    r = coord.get_last_click_image()
    assert r.ok and (r.data["x"], r.data["y"]) == (11, 22), r.message
    assert r.preview is not None, "序列点击也必须留下可回看的图"


def test_review_tool_returns_image_blocks(monkeypatch):
    """tools 层的回看工具：有图 → [文本, MCPImage, meta]；meta 要说明十字的含义。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: _shot_with_image()
    coord = _coord(monkeypatch, backend)
    coord.click_xy(7, 8)

    from computer_use_mcp.tools import action as action_mod
    mcp = _FakeMCP()
    action_mod.register(mcp, coord)
    out = mcp.tools["get_last_click_image"]()

    if blocks_mod.MCPImage is None:
        assert isinstance(out, str) and "cc-cu-click-" in out, out
        return
    assert isinstance(out, list) and len(out) == 3, out
    assert "最近一次" in out[0] or "坐标点击" in out[0], out[0]
    assert "红十字中心" in out[2], out[2]


def test_review_tool_without_image_mentions_reason(monkeypatch):
    """有记录但没图时，回看工具必须说清原因（那次 preview=false 或抓图失败）。"""
    backend = _StubBackend()
    backend.click_preview = lambda x, y: None
    coord = _coord(monkeypatch, backend)
    coord.click_xy(1, 2, preview=False)

    from computer_use_mcp.tools import action as action_mod
    mcp = _FakeMCP()
    action_mod.register(mcp, coord)
    out = mcp.tools["get_last_click_image"]()

    assert isinstance(out, str), out
    assert "未保存预览图" in out, out


# ==================== 光圈（给人看的那个圈）====================

@pytest.fixture(autouse=True)
def _reset_ring_state():
    """光圈状态是模块级的（进程内单例）：用例之间必须复位，否则相互串味。"""
    from computer_use_mcp.backend.linux import ring as ring_mod

    def _reset():
        ring_mod._FAILS.clear()
        ring_mod._DISABLED.clear()
        ring_mod._SHAPE_OK.clear()
        ring_mod._WINDOWS = []
        ring_mod._set_visible(False)
        while not ring_mod._QUEUE.empty():
            try:
                ring_mod._QUEUE.get_nowait()
            except Exception:  # noqa: BLE001
                break

    _reset()
    yield
    _reset()


def test_ring_queue_full_never_blocks_or_raises(monkeypatch):
    """队列满时**丢弃并返回**：宁可看不见圈，也绝不能让点击变慢或失败。"""
    from computer_use_mcp.backend.linux import ring as ring_mod

    monkeypatch.setattr(ring_mod, "_ensure_worker", lambda: None)
    monkeypatch.setattr(display_mod, "effective_display", lambda: ":99")
    for _ in range(ring_mod._QUEUE.maxsize):     # 先把队列塞满
        ring_mod._QUEUE.put_nowait(("noop",))

    t0 = time.monotonic()
    ring_mod.show(10, 20)                        # 不该抛、不该等
    assert time.monotonic() - t0 < 0.2


def test_ring_resolves_display_in_calling_thread(monkeypatch):
    """
    ★ 目标屏必须在**调用线程**解析并入队。

    若改成「worker 稍后再查 effective_display()」，沙箱在这期间重建/消失时圈会画到
    **宿主屏**上，而调用方毫无察觉——这正是本项目最警惕的「静默跨屏」失效。
    """
    from computer_use_mcp.backend.linux import ring as ring_mod

    sentinel = ":12345"
    monkeypatch.setattr(ring_mod, "_ensure_worker", lambda: None)
    monkeypatch.setattr(display_mod, "effective_display", lambda: sentinel)

    ring_mod.show(7, 8, ttl=0.9)

    item = ring_mod._QUEUE.get_nowait()
    assert item[0] == "show" and item[1] == sentinel, item
    assert (item[2], item[3]) == (7, 8) and item[4] == 0.9


def test_ring_env_switch(monkeypatch):
    """CC_CU_CLICK_RING=0 时不入队（连尝试都不尝试）。"""
    from computer_use_mcp.backend.linux import ring as ring_mod

    monkeypatch.setattr(ring_mod, "_ensure_worker", lambda: None)
    monkeypatch.setattr(display_mod, "effective_display", lambda: ":99")
    monkeypatch.setenv("CC_CU_CLICK_RING", "0")

    ring_mod.show(1, 2)
    assert ring_mod._QUEUE.empty()
    assert ring_mod.enabled() is False


def test_ring_fuse_is_per_display(monkeypatch):
    """
    熔断**按屏记账**（连续 2 次失败才停该屏）：沙箱屏号会变，全局熔断会被一次早已无关的
    故障永久锁死；而「第一次失败就永久关掉」属于静默能力消失，本项目最忌讳的失效形态。
    """
    from computer_use_mcp.backend.linux import ring as ring_mod

    def boom(disp):
        raise RuntimeError("X 连接失败")

    monkeypatch.setattr(ring_mod, "_ensure_conn", boom)

    ring_mod._draw(":99", 1, 1)
    assert ":99" not in ring_mod._DISABLED and ring_mod._FAILS[":99"] == 1

    ring_mod._draw(":99", 1, 1)
    assert ":99" in ring_mod._DISABLED, "连续两次失败后应停掉该屏"

    # 另一块屏不受影响（这正是「按屏记账」的意义）
    ring_mod._draw(":100", 1, 1)
    assert ":100" not in ring_mod._DISABLED and ring_mod._FAILS[":100"] == 1


def test_ring_show_never_raises_even_if_display_lookup_fails(monkeypatch):
    """取屏就出错（display 未起、模块异常）时也只是静默放弃，绝不能影响点击。"""
    from computer_use_mcp.backend.linux import ring as ring_mod

    def boom():
        raise RuntimeError("display 崩了")

    monkeypatch.setattr(display_mod, "effective_display", boom)
    ring_mod.show(1, 2)          # 不抛即通过


def test_ring_clear_now_is_free_when_nothing_visible(monkeypatch):
    """没圈时 clear_now 只是一次布尔判断（截图前会调它，不能有额外开销）。"""
    from computer_use_mcp.backend.linux import ring as ring_mod

    ring_mod._set_visible(False)
    t0 = time.monotonic()
    ring_mod.clear_now()
    assert time.monotonic() - t0 < 0.05
    assert ring_mod._QUEUE.empty(), "没圈时不该往队列里投递任何东西"


def test_ring_drawn_before_injection(monkeypatch):
    """
    ★ 时序契约：画圈必须发生在**注入之前**。

    放后面的两个坏处：① 注入抛异常时人看不到任何痕迹（失败恰恰最需要知道它想点哪）；
    ② 点完弹窗可能已关，圈贴在完全不同的界面上。
    """
    from computer_use_mcp.backend.linux import ring as ring_mod

    order: list[str] = []

    class _FakeInjector:
        def is_available(self): return True
        def window_id_under(self, x, y): return "1"
        def click_at(self, x, y, button=1, focus_wid=None, **kw):
            order.append("inject")
            return True

    monkeypatch.setattr(ring_mod, "show", lambda x, y, ttl=None: order.append("ring"))
    be = be_mod.LinuxBackend()
    monkeypatch.setattr(be, "injector", _FakeInjector())

    assert be.click_at(5, 6) is True
    assert order == ["ring", "inject"], f"必须先画圈再注入，实际 {order}"


def test_screenshot_clears_ring_first(monkeypatch):
    """截图前必须先把残圈抹掉（`act_sequence` 里 click 步紧接 screenshot 步时，圈还在）。"""
    from computer_use_mcp.backend.linux import ring as ring_mod

    calls: list[str] = []
    monkeypatch.setattr(ring_mod, "clear_now", lambda timeout=0.05: calls.append("clear"))
    monkeypatch.setattr(grab_mod, "grab_rgb",
                        lambda region=None: (Image.new("RGB", (4, 4)), (0, 0)))

    be_mod.LinuxBackend().screenshot(max_side=20)
    assert calls == ["clear"], calls


def test_coordinator_never_imports_platform_ring():
    """
    结构性守卫：平台代码（Xlib / ring）**不得下渗**到 platform-agnostic 的 coordinator。

    这条同时守住「光圈挂在 backend.click_at 这个唯一漏斗上」的设计——一旦有人把挂载点
    搬回 coordinator，他必然要在那里 import 平台模块，本测试立刻变红。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"
    offenders = []
    for path in (root / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "Xlib" in text or "from ..backend.linux import ring" in text \
                or "from ...backend.linux import ring" in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"平台代码出现在 core/ 下：{offenders}"


def test_all_coordinate_clicks_go_through_backend_click_at():
    """
    结构性守卫：「`backend.click_at` 是坐标点击唯一漏斗」这个论证本身要被守住。

    光圈、预览图、变化对比全都依赖「所有坐标点击都经过它」——一旦有人新增一条绕过它的
    坐标路径，那些证据会**静默缺失**（工具照常返回成功，只是没有落点反馈）。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"
    allowed = {"core/coordinator/actions.py", "backend/linux/backend.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if rel in allowed or rel.startswith("backend/linux/inject"):
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.click_at\(", line) and "def click_at" not in line:
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, f"绕过了唯一漏斗的坐标点击：{offenders}"