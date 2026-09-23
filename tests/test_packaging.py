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
