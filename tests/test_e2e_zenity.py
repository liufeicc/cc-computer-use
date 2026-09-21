"""
端到端测试：zenity 对话框（复刻 demo/action_vs_coord.py 路径A）。

验证「元素级 do_action」全链路：读树 → 找按钮 → click(ref) 走元素级 → zenity 退出码 0。

隔离沙箱：默认（CC_CU_DISPLAY_MODE=isolated）本套 e2e 自启 Xephyr 虚拟屏，
zenity 起在沙箱内、注入只作用于沙箱——**不再触碰用户真实桌面**；
CC_CU_DISPLAY_MODE=real 时 start() 为 no-op，退回旧的活桌面行为。

仅在以下条件全部满足时运行，否则 skip：
  - 显式 opt-in：环境变量 CC_CU_E2E=1（端到端会起窗口 + AT-SPI 遍历 + 元素级点击，
    误跑可能拖垮 GNOME Shell / X 会话，故默认禁跑）
  - Linux + X11(DISPLAY 存在)
  - zenity / xdotool 已安装
  - AT-SPI 可用（无障碍开关已开）
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time

import pytest

from computer_use_mcp.backend.base import get_backend
from computer_use_mcp.core import display
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import LEVEL_ELEMENT
from computer_use_mcp.utils.refs import RefTable

# 「肯定」按钮的可能名字（中英文环境）
_AFFIRMATIVE = ("是", "Yes", "确定", "OK", "好")

_skip_reason = None
if platform.system().lower() != "linux":
    _skip_reason = "仅 Linux"
elif os.environ.get("CC_CU_E2E") != "1":
    # 显式 opt-in：本套端到端会操作活桌面（起 zenity + AT-SPI 遍历 + 元素级点击）。
    # 实测误跑（如 CI/日常全量 pytest 且环境带 DISPLAY）曾把 at-spi2-registryd /
    # GNOME Shell 拖垮、X 会话整体重启。故默认 skip，需 CC_CU_E2E=1 显式开启。
    _skip_reason = "未显式开启（需 CC_CU_E2E=1；端到端操作活桌面，默认禁跑）"
elif not os.environ.get("DISPLAY"):
    _skip_reason = "无 DISPLAY（需 X11 图形会话）"
elif shutil.which("zenity") is None:
    _skip_reason = "未安装 zenity"

pytestmark = pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "")


@pytest.fixture(scope="module", autouse=True)
def sandbox():
    """模块级：先就绪沙箱（isolated 自启 Xephyr；real 为 no-op）。进程退出时 atexit 回收。"""
    display.start()
    yield


@pytest.fixture
def coord():
    backend = get_backend()
    if not backend.is_available():
        pytest.skip(f"AT-SPI 不可用：{backend.reader.error}（请开启无障碍开关）")
    return Coordinator(backend, RefTable())


def _wait_app_on_tree(keyword: str, timeout: float = 10.0, poll: float = 0.25) -> bool:
    """
    轮询等待应用**真的出现在 a11y 树上**；出现返回 True，超时返回 False。

    为什么不能像历史实现那样 `time.sleep(2.5)` 了事（I-17，见 docs/REVIEW/review_0.1.0.md）：
    「应用已上树」是本套 e2e 的**真实前提**，而固定等待只是对它的**猜测**。实测，沙箱内
    zenity 上树耗时 1.60 / 2.28 / **2.54** 秒 —— 与 2.5 这个魔数**正好重叠**，于是同一份
    未改动的代码会在「140 passed」与「3 failed」之间随机翻转。更糟的是失败表现是「树是空的」，
    会把排查者引向「无障碍开关没开 / 这个应用没有树」这种完全错误的方向（本次实测就绕了远路）。

    这个前提本来就可以**问出来**（本项目自己就有 `wait_window` 的轮询写法），不该靠猜。

    探测用**独立 backend**：这里只问「私有总线上有没有这个应用」，不涉及任何元素引用，
    故不需要 Coordinator 那张 ref 表；也正因为不建 Coordinator，才不会顺带触发别的副作用。
    """
    backend = get_backend()
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        try:
            seen = [backend.reader.get_name(a) for a in backend.reader.iter_apps()]
        except Exception:  # noqa: BLE001 —— 探测失败按「还没上树」处理，超时统一报错
            seen = []
        if any(keyword.lower() in (n or "").lower() for n in seen):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _start_zenity(title: str) -> subprocess.Popen:
    # 用 -x 精确匹配进程名 zenity（不能用 -f：会匹配到本测试进程命令行里的 'zenity' 而自杀）
    subprocess.run(["pkill", "-9", "-x", "zenity"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    zen = subprocess.Popen(
        ["zenity", "--question", "--title", title, "--text", "MCP 端到端测试"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        # 起在目标 display：isolated=沙箱内，real=宿主。
        # ⚠️ 必须用 app_env() 而非 env_for()：zenity 是**应用**，要的是「沙箱私有
        #    AT-SPI 总线」这条信息；env_for() 是给 xdotool/i3 那类 X11 工具的通道，
        #    按设计已剥掉 AT_SPI_BUS_ADDRESS（见 display.env_for 的 docstring）。
        #    用 env_for() 起应用 = 应用静默落到**宿主**总线上，a11y 隔离失效。
        env=display.app_env(),
    )
    time.sleep(0.3)  # 给窗口 mapped 一点时间（上树的前提），真正的判据是下面的轮询
    if not _wait_app_on_tree("zenity"):
        raise AssertionError(
            "10 秒内 zenity 未出现在 a11y 树上 —— 这**不等于**「无障碍没开」，"
            "更可能是沙箱私有总线/registryd 起步慢或起不来；先看 display 日志里的"
            "「沙箱私有 AT-SPI 总线就绪」。"
        )
    return zen


def test_read_zenity_tree(coord):
    """能读到 zenity 应用树（非空）。"""
    title = "E2ETree"
    zen = _start_zenity(title)
    try:
        tree = coord.get_ui_tree(scope="app", app="zenity", max_nodes=80)
        assert tree and "push button" in tree, f"未读到 zenity 按钮树：\n{tree}"
    finally:
        if zen.poll() is None:
            zen.terminate()
        _wait_window_gone(title)


def test_click_zenity_affirmative_element_level(coord):
    """元素级 do_action 点「是」→ zenity 退出码 0（复刻 demo 路径A 成功）。"""
    title = "E2EClick"
    zen = _start_zenity(title)
    try:
        buttons, _notice = coord.find_elements(role="push button", app="zenity")
        assert buttons, "未找到 zenity 按钮"
        # 选肯定按钮：名字命中 _AFFIRMATIVE，否则取第一个
        target = next(
            (b for b in buttons if any(a in b.name for a in _AFFIRMATIVE)),
            buttons[0],
        )
        result = coord.click(ref=target.ref)
        # 等待 zenity 退出
        try:
            code = zen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            zen.terminate()
            pytest.fail(f"点击后 zenity 未退出。result={result.to_text()}")
        assert result.ok, f"click 未成功：{result.to_text()}"
        assert code == 0, f"退出码={code}（期望0=点了肯定按钮）。result={result.to_text()}"
        # 核心主张：应走元素级（零坐标）
        assert result.level == LEVEL_ELEMENT, f"未走元素级降级：{result.to_text()}"
    finally:
        if zen.poll() is None:
            zen.terminate()
        _wait_window_gone(title)


def _start_zenity_entry(title: str) -> subprocess.Popen:
    """
    起 zenity --entry（键盘注入用例专用）。

    为什么用 --entry 而不是 --question：敲进去的文本会从 **stdout 回显**，于是「按键真的
    落到了那个输入框」有了确定性的判据。原用例（`test_type_and_key_smoke`）名与 docstring
    都写着「键盘注入冒烟」、函数体里却一次 press_key 都没有，只能断言「没抛异常」—— 那
    正是 I-14 记的问题：看名字的人以为键盘注入有 e2e 覆盖，其实完全没有。

    必须 stdout=PIPE + text=True：回显是文本，且要在按下回车后读出来。
    """
    # 用 -x 精确匹配进程名 zenity（不能用 -f：会匹配到本测试进程命令行里的 'zenity' 而自杀）
    subprocess.run(["pkill", "-9", "-x", "zenity"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    zen = subprocess.Popen(
        ["zenity", "--entry", "--title", title, "--text", "敲入文本后按回车"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        # 起在目标 display：isolated=沙箱内，real=宿主。理由同 _start_zenity（必须 app_env，
        # 否则应用静默落到宿主 AT-SPI 总线上、a11y 隔离失效）。
        env=display.app_env(),
    )
    # 等窗口 mapped + 拿到键盘焦点：同 _start_zenity，用**轮询真实前提**代替猜一个固定等待
    # （I-17）。这里额外多等一拍是因为本用例还要靠键盘焦点，窗口上树 ≠ 焦点已到位。
    time.sleep(0.3)
    if not _wait_app_on_tree("zenity"):
        raise AssertionError(
            "10 秒内 zenity 未出现在 a11y 树上 —— 不等于「无障碍没开」，"
            "更可能是沙箱私有总线/registryd 起步慢或起不来。"
        )
    time.sleep(0.3)  # 焦点落定
    return zen


def test_type_and_key_smoke(coord):
    """
    键盘注入端到端：**真的敲字、真的按回车**，并按可观察的副作用断言。

    整条链路：`xdotool type` 把文本送进焦点窗口 → `xdotool key Return` 提交 →
    zenity 以退出码 0 结束、并把输入框内容回显到 stdout。三个环节任一处断裂
    （焦点没落到对话框上、按键没送出去、文本没进输入框）本用例都会失败 ——
    这是它跟旧版（只调一次 screen_layout）的根本区别。

    在沙箱内跑（CC_CU_E2E=1 默认），不会往真实桌面的输入框里打字。
    """
    title = "E2EKey"
    zen = _start_zenity_entry(title)
    try:
        typed = "hello mcp"
        r_type = coord.type_text(text=typed)
        assert r_type.ok, f"键盘注入文本失败：{r_type.to_text()}"

        r_key = coord.press_key("Return")
        assert r_key.ok, f"按下回车失败：{r_key.to_text()}"

        try:
            out, _ = zen.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("按下回车后 zenity 未退出 —— 按键没落到对话框上")
        assert zen.returncode == 0, f"退出码={zen.returncode}（期望 0，即回车被当作「确定」）"
        assert (out or "").strip() == typed, \
            f"回显={out!r}，期望 {typed!r} —— 文本没进到输入框（焦点被别的窗口抢走了？）"
    finally:
        if zen.poll() is None:
            zen.terminate()
        _wait_window_gone(title)


# ==================== 坐标点击的落点反馈（准星图 / 偏差 / 变化）====================

def _grab(region):
    from computer_use_mcp.backend.linux import grab as grab_mod

    img, _origin = grab_mod.grab_rgb(region)
    return img


def _is_red(p) -> bool:
    return p[0] > 150 and p[1] < 90 and p[2] < 90


def _window_rect(title: str) -> tuple[int, int, int, int]:
    """按标题取窗口几何（沙箱内用 xdotool 直接问，判据与窗口管理无关）。"""
    ids = subprocess.run(["xdotool", "search", "--name", title],
                         capture_output=True, text=True,
                         env=display.env_for()).stdout.split()
    assert ids, f"没找到标题为 {title!r} 的窗口"
    out = subprocess.run(["xdotool", "getwindowgeometry", "--shell", ids[-1]],
                         capture_output=True, text=True, env=display.env_for()).stdout
    vals = dict(line.split("=") for line in out.strip().splitlines())
    return int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"])


def _window_gone(title: str) -> bool:
    """标题为 title 的窗口是否**真的**从 X 上消失了（判据与 `_window_rect` 同源）。"""
    ids = subprocess.run(["xdotool", "search", "--name", title],
                         capture_output=True, text=True,
                         env=display.env_for()).stdout.split()
    return not ids


def _wait_window_gone(title: str, timeout: float = 8.0) -> bool:
    """
    收尾用：轮询等窗口从 X 上真的消失，返回是否已消失。

    ⚠️ **不要退回「`zen.terminate()` 之后就走人」**：SIGTERM 只是**请求**进程退出，
    X 端的窗口要等客户端断开连接后由 server 回收——两者之间隔着一段真实时间。于是下一个
    用例开头读到的「活动窗口」还是上一个用例那个**正在死**的对话框，而它会在随后几秒里消失，
    把「画圈不改变活动窗口」这类判据打成偶发红。实测就是这样崩的：
    `test_ring_visible_has_hole_and_autoclears` 报 `assert 'E2EPreview' == None` ——
    前后两次读数差不是光圈造成的，是上一个用例的对话框在两次读数之间没了（与 I-17 同源：
    收尾也要等**事实**发生，不能假设它已经发生）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _window_gone(title):
            return True
        time.sleep(0.1)
    return _window_gone(title)


