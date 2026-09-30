# -*- coding: utf-8 -*-
"""★「默认不碰用户键鼠」的守卫（Windows 隐藏桌面那一档）。

═══ 这一组守的是用户的原话 ═══

「windows 操作时，启动的程序并不是像 LINUX 一样启动在单独的虚拟屏中的，
**操作软件时会跟用户抢夺鼠标和焦点**」。

Phase 2 解决了"启动在哪儿"（隐藏桌面），本组守的是**另一半**：**操作时会不会碰到用户**。

═══ 为什么这一档必须由测试守 ═══

Windows 与 Linux 在这一点上是**结构性差异**，不是程度差异：

  · Linux 隔离模式下，XTEST 注入进的是 **Xephyr 那块虚拟屏** —— 用户碰不到，
    所以坐标点击/键盘注入照常可用，不需要闸门；
  · Windows 隔离模式下，MCP server 进程**自己就跑在用户桌面上**（跨桌面操作是刻意
    保留的能力），于是进程里发出的 `SendInput` **只可能**落到当前输入桌面 = 用户的桌面。

后果的形态是最坏的那一种：`SetForegroundWindow` 成功、`SendInput` 返回 1、
我们的 `ActionResult` 报 ok —— 而**用户眼前正在被点、字正在被敲进他的编辑器**。
零报错、零日志、用户只能自己发现。所以判据必须是**结构性**的（反射/源码顺序），
不能靠"跑一遍看看有没有事"。

═══ 判据纪律（同 test_capture_scope.py）═══

每条都要能被证伪；**正控不许空转** —— 只断言"某分支存在"而没有反向用例，等于没测。
本文件里凡是"要求 X 在 Y 之前"的地方，都额外断言 Y 真的出现在被检视的代码里
（否则 Y 不在场时"X 在 Y 前面"恒真）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from computer_use_mcp.backend.windows.backend import WindowsBackend
from computer_use_mcp.backend.windows.driver import actions as w_actions
from computer_use_mcp.backend.windows.driver import desktop as w_desktop
from computer_use_mcp.core import session
from computer_use_mcp.core.session import WindowsDesktopSession
from computer_use_mcp.core.session_types import (
    OP_INJECT,
    OP_INJECT_GLOBAL,
    SessionCapabilities,
    display_mode_from_env,
    global_input_allowed,
    global_input_note,
    global_input_refusal,
)
from computer_use_mcp.utils.errors import GlobalInputRefusedError

from _helpers import _StubBackend  # noqa: E402

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"

# ⚠️ 源根可被**反证脚本**重定向（`tests/manual_windows_guards_reverse.py` 会把一份
#    改坏的 src/ 副本挂在这里）。所以凡是读源码的判据都必须经 `_src_root()` 取路径，
#    **不许**直接用 `_SRC` —— 直接用的话那几条反证会永远跑在真实源码上，
#    于是"守卫坏了"与"代码没问题"又长得一模一样（那正是反证脚本存在的理由）。
_TEST_SRC: pathlib.Path | None = None


def _src_root() -> pathlib.Path:
    """默认是仓库的 `src/computer_use_mcp`；反证脚本运行期会指向一份被改坏的副本。"""
    return _TEST_SRC or _SRC


def _win_backend() -> pathlib.Path:
    return _src_root() / "backend" / "windows" / "backend.py"


def _coord_actions() -> pathlib.Path:
    return _src_root() / "core" / "coordinator" / "actions.py"


def _driver_dir() -> pathlib.Path:
    return _src_root() / "backend" / "windows" / "driver"


# 会**改变用户输入状态**的原语。判据刻意只收这一类：读原语（`window_from_point` /
# `get_cursor_pos`）不打扰用户，把它们也算进来会让"落点证据"这类只读功能被迫登记豁免，
# 而豁免一多，这条守卫就退化成一份需要维护的清单。
_INJECTION_PRIMITIVES = (
    "send_mouse_button", "send_mouse_drag", "send_unicode_text", "send_key_combo",
    "send_input_move_absolute", "set_foreground_window", "set_cursor_pos",
)


# ── 源码顺序的取法：用 AST 精确定位方法体，不做全文子串切片 ────────────────

def _blank_noise(source: str) -> str:
    """
    把**注释**与**字符串字面量**原地挖成空格，只留会执行的 token。

    ⚠️ 为什么不满足于"去掉 docstring + 整行注释"：本项目的方法说明与注释里必然要引用
    原语名（`type_text` 的说明第一句就写着「见 `win32.send_unicode_text`」），
    行内注释里也常出现。不挖掉它们，顺序判据量到的就是"文字出现在哪"，而不是
    "**代码**执行到哪" —— 一次正确的重构（把说明挪个位置）就能把守卫判红。

    挖成空格而**不是**删掉，是为了保持字符偏移不变（判据比的是相对先后，但保持原样
    便于出错时人工核对）。多行字符串（三引号）里的换行保留，否则行号会串。
    """
    import io
    import tokenize

    chars = list(source)
    offsets = []
    pos = 0
    for ln in source.splitlines(keepends=True):
        offsets.append(pos)
        pos += len(ln)

    def _abs(row: int, col: int) -> int:
        return offsets[row - 1] + col

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        # 片段本身不完整（少了外层缩进）时 tokenize 可能报错 —— 那就退化成"不挖"，
        # 保守地多留一些文字，总好过让守卫直接崩掉。
        return source
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        start, end = _abs(*tok.start), _abs(*tok.end)
        for i in range(start, min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _method_src(path: pathlib.Path, cls: str, name: str) -> str:
    """
    取出 `class <cls>` 里 `<name>` 这个方法的**可执行**源码（注释与字符串已挖空）。

    ⚠️ 不用"全文搜索 def name"：同名方法在别的类里出现（`WindowsBackend` 与几个
    mixin 都有 `focus`/`set_value`）时，全文搜索会拿到**别处**的那一份，
    而"顺序恰好也对"的概率不低 —— 那会变成一条永远绿的假守卫。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text, str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != cls:
            continue
        for fn in node.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                start = fn.lineno - 1
                end = fn.end_lineno or fn.lineno
                return _blank_noise("".join(lines[start:end]))
    raise AssertionError(f"{path.name} 的 class {cls} 里找不到方法 {name}")


