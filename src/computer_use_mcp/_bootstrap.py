"""
引导模块 —— 必须在任何 `import gi` 之前执行。

背景（环境实测）：
  - conda 环境的 pygobject 自带 GLib/GObject typelib，但没有 Atspi。
  - Atspi typelib 与 libatspi.so 来自系统（OS 级无障碍栈，无法打进单一二进制）。
  - 因此运行前需把系统的 girepository / lib 目录追加到 GI_TYPELIB_PATH / LD_LIBRARY_PATH，
    gi 才能 `require_version('Atspi','2.0')` 成功。

本模块对外暴露：
  - setup_gi_environment(): 幂等地配置环境变量
  - import_atspi(): 配置环境后导入并返回 Atspi 仓库模块
  - ATSPI_AVAILABLE / ATSPI_IMPORT_ERROR: 可用性标志（供 backend 优雅降级）

实现逻辑：
  1. 收集一组候选 typelib 目录（系统多架构路径 + conda 前缀 + 已存在的 GI_TYPELIB_PATH）。
  2. 只保留真实存在且含 Atspi-2.0.typelib 的目录，合并进 GI_TYPELIB_PATH（去重、保序）。
  3. 同理把含 libatspi.so* 的 lib 目录合并进 LD_LIBRARY_PATH（仅当进程未冻结或需要时）。
  4. import_atspi 调用 gi.require_version + from gi.repository import Atspi。
"""

from __future__ import annotations

import glob
import os
import sys

# 候选系统 typelib 目录（覆盖 Debian/Ubuntu 多架构与常见前缀）
_TYPELIB_CANDIDATES = [
    "/usr/lib/x86_64-linux-gnu/girepository-1.0",
    "/usr/lib64/girepository-1.0",
    "/usr/lib/girepository-1.0",
    "/usr/lib/aarch64-linux-gnu/girepository-1.0",
]

# 候选系统 lib 目录（libatspi.so 所在）
_LIB_CANDIDATES = [
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib64",
    "/usr/lib",
    "/usr/lib/aarch64-linux-gnu",
]

# 可用性标志（import_atspi 后更新）
ATSPI_AVAILABLE = False
ATSPI_IMPORT_ERROR: str | None = None


def _merge_env(var: str, new_paths: list[str]) -> None:
    """把 new_paths 中真实存在的目录合并进环境变量 var（已有值优先保留在前，去重保序）。"""
    existing = os.environ.get(var, "")
    parts = [p for p in existing.split(os.pathsep) if p]
    for p in new_paths:
        if p and p not in parts and os.path.isdir(p):
            parts.append(p)
    if parts:
        os.environ[var] = os.pathsep.join(parts)


def _conda_prefix() -> str | None:
    """返回当前解释器所属 conda/venv 前缀（用于追加其自带 typelib）。"""
    return getattr(sys, "prefix", None)


def setup_gi_environment() -> None:
    """
    幂等配置 GI_TYPELIB_PATH / LD_LIBRARY_PATH。

    实现逻辑：
      - typelib：候选系统目录中，凡包含 Atspi-2.0.typelib 的都加入；同时加入 conda 前缀下的
        lib/girepository-1.0（提供 GLib/GObject 等基础 typelib）。
      - lib：候选系统目录中，凡包含 libatspi.so* 的都加入 LD_LIBRARY_PATH。
    """
    # 1. typelib 路径
    typelib_dirs: list[str] = []
    # conda/venv 自带的 girepository（GLib/GObject 基础 typelib）
    prefix = _conda_prefix()
    if prefix:
        typelib_dirs.append(os.path.join(prefix, "lib", "girepository-1.0"))
    # 系统 Atspi typelib
    for cand in _TYPELIB_CANDIDATES:
        if glob.glob(os.path.join(cand, "Atspi-2.0.typelib")):
            typelib_dirs.append(cand)
        elif os.path.isdir(cand):
            # 即使没有 Atspi 也可能是其它 typelib 来源，保留
            typelib_dirs.append(cand)
    _merge_env("GI_TYPELIB_PATH", typelib_dirs)

    # 2. lib 路径（libatspi.so）
    lib_dirs: list[str] = []
    for cand in _LIB_CANDIDATES:
        if glob.glob(os.path.join(cand, "libatspi.so*")):
            lib_dirs.append(cand)
    if lib_dirs:
        _merge_env("LD_LIBRARY_PATH", lib_dirs)


def import_atspi():
    """
    配置环境后导入 Atspi 仓库模块。

    返回：gi.repository.Atspi 模块；失败时抛 ImportError，并设置全局 ATSPI_IMPORT_ERROR。
    成功/失败都会更新 ATSPI_AVAILABLE 标志。
    """
    global ATSPI_AVAILABLE, ATSPI_IMPORT_ERROR
    setup_gi_environment()
    try:
        import gi  # 延迟导入：必须先设好环境变量

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore

        ATSPI_AVAILABLE = True
        ATSPI_IMPORT_ERROR = None
        return Atspi
    except Exception as exc:  # noqa: BLE001 —— gi 缺失/typelib 找不到/dbus 不可用均可能
        ATSPI_AVAILABLE = False
        ATSPI_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        raise ImportError(ATSPI_IMPORT_ERROR) from exc


def import_gi():
    """仅导入 gi（不 require Atspi），供需要 GLib/GObject 的模块使用。"""
    setup_gi_environment()
    import gi  # noqa: F401

    return gi
