"""
窗口与进程（core.coordinator.windows）—— 列窗口、等窗口、启动应用。

`launch_app` 是**应用进沙箱的正路**：它走 `display.app_env()`（含私有 AT-SPI 总线
地址），而单纯 `subprocess.Popen` 起的进程既不在沙箱屏上、也连不上私有总线。
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time

from ...utils.errors import ComputerUseError
from .. import display
from .hooks import _exclusive_screen, _needs_display, log


class WindowMixin:
    """窗口清单 / 等待 / 启动应用。"""

    # ================= 窗口清单 / 等待 =================
    @_needs_display
    @_exclusive_screen
    def launch_app(self, command: str, settle: float = 2.0) -> dict:
        """
        在目标 display 启动应用（isolated 模式=放进 Xephyr 沙箱的正路）。

        实现逻辑：shlex 解析命令行 → Popen（env DISPLAY=目标屏，start_new_session
        脱离本进程进程组，server 退出不连带杀应用）→ settle 秒等窗口映射。
        返回 {pid, command, display}。
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            # 引号不闭合等语法错。若不拦，ValueError 不是 ComputerUseError，会穿透
            # tools/ 的 except 到 MCP 层，模型只看到 "Error executing tool launch_app"。
            raise ComputerUseError(f"launch_app: 命令行解析失败（{exc}）：{command!r}") from exc
        if not argv:
            raise ComputerUseError("launch_app: command 为空")
        # ⚠️ 用 app_env 而非 env_for：前者多一层「沙箱内应用 a11y 处理」
        # （isolated 模式下把 AT_SPI_BUS_ADDRESS 指向沙箱私有 AT-SPI 总线，
        #   见 core/display 的 at_spi_bus 子模块与 manager 的说明）
        env = display.app_env()
        try:
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            # 命令不存在 / 不可执行。沙箱里没装某应用是常态，必须把原因明确回给模型，
            # 否则它无法区分「命令名打错」「应用没装」「引号写坏」，只能盲试。
            raise ComputerUseError(
                f"launch_app: 启动失败（{exc.strerror or exc}）：{argv[0]!r}\n"
                f"   请确认该命令已安装且在 PATH 中"
            ) from exc
        # 后台 reap 子进程：server 是父进程，不 wait 的话退出后僵尸态滞留 /proc
        threading.Thread(target=proc.wait, daemon=True,
                         name=f"reap-{proc.pid}").start()
        if settle:
            time.sleep(settle)
        log.info("launch_app: pid=%s display=%s cmd=%s", proc.pid, env["DISPLAY"], command)
        return {"pid": proc.pid, "command": command, "display": env["DISPLAY"]}

    @_needs_display
    def list_windows(self, limit: int = 100) -> list[dict]:
        """列出可见窗口（按面积降序）。"""
        return self.backend.list_windows(limit=limit)

    @_needs_display
    def wait_window(
        self, title_contains: str | None = None, window_id: str | None = None,
        timeout: float = 10.0, poll: float = 0.25,
    ) -> dict | None:
        """等待窗口标题满足条件（server 内部轮询）。"""
        return self.backend.wait_window(title_contains=title_contains, window_id=window_id,
                                        timeout=timeout, poll=poll)