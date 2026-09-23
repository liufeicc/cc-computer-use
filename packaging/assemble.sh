#!/usr/bin/env bash
# ============================================================
# 把 PyInstaller 产物 + 随包系统组件组装成可分发形态
#
# 产出：
#   <dist>/cc-computer-use/            ← 完整可分发目录（onedir + vendor/）
#   <dist>/cc-computer-use-<ver>.mcpb  ← Claude Desktop 一键安装包（ZIP）
#
# 调用方：packaging/build-in-container.sh 的第 7 步
# ============================================================
set -euo pipefail

DIST="${1:?用法: assemble.sh <dist_dir>}"
VERSION="${CC_CU_VERSION:-0.1.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PKG="$DIST/cc-computer-use"
rm -rf "$PKG"
mkdir -p "$PKG/server" "$PKG/vendor"

echo "=== [组装 1/4] 搬运 PyInstaller 产物 ==="
cp -a "$DIST/computer-use-mcp-bin/." "$PKG/server/"
# 顶层 wrapper 不再需要（.mcpb 直接指向 server/computer-use-mcp-bin）
rm -f "$PKG/server/computer-use-mcp"

echo "=== [组装 2/4] 随包系统组件（vendor/） ==="
python3 "$HERE/vendor_libs.py" "$DIST" "$PKG/vendor"

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
MCPB="$DIST/cc-computer-use-${VERSION}.mcpb"
rm -f "$MCPB"
( cd "$PKG" && zip -qr9 "$MCPB" . )
echo "  $MCPB  ($(du -h "$MCPB" | cut -f1))"

echo
echo "=== 组装完成 ==="
du -sh "$PKG"
du -sh "$PKG/server" "$PKG/vendor" 2>/dev/null