def _index_of(body: str, needle: str) -> int:
    idx = body.find(needle)
    assert idx >= 0, f"被检视的代码里找不到 {needle!r} —— 判据的前提不成立（见文件头纪律）"
    return idx


def test_blank_noise_removes_comments_and_strings_but_not_code():
    """尺子自检：`_blank_noise` 必须**只**挖注释与字符串，且真的挖干净。

    两个方向都要断，缺一个就等于没测：
      · 挖得掉 —— 否则说明里的原语名会被当成"代码里出现过"，顺序判据量错东西；
      · 挖不坏 —— 把代码也挖了的话，`_index_of` 会找不到原语而**假装**判据成立
        （它撞的是"前提不成立"那条断言，报出来的原因却与真实问题无关）。
    """
    src = (
        'def f():\n'
        '    """说明里提到 send_unicode_text 与 require_global_input。"""\n'
        '    # 行内注释也提到 send_unicode_text\n'
        '    tag = "send_unicode_text"\n'
        '    return win32.send_key_combo([])   # noqa\n'
    )
    out = _blank_noise(src)
    assert "send_unicode_text" not in out, f"字符串/注释没挖干净：{out!r}"
    assert "require_global_input" not in out
    assert "win32.send_key_combo([])" in out, f"真代码被误挖了：{out!r}"


# ── ① Windows backend：四个注入方法都必须先过闸门 ────────────────────────

@pytest.mark.parametrize("method,primitive", [
    ("click_at", "send_mouse_button"),
    ("drag_at", "set_foreground_window"),
    ("type_text", "send_unicode_text"),
    ("press_key", "send_key_combo"),
])
def test_every_global_input_method_gates_before_injecting(method, primitive):
    """★ 每一个会动用户键鼠的方法，闸门必须**早于**注入原语。

    判据是**顺序**而不是"出现过"：把 `require_global_input` 写在方法末尾照样能过
    `in` 断言，而那时事件已经发出去了。

    ⚠️ `_index_of(body, primitive)` 同时充当正控：原语不在场时它会先红，
    不会出现"X 在 Y 前面"在 Y 缺席时恒真的空转。
    """
    body = _method_src(_win_backend(), "WindowsBackend", method)
    gate = _index_of(body, "require_global_input(")
    inject = _index_of(body, primitive)
    assert gate < inject, (
        f"WindowsBackend.{method} 的全局注入闸门晚于注入原语 {primitive} —— "
        f"事件会先发出去、闸门再拒（等于没拦）"
    )


def test_injection_sweep_is_not_blind_and_has_no_unguarded_method():
    """★ 反射式清扫：**任何**碰注入原语的 WindowsBackend 方法都必须过闸门。

    这一条是为了防"将来加第 5 个注入方法时忘了挂闸门" —— 那种漏挂**不会报错**，
    只会让新方法静默地动用户的鼠标。逐方法的参数化用例（上一条）拦不住它，
    因为新方法不在那张 parametrize 清单里。
    """
    text = _win_backend().read_text(encoding="utf-8")
    tree = ast.parse(text, str(_win_backend()))
    lines = text.splitlines()

    checked: list[str] = []
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "WindowsBackend":
            continue
        for fn in node.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = _blank_noise("\n".join(lines[fn.lineno - 1:(fn.end_lineno or fn.lineno)]))
            if not any(p in body for p in _INJECTION_PRIMITIVES):
                continue
            checked.append(fn.name)
            if "require_global_input(" not in body:
                offenders.append(fn.name)

    # 正控：尺子必须真的量到了东西（清单空了 = 扫描写错了，而不是"代码很干净"）
    assert len(checked) >= 4, (
        f"只扫到 {checked} 个碰注入原语的方法 —— 少于已知的 4 个，"
        f"扫描逻辑或原语清单写错了，本规则正在空转"
    )
    assert not offenders, (
        f"这些方法会动用户的真实键鼠却没有过闸门：{offenders}。"
        f"隔离模式下这一下会直接打到用户的桌面上，而每一步都返回成功。"
    )


