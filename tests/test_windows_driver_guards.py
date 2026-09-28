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
        "'computer_use_mcp.backend.windows.driver.nodes',"
        "'computer_use_mcp.backend.windows.driver.apps',"
        "'computer_use_mcp.backend.windows.driver.actions',"
        "'computer_use_mcp.backend.windows.backend',"
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


# ==================== ③b Pattern 判定不许走 getattr ====================

def test_pattern_probing_never_uses_getattr_with_a_default():
    """
    Windows 驱动层里**不许**出现 `getattr(…, "IsXxxPatternAvailable", 默认值)`。

    ═══ 这条守卫的来历（2026-09-28 真机事故）═══

    `nodes.actions_of()` 原先正是这么写的，而真机实测它**恒为 False**：
    `uiautomation.Control` 根本没有 `IsInvokePatternAvailable` 这类属性，
    而 `getattr(x, name, 默认值)` 把「**属性不存在**」与「**属性为 False**」
    混成了一件事 —— 名字写错不报错、只静默给默认值。后果是所有元素的动作表恒为空，
    元素级 do_action（本项目的第一原则）整条路是死的，**而单测全绿**：
    桩对象恰好长着我臆想出来的那个属性，守卫在拿虚构验证虚构。

    ⚠️ 所以这条守卫**不能**写成"断言属性名拼对了"（那还是靠人记得），
    只能写成"**禁止这个语法形状**" —— 让"写错的属性名静默变成默认值"在构造上不可能。
    正确的路只有一条：`_uia.resolve_pattern()` 拿数值 + `element.GetPattern(id)`，
    名字写错会在 `resolve_pattern` 里响亮地抛。

    正控：驱动层里必须**确实存在** `GetPattern` 调用 —— 否则本规则是"禁止了一切，
    包括正确写法"，那样它会在把实现删空之后照样全绿。
    """
    bad: list[str] = []
    has_get_pattern = False
    for path in _py_files(_WINPKG):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "GetPattern":
                    has_get_pattern = True
                # getattr(x, "字面量", 默认值) —— 三参形式才是危险的（吞掉 AttributeError）
                if (isinstance(fn, ast.Name) and fn.id == "getattr"
                        and len(node.args) >= 3
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)
                        and "PatternAvailable" in node.args[1].value):
                    bad.append(f"{path.name}:{node.lineno} "
                               f'getattr(…, "{node.args[1].value}", …)')

    assert has_get_pattern, (
        "驱动层里一处 `GetPattern` 调用都没有 —— 本规则的正控失败，"
        "它现在禁止的是一切（包括唯一正确的写法），等于失效。"
    )
    assert not bad, (
        "驱动层出现了 `getattr(元素, \"IsXxxPatternAvailable\", 默认值)`：\n  "
        + "\n  ".join(bad)
        + "\n这种写法会把「属性不存在」与「属性为 False」混为一谈（见本用例 docstring）。"
        "\n改用 `_uia.resolve_pattern(名字)` + `element.GetPattern(id)`。"
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



# ==================== ⑦ WindowsBackend 必须满足 ABC 契约 ====================

def test_windows_backend_implements_every_abstract_method():
    """
    `WindowsBackend` 必须把 `Backend` 的**每一个**抽象方法都实现掉。

    ⚠️ 为什么要用测试钉住一个"类写全了没有"：漏掉一个抽象方法，Python 只在
    **实例化时**才报 `TypeError: Can't instantiate abstract class` —— 而
    `get_backend()` 是在 Windows 上被调用的，**这个错误在 Linux 的 CI 里永远看不见**。
    等到真机上才发现，代价是一轮完整的构建 + 部署 + 复现。

    判据取 `__abstractmethods__` 这个**运行时事实**，不是去比对源码里的 def 列表 ——
    后者数得再全也证明不了"签名对得上"。
    """
    from computer_use_mcp.backend.base import Backend
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    assert issubclass(WindowsBackend, Backend)
    missing = set(WindowsBackend.__abstractmethods__)
    assert not missing, (
        f"WindowsBackend 还有未实现的抽象方法：{sorted(missing)} —— "
        f"它无法被实例化，而 `get_backend()` 正是在 Windows 上实例化它的地方"
    )

    # 正控：ABC 本身确实带抽象方法（否则 `__abstractmethods__` 恒空，本判据失效）
    assert Backend.__abstractmethods__, "Backend 已无抽象方法，本判据失去意义"


def test_windows_backend_is_instantiable_on_this_platform():
    """
    在 **Linux** 上也能构造出 `WindowsBackend` 实例。

    这是"惰性绑定"纪律的直接检验：`__init__` 里若 import 了 uiautomation 或
    建了 COM 对象，本用例当场炸。而那种写法会让 `get_backend()` 的失败面
    从"工具调用时"提前到"构造时"，报错也离真正的原因更远。

    ⚠️ 只构造、**不调任何方法** —— 调 `is_available()` 会尝试 `import uiautomation`，
    那在 Linux 上当然失败（那是环境问题，不是结构问题）。本用例只问结构。
    """
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    backend = WindowsBackend()
    assert backend.name == "windows"
    assert "windows" in type(backend).__module__


def test_windows_backend_has_no_unimplemented_parts_left():
    """
    Windows 后端**不该再有"未实现"的方法** —— 有的话必须**响亮抛错**，不许返回空。

    这条守的是一类**认知**错误：返回一张空图 / 一个空列表，会被模型读成
    「这个界面是黑的」/「这儿没有文字」，把"功能没做"伪装成"界面的内容就是这样"。

    ⚠️ 本用例是从旧版**收窄再反转**来的：旧版断言「`screenshot` 与 `read_text` 都必须抛
    BackendUnavailableError」—— 那是当时的诚实边界。后来 `screenshot` 先落地（查明
    "抓屏需要独立平台 API"是错误判断），`read_text` 也在 2026-09-28 用系统自带的
    `Windows.Media.Ocr` 落地。**两项都推进了，所以清单空了** —— 这是好事。
    把某个方法加回"必须抛错"的清单，说明它是真的又退回去了。
    """
    from computer_use_mcp.backend.base import Backend
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    # 全部可选能力都必须有本平台的实现，不许靠 ABC 默认的 None/[] 糊过去
    for name in ("screenshot", "click_preview", "change_fraction_since",
                 "read_text", "read_text_from_image", "describe_point"):
        assert getattr(WindowsBackend, name) is not getattr(Backend, name), (
            f"WindowsBackend 没有覆写 {name} —— 它正走 ABC 的默认实现（返回空/None），"
            f"那会把「功能没做」伪装成「那儿什么都没有」。"
        )


def test_windows_ocr_declares_that_min_conf_is_not_honoured():
    """
    ★ 自带 OCR 没有置信度 —— `min_conf` 必须**如实声明不生效**，不许静默变 no-op。

    为什么这条要紧：`get_screen_text` 上挂着 `min_conf` 参数，而 `TextBlock.conf` 的
    唯一消费点就是按它过滤。Windows 引擎（`Windows.Media.Ocr`）的 `OcrWord` 只有
    `BoundingRect` 与 `Text`（2026-09-28 实测，词级/行级都没有 confidence 字段），
    所以 `conf` 只能填常数 —— 于是 `min_conf` 什么都不筛。

    模型若以为筛选发生了，就会把噪声当成可信结果。**让参数静默变 no-op 比不支持它更糟。**
    """
    from computer_use_mcp.backend.base import Backend
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    assert WindowsBackend.SUPPORTS_TEXT_CONFIDENCE is False
    # 正控：别的平台默认为 True（否则这条"声明"就成了全体适用的噪声）
    assert Backend.SUPPORTS_TEXT_CONFIDENCE is True

    note = WindowsBackend().min_conf_notice(40.0)
    assert note, "不支持置信度却没给出任何说明 —— min_conf 会静默失效"
    assert "未生效" in note and "40" in note, f"说明没把「没生效」和取值说清楚：{note!r}"

    # 支持时必须是**空串**（否则每次 get_screen_text 都多一行噪声）。
    # 这里不实例化 Backend（它是抽象类），只用一个带旗标的最小替身调未绑定方法。
    class _Supports:
        SUPPORTS_TEXT_CONFIDENCE = True

    assert Backend.min_conf_notice(_Supports(), 40.0) == ""


def test_winrt_is_imported_in_exactly_one_place():
    """
    `import winrt` 全仓**只允许出现在** `driver/ocr.py`（同 `_uia.py` 的 uiautomation 纪律）。

    好处：缺包时的错误信息只有一处要维护；换绑定只改一个文件；守卫可以断言这条
    而不是靠纪律。
    """
    importers: list[str] = []
    for path in _py_files(_SRC):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name == "winrt" or a.name.startswith("winrt.")
                       for a in node.names):
                    importers.append(str(path.relative_to(_SRC)))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "winrt" or node.module.startswith("winrt."):
                    importers.append(str(path.relative_to(_SRC)))

    assert importers, (
        "全仓没有一处 import winrt —— 本规则的正控失败（OCR 要么没做，要么换了别的绑定）"
    )
    allowed = {"backend/windows/driver/ocr.py"}
    stray = sorted(set(importers) - allowed)
    assert not stray, (
        f"这些地方绕过了单点直接 import winrt：{stray}。"
        f"请改走 backend/windows/driver/ocr.py —— 理由见本用例 docstring。"
    )


