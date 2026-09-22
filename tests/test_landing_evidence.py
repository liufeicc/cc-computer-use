"""
落点证据（拆分自 test_optimizations.py）—— 省掉「再截一张图确认」的整个来回。

这一组的共同契约是：**证据一律不得让主操作失败**，以及「点前活动窗口」不能省
（点「确定」会关掉对话框，点后就没标题可读了）。
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




# ---------- 方向二：落点证据（省掉「再截一张图确认」的整个来回）----------
def test_click_xy_reports_landing_evidence_with_text(monkeypatch):
    """
    裸坐标点击必须回报落点证据，且**点之前**就取好：
      - with_text=True（裸坐标最容易点偏，落点文字是唯一能判断「打没打对」的信号）；
      - describe_point 的调用要早于 click_at —— 点完再问就晚了，弹窗可能已盖住原位置。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    backend.point = {"x": 875, "y": 578, "window_id": "1", "window_title": "确认删除",
                     "window_rect": [600, 380, 400, 240], "text": "确定", "conf": 91}
    backend.active_title = "确认删除"
    order: list[str] = []
    orig_dp, orig_click = backend.describe_point, backend.click_at
    backend.describe_point = lambda x, y, with_text=False: (order.append("describe"), orig_dp(x, y, with_text))[1]
    backend.click_at = lambda x, y, button=1, focus_window=True, **kw: (order.append("click"), orig_click(x, y, button, focus_window, **kw))[1]

    coord = Coordinator(backend=backend)
    r = coord.click_xy(875, 578)
    assert r.ok
    assert order == ["describe", "click"], f"必须先取证据再点，实际 {order}"
    assert backend.calls[0] == ("describe_point", 875, 578, True), "裸坐标点击要带落点文字"
    assert "落点：窗口「确认删除」 400x240" in r.message, r.message
    assert "落点文字：「确定」" in r.message, r.message
    assert "点后活动窗口：「确认删除」" in r.message, r.message
    assert r.data["text"] == "确定"





def test_click_coord_fallback_reports_landing_without_ocr(monkeypatch):
    """
    元素级失败 → 坐标兜底那条路也要回报落点，但**不做 OCR**（with_text=False）：
    目标元素的名字已经知道，缺的只是「这一下打在哪扇窗」，省掉那 0.3 秒。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    backend.element_screen_rect = lambda n: Rect(862, 571, 26, 14)
    backend.invoke = lambda n, action=None: False        # 元素级失败，逼出坐标兜底
    coord = Coordinator(backend=backend)

    blk = TextBlock(text="保存", rect=Rect(862, 571, 26, 14), conf=88)
    ref = coord.refs.register(blk, role="text-block", name="保存", app="ocr")
    r = coord.click(ref=ref)
    assert r.ok and r.level == "coord", r.message
    assert ("describe_point", 875, 578, False) in backend.calls, "坐标兜底要有落点证据、且不 OCR"
    assert "落点：窗口" in r.message, r.message





def test_landing_evidence_failure_never_breaks_click(monkeypatch):
    """
    落点证据是**附加信息**：取不到（X 查询失败、没有活动窗口等）绝不能连带点击失败
    ——否则为省一个来回反而把主功能弄挂了。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()

    def boom(*a, **k):
        raise RuntimeError("xdotool 挂了")

    backend.describe_point = boom
    backend.active_window_title = boom
    coord = Coordinator(backend=backend)
    r = coord.click_xy(10, 20)
    assert r.ok and "坐标级点击已执行" in r.message, r.message
    assert backend.calls[-1] == ("click", 10, 20), "点击本身必须照常执行"





def test_keyboard_injection_reports_active_window(monkeypatch):
    """
    键盘注入的 `ok` 只说明「按键发出去了」，不说「送到哪个应用去了」——
    实测有过把文本打进飞书/Remmina 的事故。回报活动窗口标题即可一眼看穿，
    不必再截图核对。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    backend.active_title = "飞书"
    coord = Coordinator(backend=backend)

    r = coord.type_text("hello")
    assert r.ok and "活动窗口：「飞书」" in r.message, r.message
    r = coord.press_key("ctrl+s")
    assert r.ok and "活动窗口：「飞书」" in r.message, r.message

    # 拿不到标题时安静省略，不写 None 也不报错
    backend.active_title = None
    r = coord.press_key("ctrl+s")
    assert r.ok and "活动窗口" not in r.message, r.message





def test_format_landing_omits_missing_pieces():
    f = Coordinator._format_landing
    assert f({}) == ""
    assert f({}, {}) == ""
    assert f({"window_title": "A"}) == "落点：窗口「A」"
    assert f({"window_rect": [0, 0, 800, 600]}) == "落点：窗口「?」 800x600"
    assert f({"text": "确定"}) == "落点文字：「确定」"
    assert f({}, {"active_window": "B"}) == "点后活动窗口：「B」"
    # 有前无后 = 窗口被这一下关掉了，这本身就是「点中了」的强信号，必须说出来
    assert f({}, {}, "A") == "点后活动窗口：无（原「A」已消失）"
    assert f({}, {"active_window": "B"}, "A") == "点后活动窗口：「B」（原「A」）"
    assert f({}, {"active_window": "A"}, "A") == "点后活动窗口：「A」"





def test_click_xy_reports_window_disappeared(monkeypatch):
    """
    点「确定」这类按钮会把对话框**关掉**，点后已无活动窗口可读——最关键那一刻的证据
    反而会丢。靠「点前活动窗口」才能说清结果：『无（原「OCR故事」已消失）』本身就是
    「点中了、窗口确实关了」的强信号（端到端实测踩到过：只有点后值时报不出这条）。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    seq = iter(["OCR故事", None])   # 点前读到「OCR故事」，点后窗口已被关掉
    backend.active_window_title = lambda: next(seq, None)
    coord = Coordinator(backend=backend)
    r = coord.click_xy(875, 578)
    assert r.ok
    assert "点后活动窗口：无（原「OCR故事」已消失）" in r.message, r.message





def test_injection_landing_reads_after_settle(monkeypatch):
    """
    落点证据的**读取时机**：注入类操作的事后读取必须等一个 settle。

    2026-09-15 DBeaver 实测：`alt+F4` 关掉 Chrome 后立即读活动窗口，回报的仍是
    Chrome——窗口销毁/焦点转移还没在 X 上落定。我据此以为 alt+F4 没生效，多花了
    一轮去确认。**证据读早了比不读更糟**（不读只是缺信息，读早是给错信息）。

    同时锁住反面：点击**前**的快照不该等待——那时没有等待的理由，白等只拖慢每次点击。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    seen: list[float] = []
    orig = coord._landing

    def spy(settle: float = 0.0):
        seen.append(settle)
        return orig(settle)

    monkeypatch.setattr(coord, "_landing", spy)

    t0 = time.monotonic()
    coord.press_key("alt+F4")
    elapsed = time.monotonic() - t0
    # 按键前读一次（供「原窗口已消失」这类判定），按键后等 settle 再读一次
    assert seen == [0.0, Coordinator._INJECT_SETTLE], f"按键后必须等 settle 再读，实际 {seen}"
    assert elapsed >= Coordinator._INJECT_SETTLE, f"确实等待了（实测 {elapsed:.3f}s）"

    seen.clear()
    coord.click_xy(5, 6)
    assert seen[0] == 0.0, "点击前的快照不该等待"
    assert seen[-1] == Coordinator._INJECT_SETTLE, "点击后的事件读取要等待"

    seen.clear()
    coord.type_text("hi")
    assert seen == [Coordinator._INJECT_SETTLE], f"输入后要等待，实际 {seen}"
