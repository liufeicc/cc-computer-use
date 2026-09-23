# -*- coding: utf-8 -*-
"""
`core/display.py` 沙箱生命周期的回归集（REVIEW 第四节 M-1、M-3 ~ M-6、M-9、M-10）。

全部用例都是**纯内存**的：不起 Xephyr / i3 / dbus-daemon（需要拦住的 spawn 点在用例里
被替换成记录器）。

刻意**不含** M-2 与 M-7：经确认二者不改（都触及 `is_sandbox_up()` 的语义与沙箱生命周期，
改动面大于收益，且现有行为在实测中未出过问题），仅在 REVIEW 里记录。
"""

from __future__ import annotations

import os
import stat
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from computer_use_mcp.core import display as dm  # noqa: E402


def _mgr() -> dm.DisplayManager:
    """一个不碰真实桌面的 DisplayManager（屏号自动分配那条路）。"""
    mgr = dm.DisplayManager()
    mgr._auto_display = True
    mgr._proc = None
    mgr._wm_proc = None
    return mgr


# ==================== M-1：displayfd 管道的 fd 泄漏 ====================

def test_displayfd_pipe_does_not_leak_when_spawn_fails(monkeypatch):
    """
    M-1：`Popen` 抛 OSError 时，**两个** fd 都要关掉。

    历史实现把 `os.close(rfd)` 写在 `_read_displayfd` 之后，于是「spawn 失败直接 return」
    与「读取抛异常」两条路都会泄漏一个 fd（每次失败一个）。
    """
    mgr = _mgr()
    fds: list[int] = []
    real_pipe = os.pipe

    def spy_pipe():
        r, w = real_pipe()
        fds.extend([r, w])
        return r, w

    def boom(*a, **k):
        raise OSError("spawn 失败")

    monkeypatch.setattr(dm.os, "pipe", spy_pipe)
    monkeypatch.setattr(dm.subprocess, "Popen", boom)

    assert mgr._spawn_xephyr("/usr/bin/Xephyr") is False
    assert len(fds) == 2
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)          # 已关闭 → EBADF
        try:
            os.close(fd)          # 万一真没关，别把泄漏带进后续用例
        except OSError:
            pass


# ==================== M-3 / M-4：沙箱 WM 的配置目录与 spawn 保护 ====================

def _stub_wm_env(monkeypatch, calls: list):
    monkeypatch.setattr(dm.shutil, "which", lambda n: "/usr/bin/i3")
    monkeypatch.setattr(dm.subprocess, "Popen",
                        lambda argv, **kw: (calls.append((argv, kw)),
                                            types.SimpleNamespace(pid=4242))[1])


def test_sandbox_wm_config_lives_in_private_temp_dir(monkeypatch):
    """
    M-3：i3 配置必须写在 `mkdtemp()` 出来的**私有目录**里，不能用 `/tmp` 固定路径。

    /tmp 是 1777 共享目录，固定文件名 + `open(..., "w")` 允许同机其他用户预置符号链接，
    让这次写入截断受害者任意可写的文件（经典 /tmp 符号链接攻击）；那个文件也从没被清理过。
    """
    mgr = _mgr()
    calls: list = []
    _stub_wm_env(monkeypatch, calls)
    monkeypatch.setenv(dm.ENV_SANDBOX_WM, "auto")
    monkeypatch.setattr(mgr, "_sandbox_display", ":99")

    mgr._start_sandbox_wm()

    assert len(calls) == 1, calls
    argv = calls[0][0]
    cfg = argv[argv.index("-c") + 1]

    assert cfg != "/tmp/cc-cu-sandbox-i3.config", "不得再用 /tmp 固定路径"
    assert os.path.dirname(cfg) == mgr._wm_dir, "配置应落在实例私有的临时目录里"
    assert os.path.isfile(cfg)
    mode = stat.S_IMODE(os.stat(mgr._wm_dir).st_mode)
    assert mode == 0o700, f"私有目录必须是 0700（mkdtemp 的默认），实际 {oct(mode)}"

    # 回收：目录必须真的被删掉（M-3 的另一半——旧实现从不清理）
    wm_dir = mgr._wm_dir
    mgr._cleanup_wm_dir()
    assert mgr._wm_dir is None and not os.path.exists(wm_dir)


