# ============================================================
# 在 ubuntu:22.04（受支持的最老基座）内构建可分发产物
#
# 为什么必须在容器里构建（2026-09-23 实测）：
#   本项目要随包分发 9 个系统二进制（xdotool / Xephyr / xclip / wmctrl / xrandr /
#   i3 / dbus-daemon / at-spi2-registryd / tesseract）。在 Ubuntu 24.04 上取到的
#   这批二进制要求 GLIBC_2.38，而 Claude Desktop Linux 的受支持底线是
#   Ubuntu 22.04 LTS+ / Debian 12+（glibc 2.35）——直接打包会恰好排除掉最低档。
#   在 22.04 上构建，产物才能向前兼容 22.04 → 24.04+。
#
# 为什么用 conda 装 pygobject：
#   PyGObject 在 PyPI 上**只有 sdist、没有任何 wheel**（实测 66 个 release 全为
#   .tar.gz），pip 安装会走源码编译。conda-forge 提供真二进制，且把 gtk/GI 栈
#   一起带进环境，不碰系统 Gi。
#
# 产物：/out/dist/（onedir + wrapper + vendor/ 系统组件 + manifest 用 JSON）
# ============================================================
set -euo pipefail

MAMBA_VER="26.7.2-0"
# pygobject 3.50 需要 Python >=3.12（与本项目 requires-python 一致）
PY_VER="3.12"

# ── 每步计时 ─────────────────────────────────────────────────────────────
# 为什么要有这个：整条流水线约 19 分钟，而**其中绝大部分不是我们的代码** ——
# 实测 apt（两步共 238 个包 / 115 MB）就占掉一半以上，且每次构建都从零开始下。
# 没有计时的话，「为什么这么慢」只能靠翻文件 mtime 反推（真这么干过一次），
# 而耗时是会随网络抖动的（实测同一份脚本两次运行差好几分钟）。
# 输出形如 `=== [1/8] ... (用时 2m24s)`，机器可读，便于事后比对。
_STEP_T0=0
_step() {                       # _step <序号> <说明>
  local now; now=$(date +%s)
  if [ "$_STEP_T0" -gt 0 ]; then
    local d=$(( now - _STEP_T0 ))
    printf '\n\033[1m── 上一步用时 %dm%02ds ──\033[0m\n' $(( d / 60 )) $(( d % 60 ))
  fi
  _STEP_T0=$now
  echo "=== [$1] $2 ==="
}
_finish() {
  local d=$(( $(date +%s) - _STEP_T0 ))
  printf '\n\033[1m── 最后一步用时 %dm%02ds ──\033[0m\n' $(( d / 60 )) $(( d % 60 ))
}

_step "1/8" "基础工具 + 项目自身需要的系统库"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# binutils 是 PyInstaller 在 Linux 上的**硬依赖**（它用 objdump 读每个二进制的
# 动态依赖来决定要收哪些库）。minimal 镜像里没有，漏了会在第 6 步报
# 「On Linux, objdump is required」—— 而那时前 5 步已经跑完，重跑代价很大。
# 这些装的是**构建期工具**，缺一个都会在中途失败（而且往往是在前几步跑完之后）：
#   binutils → PyInstaller 在 Linux 上的硬依赖（靠 objdump 读二进制的动态依赖）
#   zip      → assemble.sh 打 .mcpb（只在最后一步才用到）
#   python3  → assemble.sh 用它跑 vendor_libs.py；不能指望 PATH 里有 conda 的 python
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl bzip2 xz-utils file patchelf binutils zip python3 \
    libatspi2.0-0 gir1.2-atspi-2.0 gir1.2-freedesktop \
    >/dev/null
# gir1.2-freedesktop 显式列出（它是 gir1.2-atspi-2.0 的间接依赖，本来也会被拉进来）：
# 它提供 **DBus-1.0.typelib**，而 Atspi-2.0.typelib 的头部声明了 `...|DBus-1.0` 这个依赖。
# embed-atspi.sh 现在会把整个 typelib 闭包一起嵌进产物，少了它会在那一步直接失败。

