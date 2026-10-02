# -*- coding: utf-8 -*-
"""
Windows 打包与分发的**结构性守卫**（`packaging/windows/`）。

与 `test_packaging.py`（.deb 那套）分工：那边守 Linux 分发的约定，这边守 Windows 的。
两条路径的**纪律**是同一套（判据取「会执行的行」、要有正控、跨文件交叉核对），
但**具体契约不同**，所以是独立的文件而不是同一个文件里加一节 —— 两边改动的理由
完全不相干，混在一起改哪条都要重读另一条。

这里守的东西有个共同点：**全都在 Linux 开发机上不会被执行**。
`build-windows.sh` 只在有 Windows 客体时才跑得起来，`.ps1` 只在客体上才被解释。
所以它们错了在本机**一点动静都没有** —— 恰恰是必须用静态守卫兜住的那一类。
判据取源码文本（以及关键处的跨文件交叉核对），不取「跑一遍看看」。

⚠️ 判据纪律（本项目有血证：拆 mixin 那次两条守卫静默变成空集合、全绿但零覆盖）：
  · **尺子不许变瞎**：扫描结果为空要 fail；
  · **每条规则都要有正控**：同一把尺子必须在"本该命中"的地方命中。
"""

from __future__ import annotations

import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WIN = ("packaging", "windows")


def _read(*parts: str) -> str:
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


def _read_bytes(*parts: str) -> bytes:
    with open(os.path.join(REPO, *parts), "rb") as fh:
        return fh.read()


def _live_lines(src: str, comment: str = "#", whole_line: bool = False) -> list[str]:
    """去掉注释，只留**真正会执行**的行。

    注释里写一句「本该如此」是挡不住问题的 —— 这是本仓库反复吃过的亏，
    所以凡是「某个开关有没有真的用上」这类断言，一律先过这把尺子。

    ⚠️ `whole_line=True` 是给 Inno 的 `.iss` 用的，**不能省**：
    在 .iss 里 `;` 既是**行首注释符**、又是**参数分隔符**
    （`Filename: "x"; Parameters: "y"; Flags: "..."`）。按「从第一个 `;` 截断」处理的话，
    整行参数会被砍成只剩 `Filename: "x"` —— 于是「Flags 里有没有 postinstall」
    这类断言**永远看不到 Flags**，恒判通过（实测踩到：种一个 postinstall 进去，守卫照样绿）。

    ⚠️ PowerShell 的**块注释 `<# … #>` 也要剥掉**（2026-09-29 补）：
    只按 `#` 截断的话，块注释里的整段文字会被当成"会执行的行"。实测踩到：某个守卫
    要把范围收进一个函数体，而那个函数体开头正是一段 `<# … #>` 说明 —— 里面引用的
    **旧结论**（"之前写错过的那句话"）于是被当成运行期文案，守卫直接误报。
    这在本仓库是常态：注释里**刻意保留**错误结论的解释，正是为了让下一个人别改回去。
    """
    out = []
    in_block = False
    for ln in src.splitlines():
        if in_block:
            end = ln.find("#>")
            if end < 0:
                continue
            ln = ln[end + 2:]
            in_block = False
        # 行内可能有多段块注释，逐段剥掉
        while True:
            start = ln.find("<#")
            if start < 0:
                break
            end = ln.find("#>", start + 2)
            if end < 0:
                ln = ln[:start]
                in_block = True
                break
            ln = ln[:start] + ln[end + 2:]
        if whole_line:
            if ln.lstrip().startswith(comment):
                continue
            if ln.strip():
                out.append(ln)
            continue
        code = ln.split(comment, 1)[0]
        if code.strip():
            out.append(code)
    return out


def _joined(src: str, comment: str = "#", whole_line: bool = False) -> str:
    """把续行（shell 的 `\\`、PowerShell 的 `` ` ``）拼成一行再交给正则。

    ⚠️ 必须**先去注释再拼**：注释行里的反斜杠会把后面一行吞进来。
    """
    out: list[str] = []
    for ln in _live_lines(src, comment, whole_line):
        stripped = ln.rstrip()
        if out and out[-1].rstrip().endswith(("\\", "`")):
            out[-1] = out[-1].rstrip()[:-1] + " " + stripped.strip()
        else:
            out.append(stripped)
    return "\n".join(out)


# ==================== ① 客体脚本的编码纪律 ====================

# ⚠️ 这一节不是洁癖，是**信息丢失**问题：
#   · PowerShell 5.1 读**无 BOM** 的 .ps1 时按系统 ANSI 代码页解（中文机 = GBK），
#     脚本里的中文注释与中文字符串会变成乱码 —— 而报错位置会指向被解错的那一行，
#     排查时完全对不上。带 BOM 才能让 PS 确定地按 UTF-8 解。
#   · Inno Setup 的编译器同理读 .iss：无 BOM 时 `Description` 里的中文会以乱码
#     出现在安装向导界面里。
#   · .cmd 反过来：cmd.exe 对 BOM 与 UTF-8 都不友好，所以客体上的 .cmd 必须**纯 ASCII**。
_NEEDS_BOM = (
    (_WIN + ("build-on-windows.ps1",)),
    (_WIN + ("installer.iss",)),
    (_WIN + ("payload", "bin", "cc-computer-use-setup.ps1")),
    (_WIN + ("payload", "bin", "cc-computer-use-doctor.ps1")),
)


@pytest.mark.parametrize("path", _NEEDS_BOM, ids=lambda p: p[-1])
def test_guest_scripts_are_utf8_with_bom(path):
    raw = _read_bytes(*path)
    assert raw.startswith(b"\xef\xbb\xbf"), (
        f"{'/'.join(path)} 没有 UTF-8 BOM。中文 Windows 上 PS 5.1 / Inno 会按 ANSI 解它，"
        f"里面的中文会变成乱码（而报错位置会对不上，极难查）。"
    )
    # 正控：BOM 之后必须真的是 UTF-8 —— 否则「有 BOM」只证明前三个字节对
    raw.decode("utf-8")


def _batch_files() -> list[tuple[str, ...]]:
    """**客体上会被 cmd.exe 执行**的批处理清单：`packaging/` 的 .cmd + `tests/` 的 .bat。

    ⚠️ `tests/*.bat` 必须一起收进来（2026-09-30 补）：它们是**驱动 story 的唯一入口**
    （经计划任务 `/IT` 在客体上跑），走的正是 cmd.exe —— 与 `packaging/` 的 .cmd 同一类。
    本规则原先只写在上面那段注释里、没有任何测试盯着，而本仓库当时已经有 **6 个**
    `tests/*.bat` 在裸奔：谁哪天在里面写一行中文注释，就会得到"cmd 执行了一堆乱码"，
    而**报错完全指不到原因**。
    """
    out: list[tuple[str, ...]] = []
    win = os.path.join(REPO, *_WIN)
    if os.path.isdir(win):
        for name in sorted(os.listdir(win)):
            if name.endswith(".cmd"):
                out.append(_WIN + (name,))
    # ⚠️ 目录不存在时**跳过而不是抛**：反证脚本会把一份只有 `packaging/` 的副本挂在
    #    `REPO` 上（它只拷那一棵树），这里抛异常会让那几条反证以 `FileNotFoundError`
    #    收场 —— 而那个错**看起来和"被拦下"一样**，正是本仓库反复吃过的假通过。
    #    真正防"布局漂移"的是下面那个数量正控，不是这里的抛错。
    tests_dir = os.path.join(REPO, "tests")
    if os.path.isdir(tests_dir):
        for name in sorted(os.listdir(tests_dir)):
            if name.endswith(".bat"):
                out.append(("tests", name))
    return out


