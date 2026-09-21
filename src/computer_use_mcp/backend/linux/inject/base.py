"""
xdotool 通道的公共底座（backend.linux.inject.base）。

`XdotoolInjector` 由五个 mixin 组合而成（windows / pointer / keyboard / screens / apps），
它们共用同一份实例状态，本模块提供三样它们都依赖的东西：

  - 共用 logger 对象（见下方注释，测试会直接 patch 它）；
  - 二进制路径的定位（`__init__`）；
  - **唯一**的子进程出口 `_run` —— 所有 xdotool 调用都经它，DISPLAY 与错误契约
    （超时/失败一律转成 `InjectionError`）在这一点统一。

注入走 X11/XTest 通道（xdotool），MVP 锁定 X11。后续 Phase 2 可换 uinput。
"""

from __future__ import annotations

import shutil
import subprocess

from ....core import display
from ....utils.errors import InjectionError
from ....utils.logging import get_logger

# ⚠️ 用**固定名字**取 logger（而不是 `__name__`）：本包拆分自单文件
# `backend/linux/inject.py`，固定名字让所有子模块**共用同一个 logger 对象**
# （日志名与拆分前一致），也才能让 `monkeypatch.setattr(inject.log, "warning", ...)`
# 这类既有测试对全部子模块生效 —— 各自 `get_logger(__name__)` 会拿到不同对象，
# 补丁静默打空。
log = get_logger("computer_use_mcp.backend.linux.inject")


class InjectorBase:
    """xdotool 二进制定位与命令执行底座。"""

    def __init__(self) -> None:
        self._bin = shutil.which("xdotool")

    # ---------- 基础 ----------
    def is_available(self) -> bool:
        """xdotool 是否在 PATH 中。"""
        return self._bin is not None

    def _run(self, args: list[str], check: bool = False, timeout: float = 10.0) -> subprocess.CompletedProcess:
        """
        执行一条 xdotool 命令。

        args 为 xdotool 之后的参数列表。返回 CompletedProcess（capture stdout/stderr, text）。
        check=True 且失败时抛 InjectionError。
        """
        if not self._bin:
            raise InjectionError("xdotool 未安装或不在 PATH（请 sudo apt install xdotool）")
        cmd = [self._bin, *args]
        try:
            # DISPLAY 统一由 core.display 供给：isolated 模式指向 Xephyr 沙箱屏，
            # 注入（windowactivate/mousemove/key）只作用于沙箱，宿主桌面零干扰。
            # errors="replace"：X11 窗口标题是**任意字节串**（xdotool 原样输出），旧式应用
            # 可能给非 UTF-8 字节；默认的严格解码会抛 UnicodeDecodeError，而它**不是**
            # InjectionError，会穿透本模块的错误契约一路逃到 MCP 层——模型只拿到一句
            # 无信息量的 "Error executing tool X"，且标题含非法字节时该工具**持续**不可用。
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  errors="replace", env=display.env_for())
        except subprocess.TimeoutExpired as exc:
            raise InjectionError(f"xdotool 超时: {' '.join(args)}") from exc
        if check and proc.returncode != 0:
            raise InjectionError(
                f"xdotool {' '.join(args)} 失败(rc={proc.returncode}): {proc.stderr.strip()}"
            )
        return proc