def test_sandbox_wm_spawn_failure_does_not_propagate(monkeypatch, caplog):
    """
    M-4：i3 exec 失败（OSError）**不得**向上穿透。

    其余 spawn 点都有 OSError 保护，这里原先没有 —— 而 `_needs_display` 的契约是
    「ensure_started 自身只告警不抛」，穿透会让每一次工具调用都崩在启动环节。
    """
    mgr = _mgr()
    monkeypatch.setattr(dm.shutil, "which", lambda n: "/usr/bin/i3")
    monkeypatch.setenv(dm.ENV_SANDBOX_WM, "auto")

    def boom(*a, **k):
        raise OSError("exec i3 失败")

    monkeypatch.setattr(dm.subprocess, "Popen", boom)

    mgr._start_sandbox_wm()          # 不抛即通过
    assert mgr._wm_proc is None
    assert not os.path.exists(mgr._wm_dir or "/nonexistent")


# ==================== M-5：模块级包装已删除 ====================

def test_dead_module_level_wrappers_are_gone():
    """
    M-5：四个无调用方的模块级包装已删除。

    其中 `sandbox_at_spi_bus`（模块级）返回 `str | None`，而同名**方法**返回 `str`、
    未就绪时给**死地址**、**永不返回 None** —— 同名不同义，谁顺手 import 了模块级那个
    就会拿到 None 并以为拿到了地址。

    ⚠️ `effective_display` / `env_for` / `app_env` / `start` 是**有真实调用方**的
    （grab、inject 与手动 story 都在用），刻意保留。
    """
    for name in ("_sandbox_at_spi_bus", "sandbox_at_spi_bus",
                 "ensure_started", "wait_until_user_leaves"):
        assert not hasattr(dm, name), f"模块级 {name} 应已删除（M-5）"
    for name in ("effective_display", "env_for", "app_env", "start"):
        assert callable(getattr(dm, name, None)), f"{name} 有调用方，不该被误删"


# ==================== M-6：kill 之后必须 wait ====================

def test_kill_is_followed_by_wait(monkeypatch):
    """
    M-6：`terminate` 超时走 `kill` 后必须再 `wait()`，否则子进程留成僵尸，
    直到下次创建 Popen 时被 `subprocess._cleanup()` 顺带回收 —— 与本模块专门治僵尸的
    `_reap_children` 正好相悖。
    """
    events: list[str] = []

    class _FakeProc:
        def terminate(self):
            events.append("terminate")
            raise RuntimeError("terminate 超时")     # 逼出 kill 分支

        def kill(self):
            events.append("kill")

        def wait(self, timeout=None):
            events.append("wait")
            return 0

    dm.DisplayManager._kill_at_spi_procs(_FakeProc(), _FakeProc(), None)

    assert events.count("kill") == 2, events
    assert events.count("wait") == 2, f"每次 kill 后都要 wait：{events}"


# ==================== M-9：礼让轮询不再反复起子进程 ====================

def test_sandbox_window_rect_is_cached(monkeypatch):
    """M-9：`sandbox_window_rect()` 结果要带短 TTL 缓存（礼让轮询每轮都会调它）。"""
    mgr = _mgr()
    mgr._proc = types.SimpleNamespace(pid=1234, poll=lambda: None)
    calls: list = []
    monkeypatch.setattr(mgr, "_query_host_window_rect",
                        lambda: (calls.append(1), (10, 20, 300, 200))[1])

    assert mgr.sandbox_window_rect() == (10, 20, 300, 200)
    assert mgr.sandbox_window_rect() == (10, 20, 300, 200)
    assert len(calls) == 1, f"TTL 内不该重复查宿主窗口，实际查了 {len(calls)} 次"


def test_sandbox_window_rect_failure_is_not_cached(monkeypatch):
    """
    M-9 反向：**失败**不进缓存。

    否则一次偶然的查询失败会在整个 TTL 内一直返回 None，把礼让判成「用户不在沙箱里」
    而直接放行注入 —— 那正是礼让要防的事。
    """
    mgr = _mgr()
    mgr._proc = types.SimpleNamespace(pid=1234, poll=lambda: None)
    seq = [(1, None), (2, (0, 0, 100, 100))]
    monkeypatch.setattr(mgr, "_query_host_window_rect", lambda: seq.pop(0)[1])

    assert mgr.sandbox_window_rect() is None
    assert mgr.sandbox_window_rect() == (0, 0, 100, 100), "失败后下一轮必须重试"


def test_user_wait_poll_is_relaxed():
    """M-9：轮询间隔由 0.5s 放宽到 1s（30s 上限下轮数减半，代价是多等 ≤1 秒）。"""
    import inspect

    assert dm._USER_WAIT_POLL == 1.0
    sig = inspect.signature(dm.DisplayManager.wait_until_user_leaves)
    assert sig.parameters["poll"].default == dm._USER_WAIT_POLL


# ==================== M-10①：依赖路径的 which 兜底 ====================

def test_at_spi_deps_have_multi_path_candidates():
    """M-10①：两个 Debian/Ubuntu 硬编码路径之外必须有候选与 PATH 兜底。"""
    assert len(dm._AT_SPI_CONF_CANDIDATES) >= 2
    assert len(dm._AT_SPI_REGISTRYD_CANDIDATES) >= 2


