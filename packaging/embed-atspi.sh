#!/usr/bin/env bash
# ============================================================
# 把 AT-SPI 运行库嵌进 PyInstaller 的 onedir 产物 —— 自包含分发的前提
#
# 为什么需要（2026-09-23）
# -----------------------
# 本项目原本把 libatspi / Atspi-2.0.typelib 当作「OS 级依赖」，由 _bootstrap 在运行时
# 去系统目录里找。这在「用户自己 apt 装依赖」的场景下没问题，但 .deb 的分发前提是
# **目标机不能访问 apt 源** —— 那台机器上可能既没有 libatspi2.0-0 也没有
# gir1.2-atspi-2.0，`from gi.repository import Atspi` 直接 ImportError，无障碍能力
# 整体消失（只剩 OCR + 坐标点击那条最贵的路，而这正是本项目最忌讳的静默降级）。
#
# 为什么落在这两个位置（各有硬理由，挪了就失效）
# ----------------------------------------------
#   libatspi.so.0  →  <onedir>/_internal/
#       PyInstaller 引导器把 sys._MEIPASS（onedir 下即 _internal）设进 LD_LIBRARY_PATH，
#       所以 `dlopen("libatspi.so.0")` 能命中它。**关键在于它落在 _MEIPASS 之下**：
#       core/display/constants.py 的 _strip_frozen_lib_path() 只剥 _MEIPASS 之下的路径，
#       于是它会被自动从**子进程**的 env 里剥掉，「子进程不用产物库」这条安全不变量
#       **由构造保证**，那处代码一个字都不用改。
#       实测：LD_DEBUG=libs → `search path=<...>/_internal (LD_LIBRARY_PATH)` →
#             `trying file=<...>/_internal/libatspi.so.0`，唯一候选即命中；
#             同一进程内没有去开 /usr/lib/x86_64-linux-gnu 那份。
#
#       ⚠️ 别改成「把 libatspi 放进 vendor/lib 再设 LD_LIBRARY_PATH」：vendor/lib 不在
#          _MEIPASS 之下，剥不掉，会被**沙箱应用**继承（那里还有 libxml2/libcrypto/
#          libicu* 等约 50 个库），正是 2026-09-15/16 那两次「产物库与系统库混装」
#          事故的同一类路径。
#
#   Atspi-2.0.typelib  →  <onedir>/_internal/gi_typelibs/
#       PyInstaller 的 pyi_rth_gi 运行时钩子**无条件赋值**（不是追加）
#       `GI_TYPELIB_PATH = <_MEIPASS>/gi_typelibs`，之后 _bootstrap.setup_gi_environment()
#       只往里**追加**系统目录。所以这里是让自带 typelib 生效的**唯一**落点 —— 放在
#       别处（或指望 wrapper 设 GI_TYPELIB_PATH）会被那条钩子直接覆盖掉。
#       实测：strace -e openat → 只打开 <...>/gi_typelibs/Atspi-2.0.typelib，
#             系统的 girepository-1.0 目录一次都没被访问。
#
# 用法
# ----
#   bash packaging/embed-atspi.sh <onedir 目录>        # 例如 dist/computer-use-mcp-bin
# ============================================================
set -euo pipefail

ONEDIR="${1:?用法: embed-atspi.sh <onedir 目录，须含 _internal/>}"
INTERNAL="$ONEDIR/_internal"

if [[ ! -d "$INTERNAL" ]]; then
  echo "❌ $INTERNAL 不存在 —— 参数应是 PyInstaller onedir 产物目录本身" >&2
  exit 1
fi

# 多路径候选（与 _bootstrap.py 的 _TYPELIB_CANDIDATES / _LIB_CANDIDATES 同源）：
# 不同发行版/架构下 girepository 与 lib 目录的布局不一样，硬编码单一路径会在
# 非 amd64 或非 Debian 布局下静默取不到件。
_find_first() {
  local p
  for p in "$@"; do
    if [[ -e "$p" ]]; then printf '%s\n' "$p"; return 0; fi
  done
  return 1
}