def test_allowlist_of_input_primitives_covers_what_we_think_it_does():
    """原语清单本身不许过期：它列的每一个名字都必须真的在驱动层里存在。

    少了这条，某天把 `set_cursor_pos` 改名之后，清单里那项就成了一条**永远不匹配**的
    死字符串 —— 而清扫会因此**少覆盖**一类注入，且完全静默（正是本项目反复吃过的亏）。
    """
    driver_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in _driver_dir().rglob("*.py"))
    for prim in _INJECTION_PRIMITIVES:
        assert f"def {prim}" in driver_text, (
            f"原语清单里的 {prim!r} 在驱动层已经不存在了 —— 请更新清单"
            "（留着它会让清扫少覆盖一类注入，且不报错）"
        )


# ── ② 协调层：闸门必须早于**取证据**（否则先把用户的屏幕读进上下文）────────

@pytest.mark.parametrize("method,before_what", [
    ("click_xy", "self._preview_image("),
    ("scroll_xy", "self._scroll_point("),
    ("drag_xy", "self._preview_image("),
])
def test_coordinator_gates_before_taking_evidence(method, before_what):
    """★ 协调层的闸门必须早于**每一次取证**。

    取证那几步（预览图 + OCR）会先把**用户屏幕**上落点周围的一小块读进模型上下文。
    既然这一下注定要被拒绝，就不该先读他的屏幕 —— 那是"没做成还顺手偷看一眼"。
    """
    body = _method_src(_coord_actions(), "ActionMixin", method)
    gate = _index_of(body, "self._require_global_input(")
    evid = _index_of(body, before_what)
    assert gate < evid, f"ActionMixin.{method} 先取证（{before_what}）才过闸门"


def test_type_text_gates_only_the_keyboard_fallback_not_the_element_path():
    """★ `type_text` 的闸门**只能**卡在键盘兜底那一步，不能卡在方法开头。

    卡在开头会把元素级 `set_value` 一起误杀 —— 而元素级正是隔离模式下**唯一**还能
    打字的通道（跨进程消息，不碰输入设备）。那样的"更安全"实则是把能力砍掉一半，
    且模型只会看到"输入一律失败"，完全不知道原因。
    """
    body = _method_src(_coord_actions(), "ActionMixin", "type_text")
    element_write = _index_of(body, "self.backend.set_value(")
    gate = _index_of(body, "self._require_global_input(")
    assert element_write < gate, (
        "type_text 的闸门跑到了元素级 set_value 前面 —— 隔离模式下连元素级写入都会被拒"
    )


# ── ③ 行为：闸门真的抛错，且抛在碰 win32 之前 ─────────────────────────────

@pytest.mark.parametrize("call", [
    lambda b: b.click_at(1, 2),
    lambda b: b.drag_at(1, 2, 3, 4),
    lambda b: b.type_text("hi"),
    lambda b: b.press_key("ctrl+s"),
])
def test_isolated_mode_refuses_global_input_outright(call, monkeypatch):
    """★ 隔离模式下四个方法一律抛 `GlobalInputRefusedError`。

    判据刻意取**异常类型**而不是"抛了异常"：在 Linux 上（本用例的运行平台）这些方法
    会把 `win32.send_*` 走到 `ctypes.windll`，抛的是 `AttributeError` ——
    于是"闸门生效"与"根本没走到闸门"都能算"抛了异常"。只有认类型才分得开。
    """
    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)
    with pytest.raises(GlobalInputRefusedError) as ei:
        call(WindowsBackend())
    # 报错必须**给出路**（换元素级 / 换模式 / 显式放开三条），否则模型只会反复重试
    msg = str(ei.value)
    assert "元素级" in msg and "CC_CU_ALLOW_GLOBAL_INPUT=1" in msg, msg


@pytest.mark.parametrize("call", [
    lambda b: b.click_at(1, 2),
    lambda b: b.drag_at(1, 2, 3, 4),
    lambda b: b.type_text("hi"),
    lambda b: b.press_key("ctrl+s"),
])
def test_escape_hatch_lets_it_through(call, monkeypatch):
    """★ 反向正控：`CC_CU_ALLOW_GLOBAL_INPUT=1` 之后闸门**真的让路**。

    没有这一条，上面那条"抛 GlobalInputRefusedError"可能只是某个与开关无关的错误 ——
    一个恒定拒绝的实现也能全绿。这里断言的是"不再抛 GlobalInputRefusedError"
    （在 Linux 上会继续往下走到 `ctypes.windll` 而抛 AttributeError，那也是通过）。
    """
    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.setenv("CC_CU_ALLOW_GLOBAL_INPUT", "1")
    try:
        call(WindowsBackend())
    except GlobalInputRefusedError as exc:      # pragma: no cover - 只在改坏时命中
        pytest.fail(f"放开之后仍被闸门拦住：{exc}")
    except Exception:  # noqa: BLE001 —— 走到 win32 了（Linux 上没有 windll），正是我们要的
        pass


# ── ④ 元素级路径：隔离模式下**不许**抢前台 ────────────────────────────────

def _boom(*a, **k):  # noqa: ANN002, ANN003, ANN201
    raise AssertionError("这个动作在隔离模式下不该被调用")


class _Recorder:
    """把一次调用记下来并返回 True —— 用作"这个协作者有没有被打到"的探针。

    ⚠️ 刻意**不用"抛异常"当探针**：被测的两个方法都把协作者调用包在
    `except Exception` 里（那是刻意的容错，见它们的 docstring），异常会被吞掉，
    于是"没抛"会被读成"没调用" —— 探针与被测对象互相抵消，测了个寂寞。
    """

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls.append((args, kwargs))
        return True


