# cc-computer-use —— Computer-Use MCP Server

**中文** | [English](README.md)

让 AGENT（Claude Code 等）**直接使用 Linux 桌面**的 MCP server —— 而且**不抢你的鼠标**。

别的 computer-use server 都做同一件事：运行期间**接管**你的鼠标、键盘和屏幕。本项目默认把
Agent 放进**它自己的一块私有虚拟屏**，你可以同时继续用电脑。确实需要它操作真实桌面时，
一个环境变量即可切换。

| | 传统 computer-use MCP | 本项目 |
|---|---|---|
| Agent 工作时 | 鼠标/键盘/屏幕被**独占**，你没法用电脑 | **零干扰** —— Agent 在私有 Xephyr 虚拟屏里 |
| 隔离 Agent | 得自己搭 VM / 容器 | **默认开启**，无需额外配置 |
| 沙箱不可用 | 静默回落到你的真实桌面 | **拒绝执行**并说明原因 |
| 无障碍树 | 与你的桌面共用一条 AT-SPI 总线 | **私有 AT-SPI 总线**，宿主总线看不到沙箱内应用 |

> Anthropic 官方的 computer-use 参考实现原话：跑在虚拟机之外是 *"strongly discouraged"*，
> 且 *"no safeguards"*（没有任何防护）。本项目认真对待这一点 —— 隔离在这里是**默认值**，
> 而不是一个你得自己配对的选项。

## 感知方式

感知走**无障碍接口（AT-SPI）结构化读取**、操作走**元素级动作**，替代传统「截图 + 坐标点击」，
再从根上解决另外两个问题：

| 痛点 | 传统方案 | 本项目方案 |
|------|----------|-----------|
| 截图费 token | 每轮截图 1000+ token | 读无障碍树 / OCR 文本（都是紧凑文本，省 token） |
| 鼠标点不准 | LLM 从截图估算坐标，DPI/多屏层层丢精度 | 元素级 `do_action`，**零坐标**、不受焦点/分辨率/DPI 影响 |

> 可行性由 [`demo/`](demo/README.md) 实测确立：**元素级操作完胜坐标点击**——本项目执行层即「元素级优先，坐标点击仅兜底」。
>
> 完整部署步骤与故障排查见 [`docs/安装说明.md`](docs/安装说明.md)。

---

## 一、能力概览（17 个 MCP 工具）

