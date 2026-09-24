"""
★ 平台边界守卫：`core/` / `tools/` / `utils/` 必须是平台无关的。

═══ 这条守卫为什么是加 Windows/macOS 支持的**地基** ═══

本项目的 `core/` 里原先到处都是隐式的 Linux 假设：`grep platform.system` 一条都搜不到，
但 `core/display/`（Xephyr + 私有 AT-SPI 总线）、`core/serializer.py` 的角色词表、
`core/coordinator/` 里对 `display` 的直接 import，全是 Linux 硬写。

**隐式假设最可怕的地方是它不报错**：加第二个平台时，某人写了一行
`if platform.system() == "win32"` 塞在 `core/` 中间，测试全绿、功能可用，
而"平台差异只允许存在于三处"这条设计就此破功——之后每加一个平台都要做一次
「考古式」的逐处修改。所以这条规则不能靠纪律维持，必须由测试守。

═══ 允许平台差异存在的地方（只有这三处）═══

    1. `backend/<platform>/` 之内      —— 平台 API、策略、常量、驱动
    2. `backend/base.py` 的 get_backend() —— 唯一的工厂分支
    3. `core/session.py`               —— 唯一的会话/隔离分支（层 3）

═══════ 判据纪律（本项目有血证，读之前先读这条）═══════

拆 `Coordinator` 成 mixin 那次，三条反射守卫里**两条静默变成空集合、全绿但零覆盖**，
只有一条因为「豁免名单全成了死登记」才变红。所以本文件的每条断言都必须满足：

  · **尺子不许变瞎**：扫描结果为空要 fail，不能"空集 ⊆ 空白名单 → 通过"；
  · **每条规则都要有正控**：同一把尺子必须在"本该命中"的地方命中；
  · **每个豁免都要有正控**：被豁免的文件必须真的还含那个模式（否则豁免已过期，
    说明它保护的代码早就搬走了，留着只会误导下一个人）。

⚠️ 尺子是 **AST（只认真实 import / 真实调用）**，不是全文子串 —— 理由见
`tests/_importscan.py` 的模块 docstring（正则版会在写下这条规则的同时把自己打红：
说明文字里必然要举 `platform.system()` 这个例子）。
"""

from __future__ import annotations

import pathlib

import pytest

from _importscan import module_imports, platform_decisions, scan, scan_platform_decisions

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"

# 平台专属的 Python 包。core/tools/utils 里出现任何一个都算越界。
# 注意 `gi` 与 `Xlib` 是**现有**的 Linux 依赖，`uiautomation`/`comtypes`/`Quartz` 等
# 是为将来准备的——守卫要能提前拦住它们，而不是等 Windows 代码写进来才发现。
_PLATFORM_LIBS = (
    "gi", "Xlib",
    "uiautomation", "comtypes", "pywinauto", "win32gui", "win32api", "win32com",
    "Quartz", "AppKit", "ApplicationServices", "objc",
)

# `core/display/` 是**刻意豁免**的：它本轮仍是「把 Linux 当唯一事实硬写」的实现
# （Xephyr / DISPLAY / AT-SPI 总线），且刻意不做重构（理由见
# `core/display/provider.py` 的模块 docstring：重构会打断 55 处既有测试补丁）。
# 这个豁免**有正控**：见 test_display_exemption_is_doing_work。
_DISPLAY_EXEMPT = "core/display"

# 允许做平台判定的文件。每一项都**必须真的含**该模式（见 test_decision_allowlist_is_not_stale）。
_DECISION_ALLOWED = {
    "backend/base.py": "get_backend() —— 唯一的 backend 工厂分支",
    "core/session.py": "层 3 —— 唯一的会话/隔离 provider 分支",
}

# 允许 import 平台库的例外（按文件名）。
# `_bootstrap.py` 要 import gi 才能配 GI 环境；它已按 IS_LINUX 早退，
# 且**自己不写 platform.system()**（消费 core.session.IS_LINUX）。
_LIB_IMPORT_ALLOWED = {
    "_bootstrap.py": "gi/Atspi 引导（Linux 专属）；非 Linux 时 setup_gi_environment 整体 no-op",
}

