# -*- coding: utf-8 -*-
"""「不给范围时截哪儿」的守卫（`_capture_target` + `DEFAULT_CAPTURE_SCOPE`）。

═══ 这一组守的是什么 ═══

用户的原话：「截图时能单独截取某个软件的界面，而不是截取全屏」。

Windows 上这件事有个**会静默出错的将来**：agent 不再抢前台之后，「活动窗口」
就恒等于**用户正在用的那扇窗**。此时若"不给参数就截活动窗口"，用户切回浏览器、
agent 调一次 screenshot，**浏览器画面就进了模型上下文，而且不报任何错** ——
静默 + 泄露用户内容，本项目最不能容忍的失败形态。

所以「目标窗口」按 显式 window_id > app > 本会话最近操作过的窗口 > 前台窗口
四档推导，**且最后那一档必须在说明里挑明**。本文件把这条顺序与那句挑明钉住。

⚠️ 判据纪律（同 `test_windows_driver_guards.py`）：每条都要能被证伪，
且**正控不许空转** —— 只断言"某分支存在"而没有反向用例，等于没测。
"""

from __future__ import annotations

import pytest

from computer_use_mcp.backend.base import Backend, Rect
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import ComputerUseError

from _helpers import _StubBackend  # noqa: E402


def _with_captured(b):  # noqa: ANN001, ANN202
    """给 `_StubBackend` 补一个 `captured` 列表（它原本没有，而断言要读）。

    刻意**不去改 `_helpers.py`**：那是全仓共享的桩，为一个新用例改它的形状，
    会让别的用例在"多了个从没人写的属性"这件事上失去敏感度。
    """
    if not hasattr(b, "captured"):
        b.captured = []
    return b


class _CaptureBackend(_StubBackend):
    """在 `_StubBackend` 之上补出"按窗口渲染"这条通道，用于平台中立的行为断言。"""

    DEFAULT_CAPTURE_SCOPE = "window"

    def __init__(self) -> None:  # noqa: D107
        super().__init__()
        self.captured: list[str] = []

    # 只要**覆写**了 `capture_window`，`_window_capture_supported()` 就为真 ——
    # 这正是产品里那个判据的形状（取类方法是否被覆写，而不是 hasattr 哨兵）。
    def capture_window(self, window_id):  # noqa: ANN001, ANN201
        self.captured.append(str(window_id))
        raise ComputerUseError(f"（桩）按窗口渲染 {window_id}")

    def window_rect_of(self, window_id):  # noqa: ANN001, ANN201
        return Rect(x=10, y=20, w=300, h=200)

    def window_for_app(self, app):  # noqa: ANN001, ANN201
        return "0xabc" if app == "notepad" else None

    def active_window_id(self):  # noqa: ANN001, ANN201
        return "0xffff"

    def active_window_rect(self):  # noqa: ANN001, ANN201
        return Rect(x=0, y=0, w=100, h=100)


def _coord(backend=None):  # noqa: ANN001, ANN202
    return Coordinator(backend or _CaptureBackend())


# ── ① 平台默认范围是一个**平台差异**，不是一个随便定的默认值 ──────────────

def test_default_capture_scope_is_a_platform_difference():
    """Windows 默认按窗口、基线默认按屏 —— 两个值都要断言（只断一边等于没断）。"""
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    assert WindowsBackend.DEFAULT_CAPTURE_SCOPE == "window"
    # 正控：基线是 screen。若两边一样，这条判据就量不到"平台差异"这件事。
    assert Backend.DEFAULT_CAPTURE_SCOPE == "screen"


def test_auto_scope_follows_the_platform_default():
    """`scope="auto"` 必须**跟随类属性**，而不是硬编码成某一个值。

    判据取「改类属性 → 结果跟着变」：只看 `_capture_target` 的返回值，
    写死成 "screen" 的实现也能过前一条，过不了这一条。
    """
    b = _CaptureBackend()
    c = _coord(b)

    b.DEFAULT_CAPTURE_SCOPE = "screen"
    mode, wid, note = c._capture_target("auto")
    assert mode == "screen" and wid is None and note == ""

    b.DEFAULT_CAPTURE_SCOPE = "window"
    c._remember_target_window("0x1234")
    mode, wid, _ = c._capture_target("auto")
    assert (mode, wid) == ("window", "0x1234")


def test_explicit_screen_scope_wins_over_the_platform_default():
    """显式 `scope="screen"` 必须压过平台默认（用户明确要整屏时照办）。"""
    c = _coord()
    c._remember_target_window("0x1234")
    mode, wid, _ = c._capture_target("screen")
    assert (mode, wid) == ("screen", None)


# ── ② 解析顺序：显式 > app > 最近操作过 > 前台 ──────────────────────────

def test_explicit_window_id_beats_everything_else():
    c = _coord()
    c._remember_target_window("0x1234")
    mode, wid, note = c._capture_target("window", window_id="0xbeef", app="notepad")
    assert (mode, wid) == ("window", "0xbeef")
    assert "0xbeef" in note


