#Requires -Version 5.1
<#
  在 Windows 客体上构建分发产物：PyInstaller 冻结产物 + Inno Setup 安装包。

  ═══ 为什么是这样一个脚本 ═══

  · **PyInstaller 不能交叉编译** —— 只能在 Windows 上跑，所以这一步必须发生在客体里。
    宿主侧的 `build-windows.sh` 只负责驱动（送工具进去、调本脚本、取产物回来）。
  · **构建不在共享目录（Z:）里做**：PyInstaller 要遍历上万个文件、反复 stat/read，
    VirtualBox 共享目录的 I/O 开销会让构建慢到无法忍受。先把源码拷到本地磁盘再构建。
  · 产物**写回共享目录**（`Z:\...\dist\windows`），宿主立刻可见，不需要 guestcontrol copyto。

  ═══ 输出与编码（这一条不是风格问题，是信息丢失问题）═══

  本脚本的**中文诊断信息全部写进 UTF-8 日志文件**（`<Out>\build.log`），
  stdout 上只留 ASCII 的一行行进度。原因：客体是中文 Windows，PowerShell/cmd 往
  **管道**里写的是 OEM 代码页（GBK/cp936），经 `VBoxManage guestcontrol --wait-stdout`
  读回来必然是乱码 —— 本项目已经在 `server.py::_configure_stdio` 上栽过一次
  （冻结产物自检最后一行 `print("✅ OK")` 直接 UnicodeEncodeError 崩掉）。
  日志落盘走 UTF-8，宿主直接读，没有这一层损耗。

  ═══ 用法（在客体上）═══

      powershell -NoProfile -ExecutionPolicy Bypass `
        -File Z:\cc-computer-use\packaging\windows\build-on-windows.ps1

  退出码：0 = 成功；非 0 = 失败（失败原因在日志里，stdout 上只有一行 FAILED）。
#>
[CmdletBinding()]
param(
    # 仓库在客体上的根（VirtualBox 共享目录）
    [string]$RepoRoot = 'Z:\cc-computer-use',

    # 客体上的 Python：项目地板是 3.12，客体那份装在 C:\Users\lf\py312
    [string]$Python = 'C:\Users\lf\py312\python.exe',

    # 本地构建工作目录（**必须在本地磁盘**，见文件头）
    [string]$Work = 'C:\Users\lf\build\cc-cu',

    # 产物输出目录，默认落回共享目录，宿主立即可见
    [string]$Out = '',

    # 版本号（宿主侧从 pyproject.toml 解析后经 -Version 传入，这里是兜底默认值）
    [string]$Version = '0.1.0',

    # ISCC.exe（Inno Setup 编译器）路径；留空则按候选表自动探测
    [string]$Iscc = '',

    # 跳过打安装包，只构建冻结产物（调试冻结问题时用，能省掉压缩那几分钟）
    [switch]$SkipInstaller,

    # 只探测 Inno Setup 编译器在不在，打印一行 `ISCC=<路径|NONE>` 就退出。
    # 宿主侧 `build-windows.sh` 用它来决定要不要先把编译器送进来再构建。
    [switch]$ProbeOnly
)

$ErrorActionPreference = 'Stop'

function Find-Iscc {
    <#
      Inno Setup 编译器的候选表 —— **全项目只有这一处**。
      宿主侧脚本刻意不自己拼一份路径来 "if exist"：一是会漂，二是经
      `VBoxManage guestcontrol` 传内联 cmd 命令时，`if exist "带空格的路径"` 里的
      引号会被吃掉，判据变成拿半截路径去存在性检查 —— **恒假且不报错**
      （实测：明明装好了，探测却说没装）。让客体脚本自己判断、只回一行 ASCII，
      这类引号问题就从根上不存在了。
    #>
    $candidates = @()
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe') }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA 'InnoSetup6\ISCC.exe') }
    $candidates += 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
    $candidates += 'C:\Program Files\Inno Setup 6\ISCC.exe'
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

if ($ProbeOnly) {
    # ⚠️ 只打印这一行、别的什么都不打：宿主用正则抓 `ISCC=...`，
    #    多打内容只会让判据变脆。输出必须 ASCII（客体是中文 Windows）。
    $found = Find-Iscc
    if ($found) { Write-Host "ISCC=$found" } else { Write-Host 'ISCC=NONE' }
    exit 0
}

if (-not $Out) { $Out = Join-Path $RepoRoot 'dist\windows' }

$script:LogPath = Join-Path $Out 'build.log'

# ─────────────────────────── 日志 ───────────────────────────

function Write-Log {
    <#
      中文诊断写进 UTF-8 日志文件；`-Encoding UTF8` 不能省 ——
      PS 5.1 的 Add-Content 默认编码是 ANSI，中文会按 GBK 落盘，
      而宿主读的时候按 UTF-8 解，照样是乱码。
    #>
    param([string]$Message, [string]$Level = 'INFO')
    $line = '[{0}] {1,-5} {2}' -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message
    Add-Content -Path $script:LogPath -Value $line -Encoding UTF8
}

function Write-Step {
    <#
      stdout 上的进度：**只允许 ASCII**（理由见文件头）。中文版同步进日志。
      两行内容刻意一致，只是为了各自能读。
    #>
    param([string]$Ascii, [string]$Chinese)
    Write-Host $Ascii
    Write-Log $Chinese
}

function Fail {
    param([string]$Message)
    Write-Log $Message 'ERROR'
    Write-Host 'FAILED (see build.log)'
    exit 1
}

# ─────────────────────────── 前置检查 ───────────────────────────

New-Item -ItemType Directory -Force -Path $Out | Out-Null
# 每次构建重开日志：混着上一次的记录会让"这次到底哪一步failed"变得难查
Set-Content -Path $script:LogPath -Value '' -Encoding UTF8

# ⚠️ **兜底 trap：任何未捕获的异常都必须留下原因**。
#    2026-09-29 实测踩到：一次 Start-Process 的参数错（两个重定向指向同一个文件）
#    让脚本直接退出码 33，而日志**停在「第 4 步」、一个字的原因都没有** ——
#    因为错发生在 PowerShell 自己身上，还没轮到我们写日志。排查这种"静默死亡"
#    只能靠把异常本身记下来。以后所有未预料的失败都走这里。
trap {
    $msg = "未捕获的异常：{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    Write-Log $msg 'ERROR'
    if ($_.ScriptStackTrace) { Write-Log ("栈：" + $_.ScriptStackTrace) 'ERROR' }
    Write-Host 'FAILED (see build.log)'
    exit 1
}

Write-Log ('=' * 70)
Write-Log "cc-computer-use Windows 构建  version=$Version"
Write-Log "RepoRoot = $RepoRoot"
Write-Log "Python   = $Python"
Write-Log "Work     = $Work"
Write-Log "Out      = $Out"
Write-Log ('=' * 70)

if (-not (Test-Path $Python)) { Fail "找不到 Python: $Python" }
if (-not (Test-Path (Join-Path $RepoRoot 'entry.py'))) { Fail "RepoRoot 看着不对，里面没有 entry.py: $RepoRoot" }

$pyVersion = (& $Python --version 2>&1) -join ' '
Write-Log "客体 Python: $pyVersion"

# 依赖检查：缺哪个包会在 PyInstaller 后半程才失败，先在这里挡住
$needProbe = @'
import importlib.util as u, sys
need = ["mcp", "uiautomation", "comtypes", "mss", "PIL", "winrt", "PyInstaller"]
missing = [n for n in need if u.find_spec(n) is None]
print("MISSING:" + ",".join(missing) if missing else "OK")
'@
$probeFile = Join-Path $env:TEMP 'cc_cu_dep_probe.py'
Set-Content -Path $probeFile -Value $needProbe -Encoding ASCII
$depOut = (& $Python $probeFile 2>&1) -join ' '
if ($depOut -notmatch '^OK') {
    Fail "客体缺依赖，先补装再构建：$depOut`n（参考 packaging/windows/README.md 的 pip 命令）"
}
Write-Log "依赖检查通过：mcp / uiautomation / comtypes / mss / PIL / winrt / PyInstaller 都在"