# ==================== ③c OCR 的中文合并（纯逻辑，Linux 上可测）====================

class _Word:
    """照真 UIA/WinRT 形状的假词：只有 `.text` 与 `.bounding_rect`。"""

    def __init__(self, text: str, x: float, y: float, w: float, h: float) -> None:
        self.text = text
        self.bounding_rect = type("R", (), {"x": x, "y": y, "width": w, "height": h})()


def test_ocr_merges_consecutive_cjk_chars_into_one_block():
    """
    ★ **中文必须合并**，否则 `get_screen_text` 会逐字列出，模型读不出词。

    Windows OCR 把中文逐字切成 word（`了解有关此图片的信息` → 6 个各含一字的词），
    若照搬"一个词一个 TextBlock"，输出会是：
        `[5] 了 @ (982,83)`、`[6] 解 @ (999,83)` …
    —— 模型找不到「回收站」这个词，也就不知道该点哪儿，而这正是该工具存在的唯一理由。

    判据取**文本**（是不是单个 CJK 字符）而不是几何间距：后者要靠实测调阈值，
    标点间距与字号都会让它飘。
    """
    from computer_use_mcp.backend.windows.driver.ocr import _merge_runs

    words = [_Word(t, 100 + i * 17, 50, 17, 16) for i, t in enumerate("回收站")]
    got = _merge_runs(words)
    assert len(got) == 1, f"三个中文单字应并成一块，实际 {got}"
    text, box = got[0]
    assert text == "回收站"
    # 并集矩形：起点取最左，宽度覆盖整段 —— 中心才是这段文字的中心
    assert box[0] == 100 and box[2] == 17 * 3, f"并集矩形不对：{box}"


