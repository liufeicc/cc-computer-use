"""
MCP server 入口（FastMCP + stdio）。

启动顺序（关键）：
  1. 最先 import _bootstrap 并调用 setup_gi_environment()，确保 GI_TYPELIB_PATH 指向系统 Atspi。
  2. 创建 FastMCP 实例。
  3. 创建 backend（平台工厂）+ coordinator（共享 RefTable）。
  4. 逐个 register 工具模块。
  5. main() 以 stdio 传输运行。

隔离沙箱**不在这里启动**（惰性）：server 是 Claude Code 会话启动即常驻拉起的 stdio
进程，若在此启 Xephyr 则每次开 Claude 都弹窗。改由首次工具调用触发，见
core/display.ensure_started 与 core/coordinator._needs_display。

backend 不可用时（非 Linux / AT-SPI 加载失败 / 无障碍开关未开），server 仍启动，
但工具调用返回友好错误，便于排查而非崩溃。
"""

from __future__ import annotations

# ① 必须最先执行：配置 GI 环境（在任何 import gi 之前）
from . import _bootstrap

_bootstrap.setup_gi_environment()

# mcp 2.x 把 FastMCP 改名为 MCPServer；1.x 仍是 FastMCP。做兼容导入。
try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP  # noqa: E402
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # noqa: E402

from .backend.base import Backend, get_backend  # noqa: E402
from .core import display  # noqa: E402
from .core.coordinator import Coordinator  # noqa: E402
from .tools import (  # noqa: E402
    action, apps, find, layout, screen_text, screenshot, ui_tree, windows,
)
from .utils.errors import BackendUnavailableError  # noqa: E402
from .utils.logging import get_logger  # noqa: E402
from .utils.refs import RefTable  # noqa: E402

log = get_logger(__name__)


