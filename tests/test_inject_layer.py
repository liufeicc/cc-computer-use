"""
inject 层（xdotool 通道）：键名归一、输入分流、窗口枚举与几何解析（拆分自 test_optimizations.py）。
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




# ---------- 键名归一 ----------
def test_normalize_combo_aliases():
    n = XdotoolInjector._normalize_combo
    assert n("ctrl+pagedown") == "ctrl+Page_Down"
    assert n("Enter") == "Return"
    assert n("esc") == "Escape"
    assert n("ctrl+shift+arrowdown") == "ctrl+shift+Down"
    # 未知 token 原样保留
    assert n("ctrl+a") == "ctrl+a"
    assert n("alt+F4") == "alt+F4"





def test_press_key_fail_loud_on_unknown_key(monkeypatch):
    """xdotool 对未知 keysym rc 仍为 0 但 stderr 有 warning，必须判失败抛错。"""
    inj = XdotoolInjector()
    monkeypatch.setattr(inj, "_run", lambda *a, **k: _proc(0, "", "No such key name 'PageDown'. Ignoring it."))
    with pytest.raises(InjectionError):
        inj.press_key("PageDown")





def test_press_key_ok_and_normalized(monkeypatch):
    inj = XdotoolInjector()
    captured = {}

    def fake_run(args, **k):
        captured["args"] = args
        return _proc(0)

    monkeypatch.setattr(inj, "_run", fake_run)
    assert inj.press_key("ctrl+pagedown") is True
    assert "ctrl+Page_Down" in captured["args"]





# ---------- 输入分流 ----------
def test_type_text_long_ascii_uses_clipboard(monkeypatch):
    inj = XdotoolInjector()
    used = {"clip": False, "type": False}

    def fake_run(args, **k):
        if args and args[0] == "type":
            used["type"] = True
        return _proc(0)

    def fake_clip(text):
        used["clip"] = True
        return True

    monkeypatch.setattr(inj, "_run", fake_run)
    monkeypatch.setattr(inj, "_type_via_clipboard", fake_clip)

    inj.type_text("abc")            # 短 ASCII → xdotool type
    assert used == {"clip": False, "type": True}

    used.clear(); used.update({"clip": False, "type": False})
    inj.type_text("a" * 30)         # 长 ASCII → 剪贴板
    assert used == {"clip": True, "type": False}





# ---------- 单进程串命令 ----------
def test_click_at_single_chained_run(monkeypatch):
    """带 focus_wid 时 activate/focus/move/click 应合并为一次 _run。"""
    inj = XdotoolInjector()
    calls = []
    monkeypatch.setattr(inj, "_run", lambda args, **k: (calls.append(args), _proc(0))[1])
    inj.click_at(100, 200, focus_wid="12345")
    assert len(calls) == 1
    assert calls[0][0] == "windowactivate" and "mousemove" in calls[0] and calls[0][-2] == "click"





def test_focus_window_single_run(monkeypatch):
    """
    聚焦 + 点击必须合进**一次** xdotool 进程（M-35 后本用例改为直接盯 click_at）。

    原先它盯的是 `XdotoolInjector.focus_window`——那个方法生产代码零调用方，真正生效的是
    `click_at` 里内联的同一条链路。两份实现会各自漂移，故删掉方法、把这条回归改到实路上。
    """
    inj = XdotoolInjector()
    calls = []
    monkeypatch.setattr(inj, "_run", lambda args, **k: (calls.append(args), _proc(0))[1])
    inj.click_at(100, 200, focus_wid="999")
    assert len(calls) == 1
    assert "windowactivate" in calls[0] and "windowfocus" in calls[0]





# ---------- 批量几何解析 ----------
def test_parse_multi_geometry():
    out = (
        "Window 111\n"
        "  Position: 10,20 (screen: 0)\n"
        "  Geometry: 800x600\n"
        "Window 222\n"
        "  Position: -5,0 (screen: 1)\n"
        "  Geometry: 1920x1080\n"
    )
    g = XdotoolInjector._parse_multi_geometry(out)
    assert g["111"] == (10, 20, 800, 600)
    assert g["222"] == (-5, 0, 1920, 1080)





# ---------- 隔离沙箱：DISPLAY 贯穿 / list_windows 错位防御 / click_xy / 礼让 ----------
def test_run_env_carries_target_display(monkeypatch):
    """
    所有 xdotool 子进程必须带 DISPLAY env，且必须是**目标屏**而不是宿主屏
    （唯一来源 core.display，沙箱隔离的根）。

    ⚠️ 必须人为制造**两个可区分**的取值（I-13，见 docs/REVIEW/review_0.1.0.md）：
    conftest 把单测强制成 real 模式，此时 effective_display() 就是 host_display()
    即 os.environ["DISPLAY"]，于是「断言 env 的 DISPLAY 等于 effective_display()」
    退化成 `X == X` —— 恒真，注入静默回落宿主桌面也照样全绿。而 CLAUDE.md 明令
    「禁止各处直接读 os.environ["DISPLAY"]」，本用例是那条禁令的兜底之一。
    """
    inj = XdotoolInjector()
    captured = {}

    def fake_subprocess_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _proc(0)

    monkeypatch.setattr(inject_mod.subprocess, "run", fake_subprocess_run)
    # 宿主屏与沙箱屏**不同**：宿主 :7、沙箱 :77。于是「拿到了 env」和「env 指向目标屏」
    # 成了两件必须分别成立的事。
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(display_mod.MANAGER, "_sandbox_display", ":77")
    monkeypatch.setattr(display_mod.MANAGER, "is_sandbox_up", lambda: True)

    inj._run(["getactivewindow"])
    assert captured["env"] is not None, "子进程必须显式带 env（不能靠继承）"
    assert captured["env"]["DISPLAY"] == ":77", \
        f"注入必须打到沙箱屏 :77，实际 {captured['env']['DISPLAY']!r}"
    assert captured["env"]["DISPLAY"] != display_mod.MANAGER.host_display(), \
        "注入不得落到宿主屏（这正是「各处直接读 os.environ['DISPLAY']」的回归形态）"





def test_list_windows_fallback_keeps_every_window(monkeypatch):
    """
    退化路径（无 WM → 无 wmctrl）必须**逐窗**读取，一个窗口都不能丢。

    ⚠️ 这是 2026-09-15 实测 bug 的回归测试：本机 xdotool 3.20160805.1 的
    getwindowname/getwindowpid/getwindowgeometry **都是单窗口命令**，`xdotool
    getwindowgeometry wid1 wid2` 会把第二个 id 当成命令名（`Unknown command`），
    stdout 只回第一个窗口。旧实现照批量结果组装 → 其余窗口因「几何缺失」被当
    辅助窗过滤 → 桌面上有 DBeaver+Chrome 时 list_windows 只返回 1 个 i3 根窗，
    **且不报错**。静默的错误答案比报错危险得多。

    注意 fake 必须复现「批量只回第一项」这个真实行为，否则测不出该 bug。
    """
    inj = XdotoolInjector()
    per_wid = {"1": ("Alpha", 10, (0, 0, 800, 600)),
               "2": ("Beta", 20, (0, 0, 0, 0)),        # 零面积 → 应被过滤
               "3": ("Gamma", 30, (10, 10, 640, 480))}

    def fake_run(args, **k):
        if args[0] == "search":
            return _proc(0, "1 2 3\n")
        if len(args) != 2:                       # 复现真实行为：多传 id 一律失败
            return _proc(1, f"{per_wid[args[1]][0]}\n" if args[0] == "getwindowname" else "",
                         f"Unknown command: {args[2]}")
        wid = args[1]
        if args[0] == "getwindowname":
            return _proc(0, per_wid[wid][0] + "\n")
        if args[0] == "getwindowpid":
            return _proc(0, f"{per_wid[wid][1]}\n")
        if args[0] == "getwindowgeometry":
            x, y, w, h = per_wid[wid][2]
            return _proc(0, f"Window {wid}\n  Position: {x},{y} (screen: 0)\n  Geometry: {w}x{h}\n")
        raise AssertionError(f"未预期的调用: {args}")

    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: None)   # 强制退化路径
    monkeypatch.setattr(inj, "_run", fake_run)
    out = inj.list_windows()
    assert {d["title"] for d in out} == {"Alpha", "Gamma"}, \
        f"除首个窗外的窗口必须保留（旧实现会全丢），实际 {[d['title'] for d in out]}"
    assert [d["pid"] for d in out] == [10, 30], "PID 也必须逐窗正确对位"
    assert out[0]["title"] == "Alpha" and out[0]["area"] == 480000





def test_list_windows_wmctrl_parsing(monkeypatch):
    """
    wmctrl 路径：id 必须由 0x 十六进制**归一成十进制**（与 getactivewindow /
    落点证据给的 id 同形，否则模型没法把这里的 id 喂给 wait_window(window_id=…)）；
    标题含空格必须完整保留（split 限次）；零面积/无名窗过滤。
    """
    inj = XdotoolInjector()
    stdout = (
        "0x00a00080  0 52230  4    36   1596 961  DBeaver.DBeaver       liufei DBeaver 24.2.2 - <AI中台开发环境> Script-31\n"
        "0x00e00003  0 53694  804  36   796  961  google-chrome.Google-chrome liufei Download | DBeaver Community - Google Chrome\n"
        "0x00f00001  0 0      0    0    0    0    foo.Bar               liufei 零面积辅助窗\n"
        "0x00f00002  0 0      0    0    100  100  foo.Bar               liufei \n"
        "截断行\n"
    )
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/wmctrl")
    monkeypatch.setattr(inject_mod.subprocess, "run",
                        lambda *a, **k: _proc(0, stdout))
    out = inj.list_windows()
    assert [d["title"] for d in out] == [
        "DBeaver 24.2.2 - <AI中台开发环境> Script-31",
        "Download | DBeaver Community - Google Chrome",
    ], [d["title"] for d in out]
    assert out[0]["id"] == str(0x00a00080) == "10485888", "id 必须是十进制字符串"
    assert out[0]["pid"] == 52230 and out[0]["area"] == 1596 * 961
    assert out[0]["x"] == 4 and out[0]["y"] == 36





def test_list_windows_wmctrl_failure_falls_back(monkeypatch):
    """wmctrl 返回码非 0（无 WM / CC_CU_SANDBOX_WM=none）→ 退化 xdotool，不能返回空表。"""
    inj = XdotoolInjector()
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/wmctrl")
    monkeypatch.setattr(inject_mod.subprocess, "run", lambda *a, **k: _proc(1, "", "Cannot get client list"))

    def fake_run(args, **k):
        if args[0] == "search":
            return _proc(0, "7\n")
        if args[0] == "getwindowname":
            return _proc(0, "Fallback Window\n")
        if args[0] == "getwindowpid":
            return _proc(0, "123\n")
        if args[0] == "getwindowgeometry":
            return _proc(0, "Window 7\n  Position: 1,2 (screen: 0)\n  Geometry: 300x200\n")
        raise AssertionError(f"未预期的调用: {args}")

    monkeypatch.setattr(inj, "_run", fake_run)
    out = inj.list_windows()
    assert [d["title"] for d in out] == ["Fallback Window"]
    assert out[0]["id"] == "7" and out[0]["pid"] == 123





# ---------- 非预期异常不得逃逸到 MCP 层（I-5）----------
# 背景：mcp 2.x 对非 MCPError 异常**刻意不带原文**地包装成 UnexpectedToolError，
# 模型只拿到 "Error executing tool X"——零信息量。故各层都要把异常收口成
# ComputerUseError，工具层再补一层统一兜底。
def test_run_survives_non_utf8_subprocess_output():
    """
    回归（I-5）：子进程输出含**非 UTF-8 字节**时，_run 必须容错解码。

    真实触发场景：X11 窗口标题是任意字节串（xdotool 原样输出），旧式应用会给非法
    UTF-8。默认的严格解码抛 UnicodeDecodeError，而它不是 InjectionError，会穿透
    inject 的错误契约逃到 MCP 层，且该工具会**持续**不可用。

    这里用 /usr/bin/printf 真的吐一个 0xFF 字节来验证，不是断言「kwarg 传了没」。
    """
    import shutil

    printf = shutil.which("printf")
    if not printf:
        pytest.skip("需要 printf 才能构造非 UTF-8 输出")

    inj = XdotoolInjector()
    inj._bin = printf
    p = inj._run(["\\377"])          # printf 把 \377 解释成单字节 0xFF

    assert p.returncode == 0
    assert p.stdout != "", "应拿到替换字符，而不是抛 UnicodeDecodeError"





def test_launch_app_missing_command_gives_actionable_error():
    """回归（I-5）：命令不存在时抛 ComputerUseError（含命令名），而不是裸 FileNotFoundError。"""
    coord = Coordinator(backend=_StubBackend())

    with pytest.raises(ComputerUseError) as ei:
        coord.launch_app("/nonexistent/cc-cu-no-such-binary-xyz")

    msg = str(ei.value)
    assert "启动失败" in msg and "cc-cu-no-such-binary-xyz" in msg





def test_launch_app_bad_quoting_gives_actionable_error():
    """回归（I-5）：引号不闭合时抛 ComputerUseError，而不是裸 ValueError。"""
    coord = Coordinator(backend=_StubBackend())

    with pytest.raises(ComputerUseError) as ei:
        coord.launch_app("zenity --title='未闭合")

    assert "命令行解析失败" in str(ei.value)


# ---------- 单实例应用必须补「去单实例」参数 ----------
#
# 2026-09-24 实测：`launch_app("gnome-terminal")` 报 ok、display=":0"，窗口却出现在
# **宿主 :1**。原因是这类 GTK/GApplication 应用在会话总线上的单实例语义——只改 DISPLAY
# 搬不动它，第二次启动只是请**已有实例**开个窗口。修法是补上应用自带的去单实例开关。


def test_desingleton_args_injected_for_singleton_apps():
    """判据是**实际执行的 argv**，不是返回值里的 command（那串永远是用户原话）。"""
    from computer_use_mcp.core.coordinator.windows import _desingleton_argv

    assert _desingleton_argv(["gnome-terminal"]) == ["gnome-terminal", "--disable-factory"]
    # 只认 basename：绝对路径、带 .real 后缀的变体都要命中
    assert _desingleton_argv(["/usr/bin/gnome-terminal"]) == [
        "/usr/bin/gnome-terminal", "--disable-factory"]
    assert _desingleton_argv(["gedit"]) == ["gedit", "--standalone"]
    # 参数补在**选项区最前面**：`--` 之后的位置参数不能被顶掉
    assert _desingleton_argv(["gnome-terminal", "--", "bash"]) == [
        "gnome-terminal", "--disable-factory", "--", "bash"]


def test_desingleton_args_leave_everything_else_untouched():
    """
    反向（比正向更要紧）：**不认识的应用一个字都不许改**。
    模糊匹配或"猜一个参数塞进去"会把本来好的启动弄坏，而且只有那一类应用会出问题。
    """
    from computer_use_mcp.core.coordinator.windows import _desingleton_argv

    assert _desingleton_argv(["zenity", "--entry"]) == ["zenity", "--entry"]
    # 只是名字里含 gnome-terminal 的别的程序，不得命中
    assert _desingleton_argv(["my-gnome-terminal-wrapper"]) == ["my-gnome-terminal-wrapper"]
    assert _desingleton_argv(["gnome-terminal-helper"]) == ["gnome-terminal-helper"]
    # 调用方自己已经加过 → 不重复加（两份会被应用当成语法错）
    assert _desingleton_argv(["gnome-terminal", "--disable-factory"]) == [
        "gnome-terminal", "--disable-factory"]


def test_launch_app_popen_receives_desingleton_argv(monkeypatch):
    """端到端一点的那条：确认 launch_app 真的把补过参数的 argv 交给了 Popen。"""
    from computer_use_mcp.core.coordinator import windows as coord_windows

    seen: dict = {}

    class _P:
        pid = 4242

        def wait(self):
            return 0

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _P()

    monkeypatch.setattr(coord_windows.subprocess, "Popen", fake_popen)
    coord = Coordinator(backend=_StubBackend())
    out = coord.launch_app("gnome-terminal", settle=0)

    assert seen["argv"] == ["gnome-terminal", "--disable-factory"]
    assert out["argv"] == ["gnome-terminal", "--disable-factory"], "返回值也要如实回报"
    assert out["command"] == "gnome-terminal", "原命令串保持原样，便于对照"





def test_inject_window_title_handles_missing(monkeypatch):
    inj = XdotoolInjector()
    assert inj.window_title("") is None
    monkeypatch.setattr(inj, "_read_titles", lambda wids: [""])
    assert inj.window_title("123") is None, "空标题应归一为 None，而不是空串"
    monkeypatch.setattr(inj, "_read_titles", lambda wids: ["DBeaver"])
    assert inj.window_title("123") == "DBeaver"
