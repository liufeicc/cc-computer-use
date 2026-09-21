"""core/serializer 单元测试：去噪/flatten/interactive_only/截断/ref 分配（无需桌面）。"""

from __future__ import annotations

from computer_use_mcp.backend.base import Rect, UINode
from computer_use_mcp.core.serializer import TreeSerializer, is_actionable, is_noise
from computer_use_mcp.utils.refs import RefTable


def _node(role, name="", actions=None, native=None, children=None):
    return UINode(role=role, name=name, actions=actions or [], native=native,
                  rect=Rect(0, 0, 10, 10), children=children or [])


def _sample_tree():
    """
    构造一棵含噪声的样例树：
      window|Dialog
        panel(空名,噪声)
          push button|Yes (click)   ← 应分配 ref，被 flatten 上提
        label|Question               ← 有名字非噪声，输出
    """
    btn = _node("push button", "Yes", ["click"], native=object())
    panel = _node("panel", "", native=object(), children=[btn])
    label = _node("label", "Question", native=object())
    win = _node("window", "Dialog", native=object(), children=[panel, label])
    return [win], btn


def test_noise_detection():
    assert is_noise(_node("panel", "")) is True
    assert is_noise(_node("panel", "Named")) is False
    assert is_noise(_node("push button", "", ["click"])) is False  # 有动作不算噪声


def test_actionable_detection():
    assert is_actionable(_node("push button", "x")) is True       # 角色可操作
    assert is_actionable(_node("label", "x", ["click"])) is True  # 有动作
    assert is_actionable(_node("window", "x")) is False


def test_serialize_denoise_and_flatten():
    """空 panel 不单独成行，其子按钮被上提；按钮拿到 ref。"""
    nodes, btn = _sample_tree()
    t = RefTable()
    s = TreeSerializer(t)
    text = s.serialize(nodes)
    # panel 不应作为独立行出现（无名字的纯噪声）
    assert "panel" not in text
    # 窗口与按钮、标签都应在
    assert "window | Dialog" in text
    assert "push button | Yes" in text
    assert "label | Question" in text
    # 按钮分配了 ref 且注册进表
    assert btn.ref is not None
    assert t.get_payload(btn.ref) is btn.native


def test_serialize_ref_format():
    """输出含 [ref] 前缀。"""
    nodes, btn = _sample_tree()
    s = TreeSerializer(RefTable())
    text = s.serialize(nodes)
    assert f"[{btn.ref}] push button | Yes (click)" in text


def test_interactive_only_filters_non_actionable():
    """interactive_only 只保留可操作节点（按钮），过滤 window/label。"""
    nodes, btn = _sample_tree()
    s = TreeSerializer(RefTable())
    text = s.serialize(nodes, interactive_only=True)
    assert "push button | Yes" in text
    assert "label | Question" not in text
    assert "window | Dialog" not in text


def test_truncation_notice():
    """超过 max_nodes 追加截断提示，不静默截断。"""
    children = [_node("push button", f"b{i}", ["click"], native=object()) for i in range(20)]
    root = _node("window", "W", native=object(), children=children)
    s = TreeSerializer(RefTable())
    text = s.serialize([root], max_nodes=5)
    assert "截断" in text or "max_nodes" in text


def test_empty_tree_message():
    s = TreeSerializer(RefTable())
    text = s.serialize([])
    assert "空" in text


def test_empty_but_truncated_blames_max_nodes_not_a11y():
    """
    M-11 回归：空结果不得把截断提示顶掉。

    实测复现：`max_nodes=0` 时后端照常返回了元素，只是序列化器一个都输出不了，
    而历史实现无条件写「可能无障碍开关未开…可改用 screenshot」—— 模型拿到的是
    **方向相反**的诊断（以为没有元素树），被推向 screenshot 那条贵得多的路，
    正解却只是把 max_nodes 调大。
    """
    nodes, _btn = _sample_tree()
    text = TreeSerializer(RefTable()).serialize(nodes, max_nodes=0)
    assert "max_nodes" in text, f"空结果必须说明是 max_nodes 太小，实际：{text!r}"
    # 判据是「有没有把模型推向截图」，不是「出现没出现 screenshot 这个词」
    # ——正解文案里恰好有一句否定式「先别改用 screenshot」。
    assert "可改用 screenshot" not in text, f"不得把模型推向截图（这里根本不缺元素树）：{text!r}"
    assert "别改用 screenshot" in text, f"应显式拦住「没元素树」这个错误结论：{text!r}"


def test_empty_with_interactive_only_suggests_disabling_it():
    """
    M-11 回归（第二种成因）：interactive_only 把树过滤光了 ≠ 该应用没有元素树。

    正解是关掉过滤再读一次整棵树，历史文案却统一指向「无障碍开关未开」。
    """
    label = _node("label", "Question", native=object())  # 有名字但不可操作
    text = TreeSerializer(RefTable()).serialize([label], interactive_only=True)
    assert "interactive_only=False" in text, f"应提示关掉过滤再读：{text!r}"


def test_exactly_filling_max_nodes_is_not_truncation():
    """
    M-12 回归：节点数**恰好等于** max_nodes 时不得误报截断（一个节点都没丢）。

    历史判据是「输出之后 emitted >= max_nodes」，属安全但会误导的假阳性——
    模型会白白缩小 scope 多调一次。
    """
    kids = [_node("push button", f"b{i}", ["click"], native=object()) for i in range(4)]
    root = _node("window", "W", native=object(), children=kids)
    # 共 5 个节点（root + 4 kids）
    text = TreeSerializer(RefTable()).serialize([root], max_nodes=5)
    assert "截断" not in text, f"恰好填满不该报截断：{text!r}"
    assert len(text.splitlines()) == 5


def test_truncation_reported_only_when_a_node_is_dropped():
    """M-12 反向：确实丢掉节点时必须报截断（别把修复做成「永不报」）。"""
    kids = [_node("push button", f"b{i}", ["click"], native=object()) for i in range(4)]
    root = _node("window", "W", native=object(), children=kids)
    text = TreeSerializer(RefTable()).serialize([root], max_nodes=4)
    assert "截断" in text and "max_nodes=4" in text


def test_no_truncation_when_only_filtered_nodes_remain():
    """
    M-12 的精确性：预算用尽后剩下的若全是**本就不会输出**的噪声节点，不算截断。

    这正是不把判据放回函数开头的原因 —— 开头判「emitted 到顶了就截断」，
    会在一个噪声节点上误报（那个节点本来也不会出现在输出里）。
    """
    kids = [_node("push button", f"b{i}", ["click"], native=object()) for i in range(2)]
    root = _node("window", "W", native=object(), children=kids)
    noise = _node("panel", "", native=object())  # 无名无动作 → 噪声，不入输出
    text = TreeSerializer(RefTable()).serialize([root, noise], max_nodes=3)
    assert "截断" not in text, f"只遇到噪声节点不该报截断：{text!r}"


def test_long_name_truncated():
    long = "x" * 200
    nodes = [_node("window", long, native=object())]
    text = TreeSerializer(RefTable()).serialize(nodes)
    assert "..." in text
    assert len(long) > 60  # 确属长名
