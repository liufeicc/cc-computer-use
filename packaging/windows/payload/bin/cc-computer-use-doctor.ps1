<#
  cc-computer-use 体检脚本（Windows 版，对应 Linux 的 cc-computer-use-doctor）。

  回答一个问题：**这台机器上，它到底能不能用？**

  为什么值得单独写一个（而不是让用户自己试）：本包是自包含分发的 —— 装完之后
  用户看到的失败现象往往只是"读不到界面"，而从现象到原因之间隔着好几层
  （冻结产物收没收全依赖、有没有桌面会话、Claude Code 注没注册上…）。
  体检脚本负责把每一层单独验一遍，并把失败讲成人话：哪一项缺了、会导致什么现象。

  用法:
      powershell -NoProfile -ExecutionPolicy Bypass -File cc-computer-use-doctor.ps1

  退出码: 0 = 关键项全过；1 = 有硬伤（照标 ❌ 的那几条处理）。

  ═══ 给自动化的一条约定（与 setup 脚本同款）═══
  stdout 上除了人看的文字，还会有 ASCII 的机器可读行：
      [cc-cu-doctor] CHECK=<name> OK|FAIL|WARN
      [cc-cu-doctor] RESULT=<ok|failed>
  中文 Windows 下经管道读回的中文会乱码，判据只能建立在 ASCII 行上。
#>
[CmdletBinding()]
param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'

$McpName = 'cc-computer-use'
$AppDir = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $AppDir 'server'
$Exe = Join-Path $ServerDir 'computer-use-mcp-bin.exe'

$script:Fails = 0
$script:Warns = 0

function Say { param([string]$m) if (-not $Quiet) { Write-Host $m } }
function Ok { param([string]$m) Write-Host "  ✅ $m" }
function Bad {
    param([string]$m)
    Write-Host "  ❌ $m" -ForegroundColor Red
    $script:Fails++
}
function Soft {
    param([string]$m)
    Write-Host "  ⚠️  $m" -ForegroundColor Yellow
    $script:Warns++
}
function Head { param([string]$m) Write-Host ''; Write-Host $m }
function Check {
    param([string]$Name, [string]$Verdict)
    Write-Host "[cc-cu-doctor] CHECK=$Name $Verdict"
}

function Invoke-Exe {
    <#
      跑一个可执行文件，**带超时**、按 UTF-8 取回两个流、**拿到真实退出码**。

      ⚠️ 为什么不用 `Start-Process -PassThru`（那看起来是最自然的选择）：客体是系统自带的
      Windows PowerShell **5.1**，实测（2026-09-29）只要带了 `-RedirectStandardOutput`，
      返回对象的 `$p.ExitCode` **恒为空**（`done=True / rc-direct= / rc-after=`，
      补调无参的 `WaitForExit()` 也没用）。而本脚本要回答的正是「自检到底过没过」——
      拿到空值就等于永远报失败：**假红**，方向最坏的那种（会让人去改本来正确的产物）。

      改用 System.Diagnostics.Process：退出码可靠，还能顺手按 UTF-8 解码
      （中文 Windows 上不设就是 GBK，自检输出的中文全变乱码）。早先这里为了拿输出
      还额外落两个临时文件，现在两个流直接读回，连临时文件都不需要了。

      **先起异步读、再等退出**：两个流都读完才 WaitForExit 会在子进程输出量大时死锁。

      超时那一支**必须返回一个非 0 的哨兵值**（不能顺手写 0）：写 0 的话，一次卡住的
      自检会被读成"通过"，与"拿到空值"是同一类错误，只是更隐蔽。
    #>
    param([string]$Exe, [string[]]$Arguments, [int]$TimeoutSec = 30, [string]$GitBash = '')

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8

    # 给 claude 指路到 bash（见 Find-GitBash）。进程级注入，不动用户机器环境。
    if ($GitBash) { $psi.EnvironmentVariables['CLAUDE_CODE_GIT_BASH_PATH'] = $GitBash }

    $p = [System.Diagnostics.Process]::Start($psi)
    $tOut = $p.StandardOutput.ReadToEndAsync()
    $tErr = $p.StandardError.ReadToEndAsync()
    $finished = $p.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) { try { $p.Kill() } catch { } }
    # 进程已退出（或已被杀），流会关闭 —— 这里不会挂住。
    #
    # ⚠️ 这两个 `try` **不能写成 `(try { … } catch { … })`**：把 try 当**表达式**用是
    #    PowerShell 7 才有的语法，客体自带的 5.1 **连解析都过不了**（整份脚本一个字都不执行，
    #    而报错只指向那一行）。2026-09-29 实测踩到：体检脚本在客体上完全跑不起来。
    #    赋值形式 `$x = try { … } catch { … }` 5.1 认，括号包起来的那种不认。
    $outText = ''
    try { $outText = [string]$tOut.Result } catch { }
    $errText = ''
    try { $errText = [string]$tErr.Result } catch { }

    # ⚠️ `ExitCode` 只在**确实退出过**时才读：进程还活着时读它会抛 InvalidOperationException。
    return [pscustomobject]@{
        ExitCode = if ($finished) { [int]$p.ExitCode } else { -1 }
        TimedOut = -not $finished
        Out      = $outText.Trim()
        Err      = $errText.Trim()
    }
}

