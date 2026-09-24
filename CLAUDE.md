# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

让 AGENT 直接操作 **Linux/X11 桌面** 的 MCP server。核心主张（由 `demo/` 实测确立）：**用无障碍树（AT-SPI）结构化感知 + 元素级 `do_action` 操作，替代「截图 + 坐标点击」**。元素级操作零坐标、不受焦点/DPI/分辨率影响，从根上消除「点不准」。可行性验证见 `demo/README.md`——改架构前先读它。

> 本机已在 `~/.claude.json` 以名字 **`cc-computer-use`** 注册（**不要**用 `computer-use`：实测该名注册不上，改名后即可）。命令指向 `dist/computer-use-mcp`（wrapper）。改产物路径时记得同步这里。完整接入方式见 [`docs/安装说明.md`](docs/安装说明.md)。

## 环境与命令（务必遵守，否则导入/打包必出错）

固定用 conda 环境 `cc-computer-use` 的 python（下称 `$PY`）：`$HOME/anaconda3/envs/cc-computer-use/bin/python`——具体路径按本机 anaconda 安装位置推导。

**⚠️ 编译/打包/跑测试一律用这个 conda 环境，绝不用 base 环境**。base 里没有本项目依赖的 `pygobject`/`gi`/PyInstaller 版本组合，用它编译会得到启动即报错（或静默行为不一致）的产物——这类错误很难从报错信息反查到「用错环境」上。凡是调用 python，先显式指定 `$PY` 的绝对路径，或先 `conda activate cc-computer-use`，不要依赖当前 shell 的默认 python。

**所有命令必须加 `PYTHONNOUSERSITE=1`**：`~/.local` 里装有另一份 `mcp`，会遮蔽 conda 包导致版本错乱（开发与打包都受影响）。

```bash
PY=$HOME/anaconda3/envs/cc-computer-use/bin/python

# 自检（不进 stdio 循环，打印 backend 状态/工具/屏幕布局）
# 注意：isolated 模式（默认）自检会自启 Xephyr 沙箱窗口，进程退出时 atexit 回收
# ⚠️ 它在 2026-09-14 之前是「最快的健康检查」，但内部调了 AT-SPI 版 list_apps，
#    实测直接把宿主 GNOME Shell 打成 SIGSEGV（详见下文「child_count 是全项目最危险的单次调用」）。
#    现已改走 X11 才真正安全——**改动时务必不要把 AT-SPI 应用枚举加回 selftest**。
PYTHONNOUSERSITE=1 "$PY" -m computer_use_mcp.server --selftest

# 全量测试（共 341 项；单测无需桌面，端到端需 X11 + zenity + Xephyr，不满足自动 skip）
# ✅ 两条路径都全绿：默认 335 passed / 6 skipped；下面带 e2e 开关的 341 passed
PYTHONNOUSERSITE=1 "$PY" -m pytest -q

# e2e 需显式 opt-in（默认在沙箱内跑、不碰真实桌面）
CC_CU_E2E=1 PYTHONNOUSERSITE=1 "$PY" -m pytest -q

# 关闭沙箱、直接操作真实桌面（旧行为）：CC_CU_DISPLAY_MODE=real 前缀任意命令

# 单个测试
PYTHONNOUSERSITE=1 "$PY" -m pytest tests/test_geometry.py::test_calibrate_center -v
PYTHONNOUSERSITE=1 "$PY" -m pytest tests/test_e2e_zenity.py -v   # 端到端

# 打包为可执行文件 —— 产物是 **onedir 目录**，不是单文件
#   dist/computer-use-mcp-bin/    真实可执行文件所在目录（**实测 455MB**；可执行文件本身仅 9.5MB，其余是 _internal/ 里的打包库）
#   dist/computer-use-mcp         薄 shell wrapper，转发到上面那个
# 为何 onedir：onefile 每次启动都要自解压到临时目录（秒级冷启动开销），onedir 快一个量级。
# wrapper 的存在只为**保住已注册路径**（~/.claude.json 里写的是 dist/computer-use-mcp），
# 改动产物布局时别把 wrapper 一起删了。
bash build.sh
PYTHONNOUSERSITE=1 ./dist/computer-use-mcp --selftest   # 验证冻结产物

# 安装依赖（pygobject 必须走 conda-forge，pip 装不上 gi）
conda install -n cc-computer-use -c conda-forge pygobject -y
PYTHONNOUSERSITE=1 "$PY" -m pip install -U mcp cryptography pyinstaller pytest python-xlib
```

一次性系统准备：`gsettings set org.gnome.desktop.interface toolkit-accessibility true`（否则读不到 GTK 应用树），并安装 `xdotool`、`xserver-xephyr`、`zenity`。

**OCR（灰区感知通道，方向一）额外需要**：`sudo apt install tesseract-ocr tesseract-ocr-chi-sim`
（`chi-sim` 是简体中文语言包，不装则中文全变乱码）。**刻意不依赖 `pytesseract`** —— 直接 subprocess 调 `tesseract` 命令行，少一个 Python 依赖、PyInstaller 不用加 hidden-import，且 `tsv` 输出自带每词的 `left/top/width/height/conf`，正是我们需要的坐标。与 xdotool/Xephyr 一样，它是**运行时需系统提供**的 OS 级依赖，不内嵌进冻结产物。
- 识别语言默认 `chi_sim+eng`（`ocr.py:DEFAULT_LANG`），可用 **`CC_CU_OCR_LANG`** 覆盖——纯英文界面设 `eng` 明显更快。`tesseract --list-langs` 可查本机装了哪些语言包。

> 📖 **换新机器 / 从零部署**：完整步骤见 [`docs/安装说明.md`](docs/安装说明.md)（系统依赖、conda 环境、无障碍开关、OCR 语言包、验证、Claude Code 接入、打包、排错）。本文件只讲**改动代码时**必须知道的约束与理由。

## 架构要点（跨文件才能理解的「大图」）

### 三层 + backend 抽象
- `tools/` 的 MCP 工具（薄封装，只做参数解析 + 错误转友好文本）→ 委托给
- `core/coordinator/`（语义编排，平台无关）→ 调用
- `backend/base.py` 的 `Backend` ABC → Linux 实现在 `backend/linux/backend.py`，组合 `atspi.py`（读 + 元素级 do_action/set_value）、`inject/`（xdotool 注入 + 按 PID 查窗口几何）、`grab.py`（截图与 OCR **共用**的抓屏层）、`ocr.py`（灰区感知）。Windows backend 是 Phase 3，须实现同一 ABC。

新增能力时：先在 `base.py` 加抽象方法 → `linux/backend.py` 实现 → `coordinator/` 编排 → `tools/` 暴露。**不要**让 `tools/` 或 `coordinator/` 直接 import gi/xdotool。

**⚠️ 2026-09-18 起 `core/display`、`core/coordinator`、`backend/linux/inject` 是「包」而非单文件**（原 1297 / 973 / 809 行按职责拆成 mixin）。改这几个包前必须知道的四条：

- **对外命名空间一个都没变**：`display.MANAGER` / `display.env_for` / `inject.XdotoolInjector` / `coordinator.Coordinator` 照旧（`__init__.py` re-export）。Python 里包的导入语法与模块**完全一致**，故所有既有 import 点零改动——这也是选「拆成包」而不是「拆成平级模块」的原因。
- **各子模块必须共用同一个 logger 对象**：`from .constants import log`（或 `.hooks` / `.base`），**不要**自己写 `get_logger(__name__)`。固定名（即拆分前的原模块名）保证了日志名不变，更关键的是 `monkeypatch.setattr(display.log, "warning", ...)` 这类既有补丁对全部子模块生效——各自取会拿到不同对象，补丁**静默打空**。
- **子模块一律用属性访问调标准库**（`shutil.which(...)` 而非 `from shutil import which`）：`monkeypatch.setattr(display.shutil, ...)` 改的是**同一个模块对象**（进程级单例），属性访问才吃得到这个补丁。
- ⚠️ **`Coordinator` 拆成 mixin 后，三道反射守卫必须遍历 `__mro__`**：判据是 `tests/test_coordinator_hooks.py` 的 `_coordinator_methods()`，**不能退回 `vars(Coordinator)`** —— `vars()` 只看本类 `__dict__`、看不到继承来的方法。拆包当天实测：三条守卫里**两条静默变成空集合、全绿但零覆盖**，只有一条因为「豁免名单全成了死登记」才变红；若无那条副作用式的检查，三道守卫会被**全部废掉且不报任何错**。新增 mixin 时 `_coordinator_methods()` 自动覆盖，无需改动。

### 灰区感知：两级（OCR 文本层优先，截图兜底）
SWT/Java、自绘控件、游戏、远程桌面**没有无障碍树**（或树一读就崩，见下文 DBeaver 崩溃条目），三级降级只能落到坐标点击。这条路上最贵的**不在工具侧**（截图只要 0.2s），在**模型侧**：看图 → 估像素位置 → 换算回屏幕坐标。实测一次 DBeaver 任务 8.3 分钟里工具只占 21 秒，其余 478 秒全耗在这个循环上。