def test_guest_batch_files_are_pure_ascii():
    """
    ★ 客体上跑的 `.bat` / `.cmd` 必须**纯 ASCII**。

    ═══ 为什么（这是"信息丢失"而不是洁癖）═══

    cmd.exe 读批处理用的是**系统 ANSI 代码页**（中文机 = GBK），既不吃 BOM 也不吃 UTF-8。
    一份带中文的 `.bat` 在中文 Windows 上**可能**恰好能跑（GBK 存的中文读回来还是 GBK），
    但只要文件是 UTF-8 存的（本仓库的默认！），cmd 就会把它按 GBK 解 ——
    后果是 `REM` 注释里的某个 UTF-8 字节序列翻译出一个**能吞掉后面那个 ASCII 字符**的
    字节，于是下一行命令被拼坏，cmd 去执行一段谁也不认识的东西。

    ⚠️ 措辞刻意是"**可以**恰好能跑"：这条的性质与上面那条 BOM 规则一样，
    是**在目标机上才暴露、在开发机上时灵时不灵**的那一类 —— 所以不能靠"我这儿跑得好好的"来判。

    处置法则很简单：`.bat`/`.cmd` 里**一律只写 ASCII**，中文说明放进同名 `.py` 的
    docstring（那才是给人读的地方，且没有编码陷阱）。

    正控：清单必须**真的收到了东西**，否则「全部通过」只是"扫了个空"。
    """
    files = _batch_files()
    assert len(files) >= 7, (
        f"只找到 {len(files)} 个批处理 —— `tests/*.bat` 或 `packaging/windows/*.cmd` "
        f"的位置变了，本规则正在空转：{files}"
    )

    offenders: list[str] = []
    for parts in files:
        raw = _read_bytes(*parts)
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            # 定位到第几行、哪个字节 —— 报错要能直接拿去改，而不是让人从头翻文件
            line = raw[:exc.start].count(b"\n") + 1
            offenders.append(f"{'/'.join(parts)}（第 {line} 行有非 ASCII 字节）")

    assert not offenders, (
        "这些批处理里有非 ASCII 字节 —— cmd.exe 会按系统 ANSI 代码页解它，"
        "后果是**命令行被拼坏、执行一堆乱码**，而报错完全指不到原因；"
        "中文说明请移进同名 .py 的 docstring：\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("path", sorted(
    p for p in _NEEDS_BOM if p[-1].endswith(".ps1")
), ids=lambda p: p[-1])
def test_guest_scripts_avoid_powershell_7_only_syntax(path):
    """
    客体上是 **Windows PowerShell 5.1**（系统自带），不是 PowerShell 7。

    PS 7 才有的语法在 5.1 上**根本不解析** —— 脚本一个字都不会执行，任务静默失败。
    2026-09-29 实测踩到：验收脚本里用了空合并运算符 `??`，于是那个计划任务"跑了"却
    什么也没产出，而**报错消失在隐藏窗口里**，表现只是"结果文件没出现"。

    另一类在这里刹住的是 `&&` / `||`（PS 7 的管道链运算符，5.1 里是语法错误）——
    它们在 `.sh` 里完全正常，最容易被顺手带进 `.ps1`。
    """
    src = _read(*path)
    live = "\n".join(_live_lines(src, comment="#"))

    for op, why in (
        (r"\?\?", "空合并运算符（PS 7+）"),
        (r"\?\.", "空条件访问运算符（PS 7+）"),
        (r"^\s*\S[^\n]*\s&&\s", "管道链运算符 &&（PS 7+；5.1 里是语法错误）"),
    ):
        m = re.search(op, live, re.MULTILINE)
        assert not m, (
            f"{'/'.join(path)} 里用了{why}（第 {live[:m.start()].count(chr(10)) + 1} 行附近："
            f"{live.splitlines()[live[:m.start()].count(chr(10))].strip()!r}）—— "
            f"客体的 PS 5.1 解析不了它，脚本会**一字不执行**地静默失败"
        )
    # 正控：这份文件确实有可执行内容，否则上面等于什么都没扫
    assert len(live.splitlines()) > 20, (
        f"{'/'.join(path)} 剥完注释只剩 {len(live.splitlines())} 行 —— 守卫的选择器过期了"
    )


@pytest.mark.parametrize("path", sorted(
    p for p in _NEEDS_BOM if p[-1].endswith(".ps1")
), ids=lambda p: p[-1])
def test_guest_scripts_parse_under_powershell_51_if_available(path):
    """
    有 `pwsh`/`powershell` 时真解析一遍（没有就跳过 —— 宿主是 Linux，通常没有）。

    ⚠️ 这条只在**装了 PowerShell 的机器**上有效，所以它是上面那条静态守卫的补充而不是替代：
    静态那条管 `??` 这类已知的版本差异，这条管"语法本身写坏了"。
    判据用 `[ScriptBlock]::Create()` 做纯解析（不执行）—— 脚本里有安装、卸载这类
    副作用，**绝不能**为了"检查一下"把它跑起来。
    """
    import shutil as _sh
    import subprocess as _sp

    ps = _sh.which("pwsh") or _sh.which("powershell")
    if not ps:
        pytest.skip("本机没有 PowerShell，跳过（静态那条仍然生效）")
    full = os.path.join(REPO, *path)
    r = _sp.run(
        [ps, "-NoProfile", "-Command",
         f"$null = [ScriptBlock]::Create((Get-Content -Raw -LiteralPath '{full}')); 'PARSE-OK'"],
        capture_output=True, text=True, timeout=60,
    )
    assert "PARSE-OK" in r.stdout, f"{'/'.join(path)} 解析失败：\n{r.stdout}\n{r.stderr}"


# ==================== ② PyInstaller 的开关（动态导入那三条）====================

