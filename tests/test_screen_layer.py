"""
`backend/screen.py` 的契约守卫（可在 **Linux** 上跑）。

═══ 为什么单独一个文件 ═══

2026-09-28 把「抓屏与点击反馈」的平台中立部分从 `backend/linux/` 抽到
`backend/screen.py`，好让 Windows 复用。这次重构有两类**会在 Linux 上静默失效**的风险，
本文件就是为它们设的：

  ① **补丁打空**。`tests/` 里有 20+ 处 `monkeypatch.setattr(grab_mod, "<名字>", …)`。
     若实现搬走了、`linux/backend.py` 改调 `screen.<名字>`，那些 `setattr` 会**照样成功**
     （名字还在，因为重导出了），但生产代码不再读它 → 用例照绿而**零覆盖**。
     本项目记录过这一失效模式（拆 mixin 那次两条守卫静默变空集合）。

  ② **契约失配**。`describe_point` 的返回键名由 ABC 文档规定，而两个后端各发一套方言时
     消费方读不到键 —— **不报错、不掉测试**，只是那行证据凭空消失。

两条都是「工具报成功、实际什么都没给」的形状，故都要显式钉住。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"


# ==================== ① 补丁点契约：重导出必须是真的同一个对象 ====================

# `tests/` 里被 monkeypatch 打过的 grab 名字（改这里的判据前请先 grep 一遍）。
_PATCHED_ON_GRAB = (
    "grab_rgb", "_grab_raw", "_clamp_region", "draw_crosshair",
    "changed_fraction", "_MSS_TAKES_DISPLAY", "GRAB_LOCK", "display", "log",
)


def test_linux_grab_still_exposes_every_patched_name():
    """
    `backend/linux/grab.py` 必须仍然导出那些被测试打补丁的名字。

    名字没了 → `monkeypatch.setattr` 抛 AttributeError（响亮，还行）；
    ⚠️ 更隐蔽的是**名字还在、但它不再是被调用的那个对象** —— 见下一条。
    """
    from computer_use_mcp.backend.linux import grab

    missing = [n for n in _PATCHED_ON_GRAB if not hasattr(grab, n)]
    assert not missing, (
        f"backend/linux/grab.py 不再导出 {missing} —— "
        f"tests/ 里打在它们身上的 monkeypatch 会当场炸（或更糟：静默打空）。"
    )


@pytest.mark.parametrize(("grab_name", "screen_name"), [
    ("draw_crosshair", "draw_crosshair"),
    ("changed_fraction", "changed_fraction"),
    ("_clamp_region", "clamp_region"),          # 名字不同：历史私有名 vs 公开名
])
def test_reexports_are_the_same_object_as_screen(grab_name, screen_name):
    """
    ⚠️ **本文件的核心一条**：`grab.<名字>` 必须与 `screen.<名字>` 是**同一个函数对象**。

    这条同时排掉两种坏法：
      · 有人把实现抄了一份回 grab.py（两份会漂移）；
      · 有人写了 `def draw_crosshair(...): return screen.draw_crosshair(...)` 这种包一层
        —— 补丁打在包装上，`screen` 里真正被调用的那份毫发无损，**静默打空**。
    """
    from computer_use_mcp.backend import screen
    from computer_use_mcp.backend.linux import grab

    assert getattr(grab, grab_name) is getattr(screen, screen_name), (
        f"grab.{grab_name} 与 screen.{screen_name} 不是同一个对象 —— "
        f"要么抄了一份，要么包了一层。后者会让 monkeypatch.setattr(grab_mod, "
        f"{grab_name!r}, …) **静默打空**。"
    )


def test_linux_backend_still_calls_the_grab_names_not_screen():
    """
    ⚠️ **纪律守卫**：`backend/linux/backend.py` 里那几个平台中立函数必须**经 `grab.*` 调**，
    不许改写成直调 `screen.*`。

    为什么：`tests/test_ring_and_preview.py` 有 2 处
    `monkeypatch.setattr(grab_mod, "draw_crosshair", …)`。直调 `screen.draw_crosshair`
    之后补丁**照样 setattr 成功**、用例里的断言也**照样通过**（因为它的断言不看补丁有没有
    生效），于是守卫全绿而覆盖归零。这正是本项目最怕的那类失效。

    判据是 AST：`linux/backend.py` 里对 `screen.<中立函数>` 的调用次数必须为 0。
    （**被禁的只有"有补丁点的那三个"**；`encode_image` / `build_click_preview` 是这次新抽
    出来的、没有任何补丁点，Linux 就该直调它们 —— 把它们也禁掉等于禁止重构本身。）
    """
    banned = {"draw_crosshair", "changed_fraction", "clamp_region"}
    path = _SRC / "backend" / "linux" / "backend.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))

    offenders: list[str] = []
    screen_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            if fn.value.id == "screen":
                screen_calls += 1
                if fn.attr in banned:
                    offenders.append(f"{path.name}:{node.lineno} screen.{fn.attr}(…)")

    # 正控：`screen` 必须真的被用到（否则本规则在"整个模块没提 screen"时也全绿）
    assert screen_calls > 0, (
        "linux/backend.py 里一处 screen.* 都没有 —— 本规则的正控失败，它现在什么都没量。"
    )
    assert not offenders, (
        "Linux 生产代码直调了中立的像素函数，绕过了 grab 的补丁点：\n  "
        + "\n  ".join(offenders)
        + "\n改成 `grab.<名字>`（属性查找），理由见本用例 docstring。"
    )


# ==================== ② describe_point 的键名契约 ====================

# ABC 的 docstring 里写明的那套键（backend/base.py 的 describe_point）。
_ABC_POINT_KEYS = {"x", "y", "window_id", "window_title", "window_rect", "text", "conf"}
# 我们**额外**允许后端自带的键（Linux 没有对应物，不冲突）。
_EXTRA_POINT_KEYS = {"control", "control_type"}


def test_backends_agree_on_describe_point_keys():
    """
    ⚠️ **这条守卫本来就能抓住 2026-09-28 那个真 bug。**

    Windows 的 `describe_point` 初版返回自造的方言
    （`{"point","window","class","hwnd","control",…}`），而消费方
    `core/coordinator/landing.py` 读的是 `ev.get("window_title")` / `ev.get("window_rect")`
    ——**恒为 None**，于是「落点：窗口「…」 WxH」整行**静默消失**，不报错、不掉测试。

    判据是**两个后端实现体的返回键集合**（AST 扫描 `out[...] = ` 与字典字面量里的键），
    不是跑一遍 —— 因为 Windows 侧在 Linux 上跑不起来。
    """
    results: dict[str, set[str]] = {}
    for name, path in (
        ("linux", _SRC / "backend" / "linux" / "backend.py"),
        ("windows", _SRC / "backend" / "windows" / "backend.py"),
    ):
        keys: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "describe_point"]:
            for node in ast.walk(fn):
                # out["key"] = …
                if (isinstance(node, ast.Assign) and node.targets
                        and isinstance(node.targets[0], ast.Subscript)
                        and isinstance(node.targets[0].slice, ast.Constant)):
                    keys.add(node.targets[0].slice.value)
                # out: dict[str, Any] = {"key": …}
                if isinstance(node, ast.Dict):
                    keys.update(k.value for k in node.keys
                                if isinstance(k, ast.Constant))
        results[name] = keys

    assert all(results.values()), (
        f"没能从某个后端里扫出任何键（扫描逻辑坏了，本规则在空转）：{results}"
    )

    for name, keys in results.items():
        stray = keys - _ABC_POINT_KEYS - _EXTRA_POINT_KEYS
        assert not stray, (
            f"{name} 的 describe_point 返回了 ABC 契约之外的键 {sorted(stray)}。\n"
            f"契约（backend/base.py 的 docstring）：{sorted(_ABC_POINT_KEYS)}\n"
            f"消费方按契约取键，多出来的键**没人读** —— 而少一个契约键会让那行证据静默消失。"
        )

    # ★ 关键：契约里"有内容才给"的那几个，至少得有一个后端真的产出
    for must in ("window_title", "window_rect"):
        assert any(must in ks for ks in results.values()), (
            f"两个后端都没产出 {must!r} —— 落点证据行会整行消失。"
        )


def test_linux_describe_point_covers_the_abc_contract():
    """Linux 侧是契约的**参考实现**，必须覆盖全部契约键（Windows 可以少 OCR 那两个）。"""
    path = _SRC / "backend" / "linux" / "backend.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    keys: set[str] = set()
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "describe_point"]:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Assign) and node.targets
                    and isinstance(node.targets[0], ast.Subscript)
                    and isinstance(node.targets[0].slice, ast.Constant)):
                keys.add(node.targets[0].slice.value)
            if isinstance(node, ast.Dict):
                keys.update(k.value for k in node.keys if isinstance(k, ast.Constant))
    missing = _ABC_POINT_KEYS - keys
    assert not missing, f"Linux 的 describe_point 少了契约键 {sorted(missing)}"


# ==================== ③ 中立层自身的行为（不需要真屏）====================

def _fake_mss(monkeypatch, *, left=0, top=0, width=800, height=600, seen=None):
    """装一个假 mss 模块，`seen` 会收到构造时的 kwargs（用来断言"没传 display="）。"""
    import sys
    import types

    from PIL import Image

    class _Sct:
        monitors = [{"left": left, "top": top, "width": width, "height": height}]

        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def grab(self, mon):  # noqa: ANN001, ANN201
            if seen is not None:
                seen["mon"] = dict(mon)

            class _Raw:
                size = (mon["width"], mon["height"])
                bgra = bytes(mon["width"] * mon["height"] * 4)

            return _Raw()

    def _ctor(**kw):
        if seen is not None:
            seen["ctor_kwargs"] = dict(kw)
        return _Sct()

    fake = types.ModuleType("mss")
    fake.mss = _ctor
    monkeypatch.setitem(sys.modules, "mss", fake)
    return Image


def test_grab_desktop_passes_no_display_kwarg(monkeypatch):
    """
    ★ **这条是 `grab_desktop` 区别于 Linux `grab_rgb` 的全部要害**：
    它必须调 `mss.mss()` **不带任何参数**。

    为什么值得单测：在 Linux 上不带 `display=` 会抓到 `$DISPLAY` 指向的那块屏 ——
    沙箱模式下正是**宿主真实桌面**。所以这个函数**只能**给没有 DISPLAY 概念的平台用
    （Windows）。测的正是「它确实不带那个参数」这个前提，
    而不是"它能抓到图"（后者在 Linux CI 上根本跑不了）。
    """
    seen: dict = {}
    _fake_mss(monkeypatch, seen=seen)
    from computer_use_mcp.backend import screen

    img, origin = screen.grab_desktop()
    assert seen.get("ctor_kwargs") == {}, (
        f"grab_desktop 给 mss 传了参数 {seen.get('ctor_kwargs')} —— "
        f"它必须不带 display=，否则在 Linux 上会抓到宿主桌面。"
    )
    assert origin == (0, 0)
    assert img.size == (800, 600)


def test_grab_desktop_reports_the_clamped_origin(monkeypatch):
    """
    越界的 region 会被 `clamp_region` 裁到屏幕内，**原点取裁剪后那个角**。

    判据是原点而不是图尺寸：写错原点的话，模型按 `屏幕坐标 = origin + 图上坐标` 换算
    出来的每个坐标都整体偏出去，而图本身看着完全正常。
    """
    seen: dict = {}
    _fake_mss(monkeypatch, seen=seen)
    from computer_use_mcp.backend import screen

    _img, origin = screen.grab_desktop((-30, 5, 60, 20))   # left=-30 越界
    assert origin == (0, 5), f"原点应取裁剪后的左上角，实际 {origin}"
    assert seen["mon"] == {"left": 0, "top": 5, "width": 30, "height": 20}


def test_encode_image_meta_contract():
    """`screenshot` 的 meta 契约：模型靠 `origin` + `scale` 换算屏幕坐标，缺一不可。"""
    from PIL import Image

    from computer_use_mcp.backend import screen

    img = Image.new("RGB", (800, 600), (10, 20, 30))
    data, meta = screen.encode_image(img, (100, 50), region=(100, 50, 800, 600))
    assert meta["origin"] == [100, 50]
    assert meta["region"] == [100, 50, 800, 600]
    assert (meta["width"], meta["height"]) == (800, 600)
    assert meta["bytes"] == len(data) and data
    # 没缩放时必须**没有** scale/resized —— 给了反而会让模型多乘一次
    assert "scale" not in meta and "resized" not in meta

    data2, meta2 = screen.encode_image(img, (0, 0), max_side=200)
    assert meta2["resized"] == [200, 150]
    assert meta2["scale"] == 4.0, f"scale = 原宽/新宽 = 800/200，实际 {meta2['scale']}"
    assert meta2["bytes"] == len(data2)


def test_changed_fraction_rejects_size_mismatch():
    """
    两张尺寸不同的图**不能**比 —— 抛错而不是返回 0。

    返回 0 会被读成「界面没变」，而真实情况是「屏幕布局变了，这两张根本没法比」。
    """
    from PIL import Image

    from computer_use_mcp.backend import screen
    from computer_use_mcp.utils.errors import ComputerUseError

    a = Image.new("RGB", (10, 10), (0, 0, 0))
    b = Image.new("RGB", (20, 10), (0, 0, 0))
    with pytest.raises(ComputerUseError):
        screen.changed_fraction(a, b)


def test_changed_fraction_counts_gray_delta_above_threshold():
    """判据是「灰度差 > threshold 的像素占比」，故小幅噪声不算变化。"""
    from PIL import Image

    from computer_use_mcp.backend import screen

    a = Image.new("RGB", (10, 10), (0, 0, 0))
    same = Image.new("RGB", (10, 10), (5, 5, 5))          # 差 5 < 阈值 20
    assert screen.changed_fraction(a, same) == 0.0
    half = a.copy()
    for x in range(10):
        for y in range(5):
            half.putpixel((x, y), (255, 255, 255))        # 半个图变白
    assert screen.changed_fraction(a, half) == 0.5


def test_build_click_preview_puts_crosshair_at_point_minus_origin():
    """
    ★ **准星像素 = 落点 − 裁剪后原点**。

    写死半宽的话，越靠屏幕边缘的按钮准星指得越离谱 —— 而这个功能存在的唯一理由
    就是「位置对不对」，指错就是主动误导。
    """
    from PIL import Image

    from computer_use_mcp.backend import screen

    img = Image.new("RGB", (480, 300), (200, 200, 200))
    # 原点被裁剪改过（-120 而非 -240），落点在图上因此不是正中心
    shot = screen.build_click_preview(img, (-120, -100), 100, 50)
    assert shot is not None
    assert shot.meta["crosshair"] == [220, 150], shot.meta["crosshair"]
    assert shot.meta["scale"] == 1
    assert shot.meta["origin"] == [-120, -100]
    # 原始图必须是**没画准星**的那张（后面还要给 OCR 与点前后对比用）
    assert shot.image is img


def test_build_click_preview_returns_none_when_point_outside():
    """落点落在裁剪图外 → None。宁可不发图，也不发一张指错的图。"""
    from PIL import Image

    from computer_use_mcp.backend import screen

    img = Image.new("RGB", (480, 300), (200, 200, 200))
    assert screen.build_click_preview(img, (5000, 5000), 10, 10) is None