| 工具 | 作用 |
|------|------|
| `get_ui_tree` | 读无障碍元素树（紧凑文本，每个可操作元素带 `[ref]`）——**有树应用的核心感知** |
| `get_screen_text` | **OCR 读屏幕文字及其坐标**（`[ref] 文字 @ (x,y)`）——**灰区应用的核心感知**，比截图省得多。默认 `scope="window"` 只识别活动窗口：对话框 0.3~3s，全屏密集文字要 8~10s |
| `find_element` | 按文本/角色/应用搜元素，返回带 `ref` 的候选列表 |
| `element_info` | 查单个元素详情：值/状态/矩形/可用 actions |
| `click` | 点击：**三级降级** 元素级 `do_action` → 校准坐标点击 → 截图兜底；灰区可传裸坐标 `x+y` 直点。**坐标级点击自带落点反馈**：落点证据（哪扇窗、底下什么字、点后活动窗口）+ **点击前准星小图**（红十字 = 实际点的像素）+ **程序算好的偏差**（`expect="保存"` → 「未命中，偏差 (+17,-42) 共45px，建议改点其中心 (517,258)」）+ **点后界面变化**百分比。据此判断有没有点偏、该改点哪，**不必再截图估算**；`preview=false` 可关掉这套画面反馈 |
| `get_last_click_image` | **回看**最近一次坐标点击的准星小图与结论（`index=-1` 最近、`-2` 上上次）。点击后界面没反应时先用它看清「刚才点在哪、偏了多少、该点哪」，**别盲目重复点击** |
| `type_text` | 输入：元素级 `set_value` 优先，键盘注入兜底。键盘注入会回报输入后的活动窗口（焦点被抢走时一眼可见） |
| `press_key` | 快捷键（如 `ctrl+s`、`alt+F4`），回报按键后的活动窗口 |
| `act_sequence` | 一次调用批量执行一串动作（click/type/key/wait/sleep/list_windows/**ui_tree**/**screenshot**），**省往返的主力**。最后一步放 `ui_tree` 或 `screenshot`，可**在同一次调用里**拿到「做完之后界面长什么样」。`click` 还支持**候选名列表**（`["保存","Save"]`）：按顺序找第一个真实存在的就点，后面的不再试 |
| `double_click` | **双击**（**只能走坐标级**——元素级动作没有双击语义，所以要给屏幕绝对坐标）。两次点击的间隔由服务端定时（100ms，远小于系统约 400ms 的阈值），因此**必定构成双击**；用两次 `click` 代替则永远不是 |
| `scroll` | 在指定点滚动。`direction` 为 up/down，`amount` 是**刻度数**（一次滚轮事件 = 一格，没有半格）。⚠️ 一格滚多远由**应用**决定（GTK 约 3 行），所以正解是「滚一下 → 看结果 → 不够再滚」，配合 `act_sequence` 与 `ui_tree`/`screenshot` 放进同一次调用 |
| `drag` | 从 `(from_x,from_y)` 拖到 `(to_x,to_y)`。中间自动插值出一串连续移动——直接瞬移的话，很多应用判定不出「按住拖动」，表现为拖了但没反应 |
| `launch_app` | 在目标 display 启动应用（隔离模式=把应用放进沙箱的正路） |
| `list_windows` | 可见窗口清单（id/标题/PID/几何，按面积降序） |
| `wait_window` | 等窗口标题满足条件（server 内轮询，单次调用完成） |
| `get_screen_layout` | 显示器布局 + 虚拟桌面尺寸 |
| `screenshot` | 截图（**最后兜底**，默认 JPEG；确实需要像素级判断时才用） |

### 推荐工作流

- **有元素树的应用**：`launch_app` 起应用 → `get_ui_tree` 看结构 → 找到目标 `[ref]`（或 `find_element` 搜）→ `click(ref)` / `type_text(ref)`。
- **灰区应用**（SWT/Java、自绘控件、游戏、远程桌面——无元素树）：`get_screen_text` 读出文字与坐标 → `click(ref)` 或 `click(x=..,y=..)`。**不要**默认截图让模型自己估坐标：实测一条 DBeaver 任务 8.3 分钟里，工具只占 21 秒，其余 478 秒全花在「看图 → 估像素 → 换算坐标」上。
- **拿不准坐标时带上 `expect`**：`click(x=512, y=384, expect="保存")` —— 程序会算出「离『保存』中心偏了多少、该改点哪个坐标」，直接照抄建议坐标重试即可；点了没反应先 `get_last_click_image` 回看，别盲目重复点击。
- **能预判的连续动作，一律一次 `act_sequence` 提交**。每多一次单独调用就多一个「模型思考 + 读结果」的来回（实测每轮 30~70 秒）；只有需要看结果做分支判断时才拆开。**不确定目标叫什么**时给**候选名列表**（`text: ["保存","Save"]`）——服务端按顺序找、命中即停，不必每个猜测各花一个来回。

### 省往返的三条机制

1. **落点证据**——坐标级点击/键盘注入的返回值只说明「事件发出去了」（xdotool 不关心点到了什么）。因此点击会一并回报：

   ```
   坐标级点击已执行 @(874,578)｜落点：窗口「落点故事」 310x212 ｜ 落点文字：「确定」 ｜
   点后活动窗口：无（原「落点故事」已消失）
   ```

   模型据此立刻知道「点中了『确定』、且窗口确实关了」，**不需要再截一张图确认**。
   （元素级 `do_action` 不需要这个——它不存在「点没点中」的疑问。）

