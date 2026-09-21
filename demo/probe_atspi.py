#!/usr/bin/env python3
"""
AT-SPI 无障碍树读取探测脚本（demo 第一步）

用途：验证能否从操作系统无障碍接口读到真实桌面的窗口元素树。
这是整个「无障碍优先」方案的第一个技术前提。

运行方式：/usr/bin/python3 probe_atspi.py
（必须用系统 python，anaconda 的 python 没有 gi 模块）
"""

import sys

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi


def role_name(obj):
    """把 Atspi.Role 枚举转成可读字符串"""
    try:
        return Atspi.Role.get_name(obj.get_role()).lower()
    except Exception:
        return 'unknown'


def describe(obj):
    """用一行文本描述一个元素节点（紧凑、省 token 的雏形）"""
    try:
        name = obj.get_name() or ''
    except Exception:
        name = ''
    try:
        role = role_name(obj)
    except Exception:
        role = '?'
    return f"{role} | {name}"


def walk(obj, depth=0, max_depth=6, max_nodes=300):
    """递归遍历子树，返回节点描述列表"""
    if depth > max_depth or len(walk.result) >= max_nodes:
        return
    walk.result.append("  " * depth + describe(obj))

    for i in range(obj.get_child_count()):
        try:
            child = obj.get_child_at_index(i)
        except Exception:
            continue
        walk(child, depth + 1, max_depth, max_nodes)


def main():
    desktop = Atspi.get_desktop(0)
    print("=== 桌面上的应用数量:", desktop.get_child_count(), "===\n")

    # 遍历每个应用，只深入打印前 N 个应用的树
    shown_apps = 0
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        try:
            app_name = app.get_name() or '(无名字)'
        except Exception:
            app_name = '(异常)'
        # 跳过无窗口的应用
        if app.get_child_count() == 0:
            continue

        shown_apps += 1
        if shown_apps > 5:
            print(f"... (共 {desktop.get_child_count()} 个应用，仅显示前 5 个有窗口的)\n")
            break

        print(f"### 应用 [{shown_apps}]: {app_name}  (顶层窗口数={app.get_child_count()})")

        for w in range(app.get_child_count()):
            win = app.get_child_at_index(w)
            walk.result = []
            walk(win)
            lines = "\n".join(walk.result)
            print(f"  --- 窗口 {w}: {describe(win)} ---")
            print(lines[:2000])  # 每个窗口最多打印 2000 字符
            print()


if __name__ == '__main__':
    main()
