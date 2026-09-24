"""
窗口查询（backend.linux.inject.windows）—— 活动窗、几何、清单、标题、等待。

本模块是「按 PID/标题精确锁窗、取面积最大者」这套策略的实现处（复用
demo/calib_click.py、action_vs_coord.py 已验证的逻辑）：面积最大者天然排除了
1x1 隐形辅助窗与 WM 框架窗。

⚠️ 本文件里两处「看起来可以合并成一次批量调用」的写法**都是刻意逐个来的**
（`_read_titles` / `_windows_via_xdotool`）—— 本机 xdotool 3.20160805.1 的
getwindowname / getwindowpid / getwindowgeometry **全是单窗口命令**，多传 id 会被
当成命令名。合并回去会得到**静默错答**，详见各自 docstring。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time

from ....core import display
from ...base import Rect
from .base import log


class WindowMixin:
    """窗口枚举、几何查询与等待。"""

    # ---------- 活动窗口 ----------
    def active_window_id(self) -> str | None:
        p = self._run(["getactivewindow"])
        wid = p.stdout.strip()
        return wid or None

    def active_window_title(self) -> str | None:
        p = self._run(["getactivewindow", "getwindowname"])
        return p.stdout.strip() or None

    def active_window_pid(self) -> int | None:
        p = self._run(["getactivewindow", "getwindowpid"])
        try:
            return int(p.stdout.strip())
        except (ValueError, AttributeError):
            return None

    def active_window_class(self) -> str | None:
        """
        活动窗口的 WM_CLASS，归一成小写应用名（如 'gnome-terminal'、'xterm'）；读不到返回 None。

        用途：剪贴板粘贴要据此决定发 `ctrl+v` 还是 `ctrl+shift+v` —— 终端仿真器里
        `Ctrl+V` 不是粘贴（见 keyboard.py 的 `_paste_combo`）。标题与 pid 都替代不了它：
        标题是任意字节串且随内容变（终端标题里带着当前目录），pid 还要再解析 WM_CLASS。

        ⚠️ **不能走 xdotool**（2026-09-24 实测）：本机 xdotool 3.20160805.1 的命令表里
        **没有 `getwindowclassname`**（`xdotool help` 列出的只有 getactivewindow /
        getwindowfocus / getwindowname / getwindowpid / getwindowgeometry），调它只会得到
        `Unknown command` + 空 stdout。所以改用 **wmctrl**（本项目已随包分发、apps.py 也
        在用它读 WM_CLASS）：先 `xdotool getactivewindow` 拿活动窗口 id，再到
        `wmctrl -lpx` 的行里按 id 找它的 `instance.Class`。
        **id 要用 int() 比而不是字符串比**：xdotool 给十进制（12582918）、wmctrl 给
        零填充十六进制（0x00c00006），直接比字符串永远不相等（且不报错）。

        本方法自身**不抛**：读不到就返回 None，调用方据此退回默认行为。这条很重要——
        它只是"帮个忙"的优化，绝不允许把原本能用的输入弄坏。
        """
        wid = self.active_window_id()
        if not wid:
            return None
        try:
            want = int(wid.strip(), 0)
        except ValueError:
            return None
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
            return None
        # 行格式：<窗口id> <桌面> <pid> <instance.Class> <主机名> <标题……>
        # → split(maxsplit=5) 恰好 6 段（标题含空格，不限次会把标题切碎）。
        for line in p.stdout.splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) < 5:
                continue
            try:
                if int(parts[0], 16) != want:
                    continue
            except ValueError:
                continue
            # 复用 AppMixin 的解析（instance 与 Class 同名时 wmctrl 会拼成 X.X，
            # 直接 split('.', 1)[1] 会切错——详见 _wm_class_to_name 的 docstring）。
            return self._wm_class_to_name(parts[3]) or None
        return None

    def window_geometry(self, wid: str) -> Rect | None:
        """读窗口屏幕几何（左上角 + 尺寸）。复用 demo getwindowgeometry --shell 解析。"""
        p = self._run(["getwindowgeometry", "--shell", wid])
        geom: dict[str, int] = {}
        for line in p.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k in ("X", "Y", "WIDTH", "HEIGHT"):
                    try:
                        geom[k] = int(v)
                    except ValueError:
                        pass
        if not geom:
            return None
        return Rect(geom.get("X", 0), geom.get("Y", 0), geom.get("WIDTH", 0), geom.get("HEIGHT", 0))

    def window_screen_pos_by_pid(self, pid: int, title: str | None = None) -> Rect | None:
        """
        按 PID 精确找窗口屏幕矩形。复用 demo/calib_click.py window_screen_pos_by_pid。

        只取矩形；需要知道「这次匹配有多可信」请用 window_screen_pos_by_pid_match。
        """
        return self.window_screen_pos_by_pid_match(pid, title)[0]

    def window_screen_pos_by_pid_match(
        self, pid: int, title: str | None = None,
    ) -> tuple[Rect | None, str]:
        """
        同 window_screen_pos_by_pid，但额外回报**这次匹配有多可信**（M-16 观察点）。

        实现逻辑：
          1. xdotool search --pid <pid> [--name title] 拿候选窗口 id。
          2. 无 title / title 没命中 → 退化为只按 pid 搜。
          3. 取面积最大的真实窗口（面积最大者天然排除了 1x1 隐形辅助窗）。

        返回 (rect, match)，match ∈ {"title", "pid_fuzzy", "none"}：
          - "title"     ：按「pid + 标题」精确命中，基准可信；
          - "pid_fuzzy" ：退化成「该 pid 下面积最大的窗口」。**弹出菜单 / 下拉浮层的顶层
                          窗口通常无名**，此时基准会错成应用主窗口——调用方若拿它去校准
                          浮层内元素的坐标，整体就会偏移（已知局限，见 M-16）；
          - "none"      ：一个窗口都没找到。
        """
        args = ["search", "--pid", str(pid)]
        match = "pid_fuzzy"
        if title:
            # M-32：`search --name` 的取值是**位置参数正则**（`--name` 只是「按哪个字段匹配」
            # 的开关），而窗口标题是**外部可控**的任意字节串。不转义有两个后果：
            #   ① 标题含 `(` `|` `+` `[` 等元字符时正则非法/匹配失败 → 退化成按 pid 搜
            #      → 同 pid 多窗口时可能锁到**另一扇窗**的几何，校准坐标整体错位；
            #   ② 恶意/畸形标题可构造慢正则，让 xdotool 在搜索上空转。
            pattern = re.escape(title)
            args_t = ["search", "--pid", str(pid), "--name", pattern]
            wids = self._run(args_t).stdout.split()
            if wids:
                match = "title"
            else:
                wids = self._run(args).stdout.split()
        else:
            wids = self._run(args).stdout.split()

        best: Rect | None = None
        best_area = 0
        for wid in wids:
            rect = self.window_geometry(wid)
            if rect is None:
                continue
            area = rect.area
            if area > best_area:
                best_area = area
                best = rect
        return best, (match if best is not None else "none")

    # ---------- 窗口清单 / 等待 ----------
    def list_windows(self, limit: int = 100) -> list[dict]:
        """
        列出当前可见窗口（id/标题/PID/几何），用于窗口甄别与定位。

        实现逻辑：
          1. 优先 `wmctrl -lGpx` 一次拿全（见 _windows_via_wmctrl 的三条理由）。
          2. wmctrl 缺失/失败（如 CC_CU_SANDBOX_WM=none 未起 WM）→ 退化 xdotool 逐窗枚举。
          3. 组装 [{id,title,pid,x,y,w,h,area}] 并按面积降序（最大者通常是主窗口）。
        """
        rows = self._windows_via_wmctrl(limit)
        if rows is None:
            rows = self._windows_via_xdotool(limit)
        rows.sort(key=lambda d: d["area"], reverse=True)
        return rows

    def _windows_via_wmctrl(self, limit: int) -> list[dict] | None:
        """
        用 `wmctrl -lGpx` 取窗口清单；wmctrl 缺失/执行失败返回 None（调用方退化）。

        为什么它是首选（前两条都是实测踩过的坑）：
          ⚠️ 1. **本机 xdotool 3.20160805.1 的 getwindowname / getwindowpid /
                getwindowgeometry 全都是「单窗口」命令**——多传的 id 会被 xdotool
                当成**命令名**（`Unknown command: 2097223`），只有第一个窗口有输出。
                旧实现按「批量调用 + 几何缺失即当辅助窗过滤」组装，于是除首个窗外的
                窗口**全部被静默丢弃**：桌面上有 DBeaver 和 Chrome 时 list_windows
                只返回一个 i3 根窗，且不报任何错。静默的错误答案比报错危险得多——
                调用方会据此得出「桌面上没有别的窗口」这种完全相反的结论。
             2. wmctrl 只列**受 WM 管理的客户窗**，天然不带 WM 自身的框架窗：沙箱里
                i3 的 `[i3 con] container around 0x…`、`i3bar for output default`
                会被 `xdotool search --onlyvisible` 全部枚举出来，纯噪音。
             3. 单次子进程调用、输出逐行天然对位，没有「窗口在中途销毁即整列错位」的风险。

        输出行格式（实测确认）：
          <窗口id> <桌面> <pid> <x> <y> <w> <h> <instance.Class> <主机名> <标题……>
          → split(maxsplit=9) 恰好 10 段。必须限次分割：标题含空格，不限次会把标题切碎。
        """
        wmctrl = shutil.which("wmctrl")
        if not wmctrl:
            return None
        try:
            p = subprocess.run([wmctrl, "-lGpx"], capture_output=True, text=True,
                               timeout=5, errors="replace", env=display.env_for())
        except Exception as exc:  # noqa: BLE001
            log.debug("wmctrl -lGpx 执行失败: %s", exc)
            return None
        if p.returncode != 0:
            log.debug("wmctrl -lGpx 返回码 %d: %s", p.returncode, (p.stderr or "").strip())
            return None

        out: list[dict] = []
        for line in p.stdout.splitlines():
            parts = line.split(maxsplit=9)
            if len(parts) < 10:
                continue
            wid_hex, _desktop, pid_s, x_s, y_s, w_s, h_s = parts[:7]
            title = parts[9].strip()
            try:
                # ⚠️ id 必须归一成**十进制**：wmctrl 给的是 0x 十六进制，而项目里
                # 其它通道（xdotool getactivewindow / click 的落点证据）都是十进制。
                # 不归一，模型就没法把这里拿到的 id 喂给 wait_window(window_id=…)。
                wid = str(int(wid_hex, 16))
                x, y, w, h = int(x_s), int(y_s), int(w_s), int(h_s)
                pid = int(pid_s) or None
            except ValueError:
                log.debug("wmctrl 行解析失败，跳过: %r", line)
                continue
            # 过滤无名窗与零面积辅助窗（与 xdotool 路径保持一致的语义）
            if not title or w <= 0 or h <= 0:
                continue
            out.append({"id": wid, "title": title, "pid": pid,
                        "x": x, "y": y, "w": w, "h": h, "area": w * h})
        return out[:limit]

    def _windows_via_xdotool(self, limit: int) -> list[dict]:
        """
        xdotool 逐窗枚举（无 WM 时的兜底路径：wmctrl 依赖 EWMH 兼容的 WM）。

        ⚠️ **不要改回批量调用**（这正是上一版静默失效的根因）：本机 xdotool
        3.20160805.1 的 getwindowname/getwindowpid/getwindowgeometry 都是单窗口命令，
        `xdotool getwindowgeometry wid1 wid2` 会把第二个 id 当成命令名并报
        `Unknown command`，stdout 里只有第一个窗口的数据。按批量结果组装时，
        其余窗口会因「几何缺失」被当作辅助窗过滤掉——**不报错，但答案是错的**。
        逐窗 N 次进程启动（约 10ms/次）换正确性，这里不省。
        """
        wids = self._run(["search", "--onlyvisible", "--name", ""]).stdout.split()
        if not wids:
            return []
        out: list[dict] = []
        for wid in wids[:limit]:
            title = self._run(["getwindowname", wid]).stdout.replace("\n", " ").strip()
            if not title:
                continue
            g = self._parse_multi_geometry(self._run(["getwindowgeometry", wid]).stdout).get(wid)
            if not g:
                continue
            rect = Rect(*g)
            if rect.area == 0:
                continue
            pid_raw = self._run(["getwindowpid", wid]).stdout.split()
            pid = int(pid_raw[0]) if pid_raw and pid_raw[0].isdigit() else None
            out.append({"id": wid, "title": title, "pid": pid,
                        "x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h,
                        "area": rect.area})
        return out

    def window_title(self, wid: str) -> str | None:
        """
        取单个窗口的标题；取不到返回 None（窗口已销毁时 xdotool 只报 stderr 无输出）。

        复用 _read_titles 而不是直接拼 getwindowname，是为了共用它的两件归一化：
        标题里的换行被压成空格（X11 的 WM_NAME 允许内嵌换行，直接回给模型会把一行
        文本撑成多行），以及空标题归一为 None。
        """
        if not wid:
            return None
        titles = self._read_titles([wid])
        t = titles[0].strip() if titles else ""
        return t or None

    def _read_titles(self, wids: list[str]) -> list[str]:
        """
        批量取窗口标题，返回值与 wids **按下标对位**（取不到的位返回空串）。

        ️ 为什么逐个取、不用 `getwindowname wid1 wid2 …` 的批量形式（2026-09-16 实测，
        别改回去）：本机 xdotool 3.20160805.1 下批量形式**只打印第一个窗口的标题**，
        其余一律当作未知命令——
            $ xdotool getwindowname 44040268 8388615
            ai_middle_api
            xdotool: Unknown command: 8388615        # rc=1
        于是批量形式既拿不到数据（白起一个进程），又让「行数 ≠ 窗数即降级」这道防御
        **恒被触发**；更要命的是它留下了唯一的**击穿通道**——只要首个窗口的标题里恰好含
        「窗数 − 1」个换行，行数就会与窗数相撞，防御判为「没对错位」并放行，后续窗口
        会集体安上前一个窗口的标题碎片（静默错答，正是本函数要防的那件事）。

        逐个取现在是**更省**的：批量形式下每次要起 N+1 个进程（1 个必然失败的批量 +
        N 个逐窗），现在只有 N 个。而调用方 `wait_window` 是**轮询**调用（0.25s 一次、
        最长 10s），省下的是一个持续收益而非一次性开销。

        历史背景：早先确有「批量输出按行号对位」这个坑（窗口在 search 与取标题之间销毁
        即整列错位，上一次会话 list_windows 返回整列空 title 即此因），当时的防御就是
        上面的行数比对——但那道防御今天已无对象可防，因为数据源本身已经不成立。
        """
        return [self._run(["getwindowname", w]).stdout.replace("\n", " ").strip()
                for w in wids]

    def wait_window(
        self, title_contains: str | None = None, window_id: str | None = None,
        timeout: float = 10.0, poll: float = 0.25,
    ) -> dict | None:
        """
        等待窗口标题满足条件（server 内部轮询，单次调用完成，替代外部反复查询）。

        实现逻辑：每隔 poll 秒查一次——window_id 指定则读该窗标题；否则若给了
        title_contains，**遍历全部可见窗按标题子串匹配**（不依赖活动窗：沙箱内
        transient 对话框/无 WM 场景下 getactivewindow 不可靠）；两者都没给才盯
        活动窗（读到非空标题即返回）。超时返回 None。
        """
        deadline = time.monotonic() + timeout
        needle = (title_contains or "").lower()
        while True:
            if window_id:
                wid = window_id
                title = self._run(["getwindowname", window_id]).stdout.strip()
                if needle:
                    if needle in title.lower():
                        return {"id": wid, "title": title}
                elif title:
                    return {"id": wid, "title": title}
            elif needle:
                wids = self._run(["search", "--onlyvisible", "--name", ""]).stdout.split()
                if wids:
                    titles = self._read_titles(wids)
                    for wid, t in zip(wids, titles):
                        if needle in t.lower():
                            return {"id": wid, "title": t}
            else:
                wid = self.active_window_id() or ""
                title = self.active_window_title() or ""
                if title:
                    return {"id": wid, "title": title}
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)

    @staticmethod
    def _parse_multi_geometry(out: str) -> dict[str, tuple[int, int, int, int]]:
        """
        解析 `xdotool getwindowgeometry id1 id2 ...`（非 --shell）的批量输出。

        形如：
          Window 92274806
            Position: 14,118 (screen: 0)
            Geometry: 1920x1065
        返回 {wid: (x,y,w,h)}。
        """
        geoms: dict[str, tuple[int, int, int, int]] = {}
        cur: str | None = None
        pos: tuple[int, int] | None = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Window "):
                parts = s.split()
                cur = parts[1] if len(parts) > 1 else None
                pos = None
            elif s.startswith("Position:") and cur:
                try:
                    xy = s.split(":", 1)[1].split("(")[0].strip()
                    x, y = xy.split(",")
                    pos = (int(x), int(y))
                except (ValueError, IndexError):
                    pos = (0, 0)
            elif s.startswith("Geometry:") and cur and pos is not None:
                try:
                    w, h = s.split(":", 1)[1].strip().split("x")
                    geoms[cur] = (pos[0], pos[1], int(w), int(h))
                except (ValueError, IndexError):
                    continue
        return geoms