def test_focus_is_a_policy_noop_in_isolated_mode(monkeypatch):
    """★ 隔离模式下 `focus()` 一个动作都不发，返回 False。

    UIA 的 `SetFocus` 会让目标窗口去抢前台 —— 在隐藏桌面上，"它抢的到底是谁的前台"
    是一件没人能担保的事。而元素级写值**本来就不需要焦点**（DEMO 实测后台 SetValue
    可用），所以放弃它是零代价的。

    ⚠️ 正控：`real` 模式下同一次调用**必须**打到 `actions.focus` —— 否则"没调用"
    可能只是因为补丁打空了，而那种绿毫无意义。
    """
    probe = _Recorder()
    monkeypatch.setattr(w_actions, "focus", probe)
    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)

    token = object()
    assert WindowsBackend().focus(token) is False
    assert probe.calls == [], "隔离模式下不该发任何焦点动作"

    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "real")
    assert WindowsBackend().focus(token) is True
    assert len(probe.calls) == 1, "real 模式下必须照旧去聚焦（正控：探针是活的）"


def test_set_value_does_not_focus_in_isolated_mode(monkeypatch):
    """★ 写值时的 `allow_focus` 随模式翻转（真实路径上的那一半）。"""
    seen: dict = {}

    def _fake_set_value(element, text, allow_focus=True):  # noqa: ANN001, ANN202
        seen["allow_focus"] = allow_focus
        return True

    monkeypatch.setattr(w_actions, "set_value", _fake_set_value)
    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)
    assert WindowsBackend().set_value(object(), "x") is True
    assert seen["allow_focus"] is False, "隔离模式下写值不许聚焦"

    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "real")
    WindowsBackend().set_value(object(), "x")
    assert seen["allow_focus"] is True, "real 模式下应保留原来的聚焦行为"


class _FakeEdit:
    """最小 UIA 编辑框桩：只需要 `GetPattern` / `GetValuePattern` 两条。"""

    def __init__(self, value="", explode=False):  # noqa: ANN001
        self._value = value
        self._explode = explode
        self.set_calls: list[str] = []

    def GetPattern(self, pattern_id):  # noqa: ANN001, ANN201, N802
        return object()

    def GetValuePattern(self):  # noqa: N802
        outer = self

        class _Pat:
            def SetValue(self, v):  # noqa: ANN001, ANN202, N802
                outer.set_calls.append(v)

            @property
            def Value(self):  # noqa: ANN201
                if outer._explode:
                    raise RuntimeError("读不到")
                return outer._value

        return _Pat()


def test_driver_set_value_never_focuses_when_disallowed(monkeypatch):
    """驱动层：`allow_focus=False` 时**一个**焦点动作都不发。"""
    monkeypatch.setattr(w_actions.nodes, "pattern_available",
                        lambda el, name: name == "ValuePattern")
    probe = _Recorder()
    monkeypatch.setattr(w_actions, "focus", probe)
    assert w_actions.set_value(_FakeEdit(value="新值"), "新值",
                               allow_focus=False) is True
    assert probe.calls == [], "allow_focus=False 时不该聚焦"


def test_driver_set_value_reports_readback_mismatch(monkeypatch):
    """★ 写后回读：**确实读到且对不上**才返回 False。

    这是替代"先 SetFocus 再写"的那条防线 —— 有些 provider 会接受 `SetValue` 但静默不
    生效，回读是发现它的唯一办法。返回 False 会让协调层知道这一步没成（而不是报成功）。
    """
    monkeypatch.setattr(w_actions.nodes, "pattern_available",
                        lambda el, name: name == "ValuePattern")
    el = _FakeEdit(value="旧值")           # SetValue 不改 _value，模拟"接受但不生效"
    assert w_actions.set_value(el, "新值", allow_focus=False) is False
    assert el.set_calls == ["新值"], "应该确实写了（只是没生效）"


def test_driver_set_value_treats_unreadable_value_as_success(monkeypatch):
    """★ 读不到 = **无法验证**，不是失败。

    把"验证不了"报成 False 的后果很实在：协调层会认为元素级没成，于是降级到键盘注入
    （real 模式下）或直接报失败（隔离模式下）。用一个读不出来的探测器去触发一次
    真实键盘注入，是自伤。
    """
    monkeypatch.setattr(w_actions.nodes, "pattern_available",
                        lambda el, name: name == "ValuePattern")
    assert w_actions.set_value(_FakeEdit(explode=True), "新值", allow_focus=False) is True


def test_driver_set_value_does_not_focus_even_when_allowed(monkeypatch):
    """反向正控：`allow_focus=True` 时**才**会去聚焦（否则上面那条"没聚焦"是恒真的）。

    与 `test_focus_is_a_policy_noop_in_isolated_mode` 是同一把尺子的另一处：两处都在
    "跳过聚焦"，但原因不同（那里是策略，这里是参数）。
    """
    monkeypatch.setattr(w_actions.nodes, "pattern_available",
                        lambda el, name: name == "ValuePattern")
    probe = _Recorder()
    monkeypatch.setattr(w_actions, "focus", probe)
    w_actions.set_value(_FakeEdit(value="新值"), "新值", allow_focus=True)
    assert len(probe.calls) == 1