- **`get_screen_text`（首选）**：`ocr.py` 用 tesseract 把屏幕变成「`[ref] 文字 @ (x,y)`」文本，模型读文字即可定位，坐标是**算出来的不是猜的**；文本块注册进同一张 `RefTable`（`backend/base.py` 的 `TextBlock`），故 `click(ref=N)` 与元素级 ref 用法一致。**`element_info`（返回 `ElementDetail`）与 `element_screen_rect` 都对 TextBlock 做了分支**——它没有元素级动作，`invoke/set_value` 一律返回 False，**让 coordinator 正常降级到坐标点击**（`element_screen_rect` 直接返回它自带的 rect，即屏幕绝对坐标，**不套用 geometry 校准**，那套是给 GTK 相对坐标漂移用的）。`screenshot` **没有** `native` 参数，本不需要分支。（D-2/M-29）
  - **范围默认 `scope='window'`（只识别活动窗口）**：实测全屏密集文字 8~10s，而对话框大小只要 0.3~3s。别默认全屏。
  - **耗时**：全屏 8~10s（**1600x1000 沙箱屏**）、对话框 0.3s（视文字量）。这是 OCR 的固有成本，换的是模型侧从 30~70s 降到几秒。⚠️ 该数值**只对小屏成立**：耗时随面积超线性增长，宿主 3840x1200 全屏实测 30s。别拿它估算大屏。
  - **tesseract 必须限单线程（`OMP_THREAD_LIMIT=1`，2026-09-22 实测）**：加这一条白捡约 **2 倍**，且**识别结果逐字不变**（对照实验里词数完全相同）。原因是 tesseract 内部用 OpenMP `num_threads()` 子句并行流水线的一段，而**该子句会覆盖 `OMP_NUM_THREADS`**——只设 NUM_THREADS 拦不住，只有 `OMP_THREAD_LIMIT` 这个硬上限能。线程数扫描（1/2/4/8/22）显示**线程越多越慢**（22 线程慢一倍）：单张图是有状态流水线（二值化 → 连通域 → 版面分析 → 逐行识别），只有末段可并行，多给的线程只换来同步开销。实测宿主全屏 47.1s → 29.9s（真实代码路径 ABBA 对照）。
    - **它还消掉一个失败模式**：`_TIMEOUT=60s` 在改前**会被撞到**（实测第二轮就超时抛错），改后 30s 有充裕余量。
    - **⚠️ 别改成「并行跑多个 tesseract 子进程」提速**：那是另一条路，需先解决重叠区去重与稀疏内容丢字（实测稀疏图分块丢 30% 的词），收益不稳，**未落地**。
    - 守卫 `test_tesseract_is_invoked_single_threaded` 断言的是**实际传给 subprocess 的 env 取值**（不是「模块里有个常量」），故「常量还在、调用点没用它」也拦得住。注意 `test_spawn_env.py` 的反射扫描只认 `env=` **直接源自** `env_for`/`app_env`——所以这条必须在**调用点**叠加，不能藏进 `_tess_env()` 之类的包装函数里（实测会被那条结构性守卫打红）。
  - **喂给 tesseract 的 PNG 必须以 `compress_level=1` 编码（`_PNG_COMPRESS_LEVEL`，2026-09-22 实测）**：`img.save(buf, format="PNG")` 用的是 **Pillow 默认的 6**，而这一步**不比 tesseract 本体便宜**——真实桌面截图（2x 放大后 3192x1922）7.42s → 1.18s，全屏那张 8.89s → 5.15s；端到端 ABBA 四组配对中位 **2.20x**。**PNG 的 zlib 是无损压缩，等级只影响「压多久/压多小」，解出的像素逐位相同**——两级编码后的 tesseract stdout **md5 完全一致**（验过两张图），所以这行是纯赚。体积会大 1.13~1.19 倍，但它走 stdin、**从不落盘**，零代价。
    - **⚠️ 别向外宣称「省 N 秒」或「PNG 占 OCR 的百分之几」**：编码耗时**随画面内容变化极大**——像素数差 5.8 倍的两张图，耗时只差 1.2 倍（纯色/规则线条压得飞快，照片、渐变、抗锯齿小字慢得多）；实测区间 0.6s（合成线稿）~6.2s（真实截图）。能稳定说出口的只有「level=1 永不慢于 level=6，且结果一模一样」。反过来说，**「OCR 慢」不能一概归给 tesseract**——查下一步优化前，先把这一次 `img.save` 量出来（历史教训：正是把总耗时整个记在 tesseract 账上，才让上面那条 OMP 优化只打在了三成的工作量上）。
    - 守卫 `test_ocr_png_is_encoded_at_low_compression` 判据同样取**实际传给 `img.save` 的 kwargs**（顺带断言仍是 PNG——换 JPEG 虽快但有损，块效应正落在笔画边缘上）。
  - ️ OCR 坐标是**快照**：界面变化后必须重新调用，不能复用旧 ref。`_native_alive` 对 TextBlock 直接判活（它是纯数据对象，不存在"失效"），`get_screen_text` 返回文本里也写明了这一点。
- **`screenshot`（兜底）**：确实需要像素级判断时才用。链路已优化：**抓屏 → 缩放 → 编码一次**（历史实现是「全尺寸 PNG 编码 → 解码 → 缩放 → 再编码」，同一张图压两遍，全尺寸那次纯属浪费，实测 266ms）；默认 **JPEG(q=85)** 而非 PNG——实测 JPEG 编码 14ms/334KB vs PNG 266ms/3065KB。
  - **自动落盘路径（`inline=false` 不带 `save_path`）由 `utils/temps.py` 回收**：按 LRU 只留最近 5 张，文件名带 pid 作归属标记，**别的会话的文件只在主人进程已死时才删**（`/tmp` 是全局的，按前缀无差别裁剪等于误杀别的会话）。刻意不用 atexit——会话被强杀时 atexit 根本不跑，而那正是残留的主要来源（实测手工清掉过 118 个）。`act_sequence` 拿不到 MCP `Image` 类型时的退化落盘（`cc-cu-seq-*`）走同一套。要长期留存必须显式传 `save_path`（工具描述里写明了）。

### 隔离沙箱（core/display.py —— 唯一 DISPLAY 来源）
- 默认 `CC_CU_DISPLAY_MODE=isolated`，虚拟屏 **惰性启动**：`server.main()` **不再**启沙箱（MCP server 是 Claude Code 会话启动即常驻拉起的 stdio 进程，在那里启动会导致每次开 Claude 都弹一个 Xephyr，哪怕整场没用过本工具）。改为「**首次真正用到本 MCP 时**」启动 → `core/coordinator.py` 的 `_needs_display` 装饰器（挂在 Coordinator **每一个**对外方法上，含只读的 `get_ui_tree`）→ `display.ensure_started()`。
  - **改 Coordinator 时注意**：新增对外方法必须挂 `@_needs_display`——漏挂不会报错，只会让该工具静默回落宿主桌面（最危险的那类 bug）。`tests/test_coordinator_hooks.py::test_every_public_coordinator_method_is_lazy_hooked` 会直接失败来兜住这一点，豁免请显式登记。
    - **顺序也有要求**：`@_needs_display` 在**外**、`@_exclusive_screen` 在**内**（先启沙箱再抢屏锁；反了就会在**持锁状态**下等 Xephyr 启动，把并发的其它 agent 白白挡在门外）。由 `test_exclusive_hook_is_always_inside_needs_display` 守着。
    - **判据是装饰器自打的标记 `_cc_hooks`，不是 `hasattr(obj, "__wrapped__")`**：两个装饰器都用 `functools.wraps`，留下的痕迹**完全一样**，旧判据只能分辨「一个都没挂」、分辨不了「挂错了一个」（实测：只挂 `@_exclusive_screen`、完全没挂 `@_needs_display` 的方法照样判通过）。标记是**元组、按「自外向内」记录顺序**，故「挂了哪些」与「谁在外」一次拿全 —— **加新装饰器时必须 `return coordinator._mark(wrapper, "名字", fn)`，别直接 `return wrapper`**。
    - **`_exclusive_screen` 的豁免走文件里的 `_EXCLUSIVE_EXEMPT` 显式登记**（只有只读方法配登记，且各带理由）。判据是 `vars(Coordinator)` **反射枚举**，不是手写方法名清单——历史那份 9 个名字的清单**当场就漏了一个**（`get_screen_text` 挂着锁却不在清单里，摘掉它的装饰器照样全绿）。登记者若其实挂着锁（= 悄悄放弃覆盖）、或指向不存在的方法（死登记），测试同样会失败。
  - **只读方法为何也要挂**：`get_ui_tree` 默认 `scope=active_window` 会经 `inject.active_window_title/pid` 读 X11，沙箱未启时读到的是**宿主**活动窗口，而随后的 click 落在沙箱——感知与操作分属两个 display，极隐蔽。
  - 单测不启沙箱：`tests/conftest.py` 在非 e2e（`CC_CU_E2E!=1`）时 `setdefault("CC_CU_DISPLAY_MODE","real")`，否则跑一次 pytest 就弹一个 Xephyr；e2e 分支刻意不干预以保留「默认在沙箱内跑、不碰真实桌面」的约定。
- **屏号按会话隔离（默认自动分配）**：不设 `CC_CU_SANDBOX_DISPLAY` 时用 `Xephyr -displayfd` 让 X server **自己挑一个空闲屏号**并回写。为什么不让程序自己扫 `/tmp/.X11-unix` 挑号：扫描与绑定之间有竞态窗口，两个会话可能同时选中同一个号（实测：两边都 spawn，输的那个 Xephyr rc=1）——交给 X server 分配则零竞态，且回写时机就是「已就绪」。**每个 Claude 会话因此各得一块私有虚拟屏**，天然不抢焦点/剪贴板。窗口标题 `Claude Sandbox (mcp <pid>)` 带 server 进程号，多屏时用户可分辨归属。
  - 显式设 `CC_CU_SANDBOX_DISPLAY=:99` → 用固定屏号，且同屏号 socket 已存在时 **attach 外部沙箱、退出不回收**（这是「多会话有意共用一块屏」的唯一入口）。
    - **attach 侧的 a11y 靠「按屏号反查私有总线」**（2026-09-16 修）：私有总线工作目录名**带屏号**（`cc-cu-at-spi-d<屏号>-<随机>`，见 `_AT_SPI_DIR_PREFIX`）。attach 的进程 `_proc is None`、从没跑过 `_start_at_spi_bus`，`_at_spi_bus` 无从得知 → 由 `_ensure_at_spi_bus()` 按屏号 glob 反查（取 bus socket 最新者；只认本屏号，否则 a11y 会接到别的会话那块屏上）。
    - ⚠️ **反查必须挂在 `_start_locked` 的「两条」路径上**：① `is_sandbox_up()` 为真时的**早退分支** —— 「多会话共用一块屏」走的**正是这条**（对方起的 Xephyr 让 socket 存在，本方 `_proc` 为 None 又让 `is_sandbox_up()` 直接返回 True，**根本进不了 attach 分支**）；② attach 分支。**只修 ② 等于没修**（那是死代码，第一版就踩了这个坑）。反查不到就保持 `None` → `_DEAD_AT_SPI_BUS` 死地址兜底：宁可沙箱内没有 a11y，也绝不让流量落到宿主总线。
    - ⚠️ 只在 `_proc is None`（沙箱非本进程所起）时反查：自己起的沙箱其地址由 `_start_at_spi_bus()` 权威给出，不该被目录扫描改写。
    - **跨会话残留的清扫**（2026-09-16 修）：MCP server 被**强杀**（会话被 kill、异常退出）时 atexit 根本不跑，它起的 `dbus-daemon` / `at-spi2-registryd` 会被 systemd 收养成孤儿、`/tmp/cc-cu-at-spi-*` 目录也留存 —— `_reap_dead_sandbox()` 只管**本进程自己的**，跨会话无人回收（实测清点到一组：目录 `bmf4pzmb` + 2 个孤儿进程，父进程已是 systemd、总线上零使用者）。修法：`_start_at_spi_bus()` 建目录**之前**调 `_sweep_stale_buses()`，清掉**本屏号**的旧目录。
    - ️ 为什么敢「无脑清本屏号」：它只在**本进程刚 spawn 成功 Xephyr** 之后被调用 —— 此刻本屏上除我们自己的 X server 不可能还有活跃沙箱（**X11 每块屏只允许一个 X server**，别人的沙箱若活着，我们的 Xephyr 根本起不来），故同屏号旧目录必是残留。**只清本屏号**：跨屏会误杀别的会话正在用的总线。
    - 进程匹配靠**反向扫 `/proc`**（cmdline 含目录路径 / environ 的 `AT_SPI_BUS_ADDRESS` 指向该总线），**不记 pid 文件**：pid 会复用、文件会丢，而这两者是**当下事实**。实测两类各命中一个 —— registryd 命令行不含目录，只能靠 environ 认出（`/proc/<pid>/environ` 反映启动时环境，用来校验启动参数恰好合适）。
  - 尝试次数有上限（`_start_attempts`）：**冷启动失败只试一次**（Xephyr 缺失不会自行恢复，重试只会让每次工具调用都白等超时）；**曾成功过则允许有限次重建**（屏被回收/崩掉能自愈）。
