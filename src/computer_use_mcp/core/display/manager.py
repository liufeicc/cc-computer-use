"""
`DisplayManager` 的组装与模块级单例（core.display.manager）。

类本身只是一层**组合**：所有方法都按职责分在五个 mixin 里（`lifecycle` / `at_spi_bus` /
`wm` / `host_geom` / `env`），这里只负责
  1. 声明全部实例字段的初始化顺序（`__init__` 依次调用各组的 `_init_*`）；
  2. 提供 `describe()`（跨组的整体状态摘要）；
  3. 创建进程级单例 `MANAGER` 与几个模块级便捷函数。

mixin 之间**共享同一份实例状态**（方法用 `self.` 互调），这是刻意的：拆成独立对象会把
状态搬来搬去，而"哪块屏、哪个进程、哪条总线"本来就是同一件事的不同侧面。
"""

from __future__ import annotations

from .at_spi_bus import AtSpibusMixin
from .env import EnvMixin
from .host_geom import HostGeomMixin
from .lifecycle import LifecycleMixin
from .wm import WmMixin


class DisplayManager(LifecycleMixin, AtSpibusMixin, WmMixin, HostGeomMixin, EnvMixin):
    """
    隔离沙箱模式的唯一 DISPLAY 来源。

    职责边界：
      - 解析模式（isolated/real）与各项环境变量；
      - 管理 Xephyr 沙箱的生命周期（惰性启动、重建、回收）；
      - 沙箱内起一个最小 WM（i3），让焦点类操作可用；
      - 起一条**私有 AT-SPI 总线**做 a11y 隔离（并清扫跨会话残留）；
      - 判定用户是否把键鼠伸进了沙箱，注入前礼让；
      - 给出「注入该指向哪块屏」与「子进程该拿什么 env」。

    ⚠️ 本模块**不得 import backend/***：inject.py 反向依赖本模块取 DISPLAY，反向导入会
    循环。`user_inside_sandbox` 所需的宿主侧 xdotool 只读查询在 `host_geom` 里独立实现。
    """

    def __init__(self) -> None:
        # 各组自己管自己的字段；顺序无关（组间无初始化依赖），但保持稳定便于排查
        self._init_lifecycle()   # 模式、屏号、Xephyr 进程、锁与启动计数
        self._init_at_spi()      # 私有 AT-SPI 总线的两个进程与地址
        self._init_wm()          # 沙箱 WM 进程与配置目录
        self._init_host_geom()   # 宿主窗口矩形缓存

    # ---------- 自检 ----------
    def describe(self) -> dict:
        """selftest/日志用的状态摘要。"""
        return {
            "mode": self._mode,
            "target": self.effective_display(),
            "host": self.host_display(),
            "sandbox_display": self._sandbox_display,
            "sandbox_up": self.is_sandbox_up(),
            "sandbox_pid": self._proc.pid if self._proc and self._proc.poll() is None else None,
            "wm_pid": self._wm_proc.pid if self._wm_proc and self._wm_proc.poll() is None else None,
            "attached": self._attached,
            "screen": self._screen,
        }


# 模块级单例：inject/backend/coordinator/server 共用同一状态（同进程长驻假设，
# 与 RefTable 相同的世界观）。单测可替换 MANAGER 或 monkeypatch 其方法。
MANAGER = DisplayManager()


def effective_display() -> str:
    return MANAGER.effective_display()


def env_for(display: str | None = None) -> dict:
    return MANAGER.env_for(display)


def app_env(display: str | None = None) -> dict:
    """启动应用用的 env（isolated 模式下会设定沙箱的 AT-SPI 总线地址）。见 DisplayManager.app_env。"""
    return MANAGER.app_env(display)


def start() -> dict:
    return MANAGER.start()


# 注（M-5）：这里原有两个模块级包装 `ensure_started()` / `wait_until_user_leaves()`，
# 全项目无任何调用方（生产代码一律走 `display.MANAGER.<同名方法>`）。留着它们只会让人
# 以为存在两条等价入口 —— 而它们与方法的语义并不总是重合（如 `sandbox_at_spi_bus`
# 那一对：模块级返回 `str | None`，方法返回 `str` 且未就绪时给死地址）。已删除。
# 拆分成本包后同理：不再新增与 MANAGER 方法同名的模块级包装函数。