def test_windows_build_collects_the_dynamically_imported_packages():
    """
    Windows 冻结产物里有三处**静态分析看不见**的运行时依赖，漏了任何一个的表现都是
    「开发态一切正常、只有冻结产物里坏掉」：

      · `uiautomation` 运行时按 `__file__` 的兄弟目录找 `bin/` 下的 proxy/stub DLL
        → 必须 `--collect-all uiautomation`（只 collect-submodules 拿不到那两个 .dll）；
      · `uiautomation` 调 `comtypes.client.GetModule("UIAutomationCore.dll")`，
        那会**在运行时生成并 import 一个 Python 模块**
        → 必须 `--collect-submodules comtypes`；
      · PyWinRT 的投影包（`winrt-Windows.Media.Ocr` 等）各自往 `winrt/windows/...`
        下放子模块与 `_winrt` 扩展 → 必须 `--collect-all winrt`。

    判据取**会执行的行**：把这几个开关写在注释里、或写在某个没被调用的分支里，
    这条守卫都要能拦下来。
    """
    lines = _live_lines(_read(*(_WIN + ("build-on-windows.ps1",))))
    joined = "\n".join(lines)

    required = ("--collect-all", "uiautomation")
    assert re.search(r"'--collect-all',\s*'uiautomation'", joined), (
        "build-on-windows.ps1 没有 --collect-all uiautomation —— "
        "它 bin/ 下的 proxy/stub DLL 不是 .py，收不进来（开发态看不出来）"
    )
    assert re.search(r"'--collect-submodules',\s*'comtypes'", joined), (
        "build-on-windows.ps1 没有 --collect-submodules comtypes —— "
        "comtypes.client.GetModule 是**运行时生成并 import 模块**，静态分析看不到"
    )
    assert re.search(r"'--collect-all',\s*'winrt'", joined), (
        "build-on-windows.ps1 没有 --collect-all winrt —— Windows.Media.Ocr 的投影包收不进来"
    )

    # 反向：Linux 专属的那两个包必须**显式排除**。不排的话，get_backend() 里的
    # Linux 分支会被静态分析跟进去，客体上没装 gi/Xlib，产出一堆噪声。
    for mod in ("gi", "Xlib"):
        assert re.search(rf"'--exclude-module',\s*'{re.escape(mod)}'", joined), (
            f"build-on-windows.ps1 没有 --exclude-module {mod}"
        )


def test_windows_build_verifies_the_python_archive_not_just_directories():
    """
    产物校验必须查**归档清单**，不能只查「`_internal\\<包名>\\` 目录在不在」。

    2026-09-29 实测踩到并误杀了一轮构建：PyInstaller 把**纯 .py 的包**打进内嵌的
    `PYZ.pyz` 归档，磁盘上**没有**同名目录（只留一个 `.dist-info\\`）；
    只有带二进制/数据文件的包（uiautomation 的 DLL、winrt 的 `_winrt*.pyd`）
    才会被摊到 `_internal\\` 下。只查目录的话，对 `comtypes` / `mcp` / `mss`
    **恒判缺失** —— 而真实产物一个不少。这类"假红"比漏检更贵：它把人引去改本来正确的开关。

    判据：脚本里既要出现 `archive_viewer`（归档那一侧），
    又要真的去看 `_internal` 下的散件（磁盘那一侧）—— 两侧都要有。

    ⚠️ 归档那一侧的判据必须锚到**传给 python 的那个模块路径**，不能只 grep
    `archive_viewer` 这个词：失败提示语里也有一句 "pyi-archive_viewer 失败…"，
    只要它还在，把真正的调用删掉也照样绿（实测种这个反例时抓到的）。
    """
    live = "\n".join(_live_lines(_read(*(_WIN + ("build-on-windows.ps1",)))))
    assert re.search(
        r"'-m',\s*'PyInstaller\.utils\.cliutils\.archive_viewer'", live
    ), (
        "build-on-windows.ps1 没有用 pyi-archive_viewer 查归档 —— "
        "纯 .py 的包（comtypes/mcp/mss）不落磁盘，只查目录会恒判缺失"
    )
    assert "_internal" in live, (
        "build-on-windows.ps1 没有查 _internal 下的散件 —— "
        "uiautomation 的 DLL 与 winrt 的 _winrt*.pyd 不在归档里，只能这么查"
    )
    # 正控：清单里确实列出了这几只纯 Python 包，否则这条守卫只是在看一段死代码
    for mod in ("comtypes.client", "mcp.server", "mss"):
        assert f"'{mod}'" in live, f"归档清单里没有 {mod} —— 这条守卫正在空转"


def test_windows_build_logs_uncaught_exceptions():
    """
    脚本必须有一个兜底的 trap，把**未捕获异常的原因**写进日志。

    2026-09-29 实测踩到：一次 `Start-Process` 的参数错让脚本退出码 33，
    而日志停在「第 4 步」、**一个字的原因都没有** —— 错发生在 PowerShell 自己身上，
    还没轮到我们写日志。排查这种"静默死亡"只能靠 trap 把异常本身记下来。
    """
    live = "\n".join(_live_lines(_read(*(_WIN + ("build-on-windows.ps1",)))))
    assert re.search(r"^trap\s*\{", live, re.MULTILINE), (
        "build-on-windows.ps1 没有兜底 trap —— 未捕获异常会让日志断在半截、"
        "不留任何原因（实测踩过一次，退出码 33、日志停在『第 4 步』）"
    )
    assert "ScriptStackTrace" in live, "trap 里没有记栈 —— 只有一句话很难定位到出错那一步"


def test_windows_build_does_not_redirect_both_streams_to_one_file():
    """
    `Start-Process` 的 stdout 与 stderr **不能指向同一个文件** —— 两个句柄抢一个文件，
    PowerShell 直接抛错，而那不是我们自己的错误分支，日志里不会留原因。

    这是上面那个 trap 想兜住的**那一类**问题里最容易犯的一个（实测就是这么栽的），
    所以单列一条：它可以在编译前就静态查出来，不用等一次九分钟的构建。
    """
    live = "\n".join(_live_lines(_read(*(_WIN + ("build-on-windows.ps1",)))))
    # 找出所有 Start-Process 调用（含续行的参数），比对两个重定向的取值字符串
    calls = re.findall(
        r"Start-Process[^\n]*\n(?:[^\n]*\n){0,8}?[^\n]*RedirectStandardOutput\s+(\S+)[^\n]*"
        r"RedirectStandardError\s+(\S+)",
        live,
    )
    assert calls, "build-on-windows.ps1 里找不到带重定向的 Start-Process —— 守卫的选择器过期了"
    for out, err in calls:
        assert out != err, (
            f"有一处 Start-Process 把 stdout 与 stderr 都指向了 {out} —— "
            f"PowerShell 会直接抛错，且日志里不留原因（表现为脚本静默死在半路）"
        )


# ==================== ③ 安装器与构建产物的交叉核对 ====================

def _pyinstaller_exe_name() -> str:
    build = "\n".join(_live_lines(_read(*(_WIN + ("build-on-windows.ps1",)))))
    m = re.search(r"'--name',\s*'([^']+)'", build)
    assert m, "build-on-windows.ps1 里找不到 --name（PyInstaller 的产物名）"
    return m.group(1) + ".exe"