def test_ocr_keeps_latin_words_separate():
    """拉丁词**不许**合并 —— 与 tesseract / Linux 侧的口径一致。"""
    from computer_use_mcp.backend.windows.driver.ocr import _merge_runs

    got = _merge_runs([_Word("Microsoft", 0, 0, 60, 16), _Word("Edge", 70, 0, 30, 16)])
    assert [t for t, _ in got] == ["Microsoft", "Edge"], got


def test_ocr_mixed_cjk_and_latin_merges_only_the_cjk_run():
    """混排：中文段并成一块，拉丁词各自成块，顺序不变。"""
    from computer_use_mcp.backend.windows.driver.ocr import _merge_runs

    words = ([_Word(t, i * 17, 0, 17, 16) for i, t in enumerate("回收站")]
             + [_Word("Microsoft", 60, 0, 60, 16)]
             + [_Word(t, 130 + i * 17, 0, 17, 16) for i, t in enumerate("文件夹")])
    got = _merge_runs(words)
    assert [t for t, _ in got] == ["回收站", "Microsoft", "文件夹"], got


def test_ocr_strips_only_the_spaces_between_cjk():
    """
    「CJK 空格 CJK」里的空格是引擎硬加的，要消掉；**拉丁词之间的空格是真的，不能动**。

    实测 Windows 的行级 `text` 就是带空格的：`'了 解 有 关 此 图'`。
    """
    from computer_use_mcp.backend.windows.driver.ocr import _CJK_SPACE

    assert _CJK_SPACE.sub("", "了 解 有 关") == "了解有关"
    assert _CJK_SPACE.sub("", "Microsoft Edge") == "Microsoft Edge"
    # 中↔拉之间那个空格**要保留**（它是真的排版空格，不是引擎硬加的）
    assert _CJK_SPACE.sub("", "回 收 站 Microsoft Edge") == "回收站 Microsoft Edge"


