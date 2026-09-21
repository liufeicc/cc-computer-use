"""
display 子系统的生命周期、env 构造与私有 AT-SPI 总线（拆分自 test_optimizations.py）。

本文件覆盖的是沙箱那套最复杂的资源管理：启动计数与重建配额、孤儿回收、
总线按屏号反查与残留清扫。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from computer_use_mcp.backend.base import Backend, QueryResult, Rect, TextBlock
from computer_use_mcp.backend.linux import inject as inject_mod
from computer_use_mcp.backend.linux.inject import XdotoolInjector
from computer_use_mcp.core import display as display_mod
from computer_use_mcp.core import screen_lock
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import (
    LEVEL_ELEMENT,
    BackendUnavailableError,
    ComputerUseError,
    InjectionError,
    InvalidRefError,
    SandboxUnavailableError,
    ScreenBusyError,
    to_friendly_text,
)

# 注：拆分（2026-09-18）后各文件共用**同一份 import 头**，其中未用到的名字无害。
# 统一的好处是不会漏项——按需裁剪时漏掉一个 import，报错点会离真正的原因很远。

from _helpers import _StubBackend, _proc, _reset_display  # noqa: E402




def test_ensure_started_attempts_once_then_stops(monkeypatch):
    """冷启动只尝试一次：Xephyr 缺失时第二次调用不再重试（否则每次调用都白等超时）。"""
    _reset_display(monkeypatch)
    tried: list[str] = []
    monkeypatch.setattr(display_mod.shutil, "which",
                        lambda name: (tried.append(name), None)[1])  # 模拟什么都没装

    assert display_mod.MANAGER.ensure_started()["sandbox_up"] is False
    assert display_mod.MANAGER.ensure_started()["sandbox_up"] is False
    assert tried.count("Xephyr") == 1, "冷启动失败只应尝试一次"





def test_ensure_started_rebuilds_after_sandbox_vanished(monkeypatch):
    """
    曾成功过 → 屏后来消失（被回收/崩了）应允许**有限次重建**，而不是一次失败就永久放弃。
    这是 B 防线「自愈优先、报错兜底」的一半。
    """
    _reset_display(monkeypatch, ever_up=True)
    tried: list[str] = []
    monkeypatch.setattr(display_mod.shutil, "which",
                        lambda name: (tried.append(name), None)[1])

    display_mod.MANAGER.ensure_started()
    display_mod.MANAGER.ensure_started()
    assert tried.count("Xephyr") == 2, "曾成功过的进程应允许重建（这里试两次）"

    # 但不得超过重建上限，否则每次工具调用都白等超时
    display_mod.MANAGER.ensure_started()
    display_mod.MANAGER.ensure_started()
    assert tried.count("Xephyr") == display_mod.MANAGER._max_attempts_rebuild





def test_ensure_started_serializes_concurrent_first_calls(monkeypatch):
    """
    并发首个调用必须串行等待，而不是后者看到「沙箱还没起来」就回落宿主桌面
    —— 那会把注入打到用户真实桌面上（本改动最不能出的错）。
    """
    import threading
    import time as _time

    _reset_display(monkeypatch)
    started = threading.Event()
    results: list[dict] = []

    def slow_start_locked():
        started.set()
        _time.sleep(0.3)  # 模拟 Xephyr 启动耗时
        return {"sandbox_up": True}

    monkeypatch.setattr(display_mod.MANAGER, "_start_locked", slow_start_locked)

    def call():
        results.append(display_mod.MANAGER.ensure_started())

    t1 = threading.Thread(target=call)
    t1.start()
    started.wait(2.0)          # 确保 t1 已进入启动临界区
    t2 = threading.Thread(target=call)
    t2.start()
    t1.join(5.0)
    t2.join(5.0)

    assert len(results) == 2
    assert all(r["sandbox_up"] is True for r in results), \
        "后到者必须等前者启完，不能读到「未就绪」"





def test_ensure_started_does_not_respawn_when_already_up(monkeypatch):
    """
    沙箱已在运行时绝不能再 spawn —— 回归测试。

    背景（实测踩到）：把「只尝试一次」放宽成「允许重建」之后，若 _start_locked 里少了
    「已在跑就返回」的判断，**每次工具调用**（每次都会进 ensure_started）都会再起一个
    Xephyr，而 stop() 只回收 _proc 指向的那一个，多余的变成孤儿 X server 长期驻留。
    实测一次会话里起了 3 个、留下 2 个孤儿。
    """
    _reset_display(monkeypatch, ever_up=True)
    spawned: list[str] = []
    monkeypatch.setattr(display_mod.MANAGER, "is_sandbox_up", lambda: True)
    monkeypatch.setattr(display_mod.MANAGER, "_spawn_xephyr",
                        lambda xephyr: (spawned.append(xephyr), True)[1])

    for _ in range(5):
        display_mod.MANAGER.ensure_started()
    assert spawned == [], f"沙箱已在运行时不应再 spawn（实际 spawn 了 {len(spawned)} 次）"





def test_sandbox_rebuild_quota_counts_consecutive_failures(monkeypatch):
    """
    沙箱重建配额必须计**连续失败次数**，而不是「本进程终生启动次数」。

    2026-09-15 实测踩到：`_start_attempts` 每次尝试都 +1、成功却不清零，于是
    `_max_attempts_rebuild=3` 变成「一辈子最多起 3 次沙箱」。屏被回收两次后配额
    用尽 → 此后**即便每次重建都成功**也永久拒绝启动 → 所有注入类工具一律报
    「隔离沙箱不可用，已拒绝执行」，而 Xephyr 手动启动完全正常，排查极易跑偏
    到「Xephyr 坏了」。

    本用例模拟「屏被反复回收、每次重建都成功」，连做 5 轮，第 5 轮仍必须能起来。
    """
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_mode", dm.MODE_ISOLATED)
    monkeypatch.setattr(dm.shutil, "which", lambda n: "/usr/bin/Xephyr")
    monkeypatch.setattr(mgr, "_host_active_wid", lambda: None)
    monkeypatch.setattr(mgr, "_restore_host_focus", lambda wid: None)
    monkeypatch.setattr(mgr, "_start_sandbox_wm", lambda: None)
    # ⚠️ 必须一并拦掉私有 AT-SPI 总线：它有真实副作用（起 dbus-daemon + registryd）。
    # 漏拦的代价实测过——本用例会真的起 5 轮、耗时从 6s 涨到 31s，还会留孤儿进程。
    monkeypatch.setattr(mgr, "_start_at_spi_bus", lambda: False)

    starts = {"n": 0}

    def fake_spawn(xephyr):
        starts["n"] += 1
        mgr._sandbox_display = ":9"
        return True

    monkeypatch.setattr(mgr, "_spawn_xephyr", fake_spawn)
    # 屏状态可外部控制：模拟「起来了 → 被回收 → 再重建」
    up = {"v": False}
    monkeypatch.setattr(mgr, "is_sandbox_up", lambda: up["v"])

    for i in range(5):
        up["v"] = False          # 屏被回收
        mgr.ensure_started()
        assert starts["n"] == i + 1, \
            f"第 {i+1} 轮没重建（配额被终生计数耗尽）——这正是本次要修的 bug"
        up["v"] = True           # 起来了，下一轮前会被回收

    assert mgr._start_attempts == 0, "成功启动后计数必须清零（计的是连续失败）"





def test_sandbox_gives_up_only_after_consecutive_failures(monkeypatch):
    """反面：连续失败到上限就该停手（避免每次工具调用都白等一次启动超时）。"""
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_mode", dm.MODE_ISOLATED)
    monkeypatch.setattr(dm.shutil, "which", lambda n: "/usr/bin/Xephyr")
    monkeypatch.setattr(mgr, "_host_active_wid", lambda: None)
    monkeypatch.setattr(mgr, "is_sandbox_up", lambda: False)
    tries = {"n": 0}
    monkeypatch.setattr(mgr, "_spawn_xephyr",
                        lambda x: (tries.__setitem__("n", tries["n"] + 1), False)[1])
    mgr._ever_up = True          # 曾成功过 → 重建档上限 3

    for _ in range(10):
        mgr.ensure_started()
    assert tries["n"] == mgr._max_attempts_rebuild, \
        f"连续失败应止于 {mgr._max_attempts_rebuild} 次，实际试了 {tries['n']} 次"





# ---------- a11y 隔离：沙箱私有 AT-SPI 总线 ----------
def test_app_env_pins_sandbox_at_spi_bus(monkeypatch):
    """
    2026-09-15 事故：沙箱不隔离 AT-SPI，对沙箱内应用做 a11y 遍历会打崩宿主
    GNOME Shell（gnome-shell 自己的 atk-bridge use-after-free，已离线解 core 确证）。

    做法 = 让沙箱内应用的 AT_SPI_BUS_ADDRESS 指向沙箱**私有**总线；
    私有总线起不来时回退死地址（连不上 a11y）→ 零注册零流量 → 宿主完全不受影响。

    为什么是总线地址而不是 NO_AT_BRIDGE（实测教训，别改回去）：
      zenity 是 GTK4 根本不加载 atk-bridge；SWT（DBeaver）的库里没有任何
      NO_AT_BRIDGE/GTK_A11Y 字符串 —— 按工具链的开关覆盖不全，对最要紧的 DBeaver 无效。
      AT_SPI_BUS_ADDRESS 则工具链无关（GTK3/GTK4/SWT/Qt/Electron 都走 libatspi）。
      实测 dbus-monitor：不设变量时应用会问会话总线要 org.a11y.Bus（1 次）；
      指向死地址时**0 次** —— 尊重该变量且不回落。

    三条边界：① isolated 的应用 env 要设；② real 模式不设；③ env_for 不设
    （i3/xdotool/xclip 共用，不该被污染）。
    """
    from computer_use_mcp.core import display as dm

    monkeypatch.setattr(dm.MANAGER, "_mode", dm.MODE_ISOLATED)
    # ⚠️ 「私有总线未就绪」是**本用例的起点前提**，必须显式声明。
    # 不声明会怎样（2026-09-18 实测）：单测模式下没有真沙箱，`_at_spi_bus` 本就是 None，
    # 于是它**看起来**不依赖任何东西；可 e2e 模式下 test_e2e_zenity 的 module 级 fixture
    # 起过真沙箱、`_at_spi_bus` 有值，下面的死地址断言就会拿到真实总线地址而失败。
    # 历史上它一直靠 `_reset_display` 那条「回收现场」的副作用（把 `_at_spi_bus` 置 None
    # 且不还原）侥幸通过 —— 换句话说，**它此前是被另一个缺陷喂着的**；那条副作用修掉后
    # 立刻暴露。凡「断言某个全局默认值」的用例都该像这样把起点钉死。
    monkeypatch.setattr(dm.MANAGER, "_at_spi_bus", None)
    monkeypatch.delenv(dm.ENV_SANDBOX_AT_SPI_BUS, raising=False)
    assert dm.app_env()["AT_SPI_BUS_ADDRESS"] == dm._DEAD_AT_SPI_BUS
    assert "AT_SPI_BUS_ADDRESS" not in dm.env_for(), "env_for 是通用通道，不得被污染"

    # 私有总线落地后只需改这里：环境变量覆盖 → 应用就导向真实总线
    monkeypatch.setenv(dm.ENV_SANDBOX_AT_SPI_BUS, "unix:path=/tmp/fake-private-bus")
    assert dm.app_env()["AT_SPI_BUS_ADDRESS"] == "unix:path=/tmp/fake-private-bus"

    monkeypatch.setattr(dm.MANAGER, "_mode", dm.MODE_REAL)
    monkeypatch.delenv(dm.ENV_SANDBOX_AT_SPI_BUS, raising=False)
    assert "AT_SPI_BUS_ADDRESS" not in dm.app_env(), "real 模式不该改应用环境"





def test_app_env_strips_frozen_library_path(monkeypatch, tmp_path):
    """
    2026-09-15 实测：launch_app 把 server（PyInstaller 产物）的 LD_LIBRARY_PATH
    (`.../_internal`) 原样传给子进程，DBeaver 因此加载了其中 71 个打包库
    （libglib/libgio/libgtk/libdbus/libatspi…）。而 _internal 的 libglib/libgio 与系统
    **不是同一份文件** —— 混装后果两条都已实测：
      ① JVM 在 libc 内 abort 崩溃（hs_err: Problematic frame = libc.so.6+0x4798b）；
      ② a11y 注册泄漏到宿主总线，私有总线隔离失效。

    做法：app_env（启动沙箱应用的专用通道）剥掉指向产物目录 sys._MEIPASS 及其子目录的
    路径项；用户/conda 有意设置的路径原样保留。
    """
    from computer_use_mcp.core import display as dm

    frozen = tmp_path / "frozen"
    inner = frozen / "_internal"     # onedir 布局：_internal 是 _MEIPASS 的子目录
    inner.mkdir(parents=True)
    user_lib = tmp_path / "userlib"  # 模拟用户/conda 自己设置的路径
    user_lib.mkdir()

    monkeypatch.setattr(dm.sys, "_MEIPASS", str(frozen), raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", os.pathsep.join([str(inner), str(user_lib)]))
    assert dm.app_env()["LD_LIBRARY_PATH"] == str(user_lib), "产物目录要剥、用户路径要留"

    # 只剩产物路径 → 变量整个消失（不留空串，免得 ld.so 把空项当 cwd）
    monkeypatch.setenv("LD_LIBRARY_PATH", str(inner))
    assert "LD_LIBRARY_PATH" not in dm.app_env()

    # 未冻结（开发模式）→ 一个都不许动，否则 conda 的 gi 都导入不了
    monkeypatch.delattr(dm.sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", str(inner))
    assert dm.app_env()["LD_LIBRARY_PATH"] == str(inner)





# ---------- 沙箱残留回收（Xephyr 被外部杀死时）----------
def test_reap_dead_sandbox_cleans_orphan_residue(tmp_path):
    """
    2026-09-15 实测：用户直接关掉沙箱窗口（Xephyr 被外部杀死）时 stop() 不会被调用，
    而 _start_locked 的重建路径会**覆盖** _proc/_at_spi_proc/_wm_proc 三个 Popen 字段 ——
    旧进程再没人 wait，于是每次重建泄漏一组孤儿 dbus-daemon+registryd、一个
    /tmp/cc-cu-at-spi-* 目录，以及若干僵尸表项（现场清点时确实看到 2 组孤儿 + 4 个 Z）。

    用**真进程**验证回收确实发生，而不是只断言字段被置空。
    """
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    proc = subprocess.Popen(["sleep", "60"])   # 冒充上一轮已死的 Xephyr
    wm = subprocess.Popen(["sleep", "60"])     # 冒充上一轮的沙箱 WM(i3)
    bus_dir = tmp_path / "cc-cu-at-spi-fake"
    bus_dir.mkdir()
    mgr._proc, mgr._wm_proc = proc, wm
    mgr._at_spi_dir = str(bus_dir)

    mgr._reap_dead_sandbox()

    assert proc.poll() is not None, "自管的 Xephyr 必须被终止"
    assert wm.poll() is not None, "自管的沙箱 WM 必须被终止"
    assert not bus_dir.exists(), "私有总线工作目录必须被删除"
    assert mgr._proc is None and mgr._wm_proc is None and mgr._at_spi_dir is None





def test_reap_dead_sandbox_spares_attached_sandbox(tmp_path):
    """
    attach 的外部沙箱（用户显式设固定屏号、多会话共用一块屏）不归本进程管 —— 回收时
    必须原样放过，与 stop() 里的 attached 判断保持一致。
    """
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    mgr._attached = True
    proc = subprocess.Popen(["sleep", "60"])
    bus_dir = tmp_path / "cc-cu-at-spi-attached"
    bus_dir.mkdir()
    mgr._proc, mgr._at_spi_dir = proc, str(bus_dir)

    try:
        mgr._reap_dead_sandbox()
        assert proc.poll() is None, "外部沙箱的进程不许碰"
        assert bus_dir.exists(), "外部沙箱的总线目录不许删"
        assert mgr._attached is False, "标志要复位，供下次重新判定"
    finally:
        proc.terminate()
        proc.wait(timeout=5)





def test_reap_children_reaps_zombies():
    """
    这些 Popen 原本只在 stop() 里 wait，而 stop() 挂在显式调用/atexit 上 —— 沙箱被外部
    关掉时不会触发，退出的子进程就成了僵尸。_reap_children 用 poll() 收掉它们。
    """
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    proc = subprocess.Popen(["/bin/true"])
    time.sleep(0.3)  # 让它退出，但 Popen 还没 wait → 僵尸态
    mgr._proc = proc

    mgr._reap_children()

    assert proc.returncode is not None, "poll() 应当回收僵尸并记下退出码"
    assert mgr._proc is proc, "只 reap，不改字段引用"





# ---------- 私有 AT-SPI 总线（a11y 隔离的正式解）----------
def test_private_at_spi_bus_address_feeds_apps(monkeypatch):
    """
    私有总线就绪时，应用拿到的必须是它的地址；未就绪则回退死地址（保证不漏到宿主总线）。

    死地址是**刻意的兜底**：宁可应用完全没有 a11y，也绝不能让 a11y 流量落到宿主
    总线上——那正是打崩宿主 GNOME Shell 的路径（2026-09-15 离线解 core 确证）。

    ⚠️ 全程 mock，不起真进程（_start_at_spi_bus 有真实副作用）。
    """
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_mode", dm.MODE_ISOLATED)
    monkeypatch.delenv(dm.ENV_SANDBOX_AT_SPI_BUS, raising=False)

    # 私有总线未就绪 → 死地址
    assert mgr.sandbox_at_spi_bus() == dm._DEAD_AT_SPI_BUS
    assert mgr.at_spi_bus() is None

    # 私有总线就绪 → 它的地址
    monkeypatch.setattr(mgr, "_at_spi_bus", "unix:path=/tmp/cc-cu-at-spi-x/bus")
    assert mgr.sandbox_at_spi_bus() == "unix:path=/tmp/cc-cu-at-spi-x/bus"
    assert mgr.app_env()["AT_SPI_BUS_ADDRESS"] == "unix:path=/tmp/cc-cu-at-spi-x/bus"

    # 调试覆盖优先
    monkeypatch.setenv(dm.ENV_SANDBOX_AT_SPI_BUS, "unix:path=/tmp/override")
    assert mgr.sandbox_at_spi_bus() == "unix:path=/tmp/override"





# ---------- attach / 多会话共用一块屏：按屏号反查私有总线 ----------
def test_display_num_encodes_screen_into_resource_names(monkeypatch):
    """
    屏号必须能稳定编码进资源名（目录名 `cc-cu-at-spi-d<屏号>-<随机>`）—— 这是 attach
    反查的**唯一依据**，格式一漂就静默退回死地址（a11y 全失效且界面看不出异常）。
    """
    from computer_use_mcp.core import display as dm

    mgr = dm.DisplayManager()
    for disp, want in ((":0", "0"), (":99", "99"), (":0.0", "0"), (":12.1", "12")):
        monkeypatch.setattr(mgr, "_sandbox_display", disp)
        assert mgr._display_num() == want
        assert mgr._at_spi_dir_prefix() == f"cc-cu-at-spi-d{want}-"
        assert mgr._socket_path() == f"/tmp/.X11-unix/X{want}"

    # 屏号未分配（自动模式下 X server 还没回写）→ 空串 + 退回无屏号前缀
    monkeypatch.setattr(mgr, "_sandbox_display", "")
    assert mgr._display_num() == ""
    assert mgr._at_spi_dir_prefix() == "cc-cu-at-spi-"





def test_discover_at_spi_bus_scoped_to_screen_and_picks_newest(monkeypatch, tmp_path):
    """
    反查规则：**只认本屏号**的目录（认错 = 把 a11y 接到别的会话那块屏上），多个候选取
    bus socket 最新那个（旧残留目录可能对应一条已经死掉的总线）。
    """
    from computer_use_mcp.core import display as dm

    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_sandbox_display", ":0")

    # ① 什么都没有 → None（调用方保持死地址兜底，兜底方向不能反）
    assert mgr._discover_at_spi_bus() is None

    # ② 有目录但没有 bus socket（纯残留）→ 仍然 None
    # 目录权限必须与产线一致：`_start_at_spi_bus` 用 tempfile.mkdtemp()（0700），
    # 而 `_discover_at_spi_bus` 会据此把「group/other 可读」的目录判为**可疑的伪造
    # 总线**（M-10②）。夹具要是用 Path.mkdir() 的默认权限（0775），测的就不是产线形态。
    (tmp_path / "cc-cu-at-spi-d0-nosock").mkdir(mode=0o700)
    assert mgr._discover_at_spi_bus() is None

    # ③ 两个候选 → 取 bus socket 更新的那个
    old, new = tmp_path / "cc-cu-at-spi-d0-old", tmp_path / "cc-cu-at-spi-d0-new"
    for d, ts in ((old, 1_000_000), (new, 2_000_000)):
        d.mkdir(mode=0o700)
        (d / "bus").write_text("")
        os.utime(d / "bus", (ts, ts))
    assert mgr._discover_at_spi_bus() == f"unix:path={new}/bus"

    # ④ 别的屏号的目录再新也不认领（隔离底线）
    other = tmp_path / "cc-cu-at-spi-d9-other"
    other.mkdir(mode=0o700)
    (other / "bus").write_text("")
    os.utime(other / "bus", (3_000_000, 3_000_000))
    assert mgr._discover_at_spi_bus() == f"unix:path={new}/bus", \
        "只许认领本屏号的沙箱，否则 a11y 会接到别的会话那块屏上"

    # ⑤ 屏号未知 → 无从反查
    monkeypatch.setattr(mgr, "_sandbox_display", "")
    assert mgr._discover_at_spi_bus() is None





def test_shared_screen_path_recovers_private_bus(monkeypatch, tmp_path):
    """
    **「多会话共用一块屏」必须真的接上私有总线** —— 它走的是 is_sandbox_up() 的**早退**
    分支，不是 attach 分支（只修 attach 分支等于没修，那是死代码）。

    2026-09-16 实测缺陷链：本进程 `_proc` 为 None（沙箱是对方起的）、也从没跑过
    `_start_at_spi_bus` → `_at_spi_bus` 恒为 None → `sandbox_at_spi_bus()` 返回**死地址**
    → 沙箱内新起的应用与 MCP reader 全都没有 a11y 能力，而界面看上去一切正常（极隐蔽）。
    """
    from computer_use_mcp.core import display as dm

    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    bus_dir = tmp_path / "cc-cu-at-spi-d5-abc123"
    # 与产线一致：mkdtemp 出来的目录是 0700（见 _discover_at_spi_bus 的伪造总线校验）
    bus_dir.mkdir(mode=0o700)
    (bus_dir / "bus").write_text("")

    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_mode", dm.MODE_ISOLATED)
    monkeypatch.setattr(mgr, "_sandbox_display", ":5")    # 显式固定屏号 = 共用一块屏
    monkeypatch.setattr(mgr, "_auto_display", False)
    sock = tmp_path / "X5"
    sock.write_text("")
    monkeypatch.setattr(mgr, "_socket_path", lambda: str(sock))
    assert mgr.is_sandbox_up() is True, "前提：对方起的沙箱让 socket 存在"

    mgr._start_locked()

    assert mgr._attached is False, "这条走的是早退分支，不是 attach 分支"
    assert mgr._at_spi_bus == f"unix:path={bus_dir}/bus"
    assert mgr.sandbox_at_spi_bus() == f"unix:path={bus_dir}/bus"
    assert mgr.app_env()["AT_SPI_BUS_ADDRESS"] == f"unix:path={bus_dir}/bus", \
        "沙箱内新起的应用必须拿到私有总线，否则它们完全没有 a11y"





def test_ensure_at_spi_bus_never_overrides_self_started(monkeypatch, tmp_path):
    """
    自己起的沙箱，其总线地址由 `_start_at_spi_bus()` 权威给出，**不许**被目录扫描改写 ——
    同屏号下若残留别的目录，改写会把 a11y 接到错误的（甚至已死的）总线上。
    """
    from computer_use_mcp.core import display as dm

    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    decoy = tmp_path / "cc-cu-at-spi-d6-decoy"
    decoy.mkdir()
    (decoy / "bus").write_text("")

    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_sandbox_display", ":6")
    monkeypatch.setattr(mgr, "_proc", object())   # 自管的 Xephyr 还在
    monkeypatch.setattr(mgr, "_at_spi_bus", "unix:path=/tmp/authoritative/bus")

    mgr._ensure_at_spi_bus()
    assert mgr._at_spi_bus == "unix:path=/tmp/authoritative/bus"

    # 自起沙箱的总线启动失败（_at_spi_bus 仍为 None）→ 也不许拿别的目录顶替
    monkeypatch.setattr(mgr, "_at_spi_bus", None)
    mgr._ensure_at_spi_bus()
    assert mgr._at_spi_bus is None, "自起沙箱的总线失败就是失败，不许被残留目录顶替"





def test_sweep_stale_buses_only_touches_own_screen(monkeypatch, tmp_path):
    """
    清扫必须**只动本屏号**的残留目录，且放过自己的那个 —— 跨屏清扫会误杀别的会话正在
    用的总线（自动分配屏号下各会话各占一块屏）。

    背景（2026-09-16 实测）：MCP server 被强杀时 atexit 不跑，它起的 dbus-daemon /
    registryd 被 systemd 收养成孤儿、/tmp 目录留存，**跨会话无人回收**。
    """
    from computer_use_mcp.core import display as dm

    monkeypatch.setattr(dm.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dm.DisplayManager, "_kill_bus_processes",
                        staticmethod(lambda d: []))   # 只验目录取舍，不起真进程

    mine = tmp_path / "cc-cu-at-spi-d0-mine"
    stale = tmp_path / "cc-cu-at-spi-d0-stale"
    other = tmp_path / "cc-cu-at-spi-d7-other"
    for d in (mine, stale, other):
        d.mkdir(mode=0o700)
        (d / "bus").write_text("")

    mgr = dm.DisplayManager()
    monkeypatch.setattr(mgr, "_sandbox_display", ":0")
    monkeypatch.setattr(mgr, "_at_spi_dir", str(mine))

    mgr._sweep_stale_buses()

    assert mine.exists(), "自己的目录不许被清"
    assert not stale.exists(), "同屏号的残留必须清掉"
    assert other.exists(), "别的屏号的目录绝对不许碰（那是别人的会话）"





def test_kill_bus_processes_matches_cmdline_and_environ(tmp_path):
    """
    反向扫 /proc 的匹配依据要能认出总线上的**两类**进程，缺一不可：
      - dbus-daemon：`--address unix:path=<目录>/bus` 在 **cmdline** 里；
      - at-spi2-registryd：命令行不含目录，只能靠 **environ** 的 AT_SPI_BUS_ADDRESS。
    实测残留的正是这两个进程 —— 漏认一类就会留下孤儿。

    用**真进程**验证确实被终止（而不是只断言返回了 pid），并确认无关进程不被误杀。
    """
    from computer_use_mcp.core import display as dm

    bus_dir = tmp_path / "cc-cu-at-spi-d0-kill"
    bus_dir.mkdir()

    by_cmd = subprocess.Popen(["sh", "-c", "sleep 30; true", "-", str(bus_dir)])
    by_env = subprocess.Popen(
        ["sh", "-c", "sleep 30; true"],
        env={**os.environ, "AT_SPI_BUS_ADDRESS": f"unix:path={bus_dir}/bus"})
    innocent = subprocess.Popen(["sh", "-c", "sleep 30; true"])   # 无关进程

    try:
        killed = dm.DisplayManager._kill_bus_processes(str(bus_dir))
        assert by_cmd.pid in killed, "cmdline 里含目录的进程必须被认出"
        assert by_env.pid in killed, "只靠 environ 能认出的进程必须被认出"
        assert innocent.pid not in killed, "无关进程绝不能被误杀"

        for p in (by_cmd, by_env):
            assert p.wait(timeout=5) == -signal.SIGTERM, "必须真的被终止"
        assert innocent.poll() is None, "无关进程应当还活着"
    finally:
        for p in (by_cmd, by_env, innocent):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)





def test_reader_sync_bus_follows_sandbox(monkeypatch):
    """
    reader 必须**首次就绑对**总线；已绑定后总线再变，只能明确报错，**不能 exit**。

    背景（2026-09-16 实测根因）：`atspi_init()` 是一次性的 —— 进程内第一次连上哪条总线
    就永远是那条。本用例原先断言「换总线必须 exit 掉旧连接」，而那正是导致**永久失联**的
    写法：exit 之后本项目没有任何地方重新 init（`import_atspi()` 只是 Python 层 import，
    有模块缓存，不会重调 atspi_init），而跨总线 `Atspi.exit()` + `Atspi.init()` 实测会损坏
    libatspi 内部状态（`g_hash_table_insert_internal` / `g_object_unref` 断言失败），
    随后任何访问都报 `atspi_error: The application no longer exists`。
    故现在改为「切不动 → 标记 stale + 给可操作的错误」，让用户知道重启即可恢复。
    """
    from computer_use_mcp.backend.linux.atspi import AtspiReader, _BUS_STALE_ERROR

    r = AtspiReader()
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_ISOLATED)
    monkeypatch.setattr(display_mod.MANAGER, "is_sandbox_up", lambda: True)
    monkeypatch.setattr(display_mod.MANAGER, "_at_spi_bus", "unix:path=/tmp/priv-1")
    monkeypatch.delenv("AT_SPI_BUS_ADDRESS", raising=False)

    # ① 首次绑定：只写环境变量（真正 init 发生在下一次 _atspi()，即首次用 a11y 时）
    assert r.sync_bus() is True
    assert os.environ["AT_SPI_BUS_ADDRESS"] == "unix:path=/tmp/priv-1"
    assert r._bound_address == "unix:path=/tmp/priv-1"
    assert r._bus_stale is False

    # ② 已对齐 → no-op（每次工具调用都会走到这里，必须廉价且无副作用）
    assert r.sync_bus() is False, "已对齐就不该重复切换（每次工具调用都会走到这）"
    assert r._bus_stale is False

    # ③ 沙箱重建 → 总线变 → 进程内切不动：不 exit、不改环境变量，给明确错误
    monkeypatch.setattr(display_mod.MANAGER, "_at_spi_bus", "unix:path=/tmp/priv-2")
    r._Atspi = object()          # 假装已建立连接
    r._available = True
    r._error = None
    assert r.sync_bus() is False, "切不动时不得声称切换成功"
    assert r._Atspi is not None, "绝不能 exit —— exit 之后无法重新 init，会永久失联"
    assert r._bus_stale is True
    assert r._available is False, "已判定不可用，不能留着旧的 True 缓存"
    assert r.error == _BUS_STALE_ERROR
    assert "重启" in (r.error or ""), "错误必须给出可操作的下一步"
    assert os.environ["AT_SPI_BUS_ADDRESS"] == "unix:path=/tmp/priv-1", \
        "stale 时旧连接仍在用，不得改写环境变量"

    # ④ stale 后仍幂等：还是 False、错误与绑定状态都不变（不刷日志、不漂移）
    assert r.sync_bus() is False
    assert r.error == _BUS_STALE_ERROR
    assert r._bound_address == "unix:path=/tmp/priv-1"

    # ⑤ real 模式 + **尚未绑定** → 目标就是宿主总线，want=None == _bound_address=None，no-op
    r2 = AtspiReader()
    monkeypatch.setattr(display_mod.MANAGER, "_mode", display_mod.MODE_REAL)
    monkeypatch.delenv("AT_SPI_BUS_ADDRESS", raising=False)
    assert r2.sync_bus() is False
    assert r2._bound_address is None
    assert "AT_SPI_BUS_ADDRESS" not in os.environ
    assert r2._bus_stale is False


# ========================================================================
# _reset_display 的两道「不许碰真机现场」闸（2026-09-18 补，拆 test_optimizations 时发现）
# ========================================================================
def test_reset_display_stub_never_deletes_anything(monkeypatch, tmp_path):
    """
    `_reset_display` 打桩掉的两个方法必须**真的无副作用**——这里用行为判据钉死。

    为什么要有这条：那两行打桩在**单测模式下看不出任何效果**（real 模式、机器上没有
    我们的真沙箱，`_reap_dead_sandbox` 本就是 no-op），极易被后来者当成冗余清理掉。
    而它们防的是一件很贵的事：

        本进程更早真起过一个沙箱时，`_reset_display` 会把 `_proc` 置 None、socket 路径
        指向不存在的文件 → 再进 `_start_locked` 就被判成「沙箱不在跑」→ 走**重建**分支
        → 先调 `_reap_dead_sandbox()`，它**杀掉那个活沙箱的 dbus-daemon / registryd、
        rmtree 掉 `/tmp/cc-cu-at-spi-*`、并把 `_at_spi_bus` 置 None**（这个赋值不在
        monkeypatch 账上，teardown 不还原）。随后 e2e 的 `display.start()` 因 socket 仍在
        而早退，reader 却再也反查不到总线 → 绑不上私有总线 → 流量落到**宿主总线** →
        libatspi 在 `atspi.get_desktop(0)` 处 SIGABRT，整个 pytest 进程中止。

    ⚠️ 判据为什么不是「方法有没有被调用」：打桩是用 no-op **遮蔽**实例方法，任何调用探针
    都会跟它一起被遮蔽。所以这里改判**副作用**：给一个真目录，让它走一遍，目录必须还在
    （未打桩的真实现会 `shutil.rmtree` 掉它）。
    """
    dm = display_mod.MANAGER
    victim = tmp_path / "cc-cu-at-spi-d0-fake"
    victim.mkdir()
    assert victim.exists()

    _reset_display(monkeypatch)

    # 真实现：`_kill_at_spi_procs(..., directory)` 会 rmtree(directory)；打桩后不该动它
    dm._kill_at_spi_procs(None, None, str(victim))
    assert victim.exists(), (
        "`_reset_display` 必须打桩 `_kill_at_spi_procs` —— 否则它会删掉真机上的总线目录"
    )
    # `_reap_dead_sandbox` 真实现会把 _at_spi_* 全清空；打桩后是 no-op
    before = (dm._at_spi_bus, dm._at_spi_dir, dm._at_spi_proc, dm._at_spi_registry)
    dm._reap_dead_sandbox()
    after = (dm._at_spi_bus, dm._at_spi_dir, dm._at_spi_proc, dm._at_spi_registry)
    assert after == before, (
        "`_reset_display` 必须打桩 `_reap_dead_sandbox` —— 它带真实副作用（杀进程 + 清状态）"
    )
