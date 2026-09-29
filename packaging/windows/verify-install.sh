#!/usr/bin/env bash
# ============================================================
# cc-computer-use —— Windows 安装包验收（宿主侧壳）
#
# 一条命令跑完：装 → 自检 → 查注册 → 卸载 → 查注册被摘掉。
# 真正的验收逻辑在客体侧（packaging/windows/verify-on-windows.ps1），
# 这里只负责把脚本与安装包路径送进去、把结果取回来。
#
# ═══ ⚠️ 为什么必须经计划任务（/IT）跑 ═══
#
# 实测（2026-09-28）：客体上的 `claude` 在 **guestcontrol 直接拉起**的上下文里会报
# 「Claude Code on Windows requires either Git for Windows (for bash) or PowerShell」，
# 而同一台机器、同一个用户走**计划任务 `/IT`** 时完全正常。真实用户双击安装器走的就是
# 交互式会话那条路，验收必须走同一条，否则测的是一个用户永远遇不到的形态。
#
# 计划任务拿不到子进程退出码，所以结论**从结果文件读**（`CHECK|名称|OK|…` / `RESULT|…`），
# 不看退出码 —— 这也是本项目「判据要外部可观测」的一贯做法。
#
# 用法:
#   CC_CU_VM_PASS='<密码>' bash packaging/windows/verify-install.sh [<setup.exe 路径>]
#   （不给路径时自动取 dist/windows/ 下最新的那个）
#
# 环境变量（与 build-windows.sh 同名同义）:
#   CC_CU_VM / CC_CU_VM_USER / CC_CU_VM_PASS / CC_CU_GUEST_REPO
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VM="${CC_CU_VM:-Windows11}"
VM_USER="${CC_CU_VM_USER:-lf}"
VM_PASS="${CC_CU_VM_PASS:-}"
GUEST_REPO="${CC_CU_GUEST_REPO:-Z:\\cc-computer-use}"
TASK_NAME="CCVerifyInstall"
# 装+自检+注册+卸载，给足时间（自检本身要跑一次真 AT-SPI/UIA 初始化）
RUN_TIMEOUT="${CC_CU_VERIFY_TIMEOUT:-600}"

die() { printf '❌ %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

[[ -n "$VM_PASS" ]] || die "必须显式提供客体密码：CC_CU_VM_PASS='<密码>' bash $0
   （本项目约定：密码不写进任何仓库文件，只存在于调用者的环境里）"

# 安装包：显式给了就用，否则取 dist/windows 下最新的
SETUP_LOCAL="${1:-}"
if [[ -z "$SETUP_LOCAL" ]]; then
  SETUP_LOCAL="$(ls -t dist/windows/*-setup.exe 2>/dev/null | head -1 || true)"
fi
[[ -n "$SETUP_LOCAL" && -f "$SETUP_LOCAL" ]] || die "找不到安装包。先跑 build-windows.sh，或显式传路径。"
SETUP_NAME="$(basename "$SETUP_LOCAL")"
SETUP_GUEST="${GUEST_REPO}\\dist\\windows\\${SETUP_NAME}"
RESULT_LOCAL="$ROOT/dist/windows/verify-result.txt"
RESULT_GUEST="${GUEST_REPO}\\dist\\windows\\verify-result.txt"

info "=== Windows 安装包验收 ==="
info "  虚拟机 : $VM"
info "  安装包 : $SETUP_LOCAL"

command -v VBoxManage >/dev/null 2>&1 || die "找不到 VBoxManage"
VBoxManage list runningvms 2>/dev/null | grep -q "\"$VM\"" || die "虚拟机 $VM 没在运行"

g_run() {
  local exe="$1"; shift
  timeout "$RUN_TIMEOUT" VBoxManage guestcontrol "$VM" run \
    --exe "$exe" --username "$VM_USER" --password "$VM_PASS" \
    --wait-stdout --wait-stderr -- "$@"
}

# ── 1. 写参数文件、建 /IT 计划任务并触发 ──────────────────────────────
# 参数**经文件传**，不塞进 `/tr` 的命令行里：`/tr "<命令行>"` 里再套一层带引号的路径,
# 引号会被 cmd 吃掉 —— 实测报「无效参数/选项 - '-NoProfile'」，任务根本没建起来。
# 隔一层不含空格的 .cmd（`verify-interactive.cmd`）之后这个问题从根上不存在。
#
# 结果文件先清掉：否则「脚本根本没跑起来」会被上一轮的残留伪装成"跑过了"。
rm -f "$RESULT_LOCAL"
printf 'Setup=%s\nResult=%s\n' "$SETUP_GUEST" "$RESULT_GUEST" > "$ROOT/dist/windows/verify-params.txt"

info
info "=== [1/3] 建交互式计划任务并触发 ==="
g_run 'C:\Windows\System32\cmd.exe' /c \
  "schtasks /create /tn $TASK_NAME /tr \"${GUEST_REPO}\\packaging\\windows\\verify-interactive.cmd\" /sc once /st 23:59 /it /f & schtasks /run /tn $TASK_NAME" \
  | tail -3

# ── 2. 等结果文件出现并稳定下来 ──────────────────────────────────────
info
info "=== [2/3] 等客体跑完（最多 ${RUN_TIMEOUT}s）==="
deadline=$(( $(date +%s) + RUN_TIMEOUT ))
last=""
while [[ $(date +%s) -lt $deadline ]]; do
  sleep 10
  if [[ -f "$RESULT_LOCAL" ]] && grep -q '^RESULT|' "$RESULT_LOCAL" 2>/dev/null; then
    last="$(cat "$RESULT_LOCAL")"
    [[ "$last" == *"RESULT|ok"* || "$last" == *"RESULT|failed"* ]] && break
  fi
  printf '  … %.0fs\n' "$(( $(date +%s) - (deadline - RUN_TIMEOUT) ))"
done

# ── 3. 清理任务，报告结论 ────────────────────────────────────────────
g_run 'C:\Windows\System32\cmd.exe' /c "schtasks /delete /tn $TASK_NAME /f" >/dev/null 2>&1 || true

info
info "=== [3/3] 客体验收结果 ==="
if [[ ! -f "$RESULT_LOCAL" ]]; then
  die "客体没有产出结果文件（$RESULT_GUEST）—— 验收脚本没跑起来，或计划任务被拒。"
fi
cat "$RESULT_LOCAL"

info
if grep -q '^RESULT|ok' "$RESULT_LOCAL"; then
  info "✅ 验收通过"
  exit 0
fi
info "❌ 验收未通过（逐条见上面 CHECK 行）"
exit 1
