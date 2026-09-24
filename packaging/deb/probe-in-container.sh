#!/usr/bin/env bash
# ============================================================
# 探针的**容器内**部分（由 probe-deb-fix.sh 挂进来执行）
#
# 与 verify-in-container.sh 拆成两层是同一个理由：不用嵌套引号。那段逻辑里有 heredoc、
# 有单引号、还有 shell 变量，挤在一层字符串里极易写错且报错难读。
# ============================================================
set -uo pipefail

step() { printf '\n\033[1m── %s ─────────────────────────────\n\033[0m' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
die()  { printf '  ❌ %s\n' "$*" >&2; exit 1; }

step "[0] 前置：装 .deb + python3 + Xvfb"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends /tmp/pkg.deb python3 xvfb x11-utils >/dev/null 2>&1
ok "已安装"

# ⚠️ 必须先给一个「父显示」：isolated 模式下的沙箱是**嵌在已有 X server 上的 Xephyr**，
#    没有父显示就起不来；而 _require_sandbox 闸门在沙箱不可用时是**拒绝执行**的
#    （刻意不回落宿主桌面），于是 launch_app 会直接报「隔离沙箱不可用」。
#    容器里用 Xvfb 当那块底座，模拟的正是目标机上「用户已在 X 会话里」。
Xvfb :99 -screen 0 1600x1000x24 >/tmp/xvfb.log 2>&1 &
sleep 3
export DISPLAY=:99
xdpyinfo >/dev/null 2>&1 || { cat /tmp/xvfb.log >&2; die "Xvfb 没起来"; }
ok "父显示就绪（Xvfb :99）"

step "[1] 去单实例参数：launch_app 实际传给子进程的 argv"
# 假 gnome-terminal：只把收到的参数写进文件，不真开窗。
# 要验的是「本包有没有按约定去调用它」，不是 gnome-terminal 本身。
mkdir -p /tmp/fakebin
cat > /tmp/fakebin/gnome-terminal <<'FAKE'
#!/bin/bash
printf '%s\n' "$@" > /tmp/gt-args.txt
exit 0
FAKE
chmod +x /tmp/fakebin/gnome-terminal
export PATH="/tmp/fakebin:$PATH"
rm -f /tmp/gt-args.txt
python3 /tmp/probe_launch.py || die "launch 探针脚本失败"

[ -f /tmp/gt-args.txt ] || die "假 gnome-terminal 根本没被调用（launch_app 没走到 Popen？）"
echo "  子进程实际收到的参数："
sed 's/^/    /' /tmp/gt-args.txt
grep -qx -- "--disable-factory" /tmp/gt-args.txt \
  || die "argv 里没有 --disable-factory —— 这个包里的产物**没有**本轮修复（多半打了旧源码）"
ok "argv 里带 --disable-factory —— 修复确实在这个 .deb 里"

step "[2] 终端里的粘贴键：xev 记录到的按键组合"
# 这一步会再开一个 MCP 连接，和上面那个是独立进程（各自一块沙箱屏），互不干扰。
python3 /tmp/probe_paste.py || die "paste 探针失败"
ok "长文本路径走的是 ctrl+shift+v —— 修复确实在这个 .deb 里"

printf '\n\033[1m ✅ 两个探针都通过\033[0m\n'
