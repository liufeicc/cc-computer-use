"""
静态健康检查：把「Linux 上永远不会被执行」的那部分代码也照一遍。

═══ 为什么需要这个文件 ═══

本项目是「一套 core + 多个平台 backend」。平台专属的那半边（`backend/windows/`，
将来还有 macOS）在开发机（Linux）上**只被 import、从不被执行** ——
import 一个模块不会跑它的函数体。

于是有一整类错误能在全绿的测试套件下存活，只在真机上炸：
**引用了不存在的全局名**。2026-09-28 实测踩到：`backend.py` 里 13 处把 `win32`
写成了 `w.`，8 个方法受影响 —— 注入那一半一次都没跑通过，而 391 条测试全绿。

判据不能是"跑一遍看看"（跑不了），只能是**静态解析**。这里用标准库的 `symtable`：
把每个函数体里「引用了、但既不是局部名、也不是模块级绑定、也不是内置名」的全局名报出来。
不需要装 linter，也不给项目加依赖。

⚠️ 与 `test_windows_driver_guards.py` 的分工：那个文件守的是**驱动层的结构契约**
（单点 import、零子进程、惰性平台绑定）；本文件守的是**"这段代码根本跑不起来"**，
而且**适用于任何平台目录**，将来加 macOS 时自动覆盖。
"""

from __future__ import annotations

import builtins
import io
import pathlib
import symtable
import tokenize

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"

# `super()` 在类体里会生成一个名叫 `__class__` 的隐式闭包单元，`symtable` 把它
# 归成全局引用。它**不是**错误 —— 这是全项目唯一登记在案的例外。
_ALLOWED = frozenset({"__class__"})

_BUILTINS = frozenset(dir(builtins))


def _module_bindings(table: symtable.SymbolTable) -> set[str]:
    """模块顶层绑定了哪些名字（赋值 / import / def / class / 参数）。"""
    return {s.get_name() for s in table.get_symbols()
            if s.is_assigned() or s.is_imported()
            or s.is_namespace() or s.is_parameter()}


def _walk(table: symtable.SymbolTable, func: str, bound: set[str],
          out: list[tuple[str, str]]) -> None:
    """
    递归找「未定义的全局名」。

    `bound` 是**外层作用域可见的名字**（模块绑定 + 各层函数的局部/参数）——
    闭包里的自由变量也是合法的，所以每往下一层就并进那一层的局部名。
    """
    for child in table.get_children():
        name = child.get_name()
        if child.get_type() == "function":
            local = {s.get_name() for s in child.get_symbols()
                     if s.is_local() or s.is_parameter()
                     or s.is_imported() or s.is_namespace()}
            for sym in child.get_symbols():
                n = sym.get_name()
                if (sym.is_referenced() and n not in local
                        and n not in bound and n not in _BUILTINS
                        and n not in _ALLOWED):
                    out.append((name, n))
            _walk(child, name, bound | local, out)
        else:
            # 类体：类名本身在父作用域里可查，但类体有自己的作用域规则，
            # 简单起见只递归、不并进局部名（内部类/方法里的引用照样会被检查）。
            _walk(child, func, bound, out)


def undefined_globals(source: str, filename: str) -> list[tuple[str, str]]:
    """
    `[(所在函数, 未定义的名字), …]`。语法错时抛 SyntaxError（调用方负责展示）。

    ⚠️ 刻意**不**在函数内 try/except 兜异常：兜住的话，一个写坏的文件会
    静默变成"没有未定义名"，正是这个文件要防的那类失效。
    """
    table = symtable.symtable(source, filename, "exec")
    out: list[tuple[str, str]] = []
    _walk(table, "<module>", _module_bindings(table), out)
    return out


# ==================== ① 全 src 扫一遍 ====================