- **注入前两道闸（`coordinator._exclusive_screen` 装饰器，挂在注入类 + 截图方法上）**：
  1. **屏独占锁**（`core/screen_lock.py`）：同一会话的主 agent 与所有子 agent **共用同一个 MCP server 进程**（子 agent 不新起 `claude.exe`，因而也不新起 server），于是共用一块屏、一个指针/焦点、一份剪贴板与一张 ref 表；X11 每块屏只有一套指针，**真并行物理上不存在**。抢不到锁**立即抛 `ScreenBusyError`，绝不排队**——排队会把「基于旧界面做的决策」延迟执行，产出「看着成功实则打偏」的结果。锁可重入（`threading.local` 记账），故 `act_sequence` 能持锁跑完整串步骤。只读 a11y 与 `list_windows/screen_layout/wait_window` **不**加锁（不改屏状态；`wait_window` 还轮询 10s，持锁会白挡别人）。
  2. **沙箱闸门**（`_require_sandbox`）：isolated 且沙箱不在 → 先试重建，仍不行就抛 `SandboxUnavailableError` **拒绝执行**。**这条不能省**：`effective_display()` 在沙箱不可用时是**静默回落宿主**的，而 LLM 只看得到「操作成功」，会继续在真实桌面上点下去（实测走到过：多会话共享一块屏时建屏方退出，另一方就变成了宿主 `:1`）。确实要操作真实桌面请显式设 `CC_CU_DISPLAY_MODE=real`。
- **其余旋钮**：`CC_CU_SANDBOX_SCREEN`（默认 1600x1000）、`CC_CU_SANDBOX_WAIT_USER`（用户在沙箱内时注入礼让上限，默认 30s）、`CC_CU_SANDBOX_WM`（`none` 则沙箱内不起 i3）。
- **DISPLAY 贯穿方向**：`inject._run`/xclip/xrandr 的 subprocess env 与 mss 截图一律经 `display.env_for()`/`effective_display()` 取目标屏；**禁止各处直接读 `os.environ["DISPLAY"]`**（mss 旧版不吃 display 参数时由 backend 在锁内换 env 兜底）。
- **`env_for()` 与 `app_env()` 是两层，别合并**：`env_for()` 是**所有**子进程的 env 出口——换 DISPLAY、**主动剥掉 `AT_SPI_BUS_ADDRESS`**、**剥掉冻结产物的 `LD_LIBRARY_PATH`**；`app_env()` 专给启动**应用**用，在 env_for 之上**只多一件事**：isolated 模式下注入 `AT_SPI_BUS_ADDRESS`（real 模式不注入——用户明确要在真实桌面操作，行为应与手动启动一致）。
  - **为什么库路径剥离放在 `env_for()` 而不是 `app_env()`（2026-09-16 修的实测教训）**：`_strip_frozen_lib_path` 原先只在 `app_env` 里调，等于只保护「应用」，而**所有工具全体漏网**——实测取证（冻结产物实跑，读 `/proc/<pid>/environ`）：`Xephyr` / `dbus-daemon` / `at-spi2-registryd` **三者都**继承了产物库路径，而它们全是系统二进制。其中 `at-spi2-registryd` 链接的 libatspi/libdbus/libgio/libglib/libgobject/libX11 **产物里全都有**，一旦混装失败，表现是「私有 a11y 总线静默失效 → 无障碍能力整体消失而界面看不出异常」。修法就是把剥离下沉到 `env_for()` 这一层——它表达的是「我们启动的都是系统二进制，就该用系统库」，**对任何子进程都成立**。
  - 为什么不能把 **a11y 开关**塞进 `env_for()`：它被一堆无关子进程共用，加了既污染它们、也说不清意图。`tests/test_display_lifecycle.py::test_app_env_pins_sandbox_at_spi_bus` 断言了这条边界。**注意这条边界管的是 a11y 开关，不是库路径**——判据别写混。
  - **⚠️ 新增 spawn 点时**：`env=` 必须取自 `env_for()`/`app_env()`，**不要用裸 `dict(os.environ)`**（那等于继承产物库路径）。`tests/test_spawn_env.py` 会枚举 `src/` 下每一处 `subprocess.Popen/run/call/check_call/check_output` 来兜住这一点；例外要在它的 `EXEMPT` 里显式登记并写明理由（当前为空——全部 spawn 点都已接入）。
  - **为什么要主动剥 `AT_SPI_BUS_ADDRESS`（2026-09-16 修的实测教训）**：本进程连私有总线只能把地址写进**进程级** `os.environ`（libatspi 的 `atspi_init` 初始化时读它，没有别处可传），于是所有子进程天然继承。原先 `env_for()` 只是"不主动加"，没主动剥——后果是**用 `env_for()` 起应用（e2e 测试就这么干的）也能拿到私有总线，全靠这个继承**，属于「碰巧对」：谁先被 spawn、os.environ 当时有没有值，都会改变结果，顺序一变应用就静默落到宿主总线。现在 `env_for()` 显式 `pop`，应用必须走 `app_env()`——**要 a11y 就显式声明，别指望继承**。
- AT-SPI 走会话 D-Bus、与 display 无关：沙箱内应用照常上树；应用进沙箱的正路是 `launch_app` 工具（env DISPLAY=目标屏 Popen）。
  - **⚠️ 同源的第二个结论：光改 DISPLAY 也搬不动「会话总线单实例」的应用**（2026-09-24 实测）。GTK/GApplication 系（`gnome-terminal` / `gedit` …）在会话总线上注册一个众所周知的名字，第二次启动只是请**已有实例**开个窗口——而那个实例住在宿主的 `:1` 上。实测 `launch_app("gnome-terminal")` 返回 `display=":0"`、`ok=True`，**窗口却开在用户的真实桌面上**：`/usr/bin/gnome-terminal` 只是个 3400 字节的 D-Bus 客户端，真正开窗的 `gnome-terminal-server` 由会话总线 → `systemd --user` 激活，而 systemd 环境里写死 `DISPLAY=:1`（`systemctl --user show-environment` 可查）。这与「虚拟屏隔离不了 AT-SPI」是同一类穿透。
    - **修法**：`coordinator/windows.py` 的 `_DESINGLETON_ARGS` 表 + `_desingleton_argv()`，给这类应用补**它自己的**去单实例开关（`gnome-terminal` → `--disable-factory`，`gedit` → `--standalone`）。它自己拉起一个私有 server，环境由 `app_env()` 给，窗口天然落在目标屏；实测新 server 的 `DISPLAY=:0`、宿主零新增窗口，且应用仍有正常的会话总线（dconf / portal 不受影响）。
    - **⚠️ 别改成包一层 `dbus-run-session`**（那是最先想到、也实测过的那条路）：gnome-terminal 的客户端进程发完请求就退出 → `dbus-run-session` 随之退出 → 私有总线一死，刚开的窗口**立刻消失**。要让它可用就得再挂一个常驻进程替总线保活，等于每次启动泄漏一个守护进程。
    - 表里没有的应用**无法通用地修**（没有"去单实例"这种通用开关）——遇到时把该应用自己的开关补进表，**别加模糊匹配**（只认 `argv[0]` 的 basename，`test_desingleton_args_leave_everything_else_untouched` 钉住）。
- **⚠️ 由此推出一个致命结论：虚拟屏隔离不了 AT-SPI。** DISPLAY 隔离只挡得住 xdotool/mss 这类 **X11 通道**；无障碍遍历走的是**会话总线**，沙箱内外共用同一个 `at-spi2-registryd`。所以在 Xephyr 里跑 a11y 遍历，**照样能把宿主的 GNOME Shell 打崩**（2026-09-14 / 2026-09-15 两次实测事故）。**当前沙箱只保护「输入注入」与「截图」**——a11y 遍历的隔离见下条（**已实现**：私有 AT-SPI 总线）。
  - **2026-09-15 离线解 core 得到确切根因（别再按"量"去防）**：崩溃栈 `meta_context_run_main_loop → g_main_loop_run → libatspi → dbus_connection_dispatch → libatk-bridge → g_hash_table_foreach → g_object_ref → g_type_check_instance_is_fundamentally_a → SIGSEGV`。即 **gnome-shell 自己的 atk-bridge 在对已释放的 GObject 做引用计数（use-after-free）**，崩在主循环线程 → 整个桌面垮掉。**这不是总线拥堵、也不是树太大**——既有的一切预算/熔断/`max_nodes` 防护**都挡不住**（实测守着全部约定仍然崩了）。排查要点：`.crash` 里 `si_code=-6 (SI_TKILL)` + `si_pid==自身 pid` 是 **GJS 崩溃处理器重抛**（先打印 JS 栈再 re-raise），不是"自己发信号"；真·空指针是 `SI_KERNEL(128)`+`si_addr=0`。取 native 栈：`apport-unpack` 解出 `CoreDump` 后 `gdb -batch -ex "bt" /usr/bin/gnome-shell CoreDump`。
  - **✅ a11y 隔离已实现（私有 AT-SPI 总线，2026-09-15 端到端验证通过）**：沙箱启动时（`display._start_locked` → `_start_at_spi_bus`）自起一套私有总线 —— 私有目录里的 `dbus-daemon`（`accessibility.conf` + 显式 `--address`）+ `at-spi2-registryd`；沙箱应用（`app_env` 注入 `AT_SPI_BUS_ADDRESS`）与 MCP 自己的 `AtspiReader`（`coordinator._needs_display` → `backend.sync_at_spi_bus`）都连它。**宿主总线上完全看不到沙箱内应用**（实测：私有总线上 `iter_apps` 只有沙箱内那一个应用，gnome-shell 等宿主应用一个都不可见），**a11y 能力保留**。`stop()` 一并回收两个子进程与工作目录。
    - ⚠️ **绝不能用 `at-spi-bus-launcher`**：它按 `XDG_RUNTIME_DIR` 推导总线路径（`$XDG_RUNTIME_DIR/at-spi/bus_<n>`），会**抢占宿主同路径的 socket** —— 2026-09-15 实测踩到：宿主的 `at-spi/bus_1` 被顶掉，只能靠 `systemctl --user restart at-spi-dbus-bus.service` 恢复。自己起 `dbus-daemon` 并**显式给地址**，从构造上不可能碰到 `/run/user/*`。
    - ️ **libatspi 的 `atspi_init()` 是一次性的**：进程内第一次连上哪条总线就一直是哪条，改环境变量没用。而沙箱惰性启动且会重建（地址会变）——故 `sync_bus()` **只有「首次绑定」那一次真正生效**；此后连接已建立而总线要变时，它**保持原状 + 标记 stale + 报明确错误**（`_BUS_STALE_ERROR`，文案是「重启 Claude Code 可恢复」）。
      - **⚠️ 别改回「`exit()` 再重新 init」（那曾是这里的设计，实测救不回来）**：跨总线 `Atspi.exit()` + `Atspi.init()` 会损坏 libatspi 内部状态（`g_hash_table_insert_internal` 与 `g_object_unref` 断言失败），之后任何访问都报 APPLICATION_GONE——等于把 reader 弄成**永久失联**；何况 exit 之后本项目没有任何地方重新 init。完整理由见 `atspi.py:144-149` 的 docstring。
      - **运维后果（2026-09-20 实测踩到）**：沙箱**崩溃并被重建**（Xephyr 变僵尸 / 被回收）后总线地址必变，于是**本会话的 a11y 能力即永久失效**——`get_ui_tree`/`find_element`/元素级 `click` 一律报「沙箱 AT-SPI 总线已变化（沙箱重建过）」，只剩 OCR + 坐标点击那条最贵的路。**唯一恢复手段是重启 Claude Code**。看到这条报错**别去查无障碍开关或 pygobject**（它们没坏），直接重启。
    - **降级链**：私有总线起不来 → `app_env` 自动退回**死地址**（应用连不上 a11y，零注册零流量，宿主安全但无 a11y 能力）。死地址是刻意兜底：宁可没有 a11y，也绝不让流量落到宿主总线。
    - 验证：`PYTHONNOUSERSITE=1 "$PY" tests/manual_a11y_isolation.py`（内含**安全闸**：先断言 reader 绑在私有总线且看不到宿主应用，才继续遍历）。
  - **为什么用总线地址而不是 `NO_AT_BRIDGE`（实测教训，别改回去）**：`NO_AT_BRIDGE` 只是 GTK3 的开关（GTK4 用 `GTK_A11Y`），而 SWT（DBeaver 这类 Java 应用）**两个都不认** —— 其 `libswt-atk-gtk` 里没有任何相关字符串。`AT_SPI_BUS_ADDRESS` 则**工具链无关**（GTK3/GTK4/SWT/Qt/Electron 的 a11y 都走 libatspi）。实测（dbus-monitor 观察会话总线上 `org.a11y.Bus` 调用）：不设变量=1 次，指向死地址=**0 次**（**尊重该变量且不回落**）。
  - **另注（2026-09-20 实测修正）**：DBeaver **不是**灰区应用（a11y 树确实存在），但**能读到的只有菜单栏**——`get_ui_tree(app="DBeaver")` 的 150+ 节点**全是菜单**（菜单项、弹窗按钮齐全，`click(ref)` 元素级可用），而 **Database Navigator 里的连接/表列表根本不是 a11y 节点**（该面板只暴露两个 `scroll bar`）。所以**连接列表必须走 OCR + 坐标点击**，`find_element(text="开发环境")` 这类搜索对它一律无效。
    - **连库优先用 Enter**：选中连接节点后按 `Return` 即可建立连接并展开（出现 `> 数据库 / > 管理员 / > 系统信息`）。双击**现已支持**（`double_click` 工具 / `{op:"double_click",x,y}`），但那是 2026-09-22 才加的，见下文「三个鼠标动作」。
      ⚠️ **别再用「两次 click」拼双击，也别以为加个 `preview` 参数就能双击**：本文件原先记的归因（「click 步不支持 preview，所以点两次间隔必超阈值」）**只对了一半**——`preview=false` 只省掉抓图，落点证据的小块 OCR 照走（`with_text=shot is None` → True，约 0.3s），步骤之间还有调度开销，间隔仍不可控。双击的要害是「间隔必须极短」，那只能交给 X 侧定时。
    - **SWT 菜单项 `do_action` 报成功 ≠ 菜单真的展开**（实测「数据库(D) → 连接」元素级点击无效、节点图标仍是未连接的灰柱），别在菜单这条路上耗时间。
    - 连上后开编辑器的路径（都经坐标/键盘，不依赖 a11y）：工具栏「SQL」按钮（坐标 ~`(240,75)`）→ 菜单里按 **F3** 打开 SQL 编辑器 → **Alt+N** 新建脚本 → 编辑器里输入 SQL → **Ctrl+Enter** 执行。
