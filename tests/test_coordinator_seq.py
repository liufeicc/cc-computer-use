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
    coord.get_ui_tree = lambda **k: "TREE"

    samples = {
        "click": {"ref": 1},
        "double_click": {"x": 1, "y": 2},
        "type": {"text": "hi"},
        "key": {"combo": "Return"},
        "wait": {"title_contains": "x", "timeout": 0.01},
        "sleep": {"seconds": 0},
        "list_windows": {},
        "ui_tree": {},
        "scroll": {"direction": "down", "amount": 1, "x": 1, "y": 2},
        "drag": {"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4},
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


# ---------- 方向三：候选名列表（「不确定它叫什么」不再需要两次往返）----------
def _find_stub(existing: set[str]):
    """返回 (backend.find 桩, 搜过的名字列表)；桩只对 existing 里的名字给出命中。"""
    searched: list[str] = []

    def fake_find(text=None, role=None, app=None, interactive_only=True, limit=None):
        searched.append(text)
        return QueryResult(items=[object()] if text in existing else [])

    return fake_find, searched


def test_seq_click_candidate_names_stops_at_first_hit():
    """
    候选名列表：**命中即停**，后面的候选一次都不搜（这是本机制的核心判据）。

    为什么不是「失败就试下一个」—— 中文界面上第一个候选「保存」已经点中、对话框都关了，
    程序若还接着去找「Save」，运气好是白搜一次，运气差就点到别的窗口上。候选列表表达的
    是「同一个意图的几种写法」，故找到了就不该再看后面的。
    只断言 `ok is True` 拦不住这个错误：把实现改成「全都搜一遍、取第一个成功的」照样能过，
    所以判据必须是「搜了哪些」本身。
    """
    backend = _StubBackend()
    backend.is_alive = lambda n: True      # 让 _resolve_native 走快路径，不触发重定位
    fake_find, searched = _find_stub({"保存"})
    backend.find = fake_find
    coord = Coordinator(backend=backend)

    r = coord.act_sequence([{"op": "click", "text": ["保存", "Save", "另存为"]}])
    assert r["ok"] is True
    assert searched == ["保存"], "命中之后不该再搜后面的候选"
    assert "保存" in r["steps"][0]["message"], "命中的是哪个名字必须回报给模型"


def test_seq_click_candidate_names_all_missing_lists_every_try():
    """候选全都不存在：该步失败，且**试过哪些名字要全部列出**。"""
    backend = _StubBackend()
    fake_find, searched = _find_stub(set())
    backend.find = fake_find
    coord = Coordinator(backend=backend)

    r = coord.act_sequence([{"op": "click", "text": ["保存", "Save"]}])
    assert r["ok"] is False
    assert searched == ["保存", "Save"], "全失败时才该把每个候选都搜到"
    msg = r["steps"][0]["message"]
    assert "保存" in msg and "Save" in msg, \
        "只报最后一个名字会让模型以为只搜了一次，据此判断「这界面是英文的」就是错的"


def test_seq_click_plain_text_path_unchanged():
    """单个字符串（非列表）走的是既有的老路径，行为不受本机制影响。"""
    backend = _StubBackend()
    backend.is_alive = lambda n: True
    fake_find, searched = _find_stub({"保存"})
    backend.find = fake_find
    coord = Coordinator(backend=backend)

    r = coord.act_sequence([{"op": "click", "text": "保存"}])
    assert r["ok"] is True and searched == ["保存"]


# ---------- 方向三（另一半）：ui_tree 步骤（「点开菜单 → 读菜单」并成一次调用）----------
def test_act_sequence_ui_tree_op_returns_tree_and_passes_params():
    """
    ui_tree 步骤：树走 message，**不走**截图那条 `_images` 旁路。

    顺手断言「不走 `_images`」是为了拦住照抄截图的写法：树是纯文本，天生能 JSON 序列化，
    再给它开一条 bytes 旁路只会让工具层多做一次无谓的拆装。
    """
    import json

    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    seen: dict = {}
    coord.get_ui_tree = lambda **kw: (seen.update(kw), "[1] push button | 保存")[1]

    r = coord.act_sequence([{"op": "ui_tree", "scope": "app", "app": "gedit",
                             "max_nodes": 80}])
    assert r["ok"] is True
    assert "push button" in r["steps"][0]["message"]
    assert seen == {"scope": "app", "app": "gedit",
                    "interactive_only": False, "max_nodes": 80}
    json.dumps(r, ensure_ascii=False)
    assert "_images" not in r, "读树不该走截图的 _images 旁路"


def test_act_sequence_ui_tree_defaults_are_conservative():
    """默认值：范围取活动窗口、节点数刻意小于 ui_tree 工具的 400（序列里读树只为顺手确认）。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    seen: dict = {}
    coord.get_ui_tree = lambda **kw: (seen.update(kw), "TREE")[1]

    coord.act_sequence([{"op": "ui_tree"}])
    assert seen["scope"] == "active_window"
    assert seen["interactive_only"] is False
    assert seen["max_nodes"] == Coordinator._SEQ_TREE_DEFAULT_NODES
    assert Coordinator._SEQ_TREE_DEFAULT_NODES < 400, "不该默认就把整棵大树拉进响应"


def test_act_sequence_ui_tree_rejects_bad_params():
    """
    越界/非法的参数必须**当场报错**，不静默截断或降级（与 screenshot.region 的 M-41 同口径）。

    非法 scope 静默降级会让模型拿到**另一种**范围的结果却不自知——正是 tools/ui_tree.py
    用 Literal 而非裸 str 要防的那件事，序列内这条路径也得同样守住。
    """
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    coord.get_ui_tree = lambda **kw: "TREE"

    with pytest.raises(ComputerUseError):
        coord._run_seq_step("ui_tree", {"op": "ui_tree", "max_nodes": 9999})
    with pytest.raises(ComputerUseError):
        coord._run_seq_step("ui_tree", {"op": "ui_tree", "scope": "everything"})


def test_act_sequence_ui_tree_count_is_capped():
    """序列内读树步数有上限（M-46 同思路）：一步几百行，一次调用连读多屏就该拆开重新规划。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    coord.get_ui_tree = lambda **kw: "TREE"

    n = Coordinator._SEQ_MAX_TREES
    r = coord.act_sequence([{"op": "ui_tree"}] * n)
    assert r["ok"] is True and len(r["steps"]) == n

    r = coord.act_sequence([{"op": "ui_tree"}] * (n + 1))
    assert r["ok"] is False
    assert r["stopped_at"] == n, "第 n+1 步就该被上限挡下"
    assert "上限" in r["steps"][n]["message"]


# ---------- 方向四：三个鼠标动作接入序列（双击 / 滚动 / 拖拽）----------
def test_seq_op_double_click_sends_repeat_not_two_calls():
    """
    双击步：连击必须落到注入层的 `repeat=2`，**不是**发两次 click_at。

    判据看的是传给 backend 的 repeat 值本身——只断言 ok=True 的话，把实现改成
    「连调两次 click_xy」照样能过，而那正是双击历史上不生效的原因（两次调用之间
    隔着 0.5s 级的证据开销，超过系统双击阈值，被认成两次单击）。
    """
    backend = _StubBackend()
    coord = Coordinator(backend=backend)

    r = coord.act_sequence([{"op": "double_click", "x": 10, "y": 20}])
    assert r["ok"] is True
    assert backend.last_click["repeat"] == 2, "双击必须一次调用连发两下"
    assert (backend.last_click["x"], backend.last_click["y"]) == (10, 20)
    assert backend.calls.count(("click", 10, 20)) == 1, "不能被拆成两次坐标点击"
    assert "双击" in r["steps"][0]["message"]


def test_seq_op_double_click_requires_xy():
    """双击只能走坐标级：不给 x/y 要**当场报错**，而不是静默去找同名的元素。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    with pytest.raises(ComputerUseError) as ei:
        coord._run_seq_step("double_click", {"op": "double_click"})
    assert "坐标" in str(ei.value)


def test_seq_op_scroll_maps_direction_to_wheel_button():
    """
    滚动：方向 → X11 滚轮号（up=4 / down=5），格数 → repeat。

    ⚠️ 判据必须落在 **button** 上：只断言 ok=True 或只看 repeat 的话，把 up/down 写反、
    或两个方向都发同一个按钮号，测试照样全绿——而写反方向正是这个 op 最容易犯的错
    （滚动会往反方向跑，模型却收到「成功」）。
    """
    backend = _StubBackend()
    coord = Coordinator(backend=backend)

    coord.act_sequence([{"op": "scroll", "direction": "up", "amount": 3,
                         "x": 5, "y": 6}])
    assert backend.last_click["button"] == 4, "上滚是 button 4"
    assert backend.last_click["repeat"] == 3
    assert backend.last_click["focus_window"] is False, "滚动是看内容，不该抢焦点"

    coord.act_sequence([{"op": "scroll", "direction": "down", "amount": 7,
                         "x": 5, "y": 6}])
    assert backend.last_click["button"] == 5, "下滚是 button 5"
    assert backend.last_click["repeat"] == 7


def test_seq_op_scroll_rejects_bad_params():
    """方向非法/格数为 0/格数超上限都要当场报错（序列持屏锁执行，规模必须有界）。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)

    with pytest.raises(ComputerUseError):
        coord._run_seq_step("scroll", {"op": "scroll", "direction": "left",
                                       "x": 1, "y": 1})
    with pytest.raises(ComputerUseError):
        coord._run_seq_step("scroll", {"op": "scroll", "amount": 0, "x": 1, "y": 1})
    with pytest.raises(ComputerUseError):
        coord._run_seq_step("scroll", {"op": "scroll", "x": 1, "y": 1,
                                       "amount": Coordinator._SCROLL_MAX_AMOUNT + 1})


def test_seq_op_drag_passes_endpoints_and_steps():
    """拖拽步：起点/终点/插值步数原样交给 backend.drag_at。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)

    r = coord.act_sequence([{"op": "drag", "from_x": 1, "from_y": 2,
                             "to_x": 30, "to_y": 40, "steps": 4}])
    assert r["ok"] is True
    d = backend.last_drag
    assert (d["x1"], d["y1"], d["x2"], d["y2"]) == (1, 2, 30, 40)
    assert d["steps"] == 4


def test_seq_op_drag_requires_all_four_coords():
    """四个坐标缺一个就要**指出缺哪个**——静默拿 None 去拖是最坏的结果。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    with pytest.raises(ComputerUseError) as ei:
        coord._run_seq_step("drag", {"op": "drag", "from_x": 1, "from_y": 2, "to_x": 3})
    assert "to_y" in str(ei.value)


def test_seq_button_name_rejects_unknown_and_maps_right():
    """
    按键名只认 left/middle/right；非法值当场报错，合法值要真的翻对。

    「翻对」这条不能省：X11 里 **2 是中键、3 是右键**，这种编号没人记得住，
    写反了不会有任何报错，只会右键变中键。
    """
    backend = _StubBackend()
    coord = Coordinator(backend=backend)

    with pytest.raises(ComputerUseError):
        coord._run_seq_step("double_click", {"op": "double_click", "x": 1, "y": 2,
                                             "button": "right2"})
    coord._run_seq_step("double_click", {"op": "double_click", "x": 1, "y": 2,
                                         "button": "right"})
    assert backend.last_click["button"] == 3, "右键是 button 3（不是 2）"
    coord._run_seq_step("double_click", {"op": "double_click", "x": 1, "y": 2,
                                         "button": "middle"})
    assert backend.last_click["button"] == 2


# ---------- 使用引导（守 instructions，别再把模型带回「一步一次往返」）----------
def test_server_instructions_urge_act_sequence():
    """
    server.py 的 MCP instructions 必须引导使用 act_sequence，且**同时**给出判据。

    为什么值得用测试钉住：instructions 是模型**开箱必读**的那一段，而它的「标准工作流」
    天然会写成单步形式（get_ui_tree → find_element → click）——那等于教模型一步一次往返。
    实测一次真实任务 499s 里往返空档占 478s（96%），所以这段引导一旦被删、或被改回纯单步，
    性能会**静默**劣化：没有任何报错，只是每个任务都慢几倍。工具描述里喊得再响也补不上
    ——那要等模型已经决定用它才读得到，而 instructions 在决定**之前**就把它带偏了。

    判据部分同样必须有：只鼓励不谈边界，模型会把需要分支判断的步骤也串起来盲跑，
    界面一旦没按预期变，后续步骤全打偏，回头收拾比省下的还贵。
    """
    import inspect

    from computer_use_mcp import server

    src = inspect.getsource(server.create_server)
    assert "act_sequence" in src, "instructions 丢了 act_sequence 的使用引导"
    assert "不依赖上一步结果" in src or "分支判断" in src, \
        "引导必须带判据，否则退化成「无脑串长序列」"
    assert "ui_tree" in src, "instructions 该提到「把观察折进同一次调用」的写法"