# ─────────────────────── 第 1 步：拷源码到本地磁盘 ───────────────────────

Write-Step '[1/6] copying source tree to local disk' '第 1 步：把源码拷到本地磁盘（不在共享目录里构建）'

if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
$tree = Join-Path $Work 'tree'
New-Item -ItemType Directory -Force -Path $tree | Out-Null

Copy-Item -Recurse -Force (Join-Path $RepoRoot 'src') $tree
Copy-Item -Force (Join-Path $RepoRoot 'entry.py') $tree
Write-Log "源码已就位: $tree（未复制 tests/ 与 docs/ —— 冻结产物用不到，白拷几千个文件）"

# ─────────────────────── 第 2 步：PyInstaller ───────────────────────

Write-Step '[2/6] running PyInstaller (onedir)' '第 2 步：调用 PyInstaller 构建 onedir 产物'

# ⚠️ 开关的取舍，逐条都有理由：
#
#   --onedir 而非 --onefile：与 Linux 侧 build.sh 同理由 —— onefile 每次启动都要把
#     自己解压到临时目录（秒级冷启动开销），onedir 直接跑目录里的可执行文件。
#
#   --collect-all uiautomation：**必须**。该包在 `uiautomation/bin/` 下带着两个
#     UIAutomationClient 的 proxy/stub DLL（跨位数时才用），它们不是 .py，
#     不做 collect 不会进产物；而 uiautomation 在导入时按 `__file__` 的兄弟目录找它们。
#
#   --collect-submodules comtypes + --hidden-import comtypes.gen：**必须**。
#     uiautomation 在运行时调 `comtypes.client.GetModule("UIAutomationCore.dll")`
#     —— 那是**运行时生成并 import 一个 Python 模块**，静态分析根本看不到它。
#     这是本产物最大的风险点，也是第 3 步要实测 selftest 的原因。
#
#   --collect-all winrt：PyWinRT 的投影包（winrt-Windows.Media.Ocr 等）是分开发行的，
#     每个都往 `winrt/windows/...` 下放子模块与 `_winrt` 扩展；逐个人工列举容易漏，
#     直接整包收集。缺了它的表现是「开发态 OCR 正常、只有冻结产物里报缺包」。
#
#   --exclude-module gi / Xlib：backend 工厂 `get_backend()` 里的 Linux 分支
#     （`from .linux.backend import LinuxBackend`）会被 PyInstaller 的静态分析跟进去，
#     那是**函数级 import 也照样收集**的；客体上没有 gi/Xlib，不显式排除就是
#     一堆 "missing module" 噪声，且可能在产物里留下半截 Linux 后端。
#
#   --copy-metadata：这些库用 importlib.metadata 读自己的版本，不带 dist-info 就会
#     在运行时抛 PackageNotFoundError（Linux 侧 build.sh 同款处理）。
$pyiArgs = @(
    '--onedir'
    '--name', 'computer-use-mcp-bin'
    '--paths', 'src'
    '--collect-submodules', 'computer_use_mcp'
    '--collect-submodules', 'mcp.server'
    '--collect-all', 'uiautomation'
    '--collect-submodules', 'comtypes'
    '--hidden-import', 'comtypes.gen'
    '--collect-all', 'winrt'
    '--hidden-import', 'cryptography'
    '--hidden-import', 'cryptography.hazmat.backends.openssl'
    '--copy-metadata', 'mcp'
    '--copy-metadata', 'mcp-types'
    '--copy-metadata', 'pydantic'
    '--copy-metadata', 'anyio'
    '--copy-metadata', 'cryptography'
    '--copy-metadata', 'comtypes'
    '--copy-metadata', 'uiautomation'
    '--copy-metadata', 'winrt-runtime'
    '--exclude-module', 'gi'
    '--exclude-module', 'Xlib'
    '--exclude-module', 'mcp.cli'
    '--exclude-module', 'typer'
    '--exclude-module', 'tkinter'
    'entry.py'
)