def test_driver_set_value_tolerates_numeric_reformatting(monkeypatch):
    """★ 归一化比较：`1.50` vs `1.5` **不是**失败。

    控件把数字规范化是常态；把这种差异当成失败同样会触发上面那条自伤路径。
    """
    el = _FakeEdit(value="1.5")
    # RangeValuePattern 分支：桩里没有 GetRangeValuePattern，故这里只验纯比较函数
    assert w_actions._verify_value(el, "1.5", numeric=False) is True
    assert w_actions._verify_value(el, " 1.5 ", numeric=False) is True
    el2 = _FakeEdit(value="2")
    assert w_actions._verify_value(el2, "3", numeric=False) is False


# ── ⑤ 策略函数本身（纯函数，四条组合都要断）──────────────────────────────

def _env(mode: str, allow: str | None = None) -> dict[str, str]:
    env = {"CC_CU_DISPLAY_MODE": mode}
    if allow is not None:
        env["CC_CU_ALLOW_GLOBAL_INPUT"] = allow
    return env


def test_display_mode_only_recognises_the_literal_real():
    """★ 模式解析只认 `"real"` 字面量；其余一切（含拼错）都当 isolated。

    防的是"拼错一个字母就静默失去隔离" —— 那是本项目最不能接受的失败形态之一。
    """
    assert display_mode_from_env(_env("real")) == "real"
    assert display_mode_from_env(_env("REAL")) == "real"
    assert display_mode_from_env(_env(" real ")) == "real"
    for bad in ("isolated", "", "reall", "rela", "true", "1", "yes"):
        assert display_mode_from_env(_env(bad)) == "isolated", f"{bad!r} 不该放行隔离"


def test_global_input_switch_only_recognises_the_literal_one():
    """★ 放开全局注入只认 `"1"`：这是"请动我的鼠标键盘"级别的授权。"""
    assert global_input_allowed(_env("isolated", "1")) is True
    for bad in ("true", "TRUE", "yes", "on", "01", "2", "", "0"):
        assert global_input_allowed(_env("isolated", bad)) is False, f"{bad!r} 不该放行"


def test_refusal_and_note_are_exact_complements():
    """★ 四条组合：拒绝与提醒**互斥且覆盖**，一条都不许含糊。

    | 模式     | 开关 | 拒绝 | 响亮提醒 |
    | isolated | 开   | 否   | 是       |
    | isolated | 关   | 是   | 否       |
    | real     | 任意 | 否   | 否       |

    「real + 开关」那一格必须是"不拒绝、也不提醒"：real 模式下全局注入天经地义
    （用户自己选的），每次都在结果里喊一遍只会变成噪音，而噪音的下一站是被忽略。
    """
    combos = [
        ("isolated", "1", False, True),
        ("isolated", None, True, False),
        ("real", "1", False, False),
        ("real", None, False, False),
    ]
    for mode, allow, refused, noted in combos:
        env = _env(mode, allow)
        assert bool(global_input_refusal("op", env)) is refused, f"拒绝判据错: {mode}/{allow}"
        assert bool(global_input_note(env)) is noted, f"提醒判据错: {mode}/{allow}"


def test_session_provider_capabilities_follows_the_policy(monkeypatch):
    """★ 能力申报按**实际策略**算 —— 两个轴都断，且两个轴会一起翻转。

      · 严格档：用户安全（`input_isolated=True`），代价是能力缺一项
        （`unimplemented` 里有 coordinate_input）；
      · 放开档：能力齐了（`unimplemented` 为空），代价是安全没了
        （`input_isolated=False`）。

    报错方向的代价不对称：把 False 报成 True，用户会以为 agent 待在另一个桌面里 ——
    而它正在动他的鼠标。所以两条都要断，只断一边挡不住"恒定申报 True"的实现。
    """
    provider = WindowsDesktopSession()

    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)
    caps = provider.capabilities()
    assert caps.input_isolated is True
    assert caps.screen_isolated is True and caps.a11y_isolated is True
    assert caps.target == "isolated"
    assert "coordinate_input" in caps.unimplemented
    assert any("拒绝" in n for n in caps.notes), caps.notes

    monkeypatch.setenv("CC_CU_ALLOW_GLOBAL_INPUT", "1")
    caps = provider.capabilities()
    assert caps.input_isolated is False, (
        "放开全局注入之后必须申报 False —— 这一下真的会动用户的键鼠"
    )
    assert caps.unimplemented == ()
    assert any("ALLOW_GLOBAL_INPUT" in n for n in caps.notes), caps.notes

    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "real")
    caps = provider.capabilities()
    assert caps.target == "real" and caps.input_isolated is False
    assert caps.screen_isolated is False and caps.a11y_isolated is False


def test_session_gate_is_scoped_to_the_global_kind(monkeypatch):
    """★ 闸门**只**卡 `OP_INJECT_GLOBAL` 那一档。

    卡住默认档（`OP_INJECT`）会把元素级 `Invoke`/`SetValue` 一起误杀 —— 而它们是隔离
    模式下唯一还能用的通道，且那是跨进程消息、根本不碰输入设备。这条同时钉住了
    "闸门是按种类分档的"这个设计（否则它与一把无差别的大锁没有区别）。
    """
    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)
    provider = WindowsDesktopSession()
    provider.require("click_xy", kind=OP_INJECT)          # 不抛
    with pytest.raises(GlobalInputRefusedError):
        provider.require("click_xy", kind=OP_INJECT_GLOBAL)

    monkeypatch.setenv("CC_CU_ALLOW_GLOBAL_INPUT", "1")
    WindowsDesktopSession().require("click_xy", kind=OP_INJECT_GLOBAL)   # 放开后不抛


