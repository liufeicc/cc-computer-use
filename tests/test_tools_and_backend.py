"""
tools 层契约、a11y 遍历预算、抓屏错误包装、截图落盘（拆分自 test_optimizations.py）。
"""

from __future__ import annotations

import glob
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
from computer_use_mcp.utils import temps
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




# ---------- screenshot 落盘：绝不覆盖已存在的文件（C-1）----------
def test_screenshot_to_file_refuses_to_overwrite_existing(tmp_path):
    """
    回归（C-1）：save_path 指向已存在的文件时必须**拒绝**，绝不覆盖。

    为什么值得一条测试：save_path 由模型直接给出，写的是**宿主真实文件系统**——本项目
    其余能力（点击/输入/启应用）都只动沙箱内的屏，唯独这里落到宿主磁盘。模型误猜一个
    路径（如把 ~/.bashrc 当成存放目录）即整段覆盖用户文件，写入的还是图像二进制、不可逆。
    """
    coord = Coordinator(backend=_StubBackend())
    target = tmp_path / "important.txt"
    target.write_text("用户的重要数据", encoding="utf-8")

    with pytest.raises(ComputerUseError) as ei:
        coord.screenshot_to_file(path=str(target))

    # 报错必须可操作：既说明被拒绝，也给出下一步（留空 = 存到临时目录）
    assert "不覆盖" in str(ei.value) and "save_path 留空" in str(ei.value)
    # 最关键的一条：原文件一个字节都不能变
    assert target.read_text(encoding="utf-8") == "用户的重要数据"





def test_screenshot_to_file_writes_when_target_absent(tmp_path):
    """拒绝覆盖不能误伤正常用法：目标不存在时照常写入。"""
    coord = Coordinator(backend=_StubBackend())
    target = tmp_path / "shot.jpg"

    path, meta = coord.screenshot_to_file(path=str(target))

    assert path == str(target) and target.exists() and meta["path"] == str(target)





def test_screenshot_to_file_auto_path_is_unique_and_writable(monkeypatch, tmp_path):
    """save_path 留空 → 自动生成，路径唯一且可写（不发生任何覆盖）。"""
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    coord = Coordinator(backend=_StubBackend())
    p1, _ = coord.screenshot_to_file()
    p2, _ = coord.screenshot_to_file()
    assert p1 != p2, "两次自动落盘必须是不同文件，否则第二次就覆盖了第一次"
    assert os.path.exists(p1) and os.path.exists(p2)


# ---------- 自动路径的截图必须被回收（2026-09-18 补） ----------
#
# 为什么要专组测试：这个问题**只在长时间运行后显形** —— 单次调用永远是对的，
# 于是「写完不管」在开发期完全看不出来，代价是历次会话累积的 /tmp 垃圾
# （实测手工清掉过 118 个）。回收逻辑同时涉及**别人**的文件，删错的后果
# 比不删更糟，故判据必须是双向的：该删的删了、不该删的一个没动。

def _shots(tmp_path, family: str = "cc-cu-shot"):
    return sorted(glob.glob(str(tmp_path / f"{family}-*")))


def test_auto_path_screenshots_are_pruned_to_newest_n(monkeypatch, tmp_path):
    """
    连拍 12 张自动路径截图 → 只留最近 5 张，且**刚回报给模型的那张必须在**。

    红线：把 `screenshot_to_file` 里那次 `prune_temp_images` 调用删掉，本条即失败
    （12 个文件全留）。
    """
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    backend = _StubBackend()
    # 桩默认返回**空字节**，那样「文件是否真写进去了」就无从判断。给一张可识别的假图，
    # 顺带钉住新写法（先 mkstemp 建文件、再由调用方写内容）没有退化成「建了个空文件」。
    backend.screenshot = lambda *a, **k: (b"\xff\xd8FAKEJPEG", {"format": "jpeg"})
    coord = Coordinator(backend=backend)

    last = None
    for _ in range(12):
        last, _meta = coord.screenshot_to_file()

    left = _shots(tmp_path)
    assert len(left) == temps.DEFAULT_KEEP, f"应收窄到 {temps.DEFAULT_KEEP} 张，实际 {len(left)}"
    assert last in left, "最新那张正是回报给模型的路径，绝不能被回收掉"
    assert open(last, "rb").read() == b"\xff\xd8FAKEJPEG", "落盘内容必须就是那张图"


