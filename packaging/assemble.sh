#!/usr/bin/env bash
# ============================================================
# 把 PyInstaller 产物 + 随包系统组件组装成可分发形态
#
# 用法:
#   bash packaging/assemble.sh <pyinstaller 产物父目录> [输出目录]
#     <pyinstaller 产物父目录>  内含 computer-use-mcp-bin/（build.sh 或容器构建的 onedir）
#     [输出目录]                默认与第一个参数相同
#
# 产出（都在「输出目录」下）：
#   cc-computer-use/            ← 完整可分发目录（onedir + vendor/），.deb 的输入
#   cc-computer-use-<ver>.mcpb  ← Claude Desktop 一键安装包（ZIP）——**默认不产**，见下
#
# 为什么入参拆成两个目录（2026-09-23 改）：
#   容器构建把 PyInstaller 的中间产物放在**容器内**（/out/work），只有最终产物才落到
#   挂载出来的宿主目录。原先两者共用同一个 dist，于是容器构建产出的 computer-use-mcp-bin/
#   会和宿主 build.sh 的同名产物**互相覆盖** —— 两个包基于不同的 Ubuntu 基座（22.04 vs
#   24.04），谁覆盖谁完全取决于最后跑了哪个，而 `dist/computer-use-mcp`（已注册的 MCP
#   路径）指向的正是那个目录，出了问题根本看不出是哪个基座在跑。
#
# 为什么 .mcpb 默认不产（2026-09-23 改）：
#   本次分发只针对 Ubuntu 用户，.deb 是唯一在用的形态。而 zip 那个 300MB 的目录实测要
#   2 分钟（占整条流水线约 1/9），天天白跑。要 Claude Desktop 的包时显式开：
#       CC_CU_MCPB=1 bash packaging/assemble.sh ...
# ============================================================
set -euo pipefail

IN_DIST="${1:?用法: assemble.sh <pyinstaller 产物父目录> [输出目录]}"
DIST="${2:-$IN_DIST}"
VERSION="${CC_CU_VERSION:-0.1.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ -d "$IN_DIST/computer-use-mcp-bin" ] || {
  echo "❌ $IN_DIST/computer-use-mcp-bin 不存在 —— 第一个参数应是含 PyInstaller onedir 的目录" >&2
  exit 1; }

PKG="$DIST/cc-computer-use"
rm -rf "$PKG"
mkdir -p "$PKG/server" "$PKG/vendor" "$DIST"

echo "=== [组装 1/4] 搬运 PyInstaller 产物 ==="
cp -a "$IN_DIST/computer-use-mcp-bin/." "$PKG/server/"
# 顶层 wrapper 不再需要（.mcpb 直接指向 server/computer-use-mcp-bin）
rm -f "$PKG/server/computer-use-mcp"

echo "=== [组装 2/4] 随包系统组件（vendor/） ==="
python3 "$HERE/vendor_libs.py" "$IN_DIST" "$PKG/vendor"