2. **`act_sequence` 的 `ui_tree` / `screenshot` 步骤**——「点开菜单 → 读菜单里有什么」原先也得拆成两次调用；实测任务里另外三分之二的截图纯粹是「确认刚才那串动作」。现在两者都能折进同一次调用，分工是**看结构用 `ui_tree`（文本、便宜），看像素才用 `screenshot`（贵；无元素树的灰区应用只有这条路）**。图像作为额外的 image content block 与步骤日志一起返回。

3. **服务端会主动引导模型批量提交**——会话开始时下发给模型的 MCP `instructions` 里写明了这条规则：一次往返要 30~70 秒而动作本身只要零点几秒，所以「下一步不依赖上一步结果」的连续动作就该放进一次 `act_sequence`。没有这段引导，模型读到的是逐步工作流，默认就会一个动作一次调用——那正是上面那 96% 往返开销的来源。

### 点击反馈：光圈（给人看）+ 准星图（给模型看）+ 程序算偏差

坐标点击**只存在于** tier ② 兜底与灰区裸坐标，以下反馈全部由 `backend.click_at` 这个唯一漏斗自动附带。

- **光圈**（`backend/linux/ring.py`）——给**看屏幕的人**：落点亮一个实色红环、1.5s 自毁。没有它，虚拟屏里的一次点击对用户是完全不可见的。`CC_CU_CLICK_RING=0` 可关。
- **准星预览图**——给**模型**：点击**前**抓 480x300 裁剪、红十字标在落点上、**不缩放**（图上 1px = 屏幕 1px，零换算）。约 190 token/次；`preview=false` 连抓都不抓（零成本）；`CC_CU_CLICK_PREVIEW=0` 全局关。
- **程序算偏差**——落点坐标是程序自己发出去的（绝对精确），文字块位置来自 OCR（TSV 自带 `left/top/width/height`），偏差就是坐标系减法，没有任何智能判断。带 `expect` 时把启发式变成精确值。
- **点后界面变化**——同一区域点前/点后像素对比（<0.5% 无 / <5% 轻微 / ≥5% 明显），这是**不需要知道意图**的独立第二信号。两个信号要一起读：偏 45px 但界面变了 = 按钮热区比文字大、其实点中了；偏 45px 且毫无变化 = 该修坐标了。

---

## 二、环境要求

| 项 | 要求 |
|----|------|
| 操作系统 | Linux（Phase 1 仅 Linux；Windows 在 Phase 3） |
| 会话类型 | **X11**（Wayland 下全局输入注入受限） |
| Python | 3.12+ |
| 系统组件 | `xdotool`（注入）、`xserver-xephyr`（隔离沙箱虚拟屏）、`xclip`（非 ASCII 输入走剪贴板）、`wmctrl`（应用枚举）、`tesseract-ocr` + `chi_sim`（OCR 灰区感知）、`zenity`（测试）、AT-SPI（`gir1.2-atspi-2.0` / `libatspi`） |
| 无障碍开关 | 必须开启（见下） |

> **关键约束**：AT-SPI 依赖系统的 `libatspi` 与 `Atspi` typelib（OS 级组件），无法打进单一二进制。本项目通过 `_bootstrap.py` 在运行时设 `GI_TYPELIB_PATH` 指向系统目录来加载——即便冻结成可执行文件，运行时仍需系统提供这些组件。

---

## 三、安装与环境准备

```bash
# 1. 一次性：开启无障碍开关（否则读不到 GTK 应用的树）
gsettings set org.gnome.desktop.interface toolkit-accessibility true

# 2. 系统依赖（Debian/Ubuntu）—— 全是 OS 级依赖，打包产物不内嵌，运行时必须有
sudo apt install -y xdotool xserver-xephyr xclip wmctrl zenity \
                    gir1.2-atspi-2.0 \
                    tesseract-ocr tesseract-ocr-chi-sim

# 3. conda 环境（Python ≥3.12）；pygobject 必须走 conda-forge，pip 装不上 gi
conda create -n cc-computer-use python=3.12 -y
conda install -n cc-computer-use -c conda-forge pygobject -y
conda activate cc-computer-use

# 4. Python 依赖 + 本项目（可编辑模式）
PYTHONNOUSERSITE=1 pip install -U mcp cryptography pyinstaller pytest
PYTHONNOUSERSITE=1 pip install -e .
```

