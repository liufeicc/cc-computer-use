"""
端到端真实任务测试：通过 MCP stdio 客户端驱动本项目冻结产物（dist/computer-use-mcp），
完成「打开 gedit → 写一个约100字的故事 → 保存到 /home/liufei/data/temp」。

实现逻辑：
  1. 用 mcp 客户端库以 stdio 方式连接冻结的 MCP server（真实协议链路，非进程内调用）。
  2. 脚本自身 subprocess 启动 gedit（**刻意不用 launch_app**：本 story 要验证的是
     **真实桌面**链路——`launch_app` 会把应用放进 Xephyr 沙箱，恰恰绕开了它要测的东西），并用 xdotool
     显式激活 gedit 窗口（实测教训：不激活时 active_window 可能是 GNOME 概览界面，
     且正文编辑区无名字、find_element 搜不到——必须靠「窗口聚焦后默认焦点在正文」）。
  3. 依次调 MCP 工具：
     - type_text（不带 ref，键盘注入）把故事输入正文；
     - press_key ctrl+s 触发保存对话框；
     - get_ui_tree(active_window) 读对话框（实测教训：scope=app 会被主窗口菜单里
       的「语言高亮列表」190+ 项占满 max_nodes，对话框被截断）；
     - find_element 定位「文件名」输入框 → type_text 写绝对保存路径
       （GTK 保存对话框的文件名框直接输入绝对路径即可定位目录）；
     - click「保存」按钮（兼容中英文界面），找不到则退化为 Return。
  4. 轮询确认目标文件落盘且内容包含故事开头，最后关闭 gedit。

运行：
  PYTHONNOUSERSITE=1 /home/liufei/anaconda3/envs/cc-comptuer-use/bin/python \
      tests/manual_gedit_story.py
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time

SAVE_DIR = "/home/liufei/data/temp"
SAVE_PATH = os.path.join(SAVE_DIR, "story.txt")
MCP_BIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dist", "computer-use-mcp",
)

STORY = (
    "深夜的灯塔守望人老陈，五十年来第一次见到海上漂来一只玻璃瓶。"
    "瓶中纸条只有一句话：谢谢你每晚的光。落款是1974年。"
    "他望向漆黑的海面，忽然明白——有些善意从不孤单，"
    "它穿过半个世纪的波涛，只为在今夜抵达另一颗心。"
)


def log(msg: str) -> None:
    print(f"[test] {msg}", flush=True)


async def call_tool(session, name: str, args: dict, log_limit: int = 200) -> str:
    """调用 MCP 工具并把结果拼成纯文本返回。log_limit 控制日志打印长度。"""
    result = await session.call_tool(name, args)
    parts = []
    for c in result.content:
        parts.append(getattr(c, "text", None) or str(c))
    text = "\n".join(parts)
    log(f"call {name}({args}) -> {text[:log_limit].replace(chr(10), ' | ')}")
    # mcp 2.x 属性 snake_case（is_error），1.x 为 isError，做兼容
    is_err = getattr(result, "is_error", None)
    if is_err is None:
        is_err = getattr(result, "isError", False)
    if is_err:
        raise RuntimeError(f"工具 {name} 返回错误: {text}")
    return text


def parse_first_ref(text: str) -> int:
    """从工具返回文本中提取第一个 [ref=数字] 或 [数字]。"""
    m = re.search(r"\[ref=(\d+)\]", text) or re.search(r"\[(\d+)\]", text)
    if not m:
        raise RuntimeError(f"返回中未找到 ref:\n{text[:500]}")
    return int(m.group(1))


def wait_gedit_window(timeout: float = 15.0) -> str:
    """
    等 xdotool 能搜到 gedit 文档窗口，返回窗口 id。

    实测教训：`search --name gedit` 会命中 3 个窗口——GTK 另建了两个 10x10
    辅助窗口（名字就叫 'gedit'，位置 10,10 / -100,-100），真正的文档窗口
    「无标题文档 N - gedit」不一定排第一，head -1 会选错导致激活静默失败。
    复用 demo/window_screen_pos_by_pid 的策略：过滤小窗、取面积最大者。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["xdotool", "search", "--name", "gedit"],
                           capture_output=True, text=True)
        best, best_area = None, 0
        for w in r.stdout.split():
            g = subprocess.run(["xdotool", "getwindowgeometry", w],
                               capture_output=True, text=True).stdout
            m = re.search(r"Geometry: (\d+)x(\d+)", g)
            if not m:
                continue
            area = int(m.group(1)) * int(m.group(2))
            if area > best_area:
                best, best_area = w, area
        if best and best_area > 400:  # 排除 10x10 等 GTK 辅助小窗
            return best
        time.sleep(0.5)
    raise TimeoutError("等待 gedit 窗口超时")


