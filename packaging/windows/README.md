# Windows 打包与分发（`packaging/windows/`）

Windows 后端的**分发形态**：在客体上打 PyInstaller 冻结产物 → 用 Inno Setup 打成
一个安装文件 → 装进用户目录 → 注册进客体上的 Claude Code。

这条链路与 Linux 侧的 `.deb` 是**同一套纪律、不同的平台形态**：

| | Linux（`.deb`） | Windows |
|---|---|---|
| 冻结 | `build.sh` → onedir + wrapper | `build-on-windows.ps1` → onedir |
| 组装 | `assemble.sh` + `vendor/`（9 个外部命令） | 无 —— **Windows 后端零 subprocess**，不需要随包任何外部命令 |
| 打包 | `dpkg-deb` | `ISCC`（Inno Setup） |
| 装到哪 | `/opt/cc-computer-use`（root） | `%LOCALAPPDATA%\Programs\cc-computer-use`（**当前用户，免管理员**） |
| 注册 | `cc-computer-use-setup`（bash） | `cc-computer-use-setup.ps1` |
| 验收 | 干净容器 10 步 | 计划任务 `/IT` 里跑 `verify-on-windows.ps1` |

---

## 怎么用

```bash
# 一次构建（宿主）：备客体工具链 → 客体冻结 → 打安装包 → 产物落回 dist/windows/
CC_CU_VM_PASS='<密码>' bash packaging/windows/build-windows.sh

# 安装 + 注册 + 卸载的端到端验收
CC_CU_VM_PASS='<密码>' bash packaging/windows/verify-install.sh
```

环境变量（都有默认值）：`CC_CU_VM`（默认 `Windows11`）、`CC_CU_VM_USER`（`lf`）、
`CC_CU_VM_PASS`（**必填，刻意不从任何文件读**）、`CC_CU_VM_PY`
（默认 `C:\Users\lf\py312\python.exe`）。

一键跑通之后可以直接在客体上用：装完就已经注册进 Claude Code 了。

---

## 十条踩过的坑（都写进了代码与守卫测试）

### 1. 纯 Python 的包不在磁盘上，在内嵌的 `PYZ.pyz` 里