def test_session_and_driver_never_disagree(monkeypatch):
    """★ 两道闸门（层 3 的 provider 与层 1 的 driver）判据**同源**，不许出现一处放行一处拒绝。

    它们各有一份入口是刻意的（driver 那一道要挡住"绕过协调层直接调 backend"的路径），
    但判据必须是同一个纯函数 —— 两份实现迟早有一份改漏，而漏掉的表现是
    "隔离看起来是开的、实际某一条路能穿过去"，发现它的唯一时机是用户发现鼠标在动。
    """
    for mode, allow in (("isolated", None), ("isolated", "1"), ("real", None)):
        env = _env(mode, allow)
        monkeypatch.setenv("CC_CU_DISPLAY_MODE", mode)
        if allow is None:
            monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)
        else:
            monkeypatch.setenv("CC_CU_ALLOW_GLOBAL_INPUT", allow)

        try:
            WindowsDesktopSession().require("op", kind=OP_INJECT_GLOBAL)
            session_refused = False
        except GlobalInputRefusedError:
            session_refused = True
        try:
            w_desktop.require_global_input("op")
            driver_refused = False
        except GlobalInputRefusedError:
            driver_refused = True

        assert session_refused == driver_refused, f"两道闸门答案不一致：{env}"
        assert session_refused == bool(global_input_refusal("op", env))


# ── ⑥ 协调层的行为：闸门真的挡在 backend 调用之前 ────────────────────────

class _RefusingSession:
    """一个"永远拒绝全局注入"的会话 provider（只实现被测路径用到的方法）。

    ⚠️ `ensure_ready` 必须有：`Coordinator` 每个对外方法都挂着 `@_needs_display`，
    它第一件事就是 `session.MANAGER.ensure_ready()` —— 少这个方法会得到一个
    `AttributeError`，而那个错看起来与"闸门没生效"一模一样（都是 raise），
    极易把一次测试写坏当成产品缺陷。
    """

    def ensure_ready(self):  # noqa: ANN201
        return {}

    def require(self, op, *, kind=OP_INJECT):  # noqa: ANN001, ANN201
        if kind == OP_INJECT_GLOBAL:
            raise GlobalInputRefusedError("（桩）隔离模式下拒绝全局注入，请改用元素级")

    def guard(self):  # noqa: ANN201
        return None

    def global_input_note(self):  # noqa: ANN201
        return None


def test_coordinator_click_refuses_before_touching_the_backend(monkeypatch):
    """★ 协调层的闸门必须**在 backend 之前**生效（行为判据，不是源码顺序）。

    判据取「backend 一次都没被调用」而不是"抛了错"：后者在 backend 自己会抛错的
    实现下也成立，分不出闸门到底拦没拦住。
    """
    monkeypatch.setattr(session, "MANAGER", _RefusingSession())
    b = _StubBackend()
    from computer_use_mcp.core.coordinator import Coordinator
    coord = Coordinator(b)
    with pytest.raises(GlobalInputRefusedError):
        coord.click_xy(5, 6)
    assert b.calls == [], f"闸门应该拦在 backend 之前，实际调用了 {b.calls}"


_GATED_ENTRY_POINTS = [
    ("click_xy", lambda c: c.click_xy(1, 2, preview=False)),
    ("scroll_xy", lambda c: c.scroll_xy("down", 1, x=1, y=2)),
    ("drag_xy", lambda c: c.drag_xy(1, 2, 3, 4)),
    ("press_key", lambda c: c.press_key("ctrl+s")),
    ("type_text（无 ref，直接走键盘兜底）", lambda c: c.type_text("hi")),
]


def test_gate_refusal_shape_is_uniform_across_entry_points(monkeypatch):
    """★ 被拒时的**形态必须一致：抛 `GlobalInputRefusedError`**。

    真机实测踩到过一次（2026-09-30）：`type_text` 那一路把拒绝**转成了**
    `ActionResult(ok=False, message=…)` 返回，而 `click_xy` / `press_key` 几个入口都是抛
    异常 —— 于是探针（以及任何按"抛没抛"判断的调用方）把 `type_text` 那次读成
    **"居然成功了"**。同一道闸门有两种形态，就意味着"它到底拦没拦"要靠**读文案**才知道，
    而那正是本项目最忌讳的失效形态。

    ⚠️ 刻意写成**一次遍历 + 汇总报告**而不是 `parametrize`：反证脚本
    （`tests/manual_windows_guards_reverse.py`）要把破坏种进来再调这个函数，
    而 `parametrize` 的用例在 pytest 之外没法直接调（得自己拼出逐个参数）。
    顺带它还有个好处：一次跑完能**列出全部**形态不对的入口，而不是只报第一个。

    这条用例是那条真机判据的**单元版** —— 它不需要客体，所以每次跑 pytest 都会把关一次。
    """
    monkeypatch.setattr(session, "MANAGER", _RefusingSession())
    from computer_use_mcp.core.coordinator import Coordinator

    offenders: list[str] = []
    for label, call in _GATED_ENTRY_POINTS:
        try:
            call(Coordinator(_StubBackend()))
        except GlobalInputRefusedError:
            continue
        except Exception as exc:  # noqa: BLE001 —— 别的异常 = 拦是拦了，但不是这道闸门
            offenders.append(f"{label} → {type(exc).__name__}: {exc}")
        else:
            offenders.append(f"{label} → **返回了（没抛）**")
    assert not offenders, (
        "这些入口被拒时的形态与其他入口不一致（应当是抛 GlobalInputRefusedError）：\n  "
        + "\n  ".join(offenders)
    )


