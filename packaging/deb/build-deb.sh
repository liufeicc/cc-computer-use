#!/usr/bin/env bash
# ============================================================
# 把组装好的分发目录打成 **.deb**
#
# 用法:
#   bash packaging/deb/build-deb.sh <分发目录> [输出目录]
#
#   <分发目录> = assemble.sh 的产物 <dist>/cc-computer-use/
#                必须含 server/（PyInstaller onedir）与 vendor/（随包系统组件）
#   环境变量:
#     CC_CU_VERSION     版本号，默认 0.1.0（Debian 修订号自动追加 -1）
#     CC_CU_MAINTAINER  维护者，默认取 git config user
#
# 设计要点（每条都是为了「目标机取不到 apt 源」这个前提）
# ------------------------------------------------------
#   · **`Depends:` 只写 `libc6 (>= 2.34)`。** 目标机没有 apt 源，任何一条依赖落空
#     都会让 `apt install ./x.deb` 直接失败且无法补救。libc6 是唯一 100% 必然存在、
#     且包名跨版本不变的依赖；`>= 2.34` 把「实测产物只需 glibc 2.34」写进控制文件
#     （22.04 是 2.35，24.04 是 2.39，都满足）。**刻意不写 Recommends** ——
#     无 apt 源时 apt 解析 Recommends 可能报 `not installable`。缺什么由
#     cc-computer-use-doctor 在运行期探测并给出可操作的提示。
#
#   · **随包的 9 个二进制只进 /opt，绝不落 /usr/bin。** 目标机只要已经装过
#     xdotool / i3 / tesseract 之类，落过去就会 `dpkg: error ... trying to overwrite`，
#     **安装直接失败**，而它没有 apt 源可以补救。同理避开 at-spi2-core 的
#     /usr/libexec/at-spi2-registryd 与 /usr/share/defaults/at-spi2/accessibility.conf。
#     /usr/bin 下只放三个**软链**，名字唯一，不会是别的包的文件。
#
#   · **压缩用 `-Zxz`。** 宿主（24.04）的 dpkg-deb 默认 zstd；22.04 实测也能读 zstd，
#     但 xz 是跨版本最保守的选择，体积也更小。配 `--root-owner-group` 免掉 fakeroot。
# ============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO/packaging/deb"

SRC="${1:?用法: build-deb.sh <分发目录> [输出目录]}"
OUT="${2:-$REPO/dist}"
PREFIX="/opt/cc-computer-use"
MCP_CMD="/usr/bin/cc-computer-use-mcp"

VERSION="${CC_CU_VERSION:-0.1.0}"
MAINTAINER="${CC_CU_MAINTAINER:-$(git -C "$REPO" config user.name 2>/dev/null || echo liufei) <$(git -C "$REPO" config user.email 2>/dev/null || echo liufeicc@users.noreply.github.com)>}"

# ── 入参校验：宁可在这里失败，也不要打出一个装到目标机才发现缺件的包 ──────────
# 布局约定（与 assemble.sh 一致）：assemble.sh 把 PyInstaller onedir 的**内容**摊进
# server/，所以可执行文件与 _internal/ 在 server/ 下**平级**。
for d in server vendor; do
  [ -d "$SRC/$d" ] || { echo "❌ $SRC/$d 不存在 —— 入参应是 assemble.sh 产出的分发目录" >&2; exit 1; }
done
[ -x "$SRC/server/computer-use-mcp-bin" ] || {
  echo "❌ $SRC/server/computer-use-mcp-bin 不存在或不可执行" >&2; exit 1; }
[ -s "$SRC/server/_internal/libatspi.so.0" ] || {
  echo "❌ 产物里没有 libatspi.so.0 —— 漏了 packaging/embed-atspi.sh 那一步，" >&2
  echo "   这样打出来的包在没装 libatspi2.0-0 的机器上会失去整个无障碍能力。" >&2; exit 1; }