def focus_gedit(wid: str, timeout: float = 6.0) -> str:
    """
    激活 gedit 窗口并校验焦点确实落在 gedit，返回活动窗口标题。

    实测教训（激活风暴）：旧实现每 0.3s 重试 windowactivate 并 spam Escape，
    与用户手动操作互相抢焦点，mutter 收到激活请求风暴，是「测试把系统搞崩」的
    诱因之一。改为低频重试（每轮约 1.3s）、不再发 Escape。
    """
    deadline = time.time() + timeout
    title = ""
    while time.time() < deadline:
        subprocess.run(["xdotool", "windowactivate", "--sync", wid], capture_output=True)
        subprocess.run(["xdotool", "windowfocus", "--sync", wid], capture_output=True)
        time.sleep(0.8)
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                           capture_output=True, text=True)
        title = r.stdout.strip()
        if "gedit" in title or "文本编辑" in title:
            return title
        time.sleep(0.5)
    raise TimeoutError(f"聚焦 gedit 失败，当前活动窗口: {title!r}")


def ensure_gedit_focus(wid: str) -> None:
    """
    键盘注入前的焦点守卫：焦点不在 gedit 就重新激活。

    实测教训：这是真实使用中的桌面，注入过程中焦点可能被其他应用（如飞书）抢走，
    导致中文文本和 ctrl+s 打偏到别的窗口——每次依赖焦点的操作前都要重新校验。
    """
    r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                       capture_output=True, text=True)
    title = r.stdout.strip()
    if "gedit" not in title and "文本编辑" not in title:
        log(f"焦点丢失（当前={title!r}），重新激活 gedit")
        focus_gedit(wid)


