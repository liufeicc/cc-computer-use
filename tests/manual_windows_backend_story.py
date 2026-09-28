"""
WindowsBackend 真机 story（验收脚本，不是产品代码）。

═══ 它验什么 ═══

单测能证明 `WindowsBackend` **结构上**满足 ABC（`__abstractmethods__` 为空）、
能在 Linux 上 import。但它证明不了**任何一条语义**：树读得对不对、
`list_apps` 给的名字与 `get_tree` 标的 app 是不是同一个、元素级动作真的生效没有。
这些只能上真机。

⚠️ 判据一律取**外部可观测事实**，不取工具的返回值：
  · 读树 → 节点数 > 0 **且**树里能找到那个已知的控件名；
  · 元素级动作 → winver 的按钮点完之后**对话框从窗口列表里消失**；
  · 坐标点击 → 同上，但走 `click_at`（UIA 矩形中心 → SendInput）。
本项目的血证是"工具报 ok、实际没生效"（`type_text` 恒报成功、`launch_app` 恒报 ok），
所以判据不能建立在返回值上。

═══ 两条踩过的坑（别改回去）═══

① **等窗口一律轮询，不许固定 sleep**。初版在第 2 节查「应用/窗口」、第 3 节才起记事本，
   于是 `list_apps` 恒为空 —— 而那一刻桌面上**真的**没有用户窗口（90 个顶层窗里
   89 个是托盘/工具/外壳窗），返回 0 是**对**的。看着像后端坏了，其实是脚本的
   章节顺序把"还没起"当成了"起不来"。
   紧随其后的固定 `time.sleep(4)` 也不够：Win11 的记事本是 WinUI3，冷启动常超 4 秒，
   于是 `active_window_title()` 读到的是桌面的 'Program Manager'。
   改用 `_wait_for` 轮询后两条一起消失。

② **`#32770` 是通用对话框类**，不是 winver 的专属标记。判据必须是
   「**新出现的** #32770」（前后取差集），写成「存在一个 #32770」的话，
   winver 没起来也会拿到别人的对话框、点掉它、再断言"消失了"——**全绿而什么都没验**。

═══ 用法 ═══

    python backend_story.py --out results/backend_story.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import pathlib
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 允许**不设 PYTHONPATH 直接跑**：从本文件位置往上找到仓库根，把 src/ 挂进来。
# 真机上的调用方（VBoxManage guestcontrol）本来就会设 PYTHONPATH，这条只是兜底，
# 让"把脚本拷到机器上直接 python 它"也能工作。
_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "src" / "computer_use_mcp").is_dir():
        sys.path.insert(0, str(_parent / "src"))
        break

from computer_use_mcp.backend.base import get_backend  # noqa: E402
from computer_use_mcp.backend.windows.driver import apps, win32  # noqa: E402

_RESULTS: dict = {}


def _ok(label: str, cond: bool, detail: str = "") -> bool:
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'✅' if cond else '❌'} {label}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


def _wait_for(pred, timeout: float, interval: float = 0.5):  # noqa: ANN001, ANN202
    """轮询直到 pred() 为真，返回它的返回值；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    return None


def _dialogs() -> set[int]:
    """当前所有 `#32770` 顶层窗（通用对话框类）。"""
    return {h for h in win32.enum_top_level_windows()
            if win32.class_name(h) == "#32770"}


