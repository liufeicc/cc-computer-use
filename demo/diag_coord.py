#!/usr/bin/env python3
"""
坐标漂移诊断（demo 第 3 步）

目标：找出 zenity 对话框按钮坐标返回 (126,22) 这种错误值的根因。
方法：把同一个元素在 AT-SPI 的两种坐标系（SCREEN / WINDOW）都读出来，
      再用 X11 窗口几何（xdotool/xwininfo）拿窗口真实屏幕位置做交叉验证。

运行：/usr/bin/python3 diag_coord.py
"""

import subprocess
import time

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi


def role(o):
    try:
        return Atspi.Role.get_name(o.get_role()).lower()
    except Exception:
        return '?'


def extents(o, coord_type):
    """读元素矩形，coord_type 取 Atspi.CoordType.SCREEN 或 WINDOW"""
    try:
        e = o.get_extents(coord_type)
        return (e.x, e.y, e.width, e.height)
    except Exception as err:
        return f"ERR:{err}"


def fmt(ext):
    if isinstance(ext, tuple):
        return f"x={ext[0]} y={ext[1]} w={ext[2]} h={ext[3]}"
    return ext


def walk(o, d=0, maxd=6):
    """打印树上每个节点的 SCREEN 与 WINDOW 两种矩形"""
    if d > maxd:
        return
    name = ''
    try:
        name = o.get_name() or ''
    except Exception:
        pass
    sc = extents(o, Atspi.CoordType.SCREEN)
    wi = extents(o, Atspi.CoordType.WINDOW)
    print(f"{'  '*d}{role(o)} | {name!r}")
    print(f"{'  '*d}  SCREEN: {fmt(sc)}")
    print(f"{'  '*d}  WINDOW: {fmt(wi)}")
    for i in range(o.get_child_count()):
        try:
            walk(o.get_child_at_index(i), d + 1, maxd)
        except Exception:
            pass


def window_geometry_x11():
    """用 xdotool 列出所有 zenity 窗口及其真实屏幕几何"""
    out = subprocess.run(
        ['xdotool', 'search', '--name', 'zenity'], capture_output=True, text=True)
    wins = out.stdout.split()
    result = []
    for wid in wins:
        g = subprocess.run(
            ['xdotool', 'getwindowgeometry', wid], capture_output=True, text=True)
        pos = subprocess.run(
            ['xdotool', 'getwindowgeometry', '--shell', wid], capture_output=True, text=True)
        result.append((wid, g.stdout.strip(), pos.stdout.strip()))
    return result


def main():
    print("=== 启动 zenity 对话框 ===")
    zen = subprocess.Popen(
        ['zenity', '--question', '--title', 'CoordDiag',
         '--text', '坐标诊断'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    desktop = Atspi.get_desktop(0)
    target = None
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        nm = ''
        try:
            nm = app.get_name() or ''
        except Exception:
            pass
        if 'zenity' in nm.lower():
            target = app
            break

    if not target:
        print("未找到 zenity 应用！")
        zen.terminate()
        return

    print("=== AT-SPI 树（SCREEN vs WINDOW 两种坐标）===")
    walk(target)

    print("\n=== X11 窗口真实几何（xdotool）===")
    for wid, g, pos in window_geometry_x11():
        print(f"窗口 XID={wid}")
        print(g)
        # 提取 position 和 geometry 关键行
        for line in pos.splitlines():
            if line.startswith(('X=', 'Y=', 'WIDTH=', 'HEIGHT=')):
                print("  " + line)

    zen.terminate()
    print("\n=== 已清理 zenity ===")


if __name__ == '__main__':
    main()
