# -*- coding: utf-8 -*-
"""
`backend/linux/` 注入、抓屏与灰区感知的回归集（REVIEW 第四节 M-30 ~ M-39）。

全部用例都是**纯内存**的：不起 xdotool / mss / tesseract 子进程（需要拦住的那几个
`subprocess.run` 调用点在用例里被替换成记录器）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.backend.linux import grab as grab_mod  # noqa: E402
from computer_use_mcp.backend.linux import inject as inject_mod  # noqa: E402
from computer_use_mcp.backend.linux import ocr as ocr_mod  # noqa: E402
from computer_use_mcp.backend.linux.inject import XdotoolInjector  # noqa: E402
from computer_use_mcp.utils.errors import ComputerUseError, InjectionError  # noqa: E402


class _Proc:
    """
    子进程返回值的替身。

    ⚠️ xclip 那几条调用走的是 `capture_output=True` **不带** `text=True`，所以
    `stdout` 是 **bytes**（`_clipboard_targets` 会直接 `.decode()`）。桩要是给 str，
    就会把「解析失败」伪装成「剪贴板是空的」，让 M-30① 的用例**假绿**。
    """

    def __init__(self, stdout: bytes | str = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


def _inj() -> XdotoolInjector:
    return XdotoolInjector.__new__(XdotoolInjector)


# ==================== M-31：xclip 写入不得继承 fd 1/2 ====================

def test_clipboard_writes_are_redirected_to_devnull(monkeypatch):
    """
    M-31：两处 xclip **写入**必须 `stdout/stderr=DEVNULL`。

    这是 stdio MCP server，xclip 写入模式会 fork 一个后台守护进程长期持有 selection，
    它继承本进程的 fd 1/2 —— 子进程往 stdout 写一个字节就污染 JSON-RPC 流。
    判据还要挡住「改回 capture_output=True」那条看似更规整的写法：管道的写端会被那个
    fork 出来的守护进程一直持着，`communicate()` 要等所有写端关闭才返回 → 每次中文输入
    白等满超时（实测 5s）。
    """
    seen: list[dict] = []

    def fake_run(cmd, **kw):
        seen.append({"cmd": cmd, "kw": kw})
        if "-o" in cmd:
            return _Proc(stdout=b"old-text")
        return _Proc()

    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/xclip")
    monkeypatch.setattr(inject_mod.subprocess, "run", fake_run)
    inj = _inj()
    inj.press_key = lambda c: True

    assert inj._type_via_clipboard("中文") is True

    writes = [s for s in seen if "-o" not in s["cmd"]]
    assert len(writes) == 2, f"应有「写入新文本 + 还原」两次写入，实际 {len(writes)}"
    for s in writes:
        assert s["kw"].get("stdout") is subprocess.DEVNULL, f"stdout 未重定向：{s['cmd']}"
        assert s["kw"].get("stderr") is subprocess.DEVNULL, f"stderr 未重定向：{s['cmd']}"
        assert "capture_output" not in s["kw"], "不得用 capture_output（会被 fork 出的守护进程吊住）"


# ==================== M-30：剪贴板还原的健壮性 ====================

def test_clipboard_is_restored_even_when_paste_raises(monkeypatch):
    """
    M-30②：粘贴那一步抛异常时，**仍然**要把原剪贴板写回去。

    历史实现把还原语句放在 `press_key("ctrl+v")` 之后且不在 finally 里 —— 粘贴一抛，
    剪贴板就停在 agent 文本上，而 GNOME 的剪贴板管理器会把这段内容**永久记入历史**
    （输入口令类内容就是持续残留的泄密面）。
    """
    seen: list[dict] = []

    def fake_run(cmd, **kw):
        seen.append({"cmd": cmd, "kw": kw})
        return _Proc(stdout=b"old-text") if "-o" in cmd else _Proc()

    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/xclip")
    monkeypatch.setattr(inject_mod.subprocess, "run", fake_run)
    inj = _inj()

    def boom(combo):
        raise InjectionError("ctrl+v 发送失败")

    inj.press_key = boom

    with pytest.raises(InjectionError):
        inj._type_via_clipboard("中文")

    restores = [s for s in seen if "-o" not in s["cmd"] and s["kw"].get("input") == b"old-text"]
    assert restores, f"粘贴失败后必须还原原剪贴板，实际的调用序列：{[s['cmd'] for s in seen]}"


def test_non_text_clipboard_is_reported_as_unrestorable(monkeypatch):
    """
    M-30①：原剪贴板是非文本（图片等）时，`xclip -o` 取不回字节 → 还原不了。
    这一份**无法还原**，必须留下告警，而不是静默把用户的图片剪贴板换掉。
    """
    warns: list[str] = []
    monkeypatch.setattr(inject_mod.log, "warning",
                        lambda msg, *a, **k: warns.append(msg % a if a else msg))
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/xclip")

    def fake_run(cmd, **kw):
        if "-t" in cmd and "TARGETS" in cmd:
            return _Proc(stdout=b"TARGETS\nimage/png\n")   # 有内容，但不是文本
        if "-o" in cmd:
            return _Proc(returncode=1)                    # 非文本 → 取不回
        return _Proc()

    monkeypatch.setattr(inject_mod.subprocess, "run", fake_run)
    inj = _inj()
    inj.press_key = lambda c: True
    inj._type_via_clipboard("中文")

    assert any("无法还原" in w for w in warns), f"应告警「非文本剪贴板无法还原」：{warns}"


def test_text_clipboard_is_restored_without_warning(monkeypatch):
    """M-30 反向：正常文本剪贴板必须**安静地**还原（不能对每次中文输入都刷告警）。"""
    warns: list[str] = []
    monkeypatch.setattr(inject_mod.log, "warning",
                        lambda msg, *a, **k: warns.append(msg % a if a else msg))
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/xclip")
    monkeypatch.setattr(inject_mod.subprocess, "run",
                        lambda cmd, **kw: _Proc(stdout=b"old-text") if "-o" in cmd else _Proc())
    inj = _inj()
    inj.press_key = lambda c: True
    inj._type_via_clipboard("中文")
    assert warns == [], f"正常路径不该告警：{warns}"


# ==================== 终端里的粘贴键：ctrl+v 不是粘贴 ====================
#
# 2026-09-24 实测：终端仿真器（VTE）里 `Ctrl+V` **不是粘贴**，它的粘贴键是
# `Ctrl+Shift+V`；`Ctrl+V` 会被原样写进 pty（0x16），bash/readline 当作 lnext
# （引用下一个字符）。后果是**静默失败**——type_text 报成功、屏幕上一个字都没进来，
# 且紧随其后的那个字符被吃掉。实测：37 字符的命令走剪贴板路径后终端里只剩一个 `^V`；
# 同一串 ctrl+v 打在 GTK 输入框（zenity）里则完整粘贴成功。


def _run_paste_with_class(monkeypatch, cls, *, raise_in_probe=False):
    """跑一次剪贴板输入，返回实际按下的组合键列表（WM_CLASS 由参数给定）。"""
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/xclip")
    monkeypatch.setattr(inject_mod.subprocess, "run",
                        lambda cmd, **kw: _Proc(stdout=b"old-text") if "-o" in cmd else _Proc())
    inj = _inj()
    keys: list[str] = []
    inj.press_key = lambda c: (keys.append(c), True)[1]
    if raise_in_probe:
        def _boom():
            raise RuntimeError("xdotool 不可用")
        inj.active_window_class = _boom
    else:
        inj.active_window_class = lambda: cls
    inj._type_via_clipboard("中文")
    return keys


def test_paste_uses_ctrl_shift_v_in_terminal(monkeypatch):
    """判据是**实际按下的组合键**，不是"有没有报成功"——旧实现恒发 ctrl+v，同样报成功。"""
    assert _run_paste_with_class(monkeypatch, "Gnome-terminal") == ["ctrl+shift+v"]
    assert _run_paste_with_class(monkeypatch, "XTerm") == ["ctrl+shift+v"]
    assert _run_paste_with_class(monkeypatch, "Alacritty") == ["ctrl+shift+v"]


def test_paste_stays_ctrl_v_in_normal_widgets(monkeypatch):
    """反向：普通窗口必须**继续**用 ctrl+v —— 改错了会让所有 GUI 应用的输入全废。"""
    assert _run_paste_with_class(monkeypatch, "Zenity") == ["ctrl+v"]
    assert _run_paste_with_class(monkeypatch, None) == ["ctrl+v"]
    # 空串（xdotool 读不到 class 时的归一结果）也不能被当成终端
    assert _run_paste_with_class(monkeypatch, "") == ["ctrl+v"]


def test_paste_probe_failure_falls_back_instead_of_breaking_input(monkeypatch):
    """
    探测本身**绝不允许**把输入弄坏：活动窗口读不到（没有 WM、无活动窗、测试替身
    没有 `_bin`）时必须安静退回 ctrl+v。这是个"帮个忙"的优化，不是新依赖。
    """
    assert _run_paste_with_class(monkeypatch, None, raise_in_probe=True) == ["ctrl+v"]


def test_paste_key_env_override_wins(monkeypatch):
    """逃生口：表里没有的终端 / 判错了，可以用 CC_CU_PASTE_KEY 直接指定。"""
    monkeypatch.setenv("CC_CU_PASTE_KEY", "ctrl+v")
    assert _run_paste_with_class(monkeypatch, "Gnome-terminal") == ["ctrl+v"]
    monkeypatch.setenv("CC_CU_PASTE_KEY", "ctrl+shift+v")
    assert _run_paste_with_class(monkeypatch, "Zenity") == ["ctrl+shift+v"]


def test_active_window_class_matches_wmctrl_by_numeric_id(monkeypatch):
    """
    判据要同时挡住两个静默失效：
      ① 走 xdotool（本机 3.20160805.1 **没有** getwindowclassname，恒返回空）；
      ② 拿十进制 id 去和 wmctrl 的 `0x…` 字符串比（永远不相等，也不报错）。
    """
    inj = _inj()
    inj.active_window_id = lambda: "12582918"          # xdotool 给的是十进制
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/wmctrl")
    monkeypatch.setattr(inject_mod.subprocess, "run", lambda cmd, **kw: _Proc(
        stdout="0x00c00006  0  1234  gnome-terminal-server.Gnome-terminal  host  liufei@host: ~\n"
               "0x00c00007  0  5678  other.Other  host  别的窗口\n"))

    assert inj.active_window_class() == "gnome-terminal"

    # 活动窗口不在清单里 / 没有活动窗口 / wmctrl 缺失 → None（调用方退回 ctrl+v）
    inj.active_window_id = lambda: "999999"
    assert inj.active_window_class() is None
    inj.active_window_id = lambda: None
    assert inj.active_window_class() is None
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: None)
    inj.active_window_id = lambda: "12582918"
    assert inj.active_window_class() is None


# ==================== M-32：标题当正则用 ====================

def test_window_search_escapes_title_regex(monkeypatch):
    """
    M-32：`search --name` 的取值是**位置参数正则**，窗口标题外部可控。

    标题含 `(` `|` `+` 等元字符时不转义会匹配失败 → 退化成按 pid 搜 → 同 pid 多窗口时
    可能锁到**另一扇窗**的几何，校准坐标整体错位（也可能被畸形标题构造成慢正则）。
    """
    seen: list[list[str]] = []
    inj = _inj()
    inj._run = lambda args, **kw: (seen.append(args), _Proc(stdout=""))[1]
    inj.window_geometry = lambda wid: None

    inj.window_screen_pos_by_pid_match(42, "App (v1.2|beta)")

    name_args = [a for a in seen if "--name" in a]
    assert name_args, "应有一次按标题的 search"
    pat = name_args[0][name_args[0].index("--name") + 1]

    # 判据做成**行为**的，而不是「字符串里有没有反斜杠」（后者对 re.escape 的版本差异
    # 很脆：不同 Python 版本转义的字符集并不相同）：
    #   转义后 → 必须能匹配标题**本身**；
    #   不转义 → 匹配不到它自己（`(v1.2|beta)` 被当成了「分组 + 或」，而不是字面量）。
    title = "App (v1.2|beta)"
    assert re.fullmatch(pat, title), f"转义后必须能匹配标题本身：{pat!r}"
    assert not re.fullmatch(title, title), \
        "反向证据：未转义的标题当正则用时匹配不到它自己 —— 这正是 M-32 描述的失配"


# ==================== M-33：位置参数以 - 开头 ====================

def test_type_text_terminates_options_before_text(monkeypatch):
    """
    M-33：文本以 `-` 开头时必须用 `--` 终止选项解析（实测 `xdotool type -1`
    报 unrecognized option rc=1；加 `--` 后 rc=0）。
    `--` 的位置有讲究：必须在**所有选项之后**，放最前面会把 --clearmodifiers 一起吃掉。
    """
    seen: list[list[str]] = []
    inj = _inj()
    inj._run = lambda args, **kw: (seen.append(args), _Proc())[1]

    inj.type_text("-1")

    assert seen == [["type", "--clearmodifiers", "--delay", "12", "--", "-1"]], seen


# ==================== M-34：超时不得重试（双击风险） ====================

def test_click_at_does_not_retry_on_timeout(monkeypatch):
    """
    M-34：串命令**超时**时不得退化为「只点击再试一次」。

    串命令是逐条执行的，超时前 click **可能已经发出去了**；再补一次就是**双击**，
    对「删除/确认」这类按钮是重复触发。概率低，但后果不对称。
    """
    calls: list[list[str]] = []

    def boom(args, **kw):
        calls.append(args)
        err = InjectionError("xdotool 超时")
        err.__cause__ = subprocess.TimeoutExpired(cmd="xdotool", timeout=10)
        raise err

    inj = _inj()
    inj._run = boom

    with pytest.raises(InjectionError):
        inj.click_at(10, 20, focus_wid="999")
    assert len(calls) == 1, f"超时不该重试，实际调用 {len(calls)} 次：{calls}"


def test_click_at_still_retries_on_nonzero_exit(monkeypatch):
    """M-34 反向：`rc≠0`（没执行到点击）**仍然**要重试——那是这条降级链原本的价值。"""
    calls: list[list[str]] = []

    def boom(args, **kw):
        calls.append(args)
        raise InjectionError("xdotool windowactivate 失败(rc=1)")

    inj = _inj()
    inj._run = boom

    with pytest.raises(InjectionError):
        inj.click_at(10, 20, focus_wid="999")
    assert len(calls) == 2 and calls[1][0] == "mousemove", calls


# ==================== M-35：死代码 ====================

def test_dead_injector_methods_are_gone():
    """
    M-35：三个无调用方的方法已删除。

    - `window_id_at`：依赖的 `xdotool locate` 命令**并不存在**（本身就是坏的）；
    - `mouse_position`：注释写「自检用」，而 selftest 从来不用它；
    - `focus_window`：生产代码零调用，实路上生效的是 `click_at` 内联的同名链路
      ——两份实现会各自漂移。
    """
    inj = XdotoolInjector()
    for name in ("window_id_at", "mouse_position", "focus_window"):
        assert not hasattr(inj, name), f"{name} 应已删除（M-35）"


# ==================== M-36：source 要反映数据实际来源 ====================

def test_screen_layout_source_reflects_actual_channel(monkeypatch):
    """
    M-36：`xrandr` 存在但解析不出东西时，数据其实来自 xdotool 兜底，
    `source` 就**不能**标成 xrandr（否则排查多屏问题会指向错误的一层）。
    """
    inj = _inj()
    monkeypatch.setattr(inject_mod.shutil, "which", lambda n: "/usr/bin/xrandr")
    monkeypatch.setattr(inject_mod.subprocess, "run",
                        lambda *a, **k: _Proc(stdout="完全解析不出显示器的一段文本"))
    inj._parse_xrandr = lambda out: []          # xrandr 这次没给出任何显示器
    inj.screen_size = lambda: (1920, 1080)

    layout = inj.screen_layout()

    assert layout["source"] == "xdotool", layout
    assert layout["monitors"] and layout["monitors"][0]["w"] == 1920


# ==================== M-37：OCR 放大要用 LANCZOS ====================

def test_ocr_upscale_uses_lanczos():
    """
    M-37：不指定重采样时 Pillow 默认 BICUBIC，2x **放大**小字号文字会明显发糊——
    而糊掉的笔画正是 tesseract 最容易认错的东西。
    """
    from PIL import Image

    recorded: dict = {}
    orig = Image.Image.resize

    def spy(self, size, resample=None, **kw):
        recorded["resample"] = resample
        return orig(self, size, resample, **kw)

    Image.Image.resize = spy
    try:
        out = ocr_mod._upscale_image(Image.new("RGB", (10, 6)))
    finally:
        Image.Image.resize = orig

    assert out.size == (10 * ocr_mod._UPSCALE, 6 * ocr_mod._UPSCALE)
    assert recorded["resample"] == Image.LANCZOS, f"实际重采样={recorded['resample']}"


# ==================== M-39：region 越界必须裁剪 ====================

def test_clamp_region_trims_to_screen_and_moves_origin():
    """
    M-39：越界矩形必须与屏边界求交，且**原点取裁剪后的左上角**。

    `describe_point(with_text=True)` 的落点文字框是「落点周围半宽 160 / 半高 32」，
    在屏幕边缘必然越界（x=50 → left=-110）→ mss 抛 ScreenShotError → 被 `_text_near`
    吞掉 → 屏幕边缘的按钮**永远拿不到落点文字**，且失败原因被静默。
    """
    virtual = {"left": 0, "top": 0, "width": 1600, "height": 1000}

    # describe_point 的探测框以落点为中心算，落点贴左边缘时 left 就是负的
    mon = grab_mod._clamp_region((-110, 10, 320, 64), virtual)
    assert mon == {"left": 0, "top": 10, "width": 210, "height": 64}, mon


def test_clamp_region_keeps_origin_for_negative_virtual_origin():
    """多屏并集原点为负时，裁剪后的原点也必须跟着走（否则坐标整体偏一个屏宽）。"""
    virtual = {"left": -1920, "top": 0, "width": 3840, "height": 1080}

    mon = grab_mod._clamp_region((-1920 - 100, 5, 300, 50), virtual)
    assert mon == {"left": -1920, "top": 5, "width": 200, "height": 50}, mon


def test_clamp_region_rejects_fully_offscreen():
    virtual = {"left": 0, "top": 0, "width": 1600, "height": 1000}
    with pytest.raises(ComputerUseError, match="完全落在屏幕之外"):
        grab_mod._clamp_region((5000, 5000, 100, 100), virtual)


def test_grab_hands_the_clamped_rect_to_mss(monkeypatch):
    """
    M-39 的**集成**判据：交给 mss 的矩形必须是裁剪过的那个。

    只测 `_clamp_region` 这个纯函数是不够的 —— 它写对了但 `_grab_raw` 不用它，
    上面两条用例照样全绿（这正是「判据松的测试等于没有测试」）。
    """
    _install_fake_mss(monkeypatch, takes_display=True)
    monkeypatch.setattr(grab_mod, "_MSS_TAKES_DISPLAY", True)
    monkeypatch.setattr(grab_mod.display, "effective_display", lambda: ":77")

    handed: list[dict] = []
    orig_grab = _FakeMSS.grab

    def spy(self, mon):
        handed.append(dict(mon))
        return orig_grab(self, mon)

    monkeypatch.setattr(_FakeMSS, "grab", spy)
    grab_mod.grab_rgb((-30, 5, 60, 20))     # 左边越界 30px（屏幕是 0..100）

    assert handed == [{"left": 0, "top": 5, "width": 30, "height": 20}], handed


# ==================== M-38：mss 新版本不吃全局 env ====================

class _FakeMSS:
    """最小 mss 替身：只提供 monitors 与 grab。"""

    def __init__(self, display=None) -> None:
        if display == "__TYPEERROR__":
            raise TypeError("unexpected keyword argument 'display'")
        self.monitors = [{"left": 0, "top": 0, "width": 100, "height": 80}]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def grab(self, mon):
        return types.SimpleNamespace(size=(1, 1), bgra=b"\x00\x00\x00\xff")


def _install_fake_mss(monkeypatch, takes_display: bool) -> None:
    mod = types.ModuleType("mss")

    def _mss(display=None):
        if display is not None and not takes_display:
            raise TypeError("unexpected keyword argument 'display'")
        return _FakeMSS(display)

    mod.mss = _mss
    monkeypatch.setitem(sys.modules, "mss", mod)


def test_grab_does_not_touch_global_display_when_mss_supports_it(monkeypatch):
    """
    M-38：mss 支持 `display=` 时，抓屏**不得**改进程级 `os.environ["DISPLAY"]`。

    全局 env 有别的读者（`display.host_display()` → 礼让检查 `user_inside_sandbox`），
    而 GRAB_LOCK 挡不住它们 —— 一次锁内的 env 交换就足以让并发的礼让检查去沙箱屏上找
    Xephyr 窗口、查不到、于是**静默跳过礼让**。
    """
    _install_fake_mss(monkeypatch, takes_display=True)
    monkeypatch.setattr(grab_mod, "_MSS_TAKES_DISPLAY", None)
    monkeypatch.setattr(grab_mod.display, "effective_display", lambda: ":77")
    monkeypatch.setenv("DISPLAY", ":1")

    # ⚠️ 判据必须是「抓屏**期间** env 有没有被动过」，不能只看**终态**：
    # 走「换 env → 抓 → 还原」那条路时终态同样是 ":1"，只看终态的话这条用例**拦不住
    # 它声称要拦的那个 bug**（实测：把首选路径禁掉、一律走 env 交换，它照样全绿）。
    seen: dict = {}
    real_grab = grab_mod._grab_raw

    def spy(disp, region, use_kwarg):
        seen["use_kwarg"] = use_kwarg
        seen["env_during"] = os.environ.get("DISPLAY")
        return real_grab(disp, region, use_kwarg)

    monkeypatch.setattr(grab_mod, "_grab_raw", spy)
    grab_mod.grab_rgb()

    assert grab_mod._MSS_TAKES_DISPLAY is True
    assert seen["use_kwarg"] is True, "支持 display= 时必须走无副作用那条路"
    assert seen["env_during"] == ":1", "抓屏期间不得改动全局 DISPLAY"
    assert os.environ["DISPLAY"] == ":1"
    assert grab_mod.GRAB_LOCK.locked() is False, "无全局副作用就不该再持锁"


def test_grab_falls_back_to_env_swap_and_restores_it(monkeypatch):
    """M-38 反向：旧版 mss（不认 display=）仍要能用，且 env 必须**原样还原**。"""
    _install_fake_mss(monkeypatch, takes_display=False)
    monkeypatch.setattr(grab_mod, "_MSS_TAKES_DISPLAY", None)
    monkeypatch.setattr(grab_mod.display, "effective_display", lambda: ":77")
    monkeypatch.setenv("DISPLAY", ":1")

    seen: dict = {}
    real_grab = grab_mod._grab_raw

    def spy(disp, region, use_kwarg):
        seen["disp"] = disp
        seen["use_kwarg"] = use_kwarg
        seen["env_during"] = os.environ.get("DISPLAY")
        return real_grab(disp, region, use_kwarg)

    monkeypatch.setattr(grab_mod, "_grab_raw", spy)
    grab_mod.grab_rgb()

    assert grab_mod._MSS_TAKES_DISPLAY is False
    assert seen["use_kwarg"] is False and seen["env_during"] == ":77", seen
    assert os.environ["DISPLAY"] == ":1", "env 必须原样还原"