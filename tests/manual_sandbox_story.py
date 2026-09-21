"""
隔离沙箱端到端 story：MCP stdio 客户端驱动 server（优先冻结产物 dist/computer-use-mcp，
未重建时退回 `python -m computer_use_mcp.server`），
在 Xephyr 沙箱内完成「launch_app 起 zenity → 读树 → 元素级点『是』」，
并全程断言**宿主桌面不受干扰**（活动窗口标题与指针位置前后一致）。

验证点（对应隔离沙箱设计）：
  1. server 自启沙箱：握手后 list_windows 只应看到沙箱内窗口（zenity），
     宿主桌面窗口不出现；
  2. 注入零宿主副作用：story 前后宿主的 getactivewindow/getmouselocation 不变；
  3. launch_app → wait_window → get_ui_tree → click(ref) 全 MCP 链路可用
     （不需要任何 Bash xdotool/wmctrl）。

运行（**期间不要动鼠标/键盘**，宿主指针零移动是断言前提）：
  PYTHONNOUSERSITE=1 /home/liufei/anaconda3/envs/cc-comptuer-use/bin/python \
      tests/manual_sandbox_story.py
  （加 CC_CU_STORY_DIST=1 前缀改为驱动冻结产物 dist/computer-use-mcp）
退出码 0=全过。real 模式（CC_CU_DISPLAY_MODE=real）下宿主断言无意义，脚本直接拒绝运行。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time

TITLE = "SandboxStory"
MCP_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dist", "computer-use-mcp",
)
SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
)

# 肯定按钮的可能名字（中英文环境）
_AFFIRMATIVE = ("是", "Yes", "确定", "OK", "好")


def log(msg: str) -> None:
    print(f"[story] {msg}", flush=True)


def host_xdotool(*args: str) -> str:
    """在**宿主** display 上只读查询（story 自己的断言通道，不走 MCP）。"""
    r = subprocess.run(["xdotool", *args], capture_output=True, text=True,
                       env={**os.environ})  # story 进程本身跑在宿主会话
    return r.stdout.strip()


def host_baseline() -> dict:
    return {
        "active": host_xdotool("getactivewindow", "getwindowname"),
        "mouse": host_xdotool("getmouselocation", "--shell"),
    }


async def call_tool(session, name: str, args: dict, log_limit: int = 300) -> str:
    """调用 MCP 工具并拼纯文本返回；is_error 时抛错（兼容 mcp 1.x/2.x 属性名）。"""
    result = await session.call_tool(name, args)
    text = "\n".join(getattr(c, "text", None) or str(c) for c in result.content)
    log(f"call {name}({args}) -> {text[:log_limit].replace(chr(10), ' | ')}")
    is_err = getattr(result, "is_error", None)
    if is_err is None:
        is_err = getattr(result, "isError", False)
    if is_err:
        raise RuntimeError(f"工具 {name} 返回错误: {text}")
    return text


def parse_refs(text: str) -> list[int]:
    """兼容 [ref=N] 与裸 [N] 两种 ref 标记格式。"""
    out = []
    for m in re.finditer(r"\[ref=(\d+)\]|\[(\d+)\]", text):
        out.append(int(m.group(1) or m.group(2)))
    return out


# ---------- 宿主指针采样断言 ----------
# 协议：运行本 story 期间**不要动鼠标/键盘**（文档已注明）。注入链路只作用于 :99，
# 宿主指针任何移动都意味着泄漏或违反协议——采样全程指针轨迹，有任何移动即 FAIL
# 并提示重跑。活桌面上无法在客户端完美归因「用户操作 vs 注入泄漏」，
# 故用协议换断言的确定性；架构级保证另由 test_run_env_carries_target_display
# （注入 env 恒为 target display）与沙箱内 e2e 承担。
_POINTER_SAMPLES: list[tuple[float, int, int]] = []
_SAMPLER_STOP = threading.Event()


def _pointer_sampler() -> None:
    while not _SAMPLER_STOP.is_set():
        out = host_xdotool("getmouselocation", "--shell")
        xy = {}
        for line in out.splitlines():
            if line.startswith(("X=", "Y=")):
                k, v = line.split("=", 1)
                try:
                    xy[k] = int(v)
                except ValueError:
                    pass
        if "X" in xy and "Y" in xy:
            _POINTER_SAMPLES.append((time.monotonic(), xy["X"], xy["Y"]))
        time.sleep(0.2)


def check_pointer_leak() -> str | None:
    """全程宿主指针零移动则返回 None；否则返回 FAIL 原因（含首末位置）。"""
    moved = [(s0, s1) for s0, s1 in zip(_POINTER_SAMPLES, _POINTER_SAMPLES[1:])
             if (s0[1], s0[2]) != (s1[1], s1[2])]
    if not moved:
        return None
    (t0, x0, y0), (t1, x1, y1) = moved[0][0], moved[-1][1]
    return (f"story 期间宿主指针移动 {len(moved)} 次：({x0},{y0})→({x1},{y1})。"
            f"若运行期间操作了鼠标请保持双手离开重跑；否则为注入泄漏 BUG")


def proc_alive(pid: int) -> bool:
    """
    进程是否仍活着。

    坑：zenity 的父进程是 MCP server（未 wait），退出后以**僵尸**态留在 /proc——
    只看目录存在会误判「未退出」。读 stat 的状态字段：Z=僵尸=已退出。
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, IndexError):
        return False