ATSPI_LIB="$(_find_first \
  /usr/lib/x86_64-linux-gnu/libatspi.so.0 \
  /usr/lib64/libatspi.so.0 \
  /usr/lib/libatspi.so.0 \
  /usr/lib/aarch64-linux-gnu/libatspi.so.0)" || {
  echo "❌ 找不到 libatspi.so.0。构建机需先: sudo apt install libatspi2.0-0" >&2
  exit 1
}

# 注：Atspi-2.0.typelib 本身不再在这里单独解析 —— 下面的闭包遍历会找它，
# 而且缺件时给出的报错更准（能区分「缺 Atspi 本体」与「缺它的某个依赖」）。

# -L：deref 符号链接。libatspi.so.0 在 Debian 系上是 `→ libatspi.so.0.0.1` 的软链，
# 我们要的是真实文件（打包/搬运后软链会断）。typelib 是普通文件，加 -L 无害。
echo "=== 嵌入 AT-SPI 运行库（自包含分发用）==="
cp -Lf "$ATSPI_LIB" "$INTERNAL/libatspi.so.0"
mkdir -p "$INTERNAL/gi_typelibs"
echo "  libatspi.so.0      <- $ATSPI_LIB"
echo "      → $INTERNAL/libatspi.so.0"

# ── typelib：**连同它的依赖闭包一起**拷 ──────────────────────────────────
#
# 为什么不能只拷 Atspi-2.0.typelib 一个文件（2026-09-23 在干净容器里验收时抓到的）：
#   typelib 头部声明了自己的依赖，实测 `Atspi-2.0.typelib` 里是
#       GObject-2.0|GLib-2.0|DBus-1.0
#   其中 **DBus-1.0 来自 `gir1.2-freedesktop`**（不是 at-spi2-core 的包）。
#   只拷 Atspi 一个，在装了桌面的机器上照样能用 —— 因为 `_bootstrap` 会把系统的
#   typelib 目录追加进来兜底，**宿主上永远重现不了**。但目标机一旦没有
#   gir1.2-freedesktop，`from gi.repository import Atspi` 就报
#       Typelib file for namespace 'DBus', version '1.0' not found
#   整个无障碍能力消失（只剩最贵的 OCR 那条路），而界面看不出任何异常。
#
# 依赖是从 typelib 文件自身解析出来的，不是硬编码清单 —— 这样上游改了依赖关系也能自动跟上。
# 用 `tr` 把 NUL 转成换行再 grep，不依赖 `strings`。
#
# ⚠️ 三条规则都是踩出来的（2026-09-23 在干净容器里连挨两次），**别退回宽松版**：
#
#   ① **只在前 4KB 里找**。deps 是 typelib **头部**的一项，就在文件最前面
#      （实测 amd64 上偏移 ~0xa7）。整文件扫描会把后面 MB 级元数据段的随机字节
#      当成依赖 —— 实测 `GLib-2.0.typelib` 在**第 6906 行**匹配到 `i|E`，于是闭包
#      队列里多出一个叫 `i` 的「依赖」，构建在此处报「找不到依赖的 typelib: i」
#      当场失败。头部之外的匹配一律是噪声。
#
#   ② **每个元素必须带 `-版本号`**（`GObject-2.0` / `Gtk-4` 算，裸词 `i` 不算）。
#      GIR 命名空间的写法**永远**是「名字-版本」，所以这条不会误伤真依赖；而它正是
#      拦住 `i|E` 那道闸。顺带也拦住了共享库名（`libgio-2.0.so.0` —— 版本号后面
#      跟的不是 `.数字` 而是 `.so.0`，不匹配）。
#
#   ③ **两种存储格式都要认**。老格式把依赖拼成一个 `|` 分隔的串
#      （`GObject-2.0|GLib-2.0|DBus-1.0`）；新格式（gi 1.76+，conda 那份 pygobject
#      就是）改成一串**独立的 NUL 结尾串**，每串一个依赖。所以必须取头部**所有**
#      匹配的行再拆 —— 旧实现用 `grep -m1` 只取第一条，在新格式下只能拿到一个依赖，
#      漏收且**完全静默**（产物在构建机上照样能用，因为 _bootstrap 会追加系统目录兜底，
#      要到「目标机没装 gir1.2-* 」时才现形）。
#
# 输出：每行一个「名字-版本」，已排序去重。
_requires_of() {
  local elem='[A-Za-z][A-Za-z0-9_+]*-[0-9]+(\.[0-9]+)*'
  head -c 4096 "$1" 2>/dev/null \
    | tr '\0' '\n' \
    | grep -E "^${elem}(\|${elem})*$" \
    | tr '|' '\n' | sort -u || true
}