def _make_backend() -> Backend:
    """
    创建平台 backend；失败时返回一个「全部抛错」的占位 backend，保证 server 可启动。

    ⚠️ M-42：这里必须接**所有**异常，不能只接 `BackendUnavailableError`。
    `get_backend()` 内部还包含 `from .linux.backend import LinuxBackend` 这一串**模块导入**
    （见 backend/base.py 的工厂），导入链上任何别的异常（缺依赖、语法/属性错误、
    循环导入…）都不是 BackendUnavailableError —— 它会带 traceback **在 import 阶段**
    直接崩掉进程，`_UnavailableBackend` 根本来不及启用，与本节 docstring 的承诺相反
    （承诺是「失败时保证 server 可启动」）。多接一层，最坏也只是占位 backend 报出原因。
    """
    try:
        return get_backend()
    except BackendUnavailableError as exc:
        log.error("backend 初始化失败: %s", exc)
        return _UnavailableBackend(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("backend 初始化出现未预期异常（仍以占位 backend 启动）")
        return _UnavailableBackend(f"backend 初始化异常 {type(exc).__name__}: {exc}")


class _UnavailableBackend(Backend):
    """占位 backend：所有能力调用都抛 BackendUnavailableError，但允许 server 正常启动。"""

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def _raise(self) -> None:
        raise BackendUnavailableError(self._reason)

    def is_available(self) -> bool:
        return False

    def list_apps(self) -> list[str]:
        self._raise()

    def get_tree(self, scope="active_window", app=None, max_depth=8, max_nodes=300):  # noqa: ANN001, ANN201
        self._raise()

    def find(self, text=None, role=None, app=None, interactive_only=True,  # noqa: ANN001, ANN201
             max_depth=12, limit=40):
        self._raise()
    # 注：两者的返回类型见 Backend ABC（QueryResult）。本类不实现任何能力，
    # 一律 _raise()，故无需构造返回值。

    def element_info(self, native):  # noqa: ANN001, ANN201
        self._raise()

    def is_alive(self, native):  # noqa: ANN001, ANN201
        self._raise()

    def focus(self, native):  # noqa: ANN001, ANN201
        self._raise()

    def invoke(self, native, action=None):  # noqa: ANN001, ANN201
        self._raise()

    def set_value(self, native, text):  # noqa: ANN001, ANN201
        self._raise()

    def element_screen_rect(self, native):  # noqa: ANN001, ANN201
        self._raise()

    def click_at(self, x, y, button=1, focus_window=True):  # noqa: ANN001, ANN201
        self._raise()

    def type_text(self, text):  # noqa: ANN001, ANN201
        self._raise()

    def press_key(self, combo):  # noqa: ANN001, ANN201
        self._raise()

    def list_windows(self, limit=100):  # noqa: ANN001, ANN201
        self._raise()

    def wait_window(self, title_contains=None, window_id=None, timeout=10.0, poll=0.25):  # noqa: ANN001, ANN201, E501
        self._raise()

    def window_screen_pos(self, native):  # noqa: ANN001, ANN201
        self._raise()

    def screen_layout(self):  # noqa: ANN201
        self._raise()

    def screenshot(self, region=None, max_side=None, fmt="jpeg", quality=85):  # noqa: ANN001, ANN201, E501
        self._raise()

    def active_window_rect(self):  # noqa: ANN201
        self._raise()

    def read_text(self, region=None, min_conf=40.0):  # noqa: ANN001, ANN201
        self._raise()

    def active_window_title(self):  # noqa: ANN201
        self._raise()

    def describe_point(self, x, y, with_text=False):  # noqa: ANN001, ANN201
        self._raise()



def create_server() -> FastMCP:
    """构建并返回已注册全部工具的 FastMCP 实例。"""
    mcp = FastMCP(
        "computer-use-mcp",
        instructions=(
            "让 AGENT 直接操作 Linux 桌面。核心理念：用无障碍元素树（结构化文本）感知界面，"
            "用元素级 do_action 操作（零坐标），替代『截图+坐标点击』。"
            "标准工作流：① get_ui_tree 看结构 → ② 找到目标元素的 [ref]（或用 find_element 搜）"
            "→ ③ click(ref)/type_text(ref)。"
            "**灰区应用**（无元素树，如 SWT/Java、自绘控件、游戏、远程桌面）：优先用 "
            "get_screen_text（OCR 出带坐标的文本，读文字即可定位）；确实需要像素级判断时才用 "
            "screenshot——它费 token 且要你自己估算坐标。"
        ),
    )
    backend = _make_backend()
    coord = Coordinator(backend=backend, ref_table=RefTable())

    # 注册全部工具
    ui_tree.register(mcp, coord)
    find.register(mcp, coord)
    action.register(mcp, coord)
    layout.register(mcp, coord)
    screenshot.register(mcp, coord)
    screen_text.register(mcp, coord)
    windows.register(mcp, coord)
    apps.register(mcp, coord)

    # ⚠️ 这里**绝不能**调 backend.is_available()（历史写法是 available=%s 打在这里）：
    # 它会一路走到 import_atspi() + get_desktop(0) + get_child_count()，即真连一条 AT-SPI
    # 总线。而本函数由模块级 `mcp = create_server()` 在**进程启动时**执行，此刻沙箱还没起
    # （惰性启动，要等首次工具调用），于是 reader 连上的是**宿主会话总线**。
    # 偏偏 libatspi 的 atspi_init() 是进程一次性的：连上哪条就永远是那条，之后 sync_bus()
    # 只能靠 Atspi.exit() 断开（而 exit 之后没有任何地方重新 init），
    # 结果是所有 a11y 工具恒报 "The application no longer exists"（2026-09-16 实测根因）。
    # 判定改到首次工具调用时按需进行——那条路径必经 coordinator._needs_display，
    # 届时沙箱已起、sync_bus() 已把 AT_SPI_BUS_ADDRESS 指向私有总线，首次 init 必然连对。
    log.info("computer-use-mcp 已就绪：backend=%s", backend.name)
    return mcp


# 模块级实例：供 `python -m computer_use_mcp.server` 与 PyInstaller 入口使用
mcp = create_server()


def _x11_liveness() -> bool:
    """
    用 **X11 通道**（xdotool 读活动窗口）判断目标屏是否活着，完全不碰 AT-SPI（M-43）。

    为什么单独写一个：`backend.is_available()` 会触碰 AT-SPI，而 isolated 模式下沙箱没
    起来时那次触碰会落到**宿主总线**上（见 `_selftest` 的说明）。selftest 需要一条
    「既不做 a11y 遍历、又能说明目标屏是否可用」的判据，X11 这条路正好合适，
    与 `list_apps` 走 `wmctrl -lpx`、`_read_titles` 走 `xdotool` 是同一个理由。
    """
    import subprocess

    try:
        from .core import display as _d

        proc = subprocess.run(["xdotool", "getactivewindow"], capture_output=True,
                              text=True, timeout=5, errors="replace",
                              env=_d.env_for())
    except Exception:  # noqa: BLE001
        return False
    # rc=0 表示目标屏有 X server 在响应；"no windows" 之类的报错也算连接成功，
    # 故判据只看是否发生了 X 连接错误。
    return proc.returncode == 0 or "Can't open display" not in (proc.stderr or "")


def _selftest() -> int:
    """
    自检模式（--selftest）：不跑 stdio 循环，直接打印 backend/工具/屏幕布局状态。

    用途：验证冻结后的可执行文件能否加载 gi+Atspi、注册工具、读到桌面。
    返回进程退出码（0=正常）。
    """
    import asyncio
    import json

    info = display.start()
    print(f"[selftest] display = {json.dumps(info, ensure_ascii=False)}")

    backend = _make_backend()
    # M-43：`is_available()` 会一路走到 AT-SPI 的 `get_desktop(0) + get_child_count()`。
    # 沙箱正常时它连的是**私有总线**（安全）；但「isolated 模式 + 沙箱没起来」时
    # `_desired_bus()` 返回 None，这次触碰就会落到**宿主总线**上 —— 而「Xephyr 缺失/
    # 启动失败」恰恰是文档明说用户会遇到的场景，也正是本自检最该给出有用结论的时候。
    # 故只在「isolated 且沙箱就绪」时才走 AT-SPI 判活；否则改用 X11 通道判活，
    # 与「selftest 不碰 AT-SPI」的约定保持一致。
    sandbox_ok = (
        display.MANAGER.mode != display.MODE_ISOLATED or display.MANAGER.is_sandbox_up()
    )
    if not sandbox_ok:
        print("[selftest] ⚠️ isolated 模式但沙箱未就绪：跳过 AT-SPI 判活"
              "（避免触碰宿主总线），改用 X11 通道判断")
        x11_ok = _x11_liveness()
        print(f"[selftest] X11 通道可用 = {x11_ok}")
        if not x11_ok:
            print("[selftest] ❌ 沙箱不可用且宿主 X11 也读不到："
                  "检查 xserver-xephyr / DISPLAY 设置")
            return 2
    else:
        # 只探一次：`is_available()` 在真 backend 里会碰 AT-SPI，而 selftest 的全部意义
        # 之一就是「尽量少碰它」（见上面的 M-43 说明）。原先这里连着调了两次。
        available = backend.is_available()
        print(f"[selftest] backend = {backend.name}, available = {available}")
        if not available:
            print(f"[selftest] ❌ AT-SPI 不可用：{getattr(backend, '_reason', None) or 'see log'}")
            return 2

    async def _list() -> list[str]:
        tools = await mcp.list_tools()
        return [t.name for t in tools]

    names = asyncio.run(_list())
    print(f"[selftest] 已注册工具({len(names)}) = {names}")

    coord = Coordinator(backend=backend, ref_table=RefTable())
    print("[selftest] screen_layout =", json.dumps(coord.screen_layout(), ensure_ascii=False))
    apps = backend.list_apps()
    print(f"[selftest] 桌面应用数 = {len(apps)}，前5 = {apps[:5]}")
    # 光圈状态：最常见的「怎么没看到红圈」就是它被 env 关了（CC_CU_CLICK_RING=0）或被
    # 按屏熔断（连续失败 2 次）。这里只读内存、不画圈（画圈要真点在屏上，那是 e2e 的事）。
    from .backend.linux import ring as _ring

    print(f"[selftest] 点击光圈 = {json.dumps(_ring.status(), ensure_ascii=False)}")
    print("[selftest] ✅ OK")
    return 0


def main() -> None:
    """
    入口：默认以 stdio 传输运行 MCP server；--selftest 则只自检后退出。

    （console_scripts / PyInstaller / `python -m computer_use_mcp.server` 共用）
    """
    import sys

    # 再次确保环境就绪（冻结后 _bootstrap 已在 import 时执行，这里幂等保险）
    _bootstrap.setup_gi_environment()
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    # 隔离沙箱**不在此启动**（惰性）：本进程是 Claude Code 会话启动即常驻拉起的 stdio
    # server，在这里 start() 会导致每次开 Claude 都弹一个虚拟屏——哪怕整场会话一次都
    # 没用过本工具。改由首次工具调用触发（Coordinator 对外方法 → display.ensure_started）。
    mcp.run(transport="stdio")



if __name__ == "__main__":
    main()
