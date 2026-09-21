"""
xdotool 注入层（Linux backend 的「坐标点击 / 键盘输入 / 窗口几何」兜底通道）。

复用 demo/calib_click.py、action_vs_coord.py 已验证逻辑：
  - window_screen_pos_by_pid：按 PID 精确锁窗、过滤 1x1 隐形窗、取面积最大；
  - click_at：先 windowactivate/windowfocus --sync 再 mousemove+click（修复 demo 路径B 焦点问题）。

注入走 X11/XTest 通道（xdotool），MVP 锁定 X11。后续 Phase 2 可换 uinput。

═══ 本包的结构（2026-09-18 由单文件 backend/linux/inject.py 拆分而来）═══
`XdotoolInjector` 现在是六个 mixin 的**组合**，对外类名与方法名完全不变：

    base.py      共用 logger、二进制定位、唯一子进程出口 `_run`
    windows.py   活动窗 / 几何 / 清单 / 标题 / wait_window
    pointer.py   指针移动与坐标点击（window_id_under / mouse_move / click_at）
    keyboard.py  文本输入（含剪贴板通道）与快捷键
    screens.py   屏幕尺寸与 xrandr 布局
    apps.py      按「有可见窗口」枚举应用名（纯 X11）

这些 mixin **共享同一份实例状态**（方法之间用 `self.` 互调）：它们描述的是同一件事
（同一个 xdotool 通道）的不同侧面。

⚠️ 所有子模块共用 `base.log` 这**一个 logger 对象**（不是各自 `get_logger(__name__)`）
—— 日志名因此与拆分前一致为 `computer_use_mcp.backend.linux.inject`，且
`monkeypatch.setattr(inject.log, "warning", ...)` 这类补丁对全部子模块生效。
"""

from __future__ import annotations

# ⚠️ 保留这些标准库名是有意的：子模块一律用「属性访问」调用它们
# （`shutil.which(...)` 而非 `from shutil import which`），因此
# `monkeypatch.setattr(inject.shutil, "which", ...)` 改的是**同一个模块对象**，
# 对全部子模块生效；既有测试正是通过这些入口打补丁的。
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401

from .apps import AppMixin
from .base import InjectorBase, log  # noqa: F401
from .keyboard import KeyboardMixin
from .pointer import PointerMixin
from .screens import ScreenMixin
from .windows import WindowMixin


class XdotoolInjector(InjectorBase, WindowMixin, PointerMixin, KeyboardMixin,
                      ScreenMixin, AppMixin):
    """封装 xdotool 命令调用。所有方法对缺失/失败做防御。"""