PyInstaller 把**纯 .py 的包**（`mss` / `mcp` / `comtypes`）打包进 exe 内嵌的归档，
磁盘上**没有同名目录**（只留一个 `<包名>-<版本>.dist-info\`）；只有带二进制/数据文件的包
（`uiautomation` 的 proxy DLL、`winrt` 的 `_winrt*.pyd`）才会被摊到 `_internal\<包名>\` 下。

第一版产物校验只查了「目录在不在」，于是对前者**恒判缺失**、误杀了整轮构建 ——
而真实产物一个不少。现在改成两条都用权威来源：`pyi-archive_viewer -r -b` 读归档 +
`Test-Path` 查磁盘散件。守卫 `test_windows_build_verifies_the_python_archive_not_just_directories`
钉住这一点。

### 2. 三个静态分析看不见的运行时依赖

漏任何一个的表现都是「开发态一切正常、只有冻结产物里坏掉」：

- `--collect-all uiautomation` —— 它按 `__file__` 的兄弟目录找 `bin/` 下的
  proxy/stub DLL，那两个文件不是 `.py`，`--collect-submodules` 拿不到；
- `--collect-submodules comtypes` —— `uiautomation` 调
  `comtypes.client.GetModule("UIAutomationCore.dll")`，那是**运行时生成并 import 一个模块**；
- `--collect-all winrt` —— PyWinRT 的投影包各自往 `winrt/windows/…` 下放子模块与 `_winrt` 扩展。

### 3. 客体脚本的编码：`.ps1`/`.iss` 要 BOM，`.cmd` 要纯 ASCII

PowerShell 5.1 读**无 BOM** 的 `.ps1` 时按系统 ANSI 代码页解（中文机 = GBK），
中文注释与字符串变乱码，**报错位置还会指向被解错的那一行**；Inno 编译器读 `.iss` 同理
（中文会以乱码出现在安装向导里）。`.cmd` 反过来 —— cmd 对 BOM 与 UTF-8 都不友好。

诊断信息一律走 **UTF-8 日志文件**（`dist/windows/build.log`），stdout 上只留 ASCII 进度：
客体是中文 Windows，中文经 `VBoxManage guestcontrol` 的管道回来看必是乱码。

### 4. `claude` 要求机器上有 Git for Windows 或 PowerShell 7（**不是**上下文问题）

```
Claude Code on Windows requires either Git for Windows (for bash) or PowerShell.
```

⚠️ **这条曾经被我们归因错了，2026-09-29 真机隔离实测推翻重写**：

- 旧结论（错）：「这是 guestcontrol 的非交互上下文导致的，同一个命令在交互式会话里正常，
  照字面去装 Git 会白折腾一轮。」—— 于是注册与验收都绕道计划任务 `/IT`，
  还特意在 setup 里写了一段「别去装 Git」的提示。
- 真相：那台客体上**确实既没有 Git for Windows 也没有 PowerShell 7**，而这就是
  Claude Code 的硬要求。系统自带的 **Windows PowerShell 5.1 不算**（报错要的是 PowerShell 7）。

怎么证伪的：把两个变量**逐个隔离**试。
① 只把 PortableGit 的 `bin` 加进子进程 PATH → 报错变成
`Error: Input must be provided either through stdin or as a prompt argument when using --print`
（说明 shell 那道闸**已经过了**，后面的报错是另一个原因 —— 见坑 8）；
② 只设 `CLAUDE_CODE_GIT_BASH_PATH` 指向 `bash.exe` → `claude --version` 返回
`2.1.179 (Claude Code)`，`mcp add / get / remove / list` **全部恢复**（跑过完整 CRUD：
add 返回「Added stdio MCP server …」、get 能读回、remove 能摘掉、get-after-remove 正常失败）。

所以现在的做法（三份脚本一致）：

- `Find-GitBash`：候选表找 `bash.exe`（`%LOCALAPPDATA%\Programs\Git`、
  `%ProgramFiles%\Git`、`%ProgramFiles(x86)%\Git`，外加便携版常见位置），
  也可用 `CC_CU_GIT_BASH_PATH` 显式覆盖；
- 起 claude 的子进程时设 `CLAUDE_CODE_GIT_BASH_PATH` —— 这正是 Claude Code 报错里
  点名的那句话（「Or set CLAUDE_CODE_GIT_BASH_PATH to your bash.exe location」）。
  **用进程级环境变量而不是改 PATH**：改 PATH 要等新进程才生效（计划任务拿到的仍是旧环境），
  而进程级变量当场生效、也不污染用户的机器环境；
- 找不到就**明说缺什么、去哪装**（`https://git-scm.com/downloads/win`），不再叫用户别装。

> 验证机上 Git 是怎么来的：官方安装器（`Git-2.56.0-64-bit.exe /CURRENTUSER`）在
> `guestcontrol` 的过滤令牌下**静默什么都没做**（rc=0、不写自己的日志）；改用
> **PortableGit 2.56.0**（同样是 Git for Windows 的官方产物，`/VERYSILENT` 自解包）
> 解到 `%LOCALAPPDATA%\Programs\Git` 即可 —— 免管理员，正好也是 per-user 安装器的默认位置。

### 5. Inno Setup 的安装器请求管理员权限，`guestcontrol` 起不来

`guestcontrol` 拉起的进程是 UAC 过滤令牌（Medium 完整性），跑 Inno 的安装器会在提权那步
**直接退出码 2、连自己的日志都不写**。用 `/CURRENTUSER` 让它以当前用户身份装到
`%LOCALAPPDATA%\Programs` —— 这恰好也是本包自己安装时走的形态。

### 6. `[Run]` 不能带 `postinstall` 标志

带该标志的条目在**静默安装**（`/VERYSILENT`）下会被 Inno **跳过**，而静默安装正是自动验收
走的路径 —— 用了它就等于验收从没测过安装器自己的注册动作。

### 7. 客体 PS 5.1 解析不了的语法：`(try { … } catch { … })`

把 `try` 当**表达式**用（`$x = (try { … } catch { '' })`）是 **PowerShell 7 才有的写法**。
5.1 **连解析都过不了** —— 而且不是"跳过那一行"，是**整份脚本一个字都不执行**，
报错消失在隐藏窗口里，表现只是「体检跑不起来」。2026-09-29 实测踩到。

