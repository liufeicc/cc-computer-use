#!/usr/bin/env python3
"""
AT-SPI 元素矩形 + 动作能力探测（demo 第二步）

用途：验证两个关键能力，它们是「点不准」问题的解药：
1. 元素精确矩形（bounding rect）——操作系统自己算好的坐标，天然含 DPI 缩放/窗口偏移
2. 元素级动作（Action 接口）——对元素直接 invoke，鼠标根本不动，零坐标误差

运行方式：/usr/bin/python3 probe_element.py [应用名关键词]
不带参数则打印所有有窗口的应用名，方便挑选目标。
"""

import sys

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

# 可交互角色（有操作意义的），用于过滤掉纯装饰的 panel/label
INTERACTIVE_ROLES = {
    'push button', 'toggle button', 'menu item', 'menu', 'check box',
    'radio button', 'text', 'entry', 'combo box', 'list item', 'list',
    'table cell', 'slider', 'scroll bar', 'spin button', 'hyperlink',
    'tab', 'tree item', 'page tab', 'tool button', 'split pane',
}


def role_name(obj):
    try:
        return Atspi.Role.get_name(obj.get_role()).lower()
    except Exception:
        return 'unknown'


def get_actions(obj):
    """返回元素可执行动作的名字列表"""
    actions = []
    try:
        n = obj.get_n_actions()
        for i in range(n):
            actions.append(obj.get_action_name(i))
    except Exception:
        pass
    return actions


def get_bbox(obj):
    """返回元素的屏幕矩形 (x, y, w, h)，失败返回 None"""
    try:
        ext = obj.get_extents(Atspi.CoordType.SCREEN)
        return (ext.x, ext.y, ext.width, ext.height)
    except Exception:
        return None


def list_apps(desktop):
    apps = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app.get_child_count() == 0:
            continue
        try:
            apps.append(app.get_name() or '(无名字)')
        except Exception:
            apps.append('(异常)')
    return apps


def collect(obj, keyword, results, depth=0, max_depth=8):
    """递归收集匹配的可交互元素"""
    if depth > max_depth:
        return
    role = role_name(obj)
    try:
        name = obj.get_name() or ''
    except Exception:
        name = ''

    if role in INTERACTIVE_ROLES and name:
        results.append(obj)

    for i in range(obj.get_child_count()):
        try:
            collect(obj.get_child_at_index(i), keyword, results, depth + 1, max_depth)
        except Exception:
            continue


def main():
    desktop = Atspi.get_desktop(0)

    if len(sys.argv) < 2:
        print("当前有窗口的应用：")
        for name in list_apps(desktop):
            print(f"  - {name}")
        print("\n用法: /usr/bin/python3 probe_element.py <应用名关键词>")
        return

    keyword = sys.argv[1]

    # 找到匹配的应用
    target_app = None
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        try:
            name = app.get_name() or ''
        except Exception:
            name = ''
        if keyword in name:
            target_app = app
            break

    if target_app is None:
        print(f"未找到名字含 '{keyword}' 的应用")
        print("可用应用:", list_apps(desktop))
        return

    print(f"=== 目标应用: {target_app.get_name()} ===\n")

    # 收集所有可交互且带名字的元素
    results = []
    collect(target_app, keyword, results)

    print(f"找到 {len(results)} 个可交互元素（角色 + 名字 + 矩形 + 动作）：\n")
    shown = 0
    for el in results:
        bbox = get_bbox(el)
        actions = get_actions(el)
        name = el.get_name()
        role = role_name(el)
        bbox_str = f"({bbox[0]},{bbox[1]},{bbox[2]}x{bbox[3]})" if bbox else "(无矩形)"
        center = ""
        if bbox:
            center = f" 中心=({bbox[0]+bbox[2]//2},{bbox[1]+bbox[3]//2})"
        act_str = ",".join(actions) if actions else "(无动作)"
        print(f"[{shown}] {role} | {name}")
        print(f"     矩形={bbox_str}{center}")
        print(f"     动作={act_str}")
        shown += 1
        if shown >= 40:
            print(f"\n... (还有 {len(results)-shown} 个未显示)")
            break


if __name__ == '__main__':
    main()
