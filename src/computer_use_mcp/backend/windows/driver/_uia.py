"""
uiautomation 的**唯一**入口（backend.windows.driver._uia）。

═══ 为什么单独一个模块 ═══

同 `_bootstrap.import_atspi` 的单点纪律：全仓**只有这里** import `uiautomation`。
好处有三：
  1. 换库（或将来自己写 ctypes 封装）只改一个文件；
  2. **顺序陷阱**只需在一个地方防住（见下）；
  3. 守卫可以断言「全仓 import uiautomation 只出现在本文件」——
     一条能被证伪的结构性判据，而不是靠纪律。

═══ ⚠️ 顺序陷阱：DPI 必须先设 ═══

`uiautomation` 在**模块导入时**执行 `SetProcessDpiAwareness(PerMonitorDpiAware)`
（旧 shcore API，值 2 —— **不是** PER_MONITOR_AWARE_V2）。而 Windows 的进程 DPI 感知度
**只能设一次**，后设的静默失败。

所以顺序必须是：

    1. `win32.set_dpi_awareness_v2()`     ← 我们先设，抢在它前面
    2. `import uiautomation`              ← 它那次设置会失败（无害）

反过来的后果是**静默的**：进程感知度停在 V2 之外的值上，UIA 矩形与注入坐标不在同一空间，
表现为"在某些显示器上偏几十像素"，排查起来极难。所以本模块不暴露裸 import，
只暴露 `import_uia()`，顺序由代码固化。
"""

from __future__ import annotations

from . import win32

# 记住"本次进程是不是已经成功设过 DPI 感知"，避免重复调用刷日志。
# 注意：这只是**我们自己的**判断，不代表系统真的接受了（失败原因要进报告）。
_dpi_state: dict | None = None


def ensure_dpi_awareness() -> dict:
    """
    幂等地把进程 DPI 感知设成 PER_MONITOR_AWARE_V2，并返回**结果报告**。

    返回 dict 而不是 bool：调用结果要进 JSON 报告。`ok=False` 时整份坐标数据都不可信，
    报告里必须能看出来 —— 这正是真机 DEMO 学到的教训（服务会话下 `PrintWindow`
    返回 `api_ok=True` 却是一张全黑图，「调用成功」与「拿到内容」是两回事）。
    """
    global _dpi_state
    if _dpi_state is None:
        _dpi_state = win32.set_dpi_awareness_v2()
        _dpi_state["after"] = win32.dpi_awareness_now()
    return _dpi_state


def import_uia():
    """
    返回 `uiautomation` 模块对象。**首次调用前会先设 DPI 感知**（顺序见模块 docstring）。

    失败时抛 `WindowsDriverUnavailable` —— 不静默降级成"没有这个能力"：
    缺库是**环境问题**，用户需要知道；而"树是空的"才是需要能力自检去分辨的那类问题。
    """
    ensure_dpi_awareness()
    try:
        import uiautomation  # noqa: PLC0415 —— 必须在设完 DPI 之后
    except Exception as exc:  # noqa: BLE001 —— 缺包/非 Windows/DLL 加载失败都在这
        raise WindowsDriverUnavailable(
            f"uiautomation 不可用（{type(exc).__name__}: {exc}）。"
            f"Windows 上需要 `pip install uiautomation`（会连带装 comtypes）。"
        ) from exc
    return uiautomation


class WindowsDriverUnavailable(RuntimeError):
    """Windows 驱动层不可用（缺库、非 Windows、或平台 API 调用失败）。"""