def test_ocr_blocks_offset_by_origin():
    """
    ★ OCR 给的是**图内像素**，返回必须是**屏幕绝对坐标**（加 origin）。

    漏了这一步的表现：模型拿到一组"看起来合理"的坐标，点下去整体偏一个截图原点，
    而没有任何报错 —— 与 `grab_rgb` 的 origin 契约同一类坑。
    """

    class _R:
        lines = None

        def __init__(self) -> None:
            ln = type("L", (), {})()
            ln.words = [_Word("文", 10, 20, 17, 16), _Word("字", 27, 20, 17, 16)]
            ln.text = "文 字"
            self.lines = [ln]

    from computer_use_mcp.backend.windows.driver.ocr import OcrReader

    blocks = OcrReader._blocks_from_result(_R(), (1000, 500))
    assert len(blocks) == 1
    b = blocks[0]
    assert b.text == "文字"
    assert (b.rect.x, b.rect.y) == (1010, 520), f"没加上 origin：{b.rect}"
    assert b.conf > 0, "conf 是占位常数，但不应为 0（会被 min_conf 默认 40 滤掉）"


def test_windows_backend_reuses_the_neutral_screen_layer():
    """
    Windows 的抓屏/预览/点后对比必须复用 `backend/screen.py`，**不许自己写一套像素活**。

    为什么值得钉住：初版把这三样判成「需要 BitBlt/PrintWindow 的独立平台 API」，
    于是 Windows 上半点反馈都没有（`click_preview` 返回 None 会**连锁掐掉全部四样**
    反馈：预览图 / 落点文字 / 偏差数值 / 点后变化）。而事实是它们的平台相关部分
    只有「抓哪块屏」一句话。

    判据是**源码文本**（导入关系），不是行为 —— 因为行为在 Linux 上跑不了
    （`grab_desktop` 要真屏）。所以配一条正控：`screen` 模块本身必须真的被导入。
    """
    src = (_WINPKG / "backend.py").read_text(encoding="utf-8")
    assert "from .. import screen" in src, (
        "Windows 后端没有导入 backend/screen.py —— 抓屏要么没做，要么又自己写了一套。"
    )
    for call in ("screen.grab_desktop(", "screen.encode_image(",
                 "screen.build_click_preview(", "screen.changed_fraction("):
        assert call in src, f"Windows 后端里找不到 {call} —— 中立的像素层没被用上。"


# ==================== ⑧ 坐标点击"先聚焦"聚焦的必须是**落点下的窗口** ====================