def test_no_undefined_global_names_in_src():
    """
    整个 `src/` 里不许有「引用了但谁都没定义」的全局名。

    正控：扫描必须**真的扫到了东西** —— 文件数为 0、或一个函数都没进去，
    这条会红。否则路径写错、`rglob` 模式改了，都会让本规则变成"扫了个空 → 全绿"。
    """
    files = sorted(_SRC.rglob("*.py"))
    assert len(files) > 50, (
        f"只扫到 {len(files)} 个文件 —— 路径或匹配模式坏了，本规则正在空转。"
    )

    problems: list[str] = []
    functions_seen = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        table = symtable.symtable(source, str(path), "exec")
        functions_seen += sum(1 for _ in _iter_functions(table))
        for func, name in undefined_globals(source, str(path)):
            problems.append(f"{path.relative_to(_SRC.parent.parent)}:{func} → {name!r}")

    assert functions_seen > 500, (
        f"只遍历到 {functions_seen} 个函数 —— symtable 的走法可能改了，本规则正在空转。"
    )
    assert not problems, (
        "以下位置引用了谁都**没有定义**的全局名（在非目标平台上 import 时不会报错，"
        "只有真跑到那一行才炸）：\n  " + "\n  ".join(problems)
    )


def _iter_functions(table: symtable.SymbolTable):  # noqa: ANN202
    for child in table.get_children():
        if child.get_type() == "function":
            yield child
        yield from _iter_functions(child)


# ==================== ② 反证：这个扫描器真的抓得到 ====================

def test_the_scanner_catches_a_planted_undefined_name(tmp_path):
    """
    ⚠️ 反证用例：往临时文件里**种**一个未定义名，扫描器必须报出来。

    为什么单列一条：本文件的判据是「没有问题」，而"扫描器坏了"与"代码没问题"
    在结果上**长得一模一样**（都是空列表）。2026-09-28 实测正是这样：
    `w.` 那 13 处存活了一整轮，因为没有任何东西检查过这件事。

    这条用**真源码的形状**（模块级 import + 函数里引用一个没导入的名字），
    不是构造一个畸形文件 —— 要验的是"抓得到这个错误"，不是"抓得到语法怪东西"。
    """
    fake = tmp_path / "planted.py"
    fake.write_text(
        "import ctypes\n"
        "\n"
        "def uses_the_wrong_alias():\n"
        "    return w.get_cursor_pos()\n",
        encoding="utf-8",
    )
    got = undefined_globals(fake.read_text(encoding="utf-8"), str(fake))
    assert ("uses_the_wrong_alias", "w") in got, (
        f"扫描器没抓到种下去的未定义名 —— 它坏了，而它坏掉的样子与「代码干净」一样。"
        f"实际报出：{got}"
    )


def test_the_scanner_does_not_flag_legitimate_closure_and_builtins(tmp_path):
    """
    反向：合法的闭包自由变量、内置名、`super()` 的 `__class__` **都不许**报。

    误报比漏报更糟：一条总在响的告警会被无视，然后连真问题一起被无视。
    """
    fake = tmp_path / "legit.py"
    fake.write_text(
        "import ctypes\n"
        "\n"
        "OUTER = 1\n"
        "\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "\n"
        "    def ok(self, x):\n"
        "        def inner():\n"
        "            return len(str(x)) + OUTER + ctypes.sizeof(ctypes.c_int)\n"
        "        return inner()\n",
        encoding="utf-8",
    )
    got = undefined_globals(fake.read_text(encoding="utf-8"), str(fake))
    assert got == [], f"误报：{got}"


@pytest.mark.parametrize("path", ["backend/windows", "backend/linux"])
def test_platform_packages_are_scanned(path):
    """
    平台目录必须**确实被扫到**（它们正是这个文件存在的理由）。

    ⚠️ 判据取"扫到了文件且其中能进到函数体"，不取"没问题" —— 后者在目录改名后
    会静默变成"零文件 → 无问题 → 绿"。
    """
    root = _SRC / path
    files = sorted(root.rglob("*.py"))
    assert files, f"{path} 下一个 .py 都没有 —— 目录挪了，扫描范围已经没覆盖它。"

    functions = 0
    for f in files:
        functions += sum(1 for _ in _iter_functions(
            symtable.symtable(f.read_text(encoding="utf-8"), str(f), "exec")))
    assert functions > 20, (
        f"{path} 只解析出 {functions} 个函数 —— 它没被真正检查到。"
    )