- 用户可把键鼠伸进 Xephyr 窗口亲自操作（Xephyr 默认把宿主输入路由进嵌套屏）；坐标点击/键盘注入前 `coordinator._sandbox_guard` 轮询礼让（`wait_until_user_leaves`），超时带警告继续。元素级 do_action 与只读工具不检查。
- 沙箱未就绪时 `effective_display()` 回落宿主并告警一次（保证单测等未 start() 场景不指向空 display）——**回落不是隔离失效的静默通道**，server 正常路径必先 start()。

### 三级降级（coordinator.click 的灵魂）
① **element**：`backend.invoke()` → AT-SPI `do_action`，零坐标，首选；② **coord**：`geometry` 校准出屏幕绝对坐标 → xdotool 先聚焦窗口再点击；③ **screenshot**：找不到元素（灰区）时提示走截图。返回的 `ActionResult` 会标明实际生效层级——demo 证明 GTK 对话框坐标即使校准精确、合成点击仍会被「激活窗口」吃掉，所以**永远优先 element 级**。

### 省往返：`act_sequence` 是默认形态 + 落点证据 + 把观察折进来（实测数据支撑，改前先读）
**为什么这才是大头**：解析一条真实 DBeaver 任务（499s）得到——**工具自身执行 21s（4%），模型侧空档 478s（96%）**。工具（截图 0.2s / 点击 0.2s / list_windows 0.3s）根本不是瓶颈，**「模型每轮要看什么、要看几次」才是**。故一切优化都应指向「减少模型必须看的量 × 轮数」。

- **`act_sequence` 是模型该用的默认形态，不是可选加速器**——这条必须在**两个地方同时**说，缺一不可：
  - `server.py` 的 MCP `instructions`（模型**开箱必读**）：它的「标准工作流」天然会写成单步形式（`get_ui_tree → find_element → click`），**那等于教模型一步一次往返**。只把鼓励写在工具描述里是不够的——工具描述要等模型**已经决定用它**才读得到，而 instructions 在决定**之前**就把它带偏了。（历史版本正是这样：工具描述里喊着「省往返的关键工具」，instructions 却教单步。）
  - `tools/action.py` 的工具描述（模型决定用不用它时的唯一依据）。
  - 两处都**必须带判据**（「下一步不依赖上一步结果才合并」「需要看结果做分支判断就拆开」）。只鼓励不看判据会退化成盲跑长序列：界面一旦没按预期变，后续步骤全打偏，回头收拾比省下的还贵。
  - 守卫 `test_server_instructions_urge_act_sequence` 读 `create_server` 源码，断言 instructions 里既有 `act_sequence`、**也有**判据字样。删掉引导**不会报错**，只会让每个任务慢几倍——所以用测试钉住。
- **`{op:"ui_tree"}` 与 `{op:"screenshot"}`：把「下一步的观察」折进同一次调用**。分工是**看结构用 ui_tree（文本、便宜），看像素才用 screenshot（贵）**；灰区应用没有元素树，只有 screenshot 这条路。
  - `ui_tree`：补的是同一类浪费的**另一半**——「点开菜单 → 读菜单里有什么」原先也得拆成两次调用。树走 `message`（纯文本，天然可 JSON 序列化），**不**走截图那条 `_images` 旁路（别照抄 screenshot 的分支）。
  - `screenshot`：实测任务里三分之二的截图纯粹是「确认刚才那串动作对不对」。图像**不走 JSON**：挂在返回值的 `_images`（`[(bytes, meta)]`）下，`tools/action.py` 先 `pop` 再 `json.dumps`，然后作为额外 content block 追加（顺序：文本日志 → image → meta 文本）。
  - `_images` 是私有键，`coordinator` 之外的调用方序列化前**必须** pop 掉（测试 `test_act_sequence_screenshot_op_returns_image` 兜住）。
  - 锁可重入，故 `act_sequence` 持屏锁时再调 `screenshot_image` / `get_ui_tree`（前者挂了 `@_exclusive_screen`）不会自锁。
- **`click` 的 `text` 支持候选名列表**（`["保存","Save"]`，**命中即停**）：解决「不确定目标叫什么」（中英文界面按钮名不同）。原先只能点一个、失败再开一次调用试下一个，每个候选一次往返。
  - ⚠️ **语义是「命中即停」，不是「失败继续」**——这是设计要害，别改回去：`stop_on_error=false` 那种「失败就试下一个」在这里恰恰是**错的**。中文界面上第一个候选已经点中、对话框都关了，程序若还接着去找「Save」，运气好是白搜一次，运气差就点到别的窗口上。候选列表表达的是「同一个意图的几种写法」，故找到了就不该再看后面的。
  - ⚠️ **判定标准只能是「找不找得到」**：那是程序**自己**能确定的事实（在树文本里做字符串匹配），不需要判断力，所以放服务端做是安全的。反过来「点了保存、弹出『文件已存在，是否覆盖』」**判定不了**（属于「执行了但不对」），只能把结果交回模型。**别指望它替代分支判断**——那是能力边界，越界就等于要在 server 里塞一个大脑，而我们没有也不该有。
  - 命中后返回 **ref** 而非名字：调用方 `click(ref=...)` 走 `_resolve_native` 快路径、**不再重搜一次树**。全失败时**列出试过的每个名字**（只报最后一个会让模型以为只搜了一次，据此判断「这界面是英文的」就是错的）。
  - 守卫判据是**「搜了哪些名字」**而不是 `ok is True`：把实现改成「全搜一遍、取第一个成功的」照样能过 `ok` 断言，只有断言 `searched == ["保存"]` 才拦得住（`test_seq_click_candidate_names_stops_at_first_hit`）。
- **序列内的规模上限**（M-46，整串**持屏锁**执行，故步数/sleep/观察类步数都必须有界）：步数 50、单步 sleep 60s、wait 60s、截图 8 张、**读树 5 棵**。`ui_tree.max_nodes` 默认 **150**（刻意小于工具的 400——序列里读树是「顺手确认」不是全量分析），越界**当场报错**、不静默截断或降级（与 `screenshot.region` 的 M-41、`tools/ui_tree.py` 用 `Literal` 而非裸 `str` 同口径）。
- **落点证据**（`describe_point` / `active_window_title`）：坐标级点击与键盘注入的 `ok=True` **只说明「事件发出去了」**——xdotool 不关心点到了什么，所以模型只能再截一张图确认，那是整整一个来回。现改为回报 `落点：窗口「X」 WxH ｜ 落点文字：「确定」 ｜ 点后活动窗口：无（原「X」已消失）`。
  - **必须在点击之前取**（`_point_evidence` 会 mousemove 去问 X「那儿是哪个窗口」，点完弹窗可能已盖住原位置）。
  - **⚠️ 先读活动窗口再取落点证据**：顺序**不能反**（`describe_point` 会挪指针）。但要注意归因——沙箱里的 i3 配置**已显式** `focus_follows_mouse no`（`display.py` 的 `_SANDBOX_I3_CONFIG`），**挪指针并不会切换焦点**；这里坚持「先读后取」是**刻意不依赖那条配置**（`CC_CU_SANDBOX_WM=none` 时沙箱内根本没有 WM，X 退回 `PointerRoot` 语义，此时指针位置**确实**决定键盘去向）。**排查「活动窗口不符」时别往 `focus_follows_mouse` 上想**（D-1/M-8）。
  - **「点前活动窗口」不能省**：点「确定」这类按钮会把对话框关掉，点后已无标题可读——**最关键那一刻的证据反而丢**。有前值才能说 `无（原「X」已消失）`，而这本身就是「点中了、窗口确实关了」的强信号（端到端实测踩到过）。
  - 裸坐标点击（灰区）带 `with_text=True`（OCR 落点周围 320x64 小块，约 0.3s）；带 ref 的坐标兜底带 `with_text=False`（元素名已知，省那 0.3s）。
  - **落点证据一律不得让主操作失败**：`describe_point`/`active_window_title` 出错只记 debug 日志、返回空 dict。
  - 元素级 `do_action` **不需要**它——不存在「点没点中」的疑问。

### 点击反馈三件套：光圈（给人看）+ 准星图（给模型看）+ 程序算偏差 + 回看

