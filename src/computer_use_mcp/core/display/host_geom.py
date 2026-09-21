"""
宿主侧的几何查询与注入礼让（core.display.host_geom）。

用户随时可以把键鼠伸进 Xephyr 窗口亲自操作（Xephyr 默认把宿主输入路由进嵌套屏）。
此时注入必须**礼让**：`wait_until_user_leaves` 轮询等待用户离开，超时后由调用方
决定是否带警告继续。

判定「用户是否在沙箱内」= 宿主指针坐标 ∈ Xephyr 窗口在宿主桌面上的矩形。两者都在
**宿主** display 上查询 —— 这里只用 xdotool 的只读子命令，绝不注入。
"""

from __future__ import annotations

import shutil
import subprocess
import time

from .constants import _HOST_RECT_TTL, _USER_WAIT_POLL, log


class HostGeomMixin:
    """判定用户是否在沙箱内、并在注入前礼让。"""

    def _init_host_geom(self) -> None:
        # sandbox_window_rect() 的短 TTL 缓存（M-9）：礼让轮询每轮都要它，而 Xephyr 窗口
        # 基本不动 —— 不缓存的话，30s 礼让里能白白起上百个 xdotool 子进程，且全程持屏锁。
        self._host_rect_cache: tuple[float, tuple[int, int, int, int] | None] | None = None

    def _run_host(self, args: list[str]) -> subprocess.CompletedProcess:
        """在**宿主** display 上跑一条 xdotool（只读查询；注入不走这里）。"""
        xdotool = shutil.which("xdotool")
        if not xdotool:
            raise FileNotFoundError("xdotool 未安装")
        return subprocess.run([xdotool, *args], capture_output=True, text=True,
                              errors="replace",  # 窗口标题可为非 UTF-8 字节，勿严格解码
                              timeout=5, env=self.env_for(self.host_display()))

    def sandbox_window_rect(self) -> tuple[int, int, int, int] | None:
        """
        Xephyr 窗口在**宿主**桌面上的矩形 (x,y,w,h)（用户入口的边界）。

        实现逻辑：按自启进程 PID 在宿主 display 上 search 窗口，取面积最大者
        （与 inject.window_screen_pos_by_pid 同策略：过滤辅助小窗）。
        外部 attach（无 PID）或查询失败返回 None。

        ⚠️ 结果带 `_HOST_RECT_TTL` 秒的短缓存（M-9）：本方法是**轮询**路径上的常客
        （`user_inside_sandbox` 每轮一次，而礼让最长 30s），而每次要起
        `search --pid` + 每窗 `getwindowgeometry` 一串子进程。Xephyr 窗口在会话中
        基本不动（用户挪它是极少数），2 秒的陈旧窗口换掉上百次进程创建是划算的。
        缓存只存**成功**结果：拿不到就每轮重试，免得一次偶然失败把整段礼让判成
        「用户不在沙箱里」而放行注入。
        """
        if self._proc is None or self._proc.poll() is not None:
            return None
        cached = self._host_rect_cache
        if cached is not None and (time.monotonic() - cached[0]) < _HOST_RECT_TTL:
            return cached[1]
        rect = self._query_host_window_rect()
        if rect is not None:
            self._host_rect_cache = (time.monotonic(), rect)
        return rect

    def _query_host_window_rect(self) -> tuple[int, int, int, int] | None:
        """真正去宿主 X 上查 Xephyr 窗口矩形（无缓存；见 sandbox_window_rect）。"""
        try:
            wids = self._run_host(["search", "--pid", str(self._proc.pid)]).stdout.split()
        except Exception:  # noqa: BLE001
            return None
        best: tuple[int, int, int, int] | None = None
        best_area = 0
        for wid in wids:
            try:
                p = self._run_host(["getwindowgeometry", "--shell", wid])
            except Exception:  # noqa: BLE001
                continue
            geom: dict[str, int] = {}
            for line in p.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k in ("X", "Y", "WIDTH", "HEIGHT"):
                        try:
                            geom[k] = int(v)
                        except ValueError:
                            pass
            rect = (geom.get("X", 0), geom.get("Y", 0),
                    geom.get("WIDTH", 0), geom.get("HEIGHT", 0))
            area = rect[2] * rect[3]
            if area > best_area:
                best_area = area
                best = rect
        return best

    @staticmethod
    def point_in_rect(px: int, py: int, rect: tuple[int, int, int, int]) -> bool:
        """纯几何判断：点是否落在矩形内（单测直接覆盖）。"""
        x, y, w, h = rect
        return x <= px < x + w and y <= py < y + h

    def user_inside_sandbox(self) -> bool:
        """
        用户此刻是否把指针伸进了沙箱窗口（宿主指针坐标 ∈ Xephyr 窗口矩形）。

        real 模式/沙箱未就绪/查询失败一律 False（不误伤注入）。
        """
        if not self.is_sandbox_up():
            return False
        rect = self.sandbox_window_rect()
        if rect is None:
            return False
        try:
            p = self._run_host(["getmouselocation", "--shell"])
        except Exception:  # noqa: BLE001
            return False
        vals: dict[str, int] = {}
        for line in p.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in ("X", "Y"):
                    try:
                        vals[k] = int(v)
                    except ValueError:
                        pass
        if "X" not in vals or "Y" not in vals:
            return False
        return self.point_in_rect(vals["X"], vals["Y"], rect)

    def wait_until_user_leaves(self, timeout: float | None = None,
                               poll: float = _USER_WAIT_POLL) -> bool:
        """
        注入前礼让：用户在沙箱内则轮询等待其离开。

        返回 True=可以安全注入（用户不在/已离开/无需礼让）；False=等到超时用户仍在
        （调用方自行决定是否带警告继续）。real 模式恒 True。
        """
        limit = self._wait_user_timeout if timeout is None else timeout
        if not self.user_inside_sandbox():
            return True
        log.warning("用户正在沙箱内操作，注入礼让等待(≤%ss)", limit)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            time.sleep(poll)
            if not self.user_inside_sandbox():
                log.info("用户已离开沙箱，恢复注入")
                return True
        return False