⚠️ 赋值形式 `$x = try { … } catch { … }`（不带外面那层括号）5.1 **认**。
区别细微、代价极大，所以有专门的守卫
（`test_guest_scripts_do_not_use_try_as_an_expression`，判据是「`(` 后面紧跟 `try`」这个具体形状，
不是笼统禁 try）。

**验收里现在会真解析一遍**：`verify-on-windows.ps1` 用
`[Parser]::ParseFile()` 把随包的 `.ps1` 在**客体这台机器**上解析一遍，结果变成一条
`scripts-parse-on-ps51` 判据。静态守卫只能拦已知写法，这条才是对目标机的确定回答。

### 8. 别把函数参数命名成 `$Args`（PowerShell 自动变量）

`param([string[]]$Args)` —— `$Args` 是 **PowerShell 的自动变量**，参数会被**静默忽略**，
子进程收到的命令行是空的。表现是 `claude` 报
`Error: Input must be provided either through stdin or as a prompt argument when using --print`，
看起来像 claude 坏了，排查方向完全跑偏（2026-09-29 为此白绕了一圈）。
同类不能用的名字还有 `$Input` / `$Error` / `$Host` / `$MyInvocation` / `$PSCommandPath` 等，
守卫 `test_guest_scripts_do_not_assign_to_powershell_automatic_variables` 钉住。

> 顺带解释坑 4 里那句 `Input must be provided …`：它不是 Git 相关，也不必去猜 claude 的
> 内部逻辑 —— 那次是本探针自己的参数没传进去。**看到它先查调用方**。

### 9. 客体构建偶发 `SubprocessDiedError`（重跑即可）

PyInstaller 的 `collect_submodules('anyio._backends')` 会另起一个隔离子进程；
2026-09-29 实测它偶发以 `exit code 3221225477`（`0xC0000005` 访问违例）死掉，
整个构建在最后一步失败。**同一条命令原样重跑就过了**（前后两次构建产物一致）。
不是我们代码的问题，但一轮要 9 分钟，先知道能省时间。

### 10. `claude mcp get/list` 会冷启动我们的 server —— 超时给足，别当它是纯读配置

那句 `Status: √ Connected` **不是读配置文件读出来的**：claude 会真的把 MCP server 拉起来
做一次健康检查。而我们的 server 是个 250MB 的冻结产物，**首次冷启动**（文件缓存冷 +
UIA 初始化）实测会超过 30 秒。

2026-09-29 真机踩到：一次验收里 `mcp-get` **打印了完整正确**的输出（连同 `√ Connected`），
进程却还没退出，于是 30 秒的超时把它判成失败 —— **假红**，方向最坏的那种
（会让人去改本来正确的产物）。

实测：热态 6~12 秒（`mcp get` 6.3/6.9/8.7s，`mcp list` 11.9s），冷态可能 30 秒以上。
所以 `mcp get / list / add` 这类子命令一律给 **120 秒**；守卫
`test_claude_health_check_subcommands_get_a_generous_timeout` 钉住这条。

⚠️ 顺带一个**方向相反的**坑：`claude doctor` 是个**交互式 TUI**，它**永远不会自己退出**
（实测跑到 300 秒超时）。**别在任何自动化里调它。**

---

## 守卫与反证

```bash
PYTHONNOUSERSITE=1 $PY -m pytest tests/test_packaging_windows.py -q   # 45 条结构性守卫
PYTHONNOUSERSITE=1 $PY tests/manual_windows_guards_reverse.py         # 58 条反证
```

反证那步不是可选的：这些守卫的判据都是「没有坏东西」，而"守卫坏了"与"代码没问题"
在结果上长得一模一样。本仓库有血证 —— 拆 mixin 那次两条守卫静默变成空集合、全绿但零覆盖。
反证脚本本身也会过期（生产代码一改，「替换目标串」就对不上了），所以它**对不上时硬失败**，
而不是静默跳过。

