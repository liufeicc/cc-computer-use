"""
屏幕布局（backend.linux.inject.screens）—— 显示器尺寸与多屏布局。

优先解析 xrandr（能反映多屏），失败退化为 xdotool 的单屏尺寸。
`source` 字段必须反映**数据实际来自哪条通道**，见 `screen_layout` 的 M-36 说明。
"""

from __future__ import annotations

import shutil
import subprocess

from ....core import display
from .base import log


class ScreenMixin:
    """屏幕尺寸与布局查询。"""

    # ---------- 屏幕信息 ----------
    def screen_size(self) -> tuple[int, int] | None:
        """主屏尺寸 (w,h)。复用 getdisplaygeometry。"""
        p = self._run(["getdisplaygeometry"])
        parts = p.stdout.split()
        if len(parts) == 2:
            try:
                return (int(parts[0]), int(parts[1]))
            except ValueError:
                return None
        return None

    def screen_layout(self) -> dict:
        """
        显示器布局：优先解析 xrandr（多屏），失败退化为 xdotool 单屏尺寸。

        返回 dict：{monitors:[{name,w,h,x,y,primary}], virtual:(W,H), source}
        """
        monitors: list[dict] = []
        # M-36：source 必须反映**数据实际来自哪条通道**，不能只看「xrandr 这个程序在不在」。
        # 历史写法是 `"xrandr" if monitors and xrandr else "xdotool"`，而 monitors 可能已经被
        # 下面的 xdotool 兜底填过 —— 于是「xrandr 存在但解析不出东西、数据其实来自 xdotool」
        # 会被标成 source="xrandr"，排查多屏问题时指向错误的一层。
        src = "xdotool"
        xrandr = shutil.which("xrandr")
        if xrandr:
            try:
                out = subprocess.run([xrandr], capture_output=True, text=True, timeout=5,
                                     errors="replace", env=display.env_for()).stdout
                parsed = self._parse_xrandr(out)
                if parsed:
                    monitors = parsed
                    src = "xrandr"
            except Exception as exc:  # noqa: BLE001
                log.debug("xrandr 解析失败: %s", exc)
        size = self.screen_size()
        if not monitors and size:
            monitors = [{"name": "default", "w": size[0], "h": size[1],
                         "x": 0, "y": 0, "primary": True}]
            src = "xdotool"
        # 虚拟桌面尺寸 = 所有屏并集
        vw = max((m["x"] + m["w"] for m in monitors), default=size[0] if size else 0)
        vh = max((m["y"] + m["h"] for m in monitors), default=size[1] if size else 0)
        return {
            "monitors": monitors,
            "virtual": (vw, vh),
            "source": src,
        }

    @staticmethod
    def _parse_xrandr(xrandr_out: str) -> list[dict]:
        """
        解析 xrandr 输出中「connected」的显示器行。

        典型行：
          DP-1 connected primary 1920x1200+0+0 ...
          HDMI-1 connected 1920x1200+1920+0 ...
        提取 name / w x h / +x+y / primary。
        """
        monitors: list[dict] = []
        for line in xrandr_out.splitlines():
            if " connected" not in line:
                continue
            parts = line.split()
            name = parts[0]
            primary = "primary" in parts
            # 找形如 WxH+X+Y 的段
            geom = None
            for tok in parts:
                if "x" in tok and "+" in tok:
                    geom = tok
                    break
            if not geom:
                continue
            try:
                wh, xy = geom.split("+", 1)
                w, h = wh.split("x")
                xs, ys = xy.split("+")
                monitors.append({
                    "name": name, "w": int(w), "h": int(h),
                    "x": int(xs), "y": int(ys), "primary": primary,
                })
            except (ValueError, IndexError):
                continue
        return monitors