def test_prune_keeps_other_sessions_files_when_their_owner_is_alive(monkeypatch, tmp_path):
    """
    **别的会话**（另一个活着的进程）留下的截图一个都不许删。

    为什么这条比上一条更要紧：`/tmp` 是全局的，而本项目明确支持多会话并存
    （每会话一块私有沙箱屏）。若按前缀无差别裁剪，就等于把别的会话**正要用**的
    文件删了 —— 与 display 里「只清本屏号，跨屏会误杀别的会话正在用的总线」同一条理由。
    """
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    foreign = tmp_path / f"cc-cu-shot-{os.getppid()}-ffffffff.jpg"   # 父进程必然活着
    foreign.write_bytes(b"x")

    coord = Coordinator(backend=_StubBackend())
    for _ in range(12):
        coord.screenshot_to_file()

    assert foreign.exists(), "不得删除其它活着的进程名下的截图"


def test_prune_removes_leftovers_of_dead_sessions(monkeypatch, tmp_path):
    """
    主人**已死**的残留要清掉 —— 那才是 118 个文件的真正来源（会话被强杀时 atexit 不跑）。

    红线：把 `_pid_alive` 那条判据去掉（改成无差别保留别人的），本条失败。
    """
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    dead = next(p for p in range(999_999, 999_900, -1) if not os.path.isdir(f"/proc/{p}"))
    stale = tmp_path / f"cc-cu-shot-{dead}-deadbeef.jpg"
    stale.write_bytes(b"x")

    coord = Coordinator(backend=_StubBackend())
    coord.screenshot_to_file()          # 一次自动落盘就该顺手清掉

    assert not stale.exists(), "主人已死的残留必须被回收"


def test_explicit_save_path_is_never_pruned(monkeypatch, tmp_path):
    """
    用户显式指定的 `save_path` 落在回收范围**之外**：它不是我们的临时产物。

    这条守的是判据的边界 —— 裁剪按 `{family}-{pid}-` 匹配并解析归属 pid，**解析不出
    归属的文件一律不动**。这里刻意把显式路径起成 `cc-cu-shot-manual.jpg` 并放在**同一个
    目录**，让 glob 真的能匹配上它：若实现是「按前缀无差别裁剪」，它就会被误删。
    """
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    mine = tmp_path / "cc-cu-shot-manual.jpg"

    coord = Coordinator(backend=_StubBackend())
    path, _ = coord.screenshot_to_file(path=str(mine))
    assert path == str(mine) and mine.exists()

    for _ in range(12):                 # 再连拍 12 张自动路径，触发裁剪
        coord.screenshot_to_file()

    assert mine.exists(), "显式 save_path 的文件不许被回收"


def test_seq_fallback_dump_is_also_pruned(monkeypatch, tmp_path):
    """
    `act_sequence` 拿不到 MCP Image 类型时的退化落盘（`cc-cu-seq-*`）同样要回收。

    它比 shot 路径罕见得多，但**同类**：一次调用留一个文件。漏掉它的后果是
    「主路径干净了、边角还在漏」，而这类边角恰恰是排查时最容易忽略的。
    """
    from computer_use_mcp.tools.action import _dump_temp

    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))
    last = None
    for _ in range(12):
        last, _meta = _dump_temp(b"\xff\xd8fake", {"format": "jpeg"})

    left = _shots(tmp_path, family="cc-cu-seq")
    assert len(left) == temps.DEFAULT_KEEP, f"退化路径也要收窄，实际 {len(left)}"
    assert last in left and os.path.exists(last)


