"""
结构性防漏：**所有**子进程 spawn 点都必须经 env_for / app_env 取环境。

为什么需要这条测试（2026-09-16 实测事故，I-6）：
    server 以 PyInstaller 产物运行时，LD_LIBRARY_PATH 指向产物内部的库目录（约 71 个
    打包库）。**任何**子进程原样继承，就会优先加载产物里的同名库而不是系统库，与系统
    其余部分混装 —— 这正是 CLAUDE.md 里那两个 DBeaver 事故（JVM 在 libc 内 abort、
    a11y 注册泄漏到宿主总线）的成因。
    实测取证（冻结产物实跑，读 /proc/<pid>/environ）：Xephyr / dbus-daemon /
    at-spi2-registryd **三者都**继承了产物库路径，而它们偏偏都是系统二进制。其中
    at-spi2-registryd 链接的 libatspi / libdbus / libgio / libglib / libgobject /
    libX11 **产物里全都有**，一旦混装失败，表现是「私有 a11y 总线静默失效 → 无障碍
    能力整体消失而界面看不出异常」，正是本项目最忌讳的那类隐蔽失效。

修法是把剥离下沉进唯一的 env 出口 `display.env_for()`（`app_env` 经它再叠加 a11y
开关）。但那只解决了「当时已存在的 15 个 spawn 点」，解决不了「下次新增一个 spawn 点
又忘了接」—— 本测试补的就是这一刀。

判据（刻意做成**能被真实回退打红**，不学 I-11/I-12 那种「看着对、实际拦不住」）：
    枚举 src/ 下每一处 `subprocess.Popen/run/call/check_call/check_output`，要求它的
    `env=` 实参**源自** env_for / app_env。允许一层局部变量中转（如
    `clip_env = display.env_for()` 后 `env=clip_env`），再深的数据流不追——追不动也
    没必要，下面三个真实回退都能被它抓住：
      ① 新增 spawn 点且不写 env=            → 该点未覆盖 → 红
      ② 把 `env=display.env_for()` 改回 `env=dict(os.environ)` → 未覆盖 → 红
      ③ 把 `clip_env = display.env_for()` 改成 `clip_env = dict(os.environ)` → 红
"""

from __future__ import annotations

import ast
import pathlib
import re

# src 根目录（本文件在 tests/ 下）
SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "computer_use_mcp"

# 合法的 env 出口：只有这两个通道会剥掉冻结产物的库路径（见 display._strip_frozen_lib_path）
_PIPELINE = re.compile(r"\b(env_for|app_env)\b")

# subprocess 上会真的起进程的属性
_SPAWN_ATTRS = frozenset({"Popen", "run", "call", "check_call", "check_output"})

# 允许「不走 env 出口」的例外，键为 "相对路径:行号"。
# **当前为空**：全部 15 个 spawn 点都已接入。新增条目必须写明理由——这条测试的价值
# 全在于例外足够少且每一条都被审视过，滥加等于把它废掉。
EXEMPT: dict[str, str] = {}


def _enclosing_functions(tree: ast.AST) -> dict[int, ast.AST]:
    """call 节点 id → 它所在的最内层函数节点（模块级调用无此键）。"""
    out: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    out[id(sub)] = node   # 后写覆盖先写 → 剩下的就是最内层
    return out


def _name_binds_pipeline(func: ast.AST | None, tree: ast.AST, name: str) -> bool:
    """`name` 在同函数内是否被赋成 env_for/app_env 的结果（一层数据流）。

    找不到所在函数时退化为全模块搜索——宁可放宽，也不要因 AST 结构变化漏判。
    """
    scope = func if func is not None else tree
    for stmt in ast.walk(scope):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        if value is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                if _PIPELINE.search(ast.unparse(value)):
                    return True
    return False


def _scan() -> tuple[list[str], list[str]]:
    """返回 (覆盖了的 spawn 点, 未覆盖的 spawn 点)，元素形如 "相对路径:行号 ..."。"""
    covered: list[str] = []
    uncovered: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosing = _enclosing_functions(tree)
        rel = str(path.relative_to(SRC))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "subprocess" and fn.attr in _SPAWN_ATTRS):
                continue
            envkw = next((k for k in node.keywords if k.arg == "env"), None)
            ok = False
            detail = "无 env= 实参（继承 os.environ）"
            if envkw is not None:
                expr = envkw.value
                shown = ast.unparse(expr)
                if _PIPELINE.search(shown):
                    ok = True
                elif isinstance(expr, ast.Name) and _name_binds_pipeline(
                        enclosing.get(id(node)), tree, expr.id):
                    ok = True
                else:
                    detail = f"env= 不源自 env_for/app_env：{shown}"
            (covered if ok else uncovered).append(f"{rel}:{node.lineno} {detail}")
    return covered, uncovered


def test_spawn_sites_are_found() -> None:
    """先确认扫得到东西 —— 否则下面那条断言会因「扫了个空」而假绿。

    这是本文件自己的防呆：别的项目里出现过「重构改了目录布局 → rglob 命中 0 个文件
    → 断言空集 ⊆ 空白名单 → 永远通过」的假绿测试。
    """
    covered, uncovered = _scan()
    assert covered, (
        f"在 {SRC} 下没扫到任何经 env_for/app_env 的 spawn 点 —— 多半是扫描路径失效，"
        "本文件的两条断言已失去意义，请先修扫描逻辑"
    )


def test_every_spawn_site_uses_env_pipeline() -> None:
    """每一个 spawn 点都必须经 env_for / app_env 取环境（详见模块 docstring）。"""
    _, uncovered = _scan()
    offenders = [s for s in uncovered
                 if s.split(" ")[0] not in EXEMPT]
    assert not offenders, (
        "以下子进程 spawn 点没有经 env_for/app_env 取环境，会继承冻结产物的 "
        "LD_LIBRARY_PATH（叠装产物内的同名库）：\n  "
        + "\n  ".join(offenders)
        + "\n修法：把 env= 换成 display.env_for(...)（需要 a11y 的应用用 app_env）；"
          "确属例外的，在 EXEMPT 里登记并写明理由。"
    )