def _area(h: int) -> int:
    r = win32.window_rect(h)
    return (r[2] - r[0]) * (r[3] - r[1]) if r else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WindowsBackend 真机 story")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    backend = get_backend()
    print(f"[story] backend = {type(backend).__module__}.{type(backend).__name__} "
          f"(name={backend.name})")

    print("=== 1) 就绪 ===")
    _ok("is_available", backend.is_available())
    _ok("liveness（不碰 a11y 的判活）", backend.liveness())

    print("=== 2) 起一个已知应用并**轮询等它出现** ===")
    # 先起记事本：后面「应用/窗口/读树/find」四节都需要桌面上有个确定的东西。
    # ⚠️ 必须轮询等窗口真的出现（坑 ①），不能用固定 sleep。
    subprocess.Popen(["notepad.exe"])          # noqa: S603,S607

    def notepad_row():  # noqa: ANN202
        rows = apps.window_rows(limit=200)
        return next((r for r in rows if r["app"] == "notepad"), None)

    row = _wait_for(notepad_row, timeout=30)
    if not row:
        _ok("记事本窗口出现", False, "30 秒内等不到 notepad 的窗口")
        return _finish(args)
    _ok("记事本窗口出现", True,
        f"{row['id']} {row['title']!r} {row['w']}x{row['h']}")
    # 给它一点时间把前台拿过去（窗口出现 ≠ 已激活）
    _wait_for(lambda: backend.active_window_title() == row["title"], timeout=10)

    print("=== 3) 应用与窗口 ===")
    names = backend.list_apps()
    print(f"   应用 = {names}")
    _ok("list_apps 非空", bool(names), f"{len(names)} 个：{names}")
    _ok("list_apps 里有 notepad", "notepad" in names, f"{names}")

    rows = backend.list_windows(limit=10)
    for r in rows[:5]:
        print(f"     {r['id']} {r['app']!r} {r['title']!r} rect=({r['x']},{r['y']},"
              f"{r['w']}x{r['h']}) minimized={r['minimized']}")
    _ok("list_windows 非空", bool(rows), f"{len(rows)} 个")

    # ★ 交叉一致：list_apps 报的应用名必须真的出现在某个窗口行里
    #   （两边口径不一致会出现"存在但搜不到"，是最难查的一类问题）
    row_apps = {r["app"] for r in rows if r["app"]}
    _ok("list_apps ⊇ list_windows 里的 app",
        row_apps <= set(names) if row_apps else False,
        f"窗口里的 app 集合 {sorted(row_apps)}")

    layout = backend.screen_layout()
    print(f"   screen_layout = {json.dumps(layout, ensure_ascii=False)[:200]}")
    _ok("screen_layout 有显示器", bool(layout.get("monitors")),
        f"source={layout.get('source')}")

    print("=== 4) 读树（前台窗口）===")
    fg = backend.active_window_title()
    print(f"   前台窗口 = {fg!r}")
    _ok("前台窗口是记事本", fg == row["title"], f"读到 {fg!r}，期望 {row['title']!r}")

    res = backend.get_tree(scope="active_window", max_depth=8, max_nodes=300)
    print(f"   notice = {res.notice!r}")
    _ok("get_tree 返回了节点", bool(res.items), f"{len(res.items)} 棵树")

    def count(n):  # noqa: ANN001
        return 1 + sum(count(c) for c in n.children)

    total = sum(count(t) for t in res.items)
    print(f"   节点总数 = {total}")
    _ok("树非空", total > 0, f"{total} 个节点")

    def names_in(n):  # noqa: ANN001
        out = [n.name] if n.name else []
        for c in n.children:
            out.extend(names_in(c))
        return out

    all_names = [x for t in res.items for x in names_in(t)]
    print(f"   前 10 个名字 = {all_names[:10]}")
    _ok("树里找得到记事本的已知控件",
        any("Notepad" in n or "记事本" in n for n in all_names)
        or any("文本编辑器" in n or "Text editor" in n for n in all_names),
        f"共 {len(all_names)} 个有名字的节点")

    # ★ 交叉一致：树里标的 app 与 list_apps 的口径
    tree_apps = {t.app for t in res.items}
    _ok("树里的 app 标号与 list_apps 同口径",
        tree_apps <= set(names) if tree_apps else False,
        f"树 app={sorted(tree_apps)}")

    print("=== 5) find ===")
    found = backend.find(text="", role="button", app="notepad", limit=20)
    print(f"   notice = {found.notice!r}")
    _ok("find(role=button, app=notepad) 有结果", bool(found.items),
        f"{len(found.items)} 个" + (f"，例：{[e.name for e in found.items[:3]]}"
                                    if found.items else ""))

    print("=== 6) 元素级动作 vs 坐标点击（winver 的按钮）===")
    # winver 只有一个按钮，点了就关 —— 效果判据干净，且不依赖系统语言。
    #
    # ⚠️ **先清掉本脚本自己以前留下的对话框**（实测踩到，且极易误判成后端的 bug）：
    #    winver 的对话框每次都出现在**同一个位置**、同一个尺寸。脚本跑第二遍时，
    #    上一遍没关掉的（比如坐标点击那轮失败的）还堆在那儿，**新的那个被压在后面**；
    #    于是坐标点击打中的是最前面那个陈旧的，而判据看的是新那个 ——
    #    "点了、返回 True、但对话框没消失"，看着像坐标点击坏了，其实是 z-order。
    #    元素级不受影响（它认的是元素本身，不看谁在前面），这本身就说明了
    #    为什么元素级是第一原则。
    for h in _dialogs():
        try:
            ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
        except Exception:  # noqa: BLE001
            pass
    _wait_for(lambda: not _dialogs(), timeout=10)

    def dialog_button():  # noqa: ANN202
        """起 winver，返回 (hwnd, 按钮元素)；等不到就 (None, None)。"""
        before = _dialogs()
        subprocess.Popen(["winver.exe"])      # noqa: S603,S607

        def fresh():  # noqa: ANN202
            new = _dialogs() - before
            return max(new, key=_area) if new else None

        hwnd = _wait_for(fresh, timeout=25)
        if hwnd is None:
            return None, None
        # 按钮在对话框树的浅层，广度优先找第一个 ButtonControl
        el = backend._uia().ControlFromHandle(hwnd)
        queue, seen = [el], 0
        while queue and seen < 200:
            cur = queue.pop(0)
            seen += 1
            if str(getattr(cur, "ControlTypeName", "")) == "ButtonControl":
                return hwnd, cur
            queue.extend(cur.GetChildren() or [])
        return hwnd, None

    def assert_closed(hwnd: int, label: str, note: str) -> None:
        gone = _wait_for(lambda: not win32.is_window(hwnd), timeout=10, interval=0.25)
        _ok(label, bool(gone), note + f"，对话框 gone={bool(gone)}")

    # ---- ① 元素级：本项目的第一原则（零坐标、不受焦点/DPI/遮挡影响）----
    hwnd, btn = dialog_button()
    if hwnd is None:
        _ok("winver 对话框出现（元素级）", False, "等不到")
    else:
        _ok("winver 对话框出现（元素级）", True, f"0x{hwnd:x}")
        if btn is None:
            _ok("对话框里有按钮（元素级）", False)
        else:
            info = backend.element_info(btn)
            print(f"   按钮 {info.name!r} 动作={info.actions} states={info.states}")
            _ok("按钮暴露了 invoke 动作", "invoke" in (info.actions or ()),
                f"actions={info.actions}")
            print(f"   按钮 rect={backend.element_screen_rect(btn)}")
            ret = backend.invoke(btn)
            assert_closed(hwnd, "元素级 invoke 真的关掉了对话框（外部可观测）",
                          f"invoke 返回 {ret}")

    # ---- ② 坐标兜底：UIA 矩形中心 → SendInput（灰区应用唯一能走的那条）----
    hwnd, btn = dialog_button()
    if hwnd is None:
        _ok("winver 对话框出现（坐标级）", False, "等不到")
    else:
        _ok("winver 对话框出现（坐标级）", True, f"0x{hwnd:x}")
        if btn is None:
            _ok("对话框里有按钮（坐标级）", False)
        else:
            r = backend.element_screen_rect(btn)
            cx, cy = r.x + r.w // 2, r.y + r.h // 2
            print(f"   按钮 rect={r} → click_at({cx}, {cy})")
            clicked = backend.click_at(cx, cy, focus_window=False)
            assert_closed(hwnd, "坐标点击真的点掉了对话框（外部可观测）",
                          f"click_at 返回 {clicked}")

    def _read_value(el) -> str:  # noqa: ANN001
        try:
            return backend.element_info(el).value or ""
        except Exception:  # noqa: BLE001
            return ""

    def _read_until(el, pred, timeout: float = 10.0) -> str:  # noqa: ANN001, ANN202
        """
        反复**重新读**直到判据成立，返回最后一次读到的值。

        ⚠️ 为什么必须轮询：`SendInput` 只负责**把按键投递出去**，目标应用是**异步**
        处理它们的 —— `type_text` 返回时应用可能才处理了几个字符。实测第一次直接读
        只拿到 `'写入测试 h'`（5 个字符），而同一串文字最终完整落进了文档
        （记事本状态栏自己写着）—— **是读得太早，不是没写进去**。
        与"等窗口一律轮询"是同一条纪律：固定 sleep 要么白等要么不够。
        """
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = _read_value(el)
            if pred(last):
                return last
            time.sleep(0.2)
        return last

    print("=== 7) 注入：type_text / press_key（真机从没跑过的那两个）===")
    # ⚠️ 为什么单独一节、且必须真跑：`type_text` / `press_key` 正是 `w.` 那个未定义名
    #    bug 波及的 8 个方法之一。修完 NameError 只证明"不再炸"，**证明不了"注入真的到了"**——
    #    本项目最怕的就是"工具报 ok、目标应用没收到"。
    #
    # 判据两条，都是**外部可观测**的：
    #   ① 从目标应用**读回**文本（UIA 的 ValuePattern）。读的通道与写的通道
    #      （SendInput/KEYEVENTF_UNICODE）完全独立 —— 写成功读不到才算数。
    #   ② 记事本的状态栏从「未修改」变成「已修改」—— 这条**不看我们写的任何东西**，
    #      是应用自己说的话。两条一起才能说"文本真的进去了"。
    typed = "写入测试 hello 中文 123"

    def editor_and_state():  # noqa: ANN202
        """（编辑区元素, 状态文本）。状态文本是记事本自己标的「已修改/未修改」。"""
        res = backend.get_tree(scope="active_window", max_depth=8, max_nodes=300)
        ed, st = None, ""
        stack = list(res.items)
        while stack:
            n = stack.pop()
            if n.role in ("text", "entry", "document") and ed is None and n.name:
                ed = n.native
            if "未修改" in (n.name or "") or "已修改" in (n.name or ""):
                st = n.name
            stack.extend(n.children)
        return ed, st

    ed, state0 = editor_and_state()
    print(f"   编辑区 = {'有' if ed is not None else '**没找到**'}｜状态 = {state0!r}")
    _ok("找到记事本编辑区", ed is not None)

    if ed is not None:
        # 元素级聚焦：零坐标，不受前台/焦点漂移影响
        backend.focus(ed)
        ok = backend.type_text(typed)
        print(f"   type_text 返回 {ok}")
        # ① 从目标应用**读回**（读的通道与写的通道完全独立）
        got = _read_until(ed, lambda v: typed in v)
        _ok("读回的文本包含刚输入的内容（读通道独立于写通道）",
            typed in got, f"读回 {got[:60]!r}")
        # ② 应用自己说的
        _ed2, state1 = editor_and_state()
        _ok("记事本状态变成「已修改」（应用自己说的）",
            "已修改" in state1 and "未修改" not in state1, f"状态 {state1!r}")

        # ③ press_key：全选后覆写 —— 只有 ctrl+a 真的生效，结果才会被整段替换
        backend.press_key("ctrl+a")
        replacement = "REPLACED"
        backend.type_text(replacement)
        got2 = _read_until(ed, lambda v: v.strip() == replacement)
        _ok("press_key('ctrl+a') 真的全选了（旧内容被整段替换）",
            got2.strip() == replacement,
            f"读回 {got2[:60]!r}，期望恰好 {replacement!r}")

    print("=== 8) 未实现的部分必须**报错**而不是返回空 ===")
    for name in ("screenshot", "read_text"):
        try:
            getattr(backend, name)()
            _ok(f"{name} 抛错", False, "居然没抛")
        except Exception as exc:  # noqa: BLE001
            _ok(f"{name} 抛错", True, type(exc).__name__)

    return _finish(args)


def _finish(args) -> int:  # noqa: ANN001
    failed = [k for k, v in _RESULTS.items() if not v["ok"]]
    print(f"\n[story] {'✅ 全部通过' if not failed else '❌ 失败：' + str(failed)}")
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