#!/usr/bin/env bash
# ============================================================
# cc-computer-use —— Windows 分发构建（宿主侧总入口）
#
# 一条命令走完：解析版本 → 备齐客体上的工具链 → 在客体上构建冻结产物与安装包
# → 产物落回 dist/windows/。
#
# 为什么必须有这么一个宿主侧脚本，而不是"照文档手敲几条 guestcontrol"：
#   构建发生在 Windows 客体上（PyInstaller **不能交叉编译**），而客体是被
#   `VBoxManage guestcontrol` 驱动的。手敲意味着每次都要记住：客体 python 在哪、
#   共享目录挂到哪、Inno Setup 的 ISCC 在哪、密码从哪来 —— 少一个就是一次
#   「跑了一半才发现」的失败。这里把它固化成一条命令。
#
# 用法:
#   CC_CU_VM_PASS='<密码>' bash packaging/windows/build-windows.sh
#
# 环境变量（都有默认值，换机器时覆盖）:
#   CC_CU_VM          虚拟机名，默认 Windows11
#   CC_CU_VM_USER     客体用户名，默认 lf
#   CC_CU_VM_PASS     ⚠️ **必填**，客体用户密码。刻意不从任何文件里读 ——
#                     密码只存在于调用者的环境里（见 CLAUDE.md 的约定）
#   CC_CU_VM_PY       客体 Python，默认 C:\Users\lf\py312\python.exe
#   CC_CU_VERSION     版本号，默认从 pyproject.toml 解析（单一来源）
#   CC_CU_SKIP_INNO   非空则不去检查/安装客体上的 Inno Setup
#
# 产物:
#   dist/windows/cc-computer-use-<版本>-setup.exe     安装包
#   dist/windows/build.log                            客体侧完整构建日志（UTF-8）
#   dist/windows/payload/                             安装包的输入目录（可复查）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VM="${CC_CU_VM:-Windows11}"
VM_USER="${CC_CU_VM_USER:-lf}"
VM_PASS="${CC_CU_VM_PASS:-}"
# 客体上的路径一律用 Windows 形式；共享目录把仓库挂成 Z:
GUEST_REPO="${CC_CU_GUEST_REPO:-Z:\\cc-computer-use}"
GUEST_PY="${CC_CU_VM_PY:-C:\\Users\\lf\\py312\\python.exe}"
GUEST_PS='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
GUEST_SCRIPT="${CC_CU_GUEST_REPO:-Z:\\cc-computer-use}\\packaging\\windows\\build-on-windows.ps1"

OUT="$ROOT/dist/windows"
TOOLS="$OUT/_tools"
# 构建本身要好几分钟（PyInstaller 收上万文件 + lzma2/max 压 150MB），超时给足
BUILD_TIMEOUT="${CC_CU_BUILD_TIMEOUT:-3600}"