def test_app_resolves_through_the_backend():
    c = _coord()
    c._remember_target_window("0x1234")
    mode, wid, note = c._capture_target("window", app="notepad")
    assert (mode, wid) == ("window", "0xabc")
    assert "notepad" in note


def test_unknown_app_raises_instead_of_silently_using_another_window():
    """找不到该应用时**必须报错**，不能悄悄换一扇窗。

    静默换窗的后果：模型要截 A、拿到 B 的内容，而它以为自己看的就是 A。
    """
    c = _coord()
    c._remember_target_window("0x1234")          # 有可用的兜底，仍然不许用
    with pytest.raises(ComputerUseError) as ei:
        c._capture_target("window", app="nosuchapp")
    assert "nosuchapp" in str(ei.value)


def test_recently_touched_window_beats_the_foreground():
    """★ 这条是本文件的核心：**最近操作过的窗口**优先于「现在谁在前台」。

    agent 不抢前台之后，前台属于用户。先取前台就等于"截用户正在用的那扇窗"。
    """
    c = _coord()
    c._remember_target_window("0x1234")
    mode, wid, _ = c._capture_target("window")
    assert (mode, wid) == ("window", "0x1234")


def test_foreground_fallback_is_loud_not_silent():
    """★ 兜底到前台窗口时，说明里**必须挑明**这一点。

    这是可能截到用户自己窗口的**唯一**分支。静默走它 = 悄悄把用户的浏览器画面
    读进模型上下文，而模型只会以为"这就是当前界面"。
    """
    c = _coord()
    mode, wid, note = c._capture_target("window")
    assert mode == "window" and wid == "0xffff"      # 用了前台窗口的 id
    assert "前台" in note and "用户" in note, (
        f"兜底到前台窗口时必须明确告知（可能正是用户在用的窗口），实际说明={note!r}")


def test_remember_only_overwrites_with_a_real_id():
    """`_remember_target_window(None)` 不该把已有的目标擦掉 —— 那会让下一次截图
    悄悄退回"当前前台"，即上面那条最危险的路径。"""
    c = _coord()
    c._remember_target_window("0x1234")
    c._remember_target_window(None)
    assert c._target_window_id == "0x1234"


# ── ③ 抓图通道：按窗口优先，且**失败与"平台没这能力"必须分得开** ──────────

def test_window_capture_supported_is_decided_by_override_not_hasattr():
    """`_window_capture_supported()` 判的是**类方法有没有被覆写**。

    为什么不能用 `hasattr`：基类有默认实现，`hasattr` 恒为真 —— 那会让"本平台
    没有这条通道"与"有通道"看起来一模一样（I-10 明文否定的哨兵写法）。
    """
    assert _coord(_CaptureBackend())._window_capture_supported() is True
    assert _coord(_StubBackend())._window_capture_supported() is False


def test_capture_failure_propagates_instead_of_falling_back_to_screen():
    """★ 按窗口渲染**失败**时不许静默退回裁屏 —— 那可能把用户其它窗口截进来。

    判据：桩的 `capture_window` 抛错，`screenshot_image` 必须把错抛出来，
    且**没有**去调 `screenshot()`（裁屏那条路）。
    """
    b = _CaptureBackend()
    c = _coord(b)
    c._remember_target_window("0x1234")
    with pytest.raises(ComputerUseError):
        c.screenshot_image()
    assert b.captured == ["0x1234"], f"应当去截那扇窗，实际={b.captured}"
    # ⚠️ 刻意不写「或恒真」的兜底断言：本仓库明文否定空转的判据 —— 一条永远不会红的
    #    断言出现在测试里，比没有它更坏（它会让人以为这件事已经被守住了）。
    assert not [x for x in b.calls if x[0] == "screenshot"], (
        "按窗口渲染失败后**不该**退回裁屏（那会把用户其它窗口截进来）")


def test_platform_without_window_capture_never_touches_it():
    """反向正控：**没有**按窗口渲染能力的平台，一步都不许走那条路。

    判据取「`capture_window` 零调用 + 真的产出了 bytes」：前者证明没走新路，
    后者证明它确实从旧路拿到了图（只断言前者的话，一个"直接抛错"的实现也能过）。
    """
    b = _with_captured(_StubBackend())
    c = _coord(b)
    data, meta = c.screenshot_image()
    assert b.captured == [], "没有按窗口渲染能力的平台不该走进那条路"
    assert data == b"" and meta.get("format") == "jpeg", f"应走旧路，实际 {meta!r}"
    assert "capture" in meta, "meta 里必须标明这张图是怎么来的（模型据此判断可信度）"


def test_region_argument_short_circuits_target_resolution():
    """显式给了 region 就按 region 裁，**不去解析目标窗口**。"""
    b = _CaptureBackend()
    c = _coord(b)
    c._remember_target_window("0x1234")
    c.screenshot_image((1, 2, 3, 4))
    assert b.captured == [], "给了 region 就不该走按窗口渲染"