def test_prune_swallows_errors_and_never_breaks_the_screenshot(monkeypatch, tmp_path):
    """
    回收失败**绝不能**让截图这个主操作失败（与「落点证据不得让主操作失败」同一纪律）。

    这里制造一个「删除必失败」的场景：让 `os.unlink` 抛错。断言主操作照常成功返回。
    """
    monkeypatch.setattr(temps.tempfile, "gettempdir", lambda: str(tmp_path))

    def _boom(*a, **k):
        raise OSError("只读文件系统")

    monkeypatch.setattr(temps.os, "unlink", _boom)
    coord = Coordinator(backend=_StubBackend())
    path, _meta = coord.screenshot_to_file()
    assert os.path.exists(path), "回收失败时截图本身必须仍然成功"





def test_screenshot_to_file_bad_path_raises_friendly_error(tmp_path):
    """
    父目录不存在时抛 ComputerUseError（友好文案），而不是逃逸的 OSError。

    否则 OSError 不是 ComputerUseError，会穿透工具层的 except 到 MCP 层，
    模型只拿到一句无信息量的 "Error executing tool screenshot"。
    """
    coord = Coordinator(backend=_StubBackend())
    missing = tmp_path / "no_such_dir" / "a.jpg"

    with pytest.raises(ComputerUseError):
        coord.screenshot_to_file(path=str(missing))





def test_grab_rgb_wraps_backend_failure(monkeypatch):
    """回归（I-5）：mss 抓屏失败 → ComputerUseError（截图与 OCR 两条链路共同受益）。"""
    import sys
    import types

    from computer_use_mcp.backend.linux import grab as grab_mod

    class _Boom(Exception):
        pass

    class _FakeSct:
        monitors = [{"left": 0, "top": 0, "width": 100, "height": 100}]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def grab(self, mon):
            raise _Boom("X connection broken")

    fake = types.ModuleType("mss")
    fake.mss = lambda **kw: _FakeSct()
    monkeypatch.setitem(sys.modules, "mss", fake)

    with pytest.raises(ComputerUseError) as ei:
        grab_mod.grab_rgb()

    assert "抓屏失败" in str(ei.value) and "X connection broken" in str(ei.value)





def test_grab_rgb_wraps_missing_dependency(monkeypatch):
    """回归（I-5）：mss 未安装 → BackendUnavailableError（可操作提示），而非裸 ImportError。"""
    import sys

    from computer_use_mcp.backend.linux import grab as grab_mod

    monkeypatch.setitem(sys.modules, "mss", None)   # 令 import mss 抛 ImportError

    with pytest.raises(BackendUnavailableError) as ei:
        grab_mod.grab_rgb()

    assert "抓屏依赖缺失" in str(ei.value)





def test_grab_rgb_rejects_zero_size_region():
    """region 宽/高为 0 时就地报错（mss 对零尺寸区域会抛更难懂的错误）。"""
    from computer_use_mcp.backend.linux import grab as grab_mod

    with pytest.raises(ComputerUseError) as ei:
        grab_mod.grab_rgb((10, 10, 0, 0))

    assert "尺寸非法" in str(ei.value)





def test_to_friendly_text_passthrough_and_wrap():
    """
    工具层统一兜底：ComputerUseError 原样给出原因；其它异常给出「类型 + 原因」，
    完整堆栈只进服务端日志——既不丢线索，也不把内部细节暴露进对话。
    """
    assert to_friendly_text(ComputerUseError("没有该元素"), "点击失败") == "❌ 点击失败：没有该元素"

    wrapped = to_friendly_text(TypeError("boom"), "点击失败")
    assert "内部错误 TypeError" in wrapped and "boom" in wrapped





