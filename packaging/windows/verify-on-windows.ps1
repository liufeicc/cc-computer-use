<#
  安装包验收（客体侧，**必须在用户的交互式会话里跑**）。

  ═══ 判据一律取外部可观测事实 ═══

  · 「装上了」→ 安装目录里那个 exe **存在且 --selftest 退出码 0**（不看安装器的返回码）；
  · 「注册上了」→ `claude mcp get` 成功 **且** `claude mcp list` 里出现它；
  · 「卸载干净了」→ 安装目录**消失** 且 `claude mcp get` **失败**。

  ═══ ⚠️ 为什么经计划任务（/IT）跑，而不是 guestcontrol 直接跑 ═══

  真实用户双击安装器时就是在**交互式会话**里跑的，验收必须走同一条路，否则测的是一个
  用户永远不会遇到的形态。计划任务 `/IT` 给的正是这个形态。

  ⚠️ **这条曾经附着一个后来被推翻的归因**：先前把 `claude` 报的
  「requires either Git for Windows (for bash) or PowerShell」记成"非交互上下文导致的，
  照字面去装 Git 会白折腾一轮"。2026-09-29 隔离实测否证了 —— 那台客体上确实两样都没装，
  装上一个（Git for Windows 或 PowerShell 7）之后在**同一个**上下文里立刻正常。
  现在的做法是 `Find-GitBash` + `CLAUDE_CODE_GIT_BASH_PATH`（见函数内的说明），
  并且单独报一条 `claude-shell` 判据，缺了就直接指向"这不是本包的问题"。

  ═══ ⏱ 超时为什给得这么宽 ═══

  `claude mcp get` / `mcp list` **不是纯读配置**：它们会真的把 MCP server 拉起来做健康
  检查（输出里那句 `Status: √ Connected` 就是这么来的）。而我们的 server 是个 250MB 的
  冻结产物，**首次冷启动**（文件缓存冷 + UIA 初始化）实测会超过 30 秒 —— 2026-09-29
  真机踩到：输出已经打印完整了，进程却还没退出，于是 30 秒的超时把它判成失败（假红）。
  实测热态 6~12 秒，冷态可能 30 秒以上，所以这类子命令一律给 120 秒。
  ✅ **别把这些超时改小**：它只在真的卡住时才付出代价，改小换来的是偶发假红。

  ═══ 输出 ═══

  逐项写进 `$Result`（UTF-8），格式：

      CHECK|<名称>|OK 或 FAIL|<详情>
      ...
      RESULT|<ok 或 failed>

  宿主侧 `verify-install.sh` 读这个文件。**不用退出码传结论**：计划任务里拿不到子进程
  的退出码，而"文件里写了什么"永远可信。
#>
[CmdletBinding()]
param(
    # 安装包路径。可经 -ParamsFile 给（**推荐**，见下），也可直接传。
    [string]$Setup = '',
    [string]$Result = 'Z:\cc-computer-use\dist\windows\verify-result.txt',
    [string]$AppDir = '',
    [switch]$KeepInstalled,

    # ⚠️ **推荐走这个**：把 `Setup=` / `Result=` 两行写进一个小文本文件，由本脚本读。
    #
    # 为什么参数不直接从命令行传：本脚本必须由**计划任务 `/IT`** 拉起（交互式会话，
    # 理由见下），而 `schtasks /create /tr "<带引号的命令行>"` 里再套一层带引号的路径，
    # 引号会被 `cmd` 吃掉 —— 实测报 `无效参数/选项 - '-NoProfile'`，任务根本没建起来。
    # 把参数挪进文件之后，`/tr` 里就只有一个**不含空格**的 .cmd 路径，
    # 嵌套引号问题从根上不存在（同款做法见 docs/PR/windows-driver/probe_*.cmd）。
    [string]$ParamsFile = ''
)

$ErrorActionPreference = 'Continue'