def find_save_dialog(timeout: float = 3.0) -> str | None:
    """按窗口名搜「另存为/保存/Save As」对话框，返回窗口 id（搜不到返回 None）。

    不用 getactivewindow 判断：对话框是 transient 子窗口，未必是 WM 活动窗口。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for kw in ("另存为", "保存", "Save As", "Save"):
            r = subprocess.run(["xdotool", "search", "--name", kw],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.split()[0]
        time.sleep(0.5)
    return None


async def main() -> int:
    if not os.path.exists(MCP_BIN):
        log(f"找不到冻结产物 {MCP_BIN}，请先 bash build.sh")
        return 2
    os.makedirs(SAVE_DIR, exist_ok=True)
    if os.path.exists(SAVE_PATH):
        os.remove(SAVE_PATH)

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=MCP_BIN,
        env={**os.environ, "PYTHONNOUSERSITE": "1"},
    )

    # 启动 gedit（新文档）；先清掉上次失败运行残留的 gedit
    # 注意：必须 -x 精确匹配进程名，-f 会误杀本测试脚本自身（命令行含 gedit 字样）
    subprocess.run(["pkill", "-9", "-x", "gedit"], capture_output=True)
    time.sleep(0.3)
    gedit_bin = shutil.which("gedit")
    if not gedit_bin:
        log("系统未安装 gedit")
        return 2
    proc = subprocess.Popen([gedit_bin, "--new-document"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"已启动 gedit pid={proc.pid}")
    wid = wait_gedit_window()
    time.sleep(1.5)  # 等窗口完全映射，避免过早激活触发 X BadMatch

    # 显式激活 gedit 窗口并校验焦点（退出 GNOME 概览、确保键盘注入落在正文）
    title = focus_gedit(wid)
    time.sleep(0.8)  # 等 AT-SPI 树就绪
    log(f"已激活 gedit 窗口 wid={wid} title={title!r}")

    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                # mcp 2.x 属性改为 snake_case（server_info），1.x 为 serverInfo，做兼容
                si = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
                log(f"MCP 握手成功: {si.name} {si.version}")

                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                log(f"工具列表: {names}")

                # 1. 确认焦点回到 gedit（active_window 应显示 gedit 的 frame）
                await call_tool(session, "get_ui_tree",
                                {"scope": "active_window", "max_nodes": 60},
                                log_limit=600)

                # 2. 焦点守卫 + 键盘注入（不带 ref）把故事输入正文：
                #    窗口聚焦后默认焦点在正文编辑区；xdotool type 支持中文 Unicode keysym
                ensure_gedit_focus(wid)
                await call_tool(session, "type_text", {"text": STORY})
                time.sleep(0.5)

                # 3. 焦点守卫 + ctrl+s；用「搜窗口名」探测对话框（transient 对话框
                #    未必是 WM 活动窗口，getactivewindow 判断不可靠），未出现则重发
                dlg_wid = None
                for attempt in range(4):
                    ensure_gedit_focus(wid)
                    await call_tool(session, "press_key", {"combo": "ctrl+s"})
                    dlg_wid = find_save_dialog(timeout=2.5)
                    if dlg_wid:
                        break
                    log(f"ctrl+s 第{attempt + 1}次未探测到对话框，重试")
                if not dlg_wid:
                    raise RuntimeError("ctrl+s 后保存对话框未弹出")
                log(f"保存对话框已弹出 wid={dlg_wid}")

                # 4. 激活对话框（模态框此时通常已有焦点，激活是防焦点被抢），
                #    再用 active_window 读对话框树（小树、省 token）
                subprocess.run(["xdotool", "windowactivate", "--sync", dlg_wid],
                               capture_output=True)
                time.sleep(1.0)  # 等对话框 AT-SPI 树就绪
                dlg_tree = await call_tool(session, "get_ui_tree",
                                           {"scope": "active_window", "max_nodes": 300},
                                           log_limit=4000)
                with open("/tmp/gedit_dialog_tree.txt", "w", encoding="utf-8") as f:
                    f.write(dlg_tree)

                # 5. 从对话框树文本解析文件名框与保存按钮的 ref。
                #    实测教训：文件名输入框的 accessible name 是空的（标签是旁边的
                #    label | 名称(N)），按名字 find_element 永远搜不到；而对话框树
                #    （active_window 此时正确解析到 file chooser）里它是第一个
                #    `[ref] text`，保存按钮是唯一的 `[ref] push button | 保存`。
                m_entry = re.search(r"\[(\d+)\] text\b", dlg_tree)
                m_save = re.search(r"\[(\d+)\] push button \| 保存", dlg_tree)
                if not m_entry:
                    raise RuntimeError("对话框树中未找到文件名输入框（text 元素），"
                                       "完整树见 /tmp/gedit_dialog_tree.txt")
                entry_ref = int(m_entry.group(1))
                # 元素级 set_value 写绝对路径（零坐标、不依赖焦点、不走键盘注入；
                # GTK 文件名框输入绝对路径即可定位目录）
                await call_tool(session, "type_text",
                                {"ref": entry_ref, "text": SAVE_PATH,
                                 "clear_first": True})

                # 6. 元素级点击「保存」（do_action 零坐标、不依赖焦点）；
                #    树上找不到保存按钮才退化为激活对话框 + Return
                if m_save:
                    await call_tool(session, "click", {"ref": int(m_save.group(1))})
                else:
                    log("对话框树中未解析到保存按钮，退化为激活对话框 + Return")
                    subprocess.run(["xdotool", "windowactivate", "--sync", dlg_wid],
                                   capture_output=True)
                    time.sleep(0.4)
                    await call_tool(session, "press_key", {"combo": "Return"})
                time.sleep(1.5)  # 等 gedit 完成落盘，再进入 finally 关进程
    finally:
        # 7. 清理：关闭 gedit
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # 8. 验证文件落盘
    deadline = time.time() + 10
    ok = False
    while time.time() < deadline:
        if os.path.exists(SAVE_PATH) and os.path.getsize(SAVE_PATH) > 0:
            content = open(SAVE_PATH, encoding="utf-8").read()
            if "灯塔" in content:
                ok = True
                break
        time.sleep(0.5)

    if ok:
        log(f"GEDIT_E2E_OK 文件已保存: {SAVE_PATH} ({os.path.getsize(SAVE_PATH)} 字节)")
        return 0
    log(f"GEDIT_E2E_FAIL 文件未正确保存: {SAVE_PATH}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
