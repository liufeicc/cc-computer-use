"""
树序列化层（core/serializer）—— 省 token 的核心。

职责：把 backend 产出的 UINode 森林转成「紧凑、去噪、带 ref」的文本树返回给 LLM。

省 token 手段：
  1. 去噪：无名字、无动作的纯装饰节点（filler/空 panel/section）不单独成行，
     其子节点直接上提（flatten），避免大量缩进空壳。
  2. 紧凑格式：`[ref] role | name (actions)`，缩进表层级，无冗余字段。
  3. interactive_only：只保留可操作节点（有 ref 的），大幅压缩。
  4. max_nodes 截断并明确提示剩余数量（绝不静默截断）。

ref 分配：
  - 对「可操作」节点（角色属 ACTIONABLE_ROLES 或有 actions）分配整数 ref，
    并用 RefTable 把 ref 映射到节点的 native 活对象，供后续 click/type 直接定位。
"""

from __future__ import annotations

from ..backend.base import UINode
from ..backend.linux.atspi import ACTIONABLE_ROLES, NOISE_ROLES
from ..utils.refs import RefTable


def is_actionable(node: UINode) -> bool:
    """节点是否「可操作」（值得分配 ref）。"""
    return bool(node.actions) or node.role in ACTIONABLE_ROLES


def is_noise(node: UINode) -> bool:
    """节点是否纯噪声（无名字、无动作、角色属噪声集）。"""
    return (not node.name) and (not node.actions) and node.role in NOISE_ROLES


def _format_line(node: UINode, depth: int) -> str:
    """格式化单行：缩进 + [ref] + role + | name + (actions)。"""
    indent = "  " * depth
    parts = [indent]
    if node.ref is not None:
        parts.append(f"[{node.ref}] ")
    parts.append(node.role or "?")
    if node.name:
        # 名字过长截断，省 token
        nm = node.name if len(node.name) <= 60 else node.name[:57] + "..."
        parts.append(f" | {nm}")
    if node.actions:
        acts = ",".join(a for a in node.actions if a != "?")
        if acts:
            parts.append(f" ({acts})")
    return "".join(parts)


class TreeSerializer:
    """把 UINode 森林序列化为紧凑文本，同时把可操作节点注册进 RefTable。"""

    def __init__(self, ref_table: RefTable) -> None:
        self.refs = ref_table

    def serialize(
        self, nodes: list[UINode], interactive_only: bool = False,
        max_nodes: int = 400, register_refs: bool = True,
    ) -> str:
        """
        序列化入口。

        实现逻辑：
          1. 深度优先遍历，维护已输出行数（≤ max_nodes）。
          2. 对每个节点先决定「是否输出本行」：
             - interactive_only=True 时，仅输出可操作节点；
             - 否则输出所有非噪声节点（噪声节点跳过本行但仍递归子节点，实现 flatten）。
          3. 输出前若 register_refs 且节点可操作，注册 native → 得到 ref，写入 node.ref。
          4. 超过 max_nodes 停止，并在末尾追加截断提示（含被截断的剩余估计）。
        """
        out: list[str] = []
        state = {"emitted": 0, "truncated": False}
        for root in nodes:
            self._walk(root, 0, interactive_only, max_nodes, register_refs, out, state)
            if state["truncated"]:
                break
        text = "\n".join(out)
        if state["truncated"]:
            text += f"\n... (已达 max_nodes={max_nodes} 上限并截断；可缩小 scope 或用 find_element 精确定位)"
        if not out:
            # M-11：空结果的文案必须**分因**给出。历史实现是无条件覆盖式赋值，会把上面
            # 刚写好的截断提示整体顶掉 —— 且顶成的是一个**方向相反**的结论（「无障碍开关
            # 未开」），把模型推向 screenshot 那条昂贵得多的路，而正解往往只是把参数调大。
            text = _empty_hint(interactive_only, state["truncated"], max_nodes)
        return text

    def _walk(
        self, node: UINode, depth: int, interactive_only: bool, max_nodes: int,
        register_refs: bool, out: list[str], state: dict,
    ) -> None:
        actionable = is_actionable(node)
        # 注册 ref（可操作节点）
        if register_refs and actionable and node.native is not None and node.ref is None:
            node.ref = self.refs.register(
                node.native, role=node.role, name=node.name, app=node.app,
            )

        # 决定是否输出本行
        if interactive_only:
            emit = actionable
        else:
            emit = not is_noise(node)

        if emit:
            # M-12：截断判据必须落在「**本该输出却被上限挡下**」这一刻。历史实现是在输出
            # **之后**判 `emitted >= max_nodes`，于是「恰好填满、后面一个节点都没丢」也会报
            # 截断（已实测复现）。假阳性看着无害，实际会诱导模型白白缩小 scope、多调一次。
            if state["emitted"] >= max_nodes:
                state["truncated"] = True
                return
            out.append(_format_line(node, depth))
            state["emitted"] += 1
            child_depth = depth + 1
        else:
            # 噪声/被过滤节点：不输出本行，子节点上提到同层（flatten），省缩进
            child_depth = depth

        for child in node.children:
            self._walk(child, child_depth, interactive_only, max_nodes, register_refs, out, state)
            if state["truncated"]:
                return


def _empty_hint(interactive_only: bool, truncated: bool, max_nodes: int) -> str:
    """
    空结果时给出**方向正确**的下一步（M-11）。

    三种成因的区别是本质的，混为一谈会把模型引向错误的排查方向：
      - 被上限挡下：不是「没有元素」，而是「你没让它输出」——调大 max_nodes 即可；
      - 被 interactive_only 过滤光：树很可能是好的，只是没有可操作节点——关掉过滤再读；
      - 真的什么都没有：才轮到怀疑无障碍开关/元素树，最后才用 screenshot 兜底。
    """
    if truncated:
        return (
            f"(空 + 被截断：max_nodes={max_nodes} 太小，一个节点都来不及输出。"
            f"把它调大（如 300~400）再读一次——**这不等于「没有元素树」**，先别改用 screenshot)"
        )
    if interactive_only:
        return (
            "(空：当前范围内没有**可操作**节点。这不等于该应用没有元素树——"
            "先用 interactive_only=False 再读一次整棵树；仍为空才考虑无障碍开关未开，"
            "最后才用 screenshot 兜底)"
        )
    return "(空：未找到符合条件的节点。可能无障碍开关未开，或该应用无元素树——可改用 screenshot 兜底)"