$pyiOut = Join-Path $Work 'pyinstaller.out.log'
$pyiErr = Join-Path $Work 'pyinstaller.err.log'
$sw = [Diagnostics.Stopwatch]::StartNew()
$proc = Start-Process -FilePath $Python -ArgumentList (@('-m', 'PyInstaller') + $pyiArgs) `
    -WorkingDirectory $tree -Wait -PassThru -NoNewWindow `
    -RedirectStandardOutput $pyiOut -RedirectStandardError $pyiErr
$sw.Stop()
Write-Log ("PyInstaller 退出码 = {0}，耗时 {1:N1}s" -f $proc.ExitCode, $sw.Elapsed.TotalSeconds)

# PyInstaller 的完整输出并进日志（它是诊断"哪个包没被收进来"的唯一依据）
foreach ($f in @($pyiOut, $pyiErr)) {
    if (Test-Path $f) {
        Add-Content -Path $script:LogPath -Value ("--- {0} ---" -f (Split-Path $f -Leaf)) -Encoding UTF8
        Get-Content $f -Encoding UTF8 | Add-Content -Path $script:LogPath -Encoding UTF8
    }
}
if ($proc.ExitCode -ne 0) { Fail "PyInstaller 失败（退出码 $($proc.ExitCode)），细节见日志里的 pyinstaller.*.log 段" }

