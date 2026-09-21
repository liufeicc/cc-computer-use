"""
灰区感知：OCR 解析、get_screen_text、describe_point（拆分自 test_optimizations.py）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from computer_use_mcp.backend.base import Backend, QueryResult, Rect, TextBlock
from computer_use_mcp.backend.linux import inject as inject_mod
from computer_use_mcp.backend.linux.inject import XdotoolInjector
from computer_use_mcp.core import display as display_mod
from computer_use_mcp.core import screen_lock
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import (
    LEVEL_ELEMENT,
    BackendUnavailableError,
    ComputerUseError,
    InjectionError,
    InvalidRefError,
    SandboxUnavailableError,
    ScreenBusyError,
    to_friendly_text,
)

# 注：拆分（2026-09-18）后各文件共用**同一份 import 头**，其中未用到的名字无害。
# 统一的好处是不会漏项——按需裁剪时漏掉一个 import，报错点会离真正的原因很远。

from _helpers import _StubBackend, _proc, _reset_display  # noqa: E402




# ---------- 灰区感知：OCR 文本层 ----------
def test_join_words_keeps_cjk_together():
    """中文要被 tesseract 按字切开，拼回去时不能加空格，否则 '忽 略' 既难读也难匹配。"""
    from computer_use_mcp.backend.linux.ocr import _join_words
    assert _join_words(["忽", "略"]) == "忽略"
    assert _join_words(["Save", "As"]) == "Save As"
    assert _join_words(["保存", "Save"]) == "保存Save"      # 中英交界不加空格
    assert _join_words([]) == ""





def test_parse_tsv_groups_words_and_maps_coordinates():
    """
    TSV 解析：同一行的词要合并成一个块、包围盒取并集，且坐标必须
      (1) 除以放大倍数、(2) 加上区域偏移 —— 否则模型拿到的坐标是错的。
    """
    from computer_use_mcp.backend.linux.ocr import OcrReader, _UPSCALE
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t20\t40\t30\t16\t90.5\t忽\n"
        "5\t1\t1\t1\t1\t2\t52\t40\t30\t16\t88.0\t略\n"
        "5\t1\t1\t1\t2\t1\t20\t80\t40\t16\t95.0\tOK\n"
        "5\t1\t1\t1\t3\t1\t20\t120\t40\t16\t12.0\tnoise\n"   # 低置信度，应被丢弃
    )
    blocks = OcrReader._parse_tsv(tsv, off_x=100, off_y=200, min_conf=40.0)
    assert [b.text for b in blocks] == ["忽略", "OK"], "低置信度的块必须被丢掉"
    # 行1：x 从 min(20,52)=20 到 max(20+30,52+30)=82 → 除以 UPSCALE 再加偏移
    b0 = blocks[0]
    assert (b0.rect.x, b0.rect.y) == (100 + 20 // _UPSCALE, 200 + 40 // _UPSCALE)
    assert b0.rect.w == (82 - 20) // _UPSCALE
    assert b0.conf == 90.5





def test_ocr_reader_reports_missing_tesseract(monkeypatch):
    """tesseract 没装时要给出可操作的提示，而不是抛裸异常。"""
    from computer_use_mcp.backend.linux.ocr import OcrReader
    monkeypatch.setattr("computer_use_mcp.backend.linux.ocr.shutil.which", lambda n: None)
    r = OcrReader()
    assert r.is_available() is False
    assert "tesseract-ocr" in (r.error or "")





def test_text_block_is_not_element_actionable():
    """
    TextBlock 必须「可点但不可元素级操作」：
    invoke/set_value 返回 False，element_screen_rect 直接给自带 rect，
    这样 coordinator 才会正常降级到坐标点击（OCR 只认得出字，认不出控件语义）。
    """
    from computer_use_mcp.backend.base import Rect as _Rect
    from computer_use_mcp.backend.base import TextBlock
    from computer_use_mcp.backend.linux.backend import LinuxBackend

    b = LinuxBackend.__new__(LinuxBackend)      # 不构造 reader，本用例只走 TextBlock 快路径
    blk = TextBlock(text="保存", rect=_Rect(862, 571, 26, 14), conf=88)
    assert b.invoke(blk) is False
    assert b.set_value(blk, "x") is False
    assert b.element_screen_rect(blk) == blk.rect
    detail = b.element_info(blk)
    assert detail.role == "text-block" and detail.name == "保存"





def test_get_screen_text_defaults_to_active_window(monkeypatch):
    """默认只识别**活动窗口**那块区域——全屏 OCR 实测 8~10s，窗口区域 0.3~3s。"""
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    win = Rect(645, 392, 310, 212)
    backend.active_window_rect = lambda: win
    seen: list = []
    backend.read_text = lambda region=None, min_conf=40.0: (seen.append(region), [])[1]

    coord = Coordinator(backend=backend)
    coord.get_screen_text()
    assert seen == [win.to_tuple()], "应把活动窗口矩形作为识别区域"

    seen.clear()
    coord.get_screen_text(scope="screen")
    assert seen == [None], "scope=screen 才允许全屏"





def test_ocr_refs_are_clickable_via_coordinate_fallback(monkeypatch):
    """
    端到端：get_screen_text 分配的 ref 必须能直接 click(ref=N) ——
    它走坐标降级（元素级对 TextBlock 返回 False），落点是 rect 中心。
    """
    import re
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    backend.active_window_rect = lambda: None
    backend.read_text = lambda region=None, min_conf=40.0: [
        TextBlock(text="保存", rect=Rect(862, 571, 26, 14), conf=88),
    ]
    coord = Coordinator(backend=backend)
    txt = coord.get_screen_text()
    assert "保存" in txt and "(862,571)" in txt and "屏幕绝对坐标" in txt

    m = re.search(r"\[(\d+)\] 保存", txt)
    assert m, f"输出里应带 ref：{txt}"
    r = coord.click(ref=int(m.group(1)))
    assert r.ok, r.message
    assert ("click", 875, 578) in backend.calls, "应点在该块的 rect 中心"





def test_parse_tsv_splits_on_large_gap():
    """
    同一视觉行里相距很远的文本必须切成两块：并成一块时返回的坐标会落在两者中间，
    点下去两头都不着（tesseract 会把「左标签 …… 右值」并成一行，必须自己切）。
    同时确认中文字间的小间隙**不会**被误切。
    """
    from computer_use_mcp.backend.linux.ocr import OcrReader
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t20\t40\t30\t16\t90\t名称\n"      # 左标签：20..50
        "5\t1\t1\t1\t1\t2\t52\t40\t30\t16\t90\t列\n"        # 紧邻（间隙 2）→ 不切
        "5\t1\t1\t1\t1\t3\t400\t40\t40\t16\t90\t值\n"       # 远处（间隙 348）→ 切
    )
    blocks = OcrReader._parse_tsv(tsv, 0, 0, 40.0)
    assert [b.text for b in blocks] == ["名称列", "值"], \
        f"应在远距离处切开、中文字间不切，实际 {[b.text for b in blocks]}"





def test_describe_point_none_when_no_window(monkeypatch):
    """落点所在的窗口查不到（坐标落在根窗口/空桌面）时各项为 None，且不抛异常。"""
    from computer_use_mcp.backend.linux.backend import LinuxBackend

    b = LinuxBackend.__new__(LinuxBackend)
    b.injector = type("I", (), {"window_id_under": staticmethod(lambda x, y: None)})()
    ev = b.describe_point(5, 6)
    assert ev["window_id"] is None and ev["window_title"] is None and ev["text"] is None





def test_describe_point_picks_window_title_and_rect():
    from computer_use_mcp.backend.linux.backend import LinuxBackend

    b = LinuxBackend.__new__(LinuxBackend)
    b.injector = type("I", (), {
        "window_id_under": staticmethod(lambda x, y: "777"),
        "window_title": staticmethod(lambda wid: "DBeaver — SQL 编辑器"),
        "window_geometry": staticmethod(lambda wid: Rect(10, 20, 1280, 800)),
    })()
    ev = b.describe_point(100, 200)
    assert ev["window_id"] == "777"
    assert ev["window_title"] == "DBeaver — SQL 编辑器"
    assert ev["window_rect"] == [10, 20, 1280, 800]
    assert ev["text"] is None, "with_text=False 时不该 OCR"





def test_text_near_prefers_block_containing_point():
    """落点文字取「覆盖该点」的块；没有则取最近的一块，太远则认作没文字。"""
    from computer_use_mcp.backend.linux.backend import LinuxBackend

    b = LinuxBackend.__new__(LinuxBackend)
    blocks = [TextBlock(text="确定", rect=Rect(850, 560, 26, 20), conf=90),
              TextBlock(text="放弃", rect=Rect(700, 560, 26, 20), conf=90)]
    b.read_text = lambda region=None, min_conf=40.0: blocks
    assert b._text_near(860, 570).text == "确定"      # 落点在「确定」框内
    assert b._text_near(880, 570).text == "确定"      # 不在任何框内 → 取最近的
    assert b._text_near(2000, 2000) is None, "离得太远不该硬认一个回来"

    b.read_text = lambda region=None, min_conf=40.0: []
    assert b._text_near(860, 570) is None
