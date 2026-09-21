"""
沙箱私有 AT-SPI 总线（core.display.at_spi_bus）—— a11y 隔离的正解。

【问题】虚拟屏**隔离不了 AT-SPI**：Xephyr 只换掉 DISPLAY，而 a11y 走会话 D-Bus，
沙箱内外共用同一个 `at-spi2-registryd`。对沙箱内应用做 a11y 遍历会把宿主 GNOME Shell
打崩（2026-09-15 实测，离线解 core 确证是 gnome-shell 自己的 atk-bridge 在对已释放的
GObject 做 `g_object_ref`，use-after-free；一切"量级防护"都挡不住）。

【方案】沙箱启动时自起一套私有总线（私有目录里的 `dbus-daemon` +
`at-spi2-registryd`），沙箱应用与 MCP 的 `AtspiReader` 都连它 → 宿主总线上完全看不到
沙箱内应用，而 a11y 能力保留。起不来则应用拿到**死地址**兜底：宁可没有 a11y，
也绝不让流量落到宿主总线。

【attach 场景】多会话共用一块屏时，本进程不是沙箱的创建者，只能按**屏号**反查总线
（目录名里带屏号正是为此）。反查不到就保持死地址兜底 —— 方向不能反。

⚠️ **绝不能用 `at-spi-bus-launcher`**：它按 `XDG_RUNTIME_DIR` 推导总线路径
（`$XDG_RUNTIME_DIR/at-spi/bus_<n>`），会直接抢占宿主同路径的 socket（2026-09-15 实测
踩到：宿主的 `at-spi/bus_1` 被顶掉）。自己起 dbus-daemon 并**显式给地址**，
从构造上就不可能碰到 `/run/user/*`。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from .constants import (
    ENV_SANDBOX_AT_SPI_BUS,
    _AT_SPI_CONF_CANDIDATES,
    _AT_SPI_DIR_PREFIX,
    _AT_SPI_REGISTRYD_CANDIDATES,
    _AT_SPI_TIMEOUT,
    _DEAD_AT_SPI_BUS,
    _resolve_at_spi_deps,
    log,
)


class AtSpibusMixin:
    """起/收私有 AT-SPI 总线，并按屏号反查与清扫残留。"""

    def _init_at_spi(self) -> None:
        # 沙箱私有 AT-SPI 总线（a11y 隔离，见 _start_at_spi_bus 的长注释）
        self._at_spi_proc: subprocess.Popen | None = None      # 私有总线 dbus-daemon
        self._at_spi_registry: subprocess.Popen | None = None  # 该总线上的 registryd
        self._at_spi_dir: str | None = None                    # 私有工作目录（socket 所在）
        self._at_spi_bus: str | None = None                    # 私有总线地址
        # 旧格式总线残留的告警限流（M-56②）：每次重建沙箱都会扫，只提醒一次就够。
        self._legacy_bus_warned = False

    def sandbox_at_spi_bus(self) -> str:
        """
        沙箱应用应使用的 AT-SPI 总线地址：私有总线就绪则用它，否则回退死地址兜底。

        死地址是**刻意的兜底**：宁可应用完全没有 a11y，也不能让它落到宿主总线上
        （那正是打崩宿主 GNOME Shell 的路径）。实测 libatspi 尊重该变量且不回落。
        """
        override = os.environ.get(ENV_SANDBOX_AT_SPI_BUS)
        if override:
            return override
        return self._at_spi_bus or _DEAD_AT_SPI_BUS

    def at_spi_bus(self) -> str | None:
        """私有 AT-SPI 总线地址；未启用时为 None（a11y 侧据此决定是否切换连接）。"""
        return self._at_spi_bus

    def _start_at_spi_bus(self) -> bool:
        """
        起沙箱**私有**的 AT-SPI 总线（dbus-daemon）+ registryd；成功返回 True。

        为什么需要（2026-09-15 事故，已离线解 core 确证）：Xephyr 只隔离 X11 通道，
        a11y 走会话 D-Bus —— 沙箱内外共用宿主 at-spi2-registryd，对沙箱应用做 a11y
        遍历会把宿主 GNOME Shell 打崩（gnome-shell 自己的 atk-bridge 对已释放 GObject
        做 g_object_ref，use-after-free）。给它一条私有总线，宿主就再也看不见这些应用。

        实现逻辑（**每条路径都在我们自己的临时目录里，完全自控**）：
          1. 建私有目录（socket 放这里）。
          2. 起 dbus-daemon：用 AT-SPI 标准配置 accessibility.conf，并用 --address
             显式指定 unix:path=<私有目录>/bus。
          3. 起 at-spi2-registryd，AT_SPI_BUS_ADDRESS 指向该地址。
          4. 沙箱应用（app_env）与 MCP 的 AtspiReader 都用同一地址。

        ⚠️ **绝不能用 at-spi-bus-launcher**：它按 XDG_RUNTIME_DIR 推导总线路径
        （$XDG_RUNTIME_DIR/at-spi/bus_<n>），会直接抢占宿主同路径的 socket ——
        2026-09-15 实测踩到过：宿主的 at-spi/bus_1 被顶掉，只能靠
        `systemctl --user restart at-spi-dbus-bus.service` 恢复。自己起 dbus-daemon
        并显式给地址，从构造上就不可能碰到 /run/user/*。
        """
        at_spi_conf, registryd = _resolve_at_spi_deps()
        if not (at_spi_conf and registryd):
            log.warning(
                "缺少 accessibility.conf 或 at-spi2-registryd，私有 AT-SPI 总线不可用；"
                "沙箱应用将继续走死地址（无 a11y 能力）。config 已试：%s；"
                "registryd 已试：%s 及 PATH",
                ", ".join(_AT_SPI_CONF_CANDIDATES),
                ", ".join(_AT_SPI_REGISTRYD_CANDIDATES))
            return False
        dbus_daemon = shutil.which("dbus-daemon")
        if not dbus_daemon:
            log.warning("找不到 dbus-daemon，私有 AT-SPI 总线不可用")
            return False

        # 先扫掉本屏号下的历史残留：MCP server 被强杀时 atexit 不跑，会留下孤儿总线
        # 进程与 /tmp 目录（见 _sweep_stale_buses）。此刻我们自己的 Xephyr 已经起来了，
        # 本屏上不可能还有活跃的别家沙箱 —— 所以同屏号旧目录必是残留。
        self._sweep_stale_buses()
        # 目录名带屏号：attach 场景（多会话共用一块屏）靠它反查到这条总线
        d = tempfile.mkdtemp(prefix=self._at_spi_dir_prefix())
        bus = f"unix:path={d}/bus"
        proc = registry = None
        try:
            proc = subprocess.Popen(
                [dbus_daemon, "--config-file", at_spi_conf, "--nofork", "--address", bus],
                # 走 env_for：dbus-daemon 是系统二进制（本机 PATH 解析到 conda 的那份），
                # 而产物里打包了同名 libdbus-1.so.3 / libexpat.so.1 —— 不剥就会让它按
                # 产物内的版本加载，与系统其余部分混装（2026-09-16 实测确认原先确实继承）。
                env=self.env_for(self._sandbox_display),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            if not self._wait_socket(f"{d}/bus", _AT_SPI_TIMEOUT):
                raise RuntimeError("私有 AT-SPI 总线 socket 未出现")
            registry = subprocess.Popen(
                [registryd],
                # 在 env_for 之上单独设总线地址（registryd 是**唯一**需要 a11y 总线的
                # 子进程，故不能走 app_env —— 那是「启动应用」的语义，会带进死地址兜底
                # 之类的应用侧逻辑）。必须走 env_for 取底：registryd 链接的 libatspi /
                # libdbus / libgio / libglib / libgobject / libX11 **产物里全都有**，
                # 继承产物库路径一旦混装失败，表现是「私有总线静默失效 → a11y 整体
                # 不可用而界面看不出异常」，正是本项目最忌讳的隐蔽失效（实测确认原先
                # 确实继承）。
                env={**self.env_for(self._sandbox_display), "AT_SPI_BUS_ADDRESS": bus},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("私有 AT-SPI 总线启动失败：%s（沙箱应用将继续走死地址）", exc)
            self._kill_at_spi_procs(proc, registry, d)
            return False

        self._at_spi_proc, self._at_spi_registry = proc, registry
        self._at_spi_dir, self._at_spi_bus = d, bus
        log.info("沙箱私有 AT-SPI 总线就绪: %s（宿主 a11y 完全看不到沙箱内应用）", bus)
        return True

    def _at_spi_dir_prefix(self) -> str:
        """私有总线工作目录的前缀（含屏号），如 `cc-cu-at-spi-d0-`。

        屏号未知时退回不带屏号的前缀 —— 那种情况下也不可能有别的进程来 attach 反查它。
        """
        num = self._display_num()
        return f"{_AT_SPI_DIR_PREFIX}-d{num}-" if num else f"{_AT_SPI_DIR_PREFIX}-"

    def _ensure_at_spi_bus(self) -> None:
        """
        沙箱已就绪、但本进程还没绑定私有总线时，按屏号反查一次（幂等，廉价）。

        为什么需要（2026-09-16 实测缺陷）：attach 场景下本进程不是沙箱的创建者 ——
        `_proc is None`，`_start_at_spi_bus()` 从没跑过，于是 `_at_spi_bus` 恒为 None，
        而 `sandbox_at_spi_bus()` 在 None 时返回**死地址**兜底。结果是「多会话共用一块
        屏」时，沙箱内新起的应用与 MCP reader 全都没有 a11y 能力，而界面看上去一切
        正常 —— 属于很隐蔽的失效。

        ⚠️ 必须挂在 `_start_locked` 的**两条**路径上，缺一不可：
          ① `is_sandbox_up()` 为真时的早退分支 —— **「多会话共用一块屏」走的正是这条**：
             对方起的 Xephyr 让 socket 存在，本方 `_proc` 为 None 又让 `is_sandbox_up()`
             返回 True，于是根本进不了 attach 分支（只在那里修 = 死代码）；
          ② attach 分支 —— 本方自起的沙箱死后、屏上仍有 X server 的那条路。

        ⚠️ 只在 `_proc is None`（沙箱非本进程所起）时反查：自己起的沙箱走
        `_start_at_spi_bus()`，那条路径上的总线地址是权威的，不该被目录扫描改写。
        """
        if self._at_spi_bus is not None or self._proc is not None:
            return
        self._at_spi_bus = self._discover_at_spi_bus()

    def _discover_at_spi_bus(self) -> str | None:
        """
        attach 场景：按屏号找回这块屏所属沙箱的私有 AT-SPI 总线地址。

        为什么要专门反查（2026-09-16 实测的 attach 缺陷）：attach 时本进程不是沙箱的
        创建者，`_at_spi_bus` 恒为 None，于是 `sandbox_at_spi_bus()` 落到死地址兜底 ——
        「多会话共用一块屏」的场景下，新起的沙箱应用和 MCP reader 全都没有 a11y 能力，
        而界面看上去一切正常（隐蔽）。目录名里带屏号（见 `_at_spi_dir_prefix`）正是
        为了让这里能反查出来。

        实现逻辑：按 `<tmp>/cc-cu-at-spi-d<屏号>-*` 找候选目录，取其中 **bus socket
        最新**的那个。屏号理论上唯一，但可能残留旧目录，取新的更可能对应活着那条总线。
        找不到、或目录里没有 bus socket → 返回 None，由调用方保持死地址兜底。
        **兜底方向不能反**：宁可没有 a11y，也绝不让流量落到宿主总线。

        ⚠️ 判据是"socket 文件存在"，与 `is_sandbox_up()` 同策略 —— 不额外做连通性探测：
        真连不上时 `AtspiReader` 会给出明确的 available=False + error，代价可控。
        """
        num = self._display_num()
        if not num:
            return None
        pattern = os.path.join(tempfile.gettempdir(), f"{_AT_SPI_DIR_PREFIX}-d{num}-*")
        me = os.getuid()
        best: tuple[float, str] | None = None
        for d in glob.glob(pattern):
            # M-10②：目录名可预测（前缀带屏号），而 /tmp 是 1777 共享目录 —— 同机其他
            # 用户完全可以预置一个**伪造的总线目录**，让本进程把沙箱应用的 a11y 注册信息
            # 接到他控制的 D-Bus 上。我们自己建的目录是 mkdtemp 出来的（0700、属主本人），
            # 据此校验即可把伪造目录挡在外面。
            try:
                st = os.stat(d)
            except OSError:
                continue
            if st.st_uid != me or (st.st_mode & 0o077):
                log.warning("跳过可疑的 AT-SPI 总线目录（属主/权限不符，可能是同机他用户"
                            "预置的伪造总线）: %s uid=%s mode=%o（期望 uid=%s 且 group/other 无权限）",
                            d, st.st_uid, st.st_mode & 0o777, me)
                continue
            bus_path = os.path.join(d, "bus")
            if not os.path.exists(bus_path):
                continue
            try:
                mtime = os.path.getmtime(bus_path)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, bus_path)
        return f"unix:path={best[1]}" if best else None

    def _sweep_stale_buses(self) -> None:
        """
        清掉**本屏号**下遗留的私有总线残留（进程 + 工作目录）。

        为什么需要（2026-09-16 实测）：MCP server 被强杀（会话被 kill、异常退出）时
        atexit 根本不会跑，它起的 dbus-daemon / registryd 就被 systemd 收养成孤儿，
        `/tmp/cc-cu-at-spi-*` 目录也留着 —— **跨会话无人回收**（`_reap_dead_sandbox`
        只管本进程自己的那些）。实测清点到一个：目录 bmf4pzmb + 两个孤儿进程（父进程
        已变成 systemd），而总线上一个使用者都没有。

        为什么可以"放心清本屏号"：本函数只在**本进程刚 spawn 成功 Xephyr** 之后调用
        （见 `_start_at_spi_bus`），此刻本屏上除我们自己的 X server 不可能还有活跃
        沙箱 —— X11 每块屏只允许一个 X server，别人的沙箱若活着，我们的 Xephyr 根本
        起不来。故同屏号的旧目录必是残留。

        ⚠️ **只清本屏号**：跨屏会误伤别的会话正在用的总线（自动分配屏号下各会话各占
        一块屏，扫过去纯属误杀）。
        """
        num = self._display_num()
        if not num:
            return
        pattern = os.path.join(tempfile.gettempdir(), f"{_AT_SPI_DIR_PREFIX}-d{num}-*")
        for d in glob.glob(pattern):
            if d == self._at_spi_dir:
                continue          # 自己（重建场景下由 _reap_dead_sandbox 负责回收）
            pids = self._kill_bus_processes(d)
            shutil.rmtree(d, ignore_errors=True)
            log.info("清理私有总线残留: %s（回收进程 %s）", d, pids or "无")
        self._warn_legacy_buses()

    def _warn_legacy_buses(self) -> None:
        """
        检测**旧格式**的私有总线残留并告警（M-56②）。

        背景：2026-09-16 的改动给目录名加上了屏号（`cc-cu-at-spi-d<屏号>-<随机>`），
        好让 attach 场景能按屏号反查。**改动之前**留下的目录名不带屏号
        （`cc-cu-at-spi-<随机>`）——它们不匹配 `_sweep_stale_buses` 的任何一个前缀，
        于是**永远清不掉**（现场取证到过一组：目录 + 两个父进程已变成 systemd 的孤儿进程，
        而总线上零使用者）。这属于每台机器最多一组的一次性历史残留。

        ⚠️ **刻意不自动删**：旧格式目录没有屏号可依据，无法判断它属于哪块屏，
        直接删可能误伤「还跑着旧版 MCP server 的别的会话」。故只检测 + 告警 + 给出
        手动清理命令（`docs/安装说明.md` 的排错表里也有一条同样的说明）。
        限流：每个进程只告警一次（本方法在每次重建沙箱时都会跑）。
        """
        if self._legacy_bus_warned:
            return
        legacy = [
            d for d in glob.glob(os.path.join(tempfile.gettempdir(), f"{_AT_SPI_DIR_PREFIX}-*"))
            if not re.match(rf"^{re.escape(_AT_SPI_DIR_PREFIX)}-d\d+-", os.path.basename(d))
        ]
        if not legacy:
            return
        self._legacy_bus_warned = True
        log.warning(
            "发现 %d 个**旧格式**的 AT-SPI 总线残留（目录名不含屏号，是 2026-09-16 之前的"
            "版本留下的）：%s\n"
            "    本进程按设计**不会**自动删除它们 —— 旧格式没有屏号可依据，删了可能误伤"
            "还跑着旧版 server 的其它会话。确认无旧版会话在跑时可手动清理：\n"
            "      pkill -f 'cc-cu-at-spi-' ; rm -rf /tmp/cc-cu-at-spi-*",
            len(legacy), ", ".join(legacy))

    @staticmethod
    def _kill_bus_processes(directory: str) -> list[int]:
        """
        终止所有绑定到 `<directory>/bus` 的进程，返回被终止的 pid 列表。

        为什么用**反向扫 /proc** 而不是在目录里记 pid 文件：pid 会被复用、文件可能丢，
        而「cmdline 含该目录路径」/「environ 的 AT_SPI_BUS_ADDRESS 指向该总线」是
        **当下事实** —— 据此匹配不可能误杀别的进程。实测两者各命中一个：dbus-daemon 的
        `--address` 带目录路径，registryd 命令行不含目录、只能靠环境变量认出
        （`/proc/<pid>/environ` 反映的正是**启动时**的环境，用来校验启动参数恰好合适）。

        ⚠️ 只读 /proc，失败（权限/进程刚退出）一律跳过，绝不抛。
        """
        needle_dir = directory.encode()
        needle_env = f"AT_SPI_BUS_ADDRESS=unix:path={directory}/bus".encode()
        self_pid = os.getpid()
        killed: list[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == self_pid:
                continue
            hit = False
            for probe, needle in (("cmdline", needle_dir), ("environ", needle_env)):
                try:
                    with open(f"/proc/{entry}/{probe}", "rb") as fh:
                        hit = needle in fh.read()
                except OSError:
                    continue
                if hit:
                    break
            if not hit:
                continue
            try:
                os.kill(int(entry), signal.SIGTERM)
                killed.append(int(entry))
            except OSError:
                pass
        return killed

    @staticmethod
    def _wait_socket(path: str, timeout: float) -> bool:
        """等 unix socket 文件出现（Xephyr / dbus-daemon 共用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def _kill_at_spi_procs(proc, registry, directory: str | None) -> None:
        """回收私有总线的两个子进程与工作目录（幂等，失败不抛）。"""
        for p in (registry, proc):   # 先 registryd 后 daemon
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                    # M-6：kill 之后**必须** wait()，否则子进程留成僵尸，直到下次创建
                    # Popen 时被 subprocess._cleanup() 顺带回收 —— 与本模块专门治僵尸的
                    # _reap_children 意图正好相悖（那句 docstring 说的就是要 reap）。
                    # 放在同一个 try 里：进程刚好已退出时 wait() 立即返回，真出异常
                    # 也只是进 except，行为与原先一致。
                    p.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        if directory:
            shutil.rmtree(directory, ignore_errors=True)