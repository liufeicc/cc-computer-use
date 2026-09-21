"""
应用枚举（backend.linux.inject.apps）—— **纯 X11 通道，零 AT-SPI 成本**。

⚠️ 本模块存在本身就是一条事故教训：曾经用 AT-SPI 枚举应用（对每个 application 节点
调 `get_child_count()`），而那个调用会**逼目标应用惰性构建整棵无障碍树**。Chrome /
Electron / 微信 / QQ 这类重型应用一旦被这样触发，at-spi2-registryd 与会话 D-Bus 会在
瞬间被数万节点的 IPC 请求打满，**连带拖崩依赖同一条总线的 GNOME Shell**——实测一次
`--selftest` 即导致 gnome-shell SIGSEGV，并连锁崩掉 Chrome / Nexus / aTrust 等一批应用。

而「哪些应用有窗口」本就是 X11 的领域：WM_CLASS 一次调用即可拿到，零 a11y 代价。
**别改回 AT-SPI 版。**
"""

from __future__ import annotations

import shutil
import subprocess

from ....core import display
from .base import log


class AppMixin:
    """按「有可见窗口」枚举应用名（纯 X11）。"""

    # ---------- 应用枚举（纯 X11 通道，零 AT-SPI 成本）----------
    def list_app_names(self) -> list[str]:
        """
        列出「有可见窗口的应用名」（纯 X11，**全程不触碰无障碍树**）。

        ⚠️ 为何不走 AT-SPI 枚举应用（本项目最严重一次事故的教训，改回前务必读完）：
          AT-SPI 版必须对桌面上每个 application 节点调 get_child_count()，而该调用会
          **逼目标应用惰性构建整棵无障碍树**。Chrome / Electron / 微信 / QQ 这类重型
          应用一旦被这样触发，at-spi2-registryd 与会话 D-Bus 会在瞬间被数万节点的
          IPC 请求打满，**连带拖崩依赖同一条总线的 GNOME Shell**——实测一次
          `--selftest`（内部调 AT-SPI list_apps）即导致 gnome-shell SIGSEGV，
          并连锁崩掉 Chrome / Nexus / aTrust 等一批应用。
          而「哪些应用有窗口」本就是 X11 的领域：WM_CLASS 一次调用即可拿到，
          零 a11y 代价、零 D-Bus 压力，也不依赖 AT-SPI 是否可用。

        实现逻辑：
          1. 优先 `wmctrl -lpx` 一次拿到全部窗口的 pid + WM_CLASS。选它而非
             `xdotool search` + 逐窗 xprop：单次进程调用、输出逐行天然对位，
             没有批量 xdotool 那种「窗口在中途销毁即整列错位」的风险。
          2. wmctrl 不可用则退化 _app_names_via_proc（按窗口 pid 读 /proc/<pid>/comm）。
          3. 规范化小写、去重、排序后返回。
        """
        names = self._app_names_via_wmctrl()
        if names is None:
            names = self._app_names_via_proc()
        return sorted({n for n in names if n})

    def _app_names_via_wmctrl(self) -> list[str] | None:
        """
        用 `wmctrl -lpx` 取应用名；wmctrl 缺失/执行失败返回 None（调用方退化）。

        输出行格式（实测确认）：
          <窗口id> <桌面> <pid> <instance.Class> <主机名> <标题……>
          → split(maxsplit=5) 恰好 6 段。字段不足 5 段的行（异常/截断）跳过。

        WM_CLASS 取点号后的 Class 段（如 "nexus.Nexus" → "Nexus"）——Class 比
        instance 更接近应用标识，且多窗口的同应用天然同名，去重后即应用清单。
        """
        wmctrl = shutil.which("wmctrl")
        if not wmctrl:
            return None
        try:
            p = subprocess.run([wmctrl, "-lpx"], capture_output=True, text=True,
                               timeout=5, errors="replace", env=display.env_for())
        except Exception as exc:  # noqa: BLE001
            log.debug("wmctrl -lpx 执行失败: %s", exc)
            return None
        if p.returncode != 0:
            log.debug("wmctrl -lpx 返回码 %d: %s", p.returncode, (p.stderr or "").strip())
            return None
        out: list[str] = []
        for line in p.stdout.splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) < 5:
                continue
            out.append(self._wm_class_to_name(parts[3]))
        return out

    @staticmethod
    def _wm_class_to_name(wm_class: str) -> str:
        """
        从 wmctrl 的 "instance.Class" 复合串里提取应用名（小写）。

        难点：wmctrl 用 '.' 把 WM_CLASS 的两个字符串（instance 与 Class）拼成一个串，
        而当这两者**本身含点**时（Gnome 系应用极常见）无法简单切分——直接
        split('.', 1)[1] 会把 "org.gnome.Nautilus.org.gnome.Nautilus" 切成
        "gnome.Nautilus.org.gnome.Nautilus"，实测踩到过。

        实测样本与期望：
          "nexus.Nexus"                           → "nexus"
          "gjs.Gjs"                               → "gjs"
          "gnome-text-editor.gnome-text-editor"   → "gnome-text-editor"
          "org.gnome.Nautilus.org.gnome.Nautilus" → "org.gnome.nautilus"

        实现逻辑：先试「以中点切两半且两半相同」——这是 instance 与 Class 同名时
        wmctrl 的拼接形态，命中说明整串是 X.X（其中 X 自身可能含点），取前半即可；
        否则退化为常规 instance.Class 形态，取首个点之后的部分（= Class）。
        注意不可加奇偶判断：含点场景的长度奇偶都有（38 与 35 都出现过），
        只用「两半相等」这一个判据。
        """
        n = len(wm_class)
        if n >= 3:
            mid = n // 2
            if wm_class[:mid] == wm_class[mid + 1:]:
                return wm_class[:mid].strip().lower()
        return (wm_class.split(".", 1)[1] if "." in wm_class else wm_class).strip().lower()

    def _app_names_via_proc(self) -> list[str]:
        """
        wmctrl 缺失时的退化路径：按可见窗口的 pid 读 /proc/<pid>/comm。

        局限：comm 是内核线程名（截断到 15 字符），对 java/python 打包的应用
        （如 DBeaver 的 java 进程）只能给出 "java"，辨识度明显不如 WM_CLASS——
        仅作兜底，不追求与主路径等价。同样不碰 a11y。
        """
        names: list[str] = []
        try:
            for w in self.list_windows():
                pid = w.get("pid")
                if not pid:
                    continue
                try:
                    with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                        names.append(f.read().strip().lower())
                except OSError:
                    continue
        except Exception as exc:  # noqa: BLE001
            log.debug("退化取进程名失败: %s", exc)
        return names