[ -s "$SRC/server/_internal/gi_typelibs/Atspi-2.0.typelib" ] || {
  echo "❌ 产物里没有 Atspi-2.0.typelib —— 同上，漏了 embed-atspi.sh。" >&2; exit 1; }

BIN_COUNT="$(ls -1 "$SRC/vendor/bin" 2>/dev/null | wc -l)"
[ "$BIN_COUNT" -ge 9 ] || {
  echo "❌ vendor/bin 里只有 $BIN_COUNT 个（应为 9 个）—— 自包含分发要求一个都不能少" >&2; exit 1; }

echo "=== 从 $SRC 组装 .deb ==="
echo "  版本=$VERSION  维护者=${MAINTAINER%% <*}"
echo "  载荷: server=$(du -sh "$SRC/server" | cut -f1)  vendor=$(du -sh "$SRC/vendor" | cut -f1)"

# ── 目录骨架 ────────────────────────────────────────────────────────────
ROOT="$(mktemp -d /tmp/cc-cu-debroot.XXXXXX)"
trap 'rm -rf "$ROOT"' EXIT
mkdir -p "$ROOT/DEBIAN" \
         "$ROOT$PREFIX" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/etc/xdg/autostart" \
         "$ROOT/usr/share/doc/cc-computer-use"

echo "=== [1/5] 搬运载荷 → $PREFIX/ ==="
cp -a "$SRC/server" "$SRC/vendor" "$ROOT$PREFIX/"
mkdir -p "$ROOT$PREFIX/bin"

# 许可证：仓库根的 LICENSE（本包自身，MIT）
[ -f "$REPO/LICENSE" ] && cp "$REPO/LICENSE" "$ROOT$PREFIX/LICENSE"

# ── 模板渲染 ────────────────────────────────────────────────────────────
# 用 `|` 作 sed 分隔符：替换值都是路径，不含 `|`，不需要转义。
render() {
  local tpl="$1" dst="$2"; shift 2
  local args=()
  for kv in "$@"; do args+=(-e "s|@${kv%%=*}@|${kv#*=}|g"); done
  sed "${args[@]}" "$tpl" > "$dst"
}

echo "=== [2/5] 生成三个可执行文件（写死 $PREFIX）==="
render "$HERE/cc-computer-use-mcp.in"    "$ROOT$PREFIX/bin/cc-computer-use-mcp"    "PREFIX=$PREFIX"
render "$HERE/cc-computer-use-setup.in"  "$ROOT$PREFIX/bin/cc-computer-use-setup"  "PREFIX=$PREFIX" "MCP_BIN=$MCP_CMD"
render "$HERE/cc-computer-use-doctor.in" "$ROOT$PREFIX/bin/cc-computer-use-doctor" "PREFIX=$PREFIX" "MCP_BIN=$MCP_CMD"
chmod 0755 "$ROOT$PREFIX/bin/"*

# /usr/bin 下只放软链：名字唯一（不会是别的包的文件），真实文件留在 /opt，
# 升级时整棵载荷被整体替换、这里零变动。
for n in cc-computer-use-mcp cc-computer-use-setup cc-computer-use-doctor; do
  ln -s "$PREFIX/bin/$n" "$ROOT/usr/bin/$n"
done

echo "=== [3/5] 桌面集成与文档 ==="
cp "$HERE/autostart.desktop"    "$ROOT/etc/xdg/autostart/cc-computer-use-setup.desktop"
cp "$HERE/applications.desktop" "$ROOT/usr/share/applications/cc-computer-use-setup.desktop"
if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$ROOT/etc/xdg/autostart/cc-computer-use-setup.desktop" \
                        "$ROOT/usr/share/applications/cc-computer-use-setup.desktop" \
    && echo "  desktop 文件校验通过"
fi