> ⚠️ **为什么要 `PYTHONNOUSERSITE=1`**：若 `~/.local` 里装了别的 `mcp`，会遮蔽 conda 环境的包、导致版本错乱（开发与打包都受影响）。所有运行/打包命令都应带此前缀。
>
> `chi_sim` 是简体中文语言包，不装则中文全变乱码；纯英文界面设 `CC_CU_OCR_LANG=eng` 明显更快。
>
> **换新机器 / 从零部署 / 遇到报错**：完整的分步说明与故障排查见 [`docs/安装说明.md`](docs/安装说明.md)。

---

## 四、运行 MCP Server（开发模式）

```bash
# 自检：打印 backend 状态、工具列表、屏幕布局（不进入 stdio 循环）
PYTHONNOUSERSITE=1 python -m computer_use_mcp.server --selftest

# 正常运行（stdio 传输，供 MCP 客户端连接）
PYTHONNOUSERSITE=1 python -m computer_use_mcp.server
```

### 隔离沙箱（默认开启）

X11 只有一个物理指针/焦点：Agent 注入时用户无法同时用电脑。本 server **默认使用一块 Xephyr 可视虚拟屏**（屏号由 X server 自动分配，每个 Claude 会话各得一块），一切注入/截图/几何查询都指向虚拟屏——宿主桌面零干扰，用户可并行干活；用户也可以随时把键鼠伸进 Xephyr 窗口亲自操作（此时 Agent 注入自动礼让等待，离开后恢复）。应用经 `launch_app` 工具放进沙箱。

虚拟屏**首次真正用到本 MCP 时才启动**（不是开 Claude 就弹窗）：Claude Code 在会话启动时就会拉起本 server 进程，若那时建屏则每次开会话都白弹一个窗口。

**每个 Claude 会话各得一块私有屏**（屏号由 X server 自动分配，零竞态）——多个会话并存时互不干扰。同一个会话内，主 agent 与并发子 agent 共用这块屏，因此注入操作是**互斥**的：抢不到屏锁的一方会立刻收到「屏幕正被另一个操作独占，请等待后重试」的提示，而不是被静默排队（排队会让它基于过期的界面认知继续操作）。

若沙箱不可用（没装 Xephyr / 启动失败），注入会被**明确拒绝**并报错，不会静默落到你的真实桌面上——确实要直接操作真实桌面，请显式设 `CC_CU_DISPLAY_MODE=real`。

| 环境变量 | 默认 | 作用 |
|----------|------|------|
| `CC_CU_DISPLAY_MODE` | `isolated` | 设 `real` 关闭沙箱、直接操作真实桌面（旧行为） |
| `CC_CU_SANDBOX_DISPLAY` | *不设* | 不设=每次自动分配空闲屏号（每会话一屏）；显式设 `:99` 等=固定屏号，且同屏号已存在时 attach 不重起 |
| `CC_CU_SANDBOX_SCREEN` | `1600x1000` | 虚拟屏分辨率 |
| `CC_CU_SANDBOX_WAIT_USER` | `30` | 用户在沙箱内时注入礼让的最长等待秒数 |
| `CC_CU_SANDBOX_WM` | `auto` | 设 `none` 则沙箱内不启动 i3 窗口管理器 |
| `CC_CU_SANDBOX_AT_SPI_BUS` | *不设* | 覆盖沙箱应用的 AT-SPI 总线地址（调试用） |
| `CC_CU_OCR_LANG` | `chi_sim+eng` | OCR 识别语言；**纯英文界面设 `eng` 明显更快** |
| `CC_CU_CLICK_RING` | `1` | 设 `0` 关闭点击光圈 |
| `CC_CU_CLICK_PREVIEW` | `1` | 设 `0` 全局关闭准星预览图 |
| `PYTHONNOUSERSITE` | — | **所有命令都要设 `1`**，避免 `~/.local` 的包遮蔽 conda 环境 |

