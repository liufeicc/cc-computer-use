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
