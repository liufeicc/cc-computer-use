"""
Windows 端到端 story：用 MCP stdio 客户端驱动**真的 server**（在客体上跑）。

═══ 与 `manual_windows_backend_story.py` 的分工（两个都要，不是二选一）═══

  · `manual_windows_backend_story.py` 直连 `get_backend()` —— 只验 **backend 层**；
  · 本文件起真 server、走 stdio —— 验 **coordinator + tools + server 三层**在 Windows 上
    **真的被执行**。

为什么必须分开：`backend/windows/**` 在开发机（Linux）上"只被 import、从不被执行"，
而 **coordinator / tools 在 Windows 上连跑的机会都没有**（开发机也是 Linux）。
2026-09-28 已经栽过一次同类的跟头 —— `backend.py` 里 13 处 `win32` 写成 `w.`，
注入那一半一次没跑通过，而当时 391 条测试全绿。本文件就是给"上层那一半"补上真机执行。

═══ 判据一律取**外部可观测事实** ═══

  · "OCR 读得对" → 拿**我们自己刚写进记事本的那串字符**反过来验读数通道；
  · "点中了按钮" → 对话框**从窗口列表里消失**（不是看 `ok=True`）；
  · "server 起得来" → `initialize` 有响应且 `tools/list` 拿得到工具集。

═══ 用法（在客体上，经 guestcontrol）═══

    python Z:\\cc-computer-use\\tests\\manual_windows_mcp_story.py --out results/mcp_story.json

    # 驱动**冻结产物**（安装包里的那份）而不是源码 —— 打包链路改过之后必跑一遍：
    set CC_CU_STORY_DIST=1
    python ...\\manual_windows_mcp_story.py

⚠️ 前置：客体 Python ≥3.12 且装了 `mcp` / `uiautomation` / `pillow` / `mss` / PyWinRT，
且**用户已登录交互式会话**（UIA 与抓屏都要求桌面真在渲染）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time

try:
    # ⚠️ `line_buffering=True` 不是为了好看：本脚本在客体上由 guestcontrol 跑，stdout 是
    # **管道**，Python 默认按块缓冲 —— 一旦进程被外部 kill（超时），缓冲区里的全部输出
    # 都会丢，排查时只能看到"什么都没有"，分不清是没跑起来还是跑到一半卡住（实测踩到）。
    # 另外显式指定 utf-8：中文 Windows 的默认 stdout 编码是 GBK，本脚本的 ✅/❌ 会直接
    # UnicodeEncodeError（`--selftest` 就是这么崩的）。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:  # noqa: BLE001
    pass

_HERE = pathlib.Path(__file__).resolve()
_REPO = next((p for p in _HERE.parents if (p / "src" / "computer_use_mcp").is_dir()), None)

_RESULTS: dict = {}
# 日志写客体 %TEMP%，**不写仓库目录**：仓库在客体上是共享文件夹（Z:），
# 往里丢运行日志会变成宿主侧的未跟踪文件，还得手工清。
_SERVER_ERR = pathlib.Path(os.environ.get("TEMP") or ".") / "cc-cu-mcp-story-server.log"


def _ok(label: str, cond: bool, detail: str = "") -> bool:
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'✅' if cond else '❌'} {label}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


def _wait_for(pred, timeout: float, interval: float = 0.5):  # noqa: ANN001, ANN202
    """轮询直到 pred() 为真值，返回它；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    return None


# ═══════════════════════ MCP stdio 客户端（与 Linux 侧 story 同一形状）═══════════════════════

