"""
语义编排层（core/coordinator）—— 三级降级。

核心主张（demo 已验证）：**元素级操作优先，坐标点击仅兜底**。
同一个「点击」意图，按以下顺序降级，每级失败才进下一级：

  ① element  ：backend.invoke(native) —— AT-SPI do_action，零坐标、不受焦点/DPI 影响（首选）。
  ② coord    ：geometry 校准出屏幕绝对坐标 → backend.click_at（先聚焦落点窗口，修复 demo 路径B）。
  ③ screenshot：连元素都找不到（灰区：自绘/游戏/远程桌面）→ 提示走截图兜底，由 LLM 决策。

本层与平台无关：只依赖 Backend 抽象 + RefTable + geometry。
所有对外方法返回结构化结果（ActionResult / Element / 文本），不抛裸异常给 MCP。

═══ 本包的结构（2026-09-18 由单文件 core/coordinator.py 拆分而来）═══
对外类名与调用面完全不变，内部按职责拆成 mixin：

    hooks.py       @_needs_display / @_exclusive_screen 两个装饰器与它们的顺序标记
    base.py        构造、_require_sandbox（沙箱闸门）、_sandbox_guard（注入礼让）
    landing.py     落点证据（省掉「再截一张图确认」的整个来回）
    awareness.py   读树 / 搜索 / 元素详情 / ref 解析（只读）
    actions.py     点击 / 输入 / 按键（三级降级的执行处）
    sequences.py   act_sequence 批量动作
    screens.py     截图 / OCR 文本层 / 屏幕布局
    windows.py     窗口清单 / 等待 / 启动应用

这些 mixin **共享同一份实例状态**（方法之间用 `self.` 互调）：它们描述的是同一件事
（同一块屏上的一次操作）的不同阶段。

⚠️⚠️ **两条结构性守卫的判据必须跟着改**（2026-09-18）：
`tests/test_optimizations.py` 里三条防漏测试原先用 `vars(Coordinator)` **反射枚举**对外
方法 —— 而 `vars()` 只看本类 `__dict__`，**看不到 mixin 继承来的方法**。拆包后若不改，
它们会**静默退化成空集合、全绿但零覆盖**（正是「判据松的测试等于没有测试」）。
现三条测试一律经 `_coordinator_methods()`（遍历 `__mro__`）枚举，改判据时别退回 `vars()`。

⚠️ 所有子模块共用 `hooks.log` 这**一个 logger 对象**（不是各自 `get_logger(__name__)`）
—— 日志名因此与拆分前一致为 `computer_use_mcp.core.coordinator`。
"""

from __future__ import annotations

from .. import display, screen_lock  # noqa: F401  （兼容既有 `coordinator.xxx` 用法）
from .actions import ActionMixin
from .awareness import AwarenessMixin
from .base import CoordinatorBase
from .hooks import _exclusive_screen, _mark, _needs_display, log  # noqa: F401
from .landing import LandingMixin
from .screens import ScreenMixin
from .sequences import SequenceMixin
from .windows import WindowMixin


class Coordinator(CoordinatorBase, LandingMixin, AwarenessMixin, ActionMixin,
                  SequenceMixin, ScreenMixin, WindowMixin):
    """把 backend 能力编排成「面向意图」的高层操作。"""