def test_at_spi_deps_fall_back_to_path(monkeypatch):
    """硬编码路径都不存在时，registryd 要能从 PATH 里找到（Fedora/SUSE 的情形）。"""
    monkeypatch.delenv(dm.ENV_AT_SPI_CONF, raising=False)
    monkeypatch.setattr(dm.os.path, "exists", lambda p: False)
    monkeypatch.setattr(dm.shutil, "which", lambda n: "/opt/bin/at-spi2-registryd"
                        if n == "at-spi2-registryd" else None)

    conf, reg = dm._resolve_at_spi_deps()

    assert conf is None, "config 确实找不到 → 让调用方照实告警"
    assert reg == "/opt/bin/at-spi2-registryd"


def test_at_spi_deps_prefers_existing_candidate(monkeypatch):
    monkeypatch.delenv(dm.ENV_AT_SPI_CONF, raising=False)

    def fake_exists(p):
        return p == "/etc/xdg/at-spi2/accessibility.conf"

    monkeypatch.setattr(dm.os.path, "exists", fake_exists)
    monkeypatch.setattr(dm.shutil, "which", lambda n: "/should/not/be/used")

    conf, _reg = dm._resolve_at_spi_deps()
    assert conf == "/etc/xdg/at-spi2/accessibility.conf"


# ============ 随包自带的 accessibility.conf（.deb / .mcpb）============

def test_at_spi_conf_env_var_wins_over_system_candidates(monkeypatch):
    """
    `CC_CU_AT_SPI_CONF` 必须**压过**系统候选表。

    判据不能只看「有 conf 返回」——系统候选在开发机上本来就存在，那样写这个用例
    在「环境变量被忽略」时照样全绿。必须让系统候选**也可用**，再看返回的是哪一个。
    """
    bundled = "/opt/cc-computer-use/vendor/at-spi2/accessibility.conf"
    system = "/usr/share/defaults/at-spi2/accessibility.conf"
    monkeypatch.setenv(dm.ENV_AT_SPI_CONF, bundled)
    monkeypatch.setattr(dm.os.path, "exists", lambda p: p in (bundled, system))

    conf, _reg = dm._resolve_at_spi_deps()

    assert conf == bundled, f"自带的那份应优先（而不是系统候选 {system}）"


def test_at_spi_conf_env_var_missing_falls_back_with_warning(monkeypatch):
    """
    变量指向不存在的路径时**告警并回退**，不静默降级也不直接报错。

    为什么这条重要：.deb 装上后若 vendor/ 被误删，这条回退是「沙箱内还有没有 a11y」
    的分水岭——静默 None 会表现成「读不到元素树」，排查时完全看不出是 conf 丢了。
    """
    system = "/usr/share/defaults/at-spi2/accessibility.conf"
    monkeypatch.setenv(dm.ENV_AT_SPI_CONF, "/nonexistent/accessibility.conf")
    monkeypatch.setattr(dm.os.path, "exists", lambda p: p == system)
    warned: list[str] = []
    monkeypatch.setattr(dm.log, "warning", lambda *a, **k: warned.append(str(a)))

    conf, _reg = dm._resolve_at_spi_deps()

    assert conf == system
    assert warned, "回退必须留下告警（否则这就是一次静默降级）"
    assert dm.ENV_AT_SPI_CONF in warned[0]


# ==================== M-10②：拒绝伪造的总线目录 ====================

def test_discover_at_spi_bus_rejects_world_readable_dir(monkeypatch, tmp_path):
    """
    M-10②：group/other 可读的候选目录要判为**可疑的伪造总线**并跳过。

    /tmp 是 1777，目录名可预测（前缀带屏号）—— 同机其他用户可以预置一个伪造的总线目录，
    让本进程把沙箱应用的 a11y 注册信息接到他控制的 D-Bus 上。
    我们自己建的目录是 mkdtemp 出来的（0700、属主本人），据此校验即可。
    """
    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    mgr = _mgr()
    monkeypatch.setattr(mgr, "_sandbox_display", ":0")

    fake = tmp_path / "cc-cu-at-spi-d0-fake"
    fake.mkdir(mode=0o777 if os.umask(0) == 0 else 0o700)   # 先保证能建出来
    os.chmod(fake, 0o777)
    (fake / "bus").write_text("")

    assert mgr._discover_at_spi_bus() is None, "0777 的目录不得被采纳"
    os.chmod(fake, 0o700)
    assert mgr._discover_at_spi_bus() == f"unix:path={fake}/bus", \
        "权限收回到 0700 后就该正常采纳（判据是权限，不是目录名）"