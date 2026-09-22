# -*- coding: utf-8 -*-
"""
`tools/` 与 `server.py` 的回归集（REVIEW 第四节 M-40 ~ M-47）。

大部分用例是**纯内存**的（直接调工具闭包或 coordinator）；涉及 MCP 协议层的部分用
真 `ToolError` 类型断言，不跑 stdio 往返。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.core import display as display_mod  # noqa: E402
from computer_use_mcp.core.coordinator import Coordinator  # noqa: E402
from computer_use_mcp.utils.errors import (  # noqa: E402
    ComputerUseError,
    ToolError,
    to_friendly_text,
    to_tool_error,
)


# ==================== 桩 ====================

class _StubBackend:
    """只提供本文件用得到的形状（不继承 ABC，避免写一堆无关方法）。"""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def is_available(self): return True
    def list_apps(self): return []
    def screen_layout(self): return {}
    def list_windows(self, limit=100): return []
    def click_at(self, x, y, button=1, focus_window=True, **kw):
        self.calls.append(("click", x, y)); return True
    def drag_at(self, x1, y1, x2, y2, button=1, **kw): return True
    def element_screen_rect(self, n): return None
    def invoke(self, n, action=None): return False
    def screenshot(self, *a, **k): return b"x", {"format": "jpeg"}
    def active_window_title(self): return "Stub"


def _coord(monkeypatch, backend=None) -> Coordinator:
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    return Coordinator(backend=backend or _StubBackend())


# ==================== M-40：click 的 x/y 半给 ====================

def test_coordinator_seq_click_rejects_half_given_xy(monkeypatch):
    """
    M-40：act_sequence 的 click 步只给 x 或只给 y 时**当场报错**。

    历史行为是落进元素分支，最后回一句「未找到目标元素…建议改用 screenshot」——
    对「少给一个分量」这个错误反馈完全指错方向，模型会去排查元素树、白多一个来回。
    """
    coord = _coord(monkeypatch)

    with pytest.raises(ComputerUseError, match="x 与 y 必须同时给出"):
        coord._run_seq_step("click", {"op": "click", "x": 100})
    with pytest.raises(ComputerUseError, match="x 与 y 必须同时给出"):
        coord._run_seq_step("click", {"op": "click", "y": 200})


def test_coordinator_seq_click_still_accepts_complete_xy(monkeypatch):
    """M-40 反向：完整的一对坐标仍要走裸坐标点击（别把修复做成「一律拒绝」）。"""
    be = _StubBackend()
    coord = _coord(monkeypatch, be)
    coord._sandbox_guard = lambda: None
    coord._point_evidence = lambda *a, **k: {}
    coord._landing = lambda *a, **k: {}

    r = coord._run_seq_step("click", {"op": "click", "x": 10, "y": 20})

    assert r["ok"] and ("click", 10, 20) in be.calls, be.calls


def test_tool_click_rejects_half_given_xy():
    """M-40 同样落在 tools/action.py 的 click 上（两个入口都必须拦）。"""
    import inspect

    from computer_use_mcp.tools import action as action_mod

    src = inspect.getsource(action_mod.register)
    assert "(x is None) != (y is None)" in src, \
        "tools/action.py::click 也必须拦「只给一个分量」，否则反查方向仍然指错"
    assert "不能混用" in src, "ref/text/role/action 与 x/y 混用时也要报错，别静默优先 x/y"


# ==================== M-41：参数校验 ====================

def test_scope_params_are_literals():
    """
    M-41：`scope` 这类枚举参数必须收成 `Literal[...]`，让 pydantic 在参数校验阶段打回。

    原先写 `str`，拼错（如 "scren"）会被**静默降级**成另一种 scope —— 模型拿到的是
    另一块区域的结果，却完全无从察觉。
    """
    import typing

    from computer_use_mcp.tools import screen_text as st_mod
    from computer_use_mcp.tools import ui_tree as ut_mod

    hints_ut = typing.get_type_hints(ut_mod.register.__code__.co_consts and (lambda: None) or (lambda: None))
    del hints_ut  # 上面只是为了保持写法一致；真正的断言走源码级检查
    for mod, name in ((ut_mod, "get_ui_tree"), (st_mod, "get_screen_text")):
        src = __import__("inspect").getsource(mod.register)
        assert f'scope: Literal[' in src, f"{mod.__name__} 的 scope 必须收成 Literal"


def test_region_length_is_validated_not_silently_dropped():
    """
    M-41：`region` 长度 ≠ 4（含传 3 个数的常见笔误）原先被**静默丢成全屏** ——
    不只是慢（全屏 OCR 8~10s vs 对话框 0.3s），更糟的是模型以为在看指定区域。
    """
    import inspect

    from computer_use_mcp.tools import screenshot as ss_mod
    from computer_use_mcp.tools import screen_text as st_mod

    for mod in (ss_mod, st_mod):
        src = inspect.getsource(mod.register)
        assert "len(region) != 4" in src, f"{mod.__name__} 必须校验 region 长度并报错"


def test_coordinator_seq_screenshot_validates_region_length(monkeypatch):
    """act_sequence 的 screenshot 步同样要校验（原先也静默丢成全屏）。"""
    coord = _coord(monkeypatch)
    with pytest.raises(ComputerUseError, match="region 需为"):
        coord._run_seq_step("screenshot", {"op": "screenshot", "region": [1, 2, 3]})


# ==================== M-42：backend 初始化必须兜住所有异常 ====================

def test_make_backend_survives_unexpected_exception(monkeypatch):
    """
    M-42：`_make_backend()` 只捕 `BackendUnavailableError` 不够 —— `get_backend()` 内部
    还有一串**模块导入**，导入链上任何别的异常都会带 traceback 在 import 阶段崩掉进程，
    `_UnavailableBackend` 根本来不及启用，与「失败时保证 server 可启动」的承诺相反。
    """
    import computer_use_mcp.server as srv

    def boom(*a, **k):
        raise RuntimeError("导入 backend 时炸了（模拟缺依赖/循环导入）")

    monkeypatch.setattr(srv, "get_backend", boom)

    backend = srv._make_backend()
    assert backend.name == "unavailable"
    with pytest.raises(Exception):
        backend.list_apps()


def test_unavailable_backend_still_reports_reason(monkeypatch):
    """占位 backend 要把原因说清楚（否则模型只看到一句无信息量的失败）。"""
    import computer_use_mcp.server as srv

    monkeypatch.setattr(srv, "get_backend",
                        lambda: (_ for _ in ()).throw(RuntimeError("缺 libatspi")))
    backend = srv._make_backend()
    with pytest.raises(Exception, match="缺 libatspi"):
        backend.list_apps()


# ==================== M-43：selftest 不得触碰宿主总线 ====================

def test_selftest_skips_at_spi_when_sandbox_down(monkeypatch, capsys):
    """
    M-43：isolated 模式而沙箱没起来时，selftest **不得**调 `is_available()`
    （那次触碰会落到宿主总线）；改用 X11 通道判活。
    """
    import computer_use_mcp.server as srv

    touched: list[str] = []

    class _Be:
        name = "linux-atspi-xdotool"

        def is_available(self):
            touched.append("at-spi")
            return True

        def list_apps(self):
            return []

        def screen_layout(self):
            return {}

    monkeypatch.setattr(srv, "_make_backend", lambda: _Be())
    monkeypatch.setattr(srv.display.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(srv.display.MANAGER, "is_sandbox_up", lambda: False)
    monkeypatch.setattr(srv, "_x11_liveness", lambda: True)
    monkeypatch.setattr(srv.display, "start", lambda: {"stub": True})  # 别真起 Xephyr

    rc = srv._selftest()

    assert rc == 0
    assert touched == [], f"沙箱未就绪时不得触碰 AT-SPI，实际：{touched}"
    assert "跳过 AT-SPI 判活" in capsys.readouterr().out


def test_selftest_uses_at_spi_when_sandbox_is_up(monkeypatch):
    """M-43 反向：沙箱就绪时仍走 AT-SPI 判活（那是自检的主要价值，不能一并砍掉）。"""
    import computer_use_mcp.server as srv

    touched: list[str] = []

    class _Be:
        name = "linux-atspi-xdotool"

        def is_available(self):
            touched.append("at-spi")
            return False          # 判死 → selftest 应返回 2

        def list_apps(self):
            return []

    monkeypatch.setattr(srv, "_make_backend", lambda: _Be())
    monkeypatch.setattr(srv.display.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(srv.display.MANAGER, "is_sandbox_up", lambda: True)
    monkeypatch.setattr(srv.display, "start", lambda: {"stub": True})  # 别真起 Xephyr

    assert srv._selftest() == 2
    assert touched == ["at-spi"], f"沙箱就绪时应走 AT-SPI 判活、且只探一次：{touched}"


# ==================== M-44：_bootstrap 标志不得是值快照 ====================

def test_bootstrap_flags_are_read_live():
    """
    M-44：`from ..._bootstrap import ATSPI_IMPORT_ERROR` 是**值快照** —— 导入 backend 的
    那一刻把当时的 None 绑进来，之后 `import_atspi()` 对模块全局的重新赋值不会反映过来。
    必须改成 `from ... import _bootstrap` + 属性访问。
    """
    import inspect

    from computer_use_mcp.backend.linux import backend as be_mod

    src = inspect.getsource(be_mod)
    assert "from ..._bootstrap import" not in src, "不得再用值快照式导入"
    assert "_bootstrap.ATSPI_IMPORT_ERROR" in src, "应改为经模块属性实时读取"

    # 行为判据：改了 _bootstrap 的全局，backend 那句格式化字符串里读到的要跟着变
    from computer_use_mcp import _bootstrap
    original = _bootstrap.ATSPI_IMPORT_ERROR
    try:
        _bootstrap.ATSPI_IMPORT_ERROR = "哨兵错误"
        be = be_mod.LinuxBackend.__new__(be_mod.LinuxBackend)
        be.reader = types.SimpleNamespace(error=None, is_available=lambda: False)
        try:
            be.ensure_available()
        except Exception as exc:  # noqa: BLE001
            assert "哨兵错误" in str(exc), f"应读到实时值，实际：{exc}"
    finally:
        _bootstrap.ATSPI_IMPORT_ERROR = original


# ==================== M-45：build.sh 的 spec 名 ====================

def test_build_sh_cleans_the_actual_spec():
    """
    M-45：PyInstaller 生成的 spec 是 `name + '.spec'`，本仓库 `--name computer-use-mcp-bin`
    → 要清的是 **-bin.spec**。历史上那行只写了 `computer-use-mcp.spec`（该文件从来不存在，
    等于清理没做）。
    """
    import pathlib
    import re

    sh = (pathlib.Path(__file__).resolve().parents[1] / "build.sh").read_text(encoding="utf-8")
    # 只看**非注释行**：注释里也会出现 `--name`（那是解释文字，不是真正的参数）
    code = "\n".join(l for l in sh.splitlines() if not l.lstrip().startswith("#"))

    names = set(re.findall(r"--name\s+([A-Za-z0-9][\w.-]*)", code))
    assert names, "build.sh 里应有 --name"
    for n in names:
        assert re.search(rf"rm -rf[^\n]*\b{re.escape(n)}\.spec", code), \
            f"--name {n} 对应的 {n}.spec 没有被 rm 清理"


# ==================== M-46：act_sequence 的规模上限 ====================

def test_act_sequence_rejects_too_many_steps(monkeypatch):
    """M-46：整串是**持屏锁**执行的，步数必须有界。"""
    coord = _coord(monkeypatch)
    steps = [{"op": "sleep", "seconds": 0}] * (coord._SEQ_MAX_STEPS + 1)

    with pytest.raises(ComputerUseError, match="最多"):
        coord.act_sequence(steps)


def test_act_sequence_rejects_long_sleep(monkeypatch):
    """
    M-46：`steps=[{"op":"sleep","seconds":3600}]` 会**持屏锁**睡一小时。
    以单会话自伤为主（别人会立刻拿到 ScreenBusyError 而非排队），但没有理由留着。
    """
    coord = _coord(monkeypatch)

    out = coord.act_sequence([{"op": "sleep", "seconds": 3600}])

    assert out["ok"] is False and out["stopped_at"] == 0
    assert "超过单步上限" in out["steps"][0]["message"]


def test_act_sequence_rejects_too_long_wait(monkeypatch):
    """M-46：wait.timeout 同样要有上限（它也是持锁等）。"""
    coord = _coord(monkeypatch)

    out = coord.act_sequence([{"op": "wait", "timeout": 3600}])

    assert out["ok"] is False and "超过单步上限" in out["steps"][0]["message"]


def test_act_sequence_rejects_too_many_screenshots(monkeypatch):
    """M-46：序列内截图张数上限 —— 每张都会变成响应里的一个 image content block。"""
    coord = _coord(monkeypatch)
    coord.screenshot_image = lambda *a, **k: (b"x", {"format": "jpeg"})

    steps = [{"op": "screenshot"}] * (coord._SEQ_MAX_SCREENSHOTS + 1)
    out = coord.act_sequence(steps, stop_on_error=False)

    assert out["ok"] is False
    assert "截图步数超过上限" in out["steps"][-1]["message"]


def test_act_sequence_normal_sizes_still_work(monkeypatch):
    """M-46 反向：正常规模的批量任务不得被误伤。"""
    coord = _coord(monkeypatch)
    coord.screenshot_image = lambda *a, **k: (b"x", {"format": "jpeg"})

    out = coord.act_sequence([
        {"op": "sleep", "seconds": 0},
        {"op": "list_windows"},
        {"op": "screenshot"},
    ])

    assert out["ok"] is True, out


# ==================== M-47：兜底异常抛 ToolError ====================

def test_tool_error_carries_the_same_text_as_friendly_text():
    """
    M-47：`to_tool_error` 的文本必须与 `to_friendly_text` **完全一致** ——
    契约是「只多一个 is_error 标记，模型侧信息量不变」。
    """
    unexpected = RuntimeError("xdotool 退出码 1")
    err = to_tool_error(unexpected, "点击失败")

    assert isinstance(err, ToolError), f"应为 SDK 的 ToolError，实际 {type(err)}"
    assert str(err) == to_friendly_text(unexpected, "点击失败")


class _FakeMCP:
    """最小 MCP 桩：`@mcp.tool(...)` 原样返回函数，并把注册结果收起来供调用。"""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, name=None, description=None):
        def deco(fn):
            self.tools[name] = fn
            return fn
        return deco


class _BoomCoord:
    """coordinator 桩：click 一律抛出指定异常。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def click(self, **kw):
        raise self._exc

    def click_xy(self, *a, **k):
        raise self._exc


