"""
坐标校准层（core/geometry）。

根因（demo/diag_coord.py 诊断结论）：
  GTK 应用通过 AT-SPI 上报的元素 SCREEN 坐标，可能是「相对窗口原点」的值（实测 (0,0)），
  未叠加窗口在虚拟桌面（双屏）上的真实位置 → 直接拿去点击必然点偏。

校准公式（demo/calib_click.py 已验证）：
  屏幕绝对坐标 = 窗口真实屏幕位置(左上角) + 元素 WINDOW 相对坐标

本模块只做纯几何换算（不碰 gi / xdotool），便于单元测试。
窗口真实位置由 inject.XdotoolInjector 提供，元素相对坐标由 atspi.get_extents(WINDOW) 提供。
"""

from __future__ import annotations

from ..backend.base import Rect


def calibrate_rect(window_screen: Rect, element_window_rel: Rect) -> Rect:
    """
    把元素的「窗口相对矩形」换算成「屏幕绝对矩形」。

    参数：
      window_screen: 窗口在屏幕上的绝对矩形（左上角 x,y 为窗口原点）。
      element_window_rel: 元素在 WINDOW 坐标系下的相对矩形。

    返回：元素屏幕绝对矩形（x=窗口x+相对x, y=窗口y+相对y, w/h 不变）。
    """
    return Rect(
        x=window_screen.x + element_window_rel.x,
        y=window_screen.y + element_window_rel.y,
        w=element_window_rel.w,
        h=element_window_rel.h,
    )


def calibrate_center(window_screen: Rect, element_window_rel: Rect) -> tuple[int, int]:
    """校准后元素中心的屏幕绝对坐标（最常用：坐标点击的落点）。"""
    rect = calibrate_rect(window_screen, element_window_rel)
    return rect.center


def screen_rect_is_suspicious(screen: Rect | None, window_screen: Rect | None) -> bool:
    """
    判断 AT-SPI 的 SCREEN 坐标是否「可疑漂移」，从而决定是否必须走校准。

    启发式（基于 demo 观察）：
      - screen 读不到 → 可疑。
      - screen 落在窗口矩形之外（完全不相交）→ 可疑（漂移）。
      - screen 左上角接近 (0,0) 但窗口并不在原点 → 可疑（把窗口原点当屏幕原点）。
    返回 True 表示应改用「窗口位置 + WINDOW 相对坐标」校准值，而非直接信任 SCREEN。
    """
    if screen is None or screen.is_empty():
        return True
    if window_screen is None:
        return False
    # 完全不相交
    if (screen.x + screen.w < window_screen.x or screen.x > window_screen.x + window_screen.w
            or screen.y + screen.h < window_screen.y or screen.y > window_screen.y + window_screen.h):
        return True
    # 落在 (0,0) 附近但窗口不在原点
    if (screen.x, screen.y) == (0, 0) and (window_screen.x, window_screen.y) != (0, 0):
        return True
    return False


def resolve_element_screen_rect(
    screen_rect: Rect | None, window_screen: Rect | None, element_window_rel: Rect | None,
) -> tuple[Rect | None, str]:
    """
    综合决策元素最终的屏幕绝对矩形。

    实现逻辑：
      1. 若 SCREEN 矩形不可疑 → 直接用，source='screen'。
      2. 否则若有窗口位置 + WINDOW 相对坐标 → 用校准值，source='calibrated'。
      3. 都没有 → 返回 (None, 'unavailable')。

    返回 (rect, source)，source ∈ {screen, calibrated, unavailable}。
    """
    suspicious = screen_rect_is_suspicious(screen_rect, window_screen)
    if not suspicious and screen_rect is not None:
        return screen_rect, "screen"
    if window_screen is not None and element_window_rel is not None:
        return calibrate_rect(window_screen, element_window_rel), "calibrated"
    if screen_rect is not None:
        # 没有校准依据，只能勉强用 SCREEN（可能漂移）
        return screen_rect, "screen_unverified"
    return None, "unavailable"
