"""
Xephyr 沙箱的生命周期（core.display.lifecycle）。

承载 `DisplayManager` 的**身份与生死**：模式解析结果、屏号、socket 判定、启动/重建/
回收、以及启动期对宿主焦点的一次性补偿。

本 mixin 依赖宿主类（`manager.DisplayManager`）提供的实例字段：
    _mode / _sandbox_display / _auto_display / _screen / _wait_user_timeout
    _proc / _wm_proc / _wm_dir / _at_spi_proc / _at_spi_registry / _at_spi_dir / _at_spi_bus
    _attached / _lock / _fallback_warned / _start_attempts / _ever_up
    _max_attempts_cold / _max_attempts_rebuild / _atexit_registered / _host_rect_cache
（这些字段集中在 `manager.DisplayManager.__init__` 里声明，便于一处看清全部状态。）

它还会跨 mixin 调用：`_ensure_at_spi_bus` / `_start_at_spi_bus`（at_spi_bus）、
`_start_sandbox_wm` / `_cleanup_wm_dir`（wm）、`_restore_host_focus`（本文件）、
`env_for`（env）—— mixin 之间用 `self.` 互调是刻意的：这些逻辑共享同一份实例状态，
拆成独立对象只会把状态搬来搬去。
"""

from __future__ import annotations

import atexit
import os
import select
import shutil
import subprocess
import threading
import time

from .constants import (
    DEFAULT_SANDBOX_SCREEN,
    DEFAULT_WAIT_USER,
    ENV_MODE,
    ENV_SANDBOX_DISPLAY,
    ENV_SANDBOX_SCREEN,
    ENV_SANDBOX_WAIT_USER,
    MODE_ISOLATED,
    MODE_REAL,
    _DISPLAYFD_TIMEOUT,
    _FALLBACK_DISPLAY_FROM,
    _FALLBACK_DISPLAY_TRIES,
    _START_TIMEOUT,
    log,
)