# 系统 typelib 的来源目录（与 _bootstrap.py 的候选同源）
_TYPELIB_DIRS=(
  /usr/lib/x86_64-linux-gnu/girepository-1.0
  /usr/lib64/girepository-1.0
  /usr/lib/girepository-1.0
  /usr/lib/aarch64-linux-gnu/girepository-1.0
)

_find_typelib() {          # _find_typelib <命名空间-版本>  → 打印路径，找不到返回 1
  local ns="$1" d
  for d in "${_TYPELIB_DIRS[@]}"; do
    if [[ -e "$d/$ns.typelib" ]]; then printf '%s\n' "$d/$ns.typelib"; return 0; fi
  done
  return 1
}

queue=("Atspi-2.0")
declare -A seen=()
while (( ${#queue[@]} > 0 )); do
  ns="${queue[0]}"
  queue=("${queue[@]:1}")
  [[ -n "${seen[$ns]:-}" ]] && continue
  seen[$ns]=1

  # ⚠️ 「已存在」只跳过**拷贝**，不能跳过**遍历依赖** —— 实测踩到：先手工拷了
  #    Atspi-2.0.typelib 再跑本脚本，因为「已存在」，它的依赖 DBus-1.0 根本没被入队，
  #    产物照样缺件。判重必须只作用于复制动作，依赖解析要走**已有那份**。
  if [[ -e "$INTERNAL/gi_typelibs/$ns.typelib" ]]; then
    src="$INTERNAL/gi_typelibs/$ns.typelib"
    echo "  $ns.typelib  已存在（PyInstaller 收的），跳过拷贝，仅解析它的依赖"
  else
    src="$(_find_typelib "$ns")" || {
      echo "❌ 找不到依赖的 typelib: $ns" >&2
      echo "   两种可能：" >&2
      echo "   ① 构建机少装了 GIR 包 —— 照名字 apt-file 查一下（DBus-1.0 在 gir1.2-freedesktop 里）；" >&2
      echo "   ② _requires_of 解析出了假依赖（名字看着不像 GIR 命名空间就是这个原因，" >&2
      echo "      该修的是解析规则，不是往构建机上装包）。" >&2
      exit 1
    }
    cp -Lf "$src" "$INTERNAL/gi_typelibs/$ns.typelib"
    echo "  $ns.typelib  <- $src"
  fi

  # deps 是「每行一个」的多行文本（新格式下每个依赖一行）—— 逐行入队，别当单个名字用。
  deps="$(_requires_of "$src")"
  if [[ -n "$deps" ]]; then
    while IFS= read -r _one; do
      [[ -n "$_one" ]] && queue+=("$_one")
    done <<< "$deps"
  fi
done
echo "  → $INTERNAL/gi_typelibs/（共 $(ls -1 "$INTERNAL/gi_typelibs" | wc -l) 个）"

# 自查：两样都得真的在（copy 静默失败过一次就很难从上游症状反查）
for f in "$INTERNAL/libatspi.so.0" "$INTERNAL/gi_typelibs/Atspi-2.0.typelib"; do
  if [[ ! -s "$f" ]]; then
    echo "❌ 嵌入后 $f 不存在或为空" >&2
    exit 1
  fi
done
echo "  ✅ 两样均已就位"