@pytest.mark.parametrize(
    "who", ["cc-computer-use-setup.ps1", "cc-computer-use-doctor.ps1"]
)
def test_guest_scripts_point_at_the_actual_frozen_exe_name(who):
    """
    setup.ps1 / doctor.ps1 里拼出的可执行文件路径，必须与 PyInstaller 的 `--name` 对得上。

    **这里才是真正要紧的那条**：`.iss` 装的是整个 `server\\` 目录（通配），所以入口名
    写错它照样打包成功；而**注册进 Claude Code 的那条路径是 setup.ps1 用字符串拼出来的**
    —— 拼错了的表现是装完全都"成功"、Claude Code 一启动就报找不到文件。

    ⚠️ 判据必须是**具体那一行**，不能是「iss/ps1 里出现过这个名字」：这个名字在
    `UninstallDisplayIcon`、注释里的目录树示例等处也会出现，那些地方对不对**都不影响功能**
    —— 只要文件里还剩一处是对的，弱判据就会放行（实测：把 [Setup] 里那处改成错名，
    弱判据照样绿）。
    """
    exe = _pyinstaller_exe_name()
    src = _read(*(_WIN + ("payload", "bin", who)))
    live = "\n".join(_live_lines(src, comment="#"))
    # 定位到「给 $Exe 赋值」那一行，比对它最后那个引号里的字面量。
    #   两份脚本的父目录变量不同（setup 是 $AppDir、doctor 是 $ServerDir），
    #   但「拼出来的文件名必须等于产物名」这条判据对两者都成立 —— 别去匹配前缀，
    #   匹配前缀等于把守卫绑死在两处不同的写法上。
    lines = [ln.strip() for ln in live.splitlines()
             if "$Exe" in ln and "Join-Path" in ln]
    assert lines, f"{who} 里没有 `$Exe = Join-Path ...` —— 守卫的选择器过期了"
    tails = [ln.rsplit("'", 2)[-2] for ln in lines]
    assert any(t.endswith(exe) for t in tails), (
        f"PyInstaller 的产物是 {exe}，但 {who} 里拼的是 {tails} —— 对不上。"
        f"包照样打得出来（[Files] 是通配），用户装完才会发现 Claude Code 启动不了。"
    )


def test_installer_uninstall_icon_matches_the_frozen_exe():
    """
    `UninstallDisplayIcon` 指向的 exe 名也要对得上。

    这条**只是观感**（卸载清单里图标显示不出来而已），单独列出来是因为它和前一条
    共用同一个事实来源：都该跟着 `--name` 走。分开写，免得有人以为"iss 里那处"
    就是功能契约（前一条已经说明了为什么不看 iss）。
    """
    exe = _pyinstaller_exe_name()
    iss = _read(*(_WIN + ("installer.iss",)))
    m = re.search(r"UninstallDisplayIcon=([^\r\n]+)", iss)
    assert m, "installer.iss 里没有 UninstallDisplayIcon —— 守卫的选择器过期了"
    assert m.group(1).strip().endswith(exe), (
        f"UninstallDisplayIcon 指向 {m.group(1).strip()}，但产物是 {exe}"
    )


def test_installer_registers_and_unregisters_through_the_real_script():
    """
    `[Run]` 与 `[UninstallRun]` 都必须调**随包的那个 setup 脚本**，且：

      · 安装侧**不许带 `postinstall` 标志** —— 带该标志的条目在静默安装
        （`/SILENT`、`/VERYSILENT`）下会被 Inno **跳过**，而静默安装正是自动化验收
        走的路径。用了它 = 验收从没测过安装器自己的注册动作（等价于 Linux 侧
        `test_verify_simulates_the_real_install_path` 那条教训：只测组件、不测装配）。
      · 卸载侧**必须存在** —— 否则用户卸了软件，Claude Code 里还留着指向已删除文件的
        MCP 配置，每次开会话都报一次连接失败。
    """
    iss = _read(*(_WIN + ("installer.iss",)))
    sections: dict[str, str] = {}
    current = None
    for ln in iss.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s
            sections[current] = ""
        elif current:
            sections[current] += ln + "\n"

    run = sections.get("[Run]", "")
    unrun = sections.get("[UninstallRun]", "")
    assert "cc-computer-use-setup.ps1" in run, "[Run] 段没调随包的 setup 脚本"
    assert "cc-computer-use-setup.ps1" in unrun, (
        "[UninstallRun] 段没调 setup 脚本的 -Unregister —— 卸载不会摘掉注册项"
    )
    assert "-Unregister" in unrun, "[UninstallRun] 调了脚本但没传 -Unregister"

    # 判据取**会执行的行**：Flags 写在注释里不算。
    # 这里必须用 whole_line=True —— 见 _live_lines 的说明（`;` 在 .iss 里也是参数分隔符，
    # 按「截到第一个 `;`」处理会把 Flags 整段砍掉，断言就恒判通过了）。
    run_live = "\n".join(_live_lines(run, comment=";", whole_line=True))
    assert "Flags:" in run_live, (
        "[Run] 段里没有 Flags: —— 守卫的选择器过期了（下面那条会变成空转）"
    )
    assert "postinstall" not in run_live, (
        "[Run] 用了 postinstall 标志 —— 静默安装（/VERYSILENT）下它会被跳过，"
        "于是自动化验收永远测不到注册这一步"
    )


def test_installer_is_per_user():
    """
    必须是**仅当前用户**安装（`PrivilegesRequired=lowest` + `{localappdata}`）。

    理由不是口味：① 这个 server 必须跑在**用户自己的交互式会话**里才看得到桌面，
    per-machine 装法不带来任何好处；② per-machine 要管理员，而 UAC 弹窗没人点 ——
    自动化验收会直接卡死在那一步。
    """
    iss = _read(*(_WIN + ("installer.iss",)))
    live = "\n".join(_live_lines(iss, comment=";", whole_line=True))
    assert "PrivilegesRequired=lowest" in live, (
        "installer.iss 不是 lowest 权限 —— 装的时候会弹 UAC，静默验收走不通"
    )
    assert "localappdata" in live.lower(), (
        "installer.iss 的安装目录没有落在 {localappdata} 下 —— 用户级安装的意义就没了"
    )


# ==================== ④ setup 与 doctor 的 claude 候选路径必须同源同序 ====================

def _claude_candidates(src: str) -> list[str]:
    """从 .ps1 里抠出 claude 的候选路径列表（按出现顺序）。"""
    return re.findall(r"Join-Path \$env:(\w+) '([^']+)'", src)


@pytest.mark.parametrize("who", ["setup", "doctor"])
def test_claude_candidates_are_present(who):
    """正控：两份脚本都得真有一张候选表，否则下面的「同源同序」是空对空。"""
    name = "cc-computer-use-setup.ps1" if who == "setup" else "cc-computer-use-doctor.ps1"
    cands = _claude_candidates(_read(*(_WIN + ("payload", "bin", name))))
    assert cands, f"{name} 里没有 claude 候选路径 —— 这条守卫正在空转"


_GUEST_PS1 = (
    (_WIN + ("payload", "bin", "cc-computer-use-setup.ps1")),
    (_WIN + ("payload", "bin", "cc-computer-use-doctor.ps1")),
    (_WIN + ("verify-on-windows.ps1",)),
)


def _live_of(path: tuple) -> str:
    return "\n".join(_live_lines(_read(*path), comment="#"))


# ==================== ②claude 的 shell 前提（Git for Windows）====================

# ⚠️ 这一节守的是一条**真机实测出来的硬前提**，不是我们的口味：
# Claude Code 在 Windows 上要求有 Git for Windows(bash) 或 PowerShell 7，
# 两者都没有时连 `claude --version` 都被拒。系统自带的 Windows PowerShell 5.1 **不算**。
# 2026-09-29 实测：给子进程设上 `CLAUDE_CODE_GIT_BASH_PATH` 之后
# `mcp add / get / remove / list` 全部恢复（跑过完整 CRUD）。