def test_element_level_click_still_works_when_the_coordinate_fallback_is_refused(monkeypatch):
    """★ 反向正控：元素级成功时**根本不碰闸门**（否则隔离模式下 `click(ref=...)` 会全废）。

    造一个"元素级能成功"的 backend：此时必须在坐标兜底**之前**就返回，闸门一次都不该被问到。
    判据取「backend 没被点过」而不是"没抛异常" —— 后者在闸门压根没被调用的实现下也成立。
    """
    refused_calls: list[str] = []

    class _Session(_RefusingSession):
        def require(self, op, *, kind=OP_INJECT):  # noqa: ANN001, ANN201
            refused_calls.append(kind)
            if kind == OP_INJECT_GLOBAL:
                raise GlobalInputRefusedError("（桩）不该走到这里")

    monkeypatch.setattr(session, "MANAGER", _Session())
    b = _StubBackend()                       # invoke → True（非 TextBlock 一律成功）
    # ⚠️ `_StubBackend.is_alive` 的默认是"非 TextBlock 一律判失效"（那是它给重定位那组
    #    用例准备的驱动条件）。这里要的是**元素级真能成**，所以必须覆盖它 ——
    #    否则 ref 会先走重定位、再抛 InvalidRefError，红的样子与"闸门挂错位置"一模一样。
    b.is_alive = lambda n: True
    from computer_use_mcp.core.coordinator import Coordinator
    coord = Coordinator(b)
    ref = coord.refs.register(object(), role="push button", name="确定", app="stub")
    res = coord.click(ref)
    assert res.ok and res.level == "element", res.message
    assert b.calls == [], f"元素级成功就不该去点坐标，实际 {b.calls}"
    # ⚠️ 判据是**具体哪一档**没被问到，不是"一次都没问"：`@_exclusive_screen` 本来就会拿
    #    默认档（OP_INJECT）问一次 —— 那一档在隔离模式下是放行的（元素级不碰输入设备）。
    #    这里要证明的是：**全局注入那一档一次都没被问到**，也就是闸门没挂在方法开头。
    assert OP_INJECT_GLOBAL not in refused_calls, (
        f"元素级自己就能成，全局注入闸门不该被问到（被问到就说明它挂在方法开头了）: "
        f"{refused_calls}"
    )


def test_coordinator_reports_the_loud_note_when_globally_allowed(monkeypatch):
    """★ 放开时结果里要**响亮地**说"这一下动的是你的键鼠"。

    模型看不到环境变量，它只看到"点击成功"。不带这句话，它就会以为这一下落在隔离
    环境里，继续按那个假设决策（"我点的是隐藏桌面，碰不到用户的东西"）。
    """
    note = "⚠️ 本次使用全局注入（测试哨兵）"

    from computer_use_mcp.core.coordinator import Coordinator

    # ⚠️ 正控必须**先跑**：`monkeypatch.setattr(session, "MANAGER", …)` 在本用例内一直
    #    有效，跑在后面的话它量的还是那个打了桩的会话（"没带上"于是恒不成立）。
    #    没有这条正控，"带上了"可能来自别处（结果文案里本来就有的别的警告）。
    res_plain = Coordinator(_StubBackend()).click_xy(5, 6, preview=False)
    assert note not in res_plain.message

    class _AllowedSession(_RefusingSession):
        def require(self, op, *, kind=OP_INJECT):  # noqa: ANN001, ANN201
            # 放开档：**不拒绝**（这正是被测的那一半）。上面那个桩是"永远拒绝"，
            # 不覆盖它的话本条会红在闸门上，而红的样子与"提醒没带上"完全分不开。
            return None

        def global_input_note(self):  # noqa: ANN201
            return note

    monkeypatch.setattr(session, "MANAGER", _AllowedSession())
    b = _StubBackend()
    res = Coordinator(b).click_xy(5, 6, preview=False)
    assert note in res.message, f"结果里没带上全局注入的提醒：{res.message!r}"


# ── ⑦ 能力边界要报到**模型开工前就读到**的地方 ──────────────────────────

def test_startup_caveat_is_none_on_linux():
    """★ Linux 的 `startup_caveat()` 必须是 None。

    ⚠️ 这一条防的是一种很隐蔽的错：把 `capabilities().notes` 直接返回出去。那边的文案
    随**运行时状态**变（"隔离沙箱未就绪……"在沙箱起来之后就不成立了），而
    `startup_caveat` 的消费方会把它拼进 MCP `instructions` —— 那是模型**开工前**就读到、
    并且整场当既定事实用的文字。用一份会变的现状去填它，得到的就是一句从会话开头到
    结尾都错、且没人会怀疑的话。
    """
    assert session.MANAGER.startup_caveat() is None


