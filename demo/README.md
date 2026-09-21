# Computer Use Demo（无障碍 + 输入注入 可行性验证）

本目录是「AGENT 直接使用电脑」方案的**技术可行性 Demo**，用最小代码验证两个核心假设：

1. 能否通过**无障碍接口（AT-SPI）**读到真实桌面的结构化元素树（而非截图）→ 解决「截图费 token」
2. 能否**精确操作元素**（而非 LLM 视觉估算坐标）→ 解决「鼠标点不准」

**最终结论：两个假设都验证通过，且发现「元素级操作」远优于「坐标点击」。**

---

## 一、环境要求（当前机器已验证）

| 项 | 值 | 说明 |
|----|----|----|
| 会话类型 | **X11**（`DISPLAY=:1`） | Wayland 下全局输入注入受限，X11 最省事 |
| Python | **必须用 `/usr/bin/python3`** | ⚠️ anaconda 的 python 没有 `gi` 模块，跑不了 AT-SPI |
| AT-SPI 绑定 | 系统已装 `gir1.2-atspi-2.0` | 用 `gi.require_version('Atspi','2.0')` 访问 |
| 无障碍开关 | 需打开（见下） | 默认 false，GTK 应用不上报树 |
| 鼠标注入 | `xdotool`（XTest 通道）可用 | uinput 需 root/udev 规则，demo 用 xdotool 替代 |

**一次性环境准备：**
```bash
# 打开无障碍开关（必须，否则读不到 GTK 应用的树）
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

> ⚠️ 坑：`python-xlib` 装在 anaconda 环境，但 `gi`(Atspi) 只在系统 python。两者不互通，所以本 demo 全程用 `/usr/bin/python3` + xdotool，不依赖 Xlib。

---

## 二、脚本说明（按验证顺序）

| 脚本 | 作用 | 用法 |
|------|------|------|
| `probe_atspi.py` | dump 整张桌面的无障碍树（验证「能读到结构」） | `/usr/bin/python3 probe_atspi.py` |
| `probe_element.py` | 列出某应用的可交互元素 + 矩形 + Action | `/usr/bin/python3 probe_element.py <应用名>` |
| `diag_coord.py` | 诊断坐标漂移：对比 SCREEN/WINDOW 两种坐标系 | `/usr/bin/python3 diag_coord.py` |
| `calib_click.py` | 坐标校准：窗口绝对位置 + 元素相对坐标 | `/usr/bin/python3 calib_click.py` |
| `action_vs_coord.py` | **核心对比**：元素级操作 vs 坐标点击 | `/usr/bin/python3 action_vs_coord.py [A\|B]` |

---

## 三、验证结论（2026-09-08 实测）

### ✅ 结论 1：无障碍树可读，元素级 Action 大量存在

- 能读到桌面 18 个应用，逐个窗口 dump 出 `角色 | 名字 | 矩形` 结构，**全程无截图**。
- 全桌面扫到 31+ 个带 Action 接口的元素。**GTK 应用（文本编辑器、Nautilus、DBeaver、zenity）暴露了极丰富的语义 action**：`page.save`、`view.new-folder`、`window.close`、`click` 等，可被 AGENT 直接调用。
- Chrome 菜单也暴露 `click` action。

### ⚠️ 结论 2：坐标会「漂移」——这正是「点不准」的根因

`diag_coord.py` 实测发现：GTK 独立对话框（zenity）的元素，AT-SPI 的 `get_extents(SCREEN)` 返回 **(0,0) 起算的相对坐标**，而非屏幕绝对坐标——它把「窗口自己的左上角」当成了屏幕原点，没叠加窗口在虚拟桌面（本机双屏 3840×1200）上的真实位置。

**校准公式**（`calib_click.py` 验证）：
```
屏幕绝对坐标 = 窗口真实屏幕位置(xdotool 按 PID 查) + 元素 WINDOW 相对坐标(AT-SPI)
```
实测：窗口 (1909,115) + 按钮相对中心 (239,185) = 绝对 (2148,300)，鼠标移动后 `getmouselocation` 确认 `WINDOW=<zenity窗口>`，**坐标校准正确、鼠标精确命中**。

> 工程要点：必须用 `xdotool search --pid <PID>` 锁定本次进程窗口，否则会被残留同名窗口（级联偏移 50px）和「后台启动无焦点」干扰。

### 🏆 结论 3（核心）：元素级操作 完胜 坐标点击

`action_vs_coord.py` 同一个「点击 zenity『是』按钮」任务，两条路对比：

| 路径 | 方式 | 结果 |
|------|------|------|
| **A 元素级** | `btn.do_action('click')`，鼠标不动、零坐标 | ✅ **成功**，zenity 退出码=0 |
| **B 坐标点击** | 校准坐标(2148,300) + 先 `windowfocus` + `xdotool click` | ❌ **失败**，对话框不响应 |

**关键洞察**：路径 B 的坐标**算对了**（鼠标已精确落在按钮上），但点击仍不触发——因为 GTK 对话框对**合成鼠标事件**有特殊处理（首次点击被「激活窗口」吃掉、事件未正确路由到 widget）。

而路径 A 的 `do_action` 直接走无障碍接口，**100% 命中、零坐标误差、不受焦点/分辨率/DPI 影响**。

→ **这从根本上验证了方案主张：执行层应「元素级操作优先，坐标点击仅兜底」。** 用户的痛点②「点不准」在元素级操作下**不复存在**，因为压根不产生坐标。

---

## 四、对总体方案的印证

| 用户痛点 | Demo 验证结果 |
|---------|--------------|
| ① 截图费 token | ✅ 无障碍树是结构化文本，全程无截图即可定位并操作元素 |
| ② 鼠标点不准 | ✅ 元素级 `do_action` 零坐标，从物理上消除误差；坐标点击即使校准精确仍不可靠 |

**仍存在的灰区**（需后续处理）：
- 部分窗口 SCREEN 坐标漂移 → 需坐标校准层（已验证公式）
- 自绘控件/游戏/远程桌面无元素树 → 需截图或视觉解析兜底
- uinput 注入未验证（当前用 xdotool/XTest 替代，X11 下够用）

---

## 五、快速复现

```bash
# 0. 一次性：打开无障碍开关
gsettings set org.gnome.desktop.interface toolkit-accessibility true

# 1. 读桌面树
/usr/bin/python3 probe_atspi.py

# 2. 读某应用元素（应用名见上一步输出）
/usr/bin/python3 probe_element.py "gnome-text-editor"

# 3. 诊断坐标漂移
/usr/bin/python3 diag_coord.py

# 4. 坐标校准点击
/usr/bin/python3 calib_click.py

# 5. 核心对比：元素级(A) vs 坐标(B)
/usr/bin/python3 action_vs_coord.py A   # 元素级 → 成功
/usr/bin/python3 action_vs_coord.py B   # 坐标点击 → 失败（对照组）
```