@pytest.mark.parametrize("path", _GUEST_PS1, ids=lambda p: p[-1])
def test_guest_scripts_point_claude_at_a_bash(path):
    """三个脚本都要：找 bash → 在起 claude 的子进程上设 `CLAUDE_CODE_GIT_BASH_PATH`。

    为什么用这个环境变量而不是改 PATH：改 PATH 要等新进程才生效（计划任务拿到的
    仍是旧环境），而进程级环境变量当场生效、也不动用户的机器环境 —— 而且它就是
    Claude Code 自己报错里点名的那句话（「Or set CLAUDE_CODE_GIT_BASH_PATH …」）。
    """
    live = _live_of(path)
    name = path[-1]
    assert "function Find-GitBash" in live, (
        f"{name} 里没有 Find-GitBash —— 找不到 bash 就不知道 claude 为什么拒绝执行"
    )
    assert re.search(r"\$psi\.EnvironmentVariables\['CLAUDE_CODE_GIT_BASH_PATH'\]", live), (
        f"{name} 找到了 bash 却没把它交给 claude（没设 CLAUDE_CODE_GIT_BASH_PATH）—— "
        f"那样 claude 还是会以「requires either Git for Windows」拒绝执行"
    )


def test_setup_and_doctor_and_verify_search_the_same_git_bash_paths():
    """
    三份脚本的 bash 候选表必须**同源同序**。

    与 claude 候选表同一个理由：不一致就会出现「setup 说装不上、doctor 说环境好好的」
    这种互相打架的结论。三份是各自独立分发的副本（不互相 dot-source），只能靠守卫钉住。
    """

    def table(src: str) -> list[str]:
        # 候选项形如 (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
        m = re.search(r"function Find-GitBash.*?\n\}", src, re.S)
        assert m, "Find-GitBash 没找到 —— 守卫的选择器过期了"
        return re.findall(r"Join-Path [^\r\n]*?'([^']*bash\.exe)'", m.group(0))

    tables = {}
    for path in _GUEST_PS1:
        tables[path[-1]] = table(_read(*path))
    # 正控：每份都真有一张表，否则下面的比较是空对空
    for name, t in tables.items():
        assert len(t) >= 3, f"{name} 的 bash 候选表只有 {len(t)} 项 —— 守卫正在空转"
    first = next(iter(tables.values()))
    for name, t in tables.items():
        assert t == first, (
            f"{name} 的 bash 候选表与 setup 不一致：\n  setup: {first}\n  {name}: {t}"
        )


# ==================== ②b 客体 PS 5.1 能解析的语法 ====================

def test_guest_scripts_do_not_use_try_as_an_expression():
    """
    `(try { … } catch { … })` 是 **PowerShell 7 才有的语法**（把 try 当表达式用）。

    客体的 5.1 **连解析都过不了** —— 整份脚本一个字都不执行，而报错只指向那一行。
    2026-09-29 实测踩到：体检脚本用了这个形式，于是它在客体上完全跑不起来。

    赋值形式 `$x = try { … } catch { … }` 5.1 认，括号包起来的那种不认 —— 所以判据是
    「`(` 或 `=` 后面紧跟 `try`，且不是赋值」这个具体形状，不是笼统地禁 try。
    """
    for path in _GUEST_PS1:
        live = _live_of(path)
        m = re.search(r"\(\s*try\s*\{", live)
        assert not m, (
            f"{path[-1]} 里把 `try` 当表达式用了（第 "
            f"{live[:m.start()].count(chr(10)) + 1} 行附近）—— 那是 PS 7 语法，"
            f"客体的 PS 5.1 解析不了它，脚本会**一字不执行**地静默失败。"
            f"改成赋值形式：`$x = ''; try {{ $x = … }} catch {{ }}`"
        )


def test_guest_scripts_do_not_assign_to_powershell_automatic_variables():
    """
    别把函数参数命名成 `$Args` / `$Input` / `$Error` 这类 **PowerShell 自动变量**。

    2026-09-29 实测踩到：探针里写了 `param([string[]]$Args)`，而 `$Args` 是 PS 的自动
    变量 —— 参数被**静默忽略**，子进程拿到的命令行是空的，表现是「claude 报
    Input must be provided …」，看起来像 claude 坏了，排查方向完全跑偏。
    """
    reserved = {"Args", "Input", "Error", "Host", "PSCmdlet", "MyInvocation", "PSCommandPath"}
    for path in _GUEST_PS1:
        live = _live_of(path)
        for m in re.finditer(r"param\(([^)]*)\)", live):
            for name in re.findall(r"\$(\w+)", m.group(1)):
                assert name not in reserved, (
                    f"{path[-1]} 把参数命名成了自动变量 `${name}` —— "
                    f"PowerShell 会静默忽略它，参数根本传不进去"
                )


def test_guest_scripts_never_start_a_ps1_directly():
    """
    `Get-Command claude` 在 PowerShell 里返回的**通常就是** `claude.ps1`
    （npm 会装三个 shim：给 bash 的 `claude`、给 cmd 的 `claude.cmd`、给 PowerShell 的 `claude.ps1`），
    而 `Start-Process` **跑不了 `.ps1`**（报「不是有效的 Win32 应用程序」）。
    2026-09-29 真机实测踩到：注册与体检都卡在第一步，报错读起来像"claude 有问题"。

    修法是**换成同名的 `.cmd`**（实测那条路在交互式会话里可用），
    没有 `.cmd` 才退回 `powershell -File`。这条守卫要求那处分流还在。
    """
    for who in ("cc-computer-use-setup.ps1", "cc-computer-use-doctor.ps1",
                "verify-on-windows.ps1"):
        src = _read(*(_WIN + ("payload", "bin", who)) if who != "verify-on-windows.ps1"
                    else (_WIN + (who,)))
        live = "\n".join(_live_lines(src, comment="#"))
        assert re.search(r"EndsWith\('\.ps1'\)", live), (
            f"{who} 没有对 `.ps1` 分流 —— Start-Process 跑不了 .ps1，"
            f"而 Get-Command claude 返回的通常就是 claude.ps1（实测卡在这一步）"
        )
        assert "ChangeExtension" in live, (
            f"{who} 没有优先换成同名的 `.cmd` —— 实测 `.cmd` 那条路才真的能完成 mcp get/add"
        )


def _vars_bound_to_start_process_passthru(live: str) -> set[str]:
    """
    找出「从 `Start-Process … -PassThru` 接过返回值」的变量名。

    PS 里行尾的反引号是续行符，而客体脚本里 `$p = Start-Process … \\` 正是跨行的 ——
    先拼续行再找赋值，否则看不到后面那个 `-PassThru`。
    """
    joined = re.sub(r"`[ \t]*\r?\n[ \t]*", " ", live)
    names = set()
    for m in re.finditer(r"\$(\w+)\s*=\s*Start-Process\b[^\n]*", joined):
        if "-PassThru" in m.group(0):
            names.add(m.group(1))
    return names