⚠️ 反证现在有**三种手法**（58 条 = packaging 源码替换 36 + **`src/` 源码替换 4** +
运行期破坏 18）。中间那 4 条是 2026-09-30 为「默认不碰用户键鼠」那批**顺序判据**新加的：
那批判据读的是 `src/` 下的生产代码（"闸门必须早于注入原语"），而原来的替换机制只拷
`packaging/`，够不着。挂载点是 `test_global_input_gate.GG._TEST_SRC` —— 那几条判据
取源码路径时**必须**经 `_src_root()`（直接用模块级 `_SRC` 的话反证会永远跑在真实源码上，
于是又变回"坏了也看不出来"）。

---

## 验收状态（2026-09-29）

`verify-install.sh` 从**干净前置状态**（安装前无注册项）跑完 **15 项全过、`RESULT|ok`**：

```
CHECK|scripts-parse-on-ps51|OK|解析失败的随包脚本数 = 0
CHECK|claude-shell|OK|C:\Users\lf\AppData\Local\Programs\Git\bin\bash.exe
CHECK|pre-state|OK|安装前已有注册项 = False
CHECK|mcp-get|OK|rc=0 … Status: √ Connected
CHECK|mcp-list|OK|cc-computer-use: …\server\computer-use-mcp-bin.exe  - √ Connected
CHECK|mcp-unregistered|OK|claude mcp get 应当失败
```

即「装完 → 安装器自己完成注册 → `claude mcp list` 显示 Connected → 卸载后注册项被摘掉」
这条链路闭环。

⚠️ 两条**判据本身**在这里修正过，值得记住它们之前是怎么"假绿"的：

- `mcp-unregistered`：装上 Git **之前**，`claude mcp get` 因为 shell 缺失而一律返回非 0 ——
  看起来"注册项确实被摘掉了"，其实什么都没验到。修好 shell 之后它才是一条真判据。
- `mcp-get`：超时从 30s 改成 120s 之前，一次真·通过的健康检查会被判成失败（见坑 10）。

这两条是同一种病的两面：**判据的通过与否，不能取决于被测对象坏没坏。**

---

## 客体环境（换机器要重来一遍）

- **Python 3.12.10**：`C:\Users\lf\py312\python.exe`
  （装法见 `docs/PR/windows-driver/install_python312.py`；⚠️ 3.12.11 起只发源码包，
  最后一个带 Windows 安装器的是 3.12.10）
- 依赖：`mcp cryptography uiautomation pillow mss pyinstaller` + 全套 `winrt-*`
  （命令见本仓库的运维笔记）
- **Inno Setup 6.7.3**：经 `ghfast.top` 代理下载后 `/CURRENTUSER` 静默装到
  `%LOCALAPPDATA%\Programs\Inno Setup 6`（`build-windows.sh` 会自动做，宿主侧只负责下载）
- **Git for Windows**（**注册环节的硬前提**，见坑 4）：`%LOCALAPPDATA%\Programs\Git`。
  本机用的是 PortableGit 2.56.0（官方产物，免管理员的 .7z 自解包）；
  也可用 `CC_CU_GIT_BASH_PATH` 指到别处。装法：
  客体上直接 `curl -L -o git.7z.exe <清华镜像>/PortableGit-<版本>-64-bit.7z.exe`
  再 `git.7z.exe -oC:\Users\lf\git -y` —— 客体直连清华镜像约 1 分钟，
  走宿主 `ghfast.top` 代理反而慢且会截断。
- 客体必须在跑、且**用户已登录交互式会话**（UIA 与抓屏都要求真桌面）。
  ⚠️ 无界面（headless）启动的虚拟机默认停在登录界面 —— 必须真的登录一次
  （`VBoxManage controlvm <vm> keyboardputstring` 之类往登录框打字**不可靠**，
  虚拟机窗口里手动登录一次最省事）。判据：`query user` 里有 `console` 会话且
  `tasklist` 能看到 `explorer.exe`。

## 仍未验证

- **多显示器 / 负原点**：本机 VM 是单屏，要加屏得动 VirtualBox 的显示器配置，未做。
- **提权（UIPI）应用**：起提权进程必然弹 UAC，无人值守跑不了。
- 这两条在 `docs/PR/windows-driver/README.md` 里也如实记为未验证，**不做任何"应该没问题"的推断**。