_step "2/8" "要随包分发的 9 个系统二进制（及其运行时依赖包）"
apt-get install -y -qq --no-install-recommends \
    xdotool xserver-xephyr xclip wmctrl x11-xserver-utils \
    i3-wm dbus at-spi2-core \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    >/dev/null

_step "3/8" "安装 miniforge（conda）"
# 为什么要有「预置安装包」这条路径（2026-09-23 实测踩到）：
#   容器到 github.com 的连通性会**间歇性**失败，实测一次直接卡到
#   `curl: (28) Failed to connect to github.com port 443 after 133027 ms: Connection timed out`，
#   而同一时刻宿主与容器都能连通 —— 纯属网络抖动，却让整次构建白跑。
#   所以：① CC_CU_MINIFORGE_SH 指一个**预先下好**的安装包（挂进容器即可，完全不碰网络）；
#        ② 否则带重试下载，地址也可覆盖（CC_CU_MINIFORGE_URL，国内可指向镜像）。
MF_URL="${CC_CU_MINIFORGE_URL:-https://github.com/conda-forge/miniforge/releases/download/${MAMBA_VER}/Miniforge3-${MAMBA_VER}-Linux-x86_64.sh}"
if [ -n "${CC_CU_MINIFORGE_SH:-}" ] && [ -f "$CC_CU_MINIFORGE_SH" ]; then
  echo "  使用预置安装包: $CC_CU_MINIFORGE_SH ($(du -h "$CC_CU_MINIFORGE_SH" | cut -f1))"
  bash "$CC_CU_MINIFORGE_SH" -b -p /opt/conda >/dev/null
else
  echo "  下载 $MF_URL"
  curl -fL --retry 5 --retry-delay 5 --retry-all-errors \
       --connect-timeout 30 --max-time 900 -o /tmp/miniforge.sh "$MF_URL" || {
    echo "❌ 下载 miniforge 失败（$MF_URL）。两条出路：" >&2
    echo "   ① 有网的机器上下好，再挂进来：" >&2
    echo "        docker run ... -e CC_CU_MINIFORGE_SH=/miniforge.sh \\" >&2
    echo "                      -v <本地安装包>:/miniforge.sh:ro ..." >&2
    echo "   ② 换镜像源（实测国内速度差 16 倍：github ~0.1MB/s vs 清华 ~1.6MB/s）：" >&2
    echo "        CC_CU_MINIFORGE_URL=https://mirrors.tuna.tsinghua.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-Linux-x86_64.sh" >&2
    exit 1
  }
  bash /tmp/miniforge.sh -b -p /opt/conda >/dev/null
  rm -f /tmp/miniforge.sh
fi
export PATH="/opt/conda/bin:$PATH"
# conda / pip 也走公网，同样受抖动影响；把重试放宽（默认值在弱网下很容易整次构建失败）
conda config --set remote_max_retries 5 >/dev/null 2>&1 || true
conda config --set remote_connect_timeout_secs 30 >/dev/null 2>&1 || true

_step "4/8" "建 conda 环境并装 pygobject"
conda create -y -q -p /opt/env python="${PY_VER}" >/dev/null
conda install -y -q -p /opt/env -c conda-forge pygobject >/dev/null
PY="/opt/env/bin/python"

_step "5/8" "Python 依赖 + 本项目"
export PYTHONNOUSERSITE=1
"$PY" -m pip install -q -U pip >/dev/null
"$PY" -m pip install -q mcp cryptography pyinstaller pytest python-xlib mss pillow >/dev/null
"$PY" -m pip install -q -e /src >/dev/null