def test_windows_startup_caveat_is_stable_and_actionable(monkeypatch):
    """★ Windows 隔离模式下要有一段**稳定且可执行**的能力边界说明。

    "稳定"由实现保证（只读两个环境变量）；这里断的是内容必须**给出路** ——
    只说"不可用"而不说"那用什么"，模型会反复重试同一条路。
    """
    provider = WindowsDesktopSession()
    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "isolated")
    monkeypatch.delenv("CC_CU_ALLOW_GLOBAL_INPUT", raising=False)
    text = provider.startup_caveat()
    assert text and "元素级" in text, text
    assert "ref" in text

    monkeypatch.setenv("CC_CU_ALLOW_GLOBAL_INPUT", "1")
    text2 = provider.startup_caveat()
    assert text2 and "ALLOW_GLOBAL_INPUT" in text2 and "用户" in text2

    monkeypatch.setenv("CC_CU_DISPLAY_MODE", "real")
    assert provider.startup_caveat() is None, "real 模式没有要报的稳定边界"


def test_server_instructions_carry_the_capability_caveat(monkeypatch):
    """★ 接线判据：`create_server()` 的 instructions 必须真的**拼上**那段边界说明。

    只断"`_capability_caveat` 这个函数存在"是不够的 —— 写好了却不调用，与没写
    在模型眼里完全一样（它读不到）。所以这里打桩成哨兵、从**真实创建出来的 server**
    上把它读回来。
    """
    sentinel = "★本会话隔离边界哨兵★"
    monkeypatch.setattr(session.MANAGER, "startup_caveat", lambda: sentinel)
    monkeypatch.setattr(session, "MANAGER", session.MANAGER)

    import importlib

    from computer_use_mcp import server as server_mod
    importlib.reload(server_mod)
    try:
        mcp = server_mod.create_server()
        text = getattr(mcp, "instructions", "") or ""
        assert sentinel in text, (
            "能力边界没有进 instructions —— 模型要等撞上一次拒绝才知道，白耗一轮"
        )
    finally:
        # ⚠️ 必须还原：本模块在别处（`test_tools_and_backend` 等）被按原样使用，
        #    留着哨兵会让那些用例读到一段假 instructions。
        importlib.reload(server_mod)


def test_windows_input_unit_tests_declare_their_display_mode():
    """★ 直接调 Windows 注入方法的单测必须**显式声明**显示模式（或明说它测的就是拒绝）。

    判据的由来（2026-09-30 由 `CC_CU_E2E=1` 那一遍抓出来）：`conftest.py` 只在**非 e2e**
    时 `setdefault("CC_CU_DISPLAY_MODE", "real")`，而 e2e 分支刻意不干预（保留「默认在
    沙箱内跑」的约定）。于是三个直接调 `WindowsBackend.click_at/drag_at` 的用例在 e2e 下
    拿到的是 `isolated`，撞上本轮的全局注入闸门而红 —— **默认跑法全绿，只有带 e2e 开关
    才红**。

    那不是产品缺陷，是**用例偷偷依赖了环境**（与 zenity 那个「中文默认标签」的假红同一类：
    在开发机上永远重现不了，最耗排查时间）。所以凡是拿默认模式当前提的注入用例，
    要么把模式写出来（`CC_CU_DISPLAY_MODE` / `_real_mode(`），要么明说它测的就是那道拒绝
    （出现 `GlobalInputRefusedError`）—— 三种写法都意味着作者**想过**这件事。

    ✅ **已反证**（2026-09-30）：手动摘掉 `test_drag_at_focuses_the_window_under_the_start_point`
    里的 `_real_mode(monkeypatch)` 之后，本条立刻点名报出那一条（不是笼统地"有一条不合格"）。
    """
    calling = ("click_at(", "drag_at(", "type_text(", "press_key(")
    declared = ("CC_CU_DISPLAY_MODE", "_real_mode(", "GlobalInputRefusedError")

    checked: list[str] = []
    offenders: list[str] = []
    for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body = ast.get_source_segment(path.read_text(encoding="utf-8"), fn) or ""
            # 只看**Windows backend** 的注入方法：协调层的方法另有闸门用例覆盖
            if "WindowsBackend(" not in body:
                continue
            if not any(c in body for c in calling):
                continue
            checked.append(f"{path.name}::{fn.name}")
            if not any(d in body for d in declared):
                offenders.append(f"{path.name}::{fn.name}")

    assert len(checked) >= 3, (
        f"只扫到 {checked} 个直接调 Windows 注入方法的用例 —— 少于已知的 3 个，"
        f"扫描逻辑写错了，本规则正在空转"
    )
    assert not offenders, (
        "这些用例直接调了 Windows 的注入方法，却没有声明显示模式（也没有说明它们测的"
        "就是拒绝）—— 它们在默认跑法与 `CC_CU_E2E=1` 下会走**不同的分支**：\n  "
        + "\n  ".join(offenders)
    )


def test_session_capabilities_type_is_importable_from_session_types():
    """伞式正控：本文件断言的类型确实来自被测模块（防止 import 写错后全文件测空气）。"""
    assert SessionCapabilities.__module__.endswith("session_types")
    assert WindowsDesktopSession().kind() in ("isolated", "real")
