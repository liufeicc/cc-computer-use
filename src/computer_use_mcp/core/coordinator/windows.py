"""
窗口与进程（core.coordinator.windows）—— 列窗口、等窗口、启动应用。

`launch_app` 是**应用进沙箱的正路**：它走 `display.app_env()`（含私有 AT-SPI 总线
地址），而单纯 `subprocess.Popen` 起的进程既不在沙箱屏上、也连不上私有总线。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time

from ...utils.errors import ComputerUseError
from .. import display
from .hooks import _exclusive_screen, _needs_display, log

# 已知「会话总线单实例」应用的**去单实例参数**（2026-09-24 实测新增）。
#
# 问题：这类应用（GTK / GApplication 系）在**会话总线**上注册一个众所周知的名字，第二次
# 启动只是请求**已有实例**开个新窗口。而 `launch_app` 只改 DISPLAY、不改
# DBUS_SESSION_BUS_ADDRESS —— 于是已有实例在哪块屏，新窗口就开在哪块屏。宿主上已经
# 开着终端时，沙箱里的这次"启动"会把窗口送到**用户的真实桌面**上。实测：
# `launch_app("gnome-terminal")` 返回 display=":0"、`ok=True`，而窗口出现在宿主 :1
# （服务端 `gnome-terminal-server` 的 DISPLAY 恒为 :1，因为它是会话总线→systemd --user
# 激活的，systemd 的环境里就是 :1）—— 与 CLAUDE.md 里"虚拟屏隔离不了 AT-SPI"同源。
#
# 修法**不是**包一层 `dbus-run-session`：那条路要求外层进程一直活着替私有总线保活
# （gnome-terminal 的客户端进程发完请求就退出，dbus-run-session 随之退出、总线一死
# 窗口立刻消失，实测就是这样），等于每次启动都留一个守护进程，而且应用会丢掉 dconf /
# portal，行为与在真实桌面手动启动差很远。
#
# 正确做法是用应用**自带的**去单实例开关：它自己拉起一个私有实例，环境（含 DISPLAY）
# 全部由我们给的 `app_env()` 决定，窗口天然落在目标屏。实测 `gnome-terminal
# --disable-factory`：新 server 的 DISPLAY=:0，宿主上零窗口，应用还有正常的会话总线。
#
# 键取 argv[0] 的 basename；**未列出的应用行为完全不变**（不做任何猜测性包装）。
_DESINGLETON_ARGS: dict[str, tuple[str, ...]] = {
    # 实测验证：--disable-factory 让 wrapper 自己 spawn 一个私有 server（随机 app-id），
    # 不再去找已有实例。带 .real 后缀的是它内部真正执行的那个客户端，写法也一并支持。
    "gnome-terminal": ("--disable-factory",),
    "gnome-terminal.real": ("--disable-factory",),
    # gedit 同属 GApplication 单实例：沙箱内必须 --standalone（见 CLAUDE.md 的实测记录）。
    "gedit": ("--standalone",),
    "gnome-text-editor": ("--standalone",),
}


def _desingleton_argv(argv: list[str]) -> list[str]:
    """
    给已知的单实例应用补上去单实例参数；未列出、或调用方已经自己加了 → 原样返回。

    三条刻意的克制（改动 `_DESINGLETON_ARGS` 时请一并保持）：
      · 只按 **argv[0] 的 basename** 查表（`gnome-terminal`、`/usr/bin/gnome-terminal`
        都命中），绝不模糊匹配——把参数塞给一个不需要它的应用比不塞危险得多。
      · 参数补在**选项区最前面**（紧跟 argv[0]）：`gnome-terminal --disable-factory -- bash`
        这种把 `--` 与位置参数放在后面的写法才不会被顶掉。
      · 已经出现过的参数不重复加（调用方自己写 `--disable-factory` 或 `--standalone`
        时保持原样），免得出现两份、被应用当成语法错。
    """
    extra = _DESINGLETON_ARGS.get(os.path.basename(argv[0]))
    if not extra:
        return argv
    if any(a in argv[1:] for a in extra):
        return argv
    log.info("launch_app: 为 %r 补上去单实例参数 %s —— 否则窗口会开到「已存在的那个实例」"
             "所在的那块屏上（通常是宿主桌面）", argv[0], " ".join(extra))
    return [argv[0], *extra, *argv[1:]]


class WindowMixin:
    """窗口清单 / 等待 / 启动应用。"""

    # ================= 窗口清单 / 等待 =================
    @_needs_display
    @_exclusive_screen
    def launch_app(self, command: str, settle: float = 2.0) -> dict:
        """
        在目标 display 启动应用（isolated 模式=放进 Xephyr 沙箱的正路）。

        实现逻辑：shlex 解析命令行 → **给单实例应用补去单实例参数**（_desingleton_argv）
        → Popen（env DISPLAY=目标屏，start_new_session 脱离本进程进程组，server 退出不
        连带杀应用）→ settle 秒等窗口映射。返回 {pid, command, display, argv}。

        ⚠️ **DISPLAY 不是唯一变量**：GTK/GApplication 系应用（gnome-terminal、gedit…）在
        会话总线上是单实例的，只改 DISPLAY 改不动它们——第二次启动只是请**已有实例**
        开个窗口，而那个实例住在宿主的 :1 上。实测 `launch_app("gnome-terminal")` 返回
        `display=":0"`、`ok=True`，窗口却出现在用户真实桌面上。这就是 _DESINGLETON_ARGS
        存在的原因；表里没有的应用无法这样通用地修（没有"去单实例"这种通用开关），
        遇到时请把该应用自己的开关补进表里。
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            # 引号不闭合等语法错。若不拦，ValueError 不是 ComputerUseError，会穿透
            # tools/ 的 except 到 MCP 层，模型只看到 "Error executing tool launch_app"。
            raise ComputerUseError(f"launch_app: 命令行解析失败（{exc}）：{command!r}") from exc
        if not argv:
            raise ComputerUseError("launch_app: command 为空")
        # 单实例应用要先补去单实例参数，否则窗口会开到"已有实例所在的那块屏"上
        # （宿主桌面），而本工具还报成功。理由见 _DESINGLETON_ARGS 的注释。
        argv = _desingleton_argv(argv)
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
        # 回报**实际执行**的 argv（可能被 _desingleton_argv 补过参数）：模型据此知道
        # 自己那条命令被改成了什么，不必去翻日志，也免得下次自己再加一遍。
        return {"pid": proc.pid, "command": command, "display": env["DISPLAY"], "argv": argv}

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