def test_guest_scripts_do_not_rely_on_start_process_exit_code():
    """
    客体的 PowerShell 是**系统自带的 5.1**，它 `Start-Process -PassThru` 在带
    `-RedirectStandardOutput` 时 **`$p.ExitCode` 恒为空**（2026-09-29 实测：
    `done=True / rc-direct= / rc-after=`，补调无参 `WaitForExit()` 也没用）。

    这在本项目里是致命的：注册与体检的**全部判断**都建立在「`mcp get/add` 到底成没成」上，
    拿到空值就等于永远报失败 —— 或者更糟，把"已注册"误判成"没注册"从而反复去改用户配置。

    修法：一律用 `System.Diagnostics.Process` 直接起（退出码可靠，还能设 UTF-8 解码）。

    ⚠️ 判据是**「这个变量是不是从 `Start-Process -PassThru` 接过来的」**，不是
    「文件里出现了 `$p.ExitCode`」：改用 System.Diagnostics 之后 `$p` 就是一个普通的
    Process 对象，它的 `ExitCode` **完全可靠** —— 一刀切地禁掉字样会把**正确的写法**
    判成违规（本守卫第一版就是这么写的，2026-09-29 改完当场误杀一份好代码）。
    所以这里先算「被污染的变量名」，再只对它们下断言。
    """
    for who in ("cc-computer-use-setup.ps1", "cc-computer-use-doctor.ps1",
                "verify-on-windows.ps1"):
        src = _read(*(_WIN + ("payload", "bin", who)) if who != "verify-on-windows.ps1"
                    else (_WIN + (who,)))
        live = "\n".join(_live_lines(src, comment="#"))
        assert "System.Diagnostics.Process" in live, (
            f"{who} 还在用 Start-Process 取退出码 —— PS 5.1 下那个值恒为空"
        )
        tainted = _vars_bound_to_start_process_passthru(live)
        for name in sorted(tainted):
            assert not re.search(rf"\${name}\.ExitCode", live), (
                f"{who} 里 `${name}` 是从 `Start-Process -PassThru` 接过来的，"
                f"却读它的 `.ExitCode` —— PS 5.1 + 重定向下那个值恒为空。"
                f"改用 System.Diagnostics.Process。"
            )


def test_start_process_exit_code_guard_has_a_positive_control():
    """
    正控：上面那把尺子必须**真的量得到东西**。

    它只对「从 `Start-Process -PassThru` 接过来的变量」下断言，所以最危险的失效模式是
    **永远算不出任何变量名**（提取式过期、或续行没拼上）—— 那时它对任何代码都判通过，
    全绿但零覆盖（本仓库拆 mixin 那次的血证）。
    """
    sample = (
        "$p = Start-Process -FilePath $Exe -PassThru -NoNewWindow `\n"
        "    -RedirectStandardOutput $tmp\n"
        "$done = $p.WaitForExit(1000)\n"
        "if ($done -and $p.ExitCode -eq 0) { Ok }\n"
    )
    assert _vars_bound_to_start_process_passthru(sample) == {"p"}
    assert re.search(r"\$p\.ExitCode", sample)


def test_process_runners_default_to_a_nonzero_sentinel_on_timeout():
    """
    三个脚本各自那个 `Invoke-Exe`/`Invoke-Claude` 在**超时**时必须给出一个非 0 的哨兵值。

    .NET 的 `WaitForExit(毫秒)` 只回答"退没退出"，进程还活着时读 `ExitCode` 会直接抛
    `InvalidOperationException` —— 所以退出码**只能**在确实退出过时才读、且必须配一个
    兜底值。兜底值写 0 是比"读到空值"更隐蔽的同一类错误：一次卡死的自检/安装会被读成
    "通过"，而日志上看起来一切正常。（不写 0 这个要求就是这条守卫的全部内容。）
    """
    for who in ("cc-computer-use-setup.ps1", "cc-computer-use-doctor.ps1",
                "verify-on-windows.ps1"):
        src = _read(*(_WIN + ("payload", "bin", who)) if who != "verify-on-windows.ps1"
                    else (_WIN + (who,)))
        live = "\n".join(_live_lines(src, comment="#"))
        m = re.search(
            r"ExitCode\s*=\s*if\s*\(\s*\$finished\s*\)\s*\{\s*\[int\]\$p\.ExitCode\s*\}\s*"
            r"else\s*\{\s*(-?\d+)\s*\}",
            live,
        )
        assert m, (
            f"{who} 里没有「退出码只在进程确实退出时才读、否则给哨兵」这个形状 —— "
            f"守卫的选择器可能过期了（也可能是有人把 ExitCode 改成无条件读，那会抛异常）"
        )
        assert int(m.group(1)) != 0, (
            f"{who} 超时时把退出码兜底成 0 —— 卡死会被读成「成功」，比读到空值更隐蔽"
        )


def test_windows_verify_also_handles_the_ps1_shim():
    """验收脚本自己也要分流 —— 它同样经 `Get-Command` 找 claude（实测第一版就栽在这）。"""
    live = "\n".join(_live_lines(_read(*(_WIN + ("verify-on-windows.ps1",))), comment="#"))
    assert re.search(r"EndsWith\('\.ps1'\)", live), (
        "verify-on-windows.ps1 没有对 `.ps1` 分流 —— 验收会卡在第一步"
    )


def test_installer_deletes_runtime_files_on_uninstall():
    """
    卸载必须显式删 `{app}` 整棵树（`[UninstallDelete]`）。

    Inno 的卸载器只删**它自己装下去的**文件；运行期产生的东西（`setup.log`）它不认识、
    会留下 —— 结果是「卸载完目录还在、里面躺着一个日志文件」。
    2026-09-29 真机验收抓到：判据 `app-dir-removed` 报 FAIL，而安装/卸载本身都"成功"。
    """
    iss = _read(*(_WIN + ("installer.iss",)))
    sections: dict[str, str] = {}
    current = None
    for ln in iss.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s
            sections[current] = ""
        elif current:
            sections[current] += ln + "\n"
    blk = sections.get("[UninstallDelete]", "")
    assert blk.strip(), (
        "installer.iss 没有 [UninstallDelete] 段 —— 运行期文件（setup.log）会留下来，"
        "卸载后安装目录还在"
    )
    live = "\n".join(_live_lines(blk, comment=";", whole_line=True))
    assert re.search(r'Type:\s*filesandordirs\s*;\s*Name:\s*"\{app\}"', live), (
        "[UninstallDelete] 没有删 {app} 整棵树"
    )


def _iss_section(src: str, name: str) -> str:
    """取 `.iss` 里某个 `[段]` 的正文（**只认行首的段标题**，不认注释里提到的段名）。

    ⚠️ 不能用 `src.split("[段名]")` —— 注释里也常出现段名（本项目就爱在注释里讲
    「为什么这个段要这么写」），那样切出来的是说明文字，判据会对着注释下结论。
    """
    out: list[str] = []
    inside = False
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            inside = (s == name)
            continue
        if inside:
            out.append(ln)
    return "\n".join(out)


def test_installer_uninstallrun_has_run_once_id():
    """`[UninstallRun]` 必须带 `RunOnceId`（不给 Inno 会在编译期告警，且语义不明确）。"""
    iss = _read(*(_WIN + ("installer.iss",)))
    # ⚠️ 不能 `iss.split("[UninstallRun]", 1)` —— 注释里也出现过这个段名（讲设计理由那段），
    #    那样切出来的是注释，判据会看着一段说明文字下结论。
    blk = _iss_section(iss, "[UninstallRun]")
    live = "\n".join(_live_lines(blk, comment=";", whole_line=True))
    assert "RunOnceId:" in live, "[UninstallRun] 没有 RunOnceId"