class LifecycleMixin:
    """解析模式、管理 Xephyr 沙箱生命周期。"""

    # ---------- 初始化 ----------
    def _init_lifecycle(self) -> None:
        """
        初始化生命周期相关字段（由 `DisplayManager.__init__` 调用）。

        单独一个方法而不是直接写进 `__init__` 的函数体：让每个 mixin 自己管自己的
        状态，`__init__` 只负责按顺序调用各个 `_init_*`，避免一个巨型构造函数。
        """
        raw_mode = os.environ.get(ENV_MODE, MODE_ISOLATED).strip().lower()
        if raw_mode not in (MODE_ISOLATED, MODE_REAL):
            log.warning("未知 %s=%r，回落 %s", ENV_MODE, raw_mode, MODE_ISOLATED)
            raw_mode = MODE_ISOLATED
        self._mode = raw_mode
        # 显式设了 CC_CU_SANDBOX_DISPLAY → 用固定屏号，且「同屏号已存在则 attach」，
        # 便于用户手动让多个会话来共用一块屏（或接外部沙箱）。
        # 没设 → 自动分配（None 表示"启动时才确定"），每会话一块私有屏。
        raw_display = (os.environ.get(ENV_SANDBOX_DISPLAY) or "").strip()
        self._sandbox_display: str | None = raw_display or None
        self._auto_display = not raw_display
        self._screen = os.environ.get(
            ENV_SANDBOX_SCREEN, DEFAULT_SANDBOX_SCREEN).strip() or DEFAULT_SANDBOX_SCREEN
        try:
            self._wait_user_timeout = float(os.environ.get(ENV_SANDBOX_WAIT_USER,
                                                           DEFAULT_WAIT_USER))
        except ValueError:
            self._wait_user_timeout = DEFAULT_WAIT_USER

        self._proc: subprocess.Popen | None = None  # 自启的 Xephyr 进程
        self._attached = False                      # True=外部沙箱，退出时不杀
        # 启动/停止/回收共用的一把锁：保证并发首个调用串行等待（见 start() 的说明）
        self._lock = threading.Lock()
        self._fallback_warned = False
        # 启动尝试计数与「是否成功过」。
        #   - 计的是**连续失败次数**：启动成功即清零（见 _start_locked 的成功分支），
        #     否则它会退化成「本进程终生最多起几次沙箱」，屏多回收两次就永久锁死。
        #   - 从未成功过（如未装 Xephyr）：只试一次，重试只会让每次工具调用都白等超时。
        #   - 曾成功过但屏后来消失（Xephyr 崩了 / 外部沙箱被回收）：允许有限次重建，
        #     这是 B 防线想要的行为——自动恢复比直接报错好，报错只是最后手段。
        self._start_attempts = 0
        self._ever_up = False
        self._max_attempts_cold = 1     # 冷启动（从未成功）尝试上限
        self._max_attempts_rebuild = 3  # 曾成功后允许的重建尝试上限
        self._atexit_registered = False  # stop() 只需注册一次（重建会让 start 多次走到）

    # ---------- 基本信息 ----------
    @property
    def mode(self) -> str:
        return self._mode

    @property
    def sandbox_display(self) -> str:
        """虚拟屏号；自动模式下**启动前**为 ""（尚未分配）。"""
        return self._sandbox_display or ""

    def host_display(self) -> str:
        """宿主真实会话的 DISPLAY（Xephyr 自身与宿主侧只读查询都用它）。"""
        return os.environ.get("DISPLAY") or ":0"

    def _display_num(self) -> str:
        """:0 → "0"；:99 → "99"；屏号未分配 → ""（空串代表"还不知道是哪块屏"）。

        用于把屏号编码进沙箱侧的资源名（X socket 路径、私有总线目录名），
        使**别的进程**能按屏号反查这块屏上沙箱的资源 —— attach 场景全靠它。
        """
        if not self._sandbox_display:
            return ""
        return self._sandbox_display.lstrip(":").split(".")[0]

    def _socket_path(self) -> str:
        """:99 → /tmp/.X11-unix/X99（X server 就绪的标志）。

        自动模式启动前屏号未知 → 返回一个**必然不存在**的路径，使 is_sandbox_up()
        在分配前恒为 False（否则会误判成"已就绪"）。
        """
        num = self._display_num()
        if not num:
            return "/nonexistent/cc-cu-display-unassigned"
        return f"/tmp/.X11-unix/X{num}"

    def _reap_children(self) -> None:
        """
        对所有自管子进程做一次非阻塞 wait（`poll()` 顺带回收僵尸）。

        为什么要专门做：这些 Popen 原本只在 `stop()` 里被 `wait()`，而 stop() 只挂在
        显式调用与 atexit 上 —— 用户直接关掉沙箱窗口时不会触发它，于是 Xephyr / i3 /
        总线进程退出后会长期以僵尸态占着 PID 表项（2026-09-15 清点现场时看到 4 个 Z）。
        poll() 廉价、无副作用，可以放心挂在只读路径上。
        """
        for p in (self._proc, self._wm_proc, self._at_spi_proc, self._at_spi_registry):
            if p is not None:
                p.poll()

    def is_sandbox_up(self) -> bool:
        """沙箱是否就绪：socket 存在即视为可用（自启或外部 attach 都算）。"""
        self._reap_children()
        if self._mode != MODE_ISOLATED:
            return False
        if not os.path.exists(self._socket_path()):
            return False
        # 自启进程已死但 socket 还在（僵尸 Xephyr 残留 socket 极少见）→ 以进程为准
        if self._proc is not None and self._proc.poll() is not None:
            return False
        return True

    # ---------- 生命周期 ----------
    def start(self) -> dict:
        """
        显式启动沙箱（幂等）。isolated 模式：socket 已存在则 attach 外部沙箱；
        否则 spawn Xephyr 并等待 socket；失败仅告警（effective_display 会回落宿主）。
        real 模式：no-op。返回 describe() 便于 selftest 打印。

        持 _lock 覆盖**整个启动过程**是并发语义的一部分：MCP 工具的同步函数由 anyio
        线程池并发执行，首次使用若同时来两个调用，后者在此阻塞等前者启完，而不是看到
        「沙箱还没起来」就回落宿主桌面——那会把注入打到用户真实桌面上。
        """
        with self._lock:
            return self._start_locked()

    def ensure_started(self) -> dict:
        """
        惰性启动：**首次真正用到本 MCP 时**才拉虚拟屏（server 启动路径不再调用它）。

        背景（用户实测反馈）：MCP server 是 Claude Code 在**会话启动时**就常驻拉起的
        stdio 进程。原先 server.main() 在 stdio 循环前无条件 start()，于是每次开
        Claude 都会弹出一个虚拟屏窗口——哪怕整场会话一次都没用过本工具。现在把启动点
        从「进程启动」推迟到「首次工具调用」：Coordinator 的对外方法都经
        coordinator._needs_display 装饰器调到这里。

        为什么钩在 Coordinator 而不是 tools/ 的各工具函数：tools/ 只做参数解析、全部
        收敛到 Coordinator 的对外方法，故那里是**唯一不会漏**的入口——新增工具若忘了
        挂钩，注入会静默回落宿主桌面，那是本项目最不能出的错。
        """
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> dict:
        """
        真正的启动逻辑；**调用方必须已持有 _lock**（保证并发首个调用串行等待）。

        实现逻辑：
          1. real 模式 → no-op（用户显式要求操作真实桌面）。
          2. 尝试次数用尽 → 直接返回现状。冷启动（从未成功）只试一次：未装 Xephyr 之类
             原因不会自行恢复，重试只会让每次工具调用都白等超时。曾成功过则允许有限次
             重建——这是 B 防线要的「自动恢复优先，报错只是最后手段」。
          3. 固定屏号（用户显式设了 CC_CU_SANDBOX_DISPLAY）且 socket 已存在 → attach
             外部沙箱、退出不回收。这是「让多个会话共用一块屏」的唯一入口。
          4. 否则 spawn 自己的 Xephyr（自动模式下由 X server 挑空闲屏号）。
        """
        if self._mode == MODE_REAL:
            log.info("display 模式=real，不启沙箱，直接操作宿主桌面")
            return self.describe()

        # ⚠️ 必须先判「已经在跑」：下面的重建逻辑会放宽尝试次数上限，若少了这一步，
        # 每次工具调用（每次都会进 ensure_started）都会再 spawn 一个 Xephyr，
        # 而 stop() 只回收 _proc 指向的那一个，多余的会变成孤儿 X server 长期驻留。
        if self.is_sandbox_up():
            # 沙箱是**别人起的**时（多会话共用一块屏，或本进程自起的沙箱已死而屏上仍有
            # X server），我们没跑过 _start_at_spi_bus、_at_spi_bus 还是 None —— 必须在
            # 这里补一次反查。⚠️ 这条才是「多会话共用一块屏」的主路径：对方起的 Xephyr
            # 让 socket 存在、本方 _proc 为 None 又让 is_sandbox_up() 直接返回 True，
            # 于是根本进不了下面那个 attach 分支（只在那里修等于没修）。
            self._ensure_at_spi_bus()
            return self.describe()

        # 走到这里 = 沙箱不在跑（没起过，或起过但已死）。后者会留下上一轮的残留：用户直接
        # 关掉 Xephyr 窗口时 stop() 不会被调用，而下面的重建会**覆盖** _proc / _at_spi_proc /
        # _wm_proc 三个 Popen 字段 —— 一旦覆盖就再没人回收它们。先清干净再起新的，
        # 否则每次重建都泄漏一组孤儿进程 + 一个 /tmp/cc-cu-at-spi-* 目录。
        self._reap_dead_sandbox()

        limit = self._max_attempts_rebuild if self._ever_up else self._max_attempts_cold
        if self._start_attempts >= limit:
            return self.describe()
        self._start_attempts += 1

        if not self._auto_display and os.path.exists(self._socket_path()):
            self._attached = True
            self._ever_up = True
            self._start_attempts = 0   # 见下方 _spawn_xephyr 成功分支的说明
            # 找回这块屏所属沙箱的**私有 AT-SPI 总线**：attach 时本进程既没起 Xephyr、
            # 也没起总线，_at_spi_bus 无从得知，只能按屏号反查（总线目录名里带屏号）。
            # 反查不到（例如旧版本留下的无屏号目录）就保持 None → 仍旧死地址兜底，安全。
            self._ensure_at_spi_bus()
            log.info("检测到同屏号沙箱 %s，attach（退出时不回收）；私有 AT-SPI 总线=%s",
                     self._sandbox_display,
                     self._at_spi_bus or "(未找到 → a11y 不可用，宿主仍安全)")
            return self.describe()

        host_focus_before = self._host_active_wid()  # 记录宿主焦点，沙箱窗抢走后还原
        xephyr = shutil.which("Xephyr")
        if not xephyr:
            log.error("未安装 Xephyr（sudo apt install xserver-xephyr），沙箱不可用；"
                      "注入将被拒绝，不会静默落到宿主桌面。确要直接操作真实桌面请设 "
                      "CC_CU_DISPLAY_MODE=real")
            return self.describe()
        if self._spawn_xephyr(xephyr):
            if not self._atexit_registered:
                atexit.register(self.stop)
                self._atexit_registered = True
            self._ever_up = True
            # 私有 AT-SPI 总线：a11y 隔离的正解（失败则 app_env 回退死地址兜底）
            self._start_at_spi_bus()
            # ️ 成功即清零：_start_attempts 计的是**连续失败次数**，不是终生尝试次数。
            # 少了这一句，_max_attempts_rebuild 限的就变成「这个 server 进程一辈子
            # 最多起几次沙箱」——实测踩到过：屏被回收两次后配额用尽，此后即便每次
            # 重建都成功，也永久拒绝启动，所有注入类工具全部报「沙箱不可用」，
            # 而 Xephyr 本身手动启动完全正常，排查极易跑偏到「Xephyr 坏了」。
            self._start_attempts = 0
            self._start_sandbox_wm()
            self._restore_host_focus(host_focus_before)
        return self.describe()

    # ---------- spawn 细节 ----------
    def _sandbox_title(self) -> str:
        """
        沙箱窗口标题。带 MCP server 的 pid：每个 Claude 会话一个 server 进程，
        用户据此能一眼分清桌面上多块虚拟屏分别属于哪个会话。
        """
        return f"Claude Sandbox (mcp {os.getpid()})"

    @staticmethod
    def _socket_path_of(display: str) -> str:
        """:99 → /tmp/.X11-unix/X99。"""
        num = display.lstrip(":").split(".")[0]
        return f"/tmp/.X11-unix/X{num}"

    def _spawn_xephyr(self, xephyr: str) -> bool:
        """
        spawn 一个 Xephyr 并等到可用，成功时 self._proc / self._sandbox_display 已就位。

        自动屏号（默认）走 `-displayfd`：把管道写端交给 X server，它自己挑一个空闲屏号
        并**在就绪时**回写号码。为什么不用「我们自己扫 /tmp/.X11-unix 挑空闲号」：
        扫描与绑定之间有竞态窗口，两个会话可能同时选中同一个号（实测过：两边都 spawn，
        输的那个 Xephyr rc=1 退出）。交给 X server 分配则零竞态，且回写时机即就绪信号。

        极老版本 Xephyr 不认 -displayfd（管道读到空）→ 退化为从 :99 起逐个试。
        """
        if not self._auto_display:
            return self._spawn_at(xephyr, self._sandbox_display)

        rfd, wfd = os.pipe()
        try:
            try:
                self._proc = subprocess.Popen(
                    [xephyr, "-displayfd", str(wfd), "-screen", self._screen,
                     "-title", self._sandbox_title()],
                    pass_fds=(wfd,),  # 管道写端必须留给子进程（-displayfd 靠它回写）
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    # Xephyr 自己是个 X 客户端，要连**宿主**屏才能嵌套出虚拟屏，故显式传
                    # host_display()（不能默认 effective_display：此刻沙箱屏号还没定，且它
                    # 本就不该连自己要创建的那块屏）。走 env_for 而非裸 dict(os.environ)：
                    # 顺带剥掉产物内的 LD_LIBRARY_PATH 与 AT_SPI_BUS_ADDRESS。
                    env=self.env_for(self.host_display()),
                )
            except OSError as exc:
                log.error("spawn Xephyr 失败: %s", exc)
                self._proc = None
                return False
            finally:
                os.close(wfd)  # 父进程必须关掉写端，否则读端永远等不到 EOF
            num = self._read_displayfd(rfd)
        finally:
            # M-1：读端也必须在 finally 里关。历史实现把 `os.close(rfd)` 放在读取之后，
            # 于是「Popen 抛 OSError 直接 return」与「_read_displayfd 抛异常」两条路都会
            # **泄漏一个 fd**（每次失败一个；尝试次数有上限，故不致命，但没有理由留着）。
            os.close(rfd)
        if num is None:
            log.warning("Xephyr 未回写屏号（可能不支持 -displayfd 或启动即失败），"
                        "退化为扫描空闲屏号")
            self._kill_own_proc()
            return self._spawn_by_scan(xephyr)
        self._sandbox_display = f":{num}"
        log.info("沙箱就绪: %s (%s，屏号由 X server 自动分配)",
                 self._sandbox_display, self._screen)
        return True

    @staticmethod
    def _read_displayfd(rfd: int, timeout: float = _DISPLAYFD_TIMEOUT) -> str | None:
        """
        读 X server 回写的屏号（形如 "99\\n"）。返回数字串；超时/读到 EOF/非数字 → None。

        用 select 加超时：os.read 会阻塞，若 Xephyr 起不来又没关管道，直接读会挂死
        整个启动流程（而启动是持 display 锁的，等于把 MCP 卡住）。
        """
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            r, _, _ = select.select([rfd], [], [], max(0.05, remaining))
            if not r:
                continue
            chunk = os.read(rfd, 64)
            if not chunk:
                break  # 写端已关：Xephyr 退出了
            buf += chunk
            if b"\n" in buf:
                break
        num = buf.decode(errors="replace").strip()
        return num if num.isdigit() else None

    def _spawn_at(self, xephyr: str, display: str) -> bool:
        """在指定屏号 spawn Xephyr 并等 socket 出现（固定屏号路径）。返回是否成功。"""
        cmd = [xephyr, display, "-screen", self._screen, "-title", self._sandbox_title()]
        log.info("启动沙箱: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self.env_for(self.host_display()),  # 同 _spawn_xephyr：连宿主屏
            )
        except OSError as exc:
            log.error("spawn Xephyr 失败: %s", exc)
            self._proc = None
            return False
        self._sandbox_display = display
        sock = self._socket_path()
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                log.error("Xephyr 启动即退出(rc=%s)，屏 %s 不可用（多半已被占用）",
                          self._proc.returncode, display)
                self._proc = None
                return False
            if os.path.exists(sock):
                log.info("沙箱就绪: %s (%s)", display, self._screen)
                return True
            time.sleep(0.2)
        log.error("等待 Xephyr socket 超时(%ss)，屏 %s 不可用", _START_TIMEOUT, display)
        return False

    def _spawn_by_scan(self, xephyr: str) -> bool:
        """兜底路径：从 _FALLBACK_DISPLAY_FROM 起逐个试空闲屏号（跳过已有 socket 的）。"""
        for i in range(_FALLBACK_DISPLAY_TRIES):
            cand = f":{_FALLBACK_DISPLAY_FROM + i}"
            if os.path.exists(self._socket_path_of(cand)):
                continue
            if self._spawn_at(xephyr, cand):
                self._sandbox_display = cand
                return True
        log.error("扫描 %d 个屏号均不可用，沙箱启动失败",
                  _FALLBACK_DISPLAY_TRIES)
        return False

    def _kill_own_proc(self) -> None:
        """杀掉本次 spawn 的（失败的）Xephyr，避免残留。"""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    def _host_active_wid(self) -> str | None:
        """宿主当前活动窗口 id（只读）。"""
        try:
            return self._run_host(["getactivewindow"]).stdout.strip() or None
        except Exception:  # noqa: BLE001
            return None

    def _restore_host_focus(self, prev_wid: str | None) -> None:
        """
        沙箱窗（Xephyr）出现在宿主桌面时可能被宿主 WM 聚焦——把焦点还原回用户原窗口。

        克制原则：仅当「当前宿主活动窗 == Xephyr 窗」时才做一次 windowactivate；
        其它情况（用户自己切了窗等）一律不碰宿主。这是启动期一次性补偿，
        不是持续干预。
        """
        if not prev_wid:
            return
        cur = self._host_active_wid()
        if not cur or cur == prev_wid:
            return
        xephyr_wids = []
        try:
            xephyr_wids = self._run_host(
                ["search", "--pid", str(self._proc.pid)]).stdout.split()
        except Exception:  # noqa: BLE001
            return
        if cur in xephyr_wids:
            try:
                self._run_host(["windowactivate", "--sync", prev_wid])
                log.info("沙箱窗抢走宿主焦点，已还原到原窗口 %s", prev_wid)
            except Exception:  # noqa: BLE001
                pass

    def _reap_dead_sandbox(self) -> None:
        """
        回收「上一轮沙箱已死、残留却没清」的状态（幂等；无残留时是 no-op）。

        触发场景：用户直接关掉 Xephyr 窗口，或 Xephyr 自己崩了 —— 此时 `stop()` 不会被
        调用（它只挂在显式调用与 atexit 上）。若直接在 `_start_locked` 里重建，三个 Popen
        字段会被新进程**覆盖**，旧的就永远没人回收，每次重建泄漏：
          1. 私有总线的 dbus-daemon + registryd 两个孤儿进程；
          2. 它们的工作目录 /tmp/cc-cu-at-spi-*（私有总线 socket 就在里面）；
          3. Xephyr / i3 与上述进程的僵尸表项。
        本方法在重建**之前**把这些一次清干净。

        **只回收自己起的**：attach 的外部沙箱（用户显式指定固定屏号的场景）及其总线不归
        本进程管，与 stop() 里的 attached 判断保持一致。
        """
        # 先摘下字段引用（无论走哪个分支，自己的状态都要复位）
        proc, self._proc = self._proc, None
        wm, self._wm_proc = self._wm_proc, None
        at_spi, registry = self._at_spi_proc, self._at_spi_registry
        d, self._at_spi_dir = self._at_spi_dir, None
        self._at_spi_proc = self._at_spi_registry = self._at_spi_bus = None
        self._host_rect_cache = None            # 上一轮的窗口矩形对新沙箱无意义
        self._cleanup_wm_dir()
        if self._attached:
            # 外部沙箱：不碰它的进程与总线，只复位标志（下次经 _start_locked 重新判定）
            self._attached = False
            return
        self._kill_at_spi_procs(at_spi, registry, d)
        for p in (wm, proc):  # 先 WM 后 Xephyr（与 stop() 同序）
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=5)  # 已经死掉的进程在这里会立即返回并 reap 僵尸
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

    def stop(self) -> None:
        """终止自启的 Xephyr 与沙箱 WM（外部 attach 的不碰）。atexit 与显式调用共用，幂等。"""
        with self._lock:
            proc, self._proc = self._proc, None
            wm, self._wm_proc = self._wm_proc, None
            # 私有 AT-SPI 总线一并回收（含工作目录），否则每次重建都留一对孤儿进程 + 一个 socket
            at_spi, registry = self._at_spi_proc, self._at_spi_registry
            d, self._at_spi_dir = self._at_spi_dir, None
            self._at_spi_proc = self._at_spi_registry = self._at_spi_bus = None
            self._host_rect_cache = None
            self._cleanup_wm_dir()
            attached = self._attached        # 先记住：下面会被重置
            # 显式 stop 之后允许重新 start()（否则尝试次数用尽会让重启变成 no-op）。
            # 注意 _ever_up 刻意保留：重建限额用的是"曾成功后"的那档。
            # atexit 路径也会走到这里，但那时进程已要退出，无副作用。
            self._start_attempts = 0
            self._attached = False
        for p in (wm, proc):  # 先 WM 后 Xephyr
            if p is None or attached:
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
        # attach 外部沙箱时不回收（那块屏与总线都不属于本进程）
        if not attached:
            self._kill_at_spi_procs(at_spi, registry, d)