坐标点击**只存在于 tier ② 兜底与灰区裸坐标**（元素级零坐标，不存在「瞄哪」的问题，故一概不加）。这四样东西全部由 `backend.click_at` 这个**唯一漏斗**自动附带（`actions.py:56/:129` 是仅有的两个调用方）。

- **光圈**（`backend/linux/ring.py`，给人看）：落点亮一个实色红环、**1.5s** 自毁，`install` 级无副作用。
  - **TTL 不能短于 ~1s（实测教训）**：初版 0.4s，用户在沙箱窗口前**根本没看到**，第一反应是「是不是双击不画圈/功能没生效」。用探针在落点周围抓**纯红**像素才拿到确证：圈**确实画了**（稳定 536 像素 = 48px 环带的理论值，存活 0.35s），只是闪得太快——人眼当时正看着 Claude Code 界面，48px 细环在 1600x1000 屏上一眨眼就错过。**排查「没看到光圈」时先量时长，别急着怀疑挂载点或熔断**（挂载点是无条件的，见下条）。
  - ⚠️ **探针判据必须严格（`R>240 且 G<40 且 B<40`）**：宽松判据（`R>150,G<90`）会把 **DBeaver 选中行的棕红背景**当成光圈——实测假阳性 1759 像素，而真圈只有 536。光圈的红色是 X11 的 `0xFF0000` 实色、不抗锯齿不合成，严格判据正好，宽松判据等于没测。
  - **⚠️ 「探针读到圈」≠「用户看得到圈」（2026-09-20 实测，排查时最费时的一条）**：探针（mss / `XGetImage`）读的是 **X server 内部状态**，而用户看的是 **Xephyr 往宿主窗口渲染的那张画面**。沙箱一旦卡死/崩溃（Xephyr 变 `<defunct>`，i3 与 at-spi2-registryd 一起变僵尸），宿主窗口就**停止刷新**——于是「圈确实画了（探针稳定读到 536 像素、持续 1.36~1.47s）」与「用户死活看不到」**同时为真**，两个都对却互相矛盾，极易把排查带偏。
    - **判据三条**：`pgrep -a Xephyr` 出现 `<defunct>`；`ls /tmp/.X11-unix/` 里沙箱屏号对应的 socket 已消失；`DISPLAY=:N xdotool getactivewindow` 报 `Can't open display`。任一命中即**不是光圈的问题**。
    - **处置**：重建沙箱即可（随便调一次 MCP 工具就会触发惰性重建），重建后光圈立刻恢复正常。注意**重建会让 a11y 失效**（见上文 `sync_bus` 的运维后果），要恢复 a11y 仍须重启 Claude Code。
  - **外径 `_SIZE=48`、线宽 `_THICK=4` 是纯观感参数**（初版 100 太大——圈本身盖住了落点周围正是人想看的那些字与控件）。它不承担任何度量职责：要量偏差看准星图与程序报的偏差值。改这两个数**只需改 `ring.py`**：`test_e2e_zenity` 的像素判据是**从模块读尺寸**换算采样点的，不写字面量（写死会让用例在调参后「采样点落到圈外」，判据还在、测的已经不是那个圈了）。
  - **进程内 daemon 线程 + 队列，不起子进程**：① python-xlib 的 `Display` **非线程安全**，全部 X 流量必须关进单一 owner 线程；② X 协议保证连接断开时 server 释放该连接的全部窗口 → **SIGKILL 也不留孤儿，不需要 atexit**，也就不用碰「冻结产物自 spawn 与 `env_for` 剥库路径」的冲突（`test_spawn_env` 因此零改动）。
  - **画在注入之前**（`click_at` 开头）：xdotool 的 `windowactivate --sync` 要几十~上百毫秒，圈先亮、点击随后落；放之后则「注入抛异常时人看不到任何痕迹」。
  - **必须实色**：沙箱 Xephyr 里 i3 不做合成，ARGB 窗口的 alpha 会被忽略成脏块。
  - **点击穿透靠 SHAPE 的 Input 形状置空**（宿主 mutter 与 Xephyr 实测均为 SHAPE 1.1）；无 SHAPE 时退化为「四条细矩形拼取景括号」，**中心留空**——圈是在点击前画的，挡住自己点的那一下就是制造新 bug。
  - **失败按 display 熔断**（连续 2 次才停该屏、成功清零）：沙箱屏号会变，全局布尔会被一次早已无关的故障永久锁死；`CC_CU_CLICK_RING=0` 可整体关。
  - **截图与点后对比之前都会 `ring.clear_now()`**（无圈时只是一次布尔判断，零成本）：① `screenshot()` 开头——`act_sequence` 里 click 步紧接 screenshot 步只隔 ~250ms；② `change_fraction_since()` 抓「点后图」之前——它在点击后 ~0.2s 被调用，而圈的寿命是 1.5s，**必然还在**；圈是点击**之后**才画的，拿它跟点击前的 `before` 图比，会凭空多出 536 个变化像素（约 0.37%），而「无变化」的阈值是 0.5%——属于**侥幸不误报**，不是设计保证（圈再大一点、裁剪区再小一点就会翻车）。
- **准星预览图**（给模型看）：点击**前**抓 480x300 裁剪、红十字标在落点上、**不缩放**（scale 恒为 1，图上 1px = 屏幕 1px，模型零换算），JPEG q=85 + `subsampling=0`（4:2:0 会糊掉 1px 细红线）。~190 token/次，单发坐标点击默认附带；`preview=false` 连抓都不抓（零成本）；`CC_CU_CLICK_PREVIEW=0` 全局关。
  - **★ 准星像素 = 落点 − 裁剪后原点**（`px = x - origin[0]`）：`_clamp_region` 在屏幕边缘会改原点，写死半宽 = 越靠屏幕边缘的按钮准星指得越离谱 —— 这个功能存在的唯一理由就是「位置对不对」，指错就是主动误导。有专门测试钉住。
  - **准星画在副本上**（`ClickPreview.image` 是**未画准星**的原始图）：原始图要复用给 OCR（红线横穿文字会降低识别率）与点前后像素对比（红线会被误当「变化」，让每次点击凭空多几个百分点）。
- **程序算偏差**（`_assess_aim`，纯函数）：落点坐标是程序自己发出去的（绝对精确），文字块位置来自 OCR（TSV 自带 left/top/width/height）——**偏差就是坐标系减法**，没有任何智能判断。
  - 落点文字 / 最近候选（最大中心距 64px，超出不指认，免得把远处无关文字当成目标）/ **`expect` 声明后的精确偏差 + 建议改点坐标**（`expect="保存"` → 「未命中，偏差 (+17,-42) 共45px，建议改点其中心 (517,258)」）。
  - **`expect` 是把启发式变成精确值的关键**：没有它，程序不可能知道模型想点谁。找不到就明说「裁剪内未见该文字」，不猜。
  - **点后界面变化**（`changed_fraction`，同区域点前/点后像素对比，<0.5% 无 / <5% 轻微 / ≥5% 明显）：这是**不需要知道意图**的独立第二信号。两个信号要一起读：偏 45px 但界面变了 = 按钮热区比文字大、其实点中了；偏 45px 且毫无变化 = 该修坐标了。措辞只报事实（0.2s 窗口，慢界面可能滞后）。
  - 一次裁剪 OCR 三用（落点文字/候选/expect），省掉独立的 `_text_near` 小块 OCR；有裁剪图时才走这条，没有（preview=false）就退回旧的 `describe_point(with_text=True)`。
- **回看通道**（`get_last_click_image`）：`landing._CLICK_LOG` 是**模块级** deque(maxlen=8)，记最近 8 次坐标点击（含 `act_sequence` 内的，**只记录不附图**——逐张 attach 是 token 反模式），带坐标、评估文字与准星图。
  - 该方法是**纯读本进程内存**，故在 `test_coordinator_hooks` 的 `needs_display` 与 `_EXCLUSIVE_EXEMPT` 两处都显式登记了豁免（挂钩子会让「看一眼上次点了哪」去拉起 Xephyr）。
  - 它同时解决了「点完发现不对但界面已变」这个盲区，以及「盲目重复点击」这个最糟的应对（工具描述里写明了先回看再修坐标）。

### 三个鼠标动作：双击 / 滚动 / 拖拽（**全是坐标级**）

`double_click` / `scroll` / `drag` 三个独立工具（同时接进 `act_sequence` 的三个同名 op——独立工具负责「模型能发现」，op 负责「批量时不掉回慢路径」）。**它们都只能走坐标级**：AT-SPI 的 `do_action` 只有 activate/click，没有双击、没有滚轮、没有拖拽。故反馈链路沿用 `click_xy` / `drag_xy`（拖拽只取**起点**的落点证据——终点由参数给定不会有歧义，抓错起点才是这类操作的典型失败）。

- **双击的间隔必须交给 X 侧定时**（`xdotool click --repeat 2 --delay 100`）。实测用 `xev` 量过真实事件时间戳：两次 ButtonPress 相差**正好 100ms**，远小于系统约 400ms 的双击阈值。
  ⚠️ **绝不要改成「调用方连调两次 `click_at`」**——那正是它历史上做不到的原因：两次独立调用之间夹着落点证据、OCR、变化对比与步骤调度（约 0.5s），必然被认成两次单击。也别指望「关掉那些副作用就能压进阈值」——那只是把间隔压到另一个同样不受控的值。守卫 `test_seq_op_double_click_sends_repeat_not_two_calls` 断言的是**传给 backend 的 `repeat`**，不是 `ok`（改成连调两次照样能过 `ok`）。
- **拖拽必须插值分步移动**：X 只发一个 MotionNotify 时，很多应用判定不出「按住拖动」（拖放目标不激活、画布不跟随、滚动条回弹），表现是「拖了但没反应」而 xdotool rc=0、看着完全成功。故起点与终点之间插 `steps` 个中间点、每点之间停顿 hold 秒。**整条链拼成一条 xdotool 命令**（含 `sleep` 子命令）——分成 N 次 `_run` 时每次进程启动开销（5~10ms）会盖过 hold 本身，节奏失真。失败**不重试**：重试一次拖拽 = 再拖一遍（文件移动两次、画两笔），后果不对称。
- **滚动的最小单位是「刻度」，没有半格**：一次滚轮事件就是一格（button 4=上 / 5=下，`amount` 走 `--repeat`）。
  ⚠️ **别引入「圈」这类单位**——那是物理鼠标的转数，X11 里根本不存在，「一圈几格」只能靠我们拍脑袋约定（还取决于鼠标型号）；而且「一格滚多少内容」由**应用**决定（GTK 约 3 行、浏览器按比例、画布应用按像素），所以**无论用什么单位，模型都预测不出滚完会到哪**。既然都没有预测力，就用与底层一致的那个，少一层可能算错的换算。**正确用法是「滚一下 → 看结果 → 不够再滚」**，配合 `act_sequence` 的 `ui_tree`/`screenshot` 一次提交。
  - 作用点必须有明确来源：给了 `x/y` 就用它，否则取**活动窗口中心**——绝不沿用「指针恰好在哪」（那是上次操作留下的位置，不可预测，排查时也毫无线索）。滚动**不聚焦窗口**（`focus_window=False`）：看内容而已，抢焦点纯属副作用。
