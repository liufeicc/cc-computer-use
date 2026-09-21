"""
显示目标管理的**常量与纯工具**（core.display.constants）。

本模块不含任何状态与生命周期逻辑，只放：
  - 模式/环境变量名与默认值；
  - 私有 AT-SPI 总线相关常量与依赖路径候选；
  - 两个模块级纯函数：`_resolve_at_spi_deps`（定位系统依赖）、
    `_strip_frozen_lib_path`（剥掉冻结产物的库路径）。

拆出去的理由：这些常量被 `lifecycle` / `at_spi_bus` / `env` 等多个子模块共用，
留在任一个里都会让别的子模块反向依赖它。
"""

from __future__ import annotations

import os
import shutil
import sys

from ...utils.logging import get_logger

# ⚠️ 用**固定名字**取 logger，而不是 `__name__`：本包拆分自单文件
# `core/display.py`，固定名字才能让所有子模块**共用同一个 logger 对象**
# （日志名与拆分前一致），也才能让 `monkeypatch.setattr(display.log, "warning", ...)`
# 这类测试打补丁对全部子模块生效 —— 各子模块若各自 `get_logger(__name__)`，
# 拿到的是不同对象，补丁会静默打空。
log = get_logger("computer_use_mcp.core.display")

MODE_ISOLATED = "isolated"
MODE_REAL = "real"

ENV_MODE = "CC_CU_DISPLAY_MODE"
ENV_SANDBOX_DISPLAY = "CC_CU_SANDBOX_DISPLAY"
ENV_SANDBOX_SCREEN = "CC_CU_SANDBOX_SCREEN"
ENV_SANDBOX_WAIT_USER = "CC_CU_SANDBOX_WAIT_USER"
ENV_SANDBOX_WM = "CC_CU_SANDBOX_WM"

# ══════════════════════════════════════════════════════════════════════════════╗
# ║ 沙箱 a11y 总线开关：私有 AT-SPI 总线（正解）/ 死地址（兜底）                    ║
# ═════════════════════════════════════════════════════════════════════════════
# 【为什么需要】2026-09-15 事故，离线解 core 确证：
#   沙箱**不隔离 AT-SPI** —— Xephyr 只换掉 DISPLAY，而 a11y 走会话 D-Bus，沙箱内外
#   共用同一个 at-spi2-registryd。对沙箱内应用做 a11y 遍历，会把宿主 GNOME Shell 打崩：
#     meta_context_run_main_loop → g_main_loop_run → libatspi →
#     dbus_connection_dispatch → libatk-bridge → g_hash_table_foreach → g_object_ref →
#     g_type_check_instance_is_fundamentally_a → SIGSEGV
#   即 gnome-shell **自己**的 atk-bridge 在对**已释放的 GObject** 做引用计数
#   （use-after-free），崩在主循环线程上 → 整个桌面垮掉。
#   ⚠️ 这是"质"的 bug 不是"量"的问题：既有的一切预算/熔断/max_nodes 防护**都挡不住**
#   （实测守着全部约定仍然崩了）。
#
# 【怎么解决的】沙箱启动时自起一套**私有** AT-SPI 总线（见 at_spi_bus 子模块）：
#   私有目录里的 dbus-daemon + at-spi2-registryd。沙箱应用（app_env 注入地址）与
#   MCP 的 AtspiReader 都连它 → 宿主总线上完全看不到沙箱内应用，而 a11y 能力**保留**。
#
# 【兜底】私有总线起不来（缺 dbus-daemon / registryd / 配置）→ 应用拿到 _DEAD_AT_SPI_BUS：
#   连不上 a11y → 零注册零流量 → 宿主仍旧安全，只是沙箱内没有树可读。
#   **宁可没有 a11y，也绝不让流量落到宿主总线** —— 这是刻意的不对称。
#
# 【为什么用总线地址，而不是 NO_AT_BRIDGE】2026-09-15 实测：
#   - zenity 是 GTK4，**根本不加载 atk-bridge**；NO_AT_BRIDGE 只是 GTK3 的开关
#     （GTK4 得用 GTK_A11Y）。更要命的是 SWT（DBeaver，正是崩我们的那个应用）的
#     libswt-atk-gtk 里**没有任何 NO_AT_BRIDGE/GTK_A11Y 字符串** → 它不认这两个开关。
#     按工具链逐个加开关，覆盖不全、且对最要紧的 DBeaver 无效。
#   - AT_SPI_BUS_ADDRESS 则**工具链无关**：GTK3/GTK4/SWT/Qt/Electron 的 a11y 都走
#     libatspi，都读它。实测（用 dbus-monitor 观察会话总线上对 org.a11y.Bus 的调用）：
#       不设变量   → 1 次（走"问会话总线要 AT-SPI 总线地址"的正常路径）
#       指向死地址 → **0 次**（连问都不问）
#     → libatspi 尊重该变量，且**不回落**到会话总线（这条不成立的话整个隔离方案都不成立）。

