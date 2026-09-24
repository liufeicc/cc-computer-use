#!/usr/bin/env bash
# ============================================================
# 专项探针：确认 .deb 里装的**冻结产物**确实带着「沙箱里操作 shell」那两条修复
#
# 用法（在仓库根目录）：
#   bash packaging/deb/probe-deb-fix.sh dist/cc-computer-use_0.1.0-1_amd64.deb
#
# 为什么不能只靠 verify-install.sh：那 10 步验的是安装 / 依赖 / 注册 / a11y 全链路，
# **不会**去调 launch_app("gnome-terminal")、也不会往终端里输入长文本，所以
# 「build 时用的是不是最新源码」这件事它证明不了。这个探针补的就是那一段。
#
# 两个探针各验一条修复，判据都是**外部可观测量**，不是工具返回的 ok：
#   ① probe_launch.py —— 在 PATH 前面放一个**假的** gnome-terminal（只记录参数），
#      看 launch_app 实际传给子进程的 argv 里有没有 --disable-factory；
#   ② probe_paste.py —— 起 xev 并把它的 WM_CLASS **改成终端的样子**（不必真装终端），
#      看 type_text 的长文本路径发出的是 ctrl+shift+v 还是裸 ctrl+v。
# ============================================================
set -euo pipefail

DEB="$(readlink -f "${1:?用法: probe-deb-fix.sh <deb 文件>}")"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$DEB" ] || { echo "❌ 找不到 $DEB" >&2; exit 1; }

# 容器里需要：装 .deb、python3 跑探针、Xvfb 当「父显示」（沙箱是嵌在已有 X server 上的
# Xephyr，没有父显示就起不来，而沙箱不可用时 launch_app 会**拒绝执行**）。
exec docker run --rm \
  -v "$DEB":/tmp/pkg.deb:ro \
  -v "$HERE/probe-in-container.sh":/tmp/probe.sh:ro \
  -v "$HERE/probe_launch.py":/tmp/probe_launch.py:ro \
  -v "$HERE/probe_paste.py":/tmp/probe_paste.py:ro \
  "${2:-ubuntu:22.04}" bash /tmp/probe.sh
