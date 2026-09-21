"""
ref ↔ 元素 映射表（会话内）。

为什么需要：
  - MCP 工具返回给 LLM 的元素必须用紧凑的整数 ref 标识，而不是把无法序列化、
    且冗长的 AT-SPI Accessible 对象暴露出去。LLM 拿到 ref 后，click/type 用 ref 定位。

设计要点：
  - MCP server 是单个长驻进程，因此可直接在内存缓存「活的」payload（AT-SPI Accessible），
    ref→payload 一一对应，无需跨进程序列化。
  - payload 去重：同一对象重复 register 返回同一 ref（保证 get_ui_tree → click 间 ref 稳定）。
    去重以 id(payload) 为键，并对 payload 持强引用，避免 GC 后 id 复用导致串号。
  - 容量上限 + **LRU 淘汰**（最近用过的不淘汰），防止长会话内存膨胀。判据必须是
    「使用」而非「插入」：模型每一轮都会重新读树、重新注册屏幕上稳定显示的元素，
    按插入顺序淘汰的话，**恰恰是这些每轮都在用的元素先被淘汰**，模型上一轮的 ref
    明明还在屏上却报失效，白多一个来回（实测见 M-14）。
  - **线程安全**：同一 Claude 会话的并发子 agent 会同时调 register/remove（它们共用同一个
    MCP server 进程）。没有锁时 `ref = self._next; self._next += 1` 不是原子操作，两个线程
    可能拿到同一个 ref 号，后写者覆盖前者，表现为「按 ref 点击却点到了别的元素」。
  - 本模块不 import gi，保持纯 Python，便于单元测试；payload 的有效性校验由 backend 负责
    （resolve 只负责存取，调用方拿到 payload 后自行 try 调用以检测是否失效）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RefEntry:
    """一条 ref 记录：活对象 payload + 重定位/展示用元数据。"""

    ref: int
    payload: Any
    meta: dict[str, Any] = field(default_factory=dict)


class RefTable:
    """整数 ref 与元素 payload 的双向映射表。"""

    def __init__(self, max_entries: int = 5000) -> None:
        self._max = max_entries
        self._next = 1
        # 并发子 agent 会同时读写本表（见模块 docstring「线程安全」）。
        # 用 RLock 而非 Lock：register 的淘汰分支会调 _evict_lru，避免自锁死。
        self._lock = threading.RLock()
        self._by_ref: "OrderedDict[int, RefEntry]" = OrderedDict()
        # id(payload) -> ref，用于去重（持强引用见 _by_ref）
        self._by_payload_id: dict[int, int] = {}

    def register(self, payload: Any, **meta: Any) -> int:
        """
        注册一个 payload，返回其 ref。

        实现逻辑：
          1. 若该 payload（按 id）已注册且仍在表中，直接返回旧 ref（稳定性保证），
             并把它**移到 LRU 队尾**（M-14：命中即算「用过」，否则淘汰顺序会退化成
             「ref 号最小者」，把每轮都在用的稳定元素先淘汰掉）。
          2. 否则分配新 ref，存入 _by_ref 与 _by_payload_id（新记录天然在队尾）。
          3. 超过容量上限时，从**队首（最久未用）**开始淘汰，同步清理两个索引。

        全程持锁：ref 的读取-自增-写回必须是临界区，否则并发子 agent 会拿到同一号。
        """
        with self._lock:
            pid = id(payload)
            existing = self._by_payload_id.get(pid)
            if existing is not None and existing in self._by_ref:
                # 命中已有记录：更新 meta（树可能刷新了名字/矩形），返回同 ref
                self._by_ref[existing].meta.update(meta)
                self._by_ref.move_to_end(existing)  # 记一次「使用」，维持 LRU 序
                return existing

            ref = self._next
            self._next += 1
            self._by_ref[ref] = RefEntry(ref=ref, payload=payload, meta=dict(meta))
            self._by_payload_id[pid] = ref

            # 容量控制
            while len(self._by_ref) > self._max:
                self._evict_lru()
            return ref

    def _evict_lru(self) -> None:
        """淘汰最久未用的一条记录（队首），维护两个索引一致。"""
        with self._lock:
            if not self._by_ref:
                return
            old_ref, entry = self._by_ref.popitem(last=False)
            self._by_payload_id.pop(id(entry.payload), None)

    def get(self, ref: int) -> RefEntry | None:
        """按 ref 取记录（**算一次「使用」**，见模块 docstring 的 LRU 说明）；不存在返回 None。"""
        with self._lock:
            entry = self._by_ref.get(ref)
            if entry is not None:
                self._by_ref.move_to_end(ref)
            return entry

    def get_payload(self, ref: int) -> Any | None:
        """按 ref 取活对象 payload（同样算一次「使用」）；不存在返回 None。"""
        entry = self.get(ref)
        return entry.payload if entry else None

    def remove(self, ref: int) -> None:
        """删除一条 ref（如检测到 payload 失效）。"""
        with self._lock:
            entry = self._by_ref.pop(ref, None)
            if entry is not None:
                self._by_payload_id.pop(id(entry.payload), None)

    def clear(self) -> None:
        """
        清空整张表（如重新开始一轮感知）。

        ⚠️ **刻意不复位 `_next`（M-15）**——历史实现把它复位成 1，看似「从头开始」很干净，
        实则是最危险的那类隐患：模型上下文里还留着上一轮的 `ref=5`，清空后新注册的元素
        又会从头拿到 1、2、3…，于是旧 ref=5 **静默指向了另一个新元素**（而不是报失效）。
        模型据此点击，就会点到完全不相干的控件上——正是本项目最怕的「看着成功实则打偏」。
        `_next` 只增不减，旧 ref 只会「查不到」，不会「串到别人身上」。
        """
        with self._lock:
            self._by_ref.clear()
            self._by_payload_id.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_ref)

    def __contains__(self, ref: object) -> bool:
        with self._lock:
            return ref in self._by_ref