$dist = Join-Path $tree 'dist\computer-use-mcp-bin'
$exe = Join-Path $dist 'computer-use-mcp-bin.exe'
if (-not (Test-Path $exe)) { Fail "PyInstaller 说成功了，但找不到可执行文件: $exe" }

# ─────────────────── 第 3 步：校验产物里真有那几个动态包 ───────────────────

Write-Step '[3/6] verifying frozen payload' '第 3 步：校验冻结产物里真的收进了运行时动态依赖'

# 这一节是「判据必须外部可观测」的落点：不看 PyInstaller 说自己成功了，
# 而是去看产物里**实际有什么**。
#
# ⚠️ 这里有个**必须分清的两类落点**（2026-09-29 实测踩到，浪费了一轮构建）：
#   · **纯 .py 的包**（mss / mcp / comtypes / 大部分 winrt）被 PyInstaller 打包进
#     **`PYZ.pyz` 归档**（内嵌在 exe 里），磁盘上**根本没有同名目录** ——
#     只留一个 `<包名>-<版本>.dist-info\` 在 `_internal\` 下。
#   · **带二进制/数据文件的包**（uiautomation 的 proxy DLL、winrt 的 `_winrt*.pyd`）
#     没法进 PYZ，才会被摊到 `_internal\<包名>\` 下。
# 第一版校验只查了「目录在不在」，于是对前者**恒判缺失**、直接误杀了整个构建 ——
# 而真实产物是完好的（`PYZ` 里 comtypes/mcp/mss 一个不少）。这类"假红"比漏检更浪费时间，
# 因为它会把人引去改本来正确的收集规则。
# 所以这里改成两条都用**权威来源**：归档清单 + 磁盘散件，各查各的。
$internal = Join-Path $dist '_internal'

