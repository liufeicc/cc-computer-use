@echo off
rem ============================================================================
rem 安装包验收的入口（由计划任务 /IT 以**交互式会话**身份拉起）。
rem
rem 为什么必须有这一层 .cmd，而不让计划任务直接调 PowerShell：
rem   `/tr "<命令行>"` 里再嵌一层带引号的路径，引号会被 cmd 吃掉 ——
rem   实测报 `无效参数/选项 - '-NoProfile'`，任务根本没建起来。
rem   隔一层 .cmd 之后，`/tr` 里只有一个**不含空格**的路径，嵌套引号问题消失。
rem
rem 参数经**文件**传递（写在这里，路径在包装脚本里改）：
rem   Z:\cc-computer-use\dist\windows\verify-params.txt
rem   → 内容形如：Setup=...\xx-setup.exe  /  Result=...\verify-result.txt
rem
rem ⚠️ 本文件必须**纯 ASCII**：cmd.exe 对 BOM 与 UTF-8 都不友好，
rem    带 BOM 的 .cmd 会把第一行命令打乱（守卫测试盯着这一条）。
rem
rem ⚠️ 输出**必须落文件**：计划任务拉起的窗口是隐藏的，PowerShell 一旦解析失败
rem    （实测：脚本里用了 PS 7 才有的 `??` 运算符，而客体上是自带的 PS 5.1），
rem    报错就消失在虚空里 —— 表现只是"结果文件没出现"，完全看不出原因。
rem ============================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "Z:\cc-computer-use\packaging\windows\verify-on-windows.ps1" -ParamsFile "Z:\cc-computer-use\dist\windows\verify-params.txt" > "Z:\cc-computer-use\dist\windows\verify-interactive.log" 2>&1