DOC="$ROOT/usr/share/doc/cc-computer-use"
cp "$HERE/copyright"               "$DOC/copyright"
cp "$HERE/README.Debian"           "$DOC/README.Debian"
cp "$HERE/THIRD-PARTY-LICENSES.txt" "$DOC/THIRD-PARTY-LICENSES.txt"
# changelog.Debian 必须是 **gzip 压缩**（Debian 政策），否则 lintian 报错、apt changelog 读不了
if [ -f "$HERE/changelog" ]; then
  gzip -9nc "$HERE/changelog" > "$DOC/changelog.Debian.gz"
  chmod 0644 "$DOC/changelog.Debian.gz"
fi

echo "=== [4/5] 控制文件 ==="
# Installed-Size 单位是 KiB，口径是 debroot 去掉 DEBIAN/ 之后的大小
INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)"
render "$HERE/control.in" "$ROOT/DEBIAN/control" \
  "VERSION=${VERSION}-1" "MAINTAINER=$MAINTAINER" "INSTALLED_SIZE=$INSTALLED_SIZE"
cp "$HERE/postinst" "$ROOT/DEBIAN/postinst"; chmod 0755 "$ROOT/DEBIAN/postinst"
cp "$HERE/postrm"   "$ROOT/DEBIAN/postrm";   chmod 0755 "$ROOT/DEBIAN/postrm"

# md5sums：dpkg 用它校验载荷完整性。软链不计（dpkg 的约定）。
#
# 两个踩过的坑，都写在这儿免得下一个人重踩：
#   ① 路径必须是**相对 debroot 且不带 `./` 前缀**的形式（`opt/cc-computer-use/...`）。
#      `find -print0 | sed 's|^\./||'` 是错的 —— GNU sed 以 `\n` 分行，NUL 分隔的整串
#      会被当成**一行**，于是只有第一个条目被剥掉前缀、其余全带着 `./`，dpkg 校验对不上。
#      `find -printf '%P\n'` 直接给出规范形式，不需要再剥。
#   ② `xargs` 默认按**空白**分词，而产物里真的有带空格的文件名
#      （`setuptools/_vendor/jaraco/text/Lorem ipsum.txt`，实测报 "没有那个文件或目录"
#      并让整条管道失败）。必须 `-d '\n'` 只按换行分。
( cd "$ROOT" && find . -path ./DEBIAN -prune -o -type f -printf '%P\n' \
    | LC_ALL=C sort | xargs -d '\n' md5sum > DEBIAN/md5sums )
chmod 0644 "$ROOT/DEBIAN/md5sums"

# 数据载荷的权限归一（构建机上 umask 不同会带来不一致）
find "$ROOT$PREFIX" -type d -exec chmod 0755 {} +
find "$ROOT$PREFIX" -type f -exec chmod u+rw,go+r {} +
chmod 0755 "$ROOT$PREFIX/bin/"*  "$ROOT$PREFIX/server/computer-use-mcp-bin" \
             "$ROOT$PREFIX/vendor/bin/"*
find "$ROOT/usr" "$ROOT/etc" -type d -exec chmod 0755 {} +
chmod 0644 "$ROOT/usr/share/applications/cc-computer-use-setup.desktop" \
           "$ROOT/etc/xdg/autostart/cc-computer-use-setup.desktop"

echo "=== [5/5] 打包 ==="
mkdir -p "$OUT"
DEB="$OUT/cc-computer-use_${VERSION}-1_amd64.deb"
rm -f "$DEB"
# -Zxz：跨版本最保守的压缩（22.04 实测也能读 zstd，但 xz 体积更小、无需依赖那个回移）
# --root-owner-group：一律 root:root，免掉 fakeroot
dpkg-deb --build --root-owner-group -Zxz "$ROOT" "$DEB" >/dev/null

SIZE="$(du -h "$DEB" | cut -f1)"
echo
echo "=== 完成 ==="
echo "  $DEB  ($SIZE)"
echo
echo "安装（目标机）:  sudo apt install ./$(basename "$DEB")"
echo "装完自检:        cc-computer-use-doctor"