- **按钮名的映射只有一份**（`backend/base.py::BUTTON_NUMBERS`：left=1 / middle=2 / right=3）：工具层与 `act_sequence` 都收**语义名**，两处共用同一张表。
  ⚠️ **别让模型记数字**：X11 里 **2 是中键、3 是右键**，写反不会有任何报错，只会右键变中键。工具层用 `Literal` 在**参数校验阶段**就挡下非法值（实测报错原文会列出可选集合）。
- 规模上限沿用坐标动作那一套：单次滚动 `amount ≤ _SCROLL_MAX_AMOUNT`、拖拽 `steps ≤ _DRAG_MAX_STEPS`（都是持屏锁执行的坐标动作，无上限时能长时间占屏）。

### ref 机制（关键隐含假设）
MCP server 是**单个长驻进程**，故 `utils/refs.py` 的 `RefTable` 直接缓存「活的」AT-SPI Accessible 对象（`UINode.native`），`ref(int) → 活对象`。`get_ui_tree`/`find_element` 分配 ref，`click(ref)`/`type_text(ref)` 用 ref 定位。**这依赖同进程内存**——不可跨进程/序列化传递 native 对象。ref 失效时（界面变化）`coordinator._resolve_native` 会用 meta(role+name+app) 重定位。

### 坐标校准（core/geometry.py）
GTK 对话框 `get_extents(SCREEN)` 常返回相对窗口原点的漂移坐标。公式：`屏幕绝对 = 窗口真实位置(xdotool按PID查) + 元素WINDOW相对坐标`。`resolve_element_screen_rect` 会判断 SCREEN 是否可疑并自动决定用 screen 还是 calibrated。

### 省 token 序列化（core/serializer.py）
无障碍树→紧凑文本：去噪（无名空 panel/filler 不单独成行，子节点 flatten 上提）、`interactive_only` 过滤、`max_nodes` 截断**必带提示**（不静默截断）、可操作节点分配 `[ref]`。

### 注入与遍历的安全边界（实测确认的系统级坑，改动前必读）
- **`type_text` 非 ASCII 必须走剪贴板**（xclip + 粘贴键，`inject._type_via_clipboard`）：`xdotool type` 对中文会**临时改写全局键盘映射**（XChangeKeyboardMapping），输入期间用户物理键盘整体失灵（鼠标正常）、写完才恢复，且与 fcitx 输入法争抢 XKB 状态。纯 ASCII 且 <24 字符才走 xdotool type。
  - **⚠️ 粘贴键**不是**恒为 `ctrl+v`**：目标是**终端仿真器**时必须发 `ctrl+shift+v`（2026-09-24 实测）。VTE 的粘贴键是 `Ctrl+Shift+V`，而 `Ctrl+V` 会被原样写进 pty（字节 `0x16`），bash/readline 把它当 `lnext`（引用下一个字符）——后果是**静默失败**：`type_text` 报成功、屏幕上一个字都没进来，紧随其后的那个字符还被吃掉。实测 37 字符的命令经剪贴板路径后终端提示符上**只剩一个 `^V`**；同一串 `ctrl+v` 打在 GTK 输入框（zenity）里则完整粘贴成功，所以这不是 xclip 的问题。
  - 判定在 `keyboard._paste_combo()`：读活动窗口 WM_CLASS（`_TERMINAL_CLASSES` 白名单）→ 是终端就换键；**探测失败一律退回 `ctrl+v`**（它只是"帮个忙"，绝不允许把原本能用的输入弄坏，故整个探测包在 try/except 里）。`CC_CU_PASTE_KEY` 可强制指定，用于表里没有的终端。
  - **⚠️ 读 WM_CLASS 不能走 xdotool**：本机 `xdotool 3.20160805.1` 的命令表里**没有 `getwindowclassname`**（`xdotool help` 列出的只有 getactivewindow / getwindowfocus / getwindowname / getwindowpid / getwindowgeometry），调它只得到 `Unknown command` + 空 stdout——**静默返回 None**，于是"看起来检测了、实际永远退回 ctrl+v"。现在走 `wmctrl -lpx`（已随包分发、`apps.py` 也在用它读 WM_CLASS），并且 **id 必须 `int()` 比**：xdotool 给十进制、wmctrl 给零填充十六进制（`0x00c00006`），比字符串永远不相等且不报错。这两条都只在真机上才暴露（单测里 stub 掉的方法照样绿），所以有真机复验记录在下面。
  - 真机复验（源码直打沙箱屏 `:0`）：`launch_app("gnome-terminal")` → argv 补上 `--disable-factory`、窗口落在沙箱、宿主零新增；40 字符命令经剪贴板路径**真的进终端并执行成功**；GTK 输入框仍走 `ctrl+v` 且粘贴正常（反向没改坏）。
- **`find()` 全桌面搜索有访问节点预算熔断**（`atspi.py`：`_VISIT_BUDGET_DESKTOP=6000`；带 app 限定时改用 `_VISIT_BUDGET_SCOPED=24000`——限定范围后风险可控，故额度反而放宽）：Chromium/Electron 应用（Chrome/飞书/QQ）的 a11y 树可达数万节点，裸搜全桌面会把 at-spi2-registryd 与会话 D-Bus 打满，连带 **GNOME Shell 卡死（表现为系统崩溃）**。工具层调用务必带 app/text 限定。
  - **且不带 root 时会先用 X11 侧 pid 集合跳过无窗口应用**（2026-09-20 修，与 M-23 在 `get_active_window` 的修法同源）：gnome-shell / 输入法 / at-spi 注册器这类**没有客户窗**（`wmctrl -l` 只列受 WM 管理的窗口）、`child_count` 恒为 0 的应用，原先无条件吃掉 `_APP_TOUCH_BUDGET=8` 的名额，表现是「按 text 找不到元素」→ 模型白掉到截图那条最贵的路。预滤成立的前提是「`_NET_WM_PID`」与「AT-SPI `get_process_id`」指向同一进程，**单测（假 pid）测不到这条**，故有真机探针 `tests/manual_find_prefilter_probe.py`；改这块后跑一遍。空结果时的 notice 必须区分「没搜到」与「没去看」，不可静默。
- **GTK 角色/命名的现实**：输入框 role 是 `text` 而非 `entry`；GTK 文件对话框的文件名框 accessible name 为**空**（标签是旁边的 `label | 名称(N)`），按名字 find_element 搜不到——要从对话框树（`get_ui_tree` active_window）按结构定位第一个 `[ref] text`。
- **`_read_titles` 一律逐窗取标题，不要改回批量形式（2026-09-16 实测修正，I-7）**：本机 xdotool 3.20160805.1 下 `xdotool getwindowname wid1 wid2 …` **只打印第一个窗口的标题**，其余报 `Unknown command`（rc=1）。所以历史上那句「行数≠窗数即降级逐窗」的防御**恒被触发**——每次都要付 N+1 个进程（1 个必然失败的批量 + N 个逐窗），而 `wait_window` 是**轮询**调用（0.25s 一次、最长 10s），是持续开销。更要命的是它留了唯一的击穿通道：只要首个窗标题里的换行数恰好 = 窗数−1，行数就会相撞、防御放行，后续窗口集体安上前一个窗的标题碎片（静默错答）。现在直接逐窗（N 个进程，比原来更省），`window_title()` 复用它是为了「换行压成空格 + 空串归一为 None」。另过滤无名/零面积辅助窗。
- **`child_count(app)` / `child_at(app, i)` 是全项目最危险的单次调用**（2026-09-14 GNOME Shell 崩溃事故根因，血泪教训）：它们不是「读一个数字」——AT-SPI 里会**逼目标应用惰性构建整棵无障碍树**。Chrome/Electron/微信/QQ 单棵树上万节点，对桌面上 N 个应用连调，瞬间打满 at-spi2-registryd 与会话 D-Bus，**连带拖崩 GNOME Shell**（apport 实录：`gnome-shell signal 11`，并连锁崩掉 Chrome/Nexus/aTrust）。据此确立的判据与改动：
  - **安全**：`get_name` / `get_process_id` / `get_role` 是纯属性读，不触发建树。
  - **危险**：`child_count` / `child_at` / `get_extents` 都会触发建树；**对「每个应用」批量调用它们之前必须想清楚代价**。
  - `list_apps` 已**改走 X11**（`wmctrl -lpx` 读 WM_CLASS，零 a11y 成本），`AtspiReader` 里刻意**不再保留** AT-SPI 版实现——别加回来。
  - `get_active_window` 改为**按 pid 精确定位**（只读 `get_process_id`），受 `_APP_TOUCH_BUDGET` 约束；不再对每个应用调 `child_count`。
  - `get_tree(scope='desktop')` 受 `_TREE_APP_BUDGET` 约束，超限截断并**告警**（不静默截断）。
  - 出问题时先看 apport：`grep -E "gnome-shell|computer-use" /var/log/apport.log`，能直接看到崩溃进程与信号。
- **Xephyr 与 xdotool/libatspi 同级是 OS 级依赖**：冻结产物不内嵌，运行时需系统提供（`sudo apt install xserver-xephyr`）。

## 启动顺序约束（最易踩的坑）

`_bootstrap.py` 必须在**任何 `import gi` 之前**执行 `setup_gi_environment()`：conda 自带 GLib/GObject typelib 但**没有 Atspi**，需把 `GI_TYPELIB_PATH` 指向系统 `/usr/lib/x86_64-linux-gnu/girepository-1.0`。`server.py` 顶部已保证此顺序，新增任何会 import gi 的入口都要先 import `_bootstrap`。

**AT-SPI 依赖系统库**（`libatspi`、Atspi typelib、xdotool），无法打进单一二进制——冻结后运行时仍需系统提供，这是已接受的 OS 级依赖。

## 打包坑（build.sh 已固化，改打包时注意）

