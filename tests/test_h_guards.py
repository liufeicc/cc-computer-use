# -*- coding: utf-8 -*-
"""
批次 H 的回归集（REVIEW 第四节 M-48 ~ M-51、M-56②③）。

M-50 / M-51 的回归直接落在 `tests/test_optimizations.py`（改写原用例），本文件只覆盖
手动脚本与沙箱残留检测这几条 —— 它们原先**一条测试都没有**，正是「改坏了不会有人知道」
的地方。
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.core import display as dm  # noqa: E402

_TESTS = pathlib.Path(__file__).resolve().parent


def _load_manual(name: str):
    """按文件路径加载手动脚本（它们不在包内，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location(name[:-3], _TESTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ==================== M-48：reader.error 是属性 ====================

def test_atspi_reader_error_is_a_property():
    """前提：`AtspiReader.error` 是 `@property`（所以当方法调必然 TypeError）。"""
    from computer_use_mcp.backend.linux.atspi import AtspiReader

    assert isinstance(inspect.getattr_static(AtspiReader, "error"), property)


def test_dbeaver_probe_reads_error_as_attribute():
    """
    M-48：探针里那句「AT-SPI 不可用: <原因>」必须读**属性**。

    原先写的是 `reader.error()` —— `error` 是 `@property`，调用它会抛
    `TypeError: 'str' object is not callable`，于是本该说清「为什么不可用」的提示
    **永远打不出来**，用户只看到一个误导性的堆栈（看起来像探针自身有 bug）。
    """
    src = (_TESTS / "manual_dbeaver_a11y_probe.py").read_text(encoding="utf-8")

    assert "reader.error()" not in src, "不得把 @property 当方法调（M-48）"
    assert "reader.error}" in src, "应改为读属性"

    # 行为判据：真拿一个 reader 试一次，证明「调用它」确实会炸
    from computer_use_mcp.backend.linux.atspi import AtspiReader

    reader = AtspiReader.__new__(AtspiReader)
    reader._error = "哨兵原因"
    assert reader.error == "哨兵原因"
    with pytest.raises(TypeError):
        reader.error()          # 这正是原实现踩的坑


# ==================== M-49：_ok 必须真的会被置位 ====================

def test_dbeaver_probe_failure_flag_actually_flips():
    """
    M-49：`_fail()` 必须真的把 `_ok` 置 False。

    它此前只读不写（`global _ok` 声明了却没有任何赋值）→ 末行 banner 恒打印
    「✅ 全部通过」。当前所有失败路径都提前 `return 1`，走到末行时结论恰好是对的
    （**所以这不是活 bug**），但只要将来加一条非致命检查，banner 立刻开始说谎 ——
    而「假绿比红更危险」。
    """
    mod = _load_manual("manual_dbeaver_a11y_probe.py")

    assert mod._ok is True, "初值应为 True（还没失败过）"
    mod._fail()
    assert mod._ok is False, "_fail() 必须真的置位，否则 banner 与退出码都会说谎"


def test_dbeaver_probe_step_failure_flags_ok(monkeypatch):
    """而且那条置位要**接在真实的失败分支上**：进程死亡必须让 `_ok` 变 False。"""
    mod = _load_manual("manual_dbeaver_a11y_probe.py")

    monkeypatch.setattr(mod, "alive", lambda pid: False)      # 模拟 DBeaver 死了
    probe = mod.Probe(reader=None, pid=4242)

    cont, _ = probe.step("任意查询", lambda: 1)

    assert cont is False and mod._ok is False, \
        "步骤发现目标进程死亡时必须同时置 _ok=False，否则末行结论会与退出码打架"


# ==================== M-56②：旧格式总线残留 ====================

def test_legacy_bus_residue_is_warned_but_never_auto_deleted(monkeypatch, tmp_path):
    """
    M-56②：**旧格式**（目录名不含屏号）的残留必须被检测到并告警，但**不得自动删除**。

    它不匹配 `_sweep_stale_buses` 的新前缀，于是永远清不掉；而旧格式没有屏号可依据，
    无法判断属于哪块屏 —— 直接删可能误伤还跑着旧版 server 的别的会话。故只告警 +
    给出手动清理命令（`docs/安装说明.md` 排错表里也有一条同样的说明）。
    """
    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    warns: list[str] = []
    monkeypatch.setattr(dm.log, "warning",
                        lambda msg, *a, **k: warns.append(msg % a if a else msg))

    legacy = tmp_path / "cc-cu-at-spi-ab12cd34"      # 旧格式：无 -d<屏号>-
    legacy.mkdir(mode=0o700)
    fresh = tmp_path / "cc-cu-at-spi-d0-xyz987"      # 新格式：不该被当成残留
    fresh.mkdir(mode=0o700)

    mgr = dm.DisplayManager()
    mgr._warn_legacy_buses()

    assert warns and "旧格式" in warns[0], warns
    assert str(legacy) in warns[0] and str(fresh) not in warns[0], \
        f"只该点名旧格式目录，实际：{warns[0]}"
    assert legacy.exists(), "**不得**自动删除旧格式残留（无法判断属于哪块屏）"
    assert "rm -rf /tmp/cc-cu-at-spi-*" in warns[0], "告警里要带上手动清理命令"

    # 限流：每次重建沙箱都会扫一遍，只该提醒一次
    warns.clear()
    mgr._warn_legacy_buses()
    assert warns == [], f"同一进程内只告警一次，实际重复告警：{warns}"


def test_legacy_check_is_wired_into_the_sweep(monkeypatch, tmp_path):
    """
    M-56② 的**接线**判据：`_warn_legacy_buses` 必须真的挂在 `_sweep_stale_buses` 上。

    只测那个辅助函数是不够的 —— 函数写对了但没人调用，上面两条照样全绿
    （「判据松的测试等于没有测试」，本项目已在 M-39 踩过同一个坑）。
    """
    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_display_num", lambda: "0")     # 让 _sweep 能往下走
    called: list = []
    monkeypatch.setattr(mgr, "_warn_legacy_buses", lambda: called.append(1))

    mgr._sweep_stale_buses()

    assert called == [1], "旧格式残留的检测必须接在每次清扫上，否则它永远不会被执行"


def test_no_legacy_warning_when_tmp_is_clean(monkeypatch, tmp_path):
    """M-56② 反向：干净环境不得刷告警（否则这条提醒会被当噪音忽略）。"""
    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    warns: list[str] = []
    monkeypatch.setattr(dm.log, "warning",
                        lambda msg, *a, **k: warns.append(msg % a if a else msg))

    (tmp_path / "cc-cu-at-spi-d0-xyz987").mkdir(mode=0o700)   # 只有新格式

    dm.DisplayManager()._warn_legacy_buses()
    assert warns == [], warns


# ==================== M-56③：a11y story 的收尾判据 ====================

def test_a11y_story_reap_check_is_scoped_to_its_own_bus():
    """
    M-56③：收尾判据必须收窄到「**本次这条**总线目录是否消失」。

    原先用的是全局 `glob.glob("/tmp/cc-cu-at-spi-*")` —— 会被**别的会话**留下的残留
    带红（2026-09-16 实测：本次跑出的目录正常回收、目录总数没增长，却因另一组历史残留
    报 ❌）。判据随环境飘 = 没人会再信它。
    """
    src = (_TESTS / "manual_a11y_isolation.py").read_text(encoding="utf-8")
    # 只看**非注释行**：改动说明里会引用旧代码，直接全文搜会把「解释」当成「还在用」
    # （这正是 M-50 批评的那类「字符串存在」判据容易踩的坑，别自己也踩一次）
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))

    assert 'glob.glob("/tmp/cc-cu-at-spi-*")' not in code, \
        "收尾检查不得用全局面 glob（会被别的会话带红）"
    assert "glob.glob(" not in code, "连 import glob 都该一起去掉"
    assert "os.path.dirname(bus.split(" in code, "应改为从 bus 变量推出本次的目录"