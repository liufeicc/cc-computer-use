"""
跨文件共享的测试辅助（拆分自原 test_optimizations.py）。

**为什么抽出来**：`_StubBackend` 在原文件里被引用 40 次、跨度从第 143 行一直到 1752 行；
`_proc` 被引用 28 次、跨度 39~2127。两者都不是某一个测试组的私产，留在任一个拆分文件里
都会让其余文件反向依赖它。

其余辅助（`_FakeMCP` / `_FakeNode` / `_FakeEditable` / `_register_dead_ref` 等）都是
局部用途，刻意留在各自的文件里 —— 这是本 tests/ 目录的既有风格。
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


def _proc(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)





# ---------- act_sequence ----------
class _StubBackend(Backend):
    """最小 backend 桩：记录调用，press_key 可控失败。"""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_key = False
        self.active_title: str | None = "Stub Window"
        # focus 单独记账（不塞进 calls）：调用方 type_text 会在赋值前聚焦，
        # 记进 calls 会打乱既有的「第 N 个调用是什么」类断言（见 I-10 的回归测试）
        self.focused: list = []
        # 最近一次坐标点击/拖拽的**完整**参数（含 repeat/delay/steps）。
        # 单独记而不塞进 calls：calls 是 ("click", x, y) 这样的定长元组，既有断言
        # （如 `("click", 5, 6) in backend.calls`）依赖它的形状，改元组长度会连带崩一片；
        # 而连击/滚动的断言只需要看最近一次，不需要历史。
        self.last_click: dict | None = None
        self.last_drag: dict | None = None
        # 落点证据的默认返回值（测试里按需覆盖）
        self.point: dict = {"x": 0, "y": 0, "window_id": "1", "window_title": "Stub Window",
                            "window_rect": [0, 0, 800, 600], "text": None, "conf": None}

    def is_available(self): return True
    def list_apps(self): return []
    def get_tree(self, *a, **k): return QueryResult()
    def find(self, *a, **k): return QueryResult()
    def element_info(self, n): raise NotImplementedError
    def is_alive(self, n):
        # 桩的默认：**非 TextBlock 一律判「已失效」**——这正是 I-1 那组重定位测试的驱动
        # 条件（原实现靠 element_info raise 间接得出这个结论，现在在 ABC 层直接表态，见 I-10）。
        # 需要「仍有效」的用例直接覆盖本方法，见 test_alive_ref_never_relocates_nor_flags。
        return False
    def focus(self, n): self.focused.append(n); return True
    def invoke(self, n, action=None): return not isinstance(n, TextBlock)
    def set_value(self, n, t): return not isinstance(n, TextBlock)
    def element_screen_rect(self, n):
        # TextBlock 自带屏幕绝对坐标（OCR 算好的），走快路径、不套 geometry 校准
        return n.rect if isinstance(n, TextBlock) else None
    def click_at(self, x, y, button=1, focus_window=True, repeat=1, delay_ms=100):
        self.calls.append(("click", x, y))
        self.last_click = {"x": x, "y": y, "button": button, "repeat": repeat,
                           "delay_ms": delay_ms, "focus_window": focus_window}
        return True
    def drag_at(self, x1, y1, x2, y2, button=1, steps=10, focus_window=True):
        self.calls.append(("drag", x1, y1, x2, y2))
        self.last_drag = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button,
                          "steps": steps, "focus_window": focus_window}
        return True
    def type_text(self, t): self.calls.append(("type", t)); return True
    def press_key(self, c): self.calls.append(("key", c)); return not self.fail_key
    def window_screen_pos(self, n): return None
    def screen_layout(self): return {}
    def active_window_rect(self): return None
    def read_text(self, region=None, min_conf=40.0): return []
    def active_window_title(self): return self.active_title
    def describe_point(self, x, y, with_text=False):
        self.calls.append(("describe_point", x, y, with_text))
        return dict(self.point)
    def screenshot(self, region=None, max_side=None, fmt="jpeg", quality=85):
        return b"", {"format": fmt, "width": 0, "height": 0, "bytes": 0}
    def list_windows(self, limit=100): return [{"id": "1", "title": "t"}]
    def wait_window(self, **k): return {"id": "1", "title": "t"}





def _reset_display(monkeypatch, ever_up: bool = False) -> None:
    """
    把 MANAGER 恢复到「未启动过」的干净态，排除真实机器上的残留 socket 干扰。

    ⚠️⚠️ **必须同时把「回收现场」的动作也打桩掉**（2026-09-18 实测踩到，代价是一次
    整个 pytest 进程 SIGABRT）。只打桩状态字段是不够的：

      `_start_locked` 在「沙箱不在跑」时会**先**调 `_reap_dead_sandbox()`，而那个方法
      带**真实副作用** —— 杀掉 dbus-daemon + at-spi2-registryd、`shutil.rmtree` 掉
      `/tmp/cc-cu-at-spi-*` 目录，并把 `self._at_spi_bus` 置为 None；**那个赋值不在
      monkeypatch 的账上，teardown 不会还原它**。

      单测模式下确实无对象可伤（强制 real、机器上没有我们的沙箱），但 **e2e 模式**下
      真有沙箱在跑：本辅助一调，那个沙箱的**私有总线就被拆了，而 Xephyr 还活着**。
      之后 e2e 的 `display.start()` 走「socket 存在 → 沙箱已就绪」的早退分支，
      `_ensure_at_spi_bus()` 反查不到已被删的总线目录 → `_at_spi_bus` 保持 None →
      reader 绑不上私有总线 → 流量落到**宿主总线** → libatspi 在 `atspi.get_desktop(0)`
      处中止整个进程。

      炸点显示在 `tests/test_e2e_zenity.py::test_read_zenity_tree`，**根因却在这里** ——
      排查时别被「崩在 e2e」带偏。旧的文件布局把本辅助的调用者排在 e2e **之后**，纯属
      文件名字母序的侥幸，2026-09-18 按模块拆文件后立即暴露（conftest 里那句
      「换顺序 / 拆文件即现偶发失败」说的正是这类事）。

    打桩 `_reap_dead_sandbox` 不影响被测语义：调用它的用例测的是「沙箱起不来时拒绝注入」
    /「启动尝试计数」，而「回收上一轮残留」不是被测对象，它只是这条路径上顺手做的清理。
    """
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(display_mod.MANAGER, "_start_attempts", 0)
    monkeypatch.setattr(display_mod.MANAGER, "_ever_up", ever_up)
    monkeypatch.setattr(display_mod.MANAGER, "_proc", None)
    monkeypatch.setattr(display_mod.MANAGER, "_sandbox_display", None)
    monkeypatch.setattr(display_mod.MANAGER, "_auto_display", True)
    monkeypatch.setattr(display_mod.MANAGER, "_socket_path",
                        lambda: "/nonexistent/cc-cu-test-socket")
    # —— 以下是「不许碰真机现场」的两道闸（理由见 docstring）——
    monkeypatch.setattr(display_mod.MANAGER, "_reap_dead_sandbox", lambda: None)
    monkeypatch.setattr(display_mod.MANAGER, "_kill_at_spi_procs",
                        lambda *a, **k: None)
    # 双保险：万一还有别的路径写到这些字段，也一并记账，保证 teardown 能还原
    monkeypatch.setattr(display_mod.MANAGER, "_at_spi_bus", None)
    monkeypatch.setattr(display_mod.MANAGER, "_at_spi_dir", None)
    monkeypatch.setattr(display_mod.MANAGER, "_at_spi_proc", None)
    monkeypatch.setattr(display_mod.MANAGER, "_at_spi_registry", None)
    monkeypatch.setattr(display_mod.MANAGER, "_wm_proc", None)
    monkeypatch.setattr(display_mod.MANAGER, "_host_rect_cache", None)