# `core/` 里允许 import `core.display` 的文件 —— 应当**只有**会话层那一个。
_DISPLAY_IMPORTERS_ALLOWED = {
    "core/session.py": "层 3 的 Linux 分支（返回 display.MANAGER 本体）",
}

# `core/` 不得 import 的平台 backend 包前缀（`backend.base` 不在其中，那是契约本身）。
_PLATFORM_BACKEND_PREFIXES = ("backend.linux.", "backend.windows.", "backend.mac.")


def _rel_py_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("*.py"))


# ==================== ① 尺子本身：不许变瞎 ====================

def test_scanner_is_not_blind():
    """
    防「rglob 路径写错 / 目录改名 → 命中 0 个文件 → 断言空集 ⊆ 空白名单 → 永远通过」。

    这是本仓库已经用过的纪律（`tests/test_spawn_env.py::test_spawn_sites_are_found`
    是同一条）：一条"扫了个空"的守卫看起来和"代码很干净"一模一样，不设这条就会
    在下一次目录调整时静默失效。
    """
    files = _rel_py_files(_SRC)
    assert len(files) >= 55, (
        f"只扫到 {len(files)} 个 .py —— src 布局变了或路径写错了，"
        f"本文件其余断言会全部变成空转"
    )
    # 正控：扫出来的路径里必须包含我们**已知存在**的那几个关键文件
    rels = {str(p.relative_to(_SRC)) for p in files}
    for must in ("core/session.py", "backend/base.py", "backend/linux/atspi.py"):
        assert must in rels, f"扫描没覆盖到 {must} —— 路径基准错了"


# ==================== ② core/tools/utils 不许碰平台库 ====================

def test_core_tools_utils_are_platform_free():
    """
    `core/`（除 `core/display/`）、`tools/`、`utils/` 里不得出现平台库的 import。

    「平台差异只允许存在三处」中的第一、三条就靠它守。
    """
    offenders = []
    scanned = 0
    for part in ("core", "tools", "utils", "backend/common"):
        root = _SRC / part
        if not root.is_dir():
            continue
        scanned += len(_rel_py_files(root))
        for rel, mod in scan(root, banned_modules=_PLATFORM_LIBS):
            full = f"{part}/{rel}"
            if full.startswith(_DISPLAY_EXEMPT) or full in _LIB_IMPORT_ALLOWED:
                continue
            offenders.append(f"{full} -> {mod}")

    # 把 `_bootstrap.py`（在顶层，不在上面三个目录里）也算上，它的豁免理由同上
    for rel, mod in scan(_SRC, banned_modules=_PLATFORM_LIBS):
        if "/" in rel:            # 只看顶层文件
            continue
        if rel in _LIB_IMPORT_ALLOWED:
            continue
        offenders.append(f"{rel} -> {mod}")

    assert scanned > 30, f"只扫到 {scanned} 个文件，扫描范围不对"
    assert not offenders, (
        "平台库 import 出现在平台无关层：\n  " + "\n  ".join(offenders) +
        "\n平台差异只允许存在于 backend/<platform>/ 与 core/session.py（见模块 docstring）"
    )


def test_core_does_not_reach_into_platform_backends():
    """
    `core/` 不得 import `backend.linux.*` / `backend.windows.*` / `backend.mac.*`。

    （`backend.base` 允许——那是契约本身，平台无关。）

    这条是 `core/serializer.py` 那次迁移的**执行器**：它在迁移前必红
    （当时 serializer 硬 import 了 `backend.linux.atspi` 的角色词表）。
    正控：先拿合成代码验尺子，再拿真代码跑。
    """
    # —— ⚠️ 正控必须走**同一个扫描器**（写一个临时文件喂给它），不能另写一条
    #    更严的正则来"证明尺子好用"——那种自证是循环论证。
    for form in ("from ..backend.linux.atspi import ACTIONABLE_ROLES",
                 "from computer_use_mcp.backend.windows.driver import uia",
                 "import backend.mac.session"):
        mods = _modules_of(form)
        assert any(m.startswith(p) for m in mods for p in _PLATFORM_BACKEND_PREFIXES), \
            f"尺子认不出这种写法：{form!r}（解析出 {mods}）—— 绝对/相对两种写法必须归一"
    # 反控：契约本身不该被误伤
    assert not [m for m in _modules_of("from ..backend.base import Rect")
                if any(m.startswith(p) for p in _PLATFORM_BACKEND_PREFIXES)]

    hits = scan(_SRC / "core", banned_prefixes=_PLATFORM_BACKEND_PREFIXES)
    offenders = [f"core/{rel} -> {mod}" for rel, mod in hits]
    assert not offenders, (
        "core/ 直接依赖了某平台的 backend 实现：\n  " + "\n  ".join(offenders)
    )


