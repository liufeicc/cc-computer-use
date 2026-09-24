"""
Windows 驱动层（backend.windows.driver）。

分工（每个文件只干一件事，便于 Linux 上单测与将来替换）：
    win32.py         Win32 的 ctypes 绑定（**惰性**——Linux 上 import 本模块不得报错）
    session_info.py  会话/窗口站/DPI 自检——「这份数据可不可信」的前置闸
    hwnd.py          取点下**最深**的子 HWND（定向注入的唯一前提）
    _uia.py          uiautomation 的**唯一**入口（含 DPI 前置与能力自检）
    probe.py         尖刀入口：三个测量 + 自检 + JSON 输出

⚠️ 本包**零 subprocess**：所有平台调用走 ctypes / COM，不 spawn 任何外部命令。
这条既是为了性能，也是为了不被 `tests/test_spawn_env.py` 的 AST 扫描判成"未接 env 管道"
——那条守卫要求每个 spawn 点的 `env=` 源自 `env_for`/`app_env`，而驱动层**不该有**
spawn 点。有守卫盯着这一点（tests/test_windows_driver_guards.py）。
"""