if ($ParamsFile) {
    if (-not (Test-Path $ParamsFile)) { throw "参数文件不存在: $ParamsFile" }
    foreach ($line in Get-Content $ParamsFile -Encoding UTF8) {
        $kv = $line.Split('=', 2)
        if ($kv.Count -ne 2) { continue }
        switch ($kv[0].Trim()) {
            'Setup' { $Setup = $kv[1].Trim() }
            'Result' { $Result = $kv[1].Trim() }
            'AppDir' { if ($kv[1].Trim()) { $AppDir = $kv[1].Trim() } }
        }
    }
}
if (-not $Setup) { throw "没给安装包路径（-Setup 或 -ParamsFile 里的 Setup=）" }

if (-not $AppDir) { $AppDir = Join-Path $env:LOCALAPPDATA 'Programs\cc-computer-use' }
$AppExe = Join-Path $AppDir 'server\computer-use-mcp-bin.exe'
$McpName = 'cc-computer-use'

# 结果文件**每次重建**：混着上一轮的记录会让"这次到底过没过"变得难查
Set-Content -Path $Result -Value '' -Encoding UTF8

function Say { param([string]$m) Add-Content -Path $Result -Value $m -Encoding UTF8 }

function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    Say ("CHECK|{0}|{1}|{2}" -f $Name, $(if ($Ok) { 'OK' } else { 'FAIL' }), $Detail)
}

function Invoke-Exe {
    <#
      跑一个可执行文件，**带超时**、按 UTF-8 取回 stdout/stderr、**拿到真实退出码**。

      ⚠️ 为什么不用 `Start-Process -PassThru`（那看起来是最自然的选择）：
      实测（2026-09-29，客体是系统自带的 Windows PowerShell **5.1**）——
      只要带了 `-RedirectStandardOutput`，返回的 Process 对象 **`$p.ExitCode` 恒为空**：

          done=True / rc-direct= / rc-after= / hasExited=True

      补调无参的 `WaitForExit()` 也没用。拿不到退出码，"自检到底过没过"就无从判断 ——
      会退化成只看它打印的那句话（而那正是本项目反复强调"不能只看 ok"的地方）。
      改用 `System.Diagnostics.Process` 直接起：退出码可靠，而且能顺手把两个流
      按 UTF-8 解码（中文 Windows 上不设就是 GBK，中文全变乱码）。

      **先起异步读、再等退出**：两个流都读完才 WaitForExit 会在子进程输出量大时死锁。

      超时那一支**必须返回一个非 0 的哨兵值**（不能顺手写 0）：写 0 的话，一次卡住的
      安装/自检会被读成"通过"，与"拿到空值"是同一类错误，只是更隐蔽。
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

    # 给 claude 指路到 bash（见 Find-GitBash）。进程级注入，不动客体环境。
    if ($GitBash) { $psi.EnvironmentVariables['CLAUDE_CODE_GIT_BASH_PATH'] = $GitBash }

    $p = [System.Diagnostics.Process]::Start($psi)
    $tOut = $p.StandardOutput.ReadToEndAsync()
    $tErr = $p.StandardError.ReadToEndAsync()
    $finished = $p.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) { try { $p.Kill() } catch { } }
    # 进程已退出（或已被杀），流会关闭 —— 这里不会挂住
    $out = try { [string]$tOut.Result } catch { '' }
    $err = try { [string]$tErr.Result } catch { '' }
    return [pscustomobject]@{
        ExitCode = if ($finished) { [int]$p.ExitCode } else { -1 }
        TimedOut = -not $finished
        Out      = $out.Trim()
        Err      = $err.Trim()
    }
}

