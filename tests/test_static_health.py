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
import pathlib
import symtable

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
