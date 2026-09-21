"""
日志工具。

关键约束：MCP 走 stdio（stdout 是 JSON-RPC 协议通道），
**所有日志必须写 stderr**，否则会污染协议、导致客户端解析失败。
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def get_logger(name: str = "computer_use_mcp") -> logging.Logger:
    """
    返回一个写 stderr 的 logger（幂等配置）。

    实现逻辑：
      - 首次调用时配置 root handler 指向 sys.stderr。
      - 级别由环境变量 COMPUTER_USE_LOG_LEVEL 控制（默认 INFO）。
      - 关闭向上传播，避免重复输出。
    """
    global _CONFIGURED
    logger = logging.getLogger(name)
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        level = os.environ.get("COMPUTER_USE_LOG_LEVEL", "INFO").upper()
        logger.setLevel(getattr(logging, level, logging.INFO))
        logger.propagate = False
        _CONFIGURED = True
    return logger
