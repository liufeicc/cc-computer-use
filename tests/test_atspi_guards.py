# -*- coding: utf-8 -*-
"""
`backend/linux/atspi.py` 的回归集（REVIEW 第四节 M-20 ~ M-27）。

为什么单开一个文件：`tests/test_optimizations.py` 已逾 2000 行，本批条目按模块归属，
放这里既好找、也避免继续撑大那个文件（项目约定：单文件不过 800 行）。

全部用例都是**纯内存**的：不碰桌面、不起沙箱、不跑 xdotool。做法是把 `AtspiReader`
的 gi 读取方法替换成对本地假对象的操作（与 `test_optimizations._fake_reader` 同一手法）。
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.backend.linux import atspi as atspi_mod  # noqa: E402
from computer_use_mcp.backend.linux.atspi import AtspiReader  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_child_read_fail_counts():
    """
    清掉 M-25 的限流计数器。

    为什么必须清：它是**模块级**的（限流本来就要跨调用累计），于是用例之间会互相影响 ——
    「连打 500 次」那条会把计数推到 500 以上，随后「单次失败应记一条」的用例就会因为
    落进「每 100 次才汇总」的档位而拿不到日志，**假红**。
    """
    atspi_mod._child_read_fail_counts.clear()
    yield
    atspi_mod._child_read_fail_counts.clear()


class _FakeElement:
    """最小 a11y 元素：记录每一次 do_action 的入参，便于断言「到底做了什么」。"""

    def __init__(self, actions: list[str], name: str = "fake") -> None:
        self.actions = list(actions)
        self.name = name
        self.calls: list[int] = []          # 每次 do_action 收到的 index

    def do_action(self, idx: int) -> bool:
        self.calls.append(idx)
        return True

    def get_child_count(self) -> int:
        return 0


class _FakeNode:
    """最小 a11y 节点：只提供遍历需要的形状（与 test_optimizations 同款）。"""

    def __init__(self, name: str, children: list | None = None) -> None:
        self.name = name
        self.children = list(children or [])


def _reader() -> AtspiReader:
    """一个把 gi 读取方法换成纯内存操作的 AtspiReader。"""
    r = AtspiReader()
    r.role_name = lambda o: "panel"
    r.get_name = lambda o: getattr(o, "name", "")
    r.get_actions = lambda o: list(getattr(o, "actions", []))
    r.child_count = lambda o: len(getattr(o, "children", []))
    r.child_at = lambda o, i: o.children[i] if i < len(o.children) else None
    r.get_extents = lambda o, coord: None
    return r


# ---------- M-20：指定动作名匹配不到时，绝不「做别的」 ----------
def test_do_action_named_but_absent_does_nothing():
    """
    回归（M-20）：指定了动作名却匹配不到 → **一次 do_action 都不许发**。

    为什么必须断言「调用次数为 0」而不只是「返回 False」：本条的危害恰恰是
    「返回了什么」与「做了什么」不一致 —— 历史实现会静默执行**第 0 个**动作
    （可能是 delete/remove），却只回一个 bool，调用方无从知情。
    """
    r = _reader()
    el = _FakeElement(["delete", "click"])

    assert r.do_action(el, "不存在的动作") is False
    assert el.calls == [], f"匹配不到时不得执行任何动作，实际执行了 index={el.calls}"


def test_do_action_named_absent_does_not_fall_back_to_preferred():
    """
    回归（M-20 的危害现场）：`focus()` 的退化分支 `do_action("focus") or do_action("select")`
    在历史实现下会命中 `_PREFERRED_ACTIONS` 里的 `"click"`，**真的把元素点一下**。
    而 `coordinator.type_text` 在元素级赋值前无条件调 `focus()` 且不看返回值 ——
    「对只有 click 动作的元素调 type_text」会先误点它。这里把这个链路钉死。
    """
    r = _reader()
    el = _FakeElement(["click"])            # 只有 click

    assert r.do_action(el, "focus") is False, "元素没有 focus 动作，必须如实返回 False"
    assert r.do_action(el, "select") is False
    assert el.calls == [], f"focus/select 都不存在时绝不能退化成 click，实际执行了 {el.calls}"


def test_do_action_unspecified_still_uses_preferred_then_index0():
    """
    反向守护：**不指定**动作名时，原行为必须原样保留 ——
    先按 `_PREFERRED_ACTIONS` 优先级挑，都没有才取第 0 个。
    （这一条是防止修 M-20 时把兜底一刀切掉，那会让 `click(ref)` 在主路径上失效。）
    """
    r = _reader()

    el = _FakeElement(["delete", "click"])
    assert r.do_action(el, None) is True
    assert el.calls == [1], "未指定动作时应按优先级选到 click（index=1）"

    el2 = _FakeElement(["delete"])          # 没有任何偏好动作
    assert r.do_action(el2, None) is True
    assert el2.calls == [0], "都没有时应回落取第 0 个（历史行为，刻意保留）"


def test_do_action_substring_match_still_works():
    """精确匹配不到、但子串能匹配上时，仍应执行（这条档位不能被 M-20 的改动误伤）。"""
    r = _reader()
    el = _FakeElement(["activate-window"])

    assert r.do_action(el, "activate") is True
    assert el.calls == [0], f"子串匹配应命中 index=0，实际 {el.calls}"


def test_focus_prefers_real_focus_methods_over_actions():
    """`focus()` 优先走 grab_focus/set_focus；两者都不存在时才退化到动作（M-20 的改动只收紧退化档）。"""

    class _WithGrab:
        def grab_focus(self) -> bool:
            return True

    r = _reader()
    assert r.focus(_WithGrab()) is True


# ---------- M-25：child_count / child_at 读失败不得静默 ----------
def test_child_count_failure_is_logged(caplog):
    """
    回归（M-25）：读子节点失败仍返回 0（语义不变），但**必须留下 debug 日志**。

    为什么：吞异常本身是对的（不让异常逃到上层），但「读失败」被降级成「0 个子节点」后，
    a11y 半失效时表现为「拿到一棵空树」而不是报错，排查成本极高。
    """
    class _Broken:
        def get_child_count(self) -> int:
            raise RuntimeError("boom")

    r = AtspiReader()
    with caplog.at_level("DEBUG", logger="computer_use_mcp.backend.linux.atspi"):
        assert r.child_count(_Broken()) == 0
    assert any("child_count" in rec.getMessage() for rec in caplog.records), \
        "读失败必须留下可查的 debug 记录"


def test_child_at_failure_is_logged(caplog):
    class _Broken:
        def get_child_at_index(self, i: int):
            raise RuntimeError("boom")

    r = AtspiReader()
    with caplog.at_level("DEBUG", logger="computer_use_mcp.backend.linux.atspi"):
        assert r.child_at(_Broken(), 0) is None
    assert any("child_at" in rec.getMessage() for rec in caplog.records)


def test_child_count_failure_log_is_rate_limited(caplog):
    """
    M-25 的配套：这两个方法在遍历里**每节点调用一次**，真断链时会刷出成千上万行。
    故日志必须限流（前 N 次逐条，之后只给汇总或干脆静默）。
    """
    class _Broken:
        def get_child_count(self) -> int:
            raise RuntimeError("boom")

    r = AtspiReader()
    with caplog.at_level("DEBUG", logger="computer_use_mcp.backend.linux.atspi"):
        for _ in range(500):
            r.child_count(_Broken())
    assert len(caplog.records) < 50, \
        f"遍历中每次失败都记一条会刷屏，实际记了 {len(caplog.records)} 条"

# ---------- M-23：应用触碰预算不得被无窗口应用吃光 ----------
class _App:
    """最小 application 节点：带 pid 与子节点。"""

    def __init__(self, pid: int, children: list | None = None, name: str = "") -> None:
        self.pid = pid
        self.children = list(children or [])
        self.name = name


def _budget_reader(apps: list[_App], target_pid: int) -> tuple[AtspiReader, dict]:
    """
    造一个 reader：`iter_apps` 返回给定应用，`_scan_app_windows` 被换成记账桩。

    返回 (reader, 账本)。账本记 `scan`（扫过哪些 pid）与 `child_count`（调了几次）。
    """
    r = AtspiReader()
    ledger = {"scan": [], "child_count": 0}

    r.iter_apps = lambda: iter(apps)
    r.get_process_id = lambda o: getattr(o, "pid", None)

    def _fake_scan(app, title):
        ledger["scan"].append(app.pid)
        # 真实 `_scan_app_windows` 一进来就调 child_count（那正是「触碰」的代价），
        # 桩必须复现这一点，否则测不出「预滤到底省了几次危险调用」。
        r.child_count(app)
        if app.pid == target_pid:
            return (app, None)          # 命中
        return (None, None)

    r._scan_app_windows = _fake_scan
    r.child_count = lambda o: ledger.__setitem__("child_count", ledger["child_count"] + 1) or 0
    return r, ledger


def test_app_touch_budget_can_be_eaten_by_windowless_apps():
    """
    复现（M-23）：预算 8，前 8 个都是**无窗口**应用（gnome-shell/输入法/注册器那类），
    目标应用排在第 9 —— 不预滤时目标**根本轮不到**，表现为「找不到活动窗口」。

    这条是「问题现场」，刻意断言**当前（未预滤）行为就是扫不到**；
    它不该被修掉，而是用来固定「为什么需要预滤」这个前提。
    """
    apps = [_App(pid=1000 + i) for i in range(8)] + [_App(pid=2000)]
    r, ledger = _budget_reader(apps, target_pid=2000)

    hit = r.get_active_window(active_title="目标窗口", window_pids_provider=None)

    assert hit is None, "预算被 8 个无窗口应用吃光后，目标应用本就轮不到"
    assert 2000 not in ledger["scan"], "目标应用不该被扫到（预算已耗尽）"
    assert ledger["child_count"] == 8, (
        f"未预滤时预算全被无窗口应用吃掉（8 次危险调用白费），实际 {ledger['child_count']} 次"
    )


def test_app_touch_budget_skips_windowless_apps_and_reaches_target():
    """
    回归（M-23）：给出「有窗口的 pid 集合」后，无窗口应用被**直接跳过**，目标能被扫到。

    判据有两面，缺一不可：
      ① 目标确实被扫到了（修复生效）；
      ② **`child_count` 的调用次数没有增加** —— 那是本项目明文认定的「最危险的单次调用」
         （会逼目标应用惰性构建整棵 a11y 树，2026-09-14 打崩 GNOME Shell 的根因）。
         报告原建议「只对 child_count(app) > 0 的应用计数」恰恰会**翻倍**这个调用。
    """
    apps = [_App(pid=1000 + i) for i in range(8)] + [_App(pid=2000)]
    r, ledger = _budget_reader(apps, target_pid=2000)

    hit = r.get_active_window(active_title="目标窗口",
                              window_pids_provider=lambda: {2000})

    assert hit is not None and hit.pid == 2000, "预滤后目标必须能被扫到"
    assert ledger["scan"] == [2000], f"无窗口应用应被跳过，实际扫了 {ledger['scan']}"
    assert ledger["child_count"] == 1, (
        f"child_count 只该在真正扫描目标应用时调一次，实际 {ledger['child_count']} 次"
        "（翻倍说明按报告原建议改了，那会削弱唯一的遍历爆炸防线）"
    )


def test_window_pids_provider_failure_degrades_gracefully():
    """预滤回调取不到（X11 挂了）时，不得让主流程失败——退化为历史行为即可。"""

    def _boom():
        raise RuntimeError("wmctrl 挂了")

    apps = [_App(pid=2000)]
    r, ledger = _budget_reader(apps, target_pid=2000)
    assert r.get_active_window(active_title="t", window_pids_provider=_boom) is not None


# ---------- find() 的同源问题：无窗口应用吃光的正是同一个预算 ----------
#
# 为什么单开一组而不算 M-23 已修：M-23 修的是 `get_active_window`，而 `find()` 的全桌面
# 路径**同一个预算被同一批应用吃光**，表现却是另一回事 —— 「按 text 找不到元素」，
# 于是模型掉到截图那条最贵的路上。判据与 M-23 一致：预滤只在**不指定 root** 时生效。
def _find_reader(apps: list[_App]) -> tuple[AtspiReader, dict]:
    """
    造 reader：`iter_apps` 返回给定应用，并给 `child_count` 装**记账**。

    账本记 `touched`（哪些应用被真正遍历了）与 `child_count`（危险调用总次数）。
    后者是关键：只断言「找到了目标」的话，把报告原建议「child_count>0 才计数」实现出来
    也算通过，而那次调用**一次没省、反而每个应用多一遍**。
    """
    r = _reader()
    ledger: dict = {"touched": [], "child_count": 0}
    orig_cc = r.child_count
    r.get_process_id = lambda o: getattr(o, "pid", None)

    def _cc(o):
        ledger["child_count"] += 1
        pid = getattr(o, "pid", None)
        if pid is not None:
            ledger["touched"].append(pid)
        return orig_cc(o)

    r.child_count = _cc
    r.iter_apps = lambda: iter(apps)
    return r, ledger


def test_find_desktop_budget_can_be_eaten_by_windowless_apps():
    """
    复现现场：预算 8，前 8 个都是无窗口应用，目标排第 9 —— 不预滤时**根本轮不到**。

    与 M-23 那条同构：刻意断言「未预滤就是搜不到」，固定「为什么需要预滤」这个前提。
    """
    apps = [_App(pid=1000 + i) for i in range(8)] + [_App(pid=2000, children=[_FakeNode("确定")])]
    r, ledger = _find_reader(apps)

    res = r.find(text="确定", interactive_only=False)     # 不给 provider = 修复前行为

    assert res.items == [], "预算被 8 个无窗口应用吃光后，目标应用本就轮不到"
    assert 2000 not in ledger["touched"]
    assert ledger["child_count"] == 8, (
        f"未预滤时 8 次危险调用全打在无窗口应用上，实际 {ledger['child_count']} 次"
    )


def test_find_desktop_skips_windowless_apps_and_reaches_target():
    """
    回归：给出「有窗口的 pid 集合」后无窗口应用被直接跳过，目标搜到，且**危险调用没翻倍**。

    三条断言各司其职：① 搜到了（修复生效）；② 只遍历了目标那一个应用；
    ③ `child_count` 恰好 2 次（目标应用 + 它那个子节点）—— 挡的是「按 child_count>0
    才计数」那种看着更聪明、实则把最危险调用翻倍的改法。
    """
    apps = [_App(pid=1000 + i) for i in range(8)] + [_App(pid=2000, children=[_FakeNode("确定")])]
    r, ledger = _find_reader(apps)

    res = r.find(text="确定", interactive_only=False, window_pids_provider=lambda: {2000})

    assert [getattr(n, "name", None) for n in res.items] == ["确定"]
    assert ledger["touched"] == [2000], f"无窗口应用不该被遍历，实际 {ledger['touched']}"
    assert ledger["child_count"] == 2, (
        f"危险调用只该花在真正要搜的应用上，实际 {ledger['child_count']} 次"
    )


def test_find_tells_the_model_when_windowless_apps_were_skipped():
    """
    「没搜到」与「没去看」必须分开说 —— 否则模型会把后者当成桌面上真的没有。

    判据是双向的：**有结果时不许报**（那会白白让模型怀疑结果不全），无结果且有跳过时才报，
    且必须给出解法（带 app= 重搜走 scoped 路径，不受预滤限制）。
    """
    apps = [_App(pid=1000 + i) for i in range(3)]
    r, _ = _find_reader(apps)

    empty = r.find(text="不存在的东西", interactive_only=False,
                   window_pids_provider=lambda: {9999})
    assert empty.items == []
    assert "无 X11 窗口" in empty.notice and "app=" in empty.notice, empty.notice

    # 反向：命中结果时不该出现这条提示
    hit = r.find(text="确定", interactive_only=False,
                 window_pids_provider=lambda: {2000},
                 root=_App(pid=2000, children=[_FakeNode("确定")]))
    assert hit.items and "无 X11 窗口" not in hit.notice, hit.notice


def test_scoped_find_never_consults_the_window_prefilter():
    """
    带 app= 的搜索（root 已给定）**一律不碰预滤**，连那次 `wmctrl` 都不该付。

    这条是「预滤会不会误伤精确搜索」的闸：指定了应用还去查窗口清单，既白付开销，
    又可能因为目标应用恰好没被 WM 列出而把精确搜索也滤空 —— 那等于把唯一的兜底路径也堵死。
    """
    consulted: list[int] = []

    def _provider():
        consulted.append(1)
        return set()

    r, _ = _find_reader([])
    target = _App(pid=2000, children=[_FakeNode("确定")])

    res = r.find(text="确定", interactive_only=False, root=target,
                 window_pids_provider=_provider)

    assert [getattr(n, "name", None) for n in res.items] == ["确定"]
    assert consulted == [], "scoped 路径不该调用预滤回调（白付一次 wmctrl）"


def test_find_window_prefilter_failure_degrades_gracefully():
    """回调抛异常 → 退化为「不跳过任何应用」，绝不能让整个搜索失败。"""
    apps = [_App(pid=2000, children=[_FakeNode("确定")])]
    r, _ = _find_reader(apps)

    def _boom():
        raise RuntimeError("wmctrl 挂了")

    res = r.find(text="确定", interactive_only=False, window_pids_provider=_boom)
    assert [getattr(n, "name", None) for n in res.items] == ["确定"]


def test_backend_find_passes_the_window_pids_provider():
    """
    结构性防漏：`LinuxBackend.find` 必须把预滤回调接到 reader 上。

    为什么单独一条：reader 侧改得再对，**接线断了整套修复就是死代码**（本项目栽过的
    「只修 ② 等于没修」那一类）。而这条只在接线消失时失败，平时零成本。
    传的是**方法本身**而不是集合 —— 现取就白付一次 wmctrl，见 window_pids 的 docstring。
    """
    from computer_use_mcp.backend.base import QueryResult

    seen: dict = {}
    r = _reader()
    r.find_app = lambda name: None
    r.find = lambda **kw: (seen.update(kw), QueryResult())[1]
    be = _backend_with(r)
    be.window_pids = lambda: {1, 2, 3}

    be.find(text="确定")

    assert seen.get("window_pids_provider") is be.window_pids, (
        f"backend 没把预滤回调传给 reader，整套预滤逻辑不会被触发：{seen.keys()}"
    )


# ---------- M-26：非法 scope 必须报错，不能静默给另一种结果 ----------
def test_backend_rejects_unknown_scope():
    """
    回归（M-26）：`scope` 拼错时抛 `ComputerUseError`，而不是静默变成活动窗口树。

    历史行为：`else: # active_window` 是兜底分支，任何拼错的 scope 都落到那里，
    模型拿到的是**另一棵树**却以为是自己要的 scope —— 静默给出错误结果。
    """
    from computer_use_mcp.backend.linux.backend import LinuxBackend
    from computer_use_mcp.utils.errors import ComputerUseError

    be = LinuxBackend.__new__(LinuxBackend)          # 不跑 __init__，只测参数校验
    with pytest.raises(ComputerUseError) as ei:
        be.get_tree(scope="desktops")
    assert "desktops" in str(ei.value) and "active_window" in str(ei.value)


def test_backend_rejects_app_scope_without_app():
    """
    回归（M-26）：`scope="app"` 不带 `app=` 时抛错。

    历史行为：会退化成 `scope="desktop"` 那条分支 —— 一次预算受限的**整桌面遍历**，
    既慢又危险（触碰多个应用），而模型以为自己只是在看某一个应用。
    """
    from computer_use_mcp.backend.linux.backend import LinuxBackend
    from computer_use_mcp.utils.errors import ComputerUseError

    be = LinuxBackend.__new__(LinuxBackend)
    with pytest.raises(ComputerUseError) as ei:
        be.get_tree(scope="app")
    assert "app=" in str(ei.value)


# ---------- M-24：`写 env → import` 必须原子 ----------
def test_apply_bus_and_import_must_not_interleave(monkeypatch):
    """
    回归（M-24）：`_atspi()` 的「写 `os.environ` → `import_atspi()`」两步之间，
    **不得**被另一次 `_apply_bus()` 插入。

    为什么不变式是这个（而不是「env == `_bound_address`」）—— 我先写了后者，**红线没变红**，
    实测发现它在竞态下**仍然成立**：`_apply_bus` 同时写「env」与「`_bound_address`」，
    谁最后写谁自洽。真正被破坏的是**时序**：libatspi 只在 `atspi_init()` 那一刻读环境变量，
    所以「这个线程刚写下的值」必须**一直活到它自己 import 为止**；中途被别人改掉，
    `atspi_init()` 读到的就是别人的值（可能是「沙箱未就绪」的 None → 连到**宿主总线**）。

    判据因此落在时序上：import 发生时读到的 env，必须等于**本线程**刚写下的那个值。

    每条迭代前把 `_Atspi` 置 None 是为了**反复进入**那条临界区（真实场景里它只在进程
    首次用 a11y 时走一次，一次就跑不出竞态）；这不是篡改语义——`_Atspi = None` 正是
    「还没 import」的本义。
    """
    r = AtspiReader()
    tl = threading.local()
    applied_by_me: list = []
    violations: list = []

    real_apply = AtspiReader._apply_bus

    def _recording_apply(self, address):
        real_apply(self, address)
        tl.last = address

    def _checked_import():
        # ⚠️ 睡眠必须在**检查之前**：libatspi 是在 import（= atspi_init）那一刻读环境变量的，
        # 所以「写 env → import」之间那段时间才是要拉宽的竞态窗口。第一版我把 sleep 放在
        # 检查之后，窗口仍只有几个字节码宽 —— 红线**没变红**，实测踩到。
        time.sleep(0.005)
        now = os.environ.get("AT_SPI_BUS_ADDRESS")
        mine = getattr(tl, "last", "<本线程未写过>")
        if mine != "<本线程未写过>" and now != mine:
            violations.append(
                f"import 时 env={now!r}，但本线程刚写的是 {mine!r} —— 中间被别的线程改掉了"
            )
        return object()

    monkeypatch.setattr(r, "_apply_bus", lambda addr: _recording_apply(r, addr))
    monkeypatch.setattr(atspi_mod, "import_atspi", _checked_import)

    state = {"i": 0}
    state_lock = threading.Lock()

    def _alternating_want():
        # 交替给出「私有总线」与「沙箱暂时不可用（None）」—— 后者正是那支会**删掉**
        # 环境变量的手，也是竞态的必要条件。
        with state_lock:
            state["i"] += 1
            return "/tmp/cc-cu-priv-bus" if state["i"] % 2 else None

    monkeypatch.setattr(r, "_desired_bus", _alternating_want)

    def worker():
        for _ in range(120):
            r._Atspi = None            # 反复进入「首次绑定」那条临界区
            try:
                r._atspi()
            except Exception:  # noqa: BLE001 —— 只关心时序不变式，不关心桩的行为
                pass
            applied_by_me.append(getattr(tl, "last", None))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not violations, (
        f"「写 env → import」被插入（前 3 例：{violations[:3]}）。"
        "libatspi 可能已按**别人写的**地址 init —— 若那个值是 None/NULL，"
        "a11y 流量就落到了宿主总线，而 `_bound_address` 仍显示私有总线。"
    )


# ---------- M-27：遍历熔断常量缺回归保护 ----------
def _backend_with(reader):
    """造一个只做参数校验/遍历编排的 LinuxBackend（不跑 __init__、不碰真桌面）。"""
    import types

    from computer_use_mcp.backend.linux.backend import LinuxBackend

    be = LinuxBackend.__new__(LinuxBackend)
    be.reader = reader
    be.injector = types.SimpleNamespace()
    be.ensure_available = lambda: None
    return be


def test_find_reports_node_budget_exhaustion(monkeypatch):
    """
    回归（M-27）：`find` 的**节点数熔断**（`_VISIT_BUDGET_*`）命中时必须进 `notice`。

    这条常量是「防打爆 a11y D-Bus」的唯一闸门，而此前 `tests/` 里**零覆盖**——
    既有的两条 find 用例都把 `_collect` 桩掉了，于是永远走不到预算分支。
    （判据不能只 grep 常量名：`_APP_TOUCH_BUDGET` 其实已被另一条用例按行为覆盖，
    只 grep 会得出「全都没测」的错误结论。）
    """
    r = _reader()
    big = _FakeNode("root", [_FakeNode(f"c{i}") for i in range(300)])
    r.iter_apps = lambda: iter([_App(pid=1, children=[big])])
    monkeypatch.setattr(r, "_VISIT_BUDGET_DESKTOP", 20)

    res = r.find(text="绝不匹配的名字", interactive_only=False)

    assert res.notice, "触发节点数熔断必须给出 notice（项目要求：截断必带提示）"
    assert "节点数熔断" in res.notice or "遍历量" in res.notice, res.notice


def test_find_stays_quiet_when_no_budget_hit(monkeypatch):
    """反向守护：**没触顶就不许报**截断，否则模型会以为结果不全而白白收窄范围。"""
    r = _reader()
    small = _FakeNode("root", [_FakeNode("only")])
    r.iter_apps = lambda: iter([_App(pid=1, children=[small])])

    res = r.find(text="绝不匹配的名字", interactive_only=False)
    assert res.notice == "", f"未触顶不该有 notice，实际：{res.notice!r}"


def test_get_tree_reports_app_budget():
    """
    回归（M-27）：`get_tree(scope='desktop')` 的应用数上限（`_TREE_APP_BUDGET`）
    命中时进 `notice`，且**确实只展开了预算内的应用**。

    这是 I-4/I-9 那条「截断必带提示」在 backend 侧的落点，此前无测试。
    """
    r = _reader()
    apps = [_App(pid=100 + i, children=[_FakeNode(f"w{i}")]) for i in range(20)]
    r.iter_apps = lambda: iter(apps)
    be = _backend_with(r)

    res = be.get_tree(scope="desktop", max_nodes=1000)

    assert res.notice and "应用数上限" in res.notice, res.notice
    assert len(res.items) <= be._TREE_APP_BUDGET, (
        f"只该展开 {be._TREE_APP_BUDGET} 个应用，实际 {len(res.items)} 个"
    )


def test_get_tree_reports_window_budget():
    """回归（M-27）：单应用窗口数超 `_TREE_WINDOW_BUDGET` 时必须提示（否则静默少给窗口）。"""
    r = _reader()
    many = [_FakeNode(f"w{i}") for i in range(40)]
    r.iter_apps = lambda: iter([_App(pid=1, children=many)])
    be = _backend_with(r)

    res = be.get_tree(scope="desktop", max_nodes=1000)

    assert res.notice and "窗口数达上限" in res.notice, res.notice
    assert len(res.items) <= be._TREE_WINDOW_BUDGET
