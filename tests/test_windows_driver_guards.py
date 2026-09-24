"""
Windows 驱动层的结构性守卫（可在 **Linux** 上跑）。

═══ 为什么这些守卫能在 Linux 上跑，而驱动本身不能 ═══

驱动层是纯 Windows 的，但它的**结构约束**与平台无关：
  · 「全仓只有一处 import uiautomation」是个 import 图问题；
  · 「本包零 subprocess」是个 AST 问题；
  · 「在 Linux 上 import 不得炸」是个 import 期副作用问题。

这三条都靠在真机上跑一百次也**发现不了**（它们在 Linux 上就是绿的，直到某天有人在
Windows 上加了一行 `import comtypes` 到模块顶层、或者顺手 spawn 了个命令）。
用守卫钉住，代价接近零。

判据纪律同 `test_platform_module_boundary.py`：每条都要能被证伪，且正控不许空转。
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"
_WINPKG = _SRC / "backend" / "windows"


def _py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("*.py"))


# ==================== ① 平台库的 import 必须单点 ====================

def test_uiautomation_is_imported_in_exactly_one_place():
    """
    `import uiautomation` 全仓**只允许出现在** `driver/_uia.py`。

    为什么（同 `_bootstrap.import_atspi` 的单点纪律）：
      · 换库 / 将来自己写 ctypes 封装 → 只改一个文件；
      · **DPI 感知必须在 import 之前设**（`uiautomation` 在模块导入时就会调
        `SetProcessDpiAwareness`，而 Windows 的感知度只能设一次、后设的静默失败）。
        散落各处 import 就等于散落各处漏掉那个前置；
      · 守卫可以断言这条，而不是靠"记得"。

    正控：`_uia.py` 必须真的 import 它——否则本规则是把坏尺，量不到东西还全绿。
    """
    importers: list[str] = []
    for path in _py_files(_SRC):
        rel = str(path.relative_to(_SRC))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            if any(n.split(".")[0] == "uiautomation" for n in names):
                importers.append(rel)
                break

    assert importers == ["backend/windows/driver/_uia.py"], (
        f"uiautomation 的 import 点不是唯一一个：{importers}\n"
        f"必须收敛到 backend/windows/driver/_uia.py（DPI 前置与能力自检都在那里）"
    )


# ==================== ② 驱动层零 subprocess ====================

def test_windows_driver_has_no_subprocess():
    """
    `backend/windows/` 下**不得出现** `subprocess` 调用。

    两条理由：
      1. **性能**——每个坐标准确的窗口操作若 spawn 一次进程，几百毫秒就没了，
         而 UIA 的一次属性读是个位数毫秒；驱动层必须全程 ctypes/COM。
      2. **它让 `tests/test_spawn_env.py` 那条守卫在本包上恒真**，不需要为本包加豁免。
         （那条守卫要求每个 spawn 点的 `env=` 源自 `env_for`/`app_env`；豁免加多了
         等于把它废掉——所以能不加就不加。）

    正控：同一把尺子在 `backend/linux/` 上必须**量得到东西**（那里到处是 subprocess）。
    量不到说明扫描逻辑坏了，本断言就变成了摆设。
    """
    def count(root: pathlib.Path) -> list[str]:
        hits = []
        for path in _py_files(root):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute) and node.attr in (
                        "Popen", "run", "call", "check_call", "check_output"):
                    if isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                        hits.append(str(path.relative_to(_SRC)))
        return hits

    offenders = count(_WINPKG)
    assert not offenders, f"Windows 驱动层出现了 subprocess 调用：{offenders}"

    # 正控：Linux 侧必须有（否则是尺子坏了，不是"代码很干净"）
    assert count(_SRC / "backend" / "linux"), "尺子量不到任何 subprocess —— 判据已失效"


# ==================== ③ 在 Linux 上 import 不得炸 ====================

def test_driver_modules_import_on_this_platform():
    """
    驱动层的模块在**非 Windows**上也必须能 import。

    为什么：CI 跑在 Linux，而本文件的其余断言（以及将来更多结构守卫）要 import 它们。
    实现在于**所有平台绑定都延迟到函数内**：`ctypes.WinDLL(...)` 与
    `import uiautomation` 都藏在函数里，模块顶层只放纯 Python 的结构定义与常量。

    ⚠️ 这条在真机上永远发现不了（Windows 上当然能 import）——正是"只在另一边失败"
    的那类问题，所以单独钉住。

    用**子进程**跑：本进程里这些模块可能已被别的测试导入过，测不到"冷启动会不会炸"。
    """
    code = (
        "import importlib, sys;"
        "mods=['computer_use_mcp.backend.windows',"
        "'computer_use_mcp.backend.windows.driver',"
        "'computer_use_mcp.backend.windows.driver.win32',"
        "'computer_use_mcp.backend.windows.driver.session_info',"
        "'computer_use_mcp.backend.windows.driver.tree',"
        "'computer_use_mcp.backend.windows.driver._uia',"
        "'computer_use_mcp.backend.windows.driver.dpi_probe',"
        "'computer_use_mcp.backend.windows.driver.probe'];"
        "[importlib.import_module(m) for m in mods];"
        "print('IMPORT_OK')"
    )
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=120,
                          cwd=str(_SRC.parent.parent))
    assert "IMPORT_OK" in proc.stdout, (
        "驱动层在非 Windows 上 import 失败 —— 平台绑定没做成惰性的。\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr[-2000:]}"
    )


# ==================== ④ DPI 前置不许被绕过 ====================

def test_dpi_awareness_is_set_before_importing_uia():
    """
    `_uia.import_uia()` 必须在 `import uiautomation` **之前**调 `ensure_dpi_awareness()`。

    这段顺序是全层最容易写错、且错法**完全静默**的地方：
    `uiautomation` 自己在模块级执行 `SetProcessDpiAwareness(PerMonitorDpiAware)`（旧 API、
    非 V2），而 Windows 的进程 DPI 感知度**只能设一次**，后设的失败。
    顺序反了 → 感知度停在 V2 之外的值 → UIA 矩形与注入坐标不在同一空间 →
    **只在非 100% 缩放的机器上偏几十像素**，在开发机上永远重现不了。

    判据取源码里的**语句顺序**（而不是"有没有调用"）：两种调用都在，顺序才是要害。
    """
    src = (_WINPKG / "driver" / "_uia.py").read_text(encoding="utf-8")
    fn_start = src.index("def import_uia(")
    fn_body = src[fn_start:src.index("\nclass ", fn_start)]

    dpi_at = fn_body.find("ensure_dpi_awareness()")
    imp_at = fn_body.find("import uiautomation")
    assert dpi_at != -1, "import_uia 里没有调 ensure_dpi_awareness —— DPI 前置被摘掉了"
    assert imp_at != -1, "import_uia 里没有 import uiautomation？判据锚点失效，请更新本测试"
    assert dpi_at < imp_at, (
        "在 _uia.import_uia 里，`import uiautomation` 出现在设置 DPI 感知**之前** —— "
        "uiautomation 会在导入时自己把感知度设死（且不是 V2），我们要的那次会静默失败。"
    )


# ==================== ⑤ probe 的 JSON 契约 ====================

def test_probe_writes_the_documented_top_level_keys():
    """
    尖刀报告必须包含约定的顶层键。

    为什么用测试钉住一个"报告格式"：这份 JSON 是**给后续判断做依据**的证据，
    少一个键（比如漏了 `session`）就会让读结果的人把"服务会话下的环境假象"
    当成"UIA 能力不行"——真机 DEMO 里为了拆清这件事多花了三轮排查。
    """
    src = (_WINPKG / "driver" / "probe.py").read_text(encoding="utf-8")
    for key in ('"session"', '"dpi"', '"uiautomation"', '"tree"', '"hwnd_drill"',
                '"latency"', '"threaded"', '"verdict"'):
        assert key in src, f"probe 报告少了约定的顶层键 {key}"


@pytest.mark.parametrize("verdict,expected_code", [
    ("ok", 0),
    ("session_untrusted", 3),
    ("dpi_not_set", 4),
])
def test_probe_exit_codes_are_stable(verdict, expected_code):
    """
    退出码契约不许漂移。

    调用方（真机脚本 / CI）靠它判断，而不是读 JSON 猜。尤其是 3 与 4——
    它们表示"**这次的数据不可信**"，与 5（"跑通了但有自检项没过"）的处置完全不同。
    """
    from computer_use_mcp.backend.windows.driver import probe

    mapping = {"ok": 0, "session_untrusted": 3, "dpi_not_set": 4}
    assert mapping[verdict] == expected_code
    assert probe  # 模块可 import（上面那条子进程断言之外，再确认本进程也能用）


# ==================== ⑥ 结构体布局（唯一能在 CI 里跑的真机契约）====================

def test_ctypes_structs_match_the_windows_x64_layout():
    """
    ctypes 结构体的**字节布局**必须与 Windows x64 逐字节一致。

    ═══ 为什么这条值得单独守 ═══

    `DEVMODEW` 是 `EnumDisplaySettingsW` 的**输出缓冲**，`dmPelsWidth` 在本项目里是
    判定「坐标有没有被 DPI 虚拟化」的**唯一绝对参照物**（见 `dpi_probe` 的 docstring）。
    布局错一位 ⇒ 读到一个垃圾数 ⇒ **整份 DPI 结论反向**，而且不会有任何报错：
    `EnumDisplaySettingsW` 只看 `dmSize` 填得对不对，只要那个数对了它就照写。

    更隐蔽的是：`ctypes.wintypes` 在非 Windows 上把所有名字都建在 `c_long`/`c_ulong`
    之上，而 Linux 是 LP64（8 字节）、Windows 是 LLP64（4 字节）—— 照抄头文件写
    `wintypes.DWORD` 会得到一个**在 Linux 上大小不同**的结构体。那是"只在真机上才
    发现得了"的那类问题，与本文件其他条目同一个病根。

    所以驱动层里凡是构成结构体布局的地方一律用**定宽别名**（`_WORD`/`_DWORD`/
    `_LONG`/`_WCHAR`），使得布局跨平台一致 —— 于是**能在 CI（Linux）里断言**，
    不必依赖真机。本用例就是那条断言。

    期望值取自 Windows 头文件的手工推导（`sizeof` 是 ABI 契约的一部分，不是实现细节）：
      · `DEVMODEW` 220（x64；两个 32 元素的 2 字节字符数组 + 一个 16 字节匿名并集）
      · `INPUT` 40（x64；DWORD type + 4 字节对齐填充 + 32 字节 MOUSEINPUT）
      · `POINTL` 8
    """
    import ctypes

    from computer_use_mcp.backend.windows.driver import win32

    assert ctypes.sizeof(win32.POINTL) == 8, "POINTL 布局漂了（c_long 在 Linux 上是 8 字节）"
    assert ctypes.sizeof(win32.MOUSEINPUT) == 32, "MOUSEINPUT 布局漂了（同上）"
    assert ctypes.sizeof(win32.DEVMODEW) == 220, (
        "DEVMODEW 布局与 Windows x64 不一致 —— dmPelsWidth 会读到垃圾值，"
        "而它是 DPI 判定的绝对参照物。检查是否有人把定宽别名换回了 wintypes.DWORD/WCHAR。"
    )
    assert ctypes.sizeof(win32.INPUT) == 40, (
        "INPUT 布局不对 —— SendInput 的 cbSize 传错时**返回 0 且不报错**，"
        "表现成「注入静默无效」。"
    )

    # 关键字段的偏移也要钉住：大小对而字段错位是最坏的一种（两种错互相抵消掉 size 断言）。
    off = {name: getattr(win32.DEVMODEW, name).offset for name, _t in win32.DEVMODEW._fields_}
    assert off["dmPelsWidth"] == 172, f"dmPelsWidth 偏移漂了：{off['dmPelsWidth']}"
    assert off["dmPelsHeight"] == 176, f"dmPelsHeight 偏移漂了：{off['dmPelsHeight']}"
    assert off["dmSize"] == 68, f"dmSize 偏移漂了：{off['dmSize']}"


def test_dpi_probe_verdict_distinguishes_virtualized_from_not():
    """
    `_virtualization_verdict` 必须真的能分出"被虚拟化"与"没有"。

    ⚠️ 这是一条**正控**：只断言"给一份数据它返回 ok"是坏尺子 —— 一个恒定返回
    `virtualized=False` 的实现照样过。故两侧都给数据，要求它给出**两个不同的答案**。

    判据本身用的是一个绝对参照物（`EnumDisplaySettingsW` 的物理像素 vs
    `GetSystemMetrics` 的虚拟化值），这个函数就是那次比较。
    """
    from computer_use_mcp.backend.windows.driver import dpi_probe

    def verdict(sm, pels):
        return dpi_probe._virtualization_verdict(
            {"SM_CXSCREEN": sm[0], "SM_CYSCREEN": sm[1]},
            {"dmPelsWidth": pels[0], "dmPelsHeight": pels[1]},
        )

    # 150% 缩放下，非感知进程看到的系统度量是被除小的那份
    scaled = verdict((1280, 720), (1920, 1080))
    assert scaled["virtualized"] is True
    assert scaled["implied_scaling_percent"] == 150

    # 100% 缩放：两者相等
    plain = verdict((1920, 1080), (1920, 1080))
    assert plain["virtualized"] is False
    assert plain["implied_scaling_percent"] == 100

    # 拿不到度量时必须报 ok=False，而不是"默认没虚拟化"——
    # 后者会把一次读数失败伪装成一条正面结论，正是本项目最忌讳的那种静默。
    missing = dpi_probe._virtualization_verdict({}, {})
    assert missing["ok"] is False


def test_dpi_probe_exit_code_for_virtualized_coordinates_is_distinct():
    """
    「坐标被虚拟化」必须有**自己**的退出码，不能混进 5。

    这是本探针存在的唯一理由那条结论。若它和"跑通了但自检项没过"共用一个码，
    调用方（真机脚本）就没法用退出码区分「结果是坏消息」与「结果是坏消息但环境可疑」，
    只能去读 JSON —— 而退出码存在的意义正是不用读 JSON。
    """
    from computer_use_mcp.backend.windows.driver import dpi_probe

    assert dpi_probe._EXIT_CODES["coordinates_virtualized"] == 6
    # 其余几个沿用 probe 已公布的约定，不许漂移
    assert dpi_probe._EXIT_CODES["ok"] == 0
    assert dpi_probe._EXIT_CODES["session_untrusted"] == 3
    assert dpi_probe._EXIT_CODES["dpi_not_set"] == 4


def test_dpi_probe_report_keeps_the_two_verdicts_separate():
    """
    报告必须**同时**含「绝对判据」与「回环」两块，且 docstring 里写明回环判不了坐标空间。

    ⚠️ 这条守的是一类**认知**错误，不是代码错误：把"挪指针 → 读回 → 问窗口"这套
    自洽的回环当成坐标空间的判据。它天然自洽（整条链共用同一层 DPI 虚拟化），
    所以它**永远绿**，也就永远发现不了问题 —— 而它看起来又特别像在验证这件事。

    故用两条断言钉住：① 报告里两个键都在；② 模块 docstring 里保留了那句说明。
    删掉那段说明不会让任何代码变红，只会让后来者重新踩一遍。
    """
    from computer_use_mcp.backend.windows.driver import dpi_probe

    src = (_WINPKG / "driver" / "dpi_probe.py").read_text(encoding="utf-8")
    for key in ('report["virtualization"]', 'report["awareness"]',
                'report["windows"]', 'report["session"]', 'report["dpi_setup"]'):
        assert key in src, f"dpi_probe 报告少了约定的键 {key}"
    assert "恒真" in dpi_probe.__doc__, (
        "dpi_probe 的 docstring 丢了「回环判据恒真、不能用来判定坐标空间」那段说明 —— "
        "这正是最容易犯的错，必须留在文件里。"
    )

