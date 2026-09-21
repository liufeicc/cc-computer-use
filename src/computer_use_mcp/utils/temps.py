"""
自动路径产生的临时图像：命名约定 + 回收（utils.temps）。

**为什么需要这个模块**：有两条路径会往系统临时目录写图像文件，原先**都没有回收**：

  1. `screenshot(inline=false)` 不带 `save_path` → `coordinator.screenshot_to_file()`
     走 mkstemp 自动路径（模型只想存档 / 稍后自己读，不需要图进 context）；
  2. `act_sequence` 的截图步骤在**拿不到 MCP Image 类型**的退化路径下 → `_dump_temp()`
     落盘后只把路径写进文本（mcp SDK 版本不合时才会走到，见 CLAUDE.md「mcp SDK 版本兼容」）。

写一次留一个，一张 JPEG 约 300KB，长会话下来就是几百 MB 的无名垃圾（实测手工清掉过
118 个）。而模型侧根本不会去清理 —— 它只知道路径、不知道哪些已经作废。

**为什么不靠 atexit 一次性删干净**：那是把回收做成「进程退出时」的一次性动作，
① 会话被强杀（关终端 / kill）时 atexit 根本不跑，正是那 118 个文件的来源；
② 长会话期间无上限，回收来得太晚。改成「每次写入时裁剪」则是**有界**的，且与进程
怎么死无关。

**命名里为什么必须带 pid**：`/tmp` 是全局的，而本项目明确支持多个 Claude 会话并存
（每会话一块私有沙箱屏 + 一个常驻 MCP server 进程）。若按 family 前缀无差别删除，
就等于**误杀别的会话正在用的文件** —— 与 `display._sweep_stale_buses()` 那条
「只清本屏号，跨屏会误杀别的会话正在用的总线」同一条理由。带上 pid 之后：
  - 自己的文件：裁剪到最近 `KEEP` 个；
  - 别人的文件：**只在主人进程已死时**删（`/proc/<pid>` 不存在）。pid 复用只会让
    某个残留多留一会儿，方向保守，可接受。

**回收失败绝不能影响主操作**：所有异常吞掉只记日志，与「落点证据不得让主操作失败」
是同一条纪律（见 CLAUDE.md）。
"""

from __future__ import annotations

import glob
import os
import tempfile

from .logging import get_logger

# 与拆分后的 display/inject 包同理：共用同一个 logger 对象（固定名），
# 测试里 `monkeypatch.setattr(temps.log, ...)` 才打得中。
log = get_logger("computer_use_mcp.utils.temps")

# 每个 family 在**本进程内**最多保留几张。取 5 的理由：模型读图总是紧接在
# 「刚截图」之后，第 2 张之后的旧图基本不会再被引用；而它又不至于小到「一次
# 连续截图就把上一张删掉」（屏锁串行化下不会发生，但 act_sequence 里
# sleep+screenshot 混排时会）。
DEFAULT_KEEP = 5

# pid 归属标记的分隔符：文件名形如 `cc-cu-shot-12345-ab12cd34.jpg`
_SEP = "-"


def new_temp_image(family: str, suffix: str = ".jpg") -> str:
    """
    在系统临时目录创建一个**归属本进程**的空图像文件，返回路径（fd 已关闭）。

    只负责「起个不会被撞上的名字并把文件建出来」，写内容由调用方做 ——
    因为怎么写（截图字节 / 编码格式）是上层的事。
    """
    prefix = f"{family}{_SEP}{os.getpid()}{_SEP}"
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return path


def owner_pid(path: str, family: str) -> int | None:
    """
    从文件名解析归属 pid；解析不出（不是本家族的、或名字不合约定）返回 None。

    约定见模块 docstring：`{family}-{pid}-{mkstemp 随机串}{后缀}`。
    mkstemp 的随机串只含 `[a-z0-9_]`，不含 `-`，故第 3 段一定是 pid。
    """
    name = os.path.basename(path)
    if not name.startswith(f"{family}{_SEP}"):
        return None
    rest = name[len(family) + 1:]
    head = rest.split(_SEP, 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """进程是否存在（Linux 专用：本项目只有 X11/Linux backend，不需要跨平台兜底）。"""
    return os.path.isdir(f"/proc/{pid}")


def prune_temp_images(family: str, keep: int = DEFAULT_KEEP, protect: str | None = None) -> int:
    """
    回收临时图像，返回删除的个数。**任何异常都不外抛**。

    删除判据（两条并集）：
      1. 本进程创建的文件，超出最近 `keep` 个的部分；
      2. 其它进程创建的文件，且**主人进程已死**。

    `protect` 是「无论如何不许删」的路径 —— 通常是调用方刚写好、正准备回报给模型的那张。
    刻意**不**依赖「它按 mtime 一定排最新」：文件系统时间戳分辨率有限，同一次调用里
    刚写的文件与上一次的理论上可能同 tick，一旦排序打平就可能把自己刚给出去的路径删掉，
    而模型那边已经拿到路径了。显式保护把这个隐患从根上取消。
    代价是它要占掉一个名额：`keep` 的语义是**含这张在内**一共留 keep 个，否则
    「留 5 个」实际会稳定涨到 6 个（名额被 protect 白占一格）。
    """
    deleted = 0
    try:
        mine: list[str] = []
        dups: list[str] = []          # 别的会话留下的、主人已死的
        for p in glob.glob(os.path.join(tempfile.gettempdir(), f"{family}{_SEP}*")):
            if p == protect:
                continue
            pid = owner_pid(p, family)
            if pid is None:
                continue              # 不合约定：不是我们起的，绝不乱删
            if pid == os.getpid():
                mine.append(p)
            elif not _pid_alive(pid):
                dups.append(p)

        # 自己的：按 mtime 从新到旧，留够 keep 个。protect 只有**确实是本家族、且归本进程**
        # 时才占一格（调用方传个别的目录里的路径进来，不该把我们的名额扣掉）。
        used = 1 if (protect and owner_pid(protect, family) == os.getpid()) else 0
        quota = max(keep - used, 0)
        mine.sort(key=lambda x: _mtime(x), reverse=True)
        for p in mine[quota:]:
            if _unlink(p):
                deleted += 1
        for p in dups:
            if _unlink(p):
                deleted += 1
    except Exception as exc:  # noqa: BLE001 —— 回收失败绝不能拖垮主操作
        log.debug("回收临时图像失败（忽略）: %s", exc)
    return deleted


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError as exc:
        log.debug("删除临时图像失败（忽略）: %s %s", path, exc)
        return False
