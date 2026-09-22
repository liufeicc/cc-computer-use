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
        repeat: int = 1, delay_ms: int = 100,
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
          4. `repeat>1` 即连击（双击/三击）：交给 xdotool 的 `--repeat/--delay`，
             **间隔由 X 侧定时**。这是双击唯一可靠的做法——见下方注释。

        ⚠️ **连击绝不要改成「调用方连着调两次 click_at」**（那正是历史上双击不生效的原因）：
        两次独立调用之间夹着 Python 侧的落点证据、OCR、变化对比与步骤调度，实测每次约
        0.5s，**必然超过 GTK 约 400ms 的双击阈值**，于是系统把它们认成两次单击。而且
        「关掉那些副作用就能压进阈值」也靠不住：那只是把间隔从 0.5s 压到某个同样不受控的
        值，任何一次 GC 或注入慢一点就翻车。`--repeat 2 --delay 100` 把这件事交给 X server
        的定时器，与调用方的一切开销彻底解耦。（滚动同理：滚 N 格用 `--repeat N`。）
        """
        move_click = ["mousemove", str(int(x)), str(int(y))]
        if repeat > 1:
            move_click += ["click", "--repeat", str(int(repeat)),
                           "--delay", str(int(delay_ms)), str(int(button))]
        else:
            move_click += ["click", str(int(button))]
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

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, button: int = 1,
        steps: int = 10, hold: float = 0.02, focus_wid: str | None = None,
    ) -> bool:
        """
        拖拽：起点按下 → 插值分步移动 → 终点抬起（坐标级）。

        实现逻辑：
          1. 需要聚焦时，把 windowactivate/windowfocus 前插到同一条命令里（与 click_at 同理：
             合成事件会被「激活窗口」吃掉）。
          2. 起点 mousedown 后先 hold 一小会儿再移动——手柄/滚动条这类控件要先「抓住」，
             按下与移动挤在同一瞬间时，部分应用会当成单击。
          3. 起点到终点之间**插值 steps 个中间点**，每点之间 hold 秒。
          4. 终点 mouseup。

        ⚠️ **为什么必须插值、不能直接瞬移**：X 只发一个 MotionNotify 时，很多应用判定不出
        「按住拖动」（拖放目标不激活、画布不跟随、滚动条回弹），表现是「拖了但没反应」，
        而 xdotool 那边 rc=0、看着完全成功。插值让应用收到**一串**移动事件，才构成一次拖拽。

        ⚠️ **为什么拼成一条命令（含 sleep 子命令）而不是分 N 次 `_run`**：分次调用时每次
        subprocess 的启动开销（约 5~10ms）会盖过 hold 本身，拖拽节奏失真且总耗时随 steps
        线性膨胀。xdotool 的链式执行天然支持 `sleep`，一条命令即可控节奏。

        ⚠️ **失败不重试**（与 click_at 的 M-34 分支不同）：重试一次拖拽 = 对同一个目标
        再拖一遍，可能造成重复操作（移动了两次文件、画了两笔），后果不对称，故失败直接上抛。
        """
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        steps = max(1, int(steps))
        hold = max(0.0, float(hold))
        seq: list[str] = []
        if focus_wid:
            seq += ["windowactivate", "--sync", focus_wid, "windowfocus", "--sync", focus_wid]
        seq += ["mousemove", str(x1), str(y1), "mousedown", str(int(button))]
        if hold:
            seq += ["sleep", str(hold)]
        for i in range(1, steps + 1):
            # 最后一个点精确落在终点（避免浮点取整导致差一两个像素）
            mx = x2 if i == steps else round(x1 + (x2 - x1) * i / steps)
            my = y2 if i == steps else round(y1 + (y2 - y1) * i / steps)
            seq += ["mousemove", str(int(mx)), str(int(my))]
            if hold and i < steps:
                seq += ["sleep", str(hold)]
        seq += ["mouseup", str(int(button))]
        # 超时上限按步数放大：steps 大 + hold 大时，默认 10s 可能不够（链式命令含 sleep）
        self._run(seq, check=True, timeout=max(10.0, steps * hold * 2 + 5.0))
        return True