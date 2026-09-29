cc-computer-use —— 让 Claude Code 直接操作 Windows 桌面
============================================================

这是什么
--------
一个 MCP server。装上并注册之后，Claude Code 就能"看见"并操作你的 Windows 桌面：
读窗口里的元素树（UIA）、按名字找到按钮、点它、往里输入文字、抓屏、识别屏幕上的文字。

它的做法与"截图 + 猜坐标点击"不同：优先用 Windows 无障碍接口（UIA）拿到**结构化的
元素**，点击走元素级动作（零坐标），因此不受分辨率、缩放、窗口位置影响。
只有遇到没有元素树的应用（自绘控件、游戏、远程桌面）才退回到 OCR + 坐标点击。


装到哪儿了
----------
    安装目录\server\computer-use-mcp-bin.exe    MCP server 本体（自包含，不需要装 Python）
    安装目录\bin\cc-computer-use-setup.ps1      注册 / 注销 Claude Code 集成
    安装目录\bin\cc-computer-use-doctor.ps1     体检：检查本机能不能用

安装是**仅当前用户**的，没有动系统目录、没有写注册表里需要管理员的项。
卸载走"设置 → 应用"，或用开始菜单里的"卸载 cc-computer-use"。


装完之后
--------
1. 安装器已经自动尝试把它注册进 Claude Code（user scope）。
   如果当时没装 Claude Code，之后装好了就再跑一次开始菜单里的"注册到 Claude Code"。

2. **重启 Claude Code 会话**（或执行 /mcp）才能看到新的 MCP server。

3. 不确定能不能用？跑一次开始菜单里的"体检（doctor）"，它会把
   随包文件、自检、Claude Code 注册状态逐项验一遍。


需要知道的四条前提
------------------
· **必须有已登录的交互式桌面会话。** 这个 server 是替你操作桌面的，它读的元素树、
  抓的画面都来自你当前登录的那个会话。通过服务 / SSH / 计划任务（非交互）拉起来的
  进程落在没有桌面的会话里，读到的永远是空的 —— 体检脚本会把这种情况标出来。

· **必须有 Git for Windows（或 PowerShell 7）。** 这不是本程序的要求，是 Claude Code
  自己在 Windows 上的硬前提：两样都没有时，`claude` 命令连 `--version` 都拒绝执行
  （系统自带的 Windows PowerShell 5.1 不算，Claude Code 要的是 PowerShell 7）。
  缺了它，安装器里的"注册到 Claude Code"这一步会失败。
  装上 Git for Windows（https://git-scm.com/downloads/win）后，重跑一次开始菜单里的
  "注册到 Claude Code"即可。

· **它操作的是你的真实桌面。** 与 Linux 版本不同，Windows 版本**没有沙箱隔离**
  （实测：隐藏桌面上 SendInput 与抓屏都不可用，见项目文档）。
  也就是说：agent 点击、输入都会真的落在你正在用的桌面上。
  跑长任务时尽量不要同时用鼠标键盘。

· **用它自己的体检脚本判断"能不能用"。** 不要只看安装器有没有报错 ——
  开始菜单里的"体检（doctor）"会把随包文件、自检、shell 前提、Claude Code 注册状态
  逐项验一遍，并给出机器可读的结论。


卸载时会发生什么
----------------
卸载器会先把它从 Claude Code 里摘掉（user scope），再删文件。
正常情况下不会留下指向已删除文件的 MCP 配置。


许可
----
本程序以 MIT 许可发布。随包分发的第三方组件（CPython 运行时、MCP SDK、PyWinRT 等）
及其许可见安装目录下的 THIRD-PARTY-LICENSES.txt。