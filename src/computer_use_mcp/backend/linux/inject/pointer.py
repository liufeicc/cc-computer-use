"""
指针移动与坐标点击（backend.linux.inject.pointer）—— 三级降级里的坐标兜底层。

⚠️ 坐标点击是**兜底**通道：上层永远优先用 AT-SPI 元素级 `do_action`（零坐标、不受
焦点/DPI/分辨率影响）。只有在目标没有无障碍树（灰区应用）时才落到这里。
"""

from __future__ import annotations

import subprocess
import time

from ....utils.errors import InjectionError
from .base import log


class PointerMixin:
    """鼠标移动与坐标点击。"""

    # ---------- 窗口聚焦 ----------
    # 注（M-35）：这里曾有一个独立的 `focus_window(wid)`——生产代码零调用方，实际生效的是
    # click_at 里**内联**的同一条链路（单进程串 windowactivate/windowfocus/mousemove/click）。
    # 两份实现会各自漂移（改了一份另一份不动，且没有任何测试能同时覆盖两者），故删除；
    # 它原先的回归测试改为直接对 click_at 断言同一条优化（test_focus_window_single_run）。

    def window_id_under(self, x: int, y: int) -> str | None:
        """
        返回屏幕坐标 (x,y) 处的窗口 id（更稳：mousemove 后读 getmouselocation 的 WINDOW 字段）。

        实现逻辑：
          1. mousemove 到 (x,y)（不点击）。
          2. getmouselocation --shell，解析 WINDOW=<id>。
          3. 解析失败返回 None（调用方据此跳过聚焦，直接点击）。
        """
        try:
            self.mouse_move(x, y)
        except InjectionError:
            return None
        p = self._run(["getmouselocation", "--shell"])
        for line in p.stdout.splitlines():
            if line.startswith("WINDOW="):
                wid = line.split("=", 1)[1].strip()
                if wid and wid != "0":
                    return wid
        return None

    # ---------- 鼠标 / 点击 ----------
    def mouse_move(self, x: int, y: int) -> bool:
        self._run(["mousemove", str(int(x)), str(int(y))], check=True)
        return True

    def click_at(
        self, x: int, y: int, button: int = 1, focus_wid: str | None = None, settle: float = 0.0,
    ) -> bool:
        """
        坐标点击（兜底通道）。

        实现逻辑（优化：单进程串命令）：
          1. 若给定 focus_wid，把 windowactivate/windowfocus 与 mousemove/click 拼成
             **一条 xdotool 命令**一次执行（关键：避免合成事件被「激活窗口」吃掉，
             同时省去 3 次进程启动与中途 settle）。
          2. 若拼接命令失败，退化为仅 mousemove+click 再试一次 —— **但只对「未执行到点击」
             的失败重试**（见下面 M-34 的说明），超时不算。
          3. settle 默认 0（--sync 已等待 WM 确认）。
        """
        move_click = ["mousemove", str(int(x)), str(int(y)), "click", str(button)]
        if focus_wid:
            chained = ["windowactivate", "--sync", focus_wid,
                       "windowfocus", "--sync", focus_wid, *move_click]
            try:
                self._run(chained, check=True)
            except InjectionError as exc:
                # M-34：超时与「rc≠0」是两类完全不同的失败，不能混为一谈。
                #   - rc≠0：命令没跑到底，**没有点出去**，补一次是安全的；
                #   - TimeoutExpired：串命令是逐条执行的，超时前 windowactivate 甚至 click
                #     **可能已经发出去了**。此时再补一次点击 = **双击**，对「删除/确认」
                #     这类按钮就是重复触发。概率低，但后果不对称，故这一支不重试、直接上抛
                #     （让调用方看到失败并重新感知，而不是默默点了两下还报成功）。
                if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                    raise
                log.debug("带聚焦的串命令失败（未执行到点击），退化为仅移动+点击: %s", exc)
                self._run(move_click, check=True)
        else:
            self._run(move_click, check=True)
        if settle:
            time.sleep(settle)
        return True