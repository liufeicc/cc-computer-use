#!/usr/bin/env bash
# ============================================================
# 在 Windows 客体的**交互式会话**里跑一个探针 .bat，并把结果取回来。
#
# ═══ 为什么必须经计划任务（/IT）═══
#
# 客体上 `VBoxManage guestcontrol run` 拉起的进程落在**服务会话**：那里没有真桌面，
# UIA 根元素子级恒为 0、`PrintWindow` 返回全黑，而**什么都不报错**。
# 计划任务带 `/it` 才是交互式会话 —— 那正是真实用户跑 MCP server 的形态。
#
# ⚠️ 计划任务拿不到子进程退出码，所以结论**从客体写的日志/JSON 读**
# （探针自己会把 `EXITCODE=` 写进日志）。这是本项目「判据要外部可观测」的一贯做法。
#
# 用法:
#   CC_CU_VM_PASS='<密码>' bash tests/run_windows_probe.sh [<客体 .bat>] [<宿主日志>]
#
# 默认跑定向注入探针:
#   CC_CU_VM_PASS='<密码>' bash tests/run_windows_probe.sh
#
# 环境变量（与 packaging/windows 下的脚本同名同义）:
#   CC_CU_VM / CC_CU_VM_USER / CC_CU_VM_PASS / CC_CU_GUEST_REPO
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VM="${CC_CU_VM:-Windows11}"
VM_USER="${CC_CU_VM_USER:-lf}"
VM_PASS="${CC_CU_VM_PASS:-}"
GUEST_REPO="${CC_CU_GUEST_REPO:-Z:\\cc-computer-use}"
TASK_NAME="CCCUProbe"
RUN_TIMEOUT="${CC_CU_PROBE_TIMEOUT:-900}"

# 客体侧的 .bat（仓库相对路径，反斜杠形式）与它在宿主侧对应的日志文件
BAT_GUEST_REL="${1:-tests\\run_directed_probe.bat}"
LOG_LOCAL_REL="${2:-docs/PR/windows-driver/results/_directed_probe_console.log}"
LOG_LOCAL="$ROOT/$LOG_LOCAL_REL"

