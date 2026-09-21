"""
act_sequence 批量动作与 click_xy（拆分自 test_optimizations.py）。

`act_sequence` 的各条规模上限（M-46）也在这里：整串是**持屏锁**执行的，
没有上限时 `sleep 3600` 能锁死一块屏一小时。
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




def test_act_sequence_runs_all_and_stops_on_error():
    backend = _StubBackend()
    coord = Coordinator(backend=backend)

    # 全部成功
    r = coord.act_sequence([
        {"op": "key", "combo": "ctrl+a"},
        {"op": "type", "text": "hi"},
        {"op": "sleep", "seconds": 0},
    ])
    assert r["ok"] is True and len(r["steps"]) == 3

    # 第一步失败即停
    backend.fail_key = True
    r = coord.act_sequence([
        {"op": "key", "combo": "ctrl+a"},
        {"op": "type", "text": "never"},
    ], stop_on_error=True)
    assert r["ok"] is False
    assert r["stopped_at"] == 0
    assert len(r["steps"]) == 1  # 第二步未执行





def test_click_xy_and_seq_xy():
    """裸坐标点击通道：click_xy 与 act_sequence 的 x/y step 都落到 backend.click_at。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    r = coord.click_xy(5, 6)
    assert r.ok and ("click", 5, 6) in backend.calls
    r2 = coord.act_sequence([{"op": "click", "x": 7, "y": 8}])
    assert r2["ok"] and ("click", 7, 8) in backend.calls





def test_sandbox_guard_modes(monkeypatch):
    """礼让守卫：real 模式恒不放警告；isolated 且用户在内时返回警告文本。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    assert coord._sandbox_guard() is None
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(display_mod.MANAGER, "wait_until_user_leaves",
                        lambda *a, **k: False)
    warn = coord._sandbox_guard()
    assert warn and "沙箱" in warn





def test_point_in_rect():
    f = display_mod.DisplayManager.point_in_rect
    assert f(10, 10, (0, 0, 100, 100))
    assert not f(100, 100, (0, 0, 100, 100))  # 右/下边界开区间
    assert not f(-1, 5, (0, 0, 100, 100))





# ---------- 方向二：act_sequence 的 screenshot 步骤（动作与确认合并为一次调用）----------
def test_act_sequence_screenshot_op_returns_image():
    """
    screenshot 步骤：图像走 _images 键（bytes 无法 JSON 序列化），
    且 pop 掉之后结果必须能被 json.dumps —— 工具层就靠这个先取图再序列化。
    """
    import json

    backend = _StubBackend()
    backend.screenshot = lambda region=None, max_side=None, fmt="jpeg", quality=85: (
        b"\xff\xd8fake", {"format": fmt, "width": 1280, "height": 800, "scale": 1.25})
    coord = Coordinator(backend=backend)

    r = coord.act_sequence([
        {"op": "sleep", "seconds": 0},
        {"op": "screenshot", "max_side": 800},
    ])
    assert r["ok"] is True and len(r["steps"]) == 2
    imgs = r.pop("_images")
    assert len(imgs) == 1
    data, meta = imgs[0]
    assert data == b"\xff\xd8fake" and meta["scale"] == 1.25
    json.dumps(r, ensure_ascii=False)      # 取出图像后必须可序列化
    assert "截图 meta=" in r["steps"][1]["message"]





def test_act_sequence_without_screenshot_has_no_images_key():
    """
    `_images` 键的**双向**判据（M-51）。

    原先只有单向的负断言（「没截图时不存在 `_images`」）——即使实现里**完全删掉**
    `_images` 机制，那条也照样通过，属于「判据松的测试等于没有测试」。现在同一条用例
    里带上正向：有截图步骤时必须**真的**带 `_images` 且装的是那张图。
    """
    backend = _StubBackend()
    backend.screenshot = lambda region=None, max_side=None, fmt="jpeg", quality=85: (
        b"\xff\xd8fake", {"format": fmt})
    coord = Coordinator(backend=backend)

    without = coord.act_sequence([{"op": "sleep", "seconds": 0}])
    assert "_images" not in without, "没有截图步骤就不该带 _images（工具层据此走纯文本返回）"

    with_shot = coord.act_sequence([{"op": "screenshot"}])
    assert "_images" in with_shot, "有截图步骤就必须带 _images，否则这条负断言毫无约束力"
    assert with_shot["_images"][0][0] == b"\xff\xd8fake"





def test_act_sequence_screenshot_region_passthrough():
    backend = _StubBackend()
    seen: list = []
    backend.screenshot = lambda region=None, max_side=None, fmt="jpeg", quality=85: (
        seen.append((region, max_side)), (b"x", {"format": "jpeg"}))[1]
    Coordinator(backend=backend).act_sequence(
        [{"op": "screenshot", "region": [1, 2, 30, 40], "max_side": 640}])
    assert seen == [((1, 2, 30, 40), 640)]





def test_every_seq_op_is_actually_dispatchable():
    """
    `_SEQ_OPS` 里的每一个 op 都必须**真的能执行**（M-50）。

    原先这里的测试是 `assert "screenshot" in Coordinator._SEQ_OPS` —— 纯粹断言「常量里
    有这个字符串」，行为早已由上面几条覆盖。换成「逐个真跑一遍、不得报『未知 op』」，
    它才拦得住那个真正会出事的改动：**往元组里加了 op、却忘了在 `_run_seq_step` 里实现它**
    （模型照着工具描述传了进来，只会拿到一句「未知 op」）。
    """
    from computer_use_mcp.utils.errors import ComputerUseError

    backend = _StubBackend()
    backend.screenshot = lambda region=None, max_side=None, fmt="jpeg", quality=85: (
        b"x", {"format": fmt})
    coord = Coordinator(backend=backend)
    coord.screenshot_image = lambda *a, **k: (b"x", {"format": "jpeg"})

    samples = {
        "click": {"ref": 1},
        "type": {"text": "hi"},
        "key": {"combo": "Return"},
        "wait": {"title_contains": "x", "timeout": 0.01},
        "sleep": {"seconds": 0},
        "list_windows": {},
        "screenshot": {},
    }
    assert set(samples) == set(Coordinator._SEQ_OPS), \
        "新增 op 时请同时补上这里的样例，否则它逃出本用例的覆盖"

    for op in Coordinator._SEQ_OPS:
        step = {"op": op, **samples[op]}
        try:
            coord._run_seq_step(op, step)
        except ComputerUseError as exc:
            assert "未知 op" not in str(exc), f"_SEQ_OPS 声明了 {op}，但 _run_seq_step 没实现它"
