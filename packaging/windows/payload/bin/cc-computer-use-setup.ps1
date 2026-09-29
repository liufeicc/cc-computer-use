<#
  用户级配置：把本 MCP server 注册进 Claude Code（user scope）。

  ═══ 这是 Windows 版的 `packaging/deb/cc-computer-use-setup.in` ═══

  两边做同一件事、同一条纪律，只是平台形态不同：

  · **只做用户级的事**。Linux 侧那份第一件事是「拒绝 root」，理由是 root 写的是
    /root 的配置、对真实用户完全无效 —— 一类「命令成功、毫无作用、还很难查」的失败。
    Windows 上对应的坑是**提权安装**：安装器若以管理员身份跑，`%USERPROFILE%` /
    `%APPDATA%` 可能指向别的账户，注册就落到了别人头上。本包刻意用
    `PrivilegesRequired=lowest`（只装当前用户），就是为了从构造上避开它。
  · **幂等**：已注册就只报告、不改动（`--Force` 才替换）。
  · **按事实说话**：判据是 `claude mcp get` 的退出码，不是"命令发出去了"。
    失败时给一条**可以直接复制**的手工命令，绝不无条件打印"已注册"。

  ═══ ⚠️ 注册的**硬前提**：机器上得有 Git for Windows（或 PowerShell 7）═══

  Claude Code 在 Windows 上要求这两者之一，**系统自带的 Windows PowerShell 5.1 不算**
  （它要的是 PowerShell 7）。两者都没有时 `claude` 连 `--version` 都拒绝执行，报
  「Claude Code on Windows requires either Git for Windows (for bash) or PowerShell」。

  ⚠️ 这条曾被我们**归因错**过：旧注释说它是"非交互上下文导致的，同一个命令在交互式会话里
  正常，照字面去装 Git 会白折腾一轮"。2026-09-29 隔离实测推翻了它 —— 装上 Git 之后在
  **同一个**上下文里立刻正常（`mcp add/get/remove/list` 跑过完整 CRUD）。所以现在
  `Find-GitBash` 找到 bash 就通过 `CLAUDE_CODE_GIT_BASH_PATH` 交给 claude；
  找不到就**明说缺什么、去哪装**。

  ═══ 用法 ═══

      powershell -NoProfile -ExecutionPolicy Bypass -File cc-computer-use-setup.ps1
      powershell ... -File cc-computer-use-setup.ps1 -Unregister    # 卸载时由安装器调用
      powershell ... -File cc-computer-use-setup.ps1 -Force         # 用本包入口替换已有配置

  退出码：正常路径一律 0（"没装 Claude Code"不是本包的失败）；
  仅当**参数用法错误**（例如安装目录不完整）才返回非 0。

  ═══ 给自动化的一条约定 ═══

  最后一行永远是一条 ASCII 的机器可读结果：

      [cc-cu-setup] RESULT=<registered|already-registered|claude-not-found|add-failed|removed|not-registered|remove-failed|app-incomplete>

  为什么要有它：客体是中文 Windows，PowerShell 往**管道/重定向文件**里写的中文
  是按当前代码页编码的，宿主读回来是乱码 —— 判据不能建立在会丢字的东西上。
  结果行全 ASCII，任何编码下都不会看错。
#>
[CmdletBinding()]
param(
    [switch]$Unregister,
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'

$McpName = 'cc-computer-use'
# 安装布局：bin/ 与本脚本同级，可执行文件在 ../server/（与 Linux 侧 server/ 布局同构）
$AppDir = Split-Path -Parent $PSScriptRoot
$Exe = Join-Path $AppDir 'server\computer-use-mcp-bin.exe'

# ⚠️ 本脚本的所有输出**同时**落一份到安装目录下的 setup.log（UTF-8）。
#    为什么必须有这个文件：安装器会在收尾阶段自动跑一次本脚本（[Run] 段），
#    而那一跑的窗口是隐藏的、输出没人看得到。如果只在控制台打印，
#    「安装时到底注册成功了没有」就成了一个没有痕迹的问题 —— 用户只会发现
#    「Claude Code 里怎么没有它」。有了这个日志，任何一次运行（安装器触发的、
#    开始菜单触发的、手工触发的）都留下同一种可查的证据。
#    编码用 UTF8（带 BOM）：中文 Windows 的记事本对无 BOM 的 UTF-8 会按 ANSI 解。
$script:LogPath = Join-Path $AppDir 'setup.log'

function Write-Log {
    param([string]$Message)
    try {
        # -Encoding UTF8 在 PS 5.1 下会写 BOM；首次创建即可，后续追加不会重复写 BOM
        Add-Content -Path $script:LogPath -Value ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) -Encoding UTF8
    }
    catch {
        # 日志写不了（目录只读、被占用…）**绝不能**让注册本身失败 —— 它只是证据，不是功能
    }
}