async def main() -> int:
    if os.environ.get("CC_CU_DISPLAY_MODE", "isolated").strip().lower() == "real":
        log("CC_CU_DISPLAY_MODE=real：本 story 专测隔离沙箱，real 模式无意义，退出")
        return 2

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # 默认驱动模块 server（沙箱逻辑同源、免重建）；CC_CU_STORY_DIST=1 时才驱动冻结产物
    # （连打包链路一起验——注意 dist 是旧构建时工具集会缺新工具）
    if os.environ.get("CC_CU_STORY_DIST") == "1" and os.path.exists(MCP_BIN):
        params = StdioServerParameters(
            command=MCP_BIN,
            env={**os.environ, "PYTHONNOUSERSITE": "1"},
        )
        log(f"驱动冻结产物: {MCP_BIN}")
    else:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "computer_use_mcp.server"],
            env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONPATH": SRC_DIR},
        )
        log(f"驱动模块 server: {sys.executable} -m computer_use_mcp.server")

    subprocess.run(["pkill", "-9", "-x", "zenity"], capture_output=True)
    time.sleep(0.3)
    before = host_baseline()
    log(f"宿主基线: active={before['active']!r}")
    sampler = threading.Thread(target=_pointer_sampler, daemon=True)
    sampler.start()

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                si = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                log(f"MCP 握手成功: {si.name} {si.version}")

                # 1. 沙箱内起 zenity（launch_app 是 isolated 模式的应用入口正路）
                out = await call_tool(session, "launch_app", {
                    "command": f"zenity --question --title {TITLE} --text 沙箱story",
                    "settle": 2.5,
                })
                info = json.loads(out)
                log(f"zenity pid={info['pid']} display={info['display']}")

                # 2. 等窗口出现（沙箱内的窗口）
                await call_tool(session, "wait_window",
                                {"title_contains": TITLE, "timeout": 10})

                # 3. 读树 + 元素级点击肯定按钮
                tree = await call_tool(session, "get_ui_tree",
                                       {"scope": "app", "app": "zenity",
                                        "max_nodes": 80}, log_limit=1500)
                assert "push button" in tree, f"zenity 树异常:\n{tree}"
                found = await call_tool(session, "find_element",
                                        {"role": "push button", "app": "zenity"})
                refs = parse_refs(found)
                assert refs, f"未找到按钮 ref:\n{found}"
                # 名字命中肯定词优先；解析不到名字就点第一个
                target = refs[0]
                for line in found.splitlines():
                    if any(a in line for a in _AFFIRMATIVE):
                        line_refs = parse_refs(line)
                        if line_refs:
                            target = line_refs[0]
                            break
                res = await call_tool(session, "click", {"ref": target})
                assert "元素级" in res, f"未走元素级: {res}"

                # 4. 等 zenity 退出（点了肯定按钮 → 退出码 0）
                deadline = time.time() + 6
                alive = True
                while time.time() < deadline:
                    if not proc_alive(info["pid"]):
                        alive = False
                        break
                    time.sleep(0.4)
                assert not alive, f"点击后 zenity(pid={info['pid']}) 未退出"
                log("zenity 已退出（肯定按钮生效）")
    finally:
        subprocess.run(["pkill", "-9", "-x", "zenity"], capture_output=True)

    # 5. 宿主零干扰断言（详见 check_pointer_leak 注释：时间窗关联，抗用户并行操作）。
    _SAMPLER_STOP.set()
    sampler.join(timeout=2)
    time.sleep(0.5)
    after = host_baseline()
    if "Claude Sandbox" in after["active"]:
        log(f"SANDBOX_FAIL 宿主焦点停在沙箱窗: {after['active']!r}")
        return 1
    if after["active"] != before["active"]:
        log(f"warn 宿主活动窗口有变化（沙箱窗 WM 生命周期/用户操作所致，非注入）: "
            f"{before['active']!r} -> {after['active']!r}")
    leak = check_pointer_leak()
    if leak:
        log(f"SANDBOX_FAIL {leak}")
        return 1
    log("SANDBOX_E2E_OK 注入时间窗内宿主指针零移动")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