echo "=== [组装 3/4] 生成 manifest.json ==="
# 注意 server.type = binary：MCPB 规范里 binary 用 entry_point 指向可执行文件。
#
# ⚠️ mcp_config.env 里**刻意不设** LD_LIBRARY_PATH / GI_TYPELIB_PATH（2026-09-23 改）：
#   它们原先指向 vendor/lib 与 vendor/typelib，但两处都是错的——
#     · GI_TYPELIB_PATH 是**死配置**：PyInstaller 的 pyi_rth_gi 运行时钩子在启动时
#       **无条件赋值** GI_TYPELIB_PATH = <_MEIPASS>/gi_typelibs，把这里设的值直接覆盖掉。
#     · LD_LIBRARY_PATH 指向 vendor/lib 是**有害的**：core/display/constants.py 的
#       _strip_frozen_lib_path() 只剥 _MEIPASS 之下的路径，vendor/lib 不在其下 → 会被
#       **沙箱应用**（app_env 启动的 JVM/Electron 等）继承，而 vendor/lib 里有 libxml2 /
#       libcrypto / libicu* 等约 50 个库 —— 正是 2026-09-15/16 那两次「产物库与系统库
#       混装」事故的同一类路径。
#   现在 AT-SPI 的两样（libatspi.so.0 / Atspi-2.0.typelib）由 embed-atspi.sh 直接嵌进
#   server/_internal/，第 ① 处那条钩子与 PyInstaller 自带的 LD_LIBRARY_PATH 就会命中它们，
#   且因为落在 _MEIPASS 之下，会被自动从子进程 env 里剥掉。
#   vendor/bin 里的 9 个二进制靠 patchelf 写死的 RPATH 自定位，不经过环境变量。
cat > "$PKG/manifest.json" <<EOF
{
  "manifest_version": "0.3",
  "name": "cc-computer-use",
  "display_name": "Computer Use (Linux desktop)",
  "version": "${VERSION}",
  "description": "Drive a Linux desktop from any MCP client — on a private virtual screen by default, so the agent never seizes your mouse.",
  "long_description": "Perceives the UI through the **accessibility tree (AT-SPI)** and acts through **element-level operations**, instead of screenshot + coordinate clicking.\n\nIsolation is the default: the agent runs on its own Xephyr virtual screen with a **private AT-SPI bus**, so your desktop keeps working and sandboxed apps are invisible to the host bus. Set CC_CU_DISPLAY_MODE=real to operate the real desktop instead.",
  "author": {
    "name": "liufei",
    "url": "https://github.com/liufeicc/cc-computer-use"
  },
  "homepage": "https://github.com/liufeicc/cc-computer-use",
  "repository": {
    "type": "git",
    "url": "https://github.com/liufeicc/cc-computer-use"
  },
  "license": "MIT",
  "keywords": [
    "mcp", "computer-use", "linux", "desktop-automation",
    "accessibility", "atspi", "gui-automation", "sandbox"
  ],
  "server": {
    "type": "binary",
    "entry_point": "server/computer-use-mcp-bin",
    "mcp_config": {
      "command": "\${__dirname}/server/computer-use-mcp-bin",
      "args": [],
      "env": {
        "PYTHONNOUSERSITE": "1",
        "TESSDATA_PREFIX": "\${__dirname}/vendor/tessdata",
        "CC_CU_AT_SPI_CONF": "\${__dirname}/vendor/at-spi2/accessibility.conf",
        "PATH": "\${__dirname}/vendor/bin:\${PATH}"
      }
    }
  },
  "compatibility": {
    "claude_desktop": ">=0.10.0",
    "platforms": ["linux"],
    "runtimes": {}
  },
  "user_config": {
    "display_mode": {
      "type": "string",
      "title": "Display mode",
      "description": "isolated (default): the agent gets its own virtual screen and your desktop keeps working. real: the agent drives your actual desktop.",
      "default": "isolated",
      "required": false
    }
  }
}
EOF

echo "=== [组装 4/4] 打包 .mcpb ==="
# 默认不产（理由见文件头）：本次分发只针对 Ubuntu 用户，.deb 是唯一在用的形态，
# 而这个 zip 要 2 分钟。需要 Claude Desktop 的包时显式 CC_CU_MCPB=1。
if [ -z "${CC_CU_MCPB:-}" ]; then
  echo "  跳过（未设 CC_CU_MCPB=1）—— 本次只出 .deb"
else
  MCPB="$DIST/cc-computer-use-${VERSION}.mcpb"
  rm -f "$MCPB"
  ( cd "$PKG" && zip -qr9 "$MCPB" . )
  echo "  $MCPB  ($(du -h "$MCPB" | cut -f1))"
fi

echo
echo "=== 组装完成 ==="
du -sh "$PKG"
du -sh "$PKG/server" "$PKG/vendor" 2>/dev/null
