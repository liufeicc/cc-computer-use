#!/usr/bin/env python3
"""
坐标校准层 + 端到端点击验证（demo 第 4 步 · 最终版）

═══════════════════════════════════════════════════════════
根因（demo 第 3 步诊断结论）：
  GTK 应用（如 zenity 对话框）通过 AT-SPI 上报的元素 SCREEN 坐标
  返回的是「相对窗口原点」的值（实测 (0,0)），未叠加窗口在
  虚拟桌面上的真实位置 → 直接拿来点击必然点偏。

校准公式：
  屏幕绝对坐标 = 窗口真实屏幕位置(左上角) + 元素 WINDOW 相对坐标

关键工程点（前几次失败教训）：
  1. 用 `xdotool search --pid <PID>` 精确锁定本次启动的进程窗口，
     排除残留同名窗口（级联偏移 50px 的坑）。
  2. 不依赖 getactivewindow（后台 Popen 启动的对话框无输入焦点）。
  3. 窗口面积过滤：排除 1x1 的隐形辅助窗口。
═══════════════════════════════════════════════════════════

运行：/usr/bin/python3 calib_click.py
"""

import subprocess
import sys
import time

import gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi

TITLE = 'CalibDemoFinal'


def role_name(o):
    try:
        return Atspi.Role.get_name(o.get_role()).lower()
    except Exception:
        return '?'


def find_element(root, want_role, name_kw, depth=0, maxd=8):
    """在 AT-SPI 树里找第一个 角色匹配 + 名字含关键词 的元素"""
    if depth > maxd:
        return None
    if role_name(root) == want_role:
        nm = ''
        try:
            nm = root.get_name() or ''
        except Exception:
            pass
        if name_kw in nm:
            return root
    for i in range(root.get_child_count()):
        try:
            r = find_element(root.get_child_at_index(i), want_role, name_kw, depth + 1, maxd)
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


def element_window_center(el):
    """元素窗口内相对坐标的中心点（AT-SPI WINDOW 坐标系，实测可信）"""
    e = el.get_extents(Atspi.CoordType.WINDOW)
    return (e.x + e.width // 2, e.y + e.height // 2), (e.x, e.y, e.width, e.height)


def window_screen_pos_by_pid(pid):
    """
    用 xdotool 按 PID 精确找窗口，返回其屏幕左上角 (x, y)。
    - --pid 锁定本次进程，排除残留同名窗口
    - 过滤 1x1 隐形辅助窗口，取面积最大的真实窗口
    """
    out = subprocess.run(
        ['xdotool', 'search', '--pid', str(pid), '--name', TITLE],
        capture_output=True, text=True)
    wids = out.stdout.split()
    if not wids:
        # 退化：只按 pid 搜（不带 name）
        out = subprocess.run(['xdotool', 'search', '--pid', str(pid)],
                             capture_output=True, text=True)
        wids = out.stdout.split()

    best = None
    best_area = 0
    for wid in wids:
        g = subprocess.run(['xdotool', 'getwindowgeometry', '--shell', wid],
                           capture_output=True, text=True)
        geom = {}
        for line in g.stdout.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                if k in ('X', 'Y', 'WIDTH', 'HEIGHT'):
                    geom[k] = int(v)
        area = geom.get('WIDTH', 0) * geom.get('HEIGHT', 0)
        if area > best_area:
            best_area = area
            best = (geom.get('X', 0), geom.get('Y', 0), geom.get('WIDTH', 0), geom.get('HEIGHT', 0))
    return best


def main():
    # 0. 清理残留
    subprocess.run(['pkill', '-9', '-f', 'zenity'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)

    # 1. 启动 zenity，记录 PID
    zen = subprocess.Popen(
        ['zenity', '--question', '--title', TITLE, '--text', '校准点击最终验证'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid = zen.pid
    time.sleep(2.5)  # 等窗口完全 mapped

    try:
        desktop = Atspi.get_desktop(0)
        app = find_app(desktop, 'zenity')
        if not app:
            print("FAIL: AT-SPI 未找到 zenity 应用")
            zen.terminate()
            sys.exit(1)

        # 2. 找「是」按钮，取窗口内相对中心
        btn = find_element(app, 'push button', '是')
        if not btn:
            print("FAIL: 未找到「是」按钮")
            zen.terminate()
            sys.exit(1)
        rel_center, rel_rect = element_window_center(btn)
        print(f"① AT-SPI「是」按钮 WINDOW 相对矩形: {rel_rect}  相对中心: {rel_center}")

        # 3. 按 PID 精确拿窗口真实屏幕位置
        win = window_screen_pos_by_pid(pid)
        if not win:
            print("FAIL: xdotool 未找到窗口")
            zen.terminate()
            sys.exit(1)
        wx, wy, ww, wh = win
        print(f"② 窗口真实屏幕位置(左上角): ({wx},{wy})  尺寸: {ww}x{wh}")

        # 4. 校准：屏幕绝对 = 窗口位置 + 元素相对
        abs_x = wx + rel_center[0]
        abs_y = wy + rel_center[1]
        print(f"③ 校准后「是」按钮屏幕绝对坐标: ({abs_x},{abs_y})")

        # 5. 移动鼠标并点击
        subprocess.run(['xdotool', 'mousemove', str(abs_x), str(abs_y)], check=True)
        time.sleep(0.2)
        # 读回鼠标实际位置做自检
        mp = subprocess.run(['xdotool', 'getmouselocation', '--shell'],
                            capture_output=True, text=True).stdout
        print(f"④ 鼠标已移动到: {mp.strip().replace(chr(10),' ')}")
        subprocess.run(['xdotool', 'click', '1'], check=True)
        print(f"⑤ 已点击 ({abs_x},{abs_y})")

    finally:
        # 6. 验证退出码
        try:
            code = zen.wait(timeout=5)
        except subprocess.TimeoutExpired:
            zen.terminate()
            print("\n❌ zenity 5 秒未退出 → 点击未命中按钮（校准仍失败）")
            sys.exit(1)
        print(f"\nzenity 退出码 = {code}")
        if code == 0:
            print("✅ 成功：点到了「是」按钮 —— 坐标校准公式验证通过！")
        elif code == 1:
            print("❌ 失败：点到了「否」按钮 —— 校准坐标仍有偏差")
        else:
            print(f"⚠️  异常退出码 {code}")


if __name__ == '__main__':
    main()