### a11y（无障碍）隔离：沙箱有**自己的** AT-SPI 总线

**Xephyr 只隔离 X11 通道（注入/截图），不隔离 AT-SPI** —— 无障碍走的是会话 D-Bus，沙箱内外共用同一个 `at-spi2-registryd`。2026-09-15 实测：对沙箱内应用做 a11y 遍历会把宿主 **GNOME Shell 打崩**（离线解 core 确证是 gnome-shell 自己的 atk-bridge 在 `g_object_ref` 一个**已释放的 GObject** —— use-after-free，崩在主循环线程上）。

**解决**：沙箱启动时自起一套**私有** AT-SPI 总线（私有目录里的 `dbus-daemon` + `at-spi2-registryd`），沙箱内应用与 MCP 的 a11y 读取都连它 —— **宿主总线完全看不到沙箱内应用**，而 a11y 能力**保留**（沙箱内应用的树照常可读可点）。

```
宿主 GNOME Shell ── 宿主 AT-SPI 总线 ── 宿主应用（gnome-shell/chrome/…）
                                      ✗ 互不可见
沙箱内应用 ──────── 沙箱私有 AT-SPI 总线 ── MCP 的 a11y 读取
```

- 私有总线起不来时，应用会拿到一个**死地址**（连不上 a11y）：宿主安全，但沙箱内没有树可读 —— 这是刻意的兜底，宁可没有 a11y，也不让流量落到宿主总线。
- 为什么用总线地址而不是 `NO_AT_BRIDGE=1`：后者只是 GTK3 的开关（GTK4 用 `GTK_A11Y`），而 SWT（DBeaver 这类 Java 应用）**两个都不认**。`AT_SPI_BUS_ADDRESS` 工具链无关 —— GTK3/GTK4/SWT/Qt/Electron 的 a11y 都走 libatspi，都读它，且**不回落**到会话总线（实测）。
- 验证脚本：`PYTHONNOUSERSITE=1 python tests/manual_a11y_isolation.py`

> **运维后果**：沙箱崩溃并被重建后总线地址必变，而 libatspi 的 `atspi_init()` 每进程只能绑定一次 —— 于是**本会话的 a11y 能力即永久失效**，只剩 OCR + 坐标点击那条最贵的路。唯一恢复手段是重启 Claude Code。

---

## 五、打包为可执行文件

```bash
bash build.sh
# 产物（onedir，不是单文件）：
#   dist/computer-use-mcp-bin/     真实可执行文件所在目录，约 455MB
#   dist/computer-use-mcp          薄 shell wrapper，转发到上面那个

# 验证冻结产物
PYTHONNOUSERSITE=1 ./dist/computer-use-mcp --selftest
```

**为什么是 onedir**：onefile 每次启动都要自解压到临时目录，冷启动慢一个量级。wrapper 的存在是为了**保住已注册的路径**（`~/.claude.json` 里写的是 `dist/computer-use-mcp`），别删它。

`build.sh` 关键点（已固化）：