Write-Host 'cc-computer-use 体检'
Write-Host "安装目录: $AppDir"

# ── 1. 载荷完整性 ───────────────────────────────────────────────────
Head '[1/4] 随包文件'

if (Test-Path $Exe) { Ok "可执行文件: server\computer-use-mcp-bin.exe" }
else { Bad "缺可执行文件: $Exe" }

# 冻结产物最容易"看着成功、实际缺件"的地方就是 _internal/ 下的动态依赖
# （uiautomation 的运行时 COM 模块、PyWinRT 的投影包…），逐个点名查。
$internal = Join-Path $ServerDir '_internal'
$expect = @(
    @{ P = 'uiautomation'; W = 'UIA 元素树驱动' }
    @{ P = 'comtypes'; W = 'COM 绑定' }
    @{ P = 'winrt'; W = 'Windows.Media.Ocr（OCR 通道）' }
    @{ P = 'mss'; W = '抓屏' }
    @{ P = 'PIL'; W = 'Pillow（图像编码）' }
    @{ P = 'mcp'; W = 'MCP SDK' }
)
foreach ($e in $expect) {
    $p = Join-Path $internal $e.P
    if (Test-Path $p) { Ok ("_internal\{0,-14} ({1})" -f $e.P, $e.W) }
    else { Bad ("缺 _internal\{0} ({1}) —— 该能力整体不可用" -f $e.P, $e.W) }
}
Check 'payload' $(if ($script:Fails -eq 0) { 'OK' } else { 'FAIL' })

# ── 2. 自检（真的把 exe 跑起来）──────────────────────────────────────
Head '[2/4] 自检（--selftest）'

if (Test-Path $Exe) {
    # ⚠️ 必须走 Invoke-Exe（System.Diagnostics.Process），**不能**退回 `Start-Process`：
    #    退回的话自检的退出码在客体 PS 5.1 上恒为空，这条判据永远不成立。
    #    （早先为了避开编码问题，这里还把两个流重定向到临时文件再按 UTF-8 读回；
    #      现在进程对象本身就设了 UTF-8 解码，那一圈绕路连同临时文件一起去掉了。）
    $r = Invoke-Exe -Exe $Exe -Arguments @('--selftest') -TimeoutSec 120
    $text = $r.Out
    $etext = $r.Err

    if ($r.ExitCode -eq 0) {
        Ok '自检退出码 0'
    }
    else {
        Bad ("自检退出码 {0}（超时={1}）—— 产物或运行环境有问题" -f $r.ExitCode, $r.TimedOut)
    }
    # 判据取"自检自己打的那句结论"，不取退出码单一项：
    # 退出码可能被下游 shell 吃掉，而这行文字是 exe 自己写的。
    if ($text -match '\[selftest\] ✅ OK') { Ok '自检结论行: [selftest] ✅ OK' }
    else { Bad '自检没有打出 ✅ OK 结论行' }
    Check 'selftest' $(if ($text -match '\[selftest\] ✅ OK') { 'OK' } else { 'FAIL' })

    if (-not $Quiet) {
        Say '  --- 自检输出（节选）---'
        ($text -split "`r?`n") | Select-Object -First 12 | ForEach-Object { Say "    $_" }
    }
    if ($etext) {
        Soft "自检有 stderr 输出（通常无害，但值得看一眼）:"
        ($etext -split "`r?`n") | Select-Object -First 5 | ForEach-Object { Say "    $_" }
    }
}
else {
    Soft '可执行文件不存在，跳过自检'
}

# ── 3. Claude Code 注册状态 ─────────────────────────────────────────
Head '[3/4] Claude Code 注册状态'

# ⚠️ 候选表与 cc-computer-use-setup.ps1 **必须同源同序**（有守卫测试盯着）：
#    两边探测的路径不一致，就会出现"setup 说注册好了、doctor 说找不到"这种互相打架的结论。
$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue |
             Where-Object { $_.Source } | Select-Object -First 1