die() { printf '❌ %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

# ── 版本号：单一来源 pyproject.toml ────────────────────────────────
# Linux 侧是「CC_CU_VERSION 环境变量，兜底写死 0.1.0」，于是版本号散在
# pyproject.toml / changelog / 脚本默认值三处、靠手工同步。这里直接读 pyproject，
# 少一处会漂的地方。
VERSION="${CC_CU_VERSION:-}"
if [[ -z "$VERSION" ]]; then
  VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' pyproject.toml | head -1)"
fi
[[ -n "$VERSION" ]] || die "解析不出版本号：pyproject.toml 里没有 version = \"x.y.z\"，也未见 CC_CU_VERSION"

# ── 前置检查（都在**开始构建之前**，不要跑到一半才发现）────────────
[[ -n "$VM_PASS" ]] || die "必须显式提供客体密码：CC_CU_VM_PASS='<密码>' bash $0
   （本项目约定：密码不写进任何仓库文件，只存在于调用者的环境里）"
command -v VBoxManage >/dev/null 2>&1 || die "找不到 VBoxManage（装 VirtualBox 或在 PATH 里加它）"

info "=== cc-computer-use Windows 构建 ==="
info "  虚拟机   : $VM"
info "  客体仓库 : $GUEST_REPO"
info "  客体 Python: $GUEST_PY"
info "  版本     : $VERSION"

# 客体必须在跑、且用户已登录 —— UIA 与抓屏都要求真桌面，而 guestcontrol 本身
# 也只在「已登录用户的交互式会话」里才有意义（CLAUDE.md 里记过这条）。
if ! VBoxManage list runningvms 2>/dev/null | grep -q "\"$VM\""; then
  die "虚拟机 $VM 没在运行。先起它并等用户登录：
  VBoxManage startvm $VM --type gui     # 必须 GUI 模式，headless/VRDE 拿不到交互式会话"
fi
loggedin="$(VBoxManage guestproperty get "$VM" '/VirtualBox/GuestInfo/OS/LoggedInUsers' 2>/dev/null | sed -n 's/^Value: //p')"
if [[ "${loggedin:-0}" == "0" ]]; then
  die "客体上没有已登录的用户会话（LoggedInUsers=0）。UIA/抓屏需要真桌面，构建脚本里的自检也会因此失败。"
fi
info "  客体登录用户数: $loggedin"

mkdir -p "$OUT" "$TOOLS"

# ── 在客体上跑一条命令 ────────────────────────────────────────────
# ⚠️ 每次都显式带 --password：guestcontrol **不继承**调用者的环境变量，
#    漏了就报 "The specified user was not able to logon on guest" ——
#    那句报错读起来像"密码错了"，其实是"变量是空的"（本项目实测踩过）。
g_run() {
  local exe="$1"; shift
  timeout "$BUILD_TIMEOUT" VBoxManage guestcontrol "$VM" run \
    --exe "$exe" --username "$VM_USER" --password "$VM_PASS" \
    --wait-stdout --wait-stderr -- "$@"
}

# 客体侧探测 ISCC 在哪 —— **不在这里自己拼路径做存在性检查**。
# 理由见 build-on-windows.ps1 的 Find-Iscc 注释：经 guestcontrol 传内联 cmd 命令时，
# `if exist "<带空格的路径>"` 的引号会被吃掉，判据恒假且不报错（实测踩过）。
# 让客体脚本自己判断、只回一行 `ISCC=<路径|NONE>`，宿主只做正则解析。
g_probe_iscc() {
  g_run "$GUEST_PS" -NoProfile -ExecutionPolicy Bypass -File "$GUEST_SCRIPT" -ProbeOnly 2>/dev/null \
    | tr -d '\r' | grep -o 'ISCC=.*' | head -1 || true
}

# ── 客体工具链：Inno Setup（免管理员，/CURRENTUSER）──────────────────
# 为什么不用默认安装：Inno Setup 的安装器**请求管理员权限**，而 guestcontrol
# 起的进程是 UAC 过滤令牌（Medium 完整性）—— 实测它会在提权那一步直接退出码 2、
# 连自己的日志都不写。`/CURRENTUSER` 让安装器以当前用户身份装到
# %LOCALAPPDATA%\Programs，恰好也是本包自己安装时走的形态。
ensure_iscc() {
  local probe; probe="$(g_probe_iscc)"
  if [[ "$probe" == ISCC=* && "$probe" != "ISCC=NONE" ]]; then
    info "  客体 ISCC: ${probe#ISCC=}"
    return 0
  fi
  if [[ -n "${CC_CU_SKIP_INNO:-}" ]]; then
    die "客体上没有 ISCC，且设了 CC_CU_SKIP_INNO —— 无法打安装包"
  fi

  info "  客体上没有 ISCC，开始准备 Inno Setup 编译器"
  local local_exe="$TOOLS/innosetup-6.7.3.exe"
  if [[ ! -s "$local_exe" ]]; then
    info "    下载 innosetup-6.7.3.exe（经 ghfast.top 代理；官网直连在本机超时）"
    # ⚠️ 判据是**字节数**，不是 curl 的返回码：实测第一次拿到的是 HTTP 200 的
    #    **截断文件**（1.1MB / 应为 10.6MB），curl 报成功、"file" 也认成 PE32。
    #    与 CLAUDE.md 里记的「镜像返回 HTML 错误页也是 200」是同一类坑。
    local expect=10592232
    local got=0
    for attempt in 1 2 3; do
      curl -sSL -C - -o "$local_exe" \
        "https://ghfast.top/https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe" || true
      got="$(stat -c%s "$local_exe" 2>/dev/null || echo 0)"
      if [[ "$got" == "$expect" ]]; then break; fi
      info "    第 $attempt 次拿到 $got 字节（应为 $expect），续传重试"
    done
    [[ "$got" == "$expect" ]] || die "Inno Setup 安装包下载不完整（$got / $expect 字节）"
  fi
  info "    安装器已就位: $local_exe"

  # 客体经共享目录读它并静默安装。/CURRENTUSER 不能省，理由见上面注释。
  g_run "${GUEST_REPO}\\dist\\windows\\_tools\\innosetup-6.7.3.exe" \
    /CURRENTUSER /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- \
    "/LOG=C:\\Users\\${VM_USER}\\inno_setup_install.log" >/dev/null

  probe="$(g_probe_iscc)"
  [[ "$probe" == ISCC=* && "$probe" != "ISCC=NONE" ]] \
    || die "装完仍找不到 ISCC。客体日志：C:\\Users\\${VM_USER}\\inno_setup_install.log"
  info "    安装完成: ${probe#ISCC=}"
}

info
info "=== [1/2] 客体工具链 ==="
ensure_iscc

# ── 客体构建 ──────────────────────────────────────────────────────
info
info "=== [2/2] 客体构建（冻结产物 + 安装包）==="
info "  这一步要几分钟（PyInstaller 收上万文件，安装包再 lzma2/max 压一遍）"
info "  完整日志会落在 dist/windows/build.log（UTF-8）"

# stdout 上只有 ASCII 进度（客体是中文 Windows，中文经管道回来看必是乱码 ——
# 中文诊断全部走 UTF-8 日志文件，那条约定写在 build-on-windows.ps1 的文件头）
set +e
g_run "$GUEST_PS" -NoProfile -ExecutionPolicy Bypass \
  -File "$GUEST_SCRIPT" \
  -RepoRoot "$GUEST_REPO" -Python "$GUEST_PY" -Version "$VERSION"
rc=$?
set -e

if [[ -f "$OUT/build.log" ]]; then
  info
  info "─── 客体构建日志（尾部 40 行）───"
  tail -40 "$OUT/build.log"
fi

if [[ $rc -ne 0 ]]; then
  die "客体构建失败（退出码 $rc）。完整日志：dist/windows/build.log"
fi

SETUP="$OUT/cc-computer-use-$VERSION-setup.exe"
[[ -f "$SETUP" ]] || die "构建自称成功，但找不到安装包：$SETUP"

info
info "=== 完成 ==="
ls -lh "$SETUP"
info
info "下一步（安装 + 注册 + 验收）："
info "  CC_CU_VM_PASS='...' bash packaging/windows/verify-install.sh $SETUP"