- **布局是 onedir + wrapper，不是 onefile**：产物为 `dist/computer-use-mcp-bin/`（目录，**实测 455MB**——可执行文件本身只有 9.5MB，其余是 `_internal/` 里的打包库），另生成 `dist/computer-use-mcp` 薄 shell wrapper 转发进去。**wrapper 是为了保住已注册路径**——`~/.claude.json` 里写死 `dist/computer-use-mcp`，删了它 MCP 就找不到可执行文件了。选 onedir 是因为 onefile 每次启动都要自解压到临时目录（秒级冷启动开销）。
- **`LD_LIBRARY_PATH` 必须前置 conda 的 `lib`**：否则 PyInstaller 用系统旧 `libcrypto` 配 conda 新 `libssl`，运行时报 `OPENSSL_3.3.0 not found`。
- **`GI_TYPELIB_PATH` 要点到系统 typelib 目录**：让 PyInstaller 的 gi hook 能定位系统 `Atspi`（conda 里没有）。
- 用 `--collect-submodules mcp.server` 而非 `mcp`：全量会拉进需 `typer` 的 `mcp.cli` 导致构建失败（另显式 `--exclude-module mcp.cli/typer/tkinter`）。
- `--copy-metadata mcp/mcp-types/pydantic/anyio/cryptography`：这些库用 `importlib.metadata` 读版本。
- **`--collect-submodules Xlib` + 显式 `--hidden-import Xlib.ext.shape / Xlib.support.unix_connect`**（点击光圈用）：`Xlib.ext` 的扩展模块（shape 就在其中）是**按扩展名动态导入**的，不出现在任何静态 import 图里——漏了的表现是「开发态一切正常、**只有冻结产物里没有光圈**」。python-xlib 是纯 Python、走 socket 不链 `libX11`，故不引入产物库与系统库混装的风险。守卫测试 `test_ring_and_preview.py::test_build_sh_collects_xlib` 读 build.sh 源码兜住这一点。
- 入口是根目录的 `entry.py` 而非 `server.py`：后者用相对导入（`from . import _bootstrap`）不能当脚本跑；`entry.py` 只做一件事——绝对导入并调 `server.main()`。
- `build.sh` 开头 `rm -rf build dist computer-use-mcp.spec computer-use-mcp-bin.spec` 会**清掉整个 dist/**（wrapper 也在内，每次重建），别把它当"增量构建"用。**spec 名必须与 `--name` 一致**：PyInstaller 生成的 spec 是 `name + '.spec'`，故本仓库真正要清的是 **`computer-use-mcp-bin.spec`**——历史上那行只写了 `computer-use-mcp.spec`（**该文件从来不存在**，等于清理动作没做，实测残留的 spec 一直是 `-bin.spec`）；现已两个都删，兼容换过 `--name` 的旧工作区。（M-45）
- 环境名/路径可用 `ENV_NAME` / `CONDA_BASE` 覆盖，默认 `cc-computer-use` + `$HOME/anaconda3`（换机器打包时用得上）。
- **冻结产物仍依赖系统提供 `xdotool` / `Xephyr` / `i3` / `dbus-daemon` / `at-spi2-registryd` / `tesseract`** —— 那 9 个**外部命令**没有内嵌，是已接受的 OS 级依赖（`.deb` 里由 `vendor/bin` 提供）。
  ⚠️ **但 `libatspi.so.0` 与 `Atspi-2.0.typelib` 现在随产物嵌进 `_internal/` 了**（2026-09-23，见下条），别再按老说法把它们算作"系统必须提供"。

## 分发形态与 `.deb` 打包（`packaging/`，2026-09-23 新增）

三种产物：**本机 `build.sh`**（开发用）、**`.deb`**（Ubuntu 用户）、**`.mcpb`**（Claude Desktop）。后两者共用 `packaging/build-in-container.sh` 一次产出，**必须在 ubuntu:22.04 容器里跑**（24.04 上取到的系统二进制要求 GLIBC 2.38，会恰好排除掉 22.04 这个最低档）。

```bash
# 产物落在**本仓库的 dist/**（不是 /tmp：重启即失，且容器以 root 写入、属主是 root）。
# 两个路径分工，别合并：OUT=/out/dist（挂宿主 dist/，只放最终产物）
#                          WORK=/out/work（容器内，PyInstaller 的中间产物）
# 分开的理由：宿主 dist/ 里已有 build.sh 的 dist/computer-use-mcp-bin/（24.04 基座），
# 与本脚本的 PyInstaller 同名 —— 共用会把两边互相覆盖，而 dist/computer-use-mcp
# （已注册的 MCP 路径）正指向它，出问题看不出跑的是哪个基座。
# CC_CU_CHOWN 交还产物属主：容器以 root 写宿主目录，否则组装目录没有 sudo 删不掉。
mkdir -p dist
docker run --rm -v "$PWD":/src -v "$PWD/dist":/out/dist \
  -e CC_CU_CHOWN="$(id -u):$(id -g)" \
  -v /tmp/Miniforge3-Linux-x86_64.sh:/miniforge.sh:ro \
  -e CC_CU_MINIFORGE_SH=/miniforge.sh \
  ubuntu:22.04 bash -c 'bash /src/packaging/build-in-container.sh'
# → dist/cc-computer-use_0.1.0-1_amd64.deb（实测 68MB，96 个随包库 64.7MB）
#   外加 dist/cc-computer-use/（组装目录，.deb 的输入，留着可直接重打）
# ⚠️ 默认**只出 .deb**，不打 .mcpb（zip 那 300MB 要 2 分钟，本次分发用不到）；
#    要 Claude Desktop 的包就加 `-e CC_CU_MCPB=1`。

bash packaging/deb/verify-install.sh dist/*.deb ubuntu:22.04   # 干净容器验收（10 步）
bash packaging/deb/verify-install.sh dist/*.deb ubuntu:24.04   # 高版本再验一遍

# 只改了打包脚本/control 文件时**别重跑上面那 19 分钟**——组装目录已经有了，
# 直接重打 deb 只要约 1 分钟（实测 1m07s）：
docker run --rm -v "$PWD":/src:ro -v "$PWD/dist":/d -v /tmp/o:/o ubuntu:22.04 \
  bash -c 'bash /src/packaging/deb/build-deb.sh /d/cc-computer-use /o'
```

⚠️ **`dist/` 现在同时住着两种产物**：`build.sh` 的（`computer-use-mcp-bin/` + wrapper
`computer-use-mcp`）与容器构建的（`.deb` + `cc-computer-use/`）。**`build.sh` 的清理动作
已改成只删自己那两个**——别写回 `rm -rf dist`，那会把 68MB 的包和 300MB 的组装目录一起抹掉，
而用户不会预期「跑一次本机开发构建」会顺手删掉分发产物。

**验收状态（2026-09-23）：22.04 与 24.04 两个干净容器各跑一遍，10 步全过。** 另实测产物中所有 ELF 的最高 `GLIBC_*` 需求 = **2.35**（正好是 22.04 的基线；`Xephyr` 与 `libXfont2.so.2` 是最高那两个）—— 这是「在 22.04 里构建」这个约束要保住的东西，换构建基座前先重新量。

⚠️ **验收用例不许依赖宿主 locale**（同一批实测踩到）：`mcp_smoke.py` 原先按 zenity 的**中文默认标签「是」**找按钮，开发机上一直绿（宿主认中文 locale），一到干净容器就红 —— 那里没配 locale，GTK 渲染成 `Yes`/`No`。这不是产品缺陷，是用例偷偷依赖了宿主语言环境，属于「在开发机上永远重现不了」的假红，最耗排查时间。现在用 `--ok-label` 把标签写死成 ASCII，判据与语言无关；`tests/test_packaging.py::test_smoke_pins_zenity_button_labels` 钉住（断言 `re.search` 那几行里不许有中文字面量 —— 报错文案里的中文不算）。

几条**改之前必须知道**的：

- **`libatspi.so.0` / `Atspi-2.0.typelib` 必须落在 PyInstaller 产物的 `_internal/` 下**（`packaging/embed-atspi.sh`，两条构建路径都调它）。目标机可能既没装 `libatspi2.0-0` 也没装 `gir1.2-atspi-2.0`，缺了它们 `import Atspi` 直接失败 → 无障碍能力整体消失（只剩最贵的 OCR 那条路）。落点各有硬理由，**别挪**：
  - `libatspi.so.0` → `_internal/`：引导器把 `_MEIPASS` 设进 `LD_LIBRARY_PATH` 故能找到；**且它在 `_MEIPASS` 之下，会被 `_strip_frozen_lib_path` 自动从子进程 env 里剥掉** —— 安全不变量由构造保证。实测 `LD_DEBUG=libs` 只有一条 `trying file=<...>/_internal/libatspi.so.0`，系统那份一次都没开。
  - `Atspi-2.0.typelib` → `_internal/gi_typelibs/`：PyInstaller 的 `pyi_rth_gi` 运行时钩子**无条件赋值**（不是追加）`GI_TYPELIB_PATH = <_MEIPASS>/gi_typelibs`。放别处、或指望 wrapper 设那个变量，都会被直接覆盖成死配置。实测 `strace -e openat` 只打开这一处。
  - **⚠️ 光拷 `Atspi-2.0.typelib` 一个文件不够，必须连它的依赖闭包一起拷**（2026-09-23 在干净容器里抓到的）：typelib 头部声明了自己的依赖，`Atspi-2.0` 是 `GObject-2.0|GLib-2.0|DBus-1.0`，而 **`DBus-1.0` 来自 `gir1.2-freedesktop`**（不是 at-spi2-core 的包）。只拷一个的表现是自检报 `ImportError: Typelib file for namespace 'DBus', version '1.0' not found` → 无障碍能力整体消失。**这个坑在开发机/构建机上永远重现不了**：`_bootstrap` 会把系统 typelib 目录追加进来兜底，只有目标机没装 `gir1.2-*` 时才现形。故 `embed-atspi.sh` 用 BFS 遍历闭包（从 typelib 自身解析，不硬编码清单；构建机缺包时**当场报错**，不静默跳过）。
    - **解析那行依赖有三条硬规则，别退回宽松版**（同一处连挨两次）：① **只在前 4KB 找** —— deps 是头部的一项（偏移 ~0xa7），全文件扫描会把后面 MB 级元数据段的随机字节当依赖，实测 `GLib-2.0.typelib` 在**第 6906 行**匹配到 `i|E`，于是队列里多出一个叫 `i` 的「依赖」，构建当场失败；② **每个元素必须带 `-版本号`**（GIR 命名空间永远是「名字-版本」），这条拦住裸词 `i`，顺带拦住共享库名（`libgio-2.0.so.0` 版本号后面跟的是 `.so.0` 而非 `.数字`）；③ **两种存储格式都要认** —— 老格式是一个 `|` 分隔的串，新格式（gi 1.76+，conda 那份 pygobject 就是）是一串**独立的 NUL 结尾串**，所以必须取头部**所有**匹配行再拆；旧实现用 `grep -m1` 只取第一条，在新格式下**只能拿到一个依赖**且完全静默（同一类「构建机上能用、目标机缺件」的坑）。已用 24.04 上 **83 个系统 typelib** 全量扫过验证零假阳性；`tests/test_packaging.py` 里有两个**真跑这个函数**的用例（构造老/新格式与噪声样例喂给它），不是断言源码文本。
- **⚠️ 绝不能把 `vendor/lib` 设进 `LD_LIBRARY_PATH`**：`_strip_frozen_lib_path` 只剥 `_MEIPASS` 之下的路径，`vendor/lib` 不在其下 → 会被**沙箱应用**继承，而那里有约 50 个构建基座的库（libxml2/libcrypto/libicu*…），正是 2026-09-15/16 两次混装事故的同一类路径。`vendor/bin` 那 9 个二进制靠 patchelf 写死的 RPATH `$ORIGIN/../lib` 自定位，**本来就不经过环境变量**。`packaging/assemble.sh` 的 manifest 里那两行（`LD_LIBRARY_PATH`/`GI_TYPELIB_PATH`）已删，`tests/test_packaging.py` 钉住不许加回来。
- **`.deb` 的 `Depends:` 只有 `libc6 (>= 2.34)`**：目标机**取不到 apt 源**，任何一条依赖落空都会让安装直接失败且无法补救。也**刻意不写 Recommends**（无源时 apt 解析它可能报 `not installable`）。缺什么由 `cc-computer-use-doctor` 运行期探测。
- **9 个随包二进制只进 `/opt`，绝不落 `/usr/bin`**：目标机只要装过其中一个就会 `dpkg: error ... trying to overwrite` → **安装直接失败**。`/usr/bin` 下只放三个软链。
- **压缩必须显式 `-Zxz`**：宿主默认 zstd。（实测 22.04 的 dpkg 1.21.1 **能**读 zstd，但 xz 更保守、体积更小。）
- **用户级配置只能由用户跑**（`packaging/deb/cc-computer-use-setup`，**第一件事就是拒绝 root**）：`postinst` 以 root 运行，`gsettings` 写的是走会话总线的 dconf、`~/.claude.json` 是用户的文件 —— root 写进去只落在 `/root`，是「命令成功、完全无效」那类失败。触发点有三：`postinst` 用 `$SUDO_USER` 当场跑一次、`/etc/xdg/autostart/` 每次登录兜底、用户手动。autostart 那份**刻意不写进 `conffiles`**（conffile 在 remove 时被保留 → 卸载后还在跑脚本）。
- **`accessibility.conf` 自带一份**（`vendor/at-spi2/`，由 wrapper 经 **`CC_CU_AT_SPI_CONF`** 指过去）：系统那份 `/usr/share/defaults/at-spi2/accessibility.conf` **属 at-spi2-core**，写它就是文件冲突。`_resolve_at_spi_deps()` 里该变量优先级最高、系统候选降为兜底（变量指向不存在处会**告警回退**，不静默）。
- **`vendor_libs.py` 的基线白名单只留 glibc 工具链 + 压缩库**（2026-09-23 大幅收窄，**别再按「桌面必然有」往里加**）：实测在一个干净的 `ubuntu:22.04` 容器里跑验收，9 个随包二进制有 **44 个库解析不到** —— 整条 X11 + GLib + cairo/pango 栈都不在。即便放宽到「Ubuntu 桌面」，`libev.so.4` / `libstartup-notification` / `libxcb-icccm` / `libxcb-xrm` / `libXfont2`（i3 与 Xephyr 的私有依赖）也不是桌面默认就有的 —— 缺了沙箱就起不来。既然分发前提是「目标机不装任何东西」，白名单不能建立在「桌面必然有 X11」这个假设上。**多带库没有混装风险**：随包二进制靠 RPATH 定位、不经过 `LD_LIBRARY_PATH`，`vendor/lib` 永远进不了沙箱应用的环境。
- **容器构建的坑**：`binutils`（PyInstaller 靠 `objdump`）、`zip`（打 `.mcpb`）、`python3`（`assemble.sh` 跑 `vendor_libs.py`）、`patchelf` 四样都必须在第 1 步装上 —— 它们都在**后半程**才被用到，缺了会在前几步跑完之后才失败，重跑代价很大（`vendor_libs.py` 现在有 `_require_tools()` 前置检查）。容器到 github 会**间歇性**失败（实测卡到 `Failed to connect ... after 133027 ms`），故支持 `CC_CU_MINIFORGE_SH`（挂预置安装包，不碰网络）与 `CC_CU_MINIFORGE_URL`（换镜像；实测国内快 16 倍：github ~0.1MB/s vs 清华 ~1.6MB/s）。
- **⚠️ `ubuntu:*` 官方 Docker 镜像自带文档瘦身规则**（`/etc/dpkg/dpkg.cfg.d/excludes`：`path-exclude=/usr/share/doc/*`，只 `path-include` 回 `copyright` 与 `changelog.*`）。**真实 Ubuntu 安装没有这条**（本机查过）。在容器里验收时会表现为「`README.Debian` / `THIRD-PARTY-LICENSES.txt` 没装上」—— 与我们的包无关，`verify-in-container.sh` 里已先把它挪走。
- **合规**：deb 会重分发 **GPL-2+ 的 xclip / wmctrl**（还有 dbus-daemon 的双许可之一）等第三方二进制，必须随附 `THIRD-PARTY-LICENSES.txt`（`build-deb.sh` 已放进 `/usr/share/doc`）。

## mcp SDK 版本兼容

环境装的是 **mcp 2.2.0**（`python -c "import importlib.metadata as m; print(m.version('mcp'))"`；该包**没有** `mcp.__version__`，别用 `getattr(mcp,'__version__')` 去查，会得到假象）。2.x 把 `FastMCP` 改名为 `MCPServer`（`mcp.server.mcpserver`），`Image` 也在那里。`server.py` / `tools/screenshot.py` 做了 v1/v2 兼容导入（try 2.x 再 fallback 1.x；`Image` 都拿不到时退化为 base64 文本），升级或排查导入问题时注意。

## 测试坑

**测试分两类，别混用**：

| 文件 | 类别 | 说明 |
|------|------|------|
| `test_geometry.py`（9） / `test_refs.py`（8） / `test_serializer.py`（13） / `test_spawn_env.py`（2） / `test_i7_i8_i10.py`（11） / `test_atspi_guards.py`（24） / `test_d_guards.py`（15） / `test_e_guards.py`（22） / `test_f_guards.py`（14） / `test_g_guards.py`（21） / `test_h_guards.py`（8） / **`test_coordinator_hooks.py`（15）** / **`test_inject_layer.py`（18）** / **`test_coordinator_seq.py`（23）** / **`test_landing_evidence.py`（7）** / **`test_ocr_and_text.py`（12）** / **`test_display_lifecycle.py`（20）** / **`test_refs_and_values.py`（8）** / **`test_tools_and_backend.py`（24）** / **`test_ring_and_preview.py`（39）** / **`test_packaging.py`（21）** | pytest 单测 | 不需要桌面，`conftest.py` 会强制 `CC_CU_DISPLAY_MODE=real` 以免弹 Xephyr。`test_spawn_env.py` 是**结构性防漏**：枚举 `src/` 下每一处 subprocess spawn 点，要求 `env=` 源自 `env_for`/`app_env`；`test_i7_i8_i10.py` 是 review 报告那三条的回归集。**加粗的 8 个**由原 `test_optimizations.py`（2238 行）在 2026-09-18 按被测模块拆分而来，跨文件共享的辅助（`_StubBackend` / `_proc` / `_reset_display`）在 `_helpers.py`。**`test_ring_and_preview.py` 守住点击反馈的四条硬契约**：准星必须用「裁剪后原点」反算（屏边裁剪会改原点，写死半宽 = 越靠边指得越离谱）、准星只画在副本上（原始图要复用给 OCR 与点前后对比）、bytes 绝不进 `ActionResult.data`、`click` 工具返回注解必须是 `Any`（`str` 会让 mcp 生成 outputSchema，带图那次返回列表就 ValidationError）。**`conftest.py` 里那个 autouse 夹具 `isolate_at_spi_bus_env` 不能改成依赖 monkeypatch**：`AtspiReader` 会直接写**进程级** `os.environ`（libatspi 只能从那儿读），而 monkeypatch 的 `delenv` 对「本就不存在的键」**不入账**，记下的反而是用例自己写进去的假地址；autouse 夹具**最先实例化**、收尾回调**最后**执行，才排得进 monkeypatch 的 undo 之后 |
| `test_env_isolation.py`（1） | pytest 单测（**起子进程**） | 守「测试之间不留下进程级副作用」：自己起一个 pytest 子进程、挂探针插件在 `pytest_sessionfinish` 打印 `AT_SPI_BUS_ADDRESS` 的真实取值，断言为 `None`。**为什么必须起子进程**：判据是「跑完之后环境是否干净」，而那只在**那个会话收尾时**才观测得到——在会话内断言，观测到的是本会话自己的状态，何况 `conftest.py` 的守卫夹具已把本会话擦干净了（等于在测那把本来就该生效的扫帚） |
| `test_e2e_zenity.py`（6） | pytest 端到端 | 需 X11 + zenity + Xephyr，需 `CC_CU_E2E=1` opt-in，默认在沙箱内跑。**「等应用上树」一律用 `_wait_app_on_tree()` 轮询，别改回固定 `sleep`**：实测沙箱内 zenity 上树耗时 1.60~2.54 秒，**正好跨过**历史上的 2.5 秒魔数，于是同一份代码会在「全绿」与「全红」之间随机翻转（实现在 2026-09-17 的 I-17 修的）。**收尾同理，用 `_wait_window_gone(title)` 轮询等窗口真的从 X 上消失，不要 `terminate()` 完就走人**：SIGTERM 只是请求进程退出，X 端窗口要等客户端断开才被回收——下一个用例开头读到的「活动窗口」于是还是上一个用例**正在死**的对话框，而它会在随后几秒里消失。实测就是这样把 `test_ring_visible_has_hole_and_autoclears` 的「画圈不改变活动窗口」打成偶发红（`assert 'E2EPreview' == None`，3 轮里红 2 轮）；根因不在光圈，在收尾提前离场 |
| `manual_*.py`（6 个） | **手动 story**（不进 pytest） | 用 MCP stdio 客户端驱动**真实 server/冻结产物**，跑完整任务链路，退出码 0 = 全过 |

手动 story 清单（改相关模块后应各跑一遍）：
- `manual_sandbox_story.py` —— 沙箱隔离：launch_app 起 zenity → 读树 → 元素级点「是」，并断言**宿主活动窗口/指针前后不变**。⚠️ 运行期间**不要动鼠标键盘**（宿主指针零移动是断言前提）；real 模式下脚本直接拒绝运行。加 `CC_CU_STORY_DIST=1` 改为驱动冻结产物。
- `manual_ocr_story.py` —— 灰区感知全链路：OCR 出文字与坐标 → 按 ref 点击 → 对话框真的关闭。全程不看截图。**需先 `bash build.sh`**（它打的是冻结产物）。
- `manual_a11y_isolation.py` —— a11y 私有总线隔离，内含**安全闸**（先断言 reader 绑在私有总线且看不到宿主应用，才继续遍历）。
- `manual_gedit_story.py` —— 真实桌面 gedit 写文件并保存（下述教训的来源）。
- `manual_dbeaver_a11y_probe.py` —— DBeaver 树探针。
- `manual_find_prefilter_probe.py` —— 钉住「find 的无窗口应用预滤」那条**单测测不到的跨通道前提**：X11 的 `_NET_WM_PID` 与 AT-SPI 的 `get_process_id` 必须指向同一进程，否则预滤会把正在用的应用整个跳过。沙箱内起 zenity（按钮标签写死，判据与语言无关）→ 不带 app= 的全桌面 find 必须搜到 → 两侧 pid 对得上 → 元素级点掉。沿用 a11y 隔离那套**安全闸**（先确认绑在私有总线且看不到宿主应用，才遍历）；real 模式拒绝运行。

端到端测试用 `pkill -9 -x zenity`（**精确匹配进程名**），不能用 `-f`：`-f` 匹配完整命令行，会命中 `pytest tests/test_e2e_zenity.py` 自身把测试进程杀掉（表现为 exit 1 且无输出）。

桌面真实任务端到端见 `tests/manual_gedit_story.py`（MCP stdio 客户端驱动冻结产物，完成 gedit 写故事+保存），其中固化了这些实测教训：
- `xdotool search --name gedit` 会命中 GTK 的 10x10 辅助窗（名字也叫 gedit），文档窗口不一定排第一——必须**取面积最大**的窗口（与 `window_screen_pos_by_pid` 同策略）。
- **每次键盘注入前校验 `getactivewindow`**：真实桌面上焦点随时会被其他应用抢走，注入打偏会把文本打进用户其他应用（实测打进了飞书/Remmina）。
- windowactivate 重试要**低频**（≥1 秒/轮）且不 spam Escape——高频激活请求风暴会把 mutter 打到无响应。
- 对话框确认优先**元素级 click 保存按钮**（零坐标、免焦点），比「激活对话框+Return」更稳。