die() { printf '❌ %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

[[ -n "$VM_PASS" ]] || die "必须显式提供客体密码：CC_CU_VM_PASS='<密码>' bash $0
   （本项目约定：密码不写进任何仓库文件，只存在于调用者的环境里）"
command -v VBoxManage >/dev/null 2>&1 || die "找不到 VBoxManage"
VBoxManage list runningvms 2>/dev/null | grep -q "\"$VM\"" \
  || die "虚拟机 $VM 没在运行。先启动它，并在交互式会话里登录一次。"

g_run() {
  local exe="$1"; shift
  timeout "$RUN_TIMEOUT" VBoxManage guestcontrol "$VM" run \
    --exe "$exe" --username "$VM_USER" --password "$VM_PASS" \
    --wait-stdout --wait-stderr -- "$@"
}

info "=== 在客体交互式会话里跑探针 ==="
info "  虚拟机 : $VM"
info "  客体脚本: ${GUEST_REPO}\\${BAT_GUEST_REL}"
info "  日志     : $LOG_LOCAL_REL"

# 旧日志先删：否则「脚本根本没跑起来」会被上一轮的残留伪装成"跑过了"
rm -f "$LOG_LOCAL"

# ── 1. 注册交互式计划任务并触发 ──────────────────────────────────────
# ⚠️ **一律走 `packaging/windows/run-scheduled-task.ps1`，不要退回 `schtasks /create`。**
#
# ① ★ 电池闸门（2026-10-02 实测踩到的**静默失败**）：`schtasks /create` 装出来的任务
#    默认 `DisallowStartIfOnBatteries=true`，而 schtasks 命令行**没有任何开关能改它**。
#    宿主是笔记本且**正在用电池**时，客体随之报 `PowerLineStatus=Offline`，于是任务被
#    Task Scheduler 一直挂在**「已排队」**、**永远不启动** —— 表现只是「结果文件不出现」，
#    与"脚本自己崩了"长得一模一样（本轮为此绕了很久）。同一台客体、同一用户、
#    同为 InteractiveToken，只差电池那两行的对照见 run-scheduled-task.ps1 的注释。
# ② `schtasks /tr` 里套一层带引号的路径时引号会被 cmd 吃掉（实测报「无效参数/选项 -
#    '-NoProfile'」，任务根本没建起来）。隔一层**不含空格**的 .bat 就不会有这个问题 ——
#    那也是本仓库既有脚本一律这么做的原因。改走 PowerShell 参数化调用后这条从根上不存在，
#    但 .bat 这一层**仍然保留**（客体侧好写好查，也是各探针的统一入口）。
info
info "=== [1/3] 注册交互式计划任务并触发 ==="
reg_out="$(g_run 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' \
  -NoProfile -ExecutionPolicy Bypass \
  -File "${GUEST_REPO}\\packaging\\windows\\run-scheduled-task.ps1" \
  -Target "${GUEST_REPO}\\${BAT_GUEST_REL}" -TaskName "$TASK_NAME" 2>&1 || true)"
printf '%s\n' "$reg_out" | tail -5
printf '%s\n' "$reg_out" | grep -q 'TASKREG|' \
  || die "计划任务没注册成功（客体没回 TASKREG| 那行）—— 上面是客体原话。"
# 判据取**实际挂上去的设置**，不是"任务建出来了"：本轮踩的正是后者。
printf '%s\n' "$reg_out" | grep -q 'on_batteries_disallow=False' \
  || die "★ 任务带着电池闸门装上了 —— 宿主用电池时它会被一直排队、永远不跑。
   去修 packaging/windows/run-scheduled-task.ps1，别在这里绕过去。"
# ② ★ 必须**隐藏窗口**起目标（经 run-hidden.vbs）：直接起 .bat 会带出一个控制台窗口，
#    而那个窗口会成为**用户桌面的前台窗口** —— 隔离 story 里「用户的前台窗口没变」
#    那条判据于是为一件与产品无关的事变红。实测同一份代码连跑两轮：
#    '新通知'→'新通知' 通过，下一轮 'Program Manager'→'C:\WINDOWS\SYSTEM32\cmd.exe' 失败。
printf '%s\n' "$reg_out" | grep -q 'hidden_launcher=True' \
  || die "★ 任务没有走隐藏启动器 —— 它会在用户桌面上弹一个控制台窗口并抢前台，
   让隔离 story 的『前台没变』判据假红。去修 packaging/windows/run-scheduled-task.ps1。"

# ── 2. 等客体跑完 ────────────────────────────────────────────────────
# 完成信号是探针自己在日志末尾写的 `=== DONE ===`（由 .bat 落笔）。
# ⚠️ 不能只看"文件存在"：.bat 一开头就把日志建出来了。
info
info "=== [2/3] 等客体跑完（最多 ${RUN_TIMEOUT}s）==="
start=$(date +%s)
deadline=$(( start + RUN_TIMEOUT ))
while [[ $(date +%s) -lt $deadline ]]; do
  sleep 10
  if [[ -f "$LOG_LOCAL" ]] && grep -q '=== DONE ===' "$LOG_LOCAL" 2>/dev/null; then
    break
  fi
  printf '  … %ss\n' "$(( $(date +%s) - start ))"
done

# ── 3. 清理任务，报告结论 ────────────────────────────────────────────
g_run 'C:\Windows\System32\cmd.exe' /c "schtasks /delete /tn $TASK_NAME /f" >/dev/null 2>&1 || true

info
info "=== [3/3] 探针输出 ==="
if [[ ! -f "$LOG_LOCAL" ]]; then
  die "客体没有产出日志（$LOG_LOCAL_REL）—— 探针没跑起来，或计划任务被拒。"
fi
cat "$LOG_LOCAL"

info
if grep -q '=== DONE ===' "$LOG_LOCAL"; then
  code="$(grep -o 'EXITCODE=[0-9-]*' "$LOG_LOCAL" | tail -1 | cut -d= -f2 || true)"
  case "${code:-}" in
    0) info "✅ 探针全过（EXITCODE=0）" ;;
    3) info "⚠️ 会话不可信（EXITCODE=3）—— 客体没在交互式会话里，结论无效，请重新登录后重跑" ;;
    *) info "❌ 探针有失败项（EXITCODE=${code:-未知}）—— 看上面输出" ;;
  esac
  exit 0
fi
die "日志里没有 DONE 标记 —— 探针中途死了（客体上看 $LOG_LOCAL_REL 的最后几行）"