# ── PatternId / PropertyId 的单点解析 ────────────────────────────────────
#
# ⚠️ 这两个函数的存在理由是一条**真机上才暴露的**教训（2026-09-28）：
#
# `nodes.actions_of()` 原先写的是 `getattr(element, "IsInvokePatternAvailable", False)`。
# 真机实测它**恒为 False** —— `uiautomation` 的 `Control` 根本没有这个属性，而
# `getattr(x, name, 默认值)` 把「属性不存在」和「属性为 False」**混成了一件事**：
# 属性名写错不报错、只静默给默认值。于是所有元素的动作表都是空的，
# 元素级 do_action 那条路（本项目的第一原则）整个是死的，而单测全绿——
# 因为桩对象恰好长着那个我臆想出来的属性，**守卫在拿虚构验证虚构**。
#
# 第二层更隐蔽：`GetInvokePattern()` 这类 getter 定义在**控制类型子类**上而不在基类
# （`ButtonControl` 有 `GetInvokePattern` 却**没有** `GetSelectionItemPattern`），
# `getattr` 直接抛 AttributeError。所以唯一通用的入口是基类的 `GetPattern(patternId)`，
# 而 patternId 只能从 `PatternId` 常量表来。
#
# 结论：**Pattern 的判定只能走 `GetPattern(数值)`**，而"名字 → 数值"的翻译收在这里，
# 认不出就抛 —— 让写错名字变成一次响亮的失败，而不是一张空动作表。


def resolve_pattern(name: str) -> int:
    """
    `PatternId` 的成员名 → 数值 id（如 `"InvokePattern"` → 10000）。

    ⚠️ 认不出**抛异常**、不返回 0 或 None：静默返回会把"名字写错了"表现成
    "这个元素不支持该 Pattern"，正是上一条注释里那个坑的同一形状。
    """
    pid = getattr(import_uia().PatternId, name, None)
    if pid is None:
        raise WindowsDriverUnavailable(
            f"uiautomation 的 PatternId 里没有 {name!r} —— 该库版本可能变了，"
            f"请核对 PatternId 的成员名。"
        )
    return int(pid)


def pattern_id_cls():  # noqa: ANN202 —— 库对象，无法标注
    """`PatternId` 类本身（探针用来把常量表整体打出来核对）。"""
    return import_uia().PatternId


def property_id_cls():  # noqa: ANN202
    """`PropertyId` 类本身。"""
    return import_uia().PropertyId


# ── 能力自检 ─────────────────────────────────────────────────────────────

# 我们**依赖**的 uiautomation 对外 API。写死清单而不是 `hasattr` 探测：
# 缺一个就让整件事失败，而不是静默少读一个属性（那样表现成"树里没有这个字段"，
# 会被误判为"目标应用没暴露它"）。
_REQUIRED_API = (
    "GetRootControl",       # 根元素
    "ControlFromHandle",    # HWND → UIA 元素（按窗口直读，本项目的**主路径**）
    "ControlFromPoint",     # 坐标 → 元素（命中测试，不用挪指针）
    "WalkTree",             # 通用遍历
)


def capability_dump() -> dict:
    """
    报告 `uiautomation` 的实际能力面，供 JSON 记录。

    ⚠️ 为什么要有它：这个库的 API 名我们**没法离线核对**（构建/测试环境常常摸不到
    Windows）。与其靠记忆断言，不如让真机跑一遍把事实记下来——缺什么当场就能看见，
    而不是等集成到 server 之后才在某个分支上炸。
    """
    out: dict = {"required": {}, "optional": {}}
    try:
        uia = import_uia()
    except WindowsDriverUnavailable as exc:
        return {"available": False, "error": str(exc)}

    out["available"] = True
    out["version"] = getattr(uia, "__version__", None) or _version_from_metadata()
    for name in _REQUIRED_API:
        out["required"][name] = hasattr(uia, name)
    # 可选面：有则更好，没有也能干活
    for name in ("GetFocusedControl", "SetGlobalSearchTimeout", "InitializeUIAutomationInCurrentThread",
                 "UninitializeUIAutomationInCurrentThread", "Control", "PatternId", "ControlType"):
        out["optional"][name] = hasattr(uia, name)
    return out


def _version_from_metadata() -> str | None:
    try:
        import importlib.metadata as md

        return md.version("uiautomation")
    except Exception:  # noqa: BLE001
        return None