# ==================== ③ 比声明地板更新的语法（PEP 701）====================
#
# ═══ 为什么这个文件要管语法版本 ═══
#
# 2026-09-28 真机实测：Windows 客体的 Python 是 **3.11**，而 `server.py` 里有一个
# **跨行写的 f-string 表达式**（那种写法要 Python **3.12** 的 PEP 701 才允许）。
# 后果不是"某个功能不好用"，是 **`import computer_use_mcp.server` 整个失败** ——
# 报错是 `SyntaxError: unterminated string literal (detected at line 223)`，
# 指向那一行，**读起来像"引号写错了"**，实际是"解释器版本低"。而开发机是 3.12，
# 本地全绿、永远看不到。
#
# 这与本文件的主题是同一件事：**"只在本机成立"的假设**。前面两条管运行时
# （引用了不存在的全局名），这一条管**解析期**。
#
# ═══ 判据为什么用 tokenize 而不是 ast ═══
#
# `ast.parse(src, feature_version=(3, 11))` **拦不住**这个（实测：3.12 的解释器用
# feature_version=3.11 解析跨行 f-string 照样通过 —— f-string 的内容在词法阶段就已
# 被切成 token，feature_version 只约束语法产生式）。唯一忠实的判据是**真实的
# 3.11 解释器**，可它未必装（本机有、CI 未必）。故改用 tokenize 直接认 PEP 701 的
# 两条特征写法，见 `pep701_violations`。

_FSTRING_START = getattr(tokenize, "FSTRING_START", -1)
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", -1)
_FSTRING_END = getattr(tokenize, "FSTRING_END", -1)


def pep701_violations(source: str) -> list[tuple[int, str]]:
    """
    找出只在 Python ≥3.12（PEP 701）下合法的 f-string 写法，返回 `[(行号, 说明)]`。

    认两条（都是**真实会写出来**的形状）：

      ① **非三引号 f-string 字面量跨行** —— `f"a {x,\\n  x}"`。
         <3.12 的词法分析器把整个字面量当成一个字符串扫，遇到换行就报
         "unterminated string literal"（正是这次踩的）。
         ⚠️ 三引号跨行（`f\"\"\"...\\n...\"\"\"`）**历来合法**，不能一并拦；
         隐式拼接（`f"a" "b"`）也不算 —— 每个字面量各自成对，同样不能拦。
      ② **替换域里用了与字面量相同的引号** —— `f"{d["k"]}"`。
         <3.12 里这会让外层字面量提前闭合。

    ⚠️ **不覆盖**「替换域里出现反斜杠」（`f"{'\\n'}"` 也是 3.12-only）。它要在
    表达式内部逐 token 判，而误报（把合法的转义当违规）比漏报更糟 ——
    一条总在响的告警会被无视。故如实收窄，只认上面两条。

    实现：`tokenize` 在 3.12 里把 f-string 切成 FSTRING_START / …/ FSTRING_END，
    中间**替换域**（`{…}`）是普通的 OP/NAME/STRING token，故能精确区分
    "这是字面量文本" 与 "这是表达式"。
    """
    out: list[tuple[int, str]] = []
    # 每层 f-string 一项：(起始行, 引号字符, 是否三引号, 当前花括号深度)
    stack: list[list] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out          # 词法本身有问题 → 交给别的守卫（不是本条的职责）
    for tok in toks:
        if tok.type == _FSTRING_START:
            quote = tok.string.lstrip("fFrRbBuU")
            stack.append([tok.start[0], quote[:1], len(quote) == 3, 0])
            continue
        if tok.type == _FSTRING_END:
            if stack:
                line, _q, triple, _d = stack.pop()
                if not triple and tok.start[0] != line:
                    out.append((line, "非三引号 f-string 字面量跨行（<3.12 是语法错）"))
            continue
        if not stack:
            continue
        top = stack[-1]
        if tok.type == tokenize.OP and tok.string in "{}":
            # 只数**替换域**的花括号；格式说明里的嵌套花括号自然被同一计数覆盖
            top[3] += 1 if tok.string == "{" else -1
        elif top[3] > 0 and tok.type == tokenize.STRING:
            if tok.string[:1] == top[1]:
                out.append((tok.start[0],
                            f"替换域里用了与外层相同的引号 {top[1]}（<3.12 是语法错）"))
    return out