class Client:
    """极简 MCP stdio 客户端：后台读线程 + 按 id 派发（工具调用是并发的）。"""

    def __init__(self, cmd: list[str], env: dict[str, str]) -> None:
        self._err_path = str(_SERVER_ERR)
        self._err = open(self._err_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self._err, env=env, text=True, bufsize=1,
                                  cwd=str(_REPO) if _REPO else None,
                                  # ⚠️ 必须显式 utf-8：MCP 的 stdio 传输在**服务端**就会把
                                  # 线缆重包成 UTF-8（`mcp/server/stdio.py` 里
                                  # `TextIOWrapper(..., encoding="utf-8")`），而 Python 客户端
                                  # 若按 `text=True` 走**本机 locale**（中文 Windows = GBK）解码，
                                  # 读到中文/emoji 就 `UnicodeDecodeError` —— 而且是在**读线程**
                                  # 里抛，主线程只看到"服务端一直不回话"，白等满超时。
                                  # 实测（2026-09-28）：第一版就是这样卡了 9 分钟。
                                  encoding="utf-8", errors="replace")
        self._n = 0
        self._send_lock = threading.Lock()
        self._pending: dict[int, list] = {}
        threading.Thread(target=self._reader, daemon=True).start()
        self._req("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "win-mcp-story", "version": "1"}})
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

    def _req(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        with self._send_lock:
            self._n += 1
            rid = self._n
        ev: list = [threading.Event(), None]
        self._pending[rid] = ev
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not ev[0].wait(timeout):
            return {"error": {"message": f"timeout({timeout}s) waiting {method}"}}
        return ev[1]

    def call(self, name: str, args: dict | None = None, timeout: float = 90.0) -> str:
        r = self._req("tools/call", {"name": name, "arguments": args or {}}, timeout)
        if "error" in r:
            return f"RPC_ERROR {r['error']}"
        return "\n".join(c.get("text", "") if c.get("type") == "text"
                         else f"<{c.get('type')}>" for c in r["result"].get("content", []))

    def call_raw(self, name: str, args: dict | None = None) -> list[dict]:
        r = self._req("tools/call", {"name": name, "arguments": args or {}})
        if "error" in r:
            return [{"type": "error", "text": str(r["error"])}]
        return r["result"].get("content", [])

    def close(self) -> None:
        for step in (self.p.stdin.close, lambda: self.p.wait(timeout=15)):
            try:
                step()
            except Exception:  # noqa: BLE001
                try:
                    self.p.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            self._err.close()
        except Exception:  # noqa: BLE001
            pass


def _windows(c: Client) -> list[dict]:
    """`list_windows` 工具 → 行列表（解析失败返回空表，让调用方的断言去报错）。"""
    txt = c.call("list_windows", {})
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return []


def _ids(rows: list[dict]) -> set[str]:
    return {str(r.get("id")) for r in rows}


def _kill(name: str) -> None:
    """清场：上一轮留下的同名窗口会让"活动窗口"变成别人，判据跟着飘。"""
    subprocess.run(["taskkill", "/F", "/IM", name],  # noqa: S603,S607
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


# ═══════════════════ 清掉 Win11 记事本的「会话恢复」（2026-09-28 真机实测）═══════════════════
#
# ⚠️ 不清理会怎样：**Win11 记事本会把上次未保存的文档连同窗口一起恢复**，而且
#    **每跑一轮就多一个**（实测累积到 10 个窗口，标题全是 `*REPLACED - Notepad`）。
#    后果有两层：
#      ① 那些窗口**盖在我们的窗口上面** —— 点"我们窗口"的标题栏，实际落到了别人身上，
#         前台变成它，OCR 读到的也是它（判据全飘）；
#      ② ★ **它们和我们的窗口是同一个进程**（实测 11 个窗口共用 1 个 pid）——
#         所以"按 pid 精确关掉多余窗口"这条路**根本走不通**，关了就一起关掉。
#    正路是把会话状态清掉：`LocalState\WindowState\` 下每个 `<uuid>.{0,1}.bin` 对应
#    一扇要恢复的窗，记事本**没在跑**时删掉整个目录即可。
_NOTEPAD_STATE = pathlib.Path(os.environ.get("LOCALAPPDATA") or "") / "Packages" / \
    "Microsoft.WindowsNotepad_8wekyb3d8bbwe" / "LocalState" / "WindowState"


def _reset_notepad_session() -> None:
    """关掉记事本 → 清会话恢复状态。之后起记事本只会得到我们要的那一扇窗。"""
    _kill("notepad.exe")
    time.sleep(1.5)                      # 等进程真的退干净，否则它退出时又把状态写回来
    if _NOTEPAD_STATE.is_dir():
        try:
            shutil.rmtree(_NOTEPAD_STATE)
            print(f"   已清掉记事本会话恢复状态：{_NOTEPAD_STATE}")
        except OSError as exc:
            print(f"   ⚠️ 清会话状态失败（{exc}）—— 桌面上可能多出几扇旧窗口")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Windows MCP 端到端 story")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if _REPO is None:
        print("❌ 找不到仓库根（本文件应在 <repo>/tests/ 下）")
        return 2

    # 清场：桌面上的旧记事本/旧对话框会让「活动窗口」不是我们以为的那个
    _kill("winver.exe")
    _reset_notepad_session()
    print("=== 0) 清场完毕（记事本会话状态已清、无遗留对话框）===")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")

    # CC_CU_STORY_DIST=1 → 驱动**冻结产物**，而不是源码。
    # 与 Linux 侧 tests/manual_sandbox_story.py 同款开关，理由也一样：
    # 冻结产物与源码是**两种运行形态**（依赖收集、sys.path、编码设置都可能不同），
    # 源码上全过不能推出冻结产物上也能过 —— 必须各跑一遍。
    # 产物路径可用 CC_CU_WIN_MCP_BIN 覆盖（客体上安装位置由安装器决定）。
    dist = os.environ.get("CC_CU_WIN_MCP_BIN") or str(
        _REPO / "dist" / "windows" / "payload" / "server" / "computer-use-mcp-bin.exe"
    )
    if os.environ.get("CC_CU_STORY_DIST") == "1":
        if not os.path.exists(dist):
            print(f"❌ CC_CU_STORY_DIST=1 但找不到冻结产物：{dist}")
            print("   先跑 packaging/windows/build-windows.sh，或用 CC_CU_WIN_MCP_BIN 指定路径")
            return 2
        cmd = [dist]
        print(f"   驱动冻结产物: {dist}")
    else:
        cmd = [sys.executable, "-m", "computer_use_mcp.server"]
        print(f"   驱动源码 server: {sys.executable} -m computer_use_mcp.server")

    c = Client(cmd, env)
    try:
        return _run(c, args)
    finally:
        c.close()


def _run(c: Client, args) -> int:  # noqa: ANN001
    print("=== 1) 真 server 起得来 ===")
    tools = [t["name"] for t in (c._req("tools/list", {}).get("result") or {}).get("tools", [])]
    _ok("tools/list 拿得到工具集", len(tools) >= 17, f"{len(tools)} 个：{sorted(tools)}")
    _ok("OCR 通道已注册", "get_screen_text" in tools)
    _ok("批量动作已注册", "act_sequence" in tools)

    print("=== 2) 起记事本并**把焦点交到它身上**（不假设「起完自己会到前台」）===")
    # ⚠️ 两个真机实测的坑，决定了本节这么写：
    #   ① **Win11 记事本会恢复上次未保存的内容**：裸起 notepad 时，上一轮 story 写进去的
    #      文字会自己回来，桌面上于是有多个记事本窗口 —— 「新出现的那个」未必是我们起的
    #      那个，标题也不确定。故给它一个**确定的文件路径**，标题里带着文件名，判据就唯一了。
    #   ② **起完不一定抢到前台**：实测从 guestcontrol 起的进程去 launch_app 之后，前台
    #      仍是 Program Manager（桌面），于是紧跟的 type_text 把字打给了桌面、工具却报成功。
    #      所以这里显式点一下目标窗口的标题栏把焦点接过来，并**断言焦点真的过去了**。
    doc = pathlib.Path(os.environ.get("TEMP") or ".") / "cc-cu-e2e.txt"
    doc.write_text("", encoding="utf-8")
    res = c.call("launch_app", {"command": f"notepad.exe {doc}", "settle": 2})
    print(f"   launch_app → {res[:200]}")
    _ok("launch_app 报成功", "失败" not in res and "RPC_ERROR" not in res, res[:120])
    _ok("launch_app 回报的 argv 里路径没被分词吃掉（Windows 上反斜杠不是转义符）",
        "cc-cu-e2e" in res, res[:200])

    def our_window():  # noqa: ANN202
        return next((r for r in _windows(c) if "cc-cu-e2e" in (r.get("title") or "")), None)

    row = _wait_for(our_window, timeout=30)
    _ok("我们的记事本窗口出现了（按标题里的文件名认，不靠「新出现的那个」）",
        row is not None, f"标题={row.get('title') if row else None}")
    if row is None:
        return _finish(args)

    # ⚠️ 会话恢复会把**上一轮那份未保存文档**一并带出来，而且**盖在我们窗口上面**
    #    （实测：点我们窗口的标题栏，实际落到了它身上）。故先清掉会话状态 ——
    #    这不是"绕过问题"，而是把桌面还原成"只有我们要操作的那扇窗"这个前提。
    extras = [r for r in _windows(c)
              if r.get("app") == "notepad" and r.get("id") != row.get("id")]
    if extras:
        print(f"   ⚠️ 还多出 {len(extras)} 个记事本窗口（会话恢复没清干净）——"
              f"它们会挡住我们要点的窗口；**不能按 pid 关**（实测多窗共用一个进程）")
        for r in extras:
            print(f"      id={r.get('id')} pid={r.get('pid')} 标题={r.get('title')!r}")

    # 点标题栏 = 把焦点交给它。这是**模型真会做的那一步**（也是唯一能跨进程抢前台的可靠办法）。
    cx, cy = row["x"] + row["w"] // 2, row["y"] + 12
    res = c.call("click", {"x": cx, "y": cy})
    print("   " + res.splitlines()[0][:200])
    _ok("★ 点击之后焦点真的到了我们的窗口（证据里的「点后活动窗口」带我们的文件名）",
        "cc-cu-e2e" in res, res[:220])

    print("=== 3) 读树（coordinator + serializer 在 Windows 上首次执行）===")
    tree = c.call("get_ui_tree", {"scope": "active_window", "interactive_only": True,
                                  "max_nodes": 120})
    _ok("get_ui_tree(scope=active_window) 返回了带 ref 的树",
        "[ref" in tree or re.search(r"\[\d+\]", tree) is not None, f"{len(tree)} 字符")
    _ok("树是记事本的（标题栏那个窗口）", "记事本" in tree or "cc-cu-e2e" in tree,
        tree.splitlines()[0][:120] if tree else "(空)")

    print("=== 4) 写入已知内容（下一步拿它反过来验 OCR）===")
    # ⚠️ 唯一 token：屏幕别处不会有这串字符，出现即证明读数通道真的读到了**我们写的**
    TOKEN = "MCPE2E2026"
    typed = c.call("type_text", {"text": TOKEN})
    _ok("type_text 未报错", "RPC_ERROR" not in typed and "失败" not in typed, typed[:120])
    # 键盘是打到**前台窗口**上的：证据里那句"点后活动窗口"就是"字到底打给了谁"的唯一线索
    _ok("★ 输入之后活动窗口仍是我们的记事本（不是桌面/别的窗口）",
        "记事本" in typed or "cc-cu-e2e" in typed, typed[:200])
    time.sleep(1.0)

    print("=== 5) get_screen_text：OCR 全链路（tools → coordinator → backend）===")
    t0 = time.time()
    txt = c.call("get_screen_text", {"scope": "window"})
    dt = time.time() - t0
    for line in txt.splitlines()[:14]:
        print("   " + line)
    _ok("get_screen_text 返回了文本", re.search(r"\[\d+\]", txt) is not None,
        f"{len(txt)} 字符，耗时 {dt:.2f}s")
    _ok("★ 认出了我们自己刚写进去的 token", TOKEN in txt,
        "命中" if TOKEN in txt else f"期望含 {TOKEN} —— OCR 或注入有一边不对，"
                                    f"实际读到：{txt[:200]!r}")
    _ok("头部说明识别范围", "识别范围=window" in txt, txt.splitlines()[0][:100] if txt else "")
    # ⚠️ 这条不是锦上添花：真机上踩过「活动窗口判成了桌面」——那时 OCR 的 region 是整屏
    #    (1920x1009)，读到的是整个桌面（包括别人的窗口），而**所有断言照样能过**。
    #    拿窗口矩形去比，才真正验到 "scope=window 取的是我们那扇窗"。
    mreg = re.search(r"region=\[(-?\d+), (-?\d+), (\d+), (\d+)\]", txt)
    _ok("★ 识别范围就是**我们那扇窗**的矩形，不是整屏",
        mreg is not None and abs(int(mreg.group(3)) - row["w"]) <= 24
        and abs(int(mreg.group(4)) - row["h"]) <= 40,
        f"OCR region={mreg.groups() if mreg else None} 窗口={row['x'], row['y'], row['w'], row['h']}")
    _ok("头部说明坐标已是屏幕绝对坐标", "屏幕绝对坐标" in txt)
    # Windows 契约：自带 OCR 无置信度，必须**如实声明**、不许静默 no-op
    _ok("min_conf 不生效这件事被如实带到了输出里", "未生效" in txt,
        "自带 Windows.Media.Ocr 没有置信度；模型不能以为自己筛过了")

    print("=== 6) OCR ref → click(ref)：必走坐标降级，且要回报落点证据 ===")
    m = re.search(rf"\[(\d+)\]\s*{re.escape(TOKEN)}", txt)
    _ok("token 那条带 ref", m is not None)
    if m:
        res = c.call("click", {"ref": int(m.group(1))})
        print("   " + res.splitlines()[0][:200])
        _ok("点击报成功", "成功" in res, res.splitlines()[0][:120])
        # TextBlock 没有元素级动作 ⇒ 必走 tier②坐标点击 ⇒ 应带落点证据
        _ok("★ 回报了落点窗口（describe_point 键名契约在真机上才算数）",
            "落点：窗口" in res, res[:200])

    print("=== 7) OCR 定位对话框按钮 → 点击 → **对话框真的消失**（最强判据）===")
    # 为什么用 winver：它是一句话 + 一个按钮的 #32770 对话框，简单、必定存在、
    # 且"点完就消失"是**外部可观测**的 —— 而不是看工具返回的 ok。
    before7 = _ids(_windows(c))
    c.call("launch_app", {"command": "winver.exe", "settle": 2})

    def new_dialog():  # noqa: ANN202
        fresh = [r for r in _windows(c) if str(r.get("id")) not in before7]
        return next((r for r in fresh if r.get("class") == "#32770"), None)

    dlg = _wait_for(new_dialog, timeout=30)
    _ok("新的 #32770 对话框出现了（取差集，不写成「存在一个 #32770」——那会点到别人的对话框）",
        dlg is not None, f"id={dlg.get('id') if dlg else None}")
    if dlg is None:
        return _finish(args)
    time.sleep(1.0)          # 等它画完再识别

    dtxt = c.call("get_screen_text", {"scope": "window"})
    print("   对话框 OCR：")
    for line in dtxt.splitlines()[:10]:
        print("     " + line)
    # 按钮文字随系统语言而变（中文机是「确定」），故给候选名并在失败时把识别结果全打出来
    hit = None
    for cand in ("确定", "OK", "关闭", "Close", "好"):
        hit = re.search(rf"\[(\d+)\]\s*{cand}\b", dtxt) or re.search(rf"\[(\d+)\]\s*{cand}", dtxt)
        if hit:
            print(f"   命中按钮文字：{cand!r}")
            break
    _ok("OCR 认出了对话框按钮（候选：确定/OK/关闭…）", hit is not None,
        f"命中 {hit.group(0)!r}" if hit else
        f"识别到 {len(dtxt.splitlines())} 行，候选名一个都没命中："
        f"{[line for line in dtxt.splitlines()[2:]][:8]}")
    if hit:
        res = c.call("click", {"ref": int(hit.group(1))})
        print("   " + res.splitlines()[0][:160])
        gone = _wait_for(lambda: dlg["id"] not in _ids(_windows(c)), timeout=10)
        _ok("★ 对话框已从窗口列表消失（说明真的点中了）", gone is not None,
            f"id={dlg['id']}")

    print("=== 8) act_sequence 在 Windows 上（默认形态，此前从未执行过）===")
    blocks = c.call_raw("act_sequence", {"steps": [
        {"op": "screenshot", "max_side": 480},
    ]})
    kinds = [b.get("type") for b in blocks]
    log_json = next((b.get("text") or "" for b in blocks if b.get("type") == "text"), "")
    try:
        parsed = json.loads(log_json)
    except Exception as exc:  # noqa: BLE001
        parsed = {}
        print(f"   ⚠️ 步骤日志解析失败：{exc}")
    print(f"   content 块类型={kinds} 步骤 ok={parsed.get('ok')}")
    _ok("act_sequence 跑通且带回了图像", "image" in kinds, f"块类型={kinds}")
    _ok("步骤日志里记着 screenshot 且 _images 已被取出（否则 JSON 序列化会炸）",
        [s.get("op") for s in parsed.get("steps", [])] == ["screenshot"]
        and "_images" not in parsed, log_json[:120])

    print("=== 9) screenshot 落盘（inline=false）===")
    shot = c.call("screenshot", {"inline": False, "max_side": 640})
    print("   " + shot.splitlines()[0][:220])
    # 工具的文案是 `截图已落盘(未内联): path=… meta={…}`，meta 是 **Python dict 的 repr**
    # （单引号），不是 JSON —— 别用 json.loads 去解，会得到一个"解析失败"的假问题。
    mpath = re.search(r"path=(\S+)", shot)
    path = mpath.group(1) if mpath else ""
    _ok("截图落盘并回报路径", bool(path) and os.path.exists(path), path)
    _ok("meta 带 scale（模型换算坐标必需）", "'scale'" in shot, shot[:200])
    if path and os.path.exists(path):
        size = os.path.getsize(path)
        _ok("落盘的图不是空文件", size > 5000, f"{size} 字节")

    print("=== 10) 收尾：不留窗口给下一轮 ===")
    _kill("winver.exe")
    _kill("notepad.exe")
    _ok("清场完成（下一轮的活动窗口不会是被这一轮遗留的）", True)

    return _finish(args)


def _finish(args) -> int:  # noqa: ANN001
    failed = [k for k, v in _RESULTS.items() if not v["ok"]]
    print(f"\n[story] {'✅ 全部通过' if not failed else '❌ 失败：' + str(failed)}")
    if not failed:
        print(f"[story] server stderr 见 {_SERVER_ERR}")
    else:
        try:
            tail = _SERVER_ERR.read_text(encoding="utf-8", errors="replace")[-1500:]
            print(f"[story] server stderr 末尾：\n{tail}")
        except OSError:
            pass
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(_RESULTS, fh, ensure_ascii=False, indent=1)
            print(f"[story] 结果已写入 {args.out}")
        except OSError as exc:
            print(f"⚠️ 写 {args.out} 失败：{exc}")
    return 0 if not failed else 5


if __name__ == "__main__":
    sys.exit(main())