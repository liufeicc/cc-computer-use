"""探针：确认冻结产物里「终端里改用 ctrl+shift+v 粘贴」这条修复真的生效。

由 probe-deb-fix.sh 在容器内调用（此时 .deb 已装好、Xvfb 已起）。

做法（不装任何终端仿真器）：
  1. `launch_app` 起 xev（X 事件查看器），把它的输出重定向到文件；
  2. 用 `xdotool set_window --class xterm` **把它的 WM_CLASS 改成终端的样子** ——
     要验的是"按 WM_CLASS 判定"这个逻辑本身，不必真装一个终端；
  3. 经 MCP 调 type_text 输入一个 >24 字符的串（必走剪贴板路径）；
  4. 读 xev 的输出，看它收到的是 `Control-Shift-v` 还是 `Control-v`。

判据是 **xev 记录的按键事件**，不是工具返回的 ok —— 修复前 type_text 同样报成功。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

ART = "/usr/bin/cc-computer-use-mcp"
MARK = "CC-CU-DEB-PROBE-" + "Q" * 16          # 32 字符，稳定超过 24 的阈值

# 探针自己也要用 xdotool / wmctrl，而这三个（连同 xclip）在本包里是**随包分发**的
# —— 只有 wrapper 会把 vendor/bin 前置进 PATH，普通进程直接调是找不到的。
# 目标机上「什么都没装」是分发前提，所以这里必须指向包内的那份，而不是指望系统有。
os.environ["PATH"] = "/opt/cc-computer-use/vendor/bin:" + os.environ.get("PATH", "")


class Client:
    def __init__(self):
        self.p = subprocess.Popen([ART], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  env=dict(os.environ, PYTHONNOUSERSITE="1"),
                                  text=True, bufsize=1)
        self._n = 0
        self._lock = threading.Lock()
        self._pending: dict[int, list] = {}
        threading.Thread(target=self._reader, daemon=True).start()
        self._req("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "kbd-probe", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, obj):
        with self._lock:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()

    def _reader(self):
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ev = self._pending.get(msg.get("id"))
            if ev:
                ev[1] = msg
                ev[0].set()

    def _req(self, method, params, timeout=180.0):
        with self._lock:
            self._n += 1
            rid = self._n
        ev = [threading.Event(), None]
        self._pending[rid] = ev
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev[0].wait(timeout):
            return {"error": {"message": "timeout"}}
        return ev[1]

    def call(self, name, args=None):
        r = self._req("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            return f"RPC_ERROR {r['error']}"
        return "\n".join(c.get("text", "") for c in r["result"].get("content", [])
                         if c.get("type") == "text")

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass


def sh(cmd, display):
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "DISPLAY": display}).stdout


def find_xev_window(display):
    """在沙箱里找出 xev 的窗口（= 不是 i3 自己的那个客户窗）。

    ⚠️ 两条都试过、都不行，别再走回头路：
      · 按标题找：xev 的 WM_NAME 是空的（`xdotool getwindowname` 返回空串）；
      · 按 `-geometry 300x200` 找：沙箱里的 i3 会**平铺**客户窗，请求的尺寸被忽略。
    改用 `wmctrl -lpx`（i3 是 EWMH 兼容的 WM，客户窗会被列进 _NET_CLIENT_LIST），
    把 i3 自己的那些（i3 / i3bar / Xephyr 框架）滤掉，剩下的就是 xev。
    """
    for line in sh(["wmctrl", "-lpx"], display).splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 5:
            continue
        cls = parts[3].lower()
        if "i3" in cls or "xephyr" in cls:
            continue
        return parts[0]
    return ""


def main() -> int:
    c = Client()
    try:
        out = c.call("launch_app", {
            "command": "sh -c 'xev -geometry 300x200 > /tmp/xev.log 2>&1'", "settle": 4})
        print("  launch_app →", out.strip())
        m = re.search(r'"display"\s*:\s*"(:[\d.]+)"', out)
        if not m:
            print("  ❌ 拿不到沙箱 display", file=sys.stderr)
            return 1
        disp = m.group(1)

        wid = ""
        for _ in range(20):
            wid = find_xev_window(disp)
            if wid:
                break
            time.sleep(0.5)
        if not wid:
            print("  ❌ 找不到 xev 窗口，沙箱里的窗口：", file=sys.stderr)
            print(sh(["wmctrl", "-lpx"], disp), file=sys.stderr)
            print(open("/tmp/xev.log", encoding="utf-8", errors="replace").read()[:400],
                  file=sys.stderr)
            return 1
        print(f"  xev 窗口 wid={wid}（沙箱 {disp}）")

        # 把它的 WM_CLASS 改成终端的样子 —— 验的是判定逻辑本身
        for _ in range(10):
            sh(["xdotool", "windowactivate", "--sync", wid], disp)
            time.sleep(0.3)
            if sh(["xdotool", "getactivewindow"], disp).strip() == wid:
                break
        sh(["xdotool", "set_window", "--class", "xterm", "--classname", "xterm", wid], disp)
        time.sleep(0.5)
        listed = [l for l in sh(["wmctrl", "-lpx"], disp).splitlines() if "xterm" in l]
        print("  wmctrl 看到的活动窗 WM_CLASS：")
        for l in listed:
            print("    " + l)
        if not listed:
            print("  ❌ WM_CLASS 没设上（后面的判据就失去意义了）", file=sys.stderr)
            return 1

        # 清掉 xev.log 里启动时那堆事件噪音
        subprocess.run(["truncate", "-s", "0", "/tmp/xev.log"], check=False)

        r = c.call("type_text", {"text": MARK})
        print("  type_text →", r.strip().splitlines()[0] if r.strip() else r)
        time.sleep(2.0)

        log = open("/tmp/xev.log", encoding="utf-8", errors="replace").read()
        # xev 每次按键打若干行，形如 `keysym 0x76, v`。**只取名字本身**
        # （后面紧跟 `)` 或 `,`，用字符集限死，别用 `\S+` —— 实测会把 `),` 一起吞进来）。
        keys = re.findall(r"keysym 0x[0-9a-f]+, ([A-Za-z_0-9]+)", log)
        print(f"  xev 收到的 keysym 序列 = {keys}")

        if not any(k.lower() == "v" for k in keys):
            print("  ❌ xev 根本没收到 v —— 剪贴板路径没走通（xclip 不在？）", file=sys.stderr)
            return 1
        # 判据：v 必须与 **Shift** 同时按下。
        #   修复前 = 裸 ctrl+v  → 序列里只会出现 Control_L 与 v；
        #   修复后 = ctrl+shift+v → 还会出现 Shift_L，且 v 的 keysym 是大写 V
        #    （X 里 Shift 同时改变 keysym：v → V）。
        if "Shift_L" not in keys and "V" not in keys:
            print("  ❌ 收到的是**裸 ctrl+v** —— 这就是修复前的行为"
                  "（在终端里会静默丢失，只剩一个 ^V）", file=sys.stderr)
            return 1
        print("  ✅ 序列里带 Shift —— 走的是 ctrl+shift+v，终端分支的修复确实在这个 .deb 里")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