- **定向收集 `gi`**（`--collect-submodules/--collect-data/--collect-binaries gi`）+ 显式排除 GTK 家族：`--collect-all gi` 会连 `Gtk` 一起收，触发 PyInstaller 的 GTK hook 去收集 185MB 图标 + 42MB 主题，而本项目从不 import Gtk/Gdk。
- `--collect-submodules mcp.server`（**不用** `mcp` 全量，避免拉进需 `typer` 的 `mcp.cli`）。
- `--copy-metadata`：mcp/pydantic 等用 `importlib.metadata` 读版本，需带上元数据。
- **`LD_LIBRARY_PATH` 前置 conda 的 `lib`**：否则 PyInstaller 会用系统旧 `libcrypto` 配 conda 新 `libssl`，运行时报 `OPENSSL_3.3.0 not found`。
- **`Atspi-2.0.typelib` 与 `libatspi.so.0` 由 `packaging/embed-atspi.sh` 嵌进产物**（`_internal/` 与 `_internal/gi_typelibs/`），使分发形态不再依赖目标机装过 `gir1.2-atspi-2.0` / `libatspi2.0-0`。落点有硬约束：前者是 PyInstaller 的 `pyi_rth_gi` 钩子**无条件赋值** `GI_TYPELIB_PATH` 的唯一落点；后者放在 `_MEIPASS` 之下才能被子进程 env 剥离逻辑自动剥掉。该脚本还会**遍历 typelib 的依赖闭包**（`Atspi-2.0` 依赖 `DBus-1.0`，而它来自 `gir1.2-freedesktop`）—— 只拷一个文件的话，开发机上照样能用，目标机却会 `import Atspi` 失败。
- **`--collect-submodules Xlib` + 显式 `--hidden-import Xlib.ext.shape`**（点击光圈用）：`Xlib.ext` 的扩展模块是按扩展名动态导入的，不出现在任何静态 import 图里——漏了的表现是「开发态一切正常、**只有冻结产物里没有光圈**」。

**分发到另一台机器**（老办法）必须带上整个 `dist/` 目录；目标机器仍需自行安装系统依赖（`xdotool`/`xserver-xephyr`/`tesseract`/AT-SPI 等）。

### 打成 `.deb` 分发给 Ubuntu 用户（推荐）

```bash
mkdir -p ~/dist
docker run --rm -v "$PWD":/src -v "$HOME/dist":/out \
  -v /tmp/Miniforge3-Linux-x86_64.sh:/miniforge.sh:ro \
  -e CC_CU_MINIFORGE_SH=/miniforge.sh \
  ubuntu:22.04 bash -c 'bash /src/packaging/build-in-container.sh'
# → ~/dist/cc-computer-use_0.1.0-1_amd64.deb（实测 68 MB）

# 在干净容器里验收（装、跑、点、卸全链路；22.04 与 24.04 各跑一遍）
bash packaging/deb/verify-install.sh ~/dist/*.deb ubuntu:22.04
```

目标机 `sudo apt install ./cc-computer-use_*.deb` 即可，**不需要装任何东西**：
xdotool / Xephyr / i3 / tesseract 等 9 个系统二进制、它们的依赖闭包、Atspi typelib
与 OCR 语言包全部随包。装完由 `cc-computer-use-setup` 打开无障碍开关、并在检测到
Claude Code 时自动注册 MCP（不覆盖已有配置）；`cc-computer-use-doctor` 逐项体检。

必须在 **ubuntu:22.04 容器**里构建：24.04 上取到的那批二进制要求 GLIBC 2.38，
直接打包会恰好排除掉 22.04 这个最低档。详见 [`docs/安装说明.md`](docs/安装说明.md) 第七节。

---

## 六、接入 Claude Code

在项目根或 `~/.claude` 的 MCP 配置中加入（二选一）：

**A. 用打包好的可执行文件（推荐，无需 conda）**
```json
{
  "mcpServers": {
    "cc-computer-use": {
      "type": "stdio",
      "command": "/绝对路径/cc-computer-use/dist/computer-use-mcp",
      "args": [],
      "env": { "PYTHONNOUSERSITE": "1" }
    }
  }
}
```

**B. 用 conda 环境直接跑（开发期，改源码立即生效）**
```json
{
  "mcpServers": {
    "cc-computer-use": {
      "type": "stdio",
      "command": "/conda环境路径/envs/cc-computer-use/bin/python",
      "args": ["-m", "computer_use_mcp.server"],
      "env": {
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/绝对路径/cc-computer-use/src"
      }
    }
  }
}
```