def test_every_tool_has_blanket_except():
    """
    结构性防漏：每个工具的 `except ComputerUseError` 都必须配一条统一兜底。

    为什么值得一条测试：漏掉的那条工具，一旦抛出非 ComputerUseError 异常（依赖缺失、
    参数类型错、内部 bug），模型就只能看到零信息量的 "Error executing tool X"，且
    没有任何测试会失败——正是本项目最警惕的「静默」。这条测试让漏挂变成会红的事件。

    ⚠️ 判据必须是「调用了 to_friendly_text 的处数」而不是「except Exception 的处数」：
    action.py 的 act_sequence 内部本就有裸 `except Exception:`（用于单步失败不中断整串），
    它们会把后者撑大，导致「删掉一处真兜底」也测不出来（实测踩到过）。

    ⚠️ 兜底有两种**等价**形态，都要算（M-47）：`return to_friendly_text(...)`（返回文本）
    与 `raise to_tool_error(...)`（抛 ToolError，文案由 to_friendly_text 生成）。
    后者是 M-47 折中版引入的——文本完全一致，只是额外让协议层带上 is_error=True。
    只认前者的话，这批改造会被误判成「漏挂兜底」。
    """
    import pathlib
    import re

    tools_dir = pathlib.Path(__file__).resolve().parents[1] / "src/computer_use_mcp/tools"
    missing = []
    for f in sorted(tools_dir.glob("*.py")):
        if f.name in ("__init__.py", "screen_text.py"):
            # screen_text 从一开始就是宽捕获 + 可操作提示，写法不同，不参与本约定
            continue
        text = f.read_text(encoding="utf-8")
        n_expected = len(re.findall(r"except ComputerUseError", text))
        n_blanket = len(re.findall(r"(?:to_friendly_text|to_tool_error)\(exc,", text))
        if n_blanket < n_expected:
            missing.append(
                f"{f.name}: {n_expected} 处 except ComputerUseError 只配了 {n_blanket} 处兜底")
    assert not missing, "以下工具缺少统一兜底：" + "; ".join(missing)





# ---------- 遍历上限：跨窗口共享 + 截断必须让模型看见（I-4 + I-9）----------
class _FakeNode:
    """最小 a11y 节点：只提供遍历需要的形状。"""

    def __init__(self, name: str, children: list | None = None) -> None:
        self.name = name
        self.children = list(children or [])





def _fake_reader():
    """返回一个 AtspiReader，其 gi 读取方法被替换成对 _FakeNode 的纯内存操作。"""
    from computer_use_mcp.backend.linux.atspi import AtspiReader

    r = AtspiReader()
    r.role_name = lambda o: "panel"
    r.get_name = lambda o: o.name
    r.get_actions = lambda o: []
    r.child_count = lambda o: len(o.children)
    r.child_at = lambda o, i: o.children[i] if i < len(o.children) else None
    r.get_extents = lambda o, coord: None
    return r





def test_build_tree_shares_node_budget_across_windows():
    """
    回归（I-4①）：max_nodes 是**整次调用**的上限，不是「每窗口」的上限。

    历史实现每棵树各建一个计数器，于是 desktop scope 的实际遍历量是
    `8 应用 × 全部窗口 × max_nodes`——无总量上限，且与工具描述承诺的语义不符。
    """
    r = _fake_reader()
    win1 = _FakeNode("w1", [_FakeNode(f"c{i}") for i in range(10)])
    win2 = _FakeNode("w2", [_FakeNode(f"d{i}") for i in range(10)])
    counter: dict = {"n": 0, "truncated": False}

    r.build_tree(win1, "app", max_depth=5, max_nodes=6, counter=counter)
    n_after_first = counter["n"]
    r.build_tree(win2, "app", max_depth=5, max_nodes=6, counter=counter)

    # 不共享的话第二棵树会再吃掉 6 个（合计 12）；共享则被同一个上限挡住
    assert n_after_first == 6
    assert counter["n"] <= 7, f"计数器必须跨窗口共享，实际访问了 {counter['n']} 个节点"
    assert counter["truncated"] is True, "确有节点被丢弃时必须标记截断"





def test_build_tree_no_false_truncation_when_exactly_fits():
    """刚好装到 max_nodes 且确实没有剩余节点 → 不得误报截断（否则会误导模型收窄范围）。"""
    r = _fake_reader()
    root = _FakeNode("root", [_FakeNode("a"), _FakeNode("b")])   # 共 3 个节点
    counter: dict = {"n": 0, "truncated": False}

    r.build_tree(root, "app", max_depth=5, max_nodes=3, counter=counter)

    assert counter["n"] == 3
    assert counter["truncated"] is False, "刚好装满不是截断"





