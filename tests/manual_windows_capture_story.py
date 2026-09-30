# -*- coding: utf-8 -*-
"""按窗口渲染的真机 story（验收脚手架，跑在 Windows 客体上，不进 pytest）。

═══ 它验收什么 ═══

用户的原话是「**截图时能单独截取某个软件的界面，而不是截取全屏**」。
这条故事就把那件事钉死，判据全部取**外部可观测事实**：

  1. `screenshot(scope="auto")` 在 Windows 上拿到的是**目标窗口**的图，不是整屏
     —— 判据取 `meta` 里的 `capture`/`window_id` 与**图的实际尺寸**；
  2. **不受遮挡**：用另一扇窗把目标盖住，再截一次，拿到的仍是目标窗口的内容
     —— 判据是「图里能找到目标写的字」**且**「找不到遮挡窗写的字」；
     后者才是关键：老实现（裁屏幕上那块矩形）在这条上必然失败；
  3. `get_screen_text(scope="window")` 同样读得到被遮挡的目标窗口的文字；
  4. **最小化时如实报错**，且错误信息里带得走的原因（"窗口最小化了"），
     **绝不返回一张全黑图**（让模型以为"界面就是这样"是本项目最不能容忍的失败形态）；
  5. 全程**不动用户的指针与前台**（这是本次需求的正面判据）。

⚠️ 判据纪律：不看工具返回值，只看**外部可观测事实**（图里的内容、窗口状态、
指针/前台是否变化）。返回值只说"我做了"。

跑法（客体上，交互式会话内）：
    python tests\\manual_windows_capture_story.py --out results\\capture_story.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_HERE = pathlib.Path(__file__).resolve()
_REPO = next((p for p in _HERE.parents if (p / "src" / "computer_use_mcp").is_dir()), None)
if _REPO is not None:
    sys.path.insert(0, str(_REPO / "src"))

# `CreateProcess` 卡死的看门狗上限（见 spawn）
_SPAWN_TIMEOUT = 25.0

_RESULTS: dict[str, dict] = {}

# 进度直写文件、每行 flush。**为什么不能只靠 print**：上一轮真机跑的时候脚本卡在
# 某一步，而日志里停在节标题上、一个字都没有 —— 分不清是卡在 `taskkill`、`Popen`
# 还是窗口枚举。诊断这类"没有任何输出就卡住"的问题，得有一条不经过任何缓冲的通道。
_TRACE: pathlib.Path | None = None


def trace(msg: str) -> None:
    print(f"     [trace] {msg}", flush=True)
    if _TRACE is not None:
        try:
            with open(_TRACE, "a", encoding="utf-8") as fh:
                fh.write(f"[trace] {msg}\n")
                fh.flush()
        except OSError:
            pass


def ok(label: str, cond: bool, detail: str = "") -> bool:
    _RESULTS[label] = {"ok": bool(cond), "detail": detail}
    print(f"  {'OK  ' if cond else 'FAIL'} | {label}" + (f" | {detail}" if detail else ""),
          flush=True)
    return bool(cond)


def note(label: str, detail: str = "") -> None:
    print(f"  ·  {label}" + (f": {detail}" if detail else ""), flush=True)


def wait_for(pred, timeout: float, interval: float = 0.4, label: str = ""):  # noqa: ANN001, ANN202
    """轮询等待 —— **不用固定 sleep**（实测仓里为此吃过一次偶发红）。

    ⚠️ 每轮都 `trace` 一笔：真机上"卡住"与"在读窗口列表"从外面看一模一样，
    不把进度打出来就分不清是 `Popen` 卡了还是枚举卡了（上一轮就是这么白跑一轮的）。
    """
    end = time.time() + timeout
    n = 0
    while time.time() < end:
        n += 1
        trace(f"wait_for({label}) 第 {n} 轮：开始求值")
        got = pred()
        trace(f"wait_for({label}) 第 {n} 轮：= {bool(got)}")
        if got:
            return got
        time.sleep(interval)
    trace(f"wait_for({label}) 超时（{timeout}s）")
    return None


def kill(*names: str) -> None:
    """结束进程。**每个子进程都带超时** —— 无超时的 subprocess 一旦卡住就是整轮死等，
    而且什么都不会打印出来（上一轮真机就是这么卡住的）。"""
    for n in names:
        try:
            subprocess.run(["taskkill", "/F", "/IM", n],  # noqa: S603,S607
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=15)
        except subprocess.TimeoutExpired:
            trace(f"taskkill {n} 超时（已放弃，继续）")


def spawn(argv: list[str]) -> bool:
    """
    起一个应用，**带看门狗**：`Popen` 放进后台线程，主线程最多等 `_SPAWN_TIMEOUT`。

    ⚠️ 为什么要看门狗：真机上实测它**会卡住**（2026-09-30，僵在 `CreateProcess`
    里十几分钟、什么输出都没有）。`subprocess.Popen` 在 Windows 上对
    **Store 应用别名**（Win11 的 `notepad.exe` 就是）可能长时间不返回 ——
    这是"起个应用"这一步里最不该有的失败形态：整轮探针无声无息地死掉。

    ⚠️ 也**不要**给它重定向 stdout/stderr：实测带重定向时卡住、不带时正常。
    凭据只有「带重定向的那版卡死、不带的那版跑通」这一条对照，
    不代表已查明机制 —— 所以这里既不重定向、又保留看门狗兜底。
    """
    trace(f"spawn {argv!r}")
    done: list[bool] = []

    def _run() -> None:
        try:
            subprocess.Popen(argv)  # noqa: S603
            done.append(True)
        except Exception as exc:  # noqa: BLE001
            trace(f"spawn 抛异常：{type(exc).__name__}: {exc}")

    th = threading.Thread(target=_run, name="spawn", daemon=True)
    th.start()
    th.join(_SPAWN_TIMEOUT)
    if not done:
        trace(f"⚠️ spawn 超过 {_SPAWN_TIMEOUT}s 未返回（看门狗放行，继续跑）")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    global _TRACE
    if args.out:
        _TRACE = pathlib.Path(args.out).with_name("_capture_story_trace.log")
        try:
            _TRACE.write_text("", encoding="utf-8")
        except OSError:
            _TRACE = None

    print("=== 0) 就绪 ===", flush=True)
    trace("起步")
    from computer_use_mcp.backend.base import get_backend
    from computer_use_mcp.backend.windows.driver import _uia, apps, session_info, win32

    si = session_info.check()
    if not ok("会话可信（有真桌面）", bool(si.get("interactive")),
              f"session={si.get('session_id')} 窗口站={si.get('window_station')}"):
        return 3

    backend = get_backend()
    # ★ 坐标系必须先固定，否则跨阶段比较指针坐标是**无效判据**
    #   （见 tests/manual_windows_hidden_desktop_probe.py 记录的那一课：
    #    设 DPI 感知前后 GetCursorPos 返回的是虚拟化/物理两个空间，差 1.5 倍）
    _uia.ensure_dpi_awareness()
    note("DPI 感知", json.dumps(_uia.ensure_dpi_awareness(), ensure_ascii=False)[:120])

    trace("会话自检通过，取基线")
    cur0 = win32.get_cursor_pos() or (0, 0)
    fg0 = win32.foreground_window()
    title0 = win32.window_text(fg0) if fg0 else ""
    note("开场", f"指针={cur0} 前台={title0!r}")

    # ── 1) 起两扇记事本：目标 + 遮挡者 ──
    print("\n=== 1) 起目标窗口与遮挡窗口 ===", flush=True)
    trace("清理旧的记事本")
    kill("notepad.exe")
    time.sleep(1.2)
    trace("清理完成")
    TARGET_TOK = f"TARGET{os.getpid()}"
    COVER_TOK = f"COVER{os.getpid()}"

    spawn(["notepad.exe"])
    trace("spawn 返回了")
    rows = wait_for(lambda: [r for r in apps.window_rows(limit=200)
                             if r["app"] == "notepad"], 20.0, label="目标记事本")
    if not ok("目标记事本起来了", bool(rows), f"{rows}"):
        return 5
    target_hwnd = int(rows[0]["id"], 16)
    note("目标窗口", f"{rows[0]['id']} {rows[0]['title']!r} "
                     f"{rows[0]['w']}x{rows[0]['h']}")

    # 往目标里写字（用 UIA 值模式，读回是独立通道）
    from computer_use_mcp.backend.windows.driver import capture as cap_mod

    def value_node(root):  # noqa: ANN001, ANN202
        stack = [root]
        while stack:
            n = stack.pop()
            try:
                if isinstance(n.GetValuePattern().Value, str):
                    return n
            except Exception:  # noqa: BLE001
                pass
            try:
                stack.extend(n.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        return None

    uia = _uia.import_uia()
    tgt_ctrl = uia.ControlFromHandle(target_hwnd)
    node = wait_for(lambda: value_node(tgt_ctrl), 10.0)
    if node is None:
        ok("目标记事本可写", False, "找不到可写元素")
        return 5
    node.GetValuePattern().SetValue(TARGET_TOK)
    time.sleep(0.8)
    rb = value_node(tgt_ctrl)
    ok("目标窗口里确实写进了 token", rb is not None and TARGET_TOK in
       str(rb.GetValuePattern().Value), f"读回={str(rb.GetValuePattern().Value)[:40]!r}")

    # 遮挡者：第二扇记事本，写另一个 token，铺满屏幕盖住目标
    spawn(["notepad.exe"])
    trace("spawn 返回了")
    rows2 = wait_for(lambda: [r for r in apps.window_rows(limit=200)
                              if r["app"] == "notepad" and int(r["id"], 16) != target_hwnd],
                     20.0, label="遮挡记事本")
    if not ok("遮挡窗口起来了", bool(rows2), f"{rows2}"):
        return 5
    cover_hwnd = int(rows2[0]["id"], 16)
    cov_ctrl = uia.ControlFromHandle(cover_hwnd)
    # 最大化遮挡者（盖住目标），并把它的窗口提到前台
    win32.show_window(cover_hwnd, 3)      # SW_MAXIMIZE
    time.sleep(0.6)
    win32.set_foreground_window(cover_hwnd)
    time.sleep(0.6)
    cnode = wait_for(lambda: value_node(cov_ctrl), 10.0)
    if cnode is not None:
        try:
            cnode.GetValuePattern().SetValue(f"ZZ {COVER_TOK} ZZ\n" * 3)
        except Exception as exc:  # noqa: BLE001
            note("往遮挡窗写字失败", str(exc)[:80])
    time.sleep(0.8)
    cov_rect = win32.window_rect(cover_hwnd)
    tgt_rect = win32.window_rect(target_hwnd)
    covered = bool(cov_rect and tgt_rect and cov_rect[0] <= tgt_rect[0]
                   and cov_rect[1] <= tgt_rect[1]
                   and cov_rect[2] >= tgt_rect[2] and cov_rect[3] >= tgt_rect[3])
    ok("遮挡窗口真的把目标窗口盖住了（几何判据）", covered,
       f"遮挡={cov_rect} 目标={tgt_rect}")

    # ★ 「不打扰」的基线**只能在布置完成之后取**：上面为了搭出"被遮挡"这个局面，
    #   本脚本自己把遮挡窗提到了前台（那是**测试**的动作，不是被测路径的动作）。
    #   拿布置之前的快照当基线，这条判据测的就变成"脚本有没有布置场景"了。
    cur0 = win32.get_cursor_pos() or (0, 0)
    fg0 = win32.foreground_window()
    title0 = win32.window_text(fg0) if fg0 else ""
    trace(f"不打扰基线（布置后）：指针={cur0} 前台={title0!r}")

    # ── 2) 截图：默认 scope，必须只拿目标窗口 ──
    print("\n=== 2) screenshot(scope='auto') 只拿目标窗口 ===", flush=True)
    import io as _io

    from PIL import Image

    def ocr_texts(img, origin):  # noqa: ANN001, ANN202
        """把图喂给 OCR，返回识别到的文字（判据用「有没有出现某个词」）。"""
        try:
            return [b.text for b in backend.read_text_from_image(img, origin)]
        except Exception as exc:  # noqa: BLE001
            note("OCR 失败", f"{type(exc).__name__}: {str(exc)[:80]}")
            return []

    data, meta = backend.screenshot_window(f"0x{target_hwnd:x}")
    img = Image.open(_io.BytesIO(data))
    ok("按窗口截图拿到图", bool(data), f"{len(data)} bytes")
    ok("截图 meta 标明是「按窗口渲染」", meta.get("capture") == "print_window",
       f"capture={meta.get('capture')}")
    note("图尺寸 vs 屏幕", f"图={img.size} 目标窗={tgt_rect} "
                          f"屏幕度量={win32.system_metrics()}")
    # ★ 判据取**尺寸接近目标窗口**而不是屏幕 —— 这是"只截了那个软件"最直接的证据
    tw, th = tgt_rect[2] - tgt_rect[0], tgt_rect[3] - tgt_rect[1]
    ok("图是**目标窗口**那么大，不是整屏", abs(img.width - tw) <= 2
       and abs(img.height - th) <= 2, f"图 {img.size} vs 目标窗 {tw}x{th}")

    texts = ocr_texts(img, (tgt_rect[0], tgt_rect[1]))
    joined = " ".join(texts)
    ok("★ 图里有目标窗口写的内容（没被遮挡者顶掉）", TARGET_TOK in joined,
       f"认出 {len(texts)} 块；含目标 token={TARGET_TOK in joined}")
    ok("★ 图里**没有**遮挡窗口的内容", COVER_TOK not in joined,
       f"含遮挡 token={COVER_TOK in joined}")

    # ── 3) OCR 也走同一条通道 ──
    print("\n=== 3) 按窗口 OCR（遮挡无关）===", flush=True)
    got = backend.capture_window(f"0x{target_hwnd:x}")
    if got is not None:
        t2 = ocr_texts(got[0], got[1])
        j2 = " ".join(t2)
        ok("★ 按窗口 OCR 读到目标内容", TARGET_TOK in j2, f"含目标 token={TARGET_TOK in j2}")
        ok("★ 按窗口 OCR 没有读到遮挡者内容", COVER_TOK not in j2,
           f"含遮挡 token={COVER_TOK in j2}")
    else:
        ok("★ 按窗口 OCR 读到目标内容", False, "capture_window 返回 None")

    # ── 4) 最小化：必须如实报错，不能给黑图 ──
    print("\n=== 4) 最小化时如实报错（绝不返回黑图）===", flush=True)
    win32.show_window(target_hwnd, 6)     # SW_MINIMIZE
    time.sleep(0.8)
    err = ""
    img_min = None
    info_min = {}
    try:
        img_min, info_min = cap_mod.print_window(target_hwnd)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    note("最小化后 print_window", f"图={None if img_min is None else img_min.size} "
                                  f"err={err[:80]!r} nonblack={info_min.get('nonblack')}")
    ok("★ 最小化时**不返回图**", img_min is None or err != "",
       f"img={None if img_min is None else img_min.size}")
    reason = str(info_min.get("reason") or err)
    ok("★ 报错里带得走的原因（提到最小化）", ("最小化" in reason) or ("minimiz" in reason.lower()),
       f"reason={reason[:100]!r}")
    # 顺便：真去调 backend.screenshot_window 也必须抛错而不是给图
    threw = False
    try:
        backend.screenshot_window(f"0x{target_hwnd:x}")
    except Exception:  # noqa: BLE001
        threw = True
    ok("★ screenshot_window 在最小化时抛错而非返回黑图", threw, f"抛错={threw}")
    # ⚠️ 这里**刻意不调 `SW_RESTORE`**：它会**激活**窗口、把前台抢走，
    #    于是下面那条「前台没变」会被脚本自己弄红（`show_window(hwnd, 9)` = SW_RESTORE）。
    #    收尾统一在最后 `taskkill`，不需要先还原。

    # ── 5) 全程没动用户的指针与前台 ──
    print("\n=== 5) 用户桌面有没有被打扰 ===", flush=True)
    cur1 = win32.get_cursor_pos() or (0, 0)
    fg1 = win32.foreground_window()
    title1 = win32.window_text(fg1) if fg1 else ""
    ok("指针没动", tuple(cur0) == tuple(cur1), f"{tuple(cur0)} → {tuple(cur1)}")
    ok("前台没变", fg0 == fg1, f"{title0!r} → {title1!r}")

    # ── 6) 收尾 ──
    print("\n=== 6) 收尾 ===", flush=True)
    kill("notepad.exe")

    failed = [k for k, v in _RESULTS.items() if not v["ok"]]
    print(f"\n[story] 判据 {len(_RESULTS)} 条，失败 {len(failed)} 条", flush=True)
    for k in failed:
        print(f"  ❌ {k} — {_RESULTS[k]['detail']}", flush=True)
    if args.out:
        p = pathlib.Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"results": _RESULTS, "failed": failed},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  报告已写入 {p}", flush=True)
    return 0 if not failed else 5


if __name__ == "__main__":
    raise SystemExit(main())