def test_windows_verify_really_parses_the_payload_scripts_on_the_guest():
    """
    验收脚本必须**在客体上真解析一遍**随包的 .ps1，而不只靠静态守卫。

    2026-09-29 血证：体检脚本里用了 `(try { … } catch { … })`（PS 7 才有的写法），
    客体上连解析都过不了 —— 整份脚本一个字都不执行，报错消失在隐藏窗口里，
    表现只是「体检跑不起来」。静态守卫只能拦已知的那几种写法；在目标机器上真解析一遍
    才是对**这一台**的确定回答，成本只有几毫秒。

    ⚠️ 判据取「有没有真调 Parser::ParseFile」，不是「有没有提到解析」：注释里提一句
    挡不住任何事（本仓库的常规纪律）。
    """
    live = _live_of(_WIN + ("verify-on-windows.ps1",))
    assert re.search(r"Parser\]::ParseFile\(", live), (
        "verify-on-windows.ps1 没有在客体上真解析随包的 .ps1 —— "
        "PS 7 才有的写法会让脚本一字不执行，而现象只是「跑不起来」"
    )
    assert re.search(r"Check\s+'scripts-parse-on-ps51'", live), (
        "解析结果没有变成一条 CHECK 判据（解析失败会被静默放过）"
    )


def test_claude_health_check_subcommands_get_a_generous_timeout():
    """
    `claude mcp get` / `mcp list` / `mcp add` **不是纯读配置** —— 它们会真的把 MCP server
    拉起来做健康检查（那句 `Status: √ Connected` 就是这么来的）。而我们的 server 是个
    250MB 的冻结产物，**首次冷启动**实测会超过 30 秒。

    2026-09-29 真机踩到：一次验收里 `mcp-get` 打印了**完整正确**的输出，进程却还没退出，
    于是 30 秒的超时把它判成失败 —— 假红，方向最坏的那种（会让人去改本来正确的产物）。
    实测热态 6~12 秒、冷态可能 30 秒以上，所以这类子命令给 120 秒。

    ⚠️ 判据是**每个调用点上真的传了多少秒**，不是「文件里有个大数字」：
    超时写小了不会有任何报错，只会偶发假红 —— 正是必须用测试钉住的那一类。
    """
    threshold = 120
    for path in _GUEST_PS1:
        name = path[-1]
        joined = _joined(_read(*path), comment="#")
        checked = 0
        for m in re.finditer(r"@\('mcp',\s*'(get|list|add)'[^\n]*?-TimeoutSec\s*(\d+)", joined):
            checked += 1
            got = int(m.group(2))
            assert got >= threshold, (
                f"{name} 里 `claude mcp {m.group(1)}` 的超时只有 {got}s —— "
                f"这个子命令会冷启动 250MB 的冻结产物做健康检查，实测能超过 30s。"
                f"给到 {threshold}s：超时只在该卡住时才付出代价，改小换来的是偶发假红。"
            )
        # 正控：这份文件确实有这类调用点，否则上面等于什么都没扫
        if name == "verify-on-windows.ps1":
            assert checked >= 2, (
                f"{name} 里只扫到 {checked} 个健康检查类调用点 —— 守卫的选择器过期了"
            )


def test_setup_and_doctor_search_the_same_claude_paths_in_the_same_order():
    """
    两边的候选表必须**同源同序**。

    不一致的后果是**看起来互相打架的结论**：setup 说「已注册」而 doctor 说「找不到
    claude」—— 用户没法判断该信谁。Linux 侧有同款守卫
    （`test_doctor_searches_the_same_claude_locations_as_setup`）。

    注意两边还共用同一个前缀（`Get-Command claude`），那条在下面单列。
    """
    setup = _claude_candidates(_read(*(_WIN + ("payload", "bin", "cc-computer-use-setup.ps1"))))
    doctor = _claude_candidates(_read(*(_WIN + ("payload", "bin", "cc-computer-use-doctor.ps1"))))
    assert setup == doctor, (
        f"setup 与 doctor 的 claude 候选表不一致：\n  setup : {setup}\n  doctor: {doctor}\n"
        f"会出现「setup 说注册好了、doctor 说找不到 claude」这种互相打架的结论。"
    )


# ==================== ⑤ 宿主总入口的几条硬约束 ====================


def test_build_windows_sh_does_not_wipe_the_whole_dist():
    """
    **不许 `rm -rf dist`**，只许删自己的产物。

    `dist/` 现在同时住着 `build.sh` 的（`computer-use-mcp-bin/` + wrapper）、容器构建的
    （`.deb` + 组装目录）与本脚本的（`windows/`）。整目录清掉会顺手删掉用户不预期的东西，
    而那是**几十 MB 到几百 MB 的分发产物**。Linux 侧 `build.sh` 有同款守卫（M-45 那条）。
    """
    live = "\n".join(_live_lines(_read(*(_WIN + ("build-windows.sh",)))))
    assert not re.search(r"rm\s+-rf?\s+[^\n]*\bdist\b(?!/)", live), (
        "build-windows.sh 里有 `rm -rf dist` —— 会抹掉 .deb 与组装目录，"
        "用户不会预期「跑一次 Windows 构建」会顺手删掉它们"
    )
    assert "dist/windows" in live, (
        "build-windows.sh 没有提到 dist/windows —— 产物落点跑了，或者守卫的选择器过期了"
    )


def test_build_windows_sh_never_reads_a_password_from_a_file():
    """
    客体密码必须**只来自调用者的环境**，不许落到仓库里。

    判据有三条，缺一不可：
      ① 必须显式要求 `CC_CU_VM_PASS` 非空（否则会去用空串连，报错是
         "user was not able to logon on guest" —— 读起来像密码错，实际是变量空，
         本项目的运维笔记里专门记过这条）；
      ② 每次 `guestcontrol run` 都要**当场带上 `--password`**（guestcontrol 不继承
         调用者的环境，漏一次就是一次白等）；
      ③ 源码里**不许出现任何像密码的字面量**。
    """
    src = _read(*(_WIN + ("build-windows.sh",)))
    live = "\n".join(_live_lines(src))
    # ⚠️ 必须拼续行：`guestcontrol ... run \` 与 `--password ...` 在源码里是**两行**，
    #    只看单行的话断言恒失败（第一版就是这么误报的）。
    joined = _joined(src)

    assert re.search(r'VM_PASS="\$\{CC_CU_VM_PASS:-\}"', live), (
        "build-windows.sh 没有从 CC_CU_VM_PASS 取密码（或给了默认值）"
    )
    assert re.search(r'\[\[\s*-n\s+"\$VM_PASS"\s*\]\]', live), (
        "build-windows.sh 没有在开跑前断言密码非空 —— 空串会报成「登录失败」，极难查"
    )
    # 每个 guestcontrol run 都带 --password（正控：确实有 guestcontrol run 这一行）
    gcs = [ln for ln in joined.splitlines() if "guestcontrol" in ln and " run " in ln]
    assert gcs, "build-windows.sh 里找不到 guestcontrol run —— 守卫的选择器过期了"
    for ln in gcs:
        assert "--password" in ln, f"这一处 guestcontrol 没带 --password：{ln.strip()}"

    # ③ 不许有硬编码密码
    assert "jitaafei" not in src, "build-windows.sh 里出现了客体密码字面量"


