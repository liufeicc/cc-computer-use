"""
键盘输入（backend.linux.inject.keyboard）—— 文本输入与快捷键。

本模块有一处**刻意的不对称**：纯 ASCII 短文本走 `xdotool type`，其余（含中文、或较长）
走**剪贴板粘贴**。原因与代价见 `type_text` / `_type_via_clipboard` 的 docstring ——
`xdotool type` 处理非 Latin1 字符时会临时改写全局键盘映射，输入期间用户物理键盘整体失灵。
"""

from __future__ import annotations

import shutil
import subprocess
import time

from ....core import display
from ....utils.errors import InjectionError
from .base import log

# 长文本（含纯 ASCII）也走剪贴板粘贴的字符阈值：逐字符 xdotool type 太慢
# （60 字符约 1s），剪贴板 + ctrl+v 是一次原子操作，长串快一个量级。
_CLIPBOARD_MIN_LEN = 24

# 键名别名归一表：把 agent 常用但 xdotool 不认的写法翻译成合法 keysym，
# 避免「发错键名却静默无操作」白耗一轮。键统一小写匹配。
_KEY_ALIASES = {
    "pagedown": "Page_Down", "pgdn": "Page_Down", "pgdown": "Page_Down",
    "pageup": "Page_Up", "pgup": "Page_Up",
    "enter": "Return", "return": "Return",
    "esc": "Escape", "escape": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "space": "space", "spacebar": "space",
    "tab": "Tab", "del": "Delete", "delete": "Delete",
    "backspace": "BackSpace", "back": "BackSpace",
    "home": "Home", "end": "End", "insert": "Insert", "ins": "Insert",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "super": "super", "win": "super", "meta": "super", "cmd": "super",
}


