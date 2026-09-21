"""
ref 失效重定位与元素级 set_value（拆分自 test_optimizations.py）。

两条主线都属于「宁可让模型重新感知，也不猜」这条原则：
ref 重定位命中多个必须拒绝；set_value 清不掉原值时必须拒绝（否则会**追加**而不是替换）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from computer_use_mcp.backend.base import Backend, QueryResult, Rect, TextBlock
from computer_use_mcp.backend.linux import inject as inject_mod
from computer_use_mcp.backend.linux.inject import XdotoolInjector
from computer_use_mcp.core import display as display_mod
from computer_use_mcp.core import screen_lock
from computer_use_mcp.core.coordinator import Coordinator
from computer_use_mcp.utils.errors import (
    LEVEL_ELEMENT,
    BackendUnavailableError,
    ComputerUseError,
    InjectionError,
    InvalidRefError,
    SandboxUnavailableError,
    ScreenBusyError,
    to_friendly_text,
)

# 注：拆分（2026-09-18）后各文件共用**同一份 import 头**，其中未用到的名字无害。
# 统一的好处是不会漏项——按需裁剪时漏掉一个 import，报错点会离真正的原因很远。

from _helpers import _StubBackend, _proc, _reset_display  # noqa: E402




# ---------- ref 失效重定位：命中多个必须拒绝，唯一命中必须回报（I-1）----------
# 桩说明：_StubBackend.is_alive 默认对非 TextBlock 返回 False，故 _native_alive 会把任意
# 非 TextBlock 判定为「已失效」——正好用来驱动重定位路径（I-10 之前是靠 element_info
# raise 间接达成同一效果；改成 ABC 上的显式表态后语义更清楚）。
def _register_dead_ref(coord, **meta) -> int:
    """注册一个「已失效」的 payload，返回其 ref。"""
    return coord.refs.register(object(), **meta)





def test_relocation_refuses_when_ambiguous():
    """
    回归（I-1）：按 (role,name,app) 重定位命中 ≥2 个候选时必须**拒绝**，绝不猜。

    为什么：重定位的触发条件是「原 native 已死」= 界面已经变了，而调用方的决策基于旧
    界面。而 (role,name,app) 是**弱身份**——「确定」「保存」在每个对话框里都同名，
    limit=1 会静默选中第一个（可能是另一个窗口里的另一个按钮），随后 invoke 成功、
    回报「元素级 do_action 成功」，正是本项目最忌讳的「打偏了却报成功」。
    """
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    ref = _register_dead_ref(coord, role="push button", name="确定", app="gedit")
    seen: dict = {}

    def fake_find(**kw):
        seen.update(kw)
        return QueryResult(items=[object(), object()])   # 2 个候选 = 身份不唯一

    backend.find = fake_find

    with pytest.raises(InvalidRefError) as ei:
        coord.click(ref=ref)

    assert "不猜" in str(ei.value)
    assert seen.get("limit") == 2, "必须用 limit=2 才能识别出「不唯一」"
    assert backend.calls == [], "拒绝时绝不能真的去点击"





def test_relocation_no_match_raises():
    """失效且找不到任何候选 → InvalidRefError（提示重新感知）。"""
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    ref = _register_dead_ref(coord, role="push button", name="确定", app="gedit")
    backend.find = lambda **kw: QueryResult()

    with pytest.raises(InvalidRefError) as ei:
        coord.click(ref=ref)
    assert "无法重定位" in str(ei.value)





def test_relocation_single_match_passes_but_flags_the_model():
    """
    唯一命中时放行，但**必须**把「已重定位到新元素」回报给模型。

    已知残留（本次未覆盖）：「恰好唯一匹配、但目标已变」挡不住——同 app 的另一个对话框
    里也可能只有一个「确定」。所以唯一命中时只能放行，但必须让模型知道并核对结果。
    """
    backend = _StubBackend()
    coord = Coordinator(backend=backend)
    ref = _register_dead_ref(coord, role="push button", name="确定", app="gedit")
    target = object()
    backend.find = lambda **kw: QueryResult(items=[target])
    invoked: list = []
    backend.invoke = lambda n, action=None: (invoked.append(n), True)[1]

    res = coord.click(ref=ref)

    assert res.ok and res.level == LEVEL_ELEMENT
    assert "已失效" in res.message and "可能已不是同一个目标" in res.message, \
        f"唯一命中也要回报重定位，实际消息：{res.message}"
    assert invoked == [target], "唯一命中应放行，且动作落在重定位到的目标上"





def test_alive_ref_never_relocates_nor_flags():
    """ref 仍有效时不得走重定位、不得带任何重定位备注（防止把正常路径也拦下来）。"""
    backend = _StubBackend()
    backend.is_alive = lambda n: True            # 桩改为「仍有效」
    coord = Coordinator(backend=backend)
    ref = _register_dead_ref(coord, role="push button", name="确定", app="gedit")
    called: list = []
    backend.find = lambda **kw: (called.append(kw), QueryResult())[1]

    res = coord.click(ref=ref)

    assert res.ok and res.level == LEVEL_ELEMENT
    assert called == [], "ref 有效时不该去 find（既无必要，也是遍历成本）"
    assert "已失效" not in res.message





# ---------- set_value 路径 2：必须真清空（替换），绝不拼成「新+旧」（I-2）----------
class _FakeEditable:
    """模拟 AT-SPI EditableText：只提供路径 2 所需接口（无 set_text_contents）。"""

    def __init__(self, content: str = "", delete_works: bool = True,
                 count_readable: bool = True) -> None:
        self.content = content
        self.delete_works = delete_works
        self.count_readable = count_readable

    def get_character_count(self) -> int:
        if not self.count_readable:
            raise RuntimeError("count 不可读")
        return len(self.content)

    def delete_text(self, start: int, end: int) -> None:
        if not self.delete_works:
            raise RuntimeError("delete_text 失败")
        self.content = self.content[:start] + self.content[end:]

    def insert_text(self, pos: int, text: str, length: int) -> bool:
        self.content = self.content[:pos] + text + self.content[pos:]
        return True





class _FakeReplaceableEditable(_FakeEditable):
    """额外提供 set_text_contents —— 路径 1 可用。"""

    def set_text_contents(self, text: str) -> bool:
        self.content = text
        return True





def _reader():
    from computer_use_mcp.backend.linux.atspi import AtspiReader
    return AtspiReader()





def test_set_value_path2_replaces_instead_of_concatenating():
    """
    回归（I-2）：路径 2 必须**真清空**，不能把新文本插到旧内容前面拼成「新+旧」。

    原实现只 `set_caret_offset(0)` 就 insert_text，旧内容根本没删，却无条件 return True；
    coordinator 据此回报「元素级 set_value 成功」，模型以为替换成功——静默的数据错误。
    """
    entry = _FakeEditable(content="旧内容")

    assert _reader().set_value(entry, "新内容") is True

    assert entry.content == "新内容", f"必须是替换而非拼接，实际={entry.content!r}"





def test_set_value_prefers_replace_api_when_available():
    """路径 1（set_text_contents）可用时必须优先走它。"""
    entry = _FakeReplaceableEditable(content="旧内容")

    assert _reader().set_value(entry, "新内容") is True

    assert entry.content == "新内容"





def test_set_value_refuses_when_it_cannot_clear():
    """清不掉就一个字都不改、返回 False 交兜底——宁可不做，也不拼成「新+旧」。"""
    entry = _FakeEditable(content="旧内容", delete_works=False)

    assert _reader().set_value(entry, "新内容") is False

    assert entry.content == "旧内容", "清空失败时不得写入任何东西"





def test_set_value_refuses_when_result_unverifiable():
    """无法校验结果（读不到字符数）→ 返回 False，不上报可能错的成功。"""
    entry = _FakeEditable(content="旧内容", count_readable=False)

    assert _reader().set_value(entry, "新内容") is False

    assert entry.content == "旧内容", "无法确认就不改（不猜）"