> ⚠️ **服务名要用 `cc-computer-use`，不能用 `computer-use`** —— 实测后者注册不上，改名即可。
>
> Claude Code 必须运行在 **X11 图形会话**中（server 子进程需继承 `DISPLAY` 才能读屏/注入）。

接入后即可用自然语言下达任务，例如：
- 「看一下当前窗口有哪些可点的按钮」→ AGENT 调 `get_ui_tree`
- 「帮我点这个对话框的『是』」→ AGENT `find_element` + `click(ref)`，走元素级，零坐标
- 「在编辑器里输入 hello 并保存」→ `type_text` + `press_key('ctrl+s')`

---

## 七、测试

```bash
# 单元测试（无需桌面）：geometry 校准 / refs 映射 / serializer 去噪 / 注入与沙箱逻辑
# 全量收集 310 项；不带 e2e 开关时 304 passed / 6 skipped（端到端自动 skip）
PYTHONNOUSERSITE=1 python -m pytest -q

# 端到端（需 X11 + zenity + Xephyr）：默认在**沙箱内**跑，不碰真实桌面；需显式 opt-in
# 310 passed
CC_CU_E2E=1 PYTHONNOUSERSITE=1 python -m pytest -q

# 手动 story（共 6 个，完整清单见 docs/安装说明.md §5.4）

# 沙箱 story（驱动真实 server）：沙箱内起 zenity → 元素级点击 → 断言宿主零干扰
PYTHONNOUSERSITE=1 python tests/manual_sandbox_story.py

# 灰区感知 + 落点证据 story（驱动冻结产物，需先 bash build.sh）：OCR 出坐标 → 点击 → 断言对话框关闭
PYTHONNOUSERSITE=1 python tests/manual_ocr_story.py
```

> ⚠️ 跑 `manual_sandbox_story.py` 期间**不要动鼠标键盘**——它断言「宿主活动窗口与指针前后不变」。

测试分三类，刻意不混用：单测**不需要桌面**；端到端需要 X11 + `zenity` + `Xephyr`（不满足时自动 skip）；`manual_*.py` 是**手动 story**，用 MCP stdio 客户端驱动真实 server 或冻结产物，跑完整任务链路。

---

## 八、架构

```
Claude Code / MCP 客户端
        │ MCP 协议 (stdio, JSON-RPC)
┌───────▼──────────────────────────────────────────┐
│  server.py  (FastMCP/MCPServer 入口)               │
│  tools/      17 个工具定义（schema + 引导 description）│
│  core/coordinator   语义编排：三级降级               │
│  core/serializer    树→紧凑文本（省 token）+ ref 分配 │
│  core/geometry      坐标校准（窗口绝对 + 元素相对）    │
│  core/display       隔离沙箱：唯一 DISPLAY 来源        │
│  utils/refs         ref ↔ 活元素 映射（同进程缓存）    │
└───────┬──────────────────────────────────────────┘
        │ backend/base.py 抽象契约
┌───────▼──────────┐
│ backend/linux/   │  AT-SPI 读取(atspi.py) + xdotool 注入(inject/)
└──────────────────┘  （Windows backend = Phase 3）
```

**三级降级（coordinator.click）**：
1. **element**：`backend.invoke()` → AT-SPI `do_action`，零坐标（首选）。
2. **coord**：`geometry` 校准出屏幕绝对坐标 → `xdotool` 先聚焦窗口再点击（兜底）。
3. **screenshot**：连元素都找不到（灰区）→ 提示改用截图，由 LLM 视觉决策。

返回的 `ActionResult` 会标明实际生效层级。

---

## 九、项目结构