function Find-Claude {
    $cmd = Get-Command claude -ErrorAction SilentlyContinue |
           Where-Object { $_.Source } | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($c in @(
            (Join-Path $env:APPDATA 'npm\claude.cmd')
            (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe')
            (Join-Path $env:USERPROFILE '.local\bin\claude.exe')
            (Join-Path $env:USERPROFILE '.claude\local\claude.exe')
        )) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Resolve-ClaudeRunner {
    <#
      把「找到的 claude 路径」变成一个**能真的启动**的东西。

      ⚠️ npm 全局安装会在这台机器上留下三个 shim：`claude`（给 bash 的）、
      `claude.cmd`（给 cmd 的）、`claude.ps1`（给 PowerShell 的）。
      而 `Get-Command claude` 在 PowerShell 里返回的是 **`.ps1`**，它不是可执行文件，
      直接当 FileName 起会报「不是有效的 Win32 应用程序」。

      优先走「换成同名的 `.cmd`」（本来就是给 cmd 用的批处理入口，能被 CreateProcess 起）；
      实在没有才退回 `powershell -File`。

      ⚠️ **别再把这处分流与「requires either Git for Windows …」绑在一起**：那条报错的
      真正原因是机器上确实没有 bash / PowerShell 7，与走哪条 shim 无关 —— 2026-09-29 用探针
      逐个变量隔离后否证了原先的猜测（当时以为是多包一层 powershell 导致的）。现在由
      Find-GitBash + `CLAUDE_CODE_GIT_BASH_PATH` 解决。
    #>
    param([string]$Path)
    if ($Path -and $Path.ToLower().EndsWith('.ps1')) {
        $cmdSibling = [IO.Path]::ChangeExtension($Path, '.cmd')
        if (Test-Path $cmdSibling) {
            return [pscustomobject]@{ Exe = $cmdSibling; Prefix = @(); How = "改用同名 .cmd：$cmdSibling" }
        }
        return [pscustomobject]@{
            Exe    = 'powershell.exe'
            Prefix = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Path)
            How    = "没有 .cmd 兄弟文件，退回 powershell -File $Path"
        }
    }
    return [pscustomobject]@{ Exe = $Path; Prefix = @(); How = $Path }
}

function Find-GitBash {
    <#
      找 Git for Windows 的 `bash.exe`，用来给 claude 设 `CLAUDE_CODE_GIT_BASH_PATH`。

      ⚠️ 这是 Claude Code 在 Windows 上的**硬前提**：没有 Git for Windows（bash）
      也没有 PowerShell 7 时，`claude --version` / `mcp get` / `mcp add` 一律被拒
      （2026-09-29 真机实测；系统自带的 Windows PowerShell 5.1 **不算**）。
      候选表见 setup 脚本里的同名函数 —— 三处是同一份逻辑的副本。
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

function Invoke-Claude {
    param([string]$ClaudePath, [string[]]$Arguments, [int]$TimeoutSec = 30, [string]$GitBash = '')
    $runner = Resolve-ClaudeRunner -Path $ClaudePath
    $r = Invoke-Exe -Exe $runner.Exe -Arguments (@($runner.Prefix) + $Arguments) `
        -TimeoutSec $TimeoutSec -GitBash $GitBash
    return [pscustomobject]@{
        ExitCode = if ($r.TimedOut) { 124 } else { $r.ExitCode }
        Out      = $r.Out
        Err      = $r.Err
        How      = $runner.How
    }
}

function Wait-Gone {
    <#
      轮询等一个目录真的消失。
      ⚠️ 不能 `Start-Process -Wait` 完就判：Inno 的**卸载器**会把活交给一个副本进程
      然后自己立刻退出 —— `-Wait` 返回时文件其实还没删完，紧接着的存在性检查会
      假红（Linux 侧 e2e 收尾踩过同款：`terminate()` 之后窗口要过几秒才真的从 X 上消失）。
    #>
    param([string]$Path, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Path $Path)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return -not (Test-Path $Path)
}

Say ('=' * 60)
Say ("verify on Windows: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Say ("Setup   = {0}" -f $Setup)
Say ("AppDir  = {0}" -f $AppDir)

# ── 0. 前置 ──────────────────────────────────────────────────────────
if (-not (Test-Path $Setup)) { Check 'setup-exists' $false "找不到安装包: $Setup"; Say 'RESULT|failed'; exit 1 }
Check 'setup-exists' $true $Setup

# ⚠️ **随包脚本必须能在客体这台 PS 5.1 上解析**。这一条是 2026-09-29 血证换来的：
#    体检脚本里用了 `(try { … } catch { … })`（把 try 当表达式，PS 7 才有的写法），
#    客体上**连解析都过不了** —— 整份脚本一个字都不执行，而报错消失在隐藏窗口里，
#    表现只是「体检跑不起来」。静态守卫能拦住已知的那几种写法，但拦不住"没预料到的"；
#    在这里真解析一遍，才是对**这一台机器**的确定回答。
$parseBad = 0
foreach ($f in @(
        (Join-Path $PSScriptRoot 'payload\bin\cc-computer-use-setup.ps1')
        (Join-Path $PSScriptRoot 'payload\bin\cc-computer-use-doctor.ps1')
    )) {
    if (-not (Test-Path $f)) { continue }
    $perr = $null
    $ptok = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$ptok, [ref]$perr)
    if ($perr -and $perr.Count -gt 0) {
        $parseBad++
        Say ("PARSE-ERR|{0}|line {1}: {2}" -f (Split-Path -Leaf $f), $perr[0].Extent.StartLineNumber, $perr[0].Message)
    }
}
Check 'scripts-parse-on-ps51' ($parseBad -eq 0) ("解析失败的随包脚本数 = {0}" -f $parseBad)

$claude = Find-Claude
$claudeDesc = if ($claude) { $claude } else { '(未找到)' }
Check 'claude-found' ([bool]$claude) $claudeDesc

# claude 在 Windows 上**必须有** Git for Windows(bash) 或 PowerShell 7，否则一律被拒
# （连 --version 都不给）。缺了它，下面三条注册判据必然全红，而红的原因不在这份包里 ——
# 所以单独报一条，别让人去查我们的产物。
$bash = Find-GitBash
$bashDesc = if ($bash) { $bash } else { '缺 Git for Windows / PowerShell 7' }
Check 'claude-shell' ([bool]$bash) $bashDesc

# 装之前先记录「本来有没有注册项」—— 卸载那一步要与之对比
$preexisting = $false
if ($claude) {
    $g = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'get', $McpName) -TimeoutSec 120 -GitBash $bash
    $preexisting = ($g.ExitCode -eq 0)
}
Check 'pre-state' $true ("安装前已有注册项 = {0}" -f $preexisting)

# ── 1. 安装（真实路径：静默跑安装器，让它自己的 [Run] 去注册）──────────
# ⚠️ 走 Invoke-Exe（System.Diagnostics.Process）而不是 `Start-Process -PassThru`：
#    客体 PS 5.1 上后者的 `$p.ExitCode` **恒为空**（见 Invoke-Exe 的说明），
#    而这里要用它判 `installer-exit` —— 空值 != 0，一次成功的安装会被判失败。
$inst = Invoke-Exe -Exe $Setup `
    -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-') -TimeoutSec 600
Check 'installer-exit' ($inst.ExitCode -eq 0) ("rc={0} 超时={1}" -f $inst.ExitCode, $inst.TimedOut)

$found = $false
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline -and -not (Test-Path $AppExe)) { Start-Sleep -Milliseconds 500 }
$found = Test-Path $AppExe
Check 'app-installed' $found $AppExe

# ── 2. 已安装产物能跑（外部可观测：退出码 + 它自己打的那句结论）────────
if ($found) {
    # 走 Invoke-Exe（不是 Start-Process）：它能可靠地拿到退出码，并按 UTF-8 解码两个流。
    # ⚠️ 退回到 Start-Process 的话，自检的退出码恒为空 —— 那时这条判据的 `rc=0`
    #    永远不成立，一条真通过的自检会被判失败（实测踩到过）。
    $r = Invoke-Exe -Exe $AppExe -Arguments @('--selftest') -TimeoutSec 180
    $text = $r.Out
    $etext = $r.Err
    Check 'installed-selftest-rc' ($r.ExitCode -eq 0) ("rc={0} 超时={1}" -f $r.ExitCode, $r.TimedOut)
    Check 'installed-selftest-ok' ($text -match '\[selftest\] ✅ OK') '结论行 [selftest] ✅ OK'
    Say ("SELFTEST-TAIL|" + (($text -split "`r?`n" | Select-Object -Last 6) -join ' / '))
    if ($etext.Trim()) { Say ("SELFTEST-ERR|" + $etext.Substring(0, [Math]::Min(300, $etext.Length))) }
}
else {
    Check 'installed-selftest-rc' $false '(安装目录不存在，跳过)'
    Check 'installed-selftest-ok' $false '(安装目录不存在，跳过)'
}

# ── 3. 注册（安装器的 [Run] 应该已经做过一次）────────────────────────
if ($claude) {
    $g = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'get', $McpName) -TimeoutSec 120 -GitBash $bash
    Check 'mcp-get' ($g.ExitCode -eq 0) ("rc={0} {1}" -f $g.ExitCode, ($g.Out + $g.Err).Substring(0, [Math]::Min(200, ($g.Out + $g.Err).Length)))

    # 判据取**具体的入口路径**，不取"名字出现过"：名字在别处（比如旧配置）也可能出现，
    # 而那不说明这次装的那份被注册上了。
    $registered_ok = ($g.ExitCode -eq 0) -and ($g.Out + $g.Err) -match [regex]::Escape($AppExe)
    Check 'mcp-entry-path' $registered_ok $AppExe

    $l = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'list') -TimeoutSec 120 -GitBash $bash
    $listed = $l.Out -match [regex]::Escape($McpName)
    $line = $l.Out -split "`r?`n" | Where-Object { $_ -match [regex]::Escape($McpName) } | Select-Object -First 1
    if (-not $line) { $line = '' }
    Check 'mcp-list' $listed $line
    Say ("MCP-LIST|" + (($l.Out -split "`r?`n") -join ' / '))
}
else {
    Check 'mcp-get' $false '(未找到 claude)'
    Check 'mcp-entry-path' $false '(未找到 claude)'
    Check 'mcp-list' $false '(未找到 claude)'
}