def test_click_xy_reports_deviation_and_change(coord):
    """
    坐标点击的**程序算偏差**端到端：

    zenity 对话框正文写死一个 ASCII 串（与语言环境无关），故意点在对话框内、但离正文
    很远的空白处，并声明 `expect` —— 程序应当报出「未命中 + 偏差像素数 + 建议改点的坐标」，
    同时给出「点后界面变化」百分比。

    ⚠️ 点**对话框外**会踩到另一个既存行为：那时 `window_id_under` 返回的是 i3 的桌面窗口，
    `windowactivate --sync` 对它要等到 xdotool 超时（10s）且最终失败——所以本用例一律
    点在对话框内部（这也更接近真实用法）。
    """
    title = "E2EAim"
    zen = subprocess.Popen(
        ["zenity", "--question", "--title", title, "--text", "MCP e2e target"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=display.app_env(),
    )
    try:
        if not _wait_app_on_tree("zenity"):
            pytest.fail("zenity 未上树（沙箱总线起步问题？）")
        time.sleep(0.5)
        wx, wy, ww, wh = _window_rect(title)
        # 对话框内、贴近左侧中部：离正文与按钮都远
        x, y = wx + 15, wy + wh // 2

        r = coord.click_xy(x, y, expect="target")

        assert r.ok, r.to_text()
        assert "期望「target」" in r.message, r.message
        assert ("未命中" in r.message) or ("未见" in r.message), r.message
        assert "点后界面变化" in r.message, r.message
        assert r.preview is not None, "坐标点击必须带预览图"
        meta = r.preview[1]
        assert meta["scale"] == 1 and meta["kind"] == "click_preview", meta
    finally:
        if zen.poll() is None:
            zen.terminate()
        _wait_window_gone(title)


def test_click_preview_geometry_matches_screen(coord):
    """预览图几何：红十字中心必须落在**实际点击的像素**上（图上 1px = 屏幕 1px）。"""
    title = "E2EPreview"
    zen = _start_zenity(title)
    try:
        wx, wy, ww, wh = _window_rect(title)
        x, y = wx + ww // 2, wy + 12          # 对话框内靠上（不是按钮，不会关窗）
        r = coord.click_xy(x, y, expect=None)
        assert r.ok and r.preview is not None, r.to_text()

        from PIL import Image
        import io

        img = Image.open(io.BytesIO(r.preview[0]))
        meta = r.preview[1]
        cx, cy = meta["crosshair"]
        px = img.load()

        assert img.size == (meta["width"], meta["height"]), (img.size, meta)
        assert 0 <= cx < img.width and 0 <= cy < img.height, (cx, cy)
        # 换算公式：原点 + 图上坐标 = 屏幕绝对坐标
        assert meta["origin"][0] + cx == meta["x"], meta
        assert meta["origin"][1] + cy == meta["y"], meta
        assert _is_red(px[cx, cy]), f"十字中心不是红色：{px[cx, cy]}"
    finally:
        if zen.poll() is None:
            zen.terminate()
        _wait_window_gone(title)


def test_ring_visible_has_hole_and_autoclears(coord):
    """
    光圈像素判据。

      ★ 三条**同时**成立才算过：环带四边中点有红、**中心无红**、包围盒四角无红。
      只数红像素是不够的：实心红方块同样能凑出上千个红像素，而那样这个圈就会**挡住**
      它覆盖的一切。中心与四角正是用来识破「SHAPE 请求被接受了但没生效」的。

    安全属性：TTL 过后屏上与该点画圈前**逐像素一致**（窗口销毁后不留残留），
    且画圈不改变活动窗口（沙箱是 PointerRoot 焦点语义，建窗不能把焦点弄跑）。

    ⚠️ **不要退回「show 完 sleep 0.4 就抓屏」**（I-17 同一个坑）：worker 首连要建 X 连接，
    机器被别的进程压着时 0.4 秒可能还没上屏，于是同一份代码忽绿忽红。这里改成**轮询到
    圈真的出现**（上限 3s），TTL 给足 5 秒——判据仍是那三条像素断言，只是不再赌时间。
    """
    from computer_use_mcp.backend.linux import ring as ring_mod
    from PIL import ImageChops

    # 同进程里前面的用例可能触发过真实点击（每次点击都会 show），若期间发生过瞬时失败，
    # 按屏熔断会把本屏拉黑——那属于「之前那次的故障」，不该让本用例替它背锅，故清零重来；
    # 真有问题时下面的像素判据照样会红，并打印 status() 供定位。
    ring_mod._FAILS.clear()
    ring_mod._DISABLED.clear()

    x, y = 60, 60                      # 沙箱左上角空区域（对话框在中间）
    # ⚠️ 尺寸从模块读，**不要写死字面量**：光圈大小是观感参数（已从 100 调到 48），
    # 写死会让本用例在调参后「采样点落到圈外」——判据还在，测的东西已经不是那个圈了。
    size, thick = ring_mod._SIZE, ring_mod._THICK
    off = thick // 2                   # 边框环带的中点：离窗口上/左沿 thick/2 处
    region = (x - 60, y - 60, 120, 120)

    before = _grab(region)
    title_before = coord.backend.active_window_title()

    ring_mod.show(x, y, ttl=5.0)

    def _band_visible(img) -> bool:
        p = img.load()
        w, _h = img.size
        o_ = (w - size) // 2
        c_ = w // 2
        return _is_red(p[c_, o_ + off]) and _is_red(p[c_, o_ + size - off - 1])

    deadline = time.monotonic() + 3.0
    during = _grab(region)
    while not _band_visible(during) and time.monotonic() < deadline:
        time.sleep(0.15)
        during = _grab(region)

    px = during.load()
    w, _h = during.size
    o = (w - size) // 2                # 窗口在抓取区域内的偏移
    c = w // 2
    band = [_is_red(px[c, o + off]), _is_red(px[c, o + size - off - 1]),
            _is_red(px[o + off, c]), _is_red(px[o + size - off - 1, c])]
    center_clean = not _is_red(px[c, c])
    corners_clean = not _is_red(px[o + off + 1, o + off + 1]) and not _is_red(
        px[o + size - off - 2, o + size - off - 2])

    time.sleep(5.5)                    # 等 TTL（5s）自毁
    after = _grab(region)
    title_after = coord.backend.active_window_title()

    assert all(band), f"环带没画上：{band}（status={ring_mod.status()}）"
    assert center_clean, "圈的中心是红的 —— 环被画成了实心块"
    assert corners_clean, "圈的四角是红的 —— 形状没生效（SHAPE 请求被接受但没应用？）"
    assert ImageChops.difference(before, after).getbbox() is None, "光圈未自毁，屏上留了残留"
    assert title_before == title_after, "画圈改变了活动窗口（焦点被抢）"
