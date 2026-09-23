#!/usr/bin/env python3
"""
验收用的小客户端：**只用标准库**，直接以 JSON-RPC over stdio 驱动真实的 MCP server。

为什么不用 `mcp` 这个 Python 包
-----------------------------
验收要在**干净容器**里跑（目标机的模拟环境），而它恰恰是「不装任何东西」的那台机器。
装 `mcp` 得先 pip / apt，等于给验收引入了它本不该有的前提。MCP 的 stdio 传输就是
**换行分隔的 JSON-RPC 2.0**，手写几十行足够，且顺便验证了「这个 server 真的按协议说话」。

断言的是**客观事实**（本项目一贯的口径：不看工具返回 ok）：
  ① 沙箱里能起一个 GTK 应用（zenity）
  ② 能读到它的无障碍树，且树里有按钮
  ③ 元素级 click 能把对话框点掉 —— 进程真的退出
  ④ 全程零坐标（走的是 `do_action`，不是 xdotool 点击）

⚠️ 按钮标签必须**用 `--ok-label` 写死成 ASCII**（2026-09-23 在干净容器里踩到的）：
   原实现按 zenity 的中文默认标签「是」去找按钮，**在开发机上一直是对的**（宿主的
   zenity 认中文 locale），一到干净容器就红 —— 那里没配 locale，GTK 把按钮渲染成
   `Yes` / `No`，于是「树里没有『是』按钮」。这不是产品缺陷，是**用例偷偷依赖了
   宿主 locale**。写死标签后判据与语言无关（`--question` 同时支持这两个开关）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

MCP = os.environ.get("CC_CU_MCP", "/usr/bin/cc-computer-use-mcp")

# 验收用对话框：标签**写死成 ASCII**，判据因此与宿主语言环境无关（见模块 docstring）。
DLG_TITLE = "A11ySmoke"
OK_LABEL = "A11yOk"
CANCEL_LABEL = "A11yCancel"
LAUNCH_CMD = (f"zenity --question --title {DLG_TITLE} --text 中文汉字 "
              f"--ok-label {OK_LABEL} --cancel-label {CANCEL_LABEL}")


class Mcp:
    """极简 MCP stdio 客户端：起进程、发请求、按 id 收响应。"""

    def __init__(self, cmd: str) -> None:
        env = {**os.environ, "PYTHONNOUSERSITE": "1"}
        # stderr 不吞掉：server 的日志（沙箱就绪、私有总线就绪）正是排查时最需要的
        # 信息，验收失败时要看得到。
        self.p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, env=env)
        self._id = 0

    def _send(self, obj: dict) -> None:
        assert self.p.stdin
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 120.0):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        assert self.p.stdout
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue                       # 非协议输出（个别日志混进来），跳过
            if msg.get("id") == rid:
                return msg
        raise TimeoutError(f"{method} 超过 {timeout}s 没有响应")

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call(self, tool: str, args: dict, timeout: float = 120.0) -> str:
        """调一个工具，返回它的文本内容（拼接所有 text block）。"""
        r = self.request("tools/call", {"name": tool, "arguments": args}, timeout)
        assert r, f"{tool} 无响应"
        if "error" in r:
            raise RuntimeError(f"{tool} 协议错误: {r['error']}")
        res = r.get("result", {})
        text = "\n".join(c.get("text", "") for c in res.get("content", [])
                         if c.get("type") == "text")
        if res.get("isError"):
            raise RuntimeError(f"{tool} 返回错误: {text[:400]}")
        return text

    def close(self) -> None:
        try:
            self.p.terminate()
            self.p.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.p.kill()


def alive(name: str) -> bool:
    return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0


def main() -> int:
    cli = Mcp(MCP)
    try:
        cli.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "cc-cu-verify", "version": "0"},
        })
        cli.notify("notifications/initialized")

        tools = cli.request("tools/list")
        names = [t["name"] for t in tools["result"]["tools"]]
        print(f"  ✅ MCP 握手成功，暴露 {len(names)} 个工具")
        assert "get_ui_tree" in names and "click" in names, "工具集不完整"

        # ① 在沙箱里起一个真实的 GTK 应用
        out = cli.call("launch_app", {"command": LAUNCH_CMD, "settle": 2.0})
        print(f"  ✅ launch_app: {out[:120]}")
        pid = json.loads(out).get("pid")
        if not pid or not alive("zenity"):
            print("  ❌ zenity 没起来"); return 1

        # ② 读无障碍树 —— 这一步是整个项目的地基
        #
        # ⚠️ **必须轮询，不能固定 sleep**（这是本仓库踩过的坑）：应用「窗口已出现」与
        #    「在 AT-SPI 总线上完成注册」是两个时刻，实测沙箱内 zenity 上树要 1.6~2.5 秒，
        #    正好跨过常见的那类 2 秒魔数 —— 于是同一份代码会在全绿与全红之间随机翻转。
        #    所以这里一直读到有树为止（上限 30 秒），失败才判负。
        tree = ""
        for _ in range(30):
            tree = cli.call("get_ui_tree",
                            {"scope": "app", "app": "zenity", "max_nodes": 80})
            if "push button" in tree:
                break
            time.sleep(1)
        if "push button" not in tree:
            print(f"  ❌ 30 秒内读不到无障碍树：{tree[:300]}"); return 1
        print("  ✅ 读到无障碍树（含 push button）")

        # ③ 元素级点击写死标签的那个按钮。判据是**zenity 真的退出**，不是"工具返回 ok"
        m = re.search(r"\[(\d+)\] push button \| " + re.escape(OK_LABEL), tree)
        if not m:
            print(f"  ❌ 树里没有「{OK_LABEL}」按钮：{tree[:300]}"); return 1
        res = cli.call("click", {"ref": int(m.group(1))})
        if "元素级" not in res:
            print(f"  ❌ 没走元素级（退化了）：{res[:200]}"); return 1
        print(f"  ✅ 元素级 do_action：{res.splitlines()[0][:110]}")

        time.sleep(2)
        if alive("zenity"):
            print("  ❌ zenity 仍在运行 —— 点击没生效"); return 1
        print("  ✅ zenity 已被点掉（客观事实，不看 ok 字段）")
        return 0
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