$claude = if ($claudeCmd) { $claudeCmd.Source } else { $null }
if (-not $claude) {
    foreach ($c in @(
            (Join-Path $env:APPDATA 'npm\claude.cmd')
            (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe')
            (Join-Path $env:USERPROFILE '.local\bin\claude.exe')
            (Join-Path $env:USERPROFILE '.claude\local\claude.exe')
        )) {
        if ($c -and (Test-Path $c)) { $claude = $c; break }
    }
}

function Resolve-ClaudeRunner {
    <#
      把「找到的 claude 路径」变成一个**能真的启动**的东西。

      ⚠️ npm 全局安装会留下三个 shim：`claude`（给 bash 的）、`claude.cmd`（给 cmd 的）、
      `claude.ps1`（给 PowerShell 的）。而 `Get-Command claude` 在 PowerShell 里返回的是
      **`.ps1`**，它**不是可执行文件** —— 直接拿它当 FileName 起会报
      「不是有效的 Win32 应用程序」，读起来还像"claude 有问题"。

      优先换成同名的 `.cmd`（它本来就是给 cmd 用的批处理入口，能直接被 CreateProcess 起）；
      没有 `.cmd` 才退回 `powershell -File`。

      ⚠️ **别再把这处分流与「requires either Git for Windows …」混为一谈**：那条报错的
      真正原因是机器上确实没有 bash / PowerShell 7，与走哪条 shim 无关（2026-09-29 隔离实测），
      见下面 Find-GitBash。

      ⚠️ 本函数与 `cc-computer-use-setup.ps1` / `verify-on-windows.ps1` 里的同名函数
      是**同一份逻辑的副本**（三个脚本各自独立分发、不互相 dot-source）。改一处就要
      三处一起改，守卫 `test_guest_scripts_never_start_a_ps1_directly` 盯着这一点。
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

function Find-GitBash {
    <#
      找 Git for Windows 的 `bash.exe` —— claude 能不能跑起来的前提。

      ⚠️ Claude Code 在 Windows 上**要求**有 Git for Windows（bash）或 PowerShell 7，
      两者都没有时连 `claude --version` 都会被拒（2026-09-29 真机实测）。系统自带的
      Windows PowerShell 5.1 **不算** —— 报错原文要的是 PowerShell 7。
      把 `CLAUDE_CODE_GIT_BASH_PATH` 指向可用的 bash 之后 `mcp add/get/remove/list`
      全部恢复（真机跑过完整 CRUD）。

      候选表与 Find-Claude 同源同序纪律，理由也一样：`Get-Command` 只读**本进程**的 PATH，
      而安装器 / 计划任务拿到的 PATH 往往比用户登录 shell 里短。
    #>
    if ($env:CC_CU_GIT_BASH_PATH -and (Test-Path $env:CC_CU_GIT_BASH_PATH)) {
        return $env:CC_CU_GIT_BASH_PATH
    }
    $cmd = Get-Command bash -ErrorAction SilentlyContinue |
           Where-Object { $_.Source } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($c in @(
            (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe')
            (Join-Path $env:ProgramFiles 'Git\bin\bash.exe')
            (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe')
            (Join-Path $env:USERPROFILE 'git\bin\bash.exe')
            (Join-Path $env:USERPROFILE 'PortableGit\bin\bash.exe')
        )) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

if (-not $claude) {
    Soft '未找到 claude 命令 —— 未安装 Claude Code 时属正常'
    Say "  装好 Claude Code 后重跑: $(Join-Path $AppDir 'bin\cc-computer-use-setup.ps1')"
    Check 'claude-registration' 'WARN'
}
else {
    Ok "找到 claude: $claude"

    # 先单独把「claude 要的那个 shell」报出来：缺了它，注册那一步必然失败，
    # 而报错长得像"claude 坏了"。这条必须在注册检查之前说。
    $bash = Find-GitBash
    if ($bash) {
        Ok "claude 需要的 shell: $bash"
        Check 'claude-shell' 'OK'
    }
    else {
        Soft '缺 Git for Windows / PowerShell 7 —— Claude Code 会拒绝执行，注册必然失败'
        Say '    装一个再重跑: https://git-scm.com/downloads/win 或 https://aka.ms/powershell'
        Check 'claude-shell' 'WARN'
    }

    # 走与 setup 完全相同的启动方式（同一个 Resolve-ClaudeRunner + Invoke-Exe +
    # 同一个 CLAUDE_CODE_GIT_BASH_PATH 注入），免得出现「setup 说注册好了、
    # doctor 说没注册」——那类互相打架的结论最难查。
    $runner = Resolve-ClaudeRunner -Path $claude
    $psiArgs = @($runner.Prefix) + @('mcp', 'get', $McpName)
    $r = Invoke-Exe -Exe $runner.Exe -Arguments $psiArgs -TimeoutSec 120 -GitBash $bash

    if ($r.ExitCode -eq 0) {
        Ok "已在 Claude Code 中注册「$McpName」（user scope）"
        if (-not $Quiet -and $r.Out) { $r.Out -split "`n" | Select-Object -First 8 | ForEach-Object { Say "    $_" } }
        Check 'claude-registration' 'OK'
    }
    else {
        Soft "Claude Code 中还没有「$McpName」"
        if ($r.Out + $r.Err -match 'requires either Git for Windows') {
            Soft '  （原因就是上面那条：机器上缺 Git for Windows / PowerShell 7）'
        }
        Say "  注册: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $AppDir 'bin\cc-computer-use-setup.ps1')`""
        Check 'claude-registration' 'WARN'
    }
}

# ── 4. 结论 ─────────────────────────────────────────────────────────
Head '[4/4] 结论'
if ($script:Fails -eq 0) {
    Write-Host "  ✅ 关键项全过（警告 $($script:Warns) 条）"
    Write-Host '[cc-cu-doctor] RESULT=ok'
    exit 0
}
Write-Host "  ❌ 有 $($script:Fails) 项硬伤，照上面标 ❌ 的条目处理"
Write-Host '[cc-cu-doctor] RESULT=failed'
exit 1