```
cc-computer-use/
├── src/computer_use_mcp/
│   ├── _bootstrap.py        # 最先执行：设 GI_TYPELIB_PATH 再 import gi
│   ├── server.py            # MCP 入口 + 工具注册 + --selftest
│   ├── backend/
│   │   ├── base.py          # Backend ABC + Rect/UINode/Element/TextBlock 数据类
│   │   └── linux/
│   │       ├── atspi.py     # AT-SPI 读取 + do_action/set_value
│   │       ├── inject/      # xdotool 注入 + 窗口几何
│   │       │                #   （apps/base/keyboard/pointer/screens/windows）
│   │       ├── grab.py      # 抓屏（截图与 OCR 共用）
│   │       ├── ocr.py       # 灰区感知：tesseract → 带坐标的文本块
│   │       ├── ring.py      # 点击光圈（给人看的落点反馈）
│   │       └── backend.py   # LinuxBackend 组合实现
│   ├── core/
│   │   ├── serializer.py    # 树序列化（去噪/省 token/ref 分配）
│   │   ├── geometry.py      # 坐标校准公式
│   │   ├── screen_lock.py   # 屏独占锁（注入操作互斥）
│   │   ├── display/         # 隔离沙箱（Xephyr + 私有 AT-SPI 总线 +
│   │   │                    #   DISPLAY 供给 + 用户礼让）
│   │   └── coordinator/     # 三级降级编排
│   ├── tools/               # ui_tree/find/action/layout/screenshot/screen_text/windows/apps
│   └── utils/               # refs/errors/logging/blocks/temps
├── tests/                   # 单测 + zenity 端到端 + manual_* 手动 story
├── demo/                    # Phase 0 可行性验证
├── docs/                    # 部署与排错手册
├── entry.py                 # PyInstaller 入口
├── build.sh                 # 本机打包脚本（onedir + wrapper）
├── packaging/               # 分发：.deb / .mcpb
│   ├── build-in-container.sh  #   整套流水线，在 ubuntu:22.04 容器里跑
│   ├── assemble.sh            #   组装可分发目录 + manifest
│   ├── vendor_libs.py         #   9 个随包二进制的依赖闭包与 RPATH
│   ├── embed-atspi.sh         #   libatspi + Atspi typelib 闭包 → _internal/
│   └── deb/                   #   control 文件、postinst、setup/doctor 脚本、验收脚本
└── pyproject.toml
```

> `core/display`、`core/coordinator`、`backend/linux/inject` 三个包由原单文件按职责拆分而来。**对外命名空间一个都没变**（`display.MANAGER` / `inject.XdotoolInjector` / `coordinator.Coordinator` 由 `__init__.py` re-export），故所有既有 import 点零改动。

---

## 十、已知限制 / 灰区

- **无元素树的场景**（自绘控件、游戏、视频、远程桌面、DRM）：用 `get_screen_text`（OCR 出带坐标的文本）兜底；已知限制：文字紧邻深色图标时那一行会识别失败，需收窄 `region` 重试。
- **坐标漂移**：部分 GTK 对话框 `get_extents(SCREEN)` 返回相对窗口原点坐标；已由 `geometry` 校准层处理，且元素级 `do_action` 根本绕开坐标。
- **Wayland**：MVP 锁定 X11；Wayland 注入受限，需 libei（Phase 2+ 评估）。
- **uinput 注入**：当前用 xdotool（XTest），覆盖 X11 够用；uinput（需 udev 规则）在 Phase 2.3。
- **非 ASCII 输入走剪贴板**：`xdotool type` 对中文会临时改写全局键盘映射，输入期间用户物理键盘整体失灵，且与输入法争抢 XKB 状态。纯 ASCII 才走 `xdotool type`。

---

## 路线图

- ✅ **Phase 0** 可行性验证（`demo/`）
- ✅ **Phase 1** Linux MVP（本项目：17 工具 + backend + core + 打包）
- ✅ **Phase 1.5** 隔离沙箱（Xephyr 虚拟屏默认隔离 + 私有 AT-SPI 总线 + `launch_app`，宿主零干扰）
- ⬜ **Phase 2** 精度加固 + token 优化（树 diff、uinput、焦点处理）
- ⬜ **Phase 3** Windows backend（UIA + SendInput）
- ⬜ **Phase 4** 视觉解析 + 进一步的灰区兜底

---

## 许可证

[MIT](LICENSE) —— Copyright (c) 2026 刘飞 (liufei)