function Write-Info { param([string]$m) if (-not $Quiet) { Write-Host $m }; Write-Log $m }
function Write-Changed { param([string]$m) Write-Host "✅ $m"; Write-Log "CHANGED $m" }
function Write-Warn { param([string]$m) Write-Host "⚠️  $m" -ForegroundColor Yellow; Write-Log "WARN $m" }
function Write-Result {
    param([string]$Code)
    Write-Host "[cc-cu-setup] RESULT=$Code"
    Write-Log "RESULT=$Code"
}

function Find-Claude {
    <#
      找 claude 可执行文件。为什么是候选表而不是 `Get-Command` 一条路：
      `Get-Command` 读的是**本进程的 PATH**，而安装器 / 计划任务拿到的 PATH 往往比
      用户登录 shell 里短 —— 「用户明明能用 claude，这里却找不到」就是这么来的。
      候选表覆盖 npm 全局前缀（Claude Code 在 Windows 上的主流装法）与两个官方
      本地安装位置。doctor 用的是**同一张表、同一顺序**（有守卫测试盯着两边一致）。
    #>
    $cmd = Get-Command claude -ErrorAction SilentlyContinue |
           Where-Object { $_.Source } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path $env:APPDATA 'npm\claude.cmd')
        (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe')
        (Join-Path $env:USERPROFILE '.local\bin\claude.exe')
        (Join-Path $env:USERPROFILE '.claude\local\claude.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Find-GitBash {
    <#
      找 Git for Windows 的 `bash.exe` —— 这是**客体上 claude 能不能跑起来的前提**。

      ⚠️ 这条不是我们的口味，是 Claude Code 自己的硬要求。2026-09-29 真机实测：
      客体上既没有 Git for Windows 也没有 PowerShell 7（只有系统自带的
      Windows PowerShell 5.1）时，`claude --version` / `mcp get` / `mcp add`
      **一律**被拒，报「Claude Code on Windows requires either Git for Windows (for bash)
      or PowerShell」。把 `CLAUDE_CODE_GIT_BASH_PATH` 指向一个可用的 bash.exe 之后，
      `mcp add / get / remove / list` **全部恢复**（同一台机器上跑过完整 CRUD：
      add 返回 rc=0「Added stdio MCP server …」、get 能读回、remove 能摘掉）。

      ⚠️ **这个变量是 Claude Code 自己的报错里点名的那个旋钮**（原文最后一句就是
      「Or set CLAUDE_CODE_GIT_BASH_PATH to your bash.exe location」）—— 用它，而不是
      去改用户/机器的 PATH：改 PATH 要等新进程才生效（计划任务拿到的仍是旧环境），
      而进程级环境变量当场就生效、也不污染用户机器。

      候选表与 Find-Claude 同一个纪律：`Get-Command` 只读**本进程**的 PATH，
      而安装器 / 计划任务拿到的 PATH 往往比用户登录 shell 里短。官方安装器
      per-user 装到 `%LOCALAPPDATA%\Programs\Git`、per-machine 装到 `%ProgramFiles%\Git`，
      两个都查；便携版（PortableGit 解包）常见落在用户目录下，也一并兜住。
    #>
    # 显式覆盖优先（验收脚本、特殊布局靠它）
    if ($env:CC_CU_GIT_BASH_PATH -and (Test-Path $env:CC_CU_GIT_BASH_PATH)) {
        return $env:CC_CU_GIT_BASH_PATH
    }
    # 已经在 PATH 上（用户从 Git Bash / 已刷新的 shell 里跑本脚本时走这条）
    $cmd = Get-Command bash -ErrorAction SilentlyContinue |
           Where-Object { $_.Source } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
        (Join-Path $env:ProgramFiles 'Git\bin\bash.exe')
        (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe')
        (Join-Path $env:USERPROFILE 'git\bin\bash.exe')
        (Join-Path $env:USERPROFILE 'PortableGit\bin\bash.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function ConvertTo-ArgString {
    <#
      Start-Process 收的是**一整条命令行字符串**，不是参数数组 —— 含空格的路径
      不引起来就会被拆成两个参数（而 `%USERPROFILE%` 里带空格完全正常：
      "C:\Users\Zhang San\..."）。Linux 侧用的是数组传参，没有这个问题；
      这里是 Windows 特有的地方，单独抽一个函数免得散落各处。
    #>
    param([string[]]$Arguments)
    ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join ' '
}

function Resolve-ClaudeRunner {
    <#
      把「找到的 claude 路径」变成一个**能真的启动**的东西。

      ⚠️ npm 全局安装会留下三个 shim：`claude`（给 bash 的）、`claude.cmd`（给 cmd 的）、
      `claude.ps1`（给 PowerShell 的）。而 `Get-Command claude` 在 PowerShell 里返回的是
      **`.ps1`**，它**不是可执行文件** —— 直接拿它当 FileName 起会报
      「不是有效的 Win32 应用程序」（2026-09-29 真机踩到，报错读起来还像"claude 有问题"）。

      优先换成同名的 `.cmd`（它本来就是给 cmd 用的批处理入口，能直接被 CreateProcess 起）；
      没有 `.cmd` 才退回 `powershell -File`。

      ⚠️ **曾经把「requires either Git for Windows …」误记成这条分流的锅**（以为
      `powershell -File` 多包了一层导致 Claude Code 探测不到 shell）。2026-09-29 用探针
      逐个变量隔离后否证了：那条报错的真正原因是**机器上确实没有 bash 也没有 PowerShell 7**，
      与走哪条 shim 无关 —— 见 Find-GitBash。别再把这两件事混在一起。
    #>
    param([string]$Path)
    if ($Path -and $Path.ToLower().EndsWith('.ps1')) {
        $cmdSibling = [IO.Path]::ChangeExtension($Path, '.cmd')
        if (Test-Path $cmdSibling) {
            return [pscustomobject]@{ Exe = $cmdSibling; Prefix = @() }
        }
        return [pscustomobject]@{
            Exe    = 'powershell.exe'
            Prefix = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Path)
        }
    }
    return [pscustomobject]@{ Exe = $Path; Prefix = @() }
}

function Invoke-Claude {
    <#
      跑一次 claude：带超时、按 UTF-8 解码两个流、**拿到真实退出码**。

      ⚠️ 为什么不用 `Start-Process -PassThru`：客体是系统自带的 PowerShell **5.1**，
      实测（2026-09-29）它只要带了 `-RedirectStandardOutput`，返回对象的 `$p.ExitCode`
      **恒为空**（`done=True / rc-direct= / rc-after=`，补调无参 WaitForExit() 也没用）。
      而本脚本的全部判断都建立在"`mcp get/add` 到底成没成"上 —— 拿到空值就等于
      **永远报失败**，或者更糟：被当成"非 0"从而反复去改用户的配置。
      改用 System.Diagnostics.Process 直接起，退出码可靠，还顺手解决编码问题。

      **先起异步读、再等退出**：两个流都读完才 WaitForExit，子进程输出量大时会死锁。
      （超时不是洁癖：首次运行 / 版本升级时 claude 可能卡在一个交互提示上，
      而本脚本会被安装器在收尾阶段调用 —— 挂住就是安装挂住。）

      超时那一支**必须返回一个非 0 的哨兵值**（不能顺手写 0）：写 0 的话，一次卡住的
      `mcp get` 会被读成"已注册"，于是该注册的没注册、用户以为装好了。
    #>
    param([string]$ClaudePath, [string[]]$Arguments, [int]$TimeoutSec = 30)

    $runner = Resolve-ClaudeRunner -Path $ClaudePath
    $all = @($runner.Prefix) + $Arguments

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $runner.Exe
    $psi.Arguments = (ConvertTo-ArgString $all)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8

    # ⚠️ **必须把 bash 的位置告诉 claude**，否则在没有 Git/PowerShell 7 的机器上
    #    claude 会直接拒绝执行（连 `--version` 都不给）。这是进程级注入 ——
    #    只影响我们起的这一个子进程，不动用户的机器环境。见 Find-GitBash。
    $bash = Find-GitBash
    if ($bash) { $psi.EnvironmentVariables['CLAUDE_CODE_GIT_BASH_PATH'] = $bash }

    $p = [System.Diagnostics.Process]::Start($psi)
    $tOut = $p.StandardOutput.ReadToEndAsync()
    $tErr = $p.StandardError.ReadToEndAsync()
    $finished = $p.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) { try { $p.Kill() } catch { } }
    $o = try { [string]$tOut.Result } catch { '' }
    $e = try { [string]$tErr.Result } catch { '' }
    return [pscustomobject]@{
        ExitCode = if ($finished) { [int]$p.ExitCode } else { 124 }
        TimedOut = -not $finished
        Out      = $o.Trim()
        Err      = $e.Trim()
        How      = $runner.Exe
        Bash     = $bash
    }
}

function Show-ShellHintIfNeeded {
    <#
      把「Claude Code 拒绝执行」这条报错翻译成人话。

      ⚠️ **这里曾经写着一段错话，2026-09-29 实测否证后改写**：
      旧注释说这条报错是「非交互上下文导致的、同一个命令在交互式会话里正常，
      照字面去装 Git 会白折腾一轮」。**那是错的** —— 真相是这台机器上真的
      既没有 Git for Windows 也没有 PowerShell 7，而 Claude Code 就是要其中一个；
      装上之后在**同一个**（计划任务 /IT 的）上下文里立刻就正常了。

      所以现在不再叫用户"别装"，而是给出**具体缺什么、去哪装**。原样抛给用户也不行：
      报错里那两个下载链接很长，容易看漏最后那句真正有用的
      「Or set CLAUDE_CODE_GIT_BASH_PATH to your bash.exe location」。
    #>
    param([string]$Text)
    if ($Text -match 'requires either Git for Windows') {
        Write-Warn 'Claude Code 需要一个 shell 才能跑：Git for Windows 或 PowerShell 7。'
        Write-Warn '  本机两样都没有（系统自带的 Windows PowerShell 5.1 **不算** ——'
        Write-Warn '  Claude Code 要的是 PowerShell 7 或 Git Bash）。二选一即可：'
        Write-Warn '    · Git for Windows: https://git-scm.com/downloads/win'
        Write-Warn '    · PowerShell 7   : https://aka.ms/powershell'
        Write-Warn '  装完重跑本脚本即可。'
        return $true
    }
    return $false
}

# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

# 每次运行都先落一行「谁在什么时候、以什么方式跑的我」。
# 安装器触发的、开始菜单触发的、手工敲的，三者在日志里要能分出来 ——
# 否则出问题时第一句问的就是"这次到底是谁跑的"，而答案已经丢了。
Write-Log ('=' * 60)
Write-Log ("run: mode={0} elevated={1} args=[{2}]" -f `
        $(if ($Unregister) { 'unregister' } else { 'register' }), `
        ([Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)), `
        ($MyInvocation.Line))

if (-not (Test-Path $Exe)) {
    Write-Warn "安装目录不完整，找不到可执行文件：$Exe"
    Write-Warn '  请先重新安装本包（安装器应当把 server/ 完整放到安装目录下）。'
    Write-Result 'app-incomplete'
    exit 1
}

if (-not $Unregister) {
    Write-Info "cc-computer-use —— 注册到 Claude Code（user scope）"
    Write-Info "  MCP 名称 : $McpName"
    Write-Info "  入口     : $Exe"
}

$claude = Find-Claude
if (-not $claude) {
    if ($Unregister) {
        # 卸载路径：没有 claude 就没有注册项，什么都不用做 —— 这不是失败
        Write-Info '未找到 claude 命令，无需移除注册项。'
        Write-Result 'not-registered'
        exit 0
    }
    Write-Warn '未找到 claude 命令 —— 若之后才装 Claude Code，重跑本脚本即可。'
    Write-Warn '  手工注册命令（把路径换成本机实际安装位置）：'
    Write-Warn ("    claude mcp add --scope user $McpName -- `"$Exe`"")
    Write-Result 'claude-not-found'
    exit 0
}
Write-Info "  claude   : $claude"
if (-not $Unregister) {
    $bashForReport = Find-GitBash
    if ($bashForReport) {
        Write-Info "  git bash : $bashForReport"
    }
    else {
        Write-Warn '本机没有 Git for Windows / PowerShell 7 —— Claude Code 会拒绝执行，'
        Write-Warn '注册这一步会失败。装一个再重跑本脚本：'
        Write-Warn 'http://git-scm.com/downloads/win 或 https://aka.ms/powershell'
    }
}

# ── 卸载：移除注册项 ────────────────────────────────────────────────
if ($Unregister) {
    $get = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'get', $McpName) -TimeoutSec 120
    if ($get.ExitCode -ne 0) {
        Write-Info "Claude Code 中本来就没有「$McpName」，无需移除。"
        Write-Result 'not-registered'
        exit 0
    }
    $rm = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'remove', '--scope', 'user', $McpName) -TimeoutSec 120
    if ($rm.ExitCode -eq 0) {
        Write-Changed "已从 Claude Code 移除「$McpName」。"
        Write-Result 'removed'
    }
    else {
        if (-not (Show-ShellHintIfNeeded ($rm.Out + $rm.Err))) {
            Write-Warn '移除失败。请手动执行：'
            Write-Warn "  claude mcp remove --scope user $McpName"
        }
        Write-Result 'remove-failed'
    }
    exit 0
}