def _modules_of(src: str) -> list[str]:
    """把一段源码**真的写成文件**再喂给同一个扫描器。

    刻意不在这里重写一遍 AST 逻辑：那等于"用另一把尺子证明这把尺子准"。
    落盘到临时目录的开销可以忽略，换来的是正控与生产判据**逐字同源**。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "probe.py"
        path.write_text(src, encoding="utf-8")
        return module_imports(path)


# ==================== ③ core/ 只有会话层能认识 display ====================

def test_core_only_session_imports_display():
    """
    `core/` 里只有 `core/session.py` 能 import `core.display`。

    这是「统一接口层」的存在意义本身：`core/display/` 是 X11 专属实现，
    一旦别的 core 模块直接依赖它，「换平台只改一处」就不成立了。

    （`core/display/` 包**内部**的子模块互相 import 不算——扫描时排除该目录。）

    正控：会话层必须真的含那行 import，否则本规则是把坏尺（拼错的针永远量不到东西）。
    """
    hits = []
    for rel, mod in scan(_SRC / "core", banned_modules=("display",)):
        if rel.startswith("display/") or rel == "display":
            continue          # 包内部互相 import，不算越界
        if f"core/{rel}" in _DISPLAY_IMPORTERS_ALLOWED:
            continue
        hits.append(f"core/{rel} -> {mod}")

    assert not hits, (
        "core/ 里有会话层之外的模块直接 import 了 display：\n  " + "\n  ".join(hits)
    )

    # 正控：豁免的那个文件必须真的 import 了 display。
    # 若它不再 import（比如将来改为工厂注入），这条豁免就该删掉——留着会让下一个人
    # 以为"这里有个合法的 display 依赖"，而其实规则已经名存实亡。
    session_imports = module_imports(_SRC / "core" / "session.py")
    assert "display" in session_imports, (
        "core/session.py 不再 import display —— 那 core/ 就完全不该出现 display 了，"
        "请把 _DISPLAY_IMPORTERS_ALLOWED 里的这条豁免删掉（死豁免会误导人）"
    )


# ==================== ④ 平台判定只在两个文件里 ====================

def test_platform_decisions_live_in_exactly_two_files():
    """
    `platform.system()` / `sys.platform` / `os.name ==` 全仓**只允许**出现在
    `backend/base.py`（工厂）与 `core/session.py`（会话分支）。

    为什么钉死数量：这正是「平台差异只允许存在于三处」中第三条的机械化。
    分散的平台判断是这类项目最常见的技术债——每加一个平台就要满仓库找 `if platform`，
    而且**漏改一处不会报错**（只会表现为某个平台上行为怪怪的）。
    """
    found = scan_platform_decisions(_SRC)

    offenders = {rel: hits for rel, hits in found.items() if rel not in _DECISION_ALLOWED}
    assert not offenders, (
        "平台判定出现在不允许的文件里：\n  " +
        "\n  ".join(f"{rel}:{ln} {form}" for rel, hs in offenders.items() for ln, form in hs) +
        "\n平台差异只允许存在于 backend/<platform>/ 与 core/session.py；"
        "要在别处判平台，请改为消费 core.session.IS_LINUX。"
    )

    # 正控：豁免名单不许过期（那些文件必须**真的**还在做平台判定）
    for rel in _DECISION_ALLOWED:
        assert rel in found, (
            f"{rel} 不再做平台判定了 —— 请把 _DECISION_ALLOWED 里的这条豁免删掉"
            "（死豁免会让下一个人以为这里还有个平台分支）"
        )


def test_platform_decision_scanner_ignores_docstrings():
    """
    尺子自检：注释 / docstring 里的 `platform.system()` **不得**被判成平台判定。

    这条不是洁癖：本文件与 `core/session.py` 的说明里**必须**写「全仓只有这两个文件
    允许出现 platform.system()」。正则版尺子会在写下这条规则的同时把自己打红——
    逼着人删文档给守卫让路（实测差点发生）。AST 版从构造上不会。
    """
    import ast

    src = '''
"""说明文字里提到 platform.system() 是允许的。"""
# 注释里提到 sys.platform 也是允许的
X = "os.name == 只是个字符串"
def f():
    return platform.system()      # ← 只有这个是真的
'''
    tree = ast.parse(src)
    real = [n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "system"
            and isinstance(n.value, ast.Name) and n.value.id == "platform"]
    assert len(real) == 1, f"AST 尺子把 docstring/注释也算进来了：{len(real)} 处命中"


# ==================== ⑤ 豁免的有效性（防「死豁免」）====================

def test_display_exemption_is_doing_work():
    """
    `core/display/` 的豁免必须是**有效**的：它确实是被排除掉的那块，不是"早就空了"。

    若哪天 `core/display/` 被重构掉（平台代码迁走），这个豁免就成了死登记——
    而"豁免名单全成了死登记"正是本项目上一次发现守卫静默失效的那条副作用式检查
    （拆 mixin 时三条守卫里两条变空集合，只有这条因为副作用才红）。
    """
    root = _SRC / "core" / "display"
    assert root.is_dir(), "core/display/ 不在了 —— 请删掉 _DISPLAY_EXEMPT 豁免"

    # 它必须真的含有会被规则拦住的东西（X11 / DISPLAY 是它的存在理由）
    text = "\n".join(p.read_text(encoding="utf-8") for p in _rel_py_files(root))
    assert "DISPLAY" in text, (
        "core/display/ 里已经没有 DISPLAY 了？那它不该再被豁免 —— 请复查这条豁免是否还成立"
    )
    # 正控取「它确实是平台代码」而不是「它能触发上面那条 import 规则」：
    # 实测这个包**并不 import** gi/Xlib，它是靠 subprocess 驱动 xdotool / Xephyr
    # 这两条外部命令来干活的 —— 用"有没有平台库 import"当正控会永远量不到东西，
    # 而量不到却报错的话，这条守卫就成了一个假警报源（比没有更糟）。
    for hall in ("Xephyr", "AT-SPI", "DISPLAY"):
        assert hall in text, (
            f"core/display/ 里已经找不到 {hall!r} —— 它不再是「Linux 硬写」的那个包了，"
            "请复查这条豁免是否还成立（死豁免会误导下一个人）"
        )


def test_lib_import_allowlist_is_not_stale():
    """`_bootstrap.py` 的豁免同样不许过期：它必须真的还在 import gi（或从其引导函数里）。"""
    boot = _SRC / "_bootstrap.py"
    assert boot.is_file(), "_bootstrap.py 不在了 —— 请删掉 _LIB_IMPORT_ALLOWED 里的豁免"
    text = boot.read_text(encoding="utf-8")
    assert "gi" in text, (
        "_bootstrap.py 已经不提 gi 了 —— 豁免无对象，请删掉它"
    )


# ==================== ⑥ manager 单例的可见性（防拼写漂移）====================

def test_display_manager_module_is_the_only_place_constructing_it():
    """
    `DisplayManager()` 只允许在 `core/display/manager.py` 里被构造。

    为什么值得单独一条：`MANAGER` 是进程级单例，而"另一个地方也 new 了一个"是本项目
    世界观里最要命的错误（ref 表、屏锁、AT-SPI 总线全部假设只有一份）。
    它的构造点目前唯一，且**必须**保持唯一——Windows 那边将来也只有一个 `NoIsolationSession`。
    """
    offenders = []
    for path in _rel_py_files(_SRC):
        rel = str(path.relative_to(_SRC))
        if rel == "core/display/manager.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "DisplayManager()" in text:
            offenders.append(rel)
    assert not offenders, f"DisplayManager 在别处被构造（单例假设会破）：{offenders}"
