"""
沙箱内的窗口管理器（core.display.wm）。

Xephyr 只提供一块**裸 X 屏**：没有 WM 时无人响应 `_NET_ACTIVE_WINDOW`，
`windowactivate` / `getactivewindow` 全部失效，键盘注入找不到焦点窗口 —— 而坐标级
点击的聚焦链与 type_text/press_key 的兜底都依赖焦点。故沙箱内必须起一个最小 WM。

用 i3 + `-c <私有临时目录>/i3.config`：**绝不碰用户真实的 i3 配置**。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from .constants import ENV_SANDBOX_WM, _SANDBOX_I3_CONFIG, log


class WmMixin:
    """在沙箱内起/收一个最小 WM（i3）。"""

    def _init_wm(self) -> None:
        self._wm_proc: subprocess.Popen | None = None  # 沙箱内 WM（i3）
        # 沙箱 WM 的私有配置目录（M-3：mkdtemp 出来的，随沙箱一起回收；不用 /tmp 固定路径）
        self._wm_dir: str | None = None

    def _start_sandbox_wm(self) -> None:
        """
        沙箱内起一个最小 WM（i3 -c 临时配置，CC_CU_SANDBOX_WM=none 可关）。

        为什么必须：Xephyr 只提供裸 X 屏——没有 WM 时无人响应 _NET_ACTIVE_WINDOW，
        windowactivate/getactivewindow 全部失效，键盘注入找不到焦点窗口（坐标级点击的
        聚焦链与 type_text/press_key 兜底都依赖焦点）。本机仅装 i3，故用
        `i3 -c <私有临时目录>/i3.config` 起（-c 指向临时配置，**绝不碰用户真实
        i3 配置**）；i3 缺失仅告警（元素级主路径不受影响）。

        ⚠️ 配置写在 `tempfile.mkdtemp()` 出来的**私有目录**里，不用固定路径（M-3）：
        /tmp 是 1777 共享目录，固定文件名 + `open(..., "w")` 意味着同机其他用户可以
        预置一个符号链接，让这次写入截断受害者任意可写的文件（经典的 /tmp 符号链接攻击）；
        而且那个文件从来没有被清理过。私有目录同时解决了这两点（随沙箱一起回收）。
        """
        if os.environ.get(ENV_SANDBOX_WM, "auto").strip().lower() == "none":
            log.info("CC_CU_SANDBOX_WM=none，沙箱内不起 WM")
            return
        i3 = shutil.which("i3")
        if not i3:
            log.warning("沙箱内无可用 WM（未安装 i3）：焦点/键盘注入可能失效，"
                        "元素级操作不受影响")
            return
        try:
            self._cleanup_wm_dir()          # 重建场景：先收掉上一轮的配置目录
            wm_dir = tempfile.mkdtemp(prefix="cc-cu-wm-")
            cfg = os.path.join(wm_dir, "i3.config")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(_SANDBOX_I3_CONFIG)
        except OSError as exc:
            log.warning("写沙箱 i3 配置失败: %s", exc)
            return
        self._wm_dir = wm_dir
        try:
            # M-4：其余 spawn 点都有 OSError 保护，这里原先没有 —— exec 失败会穿透
            # _start_locked → ensure_started → _needs_display 直达工具层，违反
            # `_needs_display` docstring 承诺的「ensure_started 自身只告警不抛」。
            self._wm_proc = subprocess.Popen(
                [i3, "-c", cfg], env=self.env_for(self._sandbox_display),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("沙箱 WM 启动失败（i3 spawn 出错）：%s；焦点/键盘注入可能失效，"
                        "元素级操作不受影响", exc)
            self._wm_proc = None
            # 配置目录也别留下：spawn 都没成功，那个目录再没有任何用处，
            # 而重建路径（_reap_dead_sandbox / stop）都只在"有 WM 进程"时才会走到
            # 「回收上一轮目录」的逻辑 —— 不在这里收，它就会一直躺在 /tmp 里。
            self._cleanup_wm_dir()
            return
        log.info("沙箱 WM 已启动: i3 pid=%s", self._wm_proc.pid)

    def _cleanup_wm_dir(self) -> None:
        """回收沙箱 WM 的私有配置目录（幂等）。"""
        d, self._wm_dir = self._wm_dir, None
        if d:
            shutil.rmtree(d, ignore_errors=True)