# -*- coding: utf-8 -*-
"""
打包与分发的**结构性守卫**（.deb / .mcpb）。

这里守的不是「功能对不对」，而是**几条一旦被改回去就会静默出事的约定**：

  1. AT-SPI 的 Python 侧运行库（libatspi.so.0 / Atspi-2.0.typelib）必须**嵌进 PyInstaller
     产物的 `_internal/`**。落点错了不会报错，只会让「目标机没装 libatspi2.0-0」时
     `import Atspi` 失败 → 无障碍能力整体退化成只有 OCR。见 packaging/embed-atspi.sh。
  2. 两条构建路径（build.sh 与 packaging/build-in-container.sh）**都必须调那个嵌入脚本**。
     漏掉一条，那条路径产出的包就是不自包含的，而构建日志里什么都看不出来。
  3. 分发清单（mcpb manifest）**不许再设 `LD_LIBRARY_PATH`**。
     历史实现设过 `${__dirname}/vendor/lib`，注释还写着「env_for() 会剥掉，两者互不干扰」——
     那句是错的：`_strip_frozen_lib_path()` 只剥 `sys._MEIPASS` **之下**的路径，vendor/lib
     不在其下，于是会被**沙箱应用**继承，而那里有约 50 个构建基座的库
     （libxml2 / libcrypto / libicu* 等）—— 正是 2026-09-15/16 两次「产物库与系统库混装」
     事故的同一类路径。`GI_TYPELIB_PATH` 同理是死配置（被 pyi_rth_gi 无条件覆盖）。

判据一律取**源码文本**而非「文件里有某个常量」：常量还在、调用点没用它，是这类改动
最常见的漏法。同一思路见 test_ring_and_preview.py::test_build_sh_collects_xlib。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    with open(os.path.join(REPO, *parts), encoding="utf-8") as fh:
        return fh.read()


# ==================== ① 嵌入脚本的落点 ====================

def test_embed_script_puts_atspi_into_internal():
    """
    libatspi → `_internal/`（在 _MEIPASS 之下，才会被 _strip_frozen_lib_path 剥掉），
    typelib → `_internal/gi_typelibs/`（pyi_rth_gi 无条件把 GI_TYPELIB_PATH 指到这里）。

    这两条落点各有硬理由，写反了或挪出去都会**静默**失效，故直接钉住目标字符串。
    """
    src = _read("packaging", "embed-atspi.sh")

    assert "$INTERNAL/libatspi.so.0" in src, "libatspi 必须落在 _internal/ 下"
    assert "$INTERNAL/gi_typelibs/Atspi-2.0.typelib" in src, \
        "typelib 必须落在 _internal/gi_typelibs/ 下（pyi_rth_gi 只认这里）"
    assert re.search(r'^INTERNAL="\$ONEDIR/_internal"', src, re.M), \
        "INTERNAL 必须由入参目录推导，不能写死绝对路径"


def test_embed_script_derefs_symlinks():
    """
    每次拷贝都必须 `cp -L`：Debian 系上 libatspi.so.0 是 `→ libatspi.so.0.0.1` 的软链，
    原样拷走会得到一个**断链**，而症状要到目标机上才浮现（dlopen 失败 → 没有 Atspi）。
    """
    src = _read("packaging", "embed-atspi.sh")
    copies = [ln.strip() for ln in src.splitlines() if re.search(r"\bcp\s", ln)]
    assert copies, "没找到 cp 调用（脚本被大改？）"
    for ln in copies:
        assert "cp -L" in ln, f"缺 -L（软链会拷成断链）: {ln}"


def test_embed_script_walks_typelib_dependency_closure():
    """
    必须**递归**把 typelib 的依赖一起嵌进去，不能只拷 Atspi 一个文件。

    `Atspi-2.0.typelib` 的头部声明了 `GObject-2.0|GLib-2.0|DBus-1.0`，其中
    **DBus-1.0 来自 `gir1.2-freedesktop`**（不是 at-spi2-core 的包）。只拷 Atspi 时
    宿主上照样能用（`_bootstrap` 会追加系统 typelib 目录兜底），**宿主上永远重现不了**；
    目标机一旦没有 gir1.2-freedesktop 就报
    「Typelib file for namespace 'DBus', version '1.0' not found」，无障碍能力整体消失。
    这个坑是在干净容器里验收时抓到的。

    判据取「有没有解析 requires 那行」而不是「有没有某个文件名」—— 后者写死一个
    DBus-1.0 也能过，但那等于把依赖清单又硬编码回去了。
    """
    src = _read("packaging", "embed-atspi.sh")
    assert "_requires_of" in src, "没有解析 typelib 依赖的辅助函数"
    assert re.search(r"queue\+=", src), "没有把依赖入队（不是递归闭包）"
    # 真正的判据在下面两个「真跑一遍」的用例里；这里只挡住「连函数都被删了」。
    assert "< <(" not in src.split("_requires_of", 1)[1].split("}")[0], \
        "别用进程替换读 deps（set -e 下容易吞掉子 shell 的失败）"


def _requires_of_snippet() -> str:
    """从 embed-atspi.sh 里**抠出** _requires_of 函数体，好在测试里真跑它。"""
    src = _read("packaging", "embed-atspi.sh")
    m = re.search(r"^_requires_of\(\)\s*\{.*?\n\}$", src, re.S | re.M)
    assert m, "没找到 _requires_of 函数（脚本被大改？）"
    return m.group(0)


def _run_requires_of(tmp_path, blob: bytes) -> list[str]:
    p = os.path.join(tmp_path, "T-1.0.typelib")
    with open(p, "wb") as fh:
        fh.write(blob)
    script = "set -euo pipefail\n" + _requires_of_snippet() + '\n_requires_of "$1"\n'
    out = subprocess.run(["bash", "-c", script, "_", p],
                         capture_output=True, text=True, check=True)
    return out.stdout.split()


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_requires_of_reads_both_typelib_storage_formats(tmp_path):
    """
    依赖在 typelib 里有**两种存法**，两种都得认：

      · 老格式：拼成一个 `|` 分隔的串（`GObject-2.0|GLib-2.0|DBus-1.0`）
      · 新格式：一连串独立的 NUL 结尾串（gi 1.76+，conda 那份 pygobject 就是）

    旧实现用 `grep -m1` 只取第一行，在新格式下**只能拿到一个依赖** —— 而且完全静默：
    产物在构建机上照样能用（`_bootstrap` 会追加系统 typelib 目录兜底），要到目标机
    没装 `gir1.2-*` 时才现形。
    """
    header = b"GOBJ" + b"\0" * 200
    old = header + b"GObject-2.0|GLib-2.0|DBus-1.0\0Atspi\0" + b"\0" * 64
    new = header + b"GObject-2.0\0GLib-2.0\0DBus-1.0\0Atspi\0" + b"\0" * 64

    want = ["DBus-1.0", "GLib-2.0", "GObject-2.0"]
    assert _run_requires_of(str(tmp_path), old) == want
    assert _run_requires_of(str(tmp_path), new) == want


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_requires_of_ignores_noise_outside_the_header(tmp_path):
    """
    只许在**头部**找依赖，且每个元素必须带 `-版本号`。

    两条规则各对应一次实测：
      · 全文件扫描时，`GLib-2.0.typelib` 在元数据段**第 6906 行**匹配到 `i|E`，
        于是闭包队列里多出一个叫 `i` 的「依赖」，构建报「找不到依赖的 typelib: i」当场失败；
      · 光限窗口还不够：头部里也有裸词（`Atspi` / `G`）与共享库名
        （`libgio-2.0.so.0` —— 版本号后面跟的是 `.so.0` 而不是 `.数字`），一个都不能认。
    """
    header = b"GOBJ" + b"\0" * 100
    # 头部里的噪声：裸词、共享库名、以及「裸词|单词」这种半吊子
    noisy_header = header + b"Atspi\0libgio-2.0.so.0\0i|E\0GObject-2.0\0" + b"\0" * 64
    assert _run_requires_of(str(tmp_path), noisy_header) == ["GObject-2.0"], \
        "头部噪声被当成了依赖（或真依赖被误杀）"

    # 头部干净、噪声在 4KB 之外 → 必须一个都不返回
    deep_noise = header + b"GLib\0" + b"\0" * 8000 + b"i|E\0Gtk-3.0\0" + b"\0" * 64
    assert _run_requires_of(str(tmp_path), deep_noise) == [], \
        "4KB 之外的元数据噪声被当成了依赖（正是 `i` 那个 bug）"


def test_both_build_paths_embed_atspi():
    """build.sh（本机）与 build-in-container.sh（可分发产物）都必须调嵌入脚本。"""
    for path in (("build.sh",), ("packaging", "build-in-container.sh")):
        src = _read(*path)
        assert "embed-atspi.sh" in src, f"{'/'.join(path)} 没有调 embed-atspi.sh"


# ==================== ①b 容器构建的路径分工与产物范围 ====================

def _live_lines(src: str) -> list[str]:
    """去掉整行注释与行尾注释，只留**真正会执行**的行。

    好几条守卫都要用它：注释里写一句「本该如此」是挡不住问题的
    （见 test_container_build_returns_output_ownership 的教训）。
    """
    out = []
    for ln in src.splitlines():
        code = ln.split("#", 1)[0]
        if code.strip():
            out.append(code)
    return out


def test_container_build_separates_work_from_output():
    """
    容器构建必须把**中间产物**放在容器内（`$WORK`），只有最终产物落到挂载出来的
    `$OUT`（宿主 `dist/`）。

    合并成一个目录的后果（2026-09-23 实测）：宿主 `dist/` 里住着 `build.sh` 的
    `dist/computer-use-mcp-bin/`（**24.04 基座**的本地开发构建），而容器构建的
    PyInstaller 也叫 `computer-use-mcp-bin`（**22.04 基座**）。共用同一个 dist 时两者
    互相覆盖，而宿主的 `dist/computer-use-mcp`（**已注册的 MCP 启动脚本**）正指向那个
    目录 —— 于是「现在跑的是哪个基座」完全取决于最后跑了谁，出问题时看不出来。

    判据是「PyInstaller 的 distpath 指向 WORK、且它与 OUT 不是同一个值」，而不是
    「文件里有 OUT 这个词」。
    """
    src = _read("packaging", "build-in-container.sh")
    live = _live_lines(src)

    def _var(name: str) -> str:
        m = next((re.match(rf"\s*{name}=(\S+)\s*$", ln) for ln in live
                  if re.match(rf"\s*{name}=", ln)), None)
        assert m, f"没有在真实命令行上定义 {name}"
        return m.group(1)

    assert _var("OUT") != _var("WORK"), "OUT 与 WORK 是同一个目录，等于没分"

    distpath = next((ln for ln in live if "--distpath" in ln), None)
    assert distpath, "没找到 --distpath"
    assert "$WORK" in distpath, \
        f"PyInstaller 的产物没落到 WORK（会覆盖宿主的同名开发构建）: {distpath.strip()}"


def test_container_build_defaults_to_deb_only():
    """
    默认**只出 .deb**，不打 .mcpb。

    理由：本次分发只针对 Ubuntu 用户，`.deb` 是唯一在用的形态；而 zip 那 300MB 的组装
    目录实测要 2 分钟（占整条流水线约 1/9），每次构建都白跑。

    判据取「zip 那行处在 CC_CU_MCPB 的条件分支里」——只断言「文件里有 CC_CU_MCPB」
    挡不住「变量在、但 zip 照跑无条件跑」（同款漏法见 test_build_sh_collects_xlib）。
    """
    src = _read("packaging", "assemble.sh")
    live = _live_lines(src)

    assert any("CC_CU_MCPB" in ln for ln in live), "没有任何控制 .mcpb 的开关"
    zip_ln = next((i for i, ln in enumerate(live) if re.search(r"\bzip\b", ln)), None)
    assert zip_ln is not None, "没找到打 .mcpb 的 zip 调用（脚本被大改？）"

    # zip 必须出现在某个 `if [ -z "${CC_CU_MCPB:-}" ]`（或等价的反向判断）之后
    guard = next((i for i, ln in enumerate(live)
                  if "CC_CU_MCPB" in ln and re.search(r"\bif\b|\bcase\b|\|\|", ln)), None)
    assert guard is not None, "CC_CU_MCPB 没有被用在条件判断里"
    assert guard < zip_ln, "zip 在开关判断之前就跑了（等于开关无效，.mcpb 还是会产）"


def test_build_sh_does_not_wipe_the_whole_dist():
    """
    `build.sh` 的清理动作**只许删自己的两个产物**，不许 `rm -rf dist`。

    `dist/` 现在同时住着两种东西：`build.sh` 的（`computer-use-mcp-bin/` + wrapper
    `computer-use-mcp`）与容器构建的（`.deb` + 组装目录）。整目录清掉会把 68MB 的包
    连同 300MB 的组装目录一起抹掉 —— 而用户不会预期「跑一次本机开发构建」会顺手删掉
    分发产物。

    判据是**非注释行**上的 `rm -rf dist`（脚本里有一段注释专门解释为什么不这么做，
    不能把那段注释也算成违规）。
    """
    live = _live_lines(_read("build.sh"))
    bad = [ln.strip() for ln in live
           if re.search(r"\brm\s+-rf\b", ln) and re.search(r"(^|\s)dist(\s|$)", ln)]
    assert not bad, f"build.sh 又在整目录删 dist 了: {bad}"
    # 但确实要清掉自己的产物，否则 PyInstaller 会往旧目录里叠加
    assert any("dist/computer-use-mcp-bin" in ln for ln in live), \
        "build.sh 没有清理自己的 dist/computer-use-mcp-bin"


def test_container_build_returns_output_ownership():
    """
    容器构建**必须**支持把产物属主交还给宿主用户（`CC_CU_CHOWN`）。

    为什么这条值得一个测试：容器以 root 运行，而 `/out` 是从宿主挂进来的目录。
    少了这一步，产物在宿主上属主是 root —— 包文件本身还能删（删文件只看父目录的
    写权限），但 `computer-use-mcp-bin/` 里那几百个 root 所有的文件**没有 sudo 删不掉**。
    于是「把输出目录从 /tmp 挪到 ~/dist」这个动作，等于把同一个麻烦换了个地方，
    而且**要等到用户想清理时才发现**（实测踩到）。

    判据是「有没有这一步」，不是「有没有这个变量名」：所以断言 `chown -R` 真的在脚本里，
    且**必须落在一行真实命令上**（注释里的不算 —— 第一版忘了排除注释，把 `chown` 注释掉
    守卫照样全绿，等于没测）。
    """
    src = _read("packaging", "build-in-container.sh")
    assert "CC_CU_CHOWN" in src, "容器构建没有交还产物属主的机制"

    # 只看非注释行：# 开头的整行注释与行尾注释都要排除
    live = []
    for ln in src.splitlines():
        code = ln.split("#", 1)[0]
        if code.strip():
            live.append(code)
    assert any(re.search(r'chown -R\s+"\$CC_CU_CHOWN"', ln) for ln in live), \
        "CC_CU_CHOWN 只在注释里出现，没有真的 chown -R（等于没生效）"

    # 四份文档给的构建命令都必须带上它 —— 漏了的那份会把用户带坑里。
    for doc in ("README.md", "README.zh-CN.md", "CLAUDE.md",
                os.path.join("docs", "安装说明.md")):
        assert "CC_CU_CHOWN" in _read(doc), f"{doc} 的构建命令没带 CC_CU_CHOWN"


def test_build_deb_pins_umask():
    """
    `build-deb.sh` 必须在**建任何文件之前**把 umask 定死。

    实测（2026-09-23）：脚本里的 `cp` / `mkdir -p` / `gzip >` 全部受调用方 umask 影响，
    而末尾那段「权限归一」只覆盖 `$PREFIX` 与 `/usr`、`/etc` 的**目录**，没有逐个 chmod
    `/usr/share/doc/` 下的文件。于是同一份源码在两台机器上打出**权限位不同**的包：
    宿主 umask 0002 → `/usr/share/doc/.../README.Debian` 是 0664；容器里（022）是 0644。

    这不只是「不一致」——/usr/share/doc 下的文件按 Debian 政策就该是 0644，0664 是错的，
    而且它让「同一份源码产出同一个包」这个前提悄悄失效。

    判据取**非注释行**上的 `umask`：注释里写一句「本该 umask 022」是挡不住问题的
    （同款教训见上面那条 chown 守卫）。
    """
    src = _read("packaging", "deb", "build-deb.sh")
    live = [ln.split("#", 1)[0] for ln in src.splitlines()]
    live = [ln for ln in live if ln.strip()]
    assert any(re.match(r"\s*umask\s+022\s*$", ln) for ln in live), \
        "build-deb.sh 没有在任何真实命令行上定死 umask（产物权限位会随构建机漂移）"

    # 而且必须在建文件之前：第一处 mkdir / cp 的**行号**要晚于 umask 那行
    i_umask = next(i for i, ln in enumerate(live) if re.match(r"\s*umask\s+022\s*$", ln))
    first_create = next((i for i, ln in enumerate(live)
                         if re.search(r"\b(mkdir|cp|gzip|ln)\b", ln)), None)
    assert first_create is not None, "没找到建文件的命令（脚本被大改？）"
    assert i_umask < first_create, \
        "umask 定得太晚 —— 在它之前已经有文件被创建出来了"


# ==================== ② 分发清单不许泄漏库路径 ====================

def test_mcpb_manifest_does_not_leak_library_paths():
    """
    manifest 里不许出现 LD_LIBRARY_PATH / GI_TYPELIB_PATH。

    两者都是历史遗留的错误配置（见模块 docstring）：
      · GI_TYPELIB_PATH 启动时被 pyi_rth_gi 无条件覆盖 → 死配置，留着只会误导；
      · LD_LIBRARY_PATH 指向 vendor/lib → 剥不掉 → 泄漏给沙箱应用 → 混装事故。
    """
    src = _read("packaging", "assemble.sh")

    # 只看 manifest 的 env 块（脚本正文里提到这两个词是在注释里解释「为什么不设」）。
    # 不能用 `\{.*?\}` 取块：env 的值里有 `${__dirname}`，非贪婪匹配会在那里就收尾。
    m = re.search(r'"env"\s*:\s*\{(.*?)"compatibility"', src, re.S)
    assert m, "没找到 manifest 的 env 块（assemble.sh 被大改？）"
    body = m.group(1)

    assert "LD_LIBRARY_PATH" not in body, \
        "manifest 又设了 LD_LIBRARY_PATH —— 它不在 _MEIPASS 下，剥不掉，会泄漏给沙箱应用"
    assert "GI_TYPELIB_PATH" not in body, \
        "manifest 又设了 GI_TYPELIB_PATH —— 它会被 pyi_rth_gi 覆盖，是死配置"
    assert "CC_CU_AT_SPI_CONF" in body, "manifest 应指向随包自带的 accessibility.conf"
    assert "TESSDATA_PREFIX" in body and "PATH" in body


# ==================== ③ vendor 收集的完整性 ====================

def test_vendor_bins_cover_every_external_command():
    """
    随包二进制必须覆盖代码里 `shutil.which` 解析的全部外部命令。

    少一个的表现是「目标机上某个能力静默消失」——例如漏了 at-spi2-registryd，
    沙箱内就没有无障碍树；漏了 tesseract，灰区应用就读不了屏。
    """
    vendored = set(re.findall(r'\("([\w.-]+)",\s*\(', _read("packaging", "vendor_libs.py")))

    needed: set[str] = set()
    src_root = os.path.join(REPO, "src", "computer_use_mcp")
    for dirpath, _dirs, files in os.walk(src_root):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(dirpath, fn), encoding="utf-8") as fh:
                needed |= set(re.findall(r'shutil\.which\(\s*"([\w.-]+)"', fh.read()))

    assert needed, "没扫到任何 shutil.which 调用（扫描逻辑失效了？）"
    assert needed <= vendored, f"这些外部命令没被随包收集: {sorted(needed - vendored)}"


def test_at_spi_registryd_has_explicit_path_candidates():
    """
    `at-spi2-registryd` **不在 PATH 里**（Debian 系放 /usr/libexec/），
    只用 `shutil.which` 会静默收集不到。它必须有显式路径候选。
    """
    src = _read("packaging", "vendor_libs.py")
    m = re.search(r'"at-spi2-registryd",\s*\((.*?)\)', src, re.S)
    assert m, "vendor_libs.py 里没有 at-spi2-registryd 的候选路径登记"
    assert "/usr/libexec/at-spi2-registryd" in m.group(1)


def test_vendor_collects_bundled_accessibility_conf():
    """
    必须自带 conf：系统那份 `/usr/share/defaults/at-spi2/accessibility.conf` 属
    at-spi2-core，随包再写一份就是 dpkg 文件冲突（安装直接失败，且目标机没有 apt 源可补救）。
    没有自带 conf 的话，目标机若没装 at-spi2-core 就起不了私有总线 —— 而症状只是
    「读不到元素树」，完全看不出是 conf 丢了。
    """
    src = _read("packaging", "vendor_libs.py")
    assert "AT_SPI_CONF_SRC_DIRS" in src, "没有自带 accessibility.conf 的来源目录登记"
    assert "VENDOR_AT_SPI_CONF" in src, "没有登记要收集的 conf 文件名"


# ==================== ④ 验收用例不许依赖宿主 locale ====================

def test_smoke_pins_zenity_button_labels():
    """
    验收用的 zenity 对话框必须**写死按钮标签**，不能靠它的本地化默认值。

    实测（2026-09-23）：原实现按中文默认标签「是」找按钮，开发机上一直绿 —— 宿主的
    zenity 认中文 locale；一到干净容器就红，那里没配 locale，GTK 渲染成 `Yes`/`No`，
    报「树里没有『是』按钮」。这**不是产品缺陷**，是用例偷偷依赖了宿主语言环境，
    属于「在开发机上永远重现不了」的那类假红 —— 最耗排查时间的一种。
    """
    src = _read("packaging", "deb", "mcp_smoke.py")
    assert "--ok-label" in src and "--cancel-label" in src, \
        "zenity 启动命令没有写死按钮标签（判据会随宿主 locale 翻转）"
    # 断言判据取的是那个写死的常量，而不是又硬编码回某个词
    assert "OK_LABEL" in src.split("re.search", 1)[-1], \
        "按标签找按钮时没有引用 OK_LABEL 常量"
    # 断言的表达式里不许出现中文字面量 —— 那正是会随 locale 翻转的东西。
    # （只查 `re.search` 那几行：报错文案「树里没有…」是给人看的中文，不算。）
    exprs = [ln for ln in src.splitlines() if "re.search" in ln]
    assert exprs, "没找到按标签找按钮的匹配表达式"
    for line in exprs:
        assert not re.search(r"[一-鿿]", line), \
            f"匹配表达式里出现了中文字面量（会随 locale 翻转）: {line.strip()}"


# ==================== ⑤ postinst 的 runuser 路径 ====================
#
# 这一组守的是 2026-09-23 的一次真实故障：装完 .deb **不注册到 Claude Code**。
#
# 根因不在 setup 脚本，而在 postinst 里那句
#     runuser -l -u "$u" -c '/usr/bin/cc-computer-use-setup --quiet' >/dev/null 2>&1 || true
# `-l/--login` 与 `-u/--user` 在 util-linux 里是**互斥**的，这条命令当场报
#     runuser: options --{shell,fast,command,session-command,login} and --user are mutually exclusive
# 然后什么都不做。而紧接着那句"已为该用户完成配置"是**无条件打印**的 ——
# 于是用户级配置整体静默失效，还把成功消息摆在用户面前。
#
# 更要命的是**验收全绿**：verify-in-container.sh 当时只测了
# 「直接 su - tester 跑 setup」这条手动路径，从没走过 apt → postinst → runuser。

_LOGIN_FLAGS = {"-l", "--login"}
_USER_FLAGS = {"-u", "--user"}


def test_postinst_runuser_flags_are_not_mutually_exclusive():
    """
    postinst 里的 runuser 调用**只能**是 `runuser -l <用户> -c <命令>`。

    `-l/--login` 与 `-u/--user` 互斥（util-linux），同时给会**直接报错退出、
    什么都不做**。判据是「同一行的 runuser 后面不许同时出现这两类开关」——
    这条规则直接来自 util-linux 的语义，不是对某种写法的偏好。
    """
    live = [ln for ln in _live_lines(_read("packaging", "deb", "postinst"))
            if "runuser" in ln]
    assert live, "postinst 里没有 runuser —— 用户级配置不会在安装时被触发"

    for ln in live:
        toks = ln.split()
        i = toks.index("runuser")
        flags = {t for t in toks[i + 1:] if t.startswith("-")}
        assert not (flags & _LOGIN_FLAGS and flags & _USER_FLAGS), (
            f"runuser 同时给了 --login 与 --user（util-linux 判为互斥，命令会直接失败、"
            f"什么都不做）: {ln.strip()}"
        )


def test_postinst_reports_failure_instead_of_claiming_success():
    """
    跑完 runuser 之后必须**按退出码**说话，不能无条件打印"已为该用户完成配置"。

    历史实现是 `runuser ... || true` 紧跟一句无条件的成功提示 —— 这正是本仓库
    CLAUDE.md 里反复警告的那类失败：「命令成功、完全无效、极难排查」。
    用户看到的是一句确定的好消息，而实际上无障碍开关没开、Claude Code 没注册。
    """
    live = _live_lines(_read("packaging", "deb", "postinst"))
    r_idx = next(i for i, ln in enumerate(live) if "runuser" in ln)

    assert re.match(r"\s*if\s+runuser\b", live[r_idx]), \
        "runuser 没有被 if 包住 —— 退出码根本没被检查"

    # 这段 if 的 else 分支：必须给出让用户能自救的下一步
    else_idx = next((i for i in range(r_idx + 1, len(live))
                     if re.match(r"\s*else\s*$", live[i])), None)
    fi_idx = next((i for i in range(r_idx + 1, len(live))
                   if re.match(r"\s*fi\s*$", live[i])), None)
    assert else_idx is not None and fi_idx is not None and else_idx < fi_idx, \
        "runuser 的 if 没有 else 分支（失败时什么都不说？）"

    else_body = " ".join(live[else_idx + 1:fi_idx])
    assert "cc-computer-use-setup" in else_body, \
        "失败分支没告诉用户怎么补救（应当提示手动跑 cc-computer-use-setup）"


def test_verify_simulates_the_real_install_path():
    """
    验收脚本必须真的走一遍 **apt → postinst → runuser → setup**。

    这是上面那个故障能潜伏下来的原因：验收只测了「直接以 tester 身份跑 setup」
    这条手动路径（它是通的，所以全绿），而**真正的安装路径**从没被执行过。
    只测组件、不测装配，等于没测。

    判据是「验收脚本里给 apt 装了 SUDO_USER」—— 那是 postinst 认出"有一个真实
    用户要配置"的唯一依据；不设它，postinst 会走到另一个分支（提示用户手动跑）。
    """
    src = _read("packaging", "deb", "verify-in-container.sh")
    live = _live_lines(src)

    assert any(re.search(r"\bSUDO_USER=", ln) for ln in live), \
        "验收没有模拟 `sudo apt install`（缺 SUDO_USER，postinst 不会去注册）"
    # 而且这次安装必须发生在假 claude 就位**之后**，否则注册无从谈起
    i_claude = next((i for i, ln in enumerate(live) if ".local/bin/claude" in ln), None)
    i_sudo = next((i for i, ln in enumerate(live) if re.search(r"\bSUDO_USER=", ln)), None)
    assert i_claude is not None, "验收里没装假 claude CLI"
    assert i_sudo is not None and i_claude < i_sudo, \
        "先装了包、后放 claude —— 那样测不到 postinst 的注册动作"


def test_doctor_searches_the_same_claude_locations_as_setup():
    """
    doctor 的 claude 探测必须与 setup 的 `find_claude()` 同源同序。

    历史实现里那个兜底是**死代码**：
        claude_bin="$(command -v claude)"
        if [ -z "$claude_bin" ]; then  soft ...          # ← 空的话在这一支
        else  [ -n "$claude_bin" ] || claude_bin="$(bash -lc ...)"   # ← 必然非空，永不执行
    于是「PATH 里没有、但装在 ~/.local/bin」会被误报成"未找到 claude"。
    doctor 的假阴性比不报更糟：用户会照着它去重装 Claude Code，而问题不在那儿。
    """
    src = _read("packaging", "deb", "cc-computer-use-doctor.in")
    assert ".npm-global/bin/claude" in src and ".local/bin/claude" in src, \
        "doctor 没有按固定安装位置兜底探测 claude"

    setup = _read("packaging", "deb", "cc-computer-use-setup.in")
    for cand in (".local/bin/claude", ".npm-global/bin/claude",
                 ".claude/local/claude", "/usr/local/bin/claude"):
        assert cand in setup, f"setup 的候选路径少了 {cand}（doctor 却按它找，两边会不一致）"
        assert cand in src, f"doctor 的候选路径少了 {cand}（与 setup 不同源）"


def test_shipped_shell_scripts_avoid_grep_q_in_pipelines():
    """
    随包脚本里**不许**出现 `… | grep -q …` 这类管线判据。

    为什么（2026-09-23 实测，被这条坑了两处）：
      `grep -q` 一命中就**立刻退出并关掉读端**，而上游进程往往还没写完 —— 上游于是
      吃到 EPIPE 而失败。在 `set -o pipefail` 下，整条管线的退出码就变成「上游失败」，
      **即使 grep 明明命中了**。实测：34 行、1362 字节的输出稳定触发；
      同一份输出换成 `grep -c`（要读完全文才退出）就正常 —— 所以与缓冲区大小无关，
      是 `-q` 提前退出本身。

    两个方向的危害不对称，但都真实：
      · 判「通过」时被误判成失败 → 医生对着健康的安装报 ❌（用户白折腾）；
      · 判「有问题」时被误判成没问题 → **真的缺库被静默放过**（健康检查失去意义）。
    第二种更坏，所以这条守的是整类写法，不是某一处。

    替代写法：先 `x="$(cmd)"`，再用 `case "$x" in *PAT*)` 或 `${x##*PAT}`——
    完全不经管线，没有退出码可污染。
    """
    targets = [
        ("packaging", "deb", "verify-in-container.sh"),
        ("packaging", "deb", "cc-computer-use-doctor.in"),
        ("packaging", "deb", "cc-computer-use-setup.in"),
        ("packaging", "deb", "probe-in-container.sh"),
    ]
    offenders = []
    for parts in targets:
        for ln in _live_lines(_read(*parts)):
            # 只抓「有管道的 grep -q」；`grep -q 文件` 上游是 grep 自己，无此问题
            if re.search(r"\|\s*grep\s+-q", ln):
                offenders.append(f"{'/'.join(parts)}: {ln.strip()}")
    assert not offenders, (
        "这些地方用了 `| grep -q`（grep 提前退出会让上游吃 EPIPE、"
        "pipefail 下管线判为失败）—— 改成先收变量 + case 模式匹配：\n  "
        + "\n  ".join(offenders)
    )


# ==================== ⑨ .deb 专项探针（probe-deb-fix.sh）====================

def test_probe_mounts_every_script_the_container_part_calls():
    """
    探针分两层（宿主脚本起容器 / 容器内脚本干活），靠 `-v` 把文件挂进去。

    **漏挂一个的表现是「容器里报 No such file」而不是「探针漏测」**，但那仍然要跑一次
    几分钟的 docker 才暴露。更坏的一种是挂了却忘了写进 targets、于是那条判据静默消失。
    这里直接对着「容器内脚本引用的 /tmp/*.py」清单去核对「宿主脚本挂了哪些」。
    """
    host = _read("packaging", "deb", "probe-deb-fix.sh")
    inner = _read("packaging", "deb", "probe-in-container.sh")

    referenced = set(re.findall(r"/tmp/(probe_[A-Za-z0-9_]+\.py)", inner))
    assert referenced, "容器内脚本里一个 probe_*.py 都没引用，正则或脚本结构变了"

    # 挂载写法是 `-v "$HERE/xxx.py":/tmp/xxx.py:ro` —— 名字在**引号之前**，
    # 所以正则要让 `[^"]*` 吃掉引号内的目录部分，再在闭合引号前取文件名。
    mounted = set(re.findall(r'-v\s+"[^"]*/([A-Za-z0-9_.]+)":/tmp/', host))
    missing = referenced - mounted
    assert not missing, (
        f"probe-deb-fix.sh 没有把这些脚本挂进容器：{sorted(missing)}"
        "（漏挂会在容器里报 No such file）"
    )


