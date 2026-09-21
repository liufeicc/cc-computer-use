"""
手动端到端 story：**灰区感知（OCR 文本层）** —— 用 MCP stdio 客户端驱动**冻结产物**。

验证目标（「方向一」的完整链路）：
  launch_app 起一个中文对话框 → get_screen_text 读出「文字 @ 屏幕坐标」
  → 用 OCR 分配的 ref 点击 → 对话框真的关掉。
全程**不看截图、不估算坐标**，这正是灰区应用该走的路。

为什么要它：实测一条 DBeaver 任务 8.3 分钟里，工具只占 21 秒，其余 478 秒全花在
「看截图 → 估像素位置 → 换算回屏幕坐标」上。本 story 就是那条慢路的替代验证。

前置：
  - tesseract + 中文语言包（sudo apt install tesseract-ocr tesseract-ocr-chi-sim）
  - zenity、Xephyr、xdotool
  - 已执行 bash build.sh（本 story 打的是冻结产物，不是源码）

运行：
  PYTHONNOUSERSITE=1 "$PY" tests/manual_ocr_story.py
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


def log(msg: str) -> None:
    print(msg, flush=True)


def check(cond: bool, msg: str) -> None:
    global _ok
    log(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _ok = False


class Client:
    """极简 MCP stdio 客户端：后台读线程 + 按 id 派发（工具调用是并发的）。"""

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
                                 "clientInfo": {"name": "ocr-story", "version": "1"}})
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

    def call_raw(self, name: str, args: dict | None = None) -> list[dict]:
        """返回原始 content 块列表（要检查「有没有真的带回图像」时用）。"""
        r = self._req("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            return [{"type": "error", "text": str(r["error"])}]
        return r["result"].get("content", [])

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.p.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.p.kill()


def main() -> int:
    if not os.path.exists(ART):
        log(f"❌ 找不到冻结产物 {ART}，先执行 bash build.sh")
        return 2

    c = Client([ART])
    try:
        tools = [t["name"] for t in c._req("tools/list", {})["result"]["tools"]]
        log(f"① 已注册工具({len(tools)}): {tools}")
        check("get_screen_text" in tools, "get_screen_text 已注册")

        log("\n② 起一个中文对话框（模拟灰区应用的交互）")
        c.call("launch_app", {"command": "zenity --question --title=OCR故事 "
                                         "--text='确认删除？' --ok-label=确定 --cancel-label=放弃",
                              "settle": 3})
        info = c.call("wait_window", {"title_contains": "OCR故事", "timeout": 15})
        log(f"   {info}")
        check("OCR故事" in info, "对话框已出现")
        if "OCR故事" not in info:
            return 1
        # 前提校验（必须有）：下面「点完对话框不在列表里」用的是 list_windows，
        # 若 list_windows 本身坏了（2026-09-15 实测它曾因 xdotool 批量几何读取失效
        # 而只返回 1 个窗口），那条断言会**永远通过**——假绿比红更危险。
        listing = c.call("list_windows", {})
        check("OCR故事" in listing, "前提：list_windows 能看到刚起的对话框（否则后面的断言是空转）")

        log("\n③ get_screen_text（默认 scope=window，只看活动窗口）")
        t0 = time.time()
        txt = c.call("get_screen_text", {})
        dt = time.time() - t0
        for line in txt.splitlines():
            log("   " + line)
        check(dt < 10, f"耗时 {dt:.2f}s（窗口范围应为亚秒级）")
        check("确定" in txt and "放弃" in txt, "两个中文按钮都识别出来了")
        check("屏幕绝对坐标" in txt, "明确告知坐标是屏幕绝对坐标")

        log("\n④ 用 OCR 分配的 ref 点击「确定」（零坐标、零看图）")
        m = re.search(r"\[(\d+)\] 确定", txt)
        check(m is not None, "「确定」带 ref")
        if m:
            res = c.call("click", {"ref": int(m.group(1))})
            log("   " + res.splitlines()[0])
            check("成功" in res, "点击成功")
            # 方向二之 2b：坐标级点击要回报落点证据，模型据此判断有没有点偏
            check("落点：窗口" in res, "回报了落点窗口")
            # 点「确定」会把对话框关掉，点后已无活动窗口——靠「点前活动窗口」才能说清结果
            check("点后活动窗口" in res, "回报了点后活动窗口（含「已消失」这种情形）")
            time.sleep(1.5)
            left = c.call("list_windows", {})
            check("OCR故事" not in left, "对话框已关闭（说明确实点中了）")

        log("\n⑤ 截图链路：应为 JPEG 且只编码一次")
        shot = c.call("screenshot", {"inline": False})
        log("   " + shot.splitlines()[0])
        check("'format': 'jpeg'" in shot, "默认 JPEG")
        check("'scale'" in shot, "带 scale（模型换算坐标必需，缺了只能猜）")

        log("\n⑥ 方向二之 2a：act_sequence 里带 screenshot，一次调用拿回「步骤日志 + 图像」")
        # 再起一个对话框，验证「一串动作 + 最后截图确认」合并成一次调用
        c.call("launch_app", {"command": "zenity --question --title=SEQ故事 "
                                         "--text='合并验证？' --ok-label=确定 --cancel-label=放弃",
                              "settle": 3})
        c.call("wait_window", {"title_contains": "SEQ故事", "timeout": 15})
        blocks = c.call_raw("act_sequence", {"steps": [
            {"op": "screenshot", "max_side": 640},
        ]})
        kinds = [b.get("type") for b in blocks]
        log(f"   返回 content 块类型: {kinds}")
        check("image" in kinds, "同一次调用里带回了图像（不必再单独调 screenshot）")
        check(any("截图 meta" in (b.get("text") or "") for b in blocks), "图像带 meta（含 scale）")
        # 第一块是步骤日志 JSON：解析出来确认 screenshot 那一步真的记在案
        log_json = next((b.get("text") or "" for b in blocks if b.get("type") == "text"), "")
        try:
            parsed = json.loads(log_json)
            ops = [s.get("op") for s in parsed.get("steps", [])]
        except Exception as exc:  # noqa: BLE001
            parsed, ops = {}, []
            log(f"   ⚠️ 步骤日志解析失败: {exc}")
        log(f"   步骤日志 ops={ops} ok={parsed.get('ok')}")
        check(ops == ["screenshot"], "步骤日志里记录了 screenshot 这一步")
        check("_images" not in parsed, "_images 已被取出，JSON 里不该残留（否则无法序列化）")
        check("'scale'" in log_json, "步骤日志的 meta 里带 scale")
        # 收尾：把 SEQ故事 对话框关掉
        subprocess.run(["pkill", "-9", "-x", "zenity"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        log("\n⑦ 方向二之 2b：裸坐标点击要报出「落点文字」（灰区最关键的一条证据）")
        # 灰区应用没有元素树，模型手里只有「文字 + 坐标」——裸坐标直点时，
        # 「我按下去的那一点底下写着什么」是判断有没有点偏的唯一依据。
        c.call("launch_app", {"command": "zenity --question --title=落点故事 "
                                         "--text='落点验证？' --ok-label=确定 --cancel-label=放弃",
                              "settle": 3})
        c.call("wait_window", {"title_contains": "落点故事", "timeout": 15})
        txt2 = c.call("get_screen_text", {})
        m2 = re.search(r"\[(\d+)\] 确定\s+@ \((\d+),(\d+)\)", txt2)
        check(m2 is not None, "从 OCR 结果里拿到「确定」的坐标")
        if m2:
            kx, ky = int(m2.group(2)) + 12, int(m2.group(3)) + 7   # 点在该块中心附近
            res = c.call("click", {"x": kx, "y": ky})
            log("   " + res.splitlines()[0])
            check("落点文字：「确定」" in res, f"报出了落点文字（应含「确定」）")
            check("落点：窗口" in res, "报出了落点窗口")
            time.sleep(1.5)
            check("落点故事" not in c.call("list_windows", {}), "对话框已关闭（确实点中了）")
    finally:
        c.close()
        subprocess.run(["pkill", "-9", "-x", "zenity"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        # ⚠️ 只数**本会话自己的**沙箱：本仓库经常有多个 Claude 会话同时在跑，各自一块
        # Xephyr 虚拟屏（`Xephyr -displayfd` 自动分配屏号）。历史上这里是全系统计数，
        # 于是别的会话的沙箱会被算成「本故事的孤儿」，本故事反倒报假失败（实测踩到）。
        # 归属判据用**沙箱窗口标题里嵌的 server pid**（`Claude Sandbox (mcp <pid>)`），
        # 而不看父子关系：server 退出后孤儿会被 systemd 收养、PPID 变成 1，
        # 按 PPID 找就**恰恰漏掉**了要抓的那种残留。
        marker = f"Claude Sandbox (mcp {c.p.pid})"
        ps_out = subprocess.run("ps -eo stat,args", shell=True,
                                capture_output=True, text=True).stdout
        n = str(sum(1 for line in ps_out.splitlines()
                    if "Xephyr" in line and marker in line
                    and not line.split()[0].startswith("Z")))
        log(f"\n收尾：本会话存活 Xephyr = {n}（应为 0）")
        check(n == "0", "沙箱进程已回收，无孤儿")

    log("\n" + ("✅ 全过" if _ok else "❌ 有失败项"))
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())