# ── 4. 卸载 ─────────────────────────────────────────────────────────
if (-not $KeepInstalled) {
    $un = Join-Path $AppDir 'unins000.exe'
    if (Test-Path $un) {
        # 同上：退出码只作诊断信息（Inno 的卸载器会把活交给临时目录里的副本进程，
        # 它自己的退出码不总是关于"删完没有"的可靠信号），判据交给下面的 Wait-Gone。
        $unr = Invoke-Exe -Exe $un -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -TimeoutSec 300
        Check 'uninstaller-ran' $true ("{0} rc={1}" -f $un, $unr.ExitCode)
    }
    else {
        Check 'uninstaller-ran' $false "找不到卸载器: $un"
    }

    $gone = Wait-Gone -Path $AppDir -TimeoutSec 90
    Check 'app-dir-removed' $gone $AppDir

    if ($claude) {
        # 给 [UninstallRun] 里那次 -Unregister 一点时间（它由卸载器同步等，但保险起见轮询）
        $removed = $false
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            $g2 = Invoke-Claude -ClaudePath $claude -Arguments @('mcp', 'get', $McpName) -TimeoutSec 120 -GitBash $bash
            if ($g2.ExitCode -ne 0) { $removed = $true; break }
            Start-Sleep -Seconds 1
        }
        Check 'mcp-unregistered' $removed 'claude mcp get 应当失败'
    }
    else {
        Check 'mcp-unregistered' $false '(未找到 claude)'
    }
}

# ── 5. 结论 ─────────────────────────────────────────────────────────
$lines = Get-Content $Result -Encoding UTF8 | Where-Object { $_ -like 'CHECK|*' }
$fails = @($lines | Where-Object { $_ -like '*|FAIL|*' })
Say ('=' * 60)
if ($fails.Count -eq 0) {
    Say "RESULT|ok"
}
else {
    Say ("RESULT|failed  ({0} 项失败)" -f $fails.Count)
}
