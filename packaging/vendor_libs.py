#!/usr/bin/env python3
"""
计算随包分发二进制/库的动态依赖闭包，复制非基线部分到 vendor/lib，并写死 RPATH。

为什么需要这个（两个必须同时满足的约束）：

  ① 要「零命令行安装」，就得把 9 个系统二进制一起打包——用户机器上不保证装了它们。

  ② 但本项目有一条刻意的安全不变量：`core/display/env.py::env_for()` 会**主动剥掉**
     子进程的 `LD_LIBRARY_PATH`，理由是「我们启动的都是系统二进制，就该用系统库」。
     这条有 `tests/test_spawn_env.py` 守着，是防「产物库与系统库混装」的。

  这两条直接冲突：一旦二进制是我们打包的、系统上又没有，剥掉 LD_LIBRARY_PATH 后
  它们就找不到库了。

  解法是 **RPATH 而不是 LD_LIBRARY_PATH**：用 patchelf 给每个随包二进制写死
  `$ORIGIN/../lib`，它们靠自身定位依赖、**完全不经过环境变量**。
  于是 env_for() 那行剥离逻辑一个字都不用改，安全不变量完整保留。

实现逻辑：
  1. 基线白名单：任何 Ubuntu 22.04+/Debian 12+ GNOME 桌面必然存在的库（glibc/
     libstdc++/X11 核心/glib/GTK 栈等）。这些**不打包**——打包反而会与宿主版本冲突。
  2. 对 roots（9 个二进制 + typelib 对应的 libatspi）做 BFS，得到全部依赖。
  3. 非基线且真实存在的库 → 复制到 vendor/lib/。
  4. patchelf 写 RPATH：
       vendor/bin/*  → $ORIGIN/../lib
       vendor/lib/*  → $ORIGIN
  5. 另把 libatspi.so.0 收进 vendor/lib（**给 vendor/bin/at-spi2-registryd 用**，
     它链接 libatspi 且靠 RPATH 定位）；AT-SPI 总线配置 accessibility.conf 收进
     vendor/at-spi2/（系统那份属 at-spi2-core，写它就是文件冲突，必须自带一份）。
     ⚠️ Atspi-2.0.typelib **不在**这里收 —— 它必须落在 PyInstaller 产物的
     `_internal/gi_typelibs/` 下才生效，由 `packaging/embed-atspi.sh` 负责。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import deque

# ---------------------------------------------------------------------------
# 随包分发的二进制（相对 vendor/bin）
# ---------------------------------------------------------------------------
# 随包分发的二进制：`(名字, 显式路径候选)`。
#
# **为什么要有显式候选、而不是直接用 shutil.which**（2026-09-23 两处实测踩到）：
#   ① `at-spi2-registryd` **根本不在 PATH 里** —— Debian/Ubuntu 把它放在
#      `/usr/libexec/`。只用 which 会静默取不到，于是随包少了它；目标机若没装
#      at-spi2-core，私有 AT-SPI 总线起不来 → 沙箱内 a11y 整体失效，而界面看不出异常。
#   ② 构建是在 conda 环境里跑的，conda 的 bin 在 PATH 最前，而
#      **conda 自己也带一份 dbus-daemon** —— which 会优先命中 `/opt/conda/bin/dbus-daemon`，
#      于是随包发出的是 conda 版：依赖闭包完全不同，基线白名单与 RPATH 的前提也跟着变。
#      实测宿主上 `which dbus-daemon` 返回的就是 conda 环境里那一份，而不是 /usr/bin 的。
#   故判据是「先试显式系统路径，再退回 which」——与代码里 `_AT_SPI_REGISTRYD_CANDIDATES`
#   的多路径候选同一思路（那边也是硬编码路径优先、which 兜底）。
VENDOR_BINS: list[tuple[str, tuple[str, ...], str]] = [
    # 名字                     显式路径候选                        所属 apt 包
    ("xdotool",           ("/usr/bin/xdotool",), "xdotool"),
    ("Xephyr",            ("/usr/bin/Xephyr",), "xserver-xephyr"),
    ("xclip",             ("/usr/bin/xclip",), "xclip"),
    ("wmctrl",            ("/usr/bin/wmctrl",), "wmctrl"),
    ("xrandr",            ("/usr/bin/xrandr",), "x11-xserver-utils"),
    ("i3",                ("/usr/bin/i3",), "i3-wm"),
    ("dbus-daemon",       ("/usr/bin/dbus-daemon",), "dbus"),
    ("at-spi2-registryd", ("/usr/libexec/at-spi2-registryd",   # 私有总线的注册器
                           "/usr/lib/at-spi2-core/at-spi2-registryd"), "at-spi2-core"),
    ("tesseract",         ("/usr/bin/tesseract",), "tesseract-ocr"),
]

# ⚠️ 这里**刻意不再收集 Atspi-2.0.typelib 到 vendor/**（2026-09-23 改）：
#   它必须落在 PyInstaller 产物的 `_internal/gi_typelibs/` 下才生效 —— PyInstaller 的
#   pyi_rth_gi 运行时钩子**无条件赋值** GI_TYPELIB_PATH 到该目录（不是追加），放在别处
#   会被直接覆盖成死配置。那件事由 `packaging/embed-atspi.sh` 在 PyInstaller 之后完成，
#   两条构建路径（build.sh / build-in-container.sh）都会调它。

# 私有 AT-SPI 总线的 dbus 配置。系统那份 `/usr/share/defaults/at-spi2/accessibility.conf`
# **属 at-spi2-core**，写它就是文件冲突（在有 GNOME 的机器上 dpkg 直接报 overwrite 错误、
# 安装失败且无 apt 源可补救）。所以自带一份，由 wrapper 经 CC_CU_AT_SPI_CONF 指过去。
AT_SPI_CONF_SRC_DIRS = [
    "/usr/share/defaults/at-spi2",
    "/usr/share/at-spi2",
    "/etc/xdg/at-spi2",
]
VENDOR_AT_SPI_CONF = "accessibility.conf"

# tessdata：只带 OCR 实际会用的语言包，osd（10MB）不带
TESSDATA_SRC_DIRS = [
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
]
VENDOR_TESSDATA = ["eng.traineddata", "chi_sim.traineddata"]

# ---------------------------------------------------------------------------
# 基线白名单：**只有 glibc 工具链运行时 + dpkg 自身依赖的压缩库**。
#
# ⚠️ 这份名单 2026-09-23 被大幅收窄过，起因是一次实测：
#   原先它把「GNOME 桌面必然有的库」全列进来（libX11 / libglib / libcairo / libpango /
#   libxcb-* / libpng / libjpeg / libdbus-1 …），注释写着「宁可多留不可漏收」。
#   但在一个**干净的 ubuntu:22.04 容器**里跑验收时，9 个随包二进制有 **44 个库
#   解析不到** —— 整条 X11 + GLib + cairo/pango 栈都不在。
#   即便放宽到「Ubuntu 桌面」，`libev.so.4` / `libstartup-notification` /
#   `libxcb-icccm` / `libxcb-xrm` / `libXfont2` 这些（i3 与 Xephyr 的私有依赖）
#   也**不是**桌面默认就有的 —— 而它们缺了，沙箱就起不来。
#   既然本包的分发前提是「目标机不装任何东西」，白名单就不能建立在
#   「桌面必然有 X11」这种假设上。
#
# 为什么只保留 glibc 系：它们与 libc 同属一个 ABI 家族，产物一份、系统一份地混装
# 是**最严重**的那类崩溃（本项目 2026-09-15/16 两次事故都源于此），而且任何 glibc
# 系统必然自带。压缩库则是因为 dpkg/apt 自己就依赖它们，能装 .deb 的机器必然有。
#
# 其余一律随包（由依赖闭包决定）。这不会带来混装风险：随包二进制靠 patchelf 写死的
# RPATH 定位，**不经过 LD_LIBRARY_PATH**，所以 vendor/lib 永远不会被沙箱应用继承
# （详见 packaging/vendor_libs.py 抬头与 embed-atspi.sh）。
# ---------------------------------------------------------------------------
BASELINE_PREFIXES = (
    # glibc 本体与它的近亲
    "linux-vdso", "ld-linux",
    "libc.so", "libm.so", "libdl.so", "libpthread", "librt.so",
    "libnsl", "libresolv", "libutil", "libcrypt.so",
    # 编译器运行时（与 libc/libm 的 ABI 绑在一起）
    "libgcc_s", "libstdc++",
    # dpkg/apt 自身的依赖 —— 能安装 .deb 的系统必然有
    "libz.so", "liblzma", "libbz2", "libzstd", "liblz4",
)


def _is_baseline(name: str) -> bool:
    return name.startswith(BASELINE_PREFIXES)


def _resolve_bin(name: str, candidates: tuple[str, ...]) -> str | None:
    """定位一个随包二进制：先试显式系统路径，再退回 `shutil.which`。

    顺序是有意的（理由见 VENDOR_BINS 上方注释）：`which` 会命中 conda 里同名的
    另一份，而我们要的是系统那份。
    """
    for path in candidates:
        if os.path.exists(path) and os.access(path, os.X_OK):
            return path
    return shutil.which(name)


def _ldd(path: str) -> dict[str, str]:
    """返回 {soname: 绝对路径}；解析不到的（not found）不返回。"""
    try:
        out = subprocess.run(["ldd", path], capture_output=True, text=True,
                             timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if "=>" in line:
            left, _, right = line.partition("=>")
            name = left.strip()
            target = right.strip().split(" ")[0]
        elif line.startswith("/"):
            target = line.split(" ")[0]
            name = os.path.basename(target)
        else:
            continue
        if target and target != "not" and os.path.exists(target):
            result[os.path.basename(name)] = target
    return result


def _set_rpath(path: str, rpath: str) -> None:
    subprocess.run(["patchelf", "--set-rpath", rpath, path],
                   check=True, capture_output=True)


def _require_tools() -> None:
    """
    构建期工具的**前置检查**。

    为什么要单独查：这些工具是在脚本跑到**后半程**才被用到的（patchelf 在写 RPATH 那一步），
    缺了会抛一个从 subprocess 深处冒出来的 `FileNotFoundError` 回溯 —— 看起来像脚本有 bug，
    实际只是构建机上少装了个包。实测在精简容器里就是这么被绊住的（zip / python3 / patchelf
    各绊一次）。提前查、并且给出能照抄的 apt 命令，比事后看回溯划算得多。
    """
    missing = [t for t in ("patchelf", "ldd") if not shutil.which(t)]
    if missing:
        pkgs = {"patchelf": "patchelf", "ldd": "binutils"}
        print("❌ 缺少构建期工具: " + "、".join(missing), file=sys.stderr)
        print(f"     sudo apt install {' '.join(pkgs[t] for t in missing)}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: vendor_libs.py <dist_dir> <vendor_dir>", file=sys.stderr)
        return 2
    dist_dir, vendor_dir = sys.argv[1], sys.argv[2]
    _require_tools()

    bin_dir = os.path.join(vendor_dir, "bin")
    lib_dir = os.path.join(vendor_dir, "lib")
    at_spi_dir = os.path.join(vendor_dir, "at-spi2")
    tessdata_dir = os.path.join(vendor_dir, "tessdata")
    for d in (bin_dir, lib_dir, at_spi_dir, tessdata_dir):
        os.makedirs(d, exist_ok=True)

    # --- 1. 收集并复制二进制 ---
    # 缺失的必须**响亮地失败**而不是跳过：随包二进制少一个，表现是目标机上某个能力
    # 静默消失（少了 registryd → 沙箱内没有无障碍树；少了 tesseract → 灰区应用读不了屏），
    # 而构建日志里只有一行 ⚠️，很容易被当成噪音略过。
    print("[vendor] 收集二进制 …")
    missing: list[str] = []
    for name, candidates, pkg in VENDOR_BINS:
        src = _resolve_bin(name, candidates)
        if not src:
            print(f"  ❌ 找不到 {name}（试过 {'、'.join(candidates)} 与 PATH）", file=sys.stderr)
            missing.append(pkg)          # 记 **apt 包名** —— 报错要给的是能照抄的命令
            continue
        dst = os.path.join(bin_dir, name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        print(f"  {name}  <- {src}")
    if missing:
        # 注意报的是 apt 包名而不是二进制名：两者对不上的有好几个
        # （Xephyr←xserver-xephyr、i3←i3-wm、dbus-daemon←dbus、
        #  at-spi2-registryd←at-spi2-core），照着二进制名 apt install 是装不上的。
        print(f"❌ 缺少 {len(missing)} 个随包依赖，自包含分发要求一个都不能少，中止：", file=sys.stderr)
        print(f"     sudo apt install {' '.join(missing)}", file=sys.stderr)
        return 1

    # --- 2. AT-SPI 总线配置 + tessdata ---
    print("[vendor] 收集 AT-SPI 总线配置 / tessdata …")
    for d in AT_SPI_CONF_SRC_DIRS:
        src = os.path.join(d, VENDOR_AT_SPI_CONF)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(at_spi_dir, VENDOR_AT_SPI_CONF))
            print(f"  {VENDOR_AT_SPI_CONF}  <- {src}")
            break
    else:
        # 这条不能只告警：没有 conf 就起不了私有总线 → 沙箱内 a11y 整体失效，
        # 而表现只是「读不到元素树」，属于本项目最忌讳的静默降级。
        print(f"  ⚠️ 找不到 {VENDOR_AT_SPI_CONF}（apt install at-spi2-core）——"
              f"私有 AT-SPI 总线将无法启动，沙箱内没有无障碍树", file=sys.stderr)

    for name in VENDOR_TESSDATA:
        for d in TESSDATA_SRC_DIRS:
            src = os.path.join(d, name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tessdata_dir, name))
                print(f"  {name}  <- {src}")
                break
        else:
            print(f"  ⚠️ 找不到 tessdata {name}（该语言 OCR 将不可用）", file=sys.stderr)

    # --- 3. 依赖闭包 ---
    print("[vendor] 计算依赖闭包 …")
    roots = [os.path.join(bin_dir, n) for n in os.listdir(bin_dir)]
    # libatspi 也收进 vendor/lib：**vendored 的 at-spi2-registryd 链接它**，靠自身
    # RPATH `$ORIGIN/../lib` 定位；目标机若没装 libatspi2.0-0，少了它就起不了私有总线。
    #
    # ⚠️ 这份是给**子进程**用的，不是给 MCP server 进程用的。
    #   server 自己那份在 PyInstaller 产物的 `_internal/libatspi.so.0`（由
    #   embed-atspi.sh 嵌入）——因为它在 sys._MEIPASS 之下，会随 PyInstaller 的
    #   LD_LIBRARY_PATH 被 _strip_frozen_lib_path() 从子进程 env 里剥掉。
    #   两个位置看着重复，但服务的是两条不同的加载路径，缺一不可。
    for cand in ("/usr/lib/x86_64-linux-gnu/libatspi.so.0",
                 "/usr/lib/libatspi.so.0"):
        if os.path.exists(cand):
            shutil.copy2(cand, os.path.join(lib_dir, "libatspi.so.0"))
            roots.append(os.path.join(lib_dir, "libatspi.so.0"))
            print(f"  libatspi.so.0  <- {cand}  (给 vendor/bin/at-spi2-registryd 用)")
            break

    seen: dict[str, str | None] = {}
    queue: deque[str] = deque(roots)
    while queue:
        cur = queue.popleft()
        for name, path in _ldd(cur).items():
            if name in seen:
                continue
            if _is_baseline(name):
                seen[name] = None
                continue
            # 已经在 lib_dir 里的（如 libatspi）不重复复制
            local = os.path.join(lib_dir, name)
            if os.path.exists(local):
                seen[name] = local
                queue.append(local)
                continue
            dst = os.path.join(lib_dir, name)
            shutil.copy2(path, dst)
            os.chmod(dst, 0o755)
            seen[name] = dst
            queue.append(dst)

    bundled = [(n, p) for n, p in seen.items() if p]
    total = sum(os.path.getsize(p) for _, p in bundled)
    print(f"  需随包分发: {len(bundled)} 个库, {total / 1048576:.1f} MB")

    # --- 4. 写 RPATH ---
    print("[vendor] 写 RPATH（$ORIGIN 相对定位，不经过 LD_LIBRARY_PATH）…")
    n_bin = n_lib = 0
    for name in os.listdir(bin_dir):
        _set_rpath(os.path.join(bin_dir, name), "$ORIGIN/../lib")
        n_bin += 1
    for name, path in bundled:
        _set_rpath(path, "$ORIGIN")
        n_lib += 1
    print(f"  已处理 {n_bin} 个二进制（$ORIGIN/../lib）+ {n_lib} 个库（$ORIGIN）")

    # --- 5. 清掉 PyInstaller 产出的冗余 ICU ---
    # --collect-data gi 会带进 libicudata.so.78（32MB），而 conda 环境的 python
    # 用不到它（项目只走 Atspi/GLib）。保守起见只在存在同名双份时才删小的那份。
    internal = os.path.join(dist_dir, "computer-use-mcp-bin", "_internal")
    for icu in ("libicudata.so.78", "libicudata.so.74", "libicudata.so.76"):
        p = os.path.join(internal, icu)
        if os.path.exists(p) and os.path.getsize(p) > 10 * 1024 * 1024:
            print(f"  裁撤冗余 {icu} ({os.path.getsize(p) / 1048576:.0f} MB)")
            os.remove(p)

    print("[vendor] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