def _click_tool(exc: Exception):
    from computer_use_mcp.tools import action as action_mod

    mcp = _FakeMCP()
    action_mod.register(mcp, _BoomCoord(exc))
    return mcp.tools["click"]


def test_unexpected_exception_is_raised_as_tool_error():
    """
    M-47（行为判据）：非预期异常必须**抛 ToolError**，而不是返回普通文本。

    为什么这一条要看行为而不是源码：返回文本时协议层 `isError=false`，客户端无法按它
    统计或触发重试，日志里失败与成功调用也分不开 —— 而 SDK 对 ToolError 的处理是
    `CallToolResult(content=[Text(…)], is_error=True)`，文本照样原样给模型。判据落在
    「抛出的是什么类型、文本是什么」，才挡得住各种等价改写。
    """
    click = _click_tool(RuntimeError("xdotool 退出码 1"))

    with pytest.raises(ToolError) as ei:
        click(text="确定")

    assert "内部错误 RuntimeError" in str(ei.value), str(ei.value)


def test_computer_use_error_still_returns_text_not_tool_error():
    """
    M-47 折中版的**边界**：`except ComputerUseError` 那条仍是普通文本返回。

    CLAUDE.md 明确定义 tools 层只做「参数解析 + 错误转友好文本」，把可预期失败也改成
    抛异常等于把既定错误契约推倒重来；缺的只是笼统兜底那一半。
    """
    click = _click_tool(ComputerUseError("未找到目标元素"))

    out = click(text="确定")           # 不抛，返回文本

    assert out.startswith("❌ 点击失败：") and "未找到目标元素" in out, out


def test_tool_error_is_importable_from_errors():
    """`utils/errors` 必须把 ToolError 再导出（工具层靠它，且要兼容 mcp 1.x/2.x）。"""
    from computer_use_mcp.utils import errors as err_mod

    assert err_mod.ToolError is ToolError