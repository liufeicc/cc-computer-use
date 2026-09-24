#!/usr/bin/env bash
# ============================================================
# 在**干净容器内**验收 .deb（由 verify-install.sh 挂进来执行；也可以手工 docker run 调用）
#
# 独立成文件而不是塞进 verify-install.sh 的 `docker run bash -c '...'` 里，是为了
# **不用嵌套引号**：那段逻辑里有 heredoc、有单引号、还有 shell 变量，挤在一层字符串里
# 极易写错且报错难读（第一版就是这么写的，改到第三处就崩了）。
#
# 前提（由调用方准备）：
#   /tmp/pkg.deb        待验收的 .deb
#   /tmp/mcp_smoke.py   验收用的 MCP 客户端（只用标准库，见该文件抬头）
# ============================================================
set -euo pipefail

DEB=/tmp/pkg.deb
SMOKE=/tmp/mcp_smoke.py
[ -f "$DEB" ] || { echo "❌ 找不到 $DEB" >&2; exit 1; }

step() { printf '\n\033[1m── %s ─────────────────────────────\n\033[0m' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
die()  { printf '  ❌ %s\n' "$*" >&2; exit 1; }

step "[1] 安装"
# ⚠️ 先把 Docker 镜像自带的「文档瘦身」规则关掉（`/etc/dpkg/dpkg.cfg.d/excludes`，
# 实测 ubuntu:22.04 / 24.04 镜像都有）：
#     path-exclude=/usr/share/doc/*
#     path-include=/usr/share/doc/*/copyright
#     path-include=/usr/share/doc/*/changelog.*
#   **真实 Ubuntu 安装里没有这条规则**（本机查过），它纯粹是 Docker 官方镜像为瘦身加的。
#   不关掉的话，dpkg 会跳过 README.Debian / THIRD-PARTY-LICENSES.txt，验收就会报一个
#   与我们的包毫无关系的「缺文件」—— 第一版正是这么被绊住的。
if [ -e /etc/dpkg/dpkg.cfg.d/excludes ]; then
  echo "  （关闭镜像自带的 path-exclude 文档瘦身规则，让它表现得像真实 Ubuntu）"
  mv /etc/dpkg/dpkg.cfg.d/excludes /etc/dpkg/dpkg.cfg.d/excludes.disabled
fi
# --no-install-recommends：本包刻意没有 Recommends（无 apt 源时解析它可能直接报错）
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends "$DEB" >/dev/null
dpkg-query -W -f='  Package=${Package}  Version=${Version}  Arch=${Architecture}\n' cc-computer-use
ok "安装成功"

step "[2] 载荷落位"
for p in /opt/cc-computer-use/server/computer-use-mcp-bin \
         /opt/cc-computer-use/server/_internal/libatspi.so.0 \
         /opt/cc-computer-use/server/_internal/gi_typelibs/Atspi-2.0.typelib \
         /opt/cc-computer-use/vendor/bin/at-spi2-registryd \
         /opt/cc-computer-use/vendor/bin/tesseract \
         /opt/cc-computer-use/vendor/lib \
         /opt/cc-computer-use/vendor/tessdata/chi_sim.traineddata \
         /opt/cc-computer-use/vendor/at-spi2/accessibility.conf \
         /usr/bin/cc-computer-use-mcp /usr/bin/cc-computer-use-setup \
         /usr/bin/cc-computer-use-doctor \
         /etc/xdg/autostart/cc-computer-use-setup.desktop \
         /usr/share/doc/cc-computer-use/THIRD-PARTY-LICENSES.txt; do
  [ -e "$p" ] || die "缺 $p"
done
ok "关键文件齐全"

step "[3] 9 个随包命令的动态依赖（自包含分发最容易漏的一环）"
# 基线库（glibc / libX11 / GLib 栈…）刻意不打包，假定任何装了桌面的 Ubuntu 都有。
# 这里专门查「找不到」的那些 —— 精简体 / 容器里才会暴露。
missing=""
for exe in /opt/cc-computer-use/vendor/bin/*; do
  # ⚠️ 收到变量里再判，别用 `ldd | grep -q`：grep -q 命中即退出、关掉读端，
  #    ldd 还在写就吃 EPIPE，`pipefail` 下整条管线报「ldd 失败」→ if 判假 →
  #    **真的缺库被静默放过**。详见 cc-computer-use-doctor.in 里的同款注释。
  _o="$(ldd "$exe" 2>/dev/null)"
  case "$_o" in
    *"not found"*)
      missing="$missing $(basename "$exe")"
      printf '%s\n' "$_o" | grep "not found" | sed 's/^/      /' ;;
  esac
done
[ -z "$missing" ] || die "这些命令有解析不到的库:$missing"
ok "全部可解析"

step "[4] 「目标机没有 apt 源」下的安装"
# 这是本包最重要的前提，必须真的模拟一次：把 sources 指向空文件再装。
apt-get purge -y -qq cc-computer-use >/dev/null
apt-get install -y -qq --no-install-recommends \
    -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts=/dev/null \
    -o APT::Get::List-Cleanup=0 "$DEB" >/dev/null
dpkg -s cc-computer-use >/dev/null 2>&1 || die "无 apt 源时装不上"
ok "无 apt 源也能装上（Depends 只有 libc6，必然已安装）"

step "[5] 用户级配置 —— 必须由 postinst 自动完成（这才是 .deb 的主路径）"
id -u tester >/dev/null 2>&1 || useradd -m -s /bin/bash tester
TUID="$(id -u tester)"

# 装一个**有状态**的假 claude CLI：验的是「本包有没有按约定去调用它」，
# 不是 claude 本身，所以只要能回答"注册了没"就够了。
# ⚠️ 必须是**有状态**的（`mcp add` 之后 `mcp get` 要转成功），否则第二次运行会
#    再注册一遍 —— 那不是脚本不幂等，是假 CLI 在撒谎。
#
# ⚠️ 位置必须是**该用户的固定候选路径之一**（这里用 ~/.local/bin，见 setup 的
#    find_claude()），不能放 /usr/local/bin 就完事：postinst 经 `runuser -l` 跑，
#    它拿到的是 login PATH，而 npm 全局前缀这类通常只写在 .bashrc 里（非交互 login
#    shell 会立刻 return），所以**真机上 claude 常常不在 PATH 里** —— 假 CLI 也必须
#    落在同样的处境，否则测的就不是真实路径。
install -d -o tester -g tester /home/tester/.local/bin
cat > /home/tester/.local/bin/claude <<'FAKE'
#!/bin/bash
echo "$@" >> /tmp/claude-calls.log
case "$1 $2" in
  "mcp get")    [ -f /tmp/claude-registered ] && exit 0 || exit 1 ;;
  "mcp add")    touch /tmp/claude-registered; exit 0 ;;
  "mcp remove") rm -f /tmp/claude-registered; exit 0 ;;
esac
exit 0
FAKE
chmod 0755 /home/tester/.local/bin/claude
chown tester:tester /home/tester/.local/bin/claude
: > /tmp/claude-calls.log; chmod 666 /tmp/claude-calls.log
rm -f /tmp/claude-registered

# ⚠️ 这一段是**回归测试**，别删、也别改成"直接 su - 跑 setup"（2026-09-23 的教训）。
#    原先这里就是直接 `su - tester -c cc-computer-use-setup` —— 那条路是通的，
#    于是验收全绿；而**真正的安装路径**（apt → postinst → runuser → setup）从来没被
#    执行过。结果 postinst 里 `runuser -l -u tester -c ...` 这句（`-l` 与 `-u` 在
#    util-linux 里互斥，命令直接报错、什么都不做）潜伏了下来：真机装完不注册，
#    而 postinst 还无条件打印"已为该用户完成配置"。
#    现在这里走真实路径：purge 掉再装一次，让 postinst 自己去注册。
apt-get purge -y -qq cc-computer-use >/dev/null 2>&1
SUDO_USER=tester SUDO_UID="$TUID" SUDO_GID="$(id -g tester)" \
  apt-get install -y -qq --no-install-recommends "$DEB" 2>&1 \
  | grep -E "^(cc-computer-use|Setting up)" | sed 's/^/  /' || true

echo "  --- claude 实际被这样调用 ---"
sed 's/^/    /' /tmp/claude-calls.log 2>/dev/null || echo "    （一次都没被调用）"
grep -q 'mcp add' /tmp/claude-calls.log 2>/dev/null \
  || die "postinst 没有触发用户级配置（claude 一次都没被调用）"
grep -q -- "--scope user cc-computer-use -- /usr/bin/cc-computer-use-mcp" /tmp/claude-calls.log \
  || die "没有按约定注册（user scope + /usr/bin 启动器）"
[ -f /home/tester/.claude.json ] 2>/dev/null || [ -f /tmp/claude-registered ] \
  || die "假 claude 报了成功，但状态文件不在 —— 假 CLI 造假了"
ok "postinst 经 runuser 自动完成了注册（走的是真实安装路径）"

# 幂等的判据是「**没有再 add 一次**」，不是「调用日志行数不变」——
# 已注册时脚本会再调一次 `mcp get` 把现有配置打给用户看，那也是对的。
n1="$(grep -c 'mcp add' /tmp/claude-calls.log || true)"
su - tester -c "cc-computer-use-setup" >/dev/null 2>&1
n2="$(grep -c 'mcp add' /tmp/claude-calls.log || true)"
[ "$n1" = 1 ] && [ "$n2" = 1 ] || die "重复注册了（mcp add 次数 $n1 → $n2）"
ok "幂等：再跑一次没有重复注册，且保留了已有配置"

step "[6] root 必须被拒绝"
if su -c "cc-computer-use-setup" >/dev/null 2>&1; then
  die "root 竟然跑成功了 —— 它会写进 /root 的配置，必须拒绝"
fi
ok "root 被拒绝（不会污染 /root）"

step "[7] 体检（无显示环境）"
# 以 tester 跑而不是 root：doctor 要检查的正是「**这个用户**的配置对不对」
# （Claude Code 注册、无障碍开关），而它的 find_claude_bin() 会去看该用户的
# ~/.local/bin —— 用 root 跑等于在别人的 HOME 里找，永远报"未找到 claude"。
# 顺带覆盖那条曾经是死代码的兜底分支。
doctor_out="$(su - tester -c "cc-computer-use-doctor" 2>&1)" || true
printf '%s\n' "$doctor_out" | sed 's/^/  /'
# ⚠️ 用 shell 模式匹配而不是 `... | grep -q`：那条管线的退出码会因 **grep 提前退出**
#    而变成「上游失败」（上游吃 EPIPE + pipefail），即使 grep 明明命中了 ——
#    实测就是这么把一次正常的 doctor 判成失败的。
case "$doctor_out" in
  *"已在 Claude Code 中注册"*) ;;
  *) die "doctor 没能确认注册状态（find_claude_bin 的兜底分支失效了？）" ;;
esac
ok "doctor 能跑，且确认了注册状态（无 DISPLAY 时跳过服务自检属正常）"

step "[8] 图形会话下跑通服务自检"
# 沙箱（isolated 模式）是一个**嵌在已有 X server 上**的 Xephyr，没有父显示起不来。
# 容器里没有图形会话，用 Xvfb 当"底座" —— 模拟的正是目标机上「用户已在 X 会话里」。
apt-get install -y -qq --no-install-recommends xvfb x11-utils zenity >/dev/null
Xvfb :99 -screen 0 1600x1000x24 >/tmp/xvfb.log 2>&1 &
sleep 3
export DISPLAY=:99
xdpyinfo >/dev/null 2>&1 || { cat /tmp/xvfb.log >&2; die "Xvfb 没起来"; }
out="$(timeout 150 /usr/bin/cc-computer-use-mcp --selftest 2>&1 || true)"
printf '%s\n' "$out" | grep -E "^\[selftest\] (backend|display|已注册工具)" | sed 's/^/  /'
# 同上：判据用模式匹配，别用 `| grep -q`（提前退出会污染管线退出码）
case "$out" in
  *"✅ OK"*) ;;
  *) printf '%s\n' "$out" | tail -30 >&2; die "服务自检失败" ;;
esac
ok "自检通过（沙箱 + 私有 a11y 总线 + i3 全链路）"

step "[9] a11y 真实可用性：起应用 → 读树 → 元素级点掉"
# 光有 --selftest 还不够：它只证明栈起来了，没证明「能读到别人的树」。
# 这一步经 MCP 协议真的操作一次界面，判据是 **zenity 进程真的退出**（不看 ok 字段）。
apt-get install -y -qq --no-install-recommends python3 >/dev/null
python3 "$SMOKE" || die "a11y 冒烟失败"
ok "全链路可用"

step "[10] 卸载"
apt-get purge -y -qq cc-computer-use >/dev/null
[ ! -e /opt/cc-computer-use ] || die "/opt/cc-computer-use 有残留"
[ ! -e /etc/xdg/autostart/cc-computer-use-setup.desktop ] \
  || die "autostart 条目还在（它没被声明成 conffile，remove 时应被真正删掉）"
ok "干净卸载（/opt 已清、autostart 条目已删）"

printf '\n\033[1m ✅ 全部通过\033[0m\n'
