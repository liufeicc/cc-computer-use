"""
屏操作独占锁（core/screen_lock）—— 同一 MCP server 内并发调用的互斥。

问题（由进程树实测确立）：MCP server 是 `claude.exe` 的**直接子进程，一个会话一个**。
Claude Code 的 Task 工具起的子 agent 并不会新起 `claude.exe`，因此也不会新起 MCP
server —— 主 agent 与所有子 agent 共用**同一个 server 进程**，于是共用：

  - 同一块虚拟屏、同一套 X11 指针/键盘焦点（"同时点两个窗口"物理上不存在）
  - 同一份剪贴板（中文输入走 xclip 的「备份→写入→粘贴→还原」四步，并发会互相踩）
  - 同一张 RefTable（并发 register 会让 `_next` 串号，ref 指向对方元素）

本模块只解决**同一进程内**的并发互斥；跨进程（两个 Claude 实例）靠各自独立的沙箱
隔离，见 core/display 的屏号自动分配。

设计：**非阻塞 + 快速失败**，绝不排队。理由见 utils/errors.ScreenBusyError 的注释——
排队会把「基于旧界面做的决策」延迟执行，产出「看起来成功实则打偏」的结果。

可重入：act_sequence 会持有锁跑完整串步骤，其中每一步又调 click/type_text —— 同线程
重入必须直接通过；不同线程（不同子 agent）仍互斥，这正是我们要的粒度。重入用
threading.local 记账（而不是 RLock），因为 RLock 无法在"最外层释放"时清理持有者信息，
而持有者信息正是要回给 LLM 的提示内容。
"""

from __future__ import annotations

import threading
import time

from ..utils.logging import get_logger

log = get_logger(__name__)

# 回给 LLM 的提示：既说明发生了什么，也说明**该怎么做**（等待 + 重新感知）。
# 强调「不要直接重放」是因为：它上一次的决策基于一个可能已被改动的界面。
_BUSY_TEMPLATE = (
    "⚠️ 屏幕正被另一个操作独占（{holder}），本次调用**已拒绝、未执行**。\n"
    "   同一会话内的所有 agent（含并发子 agent）共用一块虚拟屏、一个指针/焦点与一份"
    "剪贴板，无法真并行。\n"
    "   请**等待其完成后再操作**：建议先 sleep 1~2 秒，并**重新 get_ui_tree 确认界面"
    "状态**，不要基于旧认知直接重放刚才那一步。"
)


class ScreenLock:
    """
    屏操作独占锁：非阻塞获取，拿不到即由调用方转成友好提示。

    用法（见 coordinator._exclusive_screen 装饰器）：
        if not lock.acquire("click"):  -> 回提示，不执行
        try: ... 执行注入 ...
        finally: lock.release()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local = threading.local()   # 每线程的重入计数
        self._owner: str | None = None    # 当前持有者（操作名，仅用于提示）
        self._since: float = 0.0
        self._info_lock = threading.Lock()  # 只护 owner/since 两个展示字段

    def acquire(self, owner: str) -> bool:
        """
        尝试获取屏锁。成功 True；已被其它线程占用 False（**不等待，立即返回**）。

        实现逻辑：
          1. 本线程已持有（重入，如 act_sequence → click）→ 直接计数返回 True。
             少了这一步会在自己内部调用时把自己挡死。
          2. 否则非阻塞抢锁；抢不到立即返回 False，由调用方回提示。
        """
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            return True
        if not self._lock.acquire(blocking=False):
            return False
        self._local.depth = 1
        with self._info_lock:
            self._owner = owner
            self._since = time.monotonic()
        return True

    def release(self) -> None:
        """释放（只对真正持有者有效）。最外层释放时才清掉持有者信息。"""
        depth = getattr(self._local, "depth", 0)
        if depth <= 0:
            return  # 非持有者调用：忽略，不抛（装饰器 finally 的健壮性）
        depth -= 1
        self._local.depth = depth
        if depth > 0:
            return
        with self._info_lock:
            self._owner = None
            self._since = 0.0
        self._lock.release()

    def holder_info(self) -> str:
        """当前持有者的人类可读描述（如「click」已进行 1.2s），用于拼提示。"""
        with self._info_lock:
            owner, since = self._owner, self._since
        if owner is None:
            return "未知操作"
        return f"「{owner}」已进行 {time.monotonic() - since:.1f}s"

    def busy_message(self, op: str) -> str:
        """拼装回给 LLM 的占用提示。op=本次被拒的操作名。"""
        return f"[{op}] " + _BUSY_TEMPLATE.format(holder=self.holder_info())


# 模块级单例：MCP server 是单进程长驻，屏也只有一个，与 RefTable 相同的世界观。
SCREEN_LOCK = ScreenLock()