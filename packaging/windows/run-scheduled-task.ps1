<#
.SYNOPSIS
    在客体的**交互式会话**里注册一个计划任务，并立刻触发它（客体侧公共工具）。

.DESCRIPTION
    本仓库有三处要在客体上「以交互式会话的形态跑一段脚本」：
    `tests/run_windows_probe.sh`、`packaging/windows/verify-install.sh`、
    以及将来任何同类的驱动脚本。它们**共用这一份**注册逻辑，理由见下。

    ═══ 为什么必须是计划任务（而不是 guestcontrol 直接拉起）═══

    `VBoxManage guestcontrol run` 拉起的进程落在**服务会话**（session 0）：那里没有真桌面，
    UIA 根元素子级恒为 0、`PrintWindow` 返回全黑，而**什么都不报错**。
    真实用户跑 MCP server 走的是交互式会话，所以驱动脚本必须也走那一条。

    ═══ ⚠️ 为什么不用 `schtasks /create` ═══

    两个理由，第二个是 2026-10-02 实测踩到的**静默失败**：

    1. `/tr "<带引号的命令行>"` 里再套一层带引号的路径时引号会被 cmd 吃掉 ——
       实测报「无效参数/选项 - '-NoProfile'」，任务根本没建起来。走 PowerShell 的
       参数化调用从构造上不存在这个问题。

    2. **`schtasks /create` 装出来的任务默认 `DisallowStartIfOnBatteries=true`**，
       而 `schtasks` 命令行**没有任何开关能改它**（只能导 XML 或走这边）。
       后果：宿主是笔记本、且**正在用电池**时，客体随之报 `PowerLineStatus=Offline`，
       于是计划任务被 Task Scheduler 一直挂在**「已排队」**状态、**永远不启动** ——
       表现是「命令成功、任务建出来了、结果文件永远不出现」，
       与"脚本自己崩了"长得一模一样（本轮排查为此绕了很久）。
       实测对照（同一台客体、同一用户、同为 InteractiveToken，只差电池那两行）：

            schtasks /create           DisallowStartIfOnBatteries=true   → 已排队，永不运行
            Register-ScheduledTask     ...=false（本脚本）              → Ready，立刻跑完

       ⚠️ **别把这条读成"虚拟机不该报电池"**：报不报是宿主/虚拟化的事，
       而我们的驱动脚本**不该依赖宿主当前的供电状态**。所以修在注册这一侧。

    .PARAMETER Target
        要在交互式会话里跑的**可执行文件或批处理**（客体上的路径，例如
        `Z:\cc-computer-use\tests\run_iso_ab.bat`）。

    .PARAMETER TaskName
        任务名。同名任务会被覆盖（宿主侧脚本收尾时用 `schtasks /delete` 清掉）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
        -File Z:\cc-computer-use\packaging\windows\run-scheduled-task.ps1 `
        -Target Z:\cc-computer-use\tests\run_iso_ab.bat -TaskName CCCUProbe
#>

param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$TaskName
)

$ErrorActionPreference = 'Stop'

# 交互式会话要跑在**当前登录用户**名下：`-LogonType Interactive` 表示
# 「只在该用户已登录交互式会话时运行」，这正是我们要的形态（与旧的 `/it` 等价）。
$runAs = $env:USERDOMAIN + '\' + $env:USERNAME

# ⚠️ 目标**经 `run-hidden.vbs` 起**，不是直接当 Action 的 Execute —— 理由见下。
#    直接起 .bat 会带一个控制台窗口出来，而那个窗口会变成**用户桌面的前台窗口**；
#    隔离 story 里那条「用户的前台窗口没变」于是会为了一件与产品无关的事变红。
#    实测（2026-10-02，同一份代码连跑两轮）：一轮 '新通知' → '新通知' 通过，
#    下一轮 'Program Manager' → 'C:\WINDOWS\SYSTEM32\cmd.exe' 失败 ——
#    而后者正是我们自己那个批处理的控制台标题。
#    wscript.exe 无窗口（不像 cscript 会分配控制台），`Run(cmd, 0, True)` 的 0 = SW_HIDE。
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $here 'run-hidden.vbs'

$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument ('//B //NoLogo "' + $launcher + '" "' + $Target + '"')
$principal = New-ScheduledTaskPrincipal -UserId $runAs -LogonType Interactive -RunLevel Limited
# ★ 这两条是本次修复的**要害**，缺任何一个都会回到"永远排队的静默失败"。
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

# 把**实际挂上去的设置**回显出来：宿主侧据此断言"要的那一档真的生效了"。
# ⚠️ 判据必须是这个，不能是"任务建出来了" —— 本轮踩的坑正是
#    「任务建出来了、看着一切正常，可它永远不会跑」。
$t = Get-ScheduledTask -TaskName $TaskName
'TASKREG|' + $TaskName +
    '|state=' + $t.State +
    '|on_batteries_disallow=' + $t.Settings.DisallowStartIfOnBatteries +
    '|stop_on_batteries=' + $t.Settings.StopIfGoingOnBatteries +
    '|logon=' + $t.Principal.LogonType +
    '|user=' + $t.Principal.UserId +
    '|hidden_launcher=' + ($t.Actions[0].Execute -like '*wscript*')
