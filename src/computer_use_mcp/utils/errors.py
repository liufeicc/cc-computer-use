"""
统一错误类型与结果封装。

设计目标：
  - backend / core / tools 各层抛出的异常都归类到 ComputerUseError 体系，
    便于 server 层统一捕获并转成对 LLM 友好的文本结果（而非 traceback）。
  - 提供 ActionResult 数据类，承载「操作是否成功 + 用了哪一级降级 + 细节」，
    这是 coordinator 三级降级编排的统一返回结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .logging import get_logger

log = get_logger(__name__)

# MCP 的 `ToolError`：抛出后 SDK 会把文本原样放进 content，并把 is_error 立成 True。
# 版本兼容（同 server.py / tools/screenshot.py 的写法）：2.x 在 mcp.server.mcpserver.exceptions，
# 1.x 在 mcp.server.fastmcp.exceptions。都拿不到时退化为一个本地异常类 —— 那等于「回到
# 普通异常」的旧行为，功能不受影响，只是少了 is_error 标记。
try:  # mcp 2.x
    from mcp.server.mcpserver.exceptions import ToolError
except Exception:  # noqa: BLE001
    try:  # mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
    except Exception:  # noqa: BLE001
        class ToolError(Exception):  # type: ignore[no-redef]
            """兜底：SDK 不提供时不改变行为（见上）。"""


class ComputerUseError(Exception):
    """所有自定义错误的基类。"""


class BackendUnavailableError(ComputerUseError):
    """平台 backend 不可用（如非 Linux、AT-SPI 加载失败、无障碍开关未开）。"""


class ElementNotFoundError(ComputerUseError):
    """按 ref / 文本 / 角色未找到目标元素。"""


class InvalidRefError(ComputerUseError):
    """ref 失效或非法（树已变化、ref 越界、ref 过期）。"""


class ActionFailedError(ComputerUseError):
    """元素级或坐标级操作执行失败。"""


class InjectionError(ComputerUseError):
    """底层注入（xdotool 等）调用失败。"""


class ScreenBusyError(ComputerUseError):
    """
    屏幕正被另一个操作独占（通常是并发的子 agent），本次调用**未被排队，而是直接拒绝**。

    为什么拒绝而不是排队（关键设计决策）：LLM 的操作是「感知 → 决策 → 操作」的序列。
    若把并发调用排进队列，A 的点击会在 B 的操作之后才真正落地，而 A 是基于**排队前**
    看到的界面做的决策——界面早已变了，点击会打偏，工具却返回「成功」。这种「延迟执行
    + 旧认知」的组合比直接报错危险得多。故一律快速失败，逼调用方重新感知。

    背景：同一 Claude 会话的主 agent 与所有子 agent 共用**同一个 MCP server 进程**
    （子 agent 不新起 claude.exe，因而也不新起 MCP server），于是共用一块虚拟屏、
    一个 X11 指针/焦点、一份剪贴板与一张 ref 表。X11 每块屏只有一套指针，**真并行在
    物理上不存在**，只能串行——而串行的边界由调用方决定，不能由服务端偷偷排队。
    """


class SandboxUnavailableError(ComputerUseError):
    """
    isolated 模式下沙箱不可用，**已拒绝注入**以免误操作宿主真实桌面（B 防线）。

    与「静默回落宿主」的区别：回落时 LLM 只看得到「点击成功」，会继续在用户真实桌面上
    操作而毫无察觉。实测这条路真会走到——多会话共享同一块沙箱时，建屏方退出会带走屏，
    另一方的 effective_display() 就变成了宿主 `:1`。

    本错误把这条通道封死：要么在沙箱里操作，要么明确报错。确实要直接操作真实桌面，
    请显式设 `CC_CU_DISPLAY_MODE=real`——那是用户主动的选择，不算偷偷。
    """


# 三级降级层级标识
#
# ⚠️ `ActionResult.level` 的语义是「**实际生效**的那一级」，不是「建议你改用哪一级」（M-18）。
#    这两者混起来会让失败回报说谎：例如 click 在「没找到元素」时既没做元素级、也没点坐标、
#    更**没有截过图**，却报 level=screenshot，to_text() 于是打印「失败（层级=screenshot）」——
#    模型会以为截图这条路已经试过并且失败了。所以：
#      - 没有任何一级生效（失败、或还没走到那一级）→ LEVEL_NONE；
#      - 「建议改用截图」这类**下一步提示**放 message / data["hint"]，不占 level 字段。
LEVEL_NONE = "none"              # 无层级生效：失败，或尚未走到任何一级
LEVEL_ELEMENT = "element"        # 元素级 do_action（首选，零坐标）
LEVEL_COORD = "coord"            # 坐标点击（兜底，需校准 + 聚焦）
LEVEL_KEY = "key"                # 键盘注入（type_text / press_key）——**不是**坐标点击
LEVEL_SCREENSHOT = "screenshot"  # 截图（灰区：无元素树）——只有**真的截了图**才可用它


def to_friendly_text(exc: Exception, label: str) -> str:
    """
    把工具层捕获的任意异常转成回给模型的友好文本。

    为什么存在（I-5，见 docs/REVIEW/review_0.1.0.md）：mcp 2.x 对**非 MCPError** 的异常
    刻意**不带原文**地包装成 UnexpectedToolError，模型只拿到一句 "Error executing tool X"
    ——既不知道是参数写错、依赖缺失，还是「命令名打错」，只能盲试。而工具层的 except
    原先只覆盖 ComputerUseError，其余异常都会走到那条路上。

    分流规则：
      - ComputerUseError（可预期失败）：原样给出原因，它本就是写给模型看的；
      - 其它异常：给出「类型 + 原因」，**完整堆栈只进服务端日志**（stderr），不回给模型。

    这样既不丢排查线索，也不把内部实现细节暴露进对话。
    """
    if isinstance(exc, ComputerUseError):
        return f"❌ {label}：{exc}"
    log.exception("工具层未预期异常（%s）", label)
    return f"❌ {label}：内部错误 {type(exc).__name__}: {exc}"


def to_tool_error(exc: Exception, label: str) -> Exception:
    """
    把一个**非预期异常**转成待抛出的 `ToolError`（M-47 折中版）。

    为什么是折中（而不是「失败一律抛 ToolError」）：CLAUDE.md 明确定义了 tools 层的职责是
    「只做参数解析 + 错误转友好文本」，`except ComputerUseError` 那条**返回文本**是本项目
    既定的错误契约，全量改成抛异常等于把它推倒重来。真正缺的是另一半——笼统的
    `except Exception` 兜底：它返回的也是普通文本，于是协议层 `isError=false`，
    客户端无法按 `isError` 统计或触发重试，日志里失败调用与成功调用也分不开。

    mcp 2.x 对 `ToolError` 的处理是 `CallToolResult(content=[Text(…)], is_error=True)`，
    即**文本原样带给模型、同时把 is_error 立起来**，正好兼得两边。故这里只把兜底那条改掉。

    文本与 `to_friendly_text` 完全一致（含 ❌ 前缀与「内部错误 <类型>: <原因>」的分流），
    故模型侧看到的信息量没有变化。
    """
    return ToolError(to_friendly_text(exc, label))


@dataclass
class ActionResult:
    """
    一次操作的统一结果。

    字段：
      ok: 是否成功
      level: **实际生效**的层级（element/coord/key/screenshot/none）——不是「建议下一步用哪一级」，
             什么都没生效时用 LEVEL_NONE（见文件上方 LEVEL_* 的说明 / M-18）
      message: 人类/LLM 可读的说明
      data: 结构化附加数据（如坐标、ref、动作名、退出码等）
      attempts: 各级尝试的记录（用于诊断「为什么降级」）
      preview: 点击前抓的准星小图 `(JPEG bytes, meta)`；没有这项能力/未开启时为 None

    ⚠️ `preview` 是**独立字段、绝不能塞进 `data`**：`to_text()` 会把 data 逐项 `f"{k}={v}"`
    拼进给模型看的文本，bytes 一旦进去就会被 repr 成 `b'\\xff\\xd8...'` 灌进上下文
    （几十 KB 的无意义文本）。独立字段则天然绕开 `to_text()`/`attempts` 两条序列化路径。
    图像本身由 tools 层取出、转成 MCP Image content block（见 utils/blocks.py）。
    """

    ok: bool
    level: str = "none"
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    preview: tuple[bytes, dict[str, Any]] | None = None

    def to_text(self) -> str:
        """序列化为对 LLM 友好的紧凑文本。"""
        head = "✅ 成功" if self.ok else "❌ 失败"
        lines = [f"{head}（层级={self.level}）：{self.message}"]
        if self.data:
            kv = " ".join(f"{k}={v}" for k, v in self.data.items())
            lines.append(f"  数据: {kv}")
        if self.attempts:
            lines.append("  尝试记录:")
            for a in self.attempts:
                lines.append(f"    - {a.get('level')}: ok={a.get('ok')} {a.get('note','')}")
        return "\n".join(lines)