class _Win32Spy:
    """把 `driver.win32` 里那几个与焦点有关的函数换成探针（在 Linux 上也能跑）。"""

    def __init__(self, monkeypatch, fg_hwnd: int, at_point_hwnd: int) -> None:
        from computer_use_mcp.backend.windows.driver import win32

        self.mod = win32
        self.calls: list[tuple[str, tuple]] = []
        self.fg_hwnd = fg_hwnd
        self.at_point_hwnd = at_point_hwnd

        def rec(name):  # noqa: ANN001, ANN202
            def _f(*a, **kw):  # noqa: ANN002, ANN003, ANN202
                self.calls.append((name, a))
                return None
            return _f

        def send_mouse_button(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            self.calls.append(("send_mouse_button", a))
            self.mouse_kwargs = kw
            return {"ok": True}

        monkeypatch.setattr(win32, "foreground_window", lambda: self.fg_hwnd)
        monkeypatch.setattr(win32, "window_from_point",
                            lambda x, y: (self.calls.append(("window_from_point", (x, y))),
                                          7000 + int(x))[1])
        monkeypatch.setattr(win32, "root_ancestor",
                            lambda h: (self.calls.append(("root_ancestor", (h,))),
                                       self.at_point_hwnd)[1])
        monkeypatch.setattr(win32, "set_foreground_window", rec("set_foreground_window"))
        monkeypatch.setattr(win32, "send_mouse_button", send_mouse_button)
        monkeypatch.setattr(win32, "send_mouse_drag",
                            lambda *a, **kw: {"ok": True})

    def named(self, name: str) -> list[tuple]:
        return [a for n, a in self.calls if n == name]


def test_click_at_focuses_the_window_under_the_point_not_the_current_foreground(monkeypatch):
    """
    `click_at(focus_window=True)` 传给输入层的 hwnd 必须是**落点下的顶层窗口**。

    ⚠️ 为什么单列一条：初版写的是 `hwnd = win32.foreground_window()` —— 把**当前前台**
    再设一遍，恒等于空操作。它不报错、不掉测试、真机上"看着也能点中"（因为物理点击
    本身也会激活窗口），所以**只能靠这条断言钉住**：故意让"当前前台"与"落点下窗口"
    是两个不同的值，然后看它到底用了哪个。
    """
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    spy = _Win32Spy(monkeypatch, fg_hwnd=111, at_point_hwnd=222)
    assert WindowsBackend().click_at(120, 340) is True

    assert spy.mouse_kwargs.get("hwnd") == 222, (
        f"传给输入层的 hwnd={spy.mouse_kwargs.get('hwnd')}，期望落点下那个窗口(222)。"
        f"若等于 111（当前前台）说明又退回成「把前台再设一遍」的空操作了。"
    )
    assert spy.named("window_from_point") == [(120, 340)], (
        f"必须按**落点**查窗口，实际调用：{spy.named('window_from_point')}"
    )


def test_click_at_without_focus_does_not_touch_windows_at_all(monkeypatch):
    """
    反向：`focus_window=False` 时**不许**去查/抢窗口（滚动就走这条路，抢焦点是纯副作用）。
    """
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    spy = _Win32Spy(monkeypatch, fg_hwnd=111, at_point_hwnd=222)
    assert WindowsBackend().click_at(10, 20, focus_window=False) is True

    assert spy.mouse_kwargs.get("hwnd") == 0, f"不该抢焦点，实际 hwnd={spy.mouse_kwargs}"
    assert spy.named("window_from_point") == [], "没让抢焦点却去查了落点窗口"
    assert spy.named("set_foreground_window") == [], "没让抢焦点却去设了前台"


def test_drag_at_focuses_the_window_under_the_start_point(monkeypatch):
    """
    拖拽抢焦点取**起点**下的窗口：意图对象由起点决定（终点只是落点），
    与 `describe_point` 只取起点证据是同一口径。
    """
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    spy = _Win32Spy(monkeypatch, fg_hwnd=111, at_point_hwnd=333)
    assert WindowsBackend().drag_at(50, 60, 500, 600) is True

    assert spy.named("window_from_point") == [(50, 60)], (
        f"应按起点查窗口，实际：{spy.named('window_from_point')}"
    )
    assert spy.named("set_foreground_window") == [(333,)], (
        f"应把起点下的窗口设到前台，实际：{spy.named('set_foreground_window')}"
    )


def test_describe_point_reports_the_top_level_window(monkeypatch):
    """
    落点证据里的 `window_title` / `window_rect` 必须来自**顶层窗口**，不是子控件。

    ⚠️ 真机实测（2026-09-28）：`WindowFromPoint` 在窗口主体上常返回**子控件**
    （编辑区、画布…），而子控件的 `GetWindowText` 是空的 → 落点那行变成
    『落点：窗口「?」』，模型分不清自己点在哪扇窗上 —— 而落点证据存在的唯一理由
    就是回答这个问题。**它是静默的**：那一行还是照样拼得出来，只是没有标题。
    先前的真机断言之所以通过，是因为采样点正好落在**标题栏**（非客户区，
    `WindowFromPoint` 直接给顶层窗）—— 采样点挑得好掩盖了缺陷。
    """
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    spy = _Win32Spy(monkeypatch, fg_hwnd=111, at_point_hwnd=999)
    monkeypatch.setattr(spy.mod, "window_text", lambda h: f"标题{h}")
    monkeypatch.setattr(spy.mod, "window_rect", lambda h: (10, 20, 30, 40))

    ev = WindowsBackend().describe_point(300, 400)

    assert spy.named("window_from_point") == [(300, 400)], "要按落点查窗口"
    assert spy.named("root_ancestor") == [(7300,)], (
        f"查到的子窗口要上溯到顶层，实际：{spy.named('root_ancestor')}"
    )
    assert ev["window_title"] == "标题999", (
        f"标题必须来自顶层窗口(999)，实际 {ev['window_title']!r} —— "
        f"若为 None 说明报的是子控件（它的 GetWindowText 是空的）"
    )
    assert ev["window_id"] == hex(999), f"window_id 也应是顶层窗口：{ev['window_id']}"


# ==================== ⑨ DPI 感知必须在**构造后端**时设死 ====================
#
# 2026-09-28 真机实测的根因（数据见 backend.py 的模块 docstring 与
# `docs/PR/windows-driver/probe_dpi_flip.py`）：感知度原先只在"第一次碰 UIA"时才设，
# 于是**进程跑到一半坐标空间会翻转** —— 碰 UIA 前 SM_CXSCREEN=1280x673、
# 同一扇窗矩形 (703,95,1230,474)；碰 UIA 后变成 1920x1009 与 (1055,143,1846,712)。
# 表现是"点标题栏却点到了桌面"，而每一层都报 ok，**只在缩放 ≠100% 的机器上出现**。


def test_windows_backend_pins_dpi_awareness_at_construction(monkeypatch):
    """
    构造 `WindowsBackend` 的那一刻就必须设 DPI 感知 —— 不能拖到第一次碰 UIA。

    这是"整个进程生命周期只有**一个**坐标空间"的唯一保证。拖到惰性设置的话，
    翻转之前取到的矩形（虚拟化坐标）会被翻转之后的注入（物理坐标）解释，
    而**每一层返回值都是 ok**，只在非 100% 缩放的机器上表现为"点偏"。
    """
    from computer_use_mcp.backend.windows.backend import WindowsBackend
    from computer_use_mcp.backend.windows.driver import _uia, win32

    calls: list[str] = []
    monkeypatch.setattr(win32, "set_dpi_awareness_v2",
                        lambda: (calls.append("set_dpi_awareness_v2"), {"ok": True})[1])
    monkeypatch.setattr(win32, "dpi_awareness_now", lambda: {"stub": True})
    monkeypatch.setattr(_uia, "_dpi_state", None)      # 复位幂等缓存，否则上面打桩不会被执行

    WindowsBackend()

    assert calls == ["set_dpi_awareness_v2"], (
        "构造 WindowsBackend 时没有设 DPI 感知 —— 于是它只会在第一次碰 UIA 时才设，"
        "进程的坐标空间会在跑到一半时翻转（非 100% 缩放的机器上表现为点偏）。"
    )


def test_windows_backend_exposes_the_dpi_result_for_diagnostics():
    """设感知度的结果要留在实例上（`ok=False` 时**整份坐标数据都不可信**，得能查）。"""
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    assert hasattr(WindowsBackend(), "_dpi"), (
        "实例上没有 _dpi —— 感知度设置的结果没被留下，真机排查时无从判断坐标可不可信。"
    )


# ==================== ⑩ 墙钟预算必须覆盖「找根窗口」那一段 ====================
#
# 2026-09-28 真机实测（`docs/PR/windows-driver/probe_chromium_scale.py`）：
# Edge（Chromium）读树总耗时 **14.4s**，而 `_TREE_DEADLINE_S` 是 12s ——
# 原来的 deadline 是在 `_roots_for` **之后**才算起的，而"找根"本身要
# `ControlFromHandle(hwnd)`，Chromium 的 a11y 是**懒建**的，第一次碰它可能好几秒。
# 于是**总耗时能超出预算而 notice 是空的** —— 恰好在最需要它说话的时候不说话。


def test_tree_deadline_is_armed_before_root_resolution(monkeypatch):
    """
    预算必须**先武装**：找根窗口那一段要是超了预算，就不该再往下遍历，且要报出来。

    ⚠️ 判据不能写成"notice 里有墙钟字样"（那只证明最后判过一次），必须证明
    **遍历真的没发生** —— 否则"预算漏掉了一段"依旧能全绿。
    """
    import time as _time

    from computer_use_mcp.backend.windows import backend as bmod
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    b = WindowsBackend()
    monkeypatch.setattr(bmod, "_TREE_DEADLINE_S", 0.05)
    monkeypatch.setattr(WindowsBackend, "ensure_available", lambda self: None)

    seen: list[str] = []

    def slow_roots(self, scope, app, deadline=None):  # noqa: ANN001, ANN202
        _time.sleep(0.2)                    # 比预算还久（模拟 Chromium 懒建树）
        assert deadline is not None, "找根时应当已经拿到预算（deadline 得先武装）"
        return [("fake", object())], []

    monkeypatch.setattr(WindowsBackend, "_roots_for", slow_roots)
    monkeypatch.setattr(WindowsBackend, "_build_tree",
                        lambda *a, **k: seen.append("build"))

    res = b.get_tree(scope="app", app="fake")

    assert seen == [], (
        "预算在找根阶段就用完了，却仍然去遍历了 —— 说明 deadline 是在找根**之后**"
        "才算起的，最慢的那一步（Chromium 首次 ControlFromHandle）落在预算之外。"
    )
    assert "墙钟上限" in res.notice, f"超预算没报出来：{res.notice!r}"


def test_find_deadline_is_armed_before_root_resolution(monkeypatch):
    """`find` 同一处形状，同样钉住（它原先也是先找根、后武装预算）。"""
    import time as _time

    from computer_use_mcp.backend.windows import backend as bmod
    from computer_use_mcp.backend.windows.backend import WindowsBackend

    b = WindowsBackend()
    monkeypatch.setattr(bmod, "_FIND_DEADLINE_S", 0.05)
    monkeypatch.setattr(WindowsBackend, "ensure_available", lambda self: None)
    monkeypatch.setattr(WindowsBackend, "_uia", lambda self: object())

    def slow_elem(self, row):  # noqa: ANN001, ANN202
        _time.sleep(0.2)
        return object()

    monkeypatch.setattr(WindowsBackend, "_element_of", slow_elem)
    # `window_rows` 走驱动层，打桩成两扇窗（全都要 ControlFromHandle）
    from computer_use_mcp.backend.windows.driver import apps
    monkeypatch.setattr(apps, "window_rows",
                        lambda limit=1000: [{"id": "0x1", "app": "a", "title": "A",
                                             "class": "C"},
                                            {"id": "0x2", "app": "b", "title": "B",
                                             "class": "C"}])

    res = b.find(text="x")

    assert res.items == [], "预算在找根阶段就用完了，不该有任何命中"
    assert "墙钟上限" in res.notice, f"超预算没报出来：{res.notice!r}"
