#!/usr/bin/env python3
"""
元素级操作 vs 坐标点击 对比验证（demo 第 5 步 · 核心）

上一步结论：坐标校准公式已正确（鼠标精确命中窗口），但后台启动的
对话框无输入焦点，导致「点击」只激活窗口、不触发按钮。

本步验证两条执行路径，对比优劣：
  路径A【元素级操作】：调用 AT-SPI 的 Action 接口 do_action('click')
                       → 鼠标不动、零坐标、不受焦点影响（终极方案）
  路径B【坐标点击】  ：先 windowfocus 激活窗口，再 mousemove+click
                       → 需要坐标校准 + 焦点处理（兜底方案）

运行：/usr/bin/python3 action_vs_coord.py [A|B]
  不带参数默认先跑 A（元素级），失败再自动跑 B（坐标）。
"""

import subprocess
import sys
import time

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

TITLE = 'ActionVsCoord'


def role_name(o):
    try:
        return Atspi.Role.get_name(o.get_role()).lower()
    except Exception:
        return '?'


def find_button(root, name_kw, depth=0, maxd=8):
    if depth > maxd:
        return None
    if role_name(root) == 'push button':
        nm = ''
        try:
            nm = root.get_name() or ''
        except Exception:
            pass
        if name_kw in nm:
            return root
    for i in range(root.get_child_count()):
        try:
            r = find_button(root.get_child_at_index(i), name_kw, depth + 1, maxd)
        except Exception:
            r = None
        if r:
            return r
    return None


def find_app(desktop, kw):
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        nm = ''
        try:
            nm = app.get_name() or ''
        except Exception:
            pass
        if kw in nm:
            return app
    return None


def list_actions(el):
    """列出元素所有 action 名（含索引）"""
    acts = []
    try:
        n = el.get_n_actions()
        for i in range(n):
            try:
                acts.append((i, el.get_action_name(i)))
            except Exception:
                acts.append((i, '?'))
    except Exception:
        pass
    return acts


def start_zenity():
    subprocess.run(['pkill', '-9', '-f', 'zenity'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    zen = subprocess.Popen(
        ['zenity', '--question', '--title', TITLE, '--text', '元素级 vs 坐标 验证'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    return zen


def path_a_element_action(zen):
    """路径A：元素级 do_action，零坐标"""
    print("\n═══ 路径A：元素级操作（do_action）═══")
    desktop = Atspi.get_desktop(0)
    app = find_app(desktop, 'zenity')
    if not app:
        print("  FAIL: 未找到 zenity 应用")
        return False
    btn = find_button(app, '是')
    if not btn:
        print("  FAIL: 未找到「是」按钮")
        return False

    acts = list_actions(btn)
    print(f"  「是」按钮可用 actions: {acts}")

    # 找 click / activate 类动作
    target_idx = None
    for idx, name in acts:
        if name in ('click', 'activate', 'press'):
            target_idx = idx
            break
    if target_idx is None and acts:
        target_idx = acts[0][0]

    if target_idx is None:
        print("  FAIL: 按钮无任何 action")
        return False

    print(f"  → 调用 do_action({target_idx}) = '{acts[target_idx][1]}'（鼠标不动、无坐标）")
    ok = btn.do_action(target_idx)
    print(f"  do_action 返回: {ok}")
    return ok


def path_b_coord_click(zen):
    """路径B：先聚焦窗口，再坐标点击"""
    print("\n═══ 路径B：坐标点击（windowfocus + mousemove + click）═══")
    # 找窗口 XID
    out = subprocess.run(['xdotool', 'search', '--pid', str(zen.pid), '--name', TITLE],
                         capture_output=True, text=True)
    wids = out.stdout.split()
    if not wids:
        out = subprocess.run(['xdotool', 'search', '--pid', str(zen.pid)],
                             capture_output=True, text=True)
        wids = out.stdout.split()
    if not wids:
        print("  FAIL: 未找到窗口")
        return False
    wid = wids[-1]

    # 关键修复：先激活并聚焦窗口
    subprocess.run(['xdotool', 'windowactivate', '--sync', wid], capture_output=True)
    subprocess.run(['xdotool', 'windowfocus', '--sync', wid], capture_output=True)
    time.sleep(0.3)

    # 拿窗口位置 + 按钮相对坐标
    g = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                       capture_output=True, text=True)
    geom = dict(l.split('=', 1) for l in g.stdout.splitlines() if '=' in l)
    wx, wy = int(geom.get('X', 0)), int(geom.get('Y', 0))

    desktop = Atspi.get_desktop(0)
    app = find_app(desktop, 'zenity')
    btn = find_button(app, '是') if app else None
    if not btn:
        print("  FAIL: 未找到按钮")
        return False
    e = btn.get_extents(Atspi.CoordType.WINDOW)
    abs_x = wx + e.x + e.width // 2
    abs_y = wy + e.y + e.height // 2
    print(f"  窗口({wx},{wy}) + 按钮相对({e.x},{e.y}) → 绝对({abs_x},{abs_y})")

    subprocess.run(['xdotool', 'mousemove', str(abs_x), str(abs_y)], check=True)
    time.sleep(0.2)
    subprocess.run(['xdotool', 'click', '1'], check=True)
    print(f"  已聚焦窗口并点击 ({abs_x},{abs_y})")
    return True


def main():
    mode = sys.argv[1].upper() if len(sys.argv) > 1 else 'AUTO'

    zen = start_zenity()
    try:
        if mode in ('A', 'AUTO'):
            ok = path_a_element_action(zen)
            if ok:
                # 等 zenity 退出验证
                try:
                    code = zen.wait(timeout=4)
                    print(f"\n  zenity 退出码={code} → {'✅ 元素级操作成功触发按钮！' if code==0 else '⚠️ 已退出但码非0'}")
                    return
                except subprocess.TimeoutExpired:
                    print("  元素级 do_action 后 zenity 未退出，尝试路径B...")
            elif mode == 'A':
                print("  路径A 失败")
                return

        if mode in ('B', 'AUTO'):
            path_b_coord_click(zen)
            try:
                code = zen.wait(timeout=4)
                print(f"\n  zenity 退出码={code} → {'✅ 坐标点击（聚焦后）成功！' if code==0 else '❌ 仍失败'}")
            except subprocess.TimeoutExpired:
                zen.terminate()
                print("\n  ❌ 坐标点击后 zenity 仍未退出")
    finally:
        if zen.poll() is None:
            zen.terminate()


if __name__ == '__main__':
    main()
