# -*- coding: utf-8 -*-
"""
`core/coordinator` + `backend/base` 的回归集（REVIEW 第四节 M-16 ~ M-19、M-28）。

全部用例都是**纯内存**的：不碰桌面、不起沙箱、不跑 xdotool。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.backend.base import (  # noqa: E402
    Backend,
    ElementDetail,
    QueryResult,
    Rect,
    TextBlock,
)
from computer_use_mcp.backend.linux import backend as backend_mod  # noqa: E402
from computer_use_mcp.backend.linux.backend import LinuxBackend  # noqa: E402
from computer_use_mcp.backend.linux.inject import XdotoolInjector  # noqa: E402
from computer_use_mcp.core import display as display_mod  # noqa: E402
from computer_use_mcp.core.coordinator import Coordinator  # noqa: E402
from computer_use_mcp.utils.errors import (  # noqa: E402
    LEVEL_KEY,
    LEVEL_NONE,
    ComputerUseError,
)


# ============================ 桩 ============================

class _StubBackend(Backend):
    """最小 backend 桩：只实现本文件用得到的那几条。"""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.type_ok = True
        self.key_ok = True

    def is_available(self): return True
    def list_apps(self): return []
    def get_tree(self, *a, **k): return QueryResult()
    def find(self, *a, **k): return QueryResult()
    def element_info(self, n): return ElementDetail(ref=0, role="label", name="x")
    def is_alive(self, n): return True
    def focus(self, n): return True
    def invoke(self, n, action=None): return False
    def set_value(self, n, t): return False
    def element_screen_rect(self, n): return None
    def click_at(self, x, y, button=1, focus_window=True, repeat=1, delay_ms=100): return True
    def drag_at(self, x1, y1, x2, y2, button=1, steps=10, focus_window=True): return True
    def type_text(self, t): self.calls.append(("type", t)); return self.type_ok
    def press_key(self, c): self.calls.append(("key", c)); return self.key_ok
    def list_windows(self, limit=100): return []
    def wait_window(self, *a, **k): return None
    def window_screen_pos(self, n): return None
    def screen_layout(self): return {}
    def screenshot(self, *a, **k): return b"", {}
    def active_window_rect(self): return None
    def read_text(self, region=None, min_conf=40.0): return []
    def active_window_title(self): return "Stub"
    def describe_point(self, x, y, with_text=False): return {"x": x, "y": y}


@pytest.fixture
def _real_mode(monkeypatch):
    """不开沙箱：这些用例只验证编排逻辑，不需要虚拟屏。"""
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    return monkeypatch


# ============================ M-18：level 语义 ============================

def test_click_element_not_found_reports_none_not_screenshot(_real_mode):
    """
    M-18①：「没找到元素」时 level 必须是 none。

    历史实现报 `level=LEVEL_SCREENSHOT`，于是 `to_text()` 打印「❌ 失败（层级=screenshot）」——
    而这一路上**截图根本没被尝试过**（元素级没做、坐标级没点、图也没截）。模型读到
    「层级=screenshot」会以为截图那条路已经试过且失败，而「改用截图」其实只是**建议**。
    """
    coord = Coordinator(backend=_StubBackend())
    r = coord.click(text="不存在的按钮")

    assert not r.ok
    assert r.level == LEVEL_NONE, f"没有任何一级生效时必须报 none，实际 {r.level}"
    assert "层级=screenshot" not in r.to_text(), r.to_text()
    # 建议仍要在（只是不占 level 字段）
    assert "screenshot" in r.to_text()


def test_click_all_levels_failed_reports_none(_real_mode):
    """M-18①（第二种）：元素级与坐标级都失败——截图同样没被尝试，level 仍是 none。"""
    coord = Coordinator(backend=_StubBackend())
    blk = TextBlock(text="保存", rect=Rect(10, 10, 20, 20), conf=90)
    ref = coord.refs.register(blk, role="text-block", name="保存", app="ocr")

    r = coord.click(ref=ref)   # 桩的 invoke 恒 False、element_screen_rect 恒 None
    assert not r.ok
    assert r.level == LEVEL_NONE, f"实际 {r.level}；截图没被尝试就不能报 screenshot"


def test_type_text_reports_key_level_not_coord(_real_mode):
    """
    M-18②：键盘注入的层级是 `key`，不是 `coord`。

    借用 LEVEL_COORD（「坐标点击」）会让 attempts 的自动分析把两类完全不同的通道混在一起
    —— 键盘注入根本没有落点坐标可言。
    """
    be = _StubBackend()
    coord = Coordinator(backend=be)
    r = coord.type_text("hello")

    assert r.ok and r.level == LEVEL_KEY, f"实际 {r.level}"
    assert all(a["level"] != "coord" for a in r.attempts), r.attempts


def test_press_key_reports_key_level(_real_mode):
    """M-18②：press_key 同理。"""
    coord = Coordinator(backend=_StubBackend())
    r = coord.press_key("ctrl+s")
    assert r.ok and r.level == LEVEL_KEY, f"实际 {r.level}"


def test_failed_key_injection_reports_none(_real_mode):
    """M-18② 反向：注入失败时不能报 key（那一级并没有生效）。"""
    be = _StubBackend()
    be.type_ok = False
    coord = Coordinator(backend=be)
    r = coord.type_text("hello")
    assert not r.ok and r.level == LEVEL_NONE, f"实际 {r.level}"


# ============================ M-17：act_sequence 异常口径 ============================

def test_act_sequence_uses_friendly_text_for_unexpected_exception(_real_mode):
    """
    M-17：step 抛出的**非** ComputerUseError，回给模型的文案要与其余工具同口径
    （「内部错误 <类型>: <原因>」，堆栈只进服务端日志），而不是裸 `str(exc)`。
    """
    coord = Coordinator(backend=_StubBackend())

    def boom(op, step):
        raise RuntimeError("xdotool 退出码 1：Error: 无法打开 display")

    coord._run_seq_step = boom
    out = coord.act_sequence([{"op": "click", "ref": 1}])

    msg = out["steps"][0]["message"]
    assert not out["ok"] and out["stopped_at"] == 0
    assert "内部错误 RuntimeError" in msg, f"应与其余工具同口径，实际：{msg!r}"


def test_act_sequence_keeps_reason_for_computer_use_error(_real_mode):
    """M-17 反向：ComputerUseError 本就是写给模型看的，原因要**原样**保留、不加「内部错误」。"""
    coord = Coordinator(backend=_StubBackend())

    def boom(op, step):
        raise ComputerUseError("ref 已失效，界面可能已变化")

    coord._run_seq_step = boom
    out = coord.act_sequence([{"op": "click", "ref": 1}])

    msg = out["steps"][0]["message"]
    assert "ref 已失效" in msg and "内部错误" not in msg, msg


# ============================ M-19：Rect.is_empty 的判据 ============================

def test_rect_is_empty_is_about_invalid_size_not_small_size():
    """
    M-19：`is_empty()` 的注释与实现必须一致 —— 判据是「尺寸非法」，**不是**「面积小」。

    1x1 的隐形辅助窗口 w/h 都 > 0，故**不算空**（它的中心仍是合法注入坐标）。
    注释原先举 1x1 为例说「无面积」，与实现相反，会让维护者误以为注入闸门会挡掉小窗。
    """
    assert Rect(0, 0, 0, 0).is_empty() is True
    assert Rect(0, 0, -1, 10).is_empty() is True     # 读矩形失败的典型形态
    assert Rect(5, 5, 10, 5).is_empty() is False
    assert Rect(5, 5, 1, 1).is_empty() is False, "1x1 隐形辅助窗不属于「空」，见 docstring"

    # 判据要能区分「注释说了 1x1」和「注释**说清了 1x1 不算空**」——只断言出现 "1x1"
    # 的话，那句与实现相反的旧注释（「无面积（如 1x1 隐形辅助窗口…）」）照样能通过。
    assert "不算空" in Rect.is_empty.__doc__, \
        "docstring 必须写明 1x1 不算空，否则注释又会与实现相漂（M-19）"


# ============================ M-28：类型与元数据 ============================

def test_textblock_detail_does_not_advertise_click_action():
    """
    M-28②：TextBlock 的 ElementDetail **不得**声称有 `click` 动作。

    `invoke()` 对 TextBlock 恒返回 False（它只是个「文字 + 坐标」），而历史实现却报
    `actions=["click"]` —— 模型看到「有 click 动作」就去走元素级点击，拿到的是
    「元素级失败，坐标点击已执行」，凭空多一轮且会怀疑自己用错了 ref。
    """
    be = LinuxBackend.__new__(LinuxBackend)
    d = be.element_info(TextBlock(text="确定", rect=Rect(1, 2, 3, 4), conf=90))

    assert d.role == "text-block" and d.name == "确定"
    assert not d.actions, f"TextBlock 没有元素级动作，不得虚报：{d.actions}"
    assert "click" not in d.to_text()


def test_list_windows_annotation_is_parameterized():
    """M-28①：`list[dict]` 丢了类型信息，应为 `list[dict[str, Any]]`。"""
    import typing

    hints = typing.get_type_hints(Backend.list_windows)
    assert hints["return"] == list[dict[str, typing.Any]], hints["return"]
    assert typing.get_type_hints(LinuxBackend.list_windows)["return"] == list[dict[str, typing.Any]]


# ============================ M-16：无名弹层的校准基准 ============================

def _injector(run_stdout_by_args: dict[tuple, str], rects: dict[str, Rect]) -> XdotoolInjector:
    """构造一个不碰 xdotool 的 injector：_run 与 window_geometry 都按预置表返回。"""
    inj = XdotoolInjector.__new__(XdotoolInjector)
    inj._run = lambda args, *a, **k: types.SimpleNamespace(
        stdout=run_stdout_by_args.get(tuple(args), ""), returncode=0)
    inj.window_geometry = lambda wid: rects.get(wid)
    return inj


def test_window_pos_by_pid_match_reports_title_hit():
    """标题命中 → 基准可信（'title'）。"""
    inj = _injector({("search", "--pid", "42", "--name", "主窗口"): "100",
                     ("search", "--pid", "42"): "100 200"},
                    {"100": Rect(0, 0, 800, 600), "200": Rect(10, 10, 50, 50)})
    rect, quality = inj.window_screen_pos_by_pid_match(42, "主窗口")
    assert quality == "title" and rect == Rect(0, 0, 800, 600)


def test_window_pos_by_pid_match_flags_unnamed_top_window():
    """
    M-16：顶层窗口无名（弹出菜单/下拉浮层的常见形态）→ 基准退化成「按 pid 取面积最大者」，
    必须标成 'pid_fuzzy' 让调用方知道这份基准不可信。
    """
    inj = _injector({("search", "--pid", "42"): "100 200"},
                    {"100": Rect(0, 0, 800, 600),      # 应用主窗口（面积最大 → 会被选中）
                     "200": Rect(900, 900, 120, 80)})  # 无名浮层
    rect, quality = inj.window_screen_pos_by_pid_match(42, None)
    assert quality == "pid_fuzzy"
    assert rect == Rect(0, 0, 800, 600), "退化分支取的是面积最大者（这里正是主窗口）"


def test_window_pos_by_pid_match_none_when_no_window():
    inj = _injector({("search", "--pid", "42"): ""}, {})
    assert inj.window_screen_pos_by_pid_match(42, None) == (None, "none")


def _backend_with(reader, injector) -> LinuxBackend:
    be = LinuxBackend.__new__(LinuxBackend)
    be.reader = reader
    be.injector = injector
    be.ensure_available = lambda: None
    return be


def _fake_reader(screen: Rect | None, rel: Rect, title: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        get_extents=lambda n, coord: screen if coord == "SCREEN" else rel,
        top_window_of=lambda n: "top",
        get_name=lambda n: title,
        get_process_id=lambda n: 42,
    )


def test_element_screen_rect_warns_when_fuzzy_baseline_meets_disjoint_screen(monkeypatch):
    """
    M-16 的**观察点**：SCREEN 与窗口矩形不相交（→ 改用校准值）**且**窗口基准是模糊得来时，
    必须留下一条可诊断的告警 —— 这条路径历史上是完全沉默的，排查「坐标点偏」无从下手。
    """
    seen: list[str] = []
    monkeypatch.setattr(backend_mod.log, "warning",
                        lambda msg, *a, **k: seen.append(msg % a if a else msg))

    inj = _injector({("search", "--pid", "42"): "100"}, {"100": Rect(0, 0, 800, 600)})
    be = _backend_with(_fake_reader(Rect(900, 900, 10, 10), Rect(5, 5, 10, 10), title=""),
                       inj)

    rect = be.element_screen_rect(object())

    assert rect == Rect(5, 5, 10, 10), "校准值本身不变（本项只加观察，不改决策）"
    assert seen and "坐标校准基准可疑" in seen[0], seen


def test_element_screen_rect_stays_quiet_when_baseline_is_exact(monkeypatch):
    """
    M-16 反向：标题精确命中时**不得**告警，否则这条告警会在每次正常坐标点击时刷屏，
    真出问题时反而被淹没。
    """
    seen: list[str] = []
    monkeypatch.setattr(backend_mod.log, "warning",
                        lambda msg, *a, **k: seen.append(msg % a if a else msg))

    inj = _injector({("search", "--pid", "42", "--name", "主窗口"): "100"},
                    {"100": Rect(0, 0, 800, 600)})
    be = _backend_with(_fake_reader(Rect(900, 900, 10, 10), Rect(5, 5, 10, 10), title="主窗口"),
                       inj)

    be.element_screen_rect(object())
    assert seen == [], f"基准可信时不该告警：{seen}"