def test_probe_judges_are_not_tautologies():
    """
    探针的判据不能是「工具报了 ok」这类恒真条件 —— 两个 bug 的**共同特征**恰恰是
    "工具报成功、实际没生效"：`type_text` 恒报成功、`launch_app` 恒报 ok=True。
    所以判据必须是外部可观测量：子进程真收到的 argv、xev 真记录的按键。

    这条守卫钉住那两样东西在源码里确实存在（判据本身在容器里跑，单测跑不了 docker）。
    """
    launch = _read("packaging", "deb", "probe_launch.py")
    paste = _read("packaging", "deb", "probe_paste.py")
    inner = _read("packaging", "deb", "probe-in-container.sh")

    # 判据只看**会执行的行**：这几个文件的注释里大段写着「修复前是裸 ctrl+v」
    # 「Shift_L 会出现」之类的说明，拿原文断言等于在测注释 —— 实测把条件里的
    # `"Shift_L" not in keys` 整个删掉，注释里的 Shift_L 照样让守卫全绿。
    paste_live = "\n".join(_live_lines(paste))
    inner_live = "\n".join(_live_lines(inner))

    # ① launch 探针：假 gnome-terminal 把**子进程实际收到的参数**写进这个文件，
    #    判据必须落在它上面（只看返回的 JSON 是恒真的）。
    assert "gt-args.txt" in inner_live, "容器内脚本没有去读子进程实际收到的参数"
    assert "grep -qx" in inner_live, "容器内脚本没有对实际参数做精确断言"
    assert "launch_app" in launch, "launch 探针没有真的调 launch_app"

    # ② paste 探针：必须断言 xev 记录的按键里带 Shift，否则裸 ctrl+v 也会判通过
    assert '"Shift_L"' in paste_live, "paste 探针没有检查 Shift（那样裸 ctrl+v 也会判通过）"
    assert "xev" in paste_live, "paste 探针没有用 xev 观测真实按键"

    # ③ 两个 bug 都在真机上才暴露，所以容器内脚本必须起 Xvfb 当父显示
    assert "Xvfb" in inner_live, "容器内脚本没起 Xvfb（沙箱嵌在已有 X server 上，没它起不来）"