# ── 安装：注册 ──────────────────────────────────────────────────────

# `mcp get` 是权威判据：它能看穿 local/user/project 各 scope，
# 比去 grep ~/.claude.json 可靠得多（Linux 侧同款做法）。
$get = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'get', $McpName) -TimeoutSec 120
if ($get.ExitCode -eq 0 -and -not $Force) {
    Write-Info "MCP「$McpName」已存在，保持不动："
    if (-not $Quiet -and $get.Out) { $get.Out -split "`n" | ForEach-Object { Write-Info "    $_" } }
    Write-Info "  如需改为本包的入口：先 -Unregister，再带 -Force 重跑本脚本"
    Write-Result 'already-registered'
    exit 0
}

if ($get.ExitCode -eq 0 -and $Force) {
    $null = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'remove', '--scope', 'user', $McpName) -TimeoutSec 120
    Write-Info "已移除旧的「$McpName」配置（-Force）"
}

$add = Invoke-Claude -ClaudePath $claude `
    -Arguments @('mcp', 'add', '--scope', 'user', $McpName, '--', $Exe) -TimeoutSec 180
if ($add.ExitCode -eq 0) {
    Write-Changed "已注册到 Claude Code（user scope）：$McpName → $Exe"
    Write-Info '  重启 Claude Code 会话（或执行 /mcp）即可看到它'
    Write-Result 'registered'
}
else {
    if (-not (Show-ShellHintIfNeeded ($add.Out + $add.Err))) {
        Write-Warn "自动注册失败（rc=$($add.ExitCode)）。请手动执行："
        Write-Warn "  claude mcp add --scope user $McpName -- `"$Exe`""
        if ($add.Out) { $add.Out -split "`n" | Select-Object -First 5 | ForEach-Object { Write-Warn "    $_" } }
        if ($add.Err) { $add.Err -split "`n" | Select-Object -First 5 | ForEach-Object { Write-Warn "    $_" } }
    }
    Write-Result 'add-failed'
}

if (-not $Quiet) {
    Write-Host ''
    Write-Host '检查是否可用：  开始菜单 → cc-computer-use → 体检（doctor）'
}
exit 0