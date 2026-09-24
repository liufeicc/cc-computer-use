"""
进程存活探针（utils.procfs）。

**为什么单独一个模块**：`/proc/<pid>` 是 Linux 专有。原先这段判定直接写在
`utils/temps.py::_pid_alive()` 里，并带着一句「本项目只有 X11/Linux backend，
不需要跨平台兜底」的注释 —— 那句话在加 Windows 支持时就不成立了。抽到本模块后，
**全仓只有这里认 `/proc`**，将来接 Windows 的判活（`OpenProcess` +
`GetExitCodeProcess`）也只有一个落点。

用途（唯一调用方 `utils/temps.py`）：判断「这个临时文件的主人进程还在不在」，
从而决定要不要回收它。判据方向必须**保守**——见下面 `pid_alive` 的说明。
"""

from __future__ import annotations

import os

_PROC = "/proc"


def pid_alive(pid: int) -> bool:
    """
    pid 对应的进程是否存在。

    - 有 `/proc`（Linux）：`/proc/<pid>` 存在即活（逐字搬自原 `temps._pid_alive`）。
    - 没有 `/proc`（Windows / macOS）：**返回 True（保守）**。

    为什么无 `/proc` 时不是 False 而是 True：调用方只有两个可选的错误方向，
    而它们的代价**不对称**——
      · 误判成「还活着」→ 多留一个几百 KB 的临时文件，无害；
      · 误判成「已死」→ 删掉**别的 Claude 会话**正在用的文件，有害。
    这与 `display._sweep_stale_buses()` 那条「只清本屏号，跨屏会误杀别的会话正在用的
    总线」是同一条理由：拿不准的时候，宁可少做。

    ⚠️ 保守是**过渡**，不是终态：Windows 上有一个可靠得多的探针
    （`OpenProcess` + `GetExitCodeProcess`，且能顺带靠进程创建时间防 pid 复用），
    接入点就在本模块，但**接入前不猜**——没在真机上量过的系统调用不写进判据。
    """
    if os.path.isdir(_PROC):
        return os.path.isdir(os.path.join(_PROC, str(pid)))
    return True
