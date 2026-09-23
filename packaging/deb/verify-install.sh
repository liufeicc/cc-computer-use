#!/usr/bin/env bash
# ============================================================
# 在**干净容器**里验收打好的 .deb
#
# 用法:
#   bash packaging/deb/verify-install.sh <deb 文件> [基座镜像]
#   例: bash packaging/deb/verify-install.sh ~/dist/cc-computer-use_0.1.0-1_amd64.deb ubuntu:22.04
#
# 宿主机上跑 doctor 跑得再绿也说明不了问题：宿主装过全套 apt 依赖，产物缺件时会被
# **宿主自己的系统库和命令**悄悄补上。目标机器没这层兜底 —— 而本包的分发前提恰恰是
# 「目标机什么都不装」。所以验收必须在一台什么都没装的机器上做。
#
# 真正的检查逻辑在 verify-in-container.sh 里（挂进容器执行），本脚本只负责起容器。
# 拆成两个文件是为了**不用嵌套引号**：内层逻辑有 heredoc、有变量、有单引号，
# 挤进 `docker run bash -c '...'` 那一层字符串里极易写错、报错也难读。
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEB="$(readlink -f "${1:?用法: verify-install.sh <deb 文件> [基座镜像]}")"
BASE="${2:-ubuntu:22.04}"
[ -f "$DEB" ] || { echo "❌ 找不到 $DEB" >&2; exit 1; }

echo "════════════════════════════════════════════════════════"
echo " 验收 $(basename "$DEB")  @  $BASE"
echo "════════════════════════════════════════════════════════"

# 容器要能联网：① 装 xvfb/zenity/python3；② 第 4 步要 apt-get update 才能制造
# 「没有 apt 源」的对照。验收本身是开发/CI 活动，联网是合理的；
# 目标机的「无 apt 源」是在**容器内**用空 sources 模拟的（见 verify-in-container.sh）。
exec docker run --rm \
  -v "$DEB":/tmp/pkg.deb:ro \
  -v "$HERE/mcp_smoke.py":/tmp/mcp_smoke.py:ro \
  -v "$HERE/verify-in-container.sh":/tmp/verify-in-container.sh:ro \
  "$BASE" bash /tmp/verify-in-container.sh
