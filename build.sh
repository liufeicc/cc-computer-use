#!/usr/bin/env bash
# ============================================================
# PyInstaller onedir 打包脚本
#
# 产物：dist/computer-use-mcp-bin/（目录）+ dist/computer-use-mcp（薄 wrapper）
#
# 为何 onedir：onefile 每次启动都要自解压到临时目录（秒级冷启动开销）；
#   onedir 直接运行目录内可执行文件，冷启动快一个量级。
#   为不破坏 ~/.claude.json 已注册的 dist/computer-use-mcp 路径，
#   在该路径放一个 shell wrapper 转发到目录内真实可执行文件。
#
# 关键约束（见 _bootstrap.py）：
#   - AT-SPI 的 **Python 侧运行库**（libatspi.so.0 与 Atspi-2.0.typelib）现已**随产物
#     嵌入**（见下面 packaging/embed-atspi.sh 那一步）——.deb 要发给不能访问 apt 源的
#     机器，那边可能根本没装 libatspi2.0-0 / gir1.2-atspi-2.0。
#     剩下仍是 OS 级运行时依赖、不内嵌的只有**外部命令**：xdotool / Xephyr / i3 /
#     dbus-daemon / at-spi2-registryd / tesseract 等（.deb 里由 vendor/ 提供）。
#   - 全程 PYTHONNOUSERSITE=1，避免 ~/.local 的 mcp 遮蔽 conda 包导致打错版本。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# conda 环境（可用 ENV_NAME 覆盖）
ENV_NAME="${ENV_NAME:-cc-computer-use}"
CONDA_BASE="${CONDA_BASE:-$HOME/anaconda3}"
PYBIN="$CONDA_BASE/envs/$ENV_NAME/bin"
PY="$PYBIN/python"
PYINSTALLER="$PYBIN/pyinstaller"

if [[ ! -x "$PY" ]]; then
  echo "❌ 找不到 conda 环境 python: $PY" >&2
  echo "   请设 ENV_NAME / CONDA_BASE 环境变量指向正确环境" >&2
  exit 1
fi

# 隔离 ~/.local；让 PyInstaller 的 gi hook 能定位系统 Atspi typelib
export PYTHONNOUSERSITE=1
export GI_TYPELIB_PATH="/usr/lib/x86_64-linux-gnu/girepository-1.0:${GI_TYPELIB_PATH:-}"
# 关键：把 conda 环境的 lib 放在 LD_LIBRARY_PATH 最前，确保 PyInstaller 解析
# libcrypto/libssl 时一致地取自 conda（否则会用系统旧 libcrypto 配 conda 新 libssl，
# 触发 'OPENSSL_3.3.0 not found' 运行时错误）。
export LD_LIBRARY_PATH="$PYBIN/../lib:${LD_LIBRARY_PATH:-}"

echo "=== 清理旧产物 ==="
# M-45：spec 名必须与 `--name` 一致。PyInstaller 生成的 spec 是 `name + '.spec'`，
# 而本仓库用的是 `--name computer-use-mcp-bin` —— 所以真正要清的是 **-bin.spec**。
# 历史那行写的是 `computer-use-mcp.spec`，那个文件**从来不存在**：上一轮的 spec 一直
# 留在仓库里（虽然 .gitignore 挡住了它，不影响构建，但这个清理动作等于没做）。
# 同时删掉两种名字：换过 --name 的旧工作区里可能残留任一种。
rm -rf build dist computer-use-mcp.spec computer-use-mcp-bin.spec

echo "=== 开始打包（onedir，冷启动快）==="
# ⚠️ 不要改回 `--collect-all gi`（2026-09-23 实测）：
#   `--collect-all gi` 会连 gi.repository 下的**全部**子模块一起收，其中就有 Gtk/Gdk。
#   而 PyInstaller 的 hook-gi.repository.Gtk.py 会无条件执行
#       collect_glib_share_files('icons')   → 实测 185 MB（Yaru/Adwaita 全套光标主题，
#                                             光 watch/wait/progress 这类 4 MB 动画光标就有十几个）
#       collect_glib_share_files('themes')  → 实测  42 MB
#   本项目只 import Atspi / GLib / GObject，**从不 import Gtk/Gdk**（grep 全 src 可验），
#   这些资源纯属搭车。实测：删掉 share/icons + share/themes 后体积 455MB → 229MB（-50%），
#   冻结产物 --selftest 全绿（17 工具、backend 可用、光圈正常）。
#   故这里改成定向收集 gi + 显式排除 GTK 家族，从源头不触发那个 hook。
"$PYINSTALLER" \
  --onedir \
  --name computer-use-mcp-bin \
  --paths src \
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

# 兜底裁撤：即便 hook 仍被触发，也把 GTK 的图标/主题清掉（本工具不用 GTK 渲染任何东西）。
# 用 -print -quit 判断存在性，避免无谓的 rm 报错。
for d in dist/computer-use-mcp-bin/_internal/share/icons dist/computer-use-mcp-bin/_internal/share/themes; do
  if [[ -d "$d" ]]; then
    echo "  裁撤未使用的 GTK 资源: $d ($(du -sh "$d" | cut -f1))"
    rm -rf "$d"
  fi
done

# 把 AT-SPI 的 Python 侧运行库嵌进产物（libatspi.so.0 → _internal/，
# Atspi-2.0.typelib → _internal/gi_typelibs/）。落点与理由见脚本头部注释——
# 这是 .deb / .mcpb 能「装完就能用、不需要 apt」的前提。
bash "$ROOT/packaging/embed-atspi.sh" dist/computer-use-mcp-bin

echo
echo "=== 生成兼容 wrapper（保持已注册路径 dist/computer-use-mcp 不变）==="
cat > dist/computer-use-mcp <<'EOF'
#!/usr/bin/env bash
# 薄 wrapper：转发到 onedir 目录内真实可执行文件，保持旧注册路径可用
exec "$(dirname "$0")/computer-use-mcp-bin/computer-use-mcp-bin" "$@"
EOF
chmod +x dist/computer-use-mcp

echo
echo "=== 产物 ==="
ls -lh dist/computer-use-mcp dist/computer-use-mcp-bin/computer-use-mcp-bin
echo
echo "验证运行（应打印工具列表后等待 stdio，按 Ctrl-C 退出）："
echo "  PYTHONNOUSERSITE=1 ./dist/computer-use-mcp --selftest"
