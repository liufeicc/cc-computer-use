"""
落点证据（core.coordinator.landing）—— 省掉「再截一张图确认」的整个来回。

**为什么这是大头**（实测数据支撑，改前先读）：解析一条真实 DBeaver 任务（499s）得到
——工具自身执行 21s（4%），模型侧空档 478s（96%）。工具（截图 0.2s / 点击 0.2s）
根本不是瓶颈，「模型每轮要看什么、要看几次」才是。

坐标级点击与键盘注入的 `ok=True` **只说明「事件发出去了」** —— xdotool 不关心点到了
什么，所以模型只能再截一张图确认，那是整整一个来回。把「落在哪扇窗、底下什么字、
点后活动窗口」直接回报，那个来回就不用走了。

⚠️ 本模块的所有方法**一律不得让主操作失败**：读证据出错只记 debug 日志、返回空 dict。
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

from ...utils.errors import LEVEL_COORD, LEVEL_NONE, ActionResult
from .hooks import log

# 最近若干次坐标点击的「回看」记录（进程内）。
#
# 为什么要留这份缓存：点击**当时**的落点画面是判断「有没有点偏」的唯一直接证据，而模型
# 拿到点击结果后未必立刻反应过来；等它发现「界面没动静」再想看「刚才到底点在哪」时，
# 界面早已变了。留一个环形缓冲，就能让 `get_last_click_image` 按需取回。
#
# 为什么只留 8 条：一次排查通常只需看最近一两次；再多只是白占内存（每条 ~10KB JPEG）。
# 为什么放模块级而不是实例级：Coordinator 是进程单例、MCP server 常驻，模块级同样是
# 「本次会话内可见」，且不会随任何重建流程被清掉（这点与 RefTable 的工作前提一致：
# **同进程内存**，不跨进程、不跨会话）。
_CLICK_LOG: deque[dict[str, Any]] = deque(maxlen=8)
_CLICK_LOG_LOCK = threading.Lock()


class LandingMixin:
    """采集与格式化「落点证据」。"""

    # 注入之后读活动窗口前的等待秒数。为什么必须有（2026-09-15 DBeaver 实测）：
    # `alt+F4` 关掉 Chrome 后立即读活动窗口，回报的**仍是 Chrome**——此时窗口销毁 /
    # 焦点转移还没在 X 上落定，读到的是「变化前」的状态。这会**误导**判断（我据此
    # 以为 alt+F4 没生效，多花了一轮去确认）。证据读早了比不读更糟，所以宁可等这一下。
    # 0.2s 相对它省下的一个来回（实测 30~70s）可忽略。
    _INJECT_SETTLE = 0.2

    def _landing(self, settle: float = 0.0) -> dict[str, Any]:
        """
        当前活动窗口标题，包装成落点证据的一部分；拿不到返回空 dict。

        settle>0 时先等待再读（注入类操作的**事后**读取用 _INJECT_SETTLE，
        事前的快照传 0——那时没有等待的理由，白等只是拖慢每次点击）。
        """
        if settle:
            time.sleep(settle)
        try:
            t = self.backend.active_window_title()
        except Exception as exc:  # noqa: BLE001
            log.debug("landing: 取活动窗口标题失败 %s", exc)
            return {}
        return {"active_window": t} if t else {}

    def _point_evidence(self, x: int, y: int, with_text: bool = False) -> dict[str, Any]:
        """
        取 (x,y) 的落点证据（哪扇窗 + 可选：底下什么字）。

        ⚠️ 必须**在点击之前**调用：describe_point 内部会把指针移到 (x,y) 去问 X
        「那儿是哪个窗口」，而点击本来也要把指针放到那儿，故此时调用无副作用；
        点完再问就晚了——弹窗可能已经盖住原位置，读到的是「点出来的新界面」而不是落点。
        """
        try:
            ev = self.backend.describe_point(x, y, with_text=with_text)
        except Exception as exc:  # noqa: BLE001
            log.debug("落点证据获取失败: %s", exc)
            return {}
        return ev or {}

    # 点击预览图的开关环境变量。**含义是「抓不抓」**：工具参数 preview=false 才决定
    # 「随响应附不附」。默认开着（每次坐标点击多 ~30ms 抓屏 + ~190 token），
    # 想彻底关掉（连抓都不抓）设 CC_CU_CLICK_PREVIEW=0。
    _PREVIEW_ENV = "CC_CU_CLICK_PREVIEW"

    def _resolve_preview(self, want: bool | None) -> bool:
        """
        决定这次坐标点击要不要抓预览图：**工具参数优先，其次环境变量，默认开**。

        为什么环境变量每次现读（而不是模块导入时读一次）：一次 dict 查找是纳秒级，
        换来的是测试可以 `monkeypatch.setenv` 直接改行为，不必与导入时序较劲。
        """
        if want is not None:
            return bool(want)
        raw = os.environ.get(self._PREVIEW_ENV)
        if raw is None:
            return True
        return raw.strip().lower() not in ("0", "false", "no", "off", "")

    def _preview_image(
        self, x: int, y: int, want: bool | None = None,
    ) -> tuple[bytes, dict[str, Any]] | None:
        """
        取「点击前」落点周围的小图（带准星），失败返回 None。

        ⚠️ 必须**在点击之前**调用（与 `_point_evidence` 同一条纪律）：点完弹窗可能已经
        关掉，画面上的证据反而丢了——而「我瞄的那个位置当时压着什么」正是这张图要回答的。

        ⚠️ 与 `_point_evidence` 同样**绝不让主操作失败**：读图出错只记 debug 日志、
        返回 None，点击照常执行（证据是附加品，把它变成新的失败点得不偿失）。
        """
        if not self._resolve_preview(want):
            return None
        try:
            return self.backend.click_preview(int(x), int(y))
        except Exception as exc:  # noqa: BLE001
            log.debug("点击预览图获取失败（不影响点击）: %s", exc)
            return None

    # ============ 点击评估：把「偏了多少」算成数字 ============
    # 报「最近文字块」的最大中心距（像素）：超过这个距离就不再指认候选——把远处一个
    # 无关的文字块当成「你想点的目标」比不报更误导（与 _text_near 的阈值同源）。
    _AIM_NEAR_MAX = 64

    @staticmethod
    def _assess_aim(
        x: int, y: int, blocks: list[Any], expect: str | None = None,
    ) -> dict[str, Any]:
        """
        纯函数：由一块区域里的 OCR 文本块，算出「落点压着什么、离目标差多少」。

        没有任何智能判断，就是**坐标系里的减法**：落点坐标是调用方给的（绝对精确），
        文字块的位置是 OCR 算出来的（TSV 自带 left/top/width/height），两者一减就是偏差。
        返回结构（各项缺失即为 None，绝不抛）：
          landing_text/conf : 压住落点的文字块（取面积最小者 = 最具体的那块）
          nearest           : 最近候选块 {text, center, dx, dy, dist}（dx>0 表示落点在中心右侧）
          expect            : 期望目标的判定 {query, found, hit, text, center, dx, dy, dist,
                              suggest}(未命中时给出「建议改点的坐标」= 该块中心)
        为什么要 `expect`：**没有它，程序不可能知道模型想点谁**，只能给「离最近文字块多远」
        这种启发式；有了它才能报出精确偏差与可直接采用的修正坐标。
        """
        out: dict[str, Any] = {"landing_text": None, "landing_conf": None,
                               "nearest": None, "expect": None}
        x, y = int(x), int(y)
        if not blocks:
            return out

        def _contains(b: Any) -> bool:
            r = b.rect
            return r.x <= x <= r.x + r.w and r.y <= y <= r.y + r.h

        def _dist(b: Any) -> float:
            cx, cy = b.rect.center
            return ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5

        def _offset(b: Any) -> dict[str, Any]:
            cx, cy = b.rect.center
            dx, dy = x - cx, y - cy
            return {"text": b.text, "conf": b.conf, "center": [int(cx), int(cy)],
                    "dx": int(dx), "dy": int(dy), "dist": int(round((dx * dx + dy * dy) ** 0.5)),
                    "suggest": [int(cx), int(cy)]}

        inside = [b for b in blocks if _contains(b)]
        if inside:
            # 面积最小的那块最具体（一块大 panel 里套着按钮时，别把 panel 的文字当成落点文字）
            best = min(inside, key=lambda b: b.rect.w * b.rect.h)
            out["landing_text"], out["landing_conf"] = best.text, best.conf

        # 最近候选：优先「没压住落点」的块（压住的那块已经在落点文字里说过了）
        # ⚠️ 用**同一性**判断而不是 `b in inside`：dataclass 的 __eq__ 是值相等，
        #    两块内容相同的文字会被误判成同一块。
        others = [b for b in blocks if not any(b is i for i in inside)]
        pool = others or blocks
        cand = min(pool, key=_dist)
        if _dist(cand) <= LandingMixin._AIM_NEAR_MAX:
            out["nearest"] = _offset(cand)

        if expect:
            q = str(expect).strip()
            # 只在**裁剪图内**的文字里找：找不到就明说「未见」，不猜（猜错比不报更糟）
            matches = [b for b in blocks if q and q.lower() in b.text.lower()]
            if not matches:
                out["expect"] = {"query": q, "found": False}
            else:
                b = min(matches, key=_dist)
                info = {"query": q, "found": True, "hit": _contains(b)}
                info.update(_offset(b))
                out["expect"] = info
        return out

    @staticmethod
    def _format_aim(assess: dict[str, Any]) -> str:
        """
        把 `_assess_aim` 的结果拼成一行给模型看的文本（只报事实，不作断言）。

        形如：『最近文字「取消」中心 (426,261)，偏差 (-17,+42) 共45px ｜
        期望「保存」：未命中，偏差 (-17,+42) 共45px，建议改点其中心 (517,258)』
        """
        parts: list[str] = []
        near = assess.get("nearest")
        if near:
            parts.append(f"最近文字「{near['text']}」中心 ({near['center'][0]},{near['center'][1]})"
                         f"，偏差 ({near['dx']:+d},{near['dy']:+d}) 共{near['dist']}px")
        exp = assess.get("expect")
        if exp:
            q = exp["query"]
            if not exp.get("found"):
                parts.append(f"期望「{q}」：裁剪范围内未见该文字（可能不在此处或未被识别）")
            elif exp.get("hit"):
                parts.append(f"期望「{q}」：已命中（落点在该文字块内）")
            else:
                sx, sy = exp["suggest"]
                parts.append(
                    f"期望「{q}」：未命中，偏差 ({exp['dx']:+d},{exp['dy']:+d}) 共{exp['dist']}px"
                    f"，建议改点其中心 ({sx},{sy})"
                )
        return " ｜ ".join(parts)

    @staticmethod
    def _format_change(frac: float | None) -> str:
        """
        把「变化像素占比」拼成一行文本。

        三段划分（<0.5% 无 / <5% 轻微 / ≥5% 明显）是**经验阈值**，故措辞只报事实与百分比，
        并注明观察窗口——慢界面（异步加载、动画）可能滞后于这个 0.2s 窗口，
        所以它是「命中与否的参考」，不是断言。光标闪烁/动画会贡献几个百分点，阈值已避开。
        """
        if frac is None:
            return ""
        pct = frac * 100.0
        level = "无" if frac < 0.005 else ("轻微" if frac < 0.05 else "明显")
        return (f"点后界面变化：{level}（{pct:.1f}%，点击后 {LandingMixin._INJECT_SETTLE}s 的"
                f"同区域像素对比，慢界面可能滞后，作参考不作断言）")

    def _aim_from_preview(
        self, x: int, y: int, shot: Any, expect: str | None,
    ) -> tuple[dict[str, Any], str]:
        """
        用预览图的**原始图**（未画准星那张）做一次 OCR，算出落点文字 / 最近候选 / 期望偏差。

        为什么用原始图而不是发给模型的那张：红十字横穿文字会让 tesseract 识别变差
        （见 ocr.py 的「文字紧邻深色图标时整行会崩」同类问题）——不能为了给模型看，
        把自己的计算依据弄脏。

        返回 `(评估结果, 文字)`；任何一步失败都退化为空结果（证据不得让点击失败）。
        """
        if shot is None or getattr(shot, "image", None) is None:
            return {}, ""
        meta = getattr(shot, "meta", {}) or {}
        origin = meta.get("origin") or [0, 0]
        try:
            blocks = self.backend.read_text_from_image(shot.image, (int(origin[0]), int(origin[1])))
        except Exception as exc:  # noqa: BLE001
            log.debug("预览图 OCR 失败（跳过点击评估）: %s", exc)
            return {}, ""
        assess = self._assess_aim(x, y, blocks, expect)
        return assess, self._format_aim(assess)

    def _change_from_preview(self, shot: Any) -> str:
        """
        点后抓同一块与预览的**原始图**对比，返回「界面变化」文本。

        ⚠️ 必须在**注入并等待 settle 之后**调用（调用方在 `_landing(_INJECT_SETTLE)` 之后
        再调本方法），否则读到的是变化前的屏幕，等于白比一次。
        """
        if shot is None or getattr(shot, "image", None) is None:
            return ""
        meta = getattr(shot, "meta", {}) or {}
        region = meta.get("region")
        if not region:
            return ""
        try:
            frac = self.backend.change_fraction_since(tuple(region), shot.image)
        except Exception as exc:  # noqa: BLE001
            log.debug("点后变化对比失败: %s", exc)
            return ""
        return self._format_change(frac)

    # ============ 回看：把最近几次点击的画面与评估留在进程内 ============
    def _remember_click(
        self, x: int, y: int, level: str, shot: Any, note: str = "",
    ) -> None:
        """
        记一次坐标点击（含预览图与评估文字），供 `get_last_click_image` 回看。

        为什么连**评估文字**也一起存：模型回看时最想知道的是「当时程序说偏了多少、建议点哪」，
        那是几行文本；把图和结论存在一起，回看一次就全拿回来了。

        `shot` 为 None（本次没抓图，如 preview=false）时只记文字与坐标——记录照留，
        否则「我明明点过」与「记录消失」对不上，会让模型以为按钮失灵。
        """
        rec = {
            "ts": time.time(), "x": int(x), "y": int(y), "level": level,
            "note": note,
            "preview": (shot.data, shot.meta) if shot is not None else None,
        }
        with _CLICK_LOG_LOCK:
            _CLICK_LOG.append(rec)

    def get_last_click_image(self, index: int = -1) -> ActionResult:
        """
        回看最近一次坐标点击的**准星小图与评估结论**（默认最后一条，index=-2 是上上次）。

        什么时候用它：点击之后界面没有预期反应，而模型想知道「刚才到底点在哪、程序算的偏差
        是多少、建议改点哪个坐标」——比重新截图重新估算便宜得多（一次调用，不重跑感知），
        也避免了「盲目再点一次」（实测那是最容易把界面点花的做法）。

        返回的 `preview` 由 tools 层转成图像块；没有图的记录（preview 被关掉的那次）
        会明确说明，而不是静默返回一个空结果。
        """
        with _CLICK_LOG_LOCK:
            entries = list(_CLICK_LOG)
        if not entries:
            return ActionResult(
                ok=False, level=LEVEL_NONE,
                message="尚无坐标点击记录（本会话内只要发生过坐标级点击就会有记录；"
                        "元素级点击零坐标，不产生记录）",
                data={"count": 0},
            )
        try:
            rec = entries[int(index)]
        except (IndexError, ValueError, TypeError):
            return ActionResult(
                ok=False, level=LEVEL_NONE,
                message=f"索引越界：本会话共 {len(entries)} 条点击记录（index=-1 是最近一次）",
                data={"count": len(entries)},
            )
        age = max(0, int(time.time() - float(rec.get("ts") or 0)))
        msg = (f"第 {abs(int(index))} 次最近的坐标点击 @({rec['x']},{rec['y']})"
               f"（{age} 秒前，层级={rec['level']}）")
        if rec.get("note"):
            msg += f"｜{rec['note']}"
        return ActionResult(
            ok=True, level=LEVEL_COORD, message=msg,
            data={"x": rec["x"], "y": rec["y"], "age_s": age,
                  "count": len(entries),
                  "has_preview": rec.get("preview") is not None},
            preview=rec.get("preview"),
        )

    @staticmethod
    def _format_landing(
        ev: dict[str, Any], active_after: dict[str, Any] | None = None,
        active_before: str | None = None,
    ) -> str:
        """
        把落点证据拼成一行人类/模型可读的文本。

        形如：『落点：窗口「DBeaver — SQL 编辑器」 1280x800 ｜ 落点文字：「确定」 ｜
        点后活动窗口：「确认删除」』。取不到的项直接省略（留白比写 None 更省 token 也更不易误读）。

        ⚠️ 为什么还要 active_before（点击**前**的活动窗口）：实测点「确定」这类按钮会
        **把对话框关掉**，此时点后已经没有活动窗口标题可读——最关键的那一刻证据反而丢了。
        有了前值就能说清结果：『点后活动窗口：无（原「OCR故事」已消失）』，这本身就是
        「点中了、且窗口确实关了」的强信号。有前有后且不同则并列显示「新（原旧）」。
        """
        parts: list[str] = []
        title = ev.get("window_title")
        rect = ev.get("window_rect")
        if title or rect:
            size = f" {rect[2]}x{rect[3]}" if rect and len(rect) == 4 else ""
            parts.append(f"落点：窗口「{title or '?'}」{size}")
        if ev.get("text"):
            parts.append(f"落点文字：「{ev['text']}」")
        after = (active_after or {}).get("active_window")
        if after:
            parts.append(f"点后活动窗口：「{after}」"
                         + (f"（原「{active_before}」）"
                            if active_before and active_before != after else ""))
        elif active_before:
            parts.append(f"点后活动窗口：无（原「{active_before}」已消失）")
        return " ｜ ".join(parts)