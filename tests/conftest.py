"""pytest 共享夹具。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 单测**默认不启隔离沙箱**：MCP 改为惰性启动后（首次工具调用经 coordinator._needs_display
# → display.ensure_started），而单测大量直接构造 Coordinator 并调用其对外方法，若 MANAGER
# 仍是默认的 isolated，跑一次 pytest 就会弹出一个 Xephyr 虚拟屏。
#
# 用 setdefault 而非直接赋值：显式传入的同名变量优先，于是
#   - CC_CU_E2E=1 …            → 整个 if 跳过，e2e 仍按 CLAUDE.md 约定在沙箱内跑
#                                （这条不能破：e2e 会起窗口 + 元素级点击，落到真实桌面很危险）
#   - CC_CU_DISPLAY_MODE=isolated … → 单测想验沙箱行为时同样生效
# conftest 由 pytest 在**收集测试模块之前**导入，而 MANAGER 单例在首次 import
# computer_use_mcp.core.display 时才构造，故这里改 env 一定赶在其前面。
_E2E = os.environ.get("CC_CU_E2E") == "1"
if not _E2E:
    os.environ.setdefault("CC_CU_DISPLAY_MODE", "real")

# 确保能 import src 下的包（即使未 editable 安装）
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def isolate_at_spi_bus_env():
    """
    每个用例前后**快照/还原**进程级 AT_SPI_BUS_ADDRESS（I-15）。

    为什么需要这层：`AtspiReader` 连私有总线时**只能**把地址写进进程级 os.environ
    （libatspi 的 atspi_init 在初始化时读它，没有别处可传 —— 见 backend/linux/atspi.py），
    那是**测试记账之外**的写入。而 monkeypatch 对此**接不住**：`delenv` 在「键本就不存在」
    时根本不入账（pytest 的 `delitem` 对不存在的键直接返回），于是用例结束时被记下的
    反而是用例自己写进去的假地址，teardown 时把它**原样还原回环境**。
    实测取证：跑完 test_reader_sync_bus_follows_sandbox 后，下一个用例读到
    `AT_SPI_BUS_ADDRESS='unix:path=/tmp/priv-1'`；而它恰好是那个文件里**最后一个**用例，
    后面没有用例能观测到 —— 泄漏一直存在却无人发现（换顺序 / 拆文件即现偶发失败）。

    为什么只对单测生效（e2e 跳过）：e2e 里沙箱与私有总线是**真的**，进程级地址必须与
    已建立的 libatspi 连接保持一致 —— 在用例之间还原它，万一下个用例才走到 atspi_init，
    就会带着空地址连上**宿主**总线，正是本项目最警惕的静默失效。单测走不到那一步
    （单测强制 real 模式，`sync_bus` 在 real 下不写环境变量）。

    为什么 autouse 就能盖住 monkeypatch：同一作用域内 autouse 夹具**最先实例化**，
    故它的收尾回调最后执行 —— 晚于 monkeypatch 的 undo。反过来（本夹具依赖 monkeypatch）
    就会先于 undo 执行，等于没修。
    """
    if _E2E:
        yield
        return
    orig = os.environ.get("AT_SPI_BUS_ADDRESS")
    yield
    if orig is None:
        os.environ.pop("AT_SPI_BUS_ADDRESS", None)
    else:
        os.environ["AT_SPI_BUS_ADDRESS"] = orig