_step "6/8" "PyInstaller 打包"
export GI_TYPELIB_PATH="/usr/lib/x86_64-linux-gnu/girepository-1.0"
export LD_LIBRARY_PATH="/opt/env/lib"
cd /src
"$PY" -m PyInstaller \
  --onedir \
  --name computer-use-mcp-bin \
  --paths src \
  --distpath /out/dist \
  --workpath /out/build \
  --specpath /out/build \
  --collect-submodules gi \
  --collect-data gi \
  --collect-binaries gi \
  --collect-submodules computer_use_mcp \
  --collect-submodules mcp.server \
  --hidden-import gi.repository.Atspi \
  --hidden-import gi.repository.GLib \
  --hidden-import gi.repository.GObject \
  --hidden-import cryptography \
  --hidden-import cryptography.hazmat.backends.openssl \
  --collect-submodules Xlib \
  --hidden-import Xlib.ext.shape \
  --hidden-import Xlib.support.unix_connect \
  --copy-metadata mcp \
  --copy-metadata mcp-types \
  --copy-metadata pydantic \
  --copy-metadata anyio \
  --copy-metadata cryptography \
  --exclude-module gi.repository.Gtk \
  --exclude-module gi.repository.Gdk \
  --exclude-module gi.repository.GdkPixbuf \
  --exclude-module gi.repository.Pango \
  --exclude-module gi.repository.PangoCairo \
  --exclude-module gi.repository.GtkSource \
  --exclude-module gi.repository.GdkX11 \
  --exclude-module mcp.cli \
  --exclude-module typer \
  --exclude-module tkinter \
  entry.py

# 把 AT-SPI 的 Python 侧运行库嵌进产物（libatspi.so.0 → _internal/，
# Atspi-2.0.typelib → _internal/gi_typelibs/）。这是「目标机不需要 apt」的前提：
# 那边可能既没装 libatspi2.0-0 也没装 gir1.2-atspi-2.0。
# 落在 _internal 之下还顺带保住了安全不变量——_strip_frozen_lib_path 只剥
# _MEIPASS 之下的路径，所以它会被自动从子进程 env 里剥掉。详见脚本注释。
bash /src/packaging/embed-atspi.sh /out/dist/computer-use-mcp-bin

_step "7/8" "组装可分发目录（.mcpb / 通用目录）"
bash /src/packaging/assemble.sh /out/dist

_step "8/8" "打 .deb（Ubuntu 用户的主要分发形态）"
# 在容器里打而不是在宿主打：这边的 dpkg 就是 22.04 的那一份，打出来的包天然与
# 最低目标平台一致，不需要额外假设宿主的 dpkg-deb 行为。
# 版本号可由 CC_CU_VERSION 覆盖（与 assemble.sh 同源）。
bash /src/packaging/deb/build-deb.sh /out/dist/cc-computer-use /out/dist

# ── 把产物交还给宿主用户 ─────────────────────────────────────────────────
# 容器以 root 运行，而 /out 是**从宿主挂进来的目录** —— 不加这一步，产物在宿主上
# 属主是 root：包文件本身还能删（删文件只看父目录的写权限），但中间目录
# （computer-use-mcp-bin/ 里几百个 root 所有的文件）**没有 sudo 删不掉**，
# 于是「把产物挪出 /tmp」这个动作等于把同一个麻烦换了个地方。
# 传 CC_CU_CHOWN=<uid>:<gid> 即可让产物归宿主用户所有：
#     -e CC_CU_CHOWN="$(id -u):$(id -g)"
# 不传就保持原样（在 CI 里挂匿名卷时无所谓）。
if [ -n "${CC_CU_CHOWN:-}" ]; then
  echo "=== 收尾：把产物属主改回 $CC_CU_CHOWN（容器内是 root，宿主上不是）==="
  chown -R "$CC_CU_CHOWN" /out
fi

_finish
echo
echo "=== 构建完成，产物在 /out/dist ==="
ls -lh /out/dist/*.deb /out/dist/*.mcpb 2>/dev/null
