"""探针：经 MCP stdio 协议调 launch_app('gnome-terminal')，确认冻结产物传下去的 argv。

由 probe-deb-fix.sh 在容器内调用（PATH 里已放好假的 gnome-terminal）。
只用标准库，容器里不装任何 Python 包。
"""
import json
import os
import subprocess
import sys
import threading

ART = "/usr/bin/cc-computer-use-mcp"


def main() -> int:
    p = subprocess.Popen([ART], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL,
                         env=dict(os.environ, PYTHONNOUSERSITE="1"),
                         text=True, bufsize=1)
    pending: dict[int, list] = {}
    n = 0
    lock = threading.Lock()

    def send(obj):
        with lock:
            p.stdin.write(json.dumps(obj) + "\n")
            p.stdin.flush()

    def reader():
        for line in p.stdout:
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ev = pending.get(msg.get("id"))
            if ev:
                ev[1] = msg
                ev[0].set()

    threading.Thread(target=reader, daemon=True).start()

    def req(method, params, timeout=180.0):
        nonlocal n
        with lock:
            n += 1
            rid = n
        ev = [threading.Event(), None]
        pending[rid] = ev
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev[0].wait(timeout):
            return {"error": {"message": "timeout"}}
        return ev[1]

    req("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "deb-probe", "version": "1"}})
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    r = req("tools/call", {"name": "launch_app",
                           "arguments": {"command": "gnome-terminal", "settle": 1}})
    if "error" in r:
        print("RPC_ERROR", r["error"], file=sys.stderr)
        return 1
    for c in r["result"].get("content", []):
        if c.get("type") == "text":
            print("  launch_app 返回:", c["text"].strip())
    try:
        p.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
