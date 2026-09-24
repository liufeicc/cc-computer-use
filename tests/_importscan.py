"""
import 级扫描器（tests/_importscan.py）—— 给「平台边界」那组守卫共用。

═══ 为什么要有它，而不是各守卫自己写字符串匹配 ═══

本项目此前有两条守卫用**全文子串匹配**（`"Xlib" in text`），两个方向都会坏：

  · **漏报（假绿）**：只认某一种相对 import 写法，换成绝对导入就一条都匹配不到 ——
    守卫静默失效，比变红危险得多。
  · **误报（假红）**：注释/docstring 里提一句 "Xlib" 就算命中。加 Windows 支持时
    立刻咬人：`core/session.py` 的说明本来就要举「平台库（Xlib / ctypes.windll）
    不得下渗」这个例子，一写就红 —— 逼着人删文档给守卫让路，本末倒置。

按 **AST 取 import 语句**两头都解决：只认真实 import，且认得出所有写法
（`import X`、`from X import Y`、`from .a.b import c`、以及**函数内**的 import）。

⚠️ 必须 `ast.walk` 整个模块、不能只看 `tree.body` 的模块级 import：
实测 `backend/linux/ring.py` 的 `from Xlib import display` 就在**函数内**——
只看模块级会让「正控」量不到东西，而正控量不到 = 判据已经失效却还全绿。
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterable


def module_imports(path: pathlib.Path) -> list[str]:
    """
    返回该文件 import 的**模块名**列表（已归一化为点分全名）。

    归一化规则（按需扩展，宁可少归一也不要猜错）：
      · `from .a.b import c`  → 记 `a.b`（相对层级信息丢弃：守卫关心的是"碰到了哪个平台包"）
      · `from ..backend.linux import ring` → 记 `backend.linux`，**同时**记 `backend.linux.ring`
        （后者让 `banned_prefixes` 能精确到子模块）
      · `import Xlib.display as d` → 记 `Xlib.display`
      · `from . import display`（module 为 None 的纯相对导入）→ 记 `display`

    ⚠️ 最后那条容易漏：`ast.ImportFrom.module` 在「只有点、没有模块名」时是 **None**，
    顺着 `if base:` 写下去就会把它整个跳过 —— 而 `core/session.py` 里那行
    `from . import display` 正是这个形状（写漏的后果是"core 只有 session 能 import display"
    这条规则**永远扫不到那个唯一该被放行的点**，规则变成空转）。

    另外统一剥掉 `computer_use_mcp.` 前缀：守卫关心的是「碰到了哪个平台包」，
    而既能写 `from ..backend.linux.atspi import X`（归一化后 `backend.linux.atspi`）
    也能写 `from computer_use_mcp.backend.linux.atspi import X`（不剥就是
    `computer_use_mcp.backend.linux.atspi`）——**两种写法必须判成同一件事**，
    否则守卫只拦得住其中一种（实测：这正是上一版字符串匹配守卫的漏报形态）。

    语法错的文件按「无 import」处理并**静默跳过**：让 py 文件编译失败是另一个问题，
    不该表现为"平台边界守卫炸了"。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                out.append(base)
            for alias in node.names:
                # 有 base 记「base.alias」（能精确到子模块）；无 base（`from . import x`）
                # 就记 alias 本身 —— 两者都要，别因为 base 为空就把整次导入丢掉。
                out.append(f"{base}.{alias.name}" if base else alias.name)
    return [_strip_pkg_prefix(m) for m in out]


_PKG_PREFIX = "computer_use_mcp."


def _strip_pkg_prefix(mod: str) -> str:
    """剥掉本包自己的顶层前缀，让绝对/相对两种写法归一到同一个名字（见 module_imports）。"""
    return mod[len(_PKG_PREFIX):] if mod.startswith(_PKG_PREFIX) else mod


# 「平台判定」的 AST 判据。
#
# ⚠️ 为什么不能用正则扫源码文本：注释与 docstring 里**一定会**出现这些字样——
# `core/session.py` 的说明要写「全仓只有本文件与 backend/base.py 允许出现
# platform.system()」，`_bootstrap.py` 的 docstring 也要写「本模块不写 platform.system()」。
# 正则版会在**写下这条规则的同时**把自己打红，逼着人删文档给守卫让路。
# AST 只认真正的调用/属性访问，从构造上不会命中注释。
_PLATFORM_DECISION_FORMS = {
    # (值表达式名, 属性名) → 人类可读的说明
    ("platform", "system"): "platform.system()",
    ("sys", "platform"): "sys.platform",
}


def platform_decisions(path: pathlib.Path) -> list[tuple[int, str]]:
    """
    找出该文件里的「平台判定点」，返回 [(行号, 形态), ...]。

    认三种形态（都是**真代码**，不是注释）：
      · `platform.system()`  —— 属性访问 + 调用
      · `sys.platform`       —— 属性访问
      · `os.name == …`       —— 比较表达式（单独判，因为它是 `os.name` 不是属性链的终点）
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            # `platform.system` / `sys.platform`
            value = node.value
            if isinstance(value, ast.Name):
                key = (value.id, node.attr)
                if key in _PLATFORM_DECISION_FORMS:
                    out.append((node.lineno, _PLATFORM_DECISION_FORMS[key]))
            # `os.name == ...` 只需要 `os.name` 这个属性访问存在且处在比较里
            if isinstance(value, ast.Name) and value.id == "os" and node.attr == "name":
                out.append((node.lineno, "os.name"))
    # 同一个 `os.name` 可能被上面两条都命中（比较式里只有一个 Attribute 节点，故不会重复），
    # 去重排序，让输出稳定可断言。
    return sorted(set(out))


def scan_platform_decisions(root: pathlib.Path) -> dict[str, list[tuple[int, str]]]:
    """扫 `root` 下全部 `.py` 的平台判定点，只返回**有命中**的文件。"""
    out: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(root.rglob("*.py")):
        hits = platform_decisions(path)
        if hits:
            out[str(path.relative_to(root))] = hits
    return out


def scan(root: pathlib.Path,
         banned_modules: Iterable[str] = (),
         banned_prefixes: Iterable[str] = ()) -> list[tuple[str, str]]:
    """
    扫描 `root` 下所有 `.py`，返回 [(相对路径, 命中的模块名), ...]。

    两种判据：
      · `banned_modules` —— **精确等于**某模块名，或它的子模块（`Xlib` 命中 `Xlib.display`）
      · `banned_prefixes` —— 模块名以该前缀开头（用于精确到子模块，如 `backend.linux.ring`）
    """
    mods = tuple(banned_modules)
    prefixes = tuple(banned_prefixes)
    hits: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root))
        for name in module_imports(path):
            if name in mods or any(name.startswith(m + ".") for m in mods) \
                    or any(name.startswith(p) for p in prefixes):
                hits.append((rel, name))
    return hits
