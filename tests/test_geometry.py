"""core/geometry 单元测试：坐标校准公式（纯几何，无需桌面）。"""

from __future__ import annotations

from computer_use_mcp.backend.base import Rect
from computer_use_mcp.core import geometry


def test_calibrate_rect_adds_window_origin():
    """屏幕绝对 = 窗口位置 + 元素相对（demo 验证公式）。"""
    win = Rect(1909, 115, 400, 300)        # 窗口左上角
    rel = Rect(200, 150, 80, 40)           # 元素窗口相对矩形
    out = geometry.calibrate_rect(win, rel)
    assert (out.x, out.y, out.w, out.h) == (1909 + 200, 115 + 150, 80, 40)


def test_calibrate_center():
    """中心点 = 校准矩形中心（对应 demo: 窗口(1909,115)+相对中心(239,185)=绝对(2148,300)）。"""
    win = Rect(1909, 115, 400, 300)
    rel = Rect(199, 165, 80, 40)           # 中心相对 = (239,185)
    cx, cy = geometry.calibrate_center(win, rel)
    assert (cx, cy) == (1909 + 239, 115 + 185)


def test_suspicious_when_screen_at_origin_but_window_not():
    """SCREEN 落 (0,0) 而窗口不在原点 → 判定漂移可疑。"""
    screen = Rect(0, 0, 80, 40)
    win = Rect(1909, 115, 400, 300)
    assert geometry.screen_rect_is_suspicious(screen, win) is True


def test_suspicious_when_outside_window():
    """SCREEN 完全落在窗口外 → 可疑。"""
    screen = Rect(5000, 5000, 80, 40)
    win = Rect(0, 0, 400, 300)
    assert geometry.screen_rect_is_suspicious(screen, win) is True


def test_not_suspicious_when_inside_window():
    """SCREEN 落在窗口内 → 可信。"""
    win = Rect(100, 100, 400, 300)
    screen = Rect(150, 150, 80, 40)
    assert geometry.screen_rect_is_suspicious(screen, win) is False


def test_suspicious_when_screen_none():
    assert geometry.screen_rect_is_suspicious(None, Rect(0, 0, 10, 10)) is True


def test_resolve_prefers_screen_when_trusted():
    """SCREEN 可信时直接用 SCREEN，source='screen'。"""
    win = Rect(100, 100, 400, 300)
    screen = Rect(150, 150, 80, 40)
    rel = Rect(50, 50, 80, 40)
    rect, source = geometry.resolve_element_screen_rect(screen, win, rel)
    assert source == "screen"
    assert rect.to_tuple() == screen.to_tuple()


def test_resolve_calibrates_when_screen_drifted():
    """SCREEN 漂移时用校准值，source='calibrated'。"""
    win = Rect(1909, 115, 400, 300)
    screen = Rect(0, 0, 80, 40)            # 漂移
    rel = Rect(199, 165, 80, 40)
    rect, source = geometry.resolve_element_screen_rect(screen, win, rel)
    assert source == "calibrated"
    assert (rect.x, rect.y) == (1909 + 199, 115 + 165)


def test_resolve_unavailable_when_nothing():
    rect, source = geometry.resolve_element_screen_rect(None, None, None)
    assert rect is None
    assert source == "unavailable"
