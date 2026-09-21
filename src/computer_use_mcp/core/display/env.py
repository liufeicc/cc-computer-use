"""
注入目标的解析与子进程 env 的构造（core.display.env）。

本模块回答两个问题：
  1. **注入/截图/几何查询该指向哪块屏**（`effective_display`，全项目唯一入口）；
  2. **起子进程时该给什么 env**（`env_for` / `app_env`）。

`env_for` 与 `app_env` 是**两层，不许合并**：前者是所有子进程的 env 出口（换 DISPLAY、
主动剥掉 `AT_SPI_BUS_ADDRESS`、剥掉冻结产物的 `LD_LIBRARY_PATH`）；后者专给「启动应用」
用，在 `env_for` 之上**只多一件事** —— isolated 模式下注入 a11y 总线地址。
"""

from __future__ import annotations

import os

from .constants import MODE_ISOLATED, _strip_frozen_lib_path, log


class EnvMixin:
    """解析实际注入目标、构造子进程 env。"""

    def effective_display(self) -> str:
        """
        注入/截图/几何查询实际使用的 display（全项目唯一入口）。

        isolated 且沙箱就绪 → 虚拟屏；否则回落宿主 DISPLAY（告警一次，避免日志刷屏）。
        """
        if self._mode == MODE_ISOLATED:
            if self.is_sandbox_up():
                return self._sandbox_display
            if not self._fallback_warned:
                self._fallback_warned = True
                log.warning("沙箱未就绪(display=%s)，注入回落宿主桌面 %s"
                            "（正常路径：首次工具调用经 coordinator._needs_display 触发 "
                            "ensure_started；若已触发仍见此警告，即沙箱启动失败）",
                            self._sandbox_display, self.host_display())
        return self.host_display()

    def env_for(self, display: str | None = None) -> dict:
        """
        子进程 env 副本：DISPLAY 替换为目标 display，其余继承。

        逻辑：拿 os.environ 的浅拷贝 → 换成目标屏 → **剥掉 AT_SPI_BUS_ADDRESS**
        → **剥掉冻结产物的 LD_LIBRARY_PATH**（见 _strip_frozen_lib_path）。

        为什么必须剥 AT_SPI_BUS_ADDRESS：本进程自己连私有 AT-SPI 总线时，只能把地址写进
        进程级 os.environ（libatspi 的 atspi_init 在初始化时读它，没有别的地方可传），
        于是它会**自动**被所有子进程继承。而走本通道的是 i3 / xdotool / xclip /
        wmctrl / xrandr —— 这些**一律不使用 a11y**，带上它既污染无关进程、
        也让「哪个进程连了哪条总线」变得说不清。

        为什么必须剥 LD_LIBRARY_PATH：本通道喂的全是**系统二进制**（同上那批，外加
        Xephyr / dbus-daemon / at-spi2-registryd / tesseract），它们该用系统库；让它们
        优先加载产物内的同名库，就是 `_strip_frozen_lib_path` docstring 里那类混装事故。
        **这条与 a11y 无关、对任何子进程都成立**，所以放这一层不违反下面那条边界约定——
        边界管的是 a11y 开关，不是库路径。原先的实现在 app_env 里剥、这里不剥，等于
        只有「应用」受保护，工具全体漏网（2026-09-16 实测取证后修正）。

        需要 a11y 的是**应用**，它们走 app_env()（先取本通道、再显式设定地址）。
        这条边界是刻意的，不许合并：合并过一次，表现为「谁先被 spawn 谁拿到总线」
        的偶然行为 —— 顺序一变，应用就静默落到宿主总线上去了。
        """
        env = dict(os.environ)
        env["DISPLAY"] = display or self.effective_display()
        env.pop("AT_SPI_BUS_ADDRESS", None)
        _strip_frozen_lib_path(env)
        return env

    def app_env(self, display: str | None = None) -> dict:
        """
        启动**应用**用的 env（launch_app 专用；env_for 是给 i3/xdotool/xclip 等工具用的）。

        为什么单独一层而不是往 env_for 里加：env_for 被注入/截图/宿主查询等一堆子进程
        共用，把 **a11y 开关**塞进去会污染无关进程、也说不清意图。

        本层只做**一件**事（其余都已在 env_for 完成，含冻结产物的库路径剥离）：
        isolated 模式下把 AT_SPI_BUS_ADDRESS 指向沙箱自己的 AT-SPI 总线（私有总线就绪→
        它的地址；否则→死地址）。**必须工具链无关** —— NO_AT_BRIDGE 那类按工具链的开关
        覆盖不到 SWT/GTK4（实测）。
        real 模式**不加** AT_SPI_BUS_ADDRESS：那时用户明确要在真实桌面上操作，行为应与
        手动启动一致。
        """
        env = self.env_for(display)
        if self._mode == MODE_ISOLATED:
            env["AT_SPI_BUS_ADDRESS"] = self.sandbox_at_spi_bus()
        return env