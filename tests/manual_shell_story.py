"""
手动端到端 story：**冻结产物**里「沙箱操作 shell」两处修复的复验。

针对 2026-09-24 修的两个 bug，各给一条判据：

  ① 终端没进沙箱：`launch_app("gnome-terminal")` 的窗口开在**宿主桌面**上
     （gnome-terminal 是会话总线单实例应用，只改 DISPLAY 搬不动它）。
     判据 = 宿主 wmctrl 里终端窗口数**不增加**，且返回的 argv 里补上了 --disable-factory。

  ② 终端里 Ctrl+V 不是粘贴：长文本/中文经剪贴板路径**静默丢失**，只剩一个 ^V。
     判据 = 命令真的执行了（屏幕上出现执行结果），而不是"type_text 报成功"。

为什么必须打冻结产物而不是源码：① 的判据依赖 PyInstaller 产物里的启动路径；
两个 bug 都只在真机上暴露，单测里 stub 掉的方法照样绿。

前置：Xephyr / xdotool / wmctrl / gnome-terminal / tesseract（中文包）
     + 已执行 bash build.sh

运行：
  PYTHONNOUSERSITE=1 "$PY" tests/manual_shell_story.py
退出码 0 = 全过。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time

ART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "dist", "computer-use-mcp")

_ok = True
HOST_DISPLAY = os.environ.get("DISPLAY", ":0")


def log(msg: str) -> None:
    print(msg, flush=True)


def check(cond: bool, msg: str) -> None:
    global _ok
    log(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _ok = False


def sh(cmd: list[str], display: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "DISPLAY": display}).stdout


def host_terminal_windows() -> list[str]:
    """宿主桌面上的终端窗口（判据①的对象：这个数不许增加）。"""
    return [l for l in sh(["wmctrl", "-lx"], HOST_DISPLAY).splitlines() if "erminal" in l]


def host_terminal_count() -> int:
    return len(host_terminal_windows())


class Client:
    """极简 MCP stdio 客户端（与 manual_ocr_story.py 同款：后台读线程 + 按 id 派发）。"""

    def __init__(self, cmd: list[str]) -> None:
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  env=dict(os.environ, PYTHONNOUSERSITE="1"),
                                  text=True, bufsize=1)
        self._n = 0
        self._send_lock = threading.Lock()
        self._pending: dict[int, list] = {}
        threading.Thread(target=self._reader, daemon=True).start()
        self._req("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "shell-story", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, obj: dict) -> None:
        with self._send_lock:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()

    def _reader(self) -> None:
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ev = self._pending.get(msg.get("id"))
            if ev:
                ev[1] = msg
                ev[0].set()

    def _req(self, method: str, params: dict, timeout: float = 180.0) -> dict:
        with self._send_lock:
            self._n += 1
            rid = self._n
        ev: list = [threading.Event(), None]
        self._pending[rid] = ev
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev[0].wait(timeout):
            return {"error": {"message": "timeout"}}
        return ev[1]

    def call(self, name: str, args: dict | None = None) -> str:
        r = self._req("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            return f"RPC_ERROR {r['error']}"
        return "\n".join(c.get("text", "") if c.get("type") == "text"
                         else f"<{c.get('type')}>" for c in r["result"].get("content", []))

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass


def norm(s: str) -> str:
    """OCR 会把 - 识别丢、把空格读散，比较前统一去掉非字母数字。"""
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def main() -> int:
    if not os.path.exists(ART):
        log(f"❌ 找不到 {ART}，先跑 bash build.sh")
        return 1
    if not os.environ.get("DISPLAY"):
        log("❌ 需要 DISPLAY（沙箱是嵌在已有 X server 上的 Xephyr）")
        return 1

    c = Client([ART])
    try:
        log("== [1] launch_app('gnome-terminal') → 必须落沙箱、宿主零新增 ==")
        before = host_terminal_count()
        log(f"  启动前宿主终端窗口数 = {before}")
        out = c.call("launch_app", {"command": "gnome-terminal", "settle": 6})
        log(f"  launch_app → {out}")
        check("--disable-factory" in out, "argv 里补上了去单实例参数 --disable-factory")
        after = host_terminal_count()
        check(after == before,
              f"宿主桌面**没有**多出终端窗口（{before} → {after}，多出来就是把窗口开到真实桌面上了）")
        if after != before:
            for l in host_terminal_windows():
                log(f"      宿主上的终端：{l}")

        # 沙箱屏号从 MCP 回给我们的里拿
        m = re.search(r'"display"\s*:\s*"(:[\d.]+)"', out)
        sbx = m.group(1) if m else None
        check(bool(sbx), f"返回里带 display（= {sbx}）")

        log("\n== [2] 终端真的在沙箱屏上 ==")
        wins = sh(["xdotool", "search", "--onlyvisible", "--name", "liufei@"], sbx or ":0").split()
        check(bool(wins), f"沙箱 {sbx} 上出现终端窗口（{len(wins)} 个）")

        log("\n== [3] 长文本（>24 字符，走剪贴板路径）必须真的进终端 ==")
        # 先清掉可能残留的 lnext/^M 脏状态（实测：一次失败的粘贴会留下 ^M 卡在行缓冲）
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+c"], sbx or ":0")
        time.sleep(0.5)
        MARK = "CC-CU-SHELL-STORY-" + "Z" * 16          # 34 字符
        r = c.call("type_text", {"text": "echo " + MARK})
        log(f"  type_text → {r}")
        c.call("press_key", {"combo": "Return"})
        time.sleep(2.5)
        screen = norm(c.call("get_screen_text"))
        check(norm(MARK) in screen,
              "长文本真的进了终端并被 shell 执行（修复前：只剩一个 ^V，屏幕上找不到）")
        if norm(MARK) not in screen:
            log("      屏幕 OCR：")
            for line in c.call("get_screen_text").splitlines()[:12]:
                log("        " + line)

        log("\n== [4] 反向：普通窗口必须仍然用 ctrl+v ==")
        # 用 zenity 读回法（输入框内容 OCR 读不可靠，而"全选+复制"能从剪贴板逐字节核对）
        c.call("launch_app", {"command": "zenity --entry --title=ShellStory "
                                         "--timeout 40", "settle": 3})
        c.call("type_text", {"text": MARK})
        time.sleep(1.0)
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+a"], sbx or ":0")
        time.sleep(0.3)
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+c"], sbx or ":0")
        time.sleep(0.5)
        got = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                             capture_output=True, text=True,
                             env={**os.environ, "DISPLAY": sbx or ":0"}).stdout.strip()
        check(got == MARK, f"GTK 输入框照常粘贴（读到 {got!r}）")
        c.call("press_key", {"combo": "Escape"})
    finally:
        c.close()

    log("\n" + ("✅ 全部通过" if _ok else "❌ 有未通过项"))
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
