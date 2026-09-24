"""
Windows backend（backend.windows）。

═══ 本轮（2026-09-24）只有驱动层，没有完整 backend ═══

这里目前只有 `driver/` —— 一个能**独立跑**的 Windows 驱动，用途是在真机上验证
「Python + uiautomation 这条路到底行不行」。真机 DEMO 当初是用 PowerShell + .NET 验证的
概念，而**真正要发货的是 Python**：COM 初始化、引用计数、DPI 感知的设置时机，
这些在 Linux 单测里永远暴露不出来，而在真机上是一个脚本就能回答的。

完整的 `WindowsBackend`（23 个抽象方法 + 会话 provider + 隔离策略）在后续轮次。

⚠️ 本包的模块在 **Linux 上也必须能 import**（CI 跑在 Linux，而驱动层的守卫测试要 import
它们）：所有平台相关的绑定一律**延迟到函数内**（`ctypes.WinDLL`、`import uiautomation`），
模块顶层只放纯 Python 的结构定义。
"""