def test_no_pep701_syntax_in_src():
    """
    整个 `src/` 不许出现 PEP 701 专属写法 —— 那会让 Python <3.12 **连 import 都做不到**。

    正控：必须真的扫到了 f-string（本仓大量使用）。否则 tokenize 的 API 一变，
    这条会静默退化成"一个 f-string 都没看见 → 没问题 → 绿"。
    """
    files = sorted(_SRC.rglob("*.py"))
    assert len(files) > 50, f"只扫到 {len(files)} 个文件 —— 路径坏了，本规则在空转。"

    problems: list[str] = []
    fstrings_seen = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        try:
            fstrings_seen += sum(
                1 for t in tokenize.generate_tokens(io.StringIO(source).readline)
                if t.type == _FSTRING_START)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
        for line, why in pep701_violations(source):
            problems.append(f"{path.relative_to(_SRC.parent.parent)}:{line} → {why}")

    assert fstrings_seen > 50, (
        f"只认出 {fstrings_seen} 个 f-string —— 判据在空转（tokenize API 变了？）。"
    )
    assert not problems, (
        "以下位置用了 Python 3.12 才允许的 f-string 写法。开发机是 3.12 看不出来，"
        "但 3.11（真机 Windows 客体实测就是它）会直接 SyntaxError、连模块都 import 不了：\n  "
        + "\n  ".join(problems)
    )


# ==================== ④ 反证：这个识别器真的抓得到 ====================

def test_the_pep701_checker_catches_a_planted_multiline_fstring(tmp_path):
    """
    ⚠️ 反证用例：种一段**真出过事的原文**（`server.py` 那处的形状），必须报出来。

    同前两条的理由：本条的判据也是「没有问题」，而"识别器坏了"与"代码干净"
    在结果上完全一样。这里种的是实测报错的那一行，不是构造的畸形样本。
    """
    fake = tmp_path / "planted.py"
    fake.write_text(
        "import json\n"
        "\n"
        "def selftest(caps):\n"
        "    print(f\"[selftest] capabilities = {json.dumps({'target': caps.target,\n"
        "          'notes': list(caps.notes)}, ensure_ascii=False)}\")\n",
        encoding="utf-8",
    )
    got = pep701_violations(fake.read_text(encoding="utf-8"))
    assert any("跨行" in why for _line, why in got), (
        f"识别器没抓到跨行 f-string —— 它坏了，而它坏掉的样子与「代码干净」一样。"
        f"实际报出：{got}"
    )


def test_the_pep701_checker_does_not_flag_legal_fstrings(tmp_path):
    """
    反向：**合法的** f-string 写法一个都不许报 —— 误报会让这条守卫被无视。

    逐个列出"看着像但其实合法"的四种，每一种都是本仓真实用到的形状：
      ① 普通单行 f-string；
      ② **三引号**跨行（历来合法，CLAUDE.md 风格的说明文本里会出现）；
      ③ **隐式拼接**跨行（`f"a"\n "b"`）—— 字面量各自单行，<3.12 完全合法；
      ④ 替换域里用**另一种**引号（`f"{d['k']}"`）。
    """
    fake = tmp_path / "legal.py"
    fake.write_text(
        "d = {'k': 1, 'w': 3}\n"
        "print(f\"单行 {d['k']}\")\n"
        "print(f\"\"\"三引号\n"
        "跨行 {d['w']}\"\"\")\n"
        "print(f\"隐式拼接\"\n"
        "      \"第二段\"\n"
        "      f\"{d['k']}\")\n",
        encoding="utf-8",
    )
    got = pep701_violations(fake.read_text(encoding="utf-8"))
    assert got == [], f"误报（这些写法在 3.11 下都合法）：{got}"