ENV_SANDBOX_AT_SPI_BUS = "CC_CU_SANDBOX_AT_SPI_BUS"

# 死地址：语法合法但不存在。libatspi 连不上、也不会回落（实测）。
_DEAD_AT_SPI_BUS = "unix:path=/nonexistent/cc-cu-no-at-spi-bus"

# 私有 AT-SPI 总线的系统依赖（与 xdotool/Xephyr 一样属 OS 级，不内嵌进冻结产物）
# 私有 AT-SPI 总线的两个依赖。**必须留多路径候选**（M-10①）：下面第一条是 Debian/Ubuntu
# 的布局，在 Fedora/SUSE 上并不成立 —— 而历史实现只做 `os.path.exists` 判断，找不到就
# **静默退化**成「没有私有总线 → 死地址」，表现为**整个 a11y 能力消失而界面看不出异常**，
# 正是本项目最忌讳的那类隐蔽失效。故候选表 + PATH 兜底，并把实际用的那条记进日志。
_AT_SPI_CONF_CANDIDATES = (
    "/usr/share/defaults/at-spi2/accessibility.conf",   # Debian/Ubuntu
    "/etc/xdg/at-spi2/accessibility.conf",              # 发行版覆盖
    "/usr/share/at-spi2/accessibility.conf",
)
_AT_SPI_REGISTRYD_CANDIDATES = (
    "/usr/libexec/at-spi2-registryd",                   # Debian/Ubuntu/Fedora
    "/usr/lib/at-spi2-core/at-spi2-registryd",          # 部分 Debian 版本
)
_AT_SPI_TIMEOUT = 5.0   # 等私有总线 socket 出现的上限（秒）

# 私有总线工作目录名前缀，完整形如 `cc-cu-at-spi-d0-<随机>`。
# **目录名里带屏号（d<num>）是刻意的**：attach 已有沙箱的进程既没起 Xephyr、也没起
# 总线，无从知道总线地址，只能靠屏号反查（见 at_spi_bus._discover_at_spi_bus）。名字里
# 不带屏号则多块屏的总线目录彼此无从区分，attach 场景只能落到死地址 → a11y 全失效。
_AT_SPI_DIR_PREFIX = "cc-cu-at-spi"

# 未显式设 CC_CU_SANDBOX_DISPLAY 时**不固定屏号**：交给 Xephyr 自己分配一个空闲号
# （-displayfd），因此每个 Claude 会话各得一块独立虚拟屏——多会话并存时天然隔离，
# 也不会因为共用一块屏而互相抢焦点/剪贴板（实测过：共享时建屏方退出会带走屏，
# 另一方静默回落到宿主桌面）。
#
# app 自带屏号扫描 + 重试的兜底路径（极老版本 Xephyr 无 -displayfd）用这个区间起点。
DEFAULT_SANDBOX_SCREEN = "1600x1000"
DEFAULT_WAIT_USER = 30.0
_FALLBACK_DISPLAY_FROM = 99     # 兜底扫描起始屏号
_FALLBACK_DISPLAY_TRIES = 12    # 兜底时最多试几个号