# ---- 3a. 归档清单：模块是否真的在 PYZ 里 ----
$viewer = Join-Path $Work 'archive_list.txt'
$procA = Start-Process -FilePath $Python `
    -ArgumentList @('-m', 'PyInstaller.utils.cliutils.archive_viewer', '-r', '-b', $exe) `
    -Wait -PassThru -NoNewWindow `
    -RedirectStandardOutput $viewer -RedirectStandardError (Join-Path $Work 'archive_list.err.txt')
if ($procA.ExitCode -ne 0 -or -not (Test-Path $viewer)) {
    Fail "pyi-archive_viewer 失败（rc=$($procA.ExitCode)）—— 没法核实冻结产物的内容"
}
# 归档清单的格式：CArchive 段不带缩进，PYZ 段每行**带一个前导空格**。
# 统一 trim 后按整行精确比对（不用 -match，那会退化成子串匹配：
# `mcp` 能匹配到 `mcp_types`，`mss` 能匹配到 `mssql` 之类）。
$entries = @{}
foreach ($ln in Get-Content $viewer -Encoding UTF8) {
    $t = $ln.Trim()
    if ($t) { $entries[$t] = $true }
}
Write-Log "归档清单读到 $($entries.Count) 个条目"

# 这些是**必须能在归档里按模块名找到**的（纯 Python 依赖的主体）。
# 注意 winrt 的类（如 Windows.Media.Ocr 的 OcrEngine）不是模块名，别写进来 ——
# 按模块名比对时它们永远找不到，那是"假红"。
$mustBeInArchive = @(
    @{ Mod = 'comtypes'; Why = 'COM 绑定（GetModule 运行时生成代码的宿主）' }
    @{ Mod = 'comtypes.client'; Why = 'GetModule 本体（uiautomation 在运行时调它）' }
    @{ Mod = 'comtypes.gen'; Why = 'GetModule 生成的模块所落的包（--hidden-import 收的）' }
    @{ Mod = 'mss'; Why = '抓屏' }
    @{ Mod = 'mcp'; Why = 'MCP SDK' }
    @{ Mod = 'mcp.server'; Why = 'stdio 传输那一层' }
    @{ Mod = 'uiautomation'; Why = 'UIA 驱动本体' }
    @{ Mod = 'PIL'; Why = 'Pillow（图像编码）' }
    @{ Mod = 'cryptography'; Why = 'MCP 的依赖' }
    @{ Mod = 'winrt.windows.media.ocr'; Why = 'Windows.Media.Ocr 的 Python 投影' }
    @{ Mod = 'winrt.windows.graphics.imaging'; Why = 'SoftwareBitmap（OCR 的输入类型）' }
    @{ Mod = 'computer_use_mcp.backend.windows.backend'; Why = '本项目自己的 Windows 后端' }
)
$missing = @()
foreach ($item in $mustBeInArchive) {
    if ($entries.ContainsKey($item.Mod)) {
        Write-Log "  ✅ PYZ $($item.Mod)  ($($item.Why))"
    }
    else {
        Write-Log "  ❌ PYZ 里没有 $($item.Mod)  ($($item.Why))" 'ERROR'
        $missing += $item.Mod
    }
}

# ---- 3b. 磁盘散件：带二进制的包必须摊在 _internal\ 下 ----
# 这两条是「收不进来就整个功能消失」的：
#   · uiautomation 的 proxy/stub DLL —— 它按 `__file__` 的兄弟目录找，必须在真磁盘上；
#   · PyWinRT 的 `_winrt*.pyd` —— Windows.Media.Ocr 真正的实现入口。
$mustBeOnDisk = @(
    @{ Path = 'uiautomation\bin\UIAutomationClient_VC140_X64.dll'; Why = 'UIA 的 proxy/stub DLL（X64）' }
    @{ Path = 'winrt\_winrt.cp312-win_amd64.pyd'; Why = 'PyWinRT 的运行时扩展' }
    @{ Path = 'winrt\_winrt_windows_media_ocr.cp312-win_amd64.pyd'; Why = 'Windows.Media.Ocr 的扩展' }
    @{ Path = 'winrt\windows\media\ocr\__init__.py'; Why = 'OCR 的 Python 投影层' }
)
foreach ($item in $mustBeOnDisk) {
    $p = Join-Path $internal $item.Path
    if (Test-Path $p) {
        Write-Log "  ✅ _internal\$($item.Path)  ($($item.Why))"
    }
    else {
        Write-Log "  ❌ 磁盘上缺 _internal\$($item.Path)  ($($item.Why))" 'ERROR'
        $missing += $item.Path
    }
}

if ($missing.Count -gt 0) {
    Fail ("冻结产物缺关键依赖：" + ($missing -join ', ') +
        "（构建机上的包版本与这里的收集规则可能对不上）")
}

$sizeMB = [math]::Round(((Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Log "冻结产物体积 = $sizeMB MB"

# ─────────────────── 第 4 步：组装安装包输入目录 ───────────────────

Write-Step '[4/6] assembling payload directory' '第 4 步：组装安装包输入目录（payload）'

$payload = Join-Path $Out 'payload'
if (Test-Path $payload) { Remove-Item -Recurse -Force $payload }
New-Item -ItemType Directory -Force -Path (Join-Path $payload 'server') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $payload 'bin') | Out-Null

# ⚠️ 复制的是 onedir 目录的**内容**（`\*`）而不是目录本身：
#    `.iss` 里 `{app}\server` 直接映射这里，PyInstaller 的 exe 与 _internal/
#    必须是**平级兄弟**。套一层会让 server\computer-use-mcp-bin\computer-use-mcp-bin.exe，
#    那是 Linux 侧 assemble.sh 专门写过注释提醒过的同一个坑。
Copy-Item -Recurse -Force (Join-Path $dist '*') (Join-Path $payload 'server')

$payloadSrc = Join-Path $RepoRoot 'packaging\windows\payload'
Copy-Item -Recurse -Force (Join-Path $payloadSrc 'bin\*') (Join-Path $payload 'bin')
Copy-Item -Force (Join-Path $payloadSrc 'README.txt') (Join-Path $payload 'README.txt')

# ⚠️ LICENSE 单一来源：仓库根那份是权威。**不要**在 packaging/windows/payload/ 里
#    再放一份 —— 两份 MIT 文本漂移起来没人会发现，而它是要随安装包发出去的。
Copy-Item -Force (Join-Path $RepoRoot 'LICENSE') (Join-Path $payload 'LICENSE')

# 第三方许可清单**现场生成**（不用仓库里的静态文件）：产物里装的是整个 Python 运行时
# 与几十个包，静态名单会在第一次升依赖时就过期 —— 而过期的许可清单比没有更糟，
# 它看起来是权威的。生成器读的是**构建环境自己的** importlib.metadata。
#
# ⚠️ `-RedirectStandardOutput` 与 `-RedirectStandardError` **不能指向同一个文件**：
#    Start-Process 会直接抛错（两个句柄抢同一个文件），而在 `$ErrorActionPreference='Stop'`
#    下这一抛就是整个脚本结束 —— 表现是「日志停在『第 4 步』、退出码 33」，
#    而日志里**一个字的原因都没有**（因为错在 PowerShell 自己身上，还没轮到我们写日志）。
#    2026-09-29 实测踩到：必须分成两个文件。
$genOut = Join-Path $Work 'gen-third-party.out.log'
$genErr = Join-Path $Work 'gen-third-party.err.log'
$proc3 = Start-Process -FilePath $Python `
    -ArgumentList @((Join-Path $RepoRoot 'packaging\windows\gen-third-party.py'),
                    (Join-Path $payload 'THIRD-PARTY-LICENSES.txt')) `
    -Wait -PassThru -NoNewWindow -RedirectStandardOutput $genOut -RedirectStandardError $genErr
foreach ($f in @($genOut, $genErr)) {
    if (Test-Path $f) {
        Add-Content -Path $script:LogPath -Value ("--- {0} ---" -f (Split-Path $f -Leaf)) -Encoding UTF8
        Get-Content $f -Encoding UTF8 | Add-Content -Path $script:LogPath -Encoding UTF8
    }
}
if ($proc3.ExitCode -ne 0 -or -not (Test-Path (Join-Path $payload 'THIRD-PARTY-LICENSES.txt'))) {
    Fail "生成第三方许可清单失败（rc=$($proc3.ExitCode)），细节见日志里的 gen-third-party.*.log 段"
}

Write-Log "payload 就位: $payload"
Write-Log "  server/  ← 冻结产物（exe 与 _internal/ 平级）"
Write-Log "  bin/     ← setup.ps1 / doctor.ps1（用户级注册与体检）"
Write-Log "  LICENSE / THIRD-PARTY-LICENSES.txt / README.txt"

if ($SkipInstaller) {
    Write-Step 'DONE (installer skipped)' '完成（-SkipInstaller：未打安装包）'
    exit 0
}

# ─────────────────── 第 5 步：找编译器 ───────────────────

Write-Step '[5/6] locating ISCC.exe' '第 5 步：定位 Inno Setup 编译器'

if (-not $Iscc) {
    $Iscc = Find-Iscc
}
if (-not $Iscc -or -not (Test-Path $Iscc)) {
    Fail @"
找不到 Inno Setup 编译器 ISCC.exe。
  装法（免管理员，绿色装到用户目录）：
    <innosetup>.exe /PORTABLE=1 /DIR="%LOCALAPPDATA%\Programs\Inno Setup 6" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
  或用 -Iscc 显式指定路径。宿主侧的 packaging/windows/build-windows.sh 会自动送进来。
"@
}
Write-Log "ISCC = $Iscc"

# ─────────────────── 第 6 步：打安装包 ───────────────────

Write-Step '[6/6] building installer' '第 6 步：用 Inno Setup 打安装包'

# 版本号经 `/D` 传给 .iss 的预处理器（Inno 自带预处理器，所以不需要外层的模板替换：
# 这正是没用 `.iss.in` + sed 的原因 —— 少一层间接，人也能直接手工跑 ISCC）。
$iss = Join-Path $RepoRoot 'packaging\windows\installer.iss'
if (-not (Test-Path $iss)) { Fail "找不到 installer.iss: $iss" }

$isccOut = Join-Path $Work 'iscc.out.log'
$isccErr = Join-Path $Work 'iscc.err.log'
$sw2 = [Diagnostics.Stopwatch]::StartNew()
$proc2 = Start-Process -FilePath $Iscc `
    -ArgumentList @("/DAppVersion=$Version", "/DPayloadDir=$payload", "/DOutDir=$Out", $iss) `
    -Wait -PassThru -NoNewWindow -RedirectStandardOutput $isccOut -RedirectStandardError $isccErr
$sw2.Stop()
foreach ($f in @($isccOut, $isccErr)) {
    if (Test-Path $f) {
        Add-Content -Path $script:LogPath -Value ("--- {0} ---" -f (Split-Path $f -Leaf)) -Encoding UTF8
        Get-Content $f -Encoding UTF8 | Add-Content -Path $script:LogPath -Encoding UTF8
    }
}
Write-Log ("ISCC 退出码 = {0}，耗时 {1:N1}s" -f $proc2.ExitCode, $sw2.Elapsed.TotalSeconds)
if ($proc2.ExitCode -ne 0) { Fail "ISCC 失败（退出码 $($proc2.ExitCode)）" }

$setup = Join-Path $Out "cc-computer-use-$Version-setup.exe"
if (-not (Test-Path $setup)) { Fail "ISCC 成功但产物不在预期路径: $setup" }
$setupMB = [math]::Round((Get-Item $setup).Length / 1MB, 1)

Write-Log ('=' * 70)
Write-Log "✅ 全部完成"
Write-Log "  冻结产物: $dist"
Write-Log "  安装包  : $setup ($setupMB MB)"
Write-Log ('=' * 70)

Write-Step 'OK' "构建完成：$setup"
exit 0