"""
显示目标管理（core.display）—— 隔离沙箱模式的唯一 DISPLAY 来源。

背景（DBeaver 真实会话的实测教训）：注入走 X11 全局通道（XTest），物理指针与焦点
是独享资源——Agent 操作期间用户无法同时使用电脑（互相抢焦点/指针）。本模块用
「隔离沙箱」从根上消除冲突：

  - 默认（CC_CU_DISPLAY_MODE=isolated）**首次真正用到本 MCP 时**自启一个 Xephyr 可视
    虚拟屏（屏号由 X server 自动分配，见 ensure_started），所有注入（xdotool）、截图
    （mss）、几何查询都指向虚拟屏；宿主桌面零干扰——windowactivate/mousemove 只作用于
    该虚拟屏。
  - **每个 Claude 会话一块私有屏**（默认）：MCP server 是 claude.exe 的子进程、一会话
    一个，故屏号不固定、各会话各得一块，天然互不干扰。
  - 用户可随时把键鼠伸进 Xephyr 窗口亲自操作（Xephyr 默认把宿主输入路由进嵌套屏）；
    此时注入侧经 wait_until_user_leaves 礼让暂停，用户离开后自动恢复。
  - CC_CU_DISPLAY_MODE=real 可整体关闭沙箱，回到直接操作真实桌面（旧行为）。

环境变量：
  CC_CU_DISPLAY_MODE      isolated（默认）| real
  CC_CU_SANDBOX_DISPLAY   虚拟屏号。**默认不设**=每次自动分配空闲号（每会话一屏）；
                          显式设成 :99 之类 = 用固定屏号，且同屏号已存在时 attach
                          外部沙箱（退出不回收）——这是"多会话共用一块屏"的唯一入口。
  CC_CU_SANDBOX_SCREEN    虚拟屏分辨率，默认 1600x1000
  CC_CU_SANDBOX_WAIT_USER 用户在沙箱内时注入礼让的最长等待秒数，默认 30

关键语义：
  - ensure_started()：**惰性启动**，唯一被 server 正常路径使用的入口（Coordinator 的
    对外方法经 _needs_display 装饰器调用它）。进程启动时**不**拉沙箱——MCP server 是
    Claude Code 会话启动即常驻拉起的 stdio 进程，若那时启动则每次开 Claude 都弹窗，
    哪怕整场会话一次都没用过本工具。详见其 docstring。
  - effective_display()：注入实际指向的 display。isolated 且沙箱就绪 → 虚拟屏；
    否则**回落宿主 DISPLAY 并告警一次**（保证未触发 ensure_started 的场景不会指向空
    display）。⚠️ 这条回落**不能作为注入路径的依赖**：它会让注入静默打到用户真实桌面，
    而 LLM 只看得到"操作成功"。注入类操作因此额外过 coordinator._require_sandbox ——
    沙箱不可用时**直接报错拒绝**，见 utils/errors.SandboxUnavailableError。
  - start()/ensure_started() 幂等且共用同一把 _lock（并发首个调用在此串行等待，
    避免后者读到「未就绪」而回落宿主）。尝试次数有上限（_start_attempts）：
    冷启动失败只试一次，曾成功过则允许有限次重建（屏被回收/崩了能自愈）。

═══ 本包的结构（2026-09-18 由单文件 core/display.py 拆分而来）═══
原先的 1297 行单文件按职责拆成六个子模块，**对外命名空间 `display.XXX` 完全不变**
（`display.MANAGER` / `display.env_for` / `display.MODE_ISOLATED` 等所有既有用法照旧）：

    constants.py    常量、共用 logger、_resolve_at_spi_deps / _strip_frozen_lib_path
    lifecycle.py    Xephyr 生命周期（启动/重建/回收）+ 基础信息
    at_spi_bus.py   私有 AT-SPI 总线（a11y 隔离）+ 残留清扫
    wm.py           沙箱内起一个最小 WM（i3）
    host_geom.py    宿主几何查询 + 注入前的用户礼让
    env.py          effective_display / env_for / app_env
    manager.py      DisplayManager 的组装、describe()、单例 MANAGER

`DisplayManager` 被拆成五个 mixin 组合而成 —— 它们**共享同一份实例状态**（方法之间用
`self.` 互调），因为「哪块屏、哪个进程、哪条总线」本来就是同一件事的不同侧面。

⚠️ 所有子模块共用 `constants.log` 这**一个 logger 对象**（不是各自 `get_logger(__name__)`）
—— 日志名因此与拆分前一致为 `computer_use_mcp.core.display`，且
`monkeypatch.setattr(display.log, "warning", ...)` 这类补丁对全部子模块生效。
"""

from __future__ import annotations

# ⚠️ 下面这些标准库名是**刻意**在本命名空间保留的：
#   ① 子模块一律用「属性访问」调用它们（`shutil.which(...)` 而非 `from shutil import which`），
#      因此 `monkeypatch.setattr(display.shutil, "which", ...)` 改的是**同一个模块对象**，
#      对全部子模块生效（模块对象是进程级单例）；
#   ② 既有测试正是通过 `display.os` / `display.tempfile` / `display.subprocess` 等入口
#      打补丁的，删掉这些名字会打断它们，而它们表达的语义（patch 标准库全局）并未改变。
import atexit  # noqa: F401
import glob  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import select  # noqa: F401
import shutil  # noqa: F401
import signal  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import tempfile  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401

from .constants import (  # noqa: F401
    DEFAULT_SANDBOX_SCREEN,
    DEFAULT_WAIT_USER,
    ENV_MODE,
    ENV_SANDBOX_AT_SPI_BUS,
    ENV_SANDBOX_DISPLAY,
    ENV_SANDBOX_SCREEN,
    ENV_SANDBOX_WAIT_USER,
    ENV_SANDBOX_WM,
    MODE_ISOLATED,
    MODE_REAL,
    _AT_SPI_CONF_CANDIDATES,
    _AT_SPI_DIR_PREFIX,
    _AT_SPI_REGISTRYD_CANDIDATES,
    _AT_SPI_TIMEOUT,
    _DEAD_AT_SPI_BUS,
    _DISPLAYFD_TIMEOUT,
    _FALLBACK_DISPLAY_FROM,
    _FALLBACK_DISPLAY_TRIES,
    _HOST_RECT_TTL,
    _SANDBOX_I3_CONFIG,
    _START_TIMEOUT,
    _USER_WAIT_POLL,
    _resolve_at_spi_deps,
    _strip_frozen_lib_path,
    log,
)
from .manager import (  # noqa: F401
    MANAGER,
    DisplayManager,
    app_env,
    effective_display,
    env_for,
    start,
)