_START_TIMEOUT = 10.0       # 等待 Xephyr socket 出现的最长秒数
_DISPLAYFD_TIMEOUT = 10.0   # 等 Xephyr 回写屏号的最长秒数
# Xephyr 窗口矩形的缓存时长（M-9）。Xephyr 窗口基本不动，2 秒的陈旧窗口足够安全，
# 换来的是礼让轮询不再每轮起一串 xdotool 子进程。
_HOST_RECT_TTL = 2.0
# 礼让轮询间隔（M-9）：原为 0.5s，30s 上限下最多 60 轮 × 每轮 3 类子进程。放宽到 1s
# 后轮数减半，而「用户离开后最多多等 1 秒」这个代价可以接受。
_USER_WAIT_POLL = 1.0

# 沙箱 WM 最小配置：i3 -c 指向它，绝不碰用户真实 i3 配置。
# 无 bar、不跟鼠标焦点；其余全走 i3 默认（新窗口平铺）。
_SANDBOX_I3_CONFIG = """# cc-computer-use sandbox WM (auto-generated, do not edit)
focus_follows_mouse no
bar { mode hidden }
"""


def _resolve_at_spi_deps() -> tuple[str | None, str | None]:
    """
    定位 `accessibility.conf` 与 `at-spi2-registryd`（M-10①）。

    返回 `(conf, registryd)`，找不到的为 None。registryd 再多走一层 `shutil.which`：
    它在有些发行版上是在 PATH 里的，硬编码路径只是最常见的那个。
    """
    conf = next((p for p in _AT_SPI_CONF_CANDIDATES if os.path.exists(p)), None)
    reg = next((p for p in _AT_SPI_REGISTRYD_CANDIDATES if os.path.exists(p)), None)
    if reg is None:
        reg = shutil.which("at-spi2-registryd")
    return conf, reg


def _strip_frozen_lib_path(env: dict) -> None:
    """
    剥掉 PyInstaller 冻结产物注入的 LD_LIBRARY_PATH 项（**原地**改 env）。

    为什么必须剥：server 以 PyInstaller 产物运行时，LD_LIBRARY_PATH 指向产物内部的库
    目录（约 71 个打包库：libglib/libgio/libgtk/libdbus/libatspi/libX11…）。**任何**
    子进程原样继承，都会加载产物里的库而不是系统库，与系统其余部分混装。实测后果有两条：
      1. DBeaver 的 JVM 在 libc 内 abort 崩溃（产物里的 libglib/libgio 与系统不是同一
         份文件，混装后堆损坏）；
      2. 它的 a11y 注册泄漏到宿主总线 —— 私有总线隔离失效。
    而我们启动的子进程（沙箱应用、xdotool、i3、Xephyr、dbus-daemon、registryd、
    tesseract）**全是系统二进制**，依赖一律应由系统提供，与用户手动启动它们的行为一致。

    ⚠️ 唯一调用点是 `env_for()`（`app_env` 经它再叠加 a11y 开关）。所有 spawn 点都必须
    经这两个通道取 env —— `tests/test_spawn_env.py` 枚举全部 spawn 点来兜住这一点。
    历史教训：这里原先只在 `app_env` 里调用，于是 Xephyr / i3 / dbus-daemon /
    at-spi2-registryd / tesseract 全部漏网（实测冻结产物下 Xephyr 确实优先加载了产物
    内的 libX11），其中 registryd 一漏就是「a11y 私有隔离静默失效」。

    只在**冻结产物**中剥离（`sys._MEIPASS` 存在时）：开发模式下 LD_LIBRARY_PATH 是
    conda/用户有意设置的，动了会连 gi 都导入不了。
    """
    lp = env.get("LD_LIBRARY_PATH")
    if not lp:
        return
    frozen = getattr(sys, "_MEIPASS", None)
    if not frozen:
        return
    frozen_real = os.path.realpath(frozen)
    kept = []
    for path in lp.split(os.pathsep):
        if not path:
            continue
        real = os.path.realpath(path)
        # 产物目录本身及其下所有子目录都剥掉（onedir 布局下 _internal 即 _MEIPASS 的子目录）
        if real == frozen_real or real.startswith(frozen_real + os.sep):
            continue
        kept.append(path)
    if kept:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(kept)
    else:
        env.pop("LD_LIBRARY_PATH", None)