def test_find_caps_app_touch_and_reports_it():
    """
    回归（I-4②）：不带 app 的搜索必须限制「触碰的应用数」，且**结果要回报给模型**。

    每触碰一个应用 = 逼它构建整棵 a11y 树；节点预算对这笔一次性成本只记 1，挡不住
    「触碰 N 个应用」。只在日志里告警等于没告警——模型会把截断误读成「没有」。
    """
    r = _fake_reader()
    apps = [object() for _ in range(50)]
    r.iter_apps = lambda: iter(apps)
    r._collect = lambda *a, **k: None      # 不真的遍历，只验证触碰计数与回报

    res = r.find(text="任意")

    assert "应用数上限" in res.notice, "触碰应用数达上限必须回报，不能只写日志"
    assert res.notice, "notice 不能为空"





def test_find_scoped_by_root_has_no_app_notice():
    """带 root（= 指定了 app）时不遍历全桌面，不该出现应用数截断提示。"""
    r = _fake_reader()
    r._collect = lambda *a, **k: None

    assert r.find(text="任意", root=object()).notice == ""





def test_get_ui_tree_surfaces_backend_notice_to_model():
    """回归（I-9）：backend 的截断说明必须出现在**返回给模型的文本**里，而不是只在日志。"""
    backend = _StubBackend()
    backend.get_tree = lambda **k: QueryResult(
        items=[], notice="⚠️ 已触及应用数上限（8 个），返回的只是部分应用")
    coord = Coordinator(backend=backend)

    text = coord.get_ui_tree(scope="desktop")

    assert "应用数上限" in text, "截断提示必须让模型看到"





def test_get_ui_tree_without_notice_is_unchanged():
    """没有截断时不得凭空多出提示（避免噪声/误报）。"""
    coord = Coordinator(backend=_StubBackend())

    text = coord.get_ui_tree()

    assert "上限" not in text and "截断" not in text





class _FakeMCP:
    """最小的 mcp 桩：捕获 register() 注册进来的工具函数，便于直接调用。"""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, name: str | None = None, description: str | None = None):
        def deco(fn):
            self.tools[name] = fn
            return fn
        return deco





def test_find_element_tool_does_not_say_not_found_when_truncated():
    """
    关键语义（I-4 + I-9）：**被截断导致的「没找到」不等于桌面上没有**。

    若不点明这一点，模型会据此断定「不存在」，转而走截图这条昂贵得多的路。
    """
    from computer_use_mcp.tools import find as find_tool

    backend = _StubBackend()
    backend.find = lambda **k: QueryResult(
        items=[], notice="⚠️ 搜索已触及应用数上限（8 个应用）")
    mcp = _FakeMCP()
    find_tool.register(mcp, Coordinator(backend=backend))

    out = mcp.tools["find_element"](text="这个词搜不到")

    assert "应用数上限" in out
    assert "不代表桌面上没有" in out, f"被截断时不能说「未找到」，实际输出：{out}"





def test_find_element_tool_says_not_found_only_when_result_complete():
    """结果完整（无截断）时，才可以说「未找到匹配元素」。"""
    from computer_use_mcp.tools import find as find_tool

    backend = _StubBackend()
    backend.find = lambda **k: QueryResult()
    mcp = _FakeMCP()
    find_tool.register(mcp, Coordinator(backend=backend))

    out = mcp.tools["find_element"](text="这个词搜不到")

    assert "未找到匹配元素" in out
    assert "不代表桌面上没有" not in out, "结果完整时不该出现「可能被截断」的措辞"





def test_find_element_tool_lists_results_with_notice_prefixed():
    """有结果但被截断时：结果照常列出，截断提示前置。"""
    from computer_use_mcp.backend.base import Element
    from computer_use_mcp.tools import find as find_tool

    backend = _StubBackend()
    backend.find = lambda **k: QueryResult(
        items=[object()], notice="⚠️ 搜索已触及应用数上限（8 个应用）")
    backend.element_info = lambda n: Element(ref=0, role="push button", name="确定")
    mcp = _FakeMCP()
    find_tool.register(mcp, Coordinator(backend=backend))

    out = mcp.tools["find_element"](text="确定")

    assert "找到 1 个候选元素" in out
    assert out.index("应用数上限") < out.index("找到 1 个候选元素"), "截断提示应在最前面"
