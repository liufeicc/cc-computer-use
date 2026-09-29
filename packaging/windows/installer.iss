; ============================================================================
; cc-computer-use —— Inno Setup 安装脚本（Windows 分发形态）
;
; 用法（由 packaging/windows/build-on-windows.ps1 调用，也可手工跑）：
;
;   ISCC.exe /DAppVersion=0.1.0 /DPayloadDir=<payload 绝对路径> /DOutDir=<产物目录> installer.iss
;
; ═══ 为什么用 Inno 的预处理器（`#define` + `/D`）而不是外层 sed 模板 ═══
;
; Linux 侧的 control/launcher 是用 `sed 's|@KEY@|值|'` 渲染的（见 build-deb.sh 的 render()），
; 那是**没有别的办法**——dpkg 不认识模板。Inno 自带预处理器（ISPP 语法已内置），
; `/D` 传进来的宏在编译期就展开，所以这里不需要 `.iss.in` 这一层间接：
; 文件本身就能直接编译（给宏兜底默认值），人也能手工跑一遍验证。
;
; ═══ 安装形态：**仅当前用户**，不需要管理员 ═══
;
;   · PrivilegesRequired=lowest + DefaultDirName={localappdata}\Programs\...
;     本 MCP server 必须跑在**用户自己的交互式会话**里才看得到桌面；per-machine
;     装法不但不带来好处，还会在自动化里卡 UAC（没人点那个弹窗）。
;   · 这与 Linux 侧「安装到 /opt」看着不同，但理由是同一条的另一面：Linux 那边
;     .deb 由 root 装、但**用户级配置（无障碍开关、~/.claude.json）坚决只由用户跑**
;     （见 cc-computer-use-setup.in 开头那段）。Windows 这里干脆整包装进用户目录，
;     从构造上不可能写错账户。
;
; ═══ 编码 ═══
; 本文件是 **UTF-8 带 BOM**。Inno 的编译器在没有 BOM 时按系统 ANSI 代码页（中文机 = GBK）
; 读 .iss —— 那样下面的中文 `Description` 会以乱码出现在安装向导里。
; ═===========================================================================

#define AppName "cc-computer-use"
#define AppPublisher "liufei"
#define AppUrl "https://github.com/liufeicc/cc-computer-use"

; 三个宏都可以由命令行 /D 覆盖；这里的默认值是为了「人能直接手工编译」也能跑通
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "..\..\dist\windows\payload"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist\windows"
#endif

[Setup]
; AppId 是**安装标识**：同一个 AppId 的新版本会被认成"升级"而不是并存安装。
; 一旦发布就不要再改它（改了等于换了一个软件，老版本不会被替换掉）。
AppId={{7E3A9C21-5B4D-4F8A-9C16-2D6E0A4B7F31}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
DefaultDirName={localappdata}\Programs\cc-computer-use
DefaultGroupName=cc-computer-use
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=cc-computer-use-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 冻结产物是 64 位的（客体 Python 是 win_amd64），装到 x64 机器上
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\server\computer-use-mcp-bin.exe
LicenseFile={#PayloadDir}\LICENSE
; 安装前给用户看一眼「这东西是干什么的、装完还要做什么」，比装完再解释好
InfoBeforeFile={#PayloadDir}\README.txt
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} MCP server for Windows
VersionInfoProductName={#AppName}

[Files]
; ⚠️ 布局要点：`server\*` 铺开复制 —— PyInstaller 的 onedir 是
;    「exe 与 _internal\ 平级」，套一层会变成 server\computer-use-mcp-bin\computer-use-mcp-bin.exe，
;    那是 Linux 侧 assemble.sh 专门写过注释提醒过的同一个坑。
;    `recursesubdirs createallsubdirs` 两个都要：_internal\ 底下还有子目录。
Source: "{#PayloadDir}\server\*"; DestDir: "{app}\server"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\bin\cc-computer-use-setup.ps1"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "{#PayloadDir}\bin\cc-computer-use-doctor.ps1"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "{#PayloadDir}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\THIRD-PARTY-LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; {group} 在 DisableProgramGroupPage=yes 下就是 {userprograms}\cc-computer-use
Name: "{group}\体检（doctor）"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\cc-computer-use-doctor.ps1"""; WorkingDir: "{app}"; Comment: "检查本机能否使用，以及是否已注册到 Claude Code"
Name: "{group}\注册到 Claude Code"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\cc-computer-use-setup.ps1"""; WorkingDir: "{app}"; Comment: "把本 server 注册进 Claude Code（user scope）"
Name: "{group}\README"; Filename: "{app}\README.txt"
Name: "{group}\卸载 cc-computer-use"; Filename: "{uninstallexe}"

[Run]
; ⚠️ 刻意**不用** `postinstall` 标志：带该标志的条目在静默安装（/SILENT、/VERYSILENT）
;    下会被 Inno **跳过**。而静默安装正是自动化验收走的路径 —— 用了它就等于
;    「验收从没测过安装器自己的注册动作」，正是 Linux 侧
;    test_verify_simulates_the_real_install_path 那条教训（只测组件、不测装配）。
;    普通 [Run] 条目两种模式下都会执行。
;    脚本自身对「没装 Claude Code」返回 0 并给出提示，所以这里不需要 ignoreerrors，
;    真失败时用户能看到退出码。
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\cc-computer-use-setup.ps1"""; Description: "把本 server 注册到 Claude Code（user scope）"; Flags: runhidden waituntilterminated

[UninstallRun]
; 卸载时把注册项一并摘掉 —— 否则用户卸了软件，Claude Code 里还留着一条指向
; 已不存在文件的 MCP 配置，每次开会话都报一次连接失败。
; [UninstallRun] 在删文件**之前**执行，所以此时脚本还在。
; `RunOnceId` 必须给（不给 Inno 会编译期告警）：它是这条条目的唯一标识，
; 保证重复/并行卸载时只跑一次。
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\bin\cc-computer-use-setup.ps1"" -Unregister"; Flags: runhidden waituntilterminated; RunOnceId: "CcCuUnregister"

[UninstallDelete]
; ⚠️ 必须显式删 `{app}` 整棵树：Inno 的卸载器只删**它自己装下去的**文件，
;    而运行期产生的东西（`setup.log` / `doctor` 的临时产物）它不认识、会留下。
;    结果是「卸载完目录还在、里面躺着一个日志文件」—— 2026-09-29 真机验收抓到
;    （判据 `app-dir-removed` 报 FAIL，而安装/卸载本身都"成功"）。
Type: filesandordirs; Name: "{app}"

[Messages]
; 默认文案是英文（Inno 6 不自带简体中文语言包）。改这一条是为了把
; 「装完还要重启 Claude Code 才看得到」说在用户关掉向导之前。
WelcomeLabel2=This will install [name/ver] on your computer.%n%nIt installs into your own user profile (no administrator rights needed) and registers itself with Claude Code if that is installed.%n%nThe MCP server drives your Windows desktop, so it runs in your own interactive session.