def test_password_is_not_hardcoded_anywhere_under_packaging_windows():
    """
    整棵 `packaging/windows/` 与这轮的安装脚本都扫一遍。

    与上面那条分开写，是因为上面只扫总入口 —— 而密码最容易被顺手写进
    **验收脚本**或**文档里的示例命令**（「照着敲一遍」时最方便）。
    """
    hits: list[str] = []
    for root, _dirs, files in os.walk(os.path.join(REPO, *_WIN)):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            if "jitaafei" in text:
                hits.append(os.path.relpath(p, REPO))
    assert not hits, f"以下文件里出现了客体密码字面量：{hits}"


# ==================== ⑥ setup 脚本的两条行为约束 ====================


def test_setup_reports_outcome_by_exit_code_not_by_optimism():
    """
    注册结果必须**按退出码说话**。

    反例是 Linux 侧 `postinst` 踩过的坑：`cmd || true` 之后无条件打印「已注册」——
    用户看到成功、实际什么都没发生，而且**没有任何痕迹**能推翻那句话。
    这里的判据是控制流：`Add` 的结果必须被 `if` 分开处理，且失败分支里要给出
    **可以直接复制**的手工命令。
    """
    src = _read(*(_WIN + ("payload", "bin", "cc-computer-use-setup.ps1")))
    live = "\n".join(_live_lines(src, comment="#"))

    assert re.search(r"if\s*\(\s*\$add\.ExitCode\s+-eq\s+0\s*\)", live), (
        "setup 没有按 `claude mcp add` 的退出码分支 —— 会退化成「无条件报成功」"
    )
    assert "mcp add --scope user" in live, (
        "setup 的失败分支里没有给出可复制的手工注册命令 —— 失败时用户无从下手"
    )
    # 正控：脚本里确实有「注册成功」这句话会被打印，否则上面两条都可能是在测空气
    assert "Write-Changed" in live and "已注册到 Claude Code" in src, (
        "setup 里根本没有「已注册」这句话 —— 这条守卫的选择器过期了"
    )


def test_setup_is_idempotent_and_has_unregister():
    """
    幂等（已注册就**只报告不改**）+ 提供 `-Unregister`（卸载器的钩子）。

    幂等的判据取「先 `mcp get` 探一次」这个**动作**，而不是某句话：
    直接 `mcp add` 也能"成功"（Claude Code 会覆盖或报重复），但那不是幂等，
    而是每次都改动用户配置。
    """
    src = _read(*(_WIN + ("payload", "bin", "cc-computer-use-setup.ps1")))
    live = "\n".join(_live_lines(src, comment="#"))

    assert re.search(r"@\('mcp',\s*'get',\s*\$McpName\)", live), (
        "setup 注册前没有先 `mcp get` 探一次 —— 不幂等，每次都会动用户配置"
    )
    assert "already-registered" in live, (
        "setup 没有「已存在、保持不动」这条路径"
    )
    assert re.search(r"if\s*\(\s*\$Unregister\s*\)", live), "setup 没有 -Unregister 分支"
    assert "-Unregister" in src, "setup 的用法说明里没写 -Unregister（安装器要调它）"


def test_setup_recognizes_the_shell_prerequisite_error():
    """
    必须识别「Claude Code 拒绝执行」那条报错，并把它翻译成**可操作**的指引。

    ⚠️ 这条守卫在 2026-09-29 被**反着改过一次**，原因值得记住：
    原先它守的是「点破这不是缺 Git，别照字面去装」。那个归因是**错的** ——
    真机隔离实测（把 Git 单独加上 PATH、把 `CLAUDE_CODE_GIT_BASH_PATH` 单独设上，
    逐个变量试）证明：那台机器上确实既没有 Git for Windows 也没有 PowerShell 7，
    而 Claude Code 就是要其中一个；装上之后在**同一个**（非交互的）上下文里立刻正常。
    所以正确的行为是**告诉用户缺什么、去哪装**，不是叫用户别装。

    判据取**会执行的那行判断**，不是「文件里出现过这句话」：这句话在脚本开头的
    说明性注释里也有一份。只查全文的话，谁把运行期的判断删了、注释留着，守卫照样绿。
    """
    src = _read(*(_WIN + ("payload", "bin", "cc-computer-use-setup.ps1")))
    live = "\n".join(_live_lines(src, comment="#"))
    assert re.search(r"-match\s+'requires either Git for Windows'", live), (
        "setup 里没有**运行期**识别「Claude Code 拒绝执行」那条报错（注释里提过不算）"
    )
    # 判据是「**那个翻译函数**有没有把用户指向安装地址」：翻译成人话还不够，
    # 得让用户知道下一步做什么。
    # ⚠️ 必须把范围**收进 Show-ShellHintIfNeeded**：脚本别处（注册前那条前置提示）
    #    也印了一次地址，按全文找的话，把翻译函数里的地址删掉照样能过 —— 实测种这个反例时发现。
    m = re.search(r"function Show-ShellHintIfNeeded\s*\{(.*?)\n\}", live, re.S)
    assert m, "找不到 Show-ShellHintIfNeeded —— 这条守卫的选择器过期了"
    body = m.group(1)
    assert re.search(r"git-scm\.com/downloads/win", body), (
        "Show-ShellHintIfNeeded 认出了那条报错，却没给出 Git for Windows 的下载地址 —— "
        "用户只知道「缺个东西」，不知道去哪装"
    )
    # 正控：这句话确实在注释里也有一份，否则上面那条判据的「注释除外」说明就是多余的
    assert "requires either Git for Windows" in src, (
        "源码里连这句话都没有 —— 这条守卫的选择器过期了"
    )


def test_setup_does_not_claim_the_shell_error_is_a_context_artifact():
    """
    反向：不许再把那条报错归因成「非交互上下文导致的」并劝用户别装。

    这是上一版**写错过的结论**，而且错得有代价 —— 它让人绕远路去查反方向。
    注释里留着解释「为什么改」是好的（那也是本仓库的惯例），但**运行期文案**里
    不能再出现这套误导。

    ⚠️ 判据收进 `Show-ShellHintIfNeeded` 的函数体：整份脚本里别处提到「交互式会话」
    是合法的（安装器 [Run] 与计划任务的确都在交互式会话里跑），按全文找会误伤。
    """
    live = _live_of(_WIN + ("payload", "bin", "cc-computer-use-setup.ps1"))
    m = re.search(r"function Show-ShellHintIfNeeded\s*\{(.*?)\n\}", live, re.S)
    assert m, "找不到 Show-ShellHintIfNeeded —— 这条守卫的选择器过期了"
    body = m.group(1)
    for bad in ("不是", "白折腾", "别去装"):
        assert bad not in body, (
            f"Show-ShellHintIfNeeded 里又出现了 {bad!r} —— 那套「报错像是缺 Git，"
            f"但不是」的归因已被真机实测否证，会把用户带偏"
        )
    # 正控：这个函数体是**有内容**的，否则上面等于在空串里找东西
    assert "requires either Git for Windows" in body, (
        "Show-ShellHintIfNeeded 的函数体没抓到 —— 守卫正在空转"
    )