class KeyboardMixin:
    """文本输入（含剪贴板通道）与快捷键。"""

    # ---------- 键盘 ----------
    def type_text(self, text: str, delay_ms: int = 12) -> bool:
        """
        键盘输入文本（按内容分流）。

        实现逻辑：
          - 短纯 ASCII（<_CLIPBOARD_MIN_LEN 字符）：xdotool type（--clearmodifiers 清粘连
            修饰键，--delay 控制节奏）。
          - 含非 ASCII（中文等）或较长文本：改走剪贴板粘贴 _type_via_clipboard。
            原因（实测坑）：xdotool type 对非 Latin1 字符会临时改写全局键盘映射
            （XChangeKeyboardMapping 把字符绑到空闲 keycode 再模拟敲击），输入期间
            物理键盘整体失灵（鼠标不受影响），且与 fcitx 等输入法争抢 XKB 状态；
            剪贴板 + ctrl+v 是一次性原子操作，不改键位映射、不冻键盘、快一个量级。
        """
        if text.isascii() and len(text) < _CLIPBOARD_MIN_LEN:
            # `--` 终止选项解析（M-33）：文本以 `-` 开头时（如 "-1"、"--flag"、" - item"），
            # getopt_long_only 会把它当成 xdotool 自己的选项 —— 实测 `xdotool type -- -1`
            # 才是「敲出 -1」，不加 `--` 则报 unrecognized option 直接失败。
            # ⚠️ `--` 必须放在**所有选项之后**、位置参数之前（放最前面会把 --clearmodifiers
            # 一起当成位置参数）。
            self._run(["type", "--clearmodifiers", "--delay", str(delay_ms), "--", text],
                      check=True)
            return True
        return self._type_via_clipboard(text)

    def _clipboard_targets(self, xclip: str) -> list[str]:
        """
        探测剪贴板当前提供哪些 target（空剪贴板返回 []）。

        用途（M-30①）：`xclip -o` 取不回内容（rc≠0）时，用它区分「剪贴板本来就是空的」
        与「有内容但不是文本（图片等）」——后者**还原不了**，必须让用户/日志看得见。

        环境就地经 `display.env_for()` 取（不接一个 `env` 形参）：`tests/test_spawn_env.py`
        会枚举每一处 spawn 点、要求 `env=` **可追溯到** env_for/app_env，形参不在它的
        追踪范围内——那条件测是防「新增 spawn 点忘了接库路径剥离」的，别为省一次调用来绕它。
        """
        try:
            p = subprocess.run([xclip, "-selection", "clipboard", "-o", "-t", "TARGETS"],
                               capture_output=True, timeout=3, env=display.env_for())
            return p.stdout.decode("utf-8", "replace").split() if p.returncode == 0 else []
        except Exception:  # noqa: BLE001
            return []

    def _type_via_clipboard(self, text: str) -> bool:
        """
        通过剪贴板输入非 ASCII 文本。

        实现逻辑：
          1. 备份当前剪贴板内容（尽力而为，失败不阻塞主流程）。
          2. xclip 写入 clipboard selection；xclip 写完会自动 fork 到后台
             持续伺服 selection 请求，目标应用 ctrl+v 时才能取到内容。
          3. 短暂等待（确保剪贴板所有权就绪）后发 ctrl+v 粘贴。
          4. **无论成功失败**（finally）都把原内容写回，避免污染用户剪贴板
             （立即写回有竞态：目标应用可能异步请求 selection 时拿到还原后的内容）。
        """
        xclip = shutil.which("xclip")
        if not xclip:
            # 无 xclip 时退回 xdotool type（会短时冻结物理键盘，告警提示装 xclip）
            log.warning("未安装 xclip，中文输入退回 xdotool type（会临时冻结物理键盘，"
                        "建议 sudo apt install xclip）")
            self._run(["type", "--clearmodifiers", "--delay", "12", "--", text], check=True)
            return True
        # 1. 备份原剪贴板（-o 输出当前内容；无内容/失败则记 None）
        #    剪贴板是 per-display 资源：读写都必须与注入目标同屏（沙箱内 ctrl+v 才能取到）
        clip_env = display.env_for()
        old: bytes | None = None
        had_non_text = False
        try:
            p = subprocess.run([xclip, "-selection", "clipboard", "-o"],
                               capture_output=True, timeout=3, env=clip_env)
            if p.returncode == 0:
                old = p.stdout
            else:
                # M-30①：rc≠0 的常见成因就是「有内容但不是文本」（图片等）。这一份
                # 无法通过 xclip 读回字节、也就**还原不了**——历史实现只是静默把 old 留成
                # None，用户图片剪贴板被替换后**毫无提示**，且 GNOME 的剪贴板管理器会把
                # 这段 agent 文本永久记入历史。故这里显式探测并告警。
                had_non_text = bool(self._clipboard_targets(xclip))
        except Exception as exc:  # noqa: BLE001
            log.debug("剪贴板备份失败（将不做还原）: %s", exc)
            old = None
        # 2. 写入新文本
        #    ⚠️ M-31：xclip **写入**模式会 fork 一个后台守护进程长期持有 selection，
        #    它继承本进程的 fd 1/2，而这是 stdio MCP server —— 子进程往 stdout 写一个字节
        #    就会污染 JSON-RPC 流。故必须显式 DEVNULL。
        #    刻意**不用 capture_output=True**：管道写端会被那个 fork 出来的守护进程一直持着，
        #    `subprocess.run` 内部的 communicate() 要等所有写端关闭才返回，于是每次中文输入
        #    都会**白等满超时**才继续（实测 5s）。DEVNULL 没有这个坑。
        try:
            w = subprocess.run([xclip, "-selection", "clipboard"],
                               input=text.encode("utf-8"), timeout=5, env=clip_env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise InjectionError("xclip 写入剪贴板超时") from exc
        if w.returncode != 0:
            raise InjectionError(f"xclip 写入剪贴板失败(rc={w.returncode})")
        try:
            # 3. 粘贴（缩短等待：剪贴板所有权就绪通常 <50ms）
            time.sleep(0.05)
            self.press_key("ctrl+v")
        finally:
            # 4. 还原原剪贴板 —— **必须在 finally 里**（M-30②）：粘贴一旦抛异常，历史实现
            #    会直接向上抛，剪贴板就**停在 agent 文本上**，而这同样会被剪贴板管理器
            #    记入历史（若刚才输入的是口令类内容，就是持续残留的泄密面）。
            time.sleep(0.2)
            if old is not None:
                try:
                    subprocess.run([xclip, "-selection", "clipboard"], input=old, timeout=5,
                                   env=clip_env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                except Exception as exc:  # noqa: BLE001
                    log.warning("原剪贴板内容还原失败（剪贴板将停留在本次输入的文本上）: %s", exc)
            elif had_non_text:
                log.warning(
                    "原剪贴板内容为非文本（图片等），本次输入已将其替换且**无法还原**；"
                    "GNOME 剪贴板历史中会留下本次输入的文本")
        return True

    def press_key(self, combo: str) -> bool:
        """
        快捷键，如 'ctrl+s'、'Return'、'alt+F4'、'Page_Down'。

        实现逻辑：
          1. 先键名归一（_normalize_combo）：把常见别名（PageDown/Enter/Esc/方向键等）
             翻译成 xdotool 认识的 keysym。
          2. 调 xdotool key。xdotool 对未知 keysym 只打 warning 且返回码仍为 0，
             因此额外检查 stderr 是否含 'No such key'/'Ignoring'，有则判失败抛错
             （fail-loud，避免静默 no-op 白耗一轮）。
        """
        normalized = self._normalize_combo(combo)
        proc = self._run(["key", "--clearmodifiers", normalized], check=True)
        err = proc.stderr or ""
        if "No such key" in err or "Ignoring" in err:
            raise InjectionError(
                f"xdotool 不识别按键 '{combo}'（归一为 '{normalized}'）: {err.strip()}"
            )
        return True

    @staticmethod
    def _normalize_combo(combo: str) -> str:
        """
        把 combo 中每个 '+' 分隔的 token 做别名归一；未知 token 原样保留。

        例：'ctrl+pagedown' → 'ctrl+Page_Down'；'Enter' → 'Return'；'ctrl+a' 不变。
        """
        tokens = combo.split("+")
        out = []
        for tok in tokens:
            key = tok.strip().lower()
            out.append(_KEY_ALIASES.get(key, tok.strip()))
        return "+".join(out)