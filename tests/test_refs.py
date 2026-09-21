"""utils/refs 单元测试：ref 注册/去重/淘汰/失效（纯 Python，无需桌面）。"""

from __future__ import annotations

from computer_use_mcp.utils.refs import RefTable


def test_register_returns_incrementing_refs():
    t = RefTable()
    a, b = object(), object()
    ra, rb = t.register(a), t.register(b)
    assert ra == 1 and rb == 2
    assert t.get_payload(ra) is a
    assert t.get_payload(rb) is b


def test_register_dedup_same_payload_returns_same_ref():
    """同一对象重复注册返回同一 ref（保证 get_ui_tree→click 间 ref 稳定）。"""
    t = RefTable()
    a = object()
    r1 = t.register(a, name="x")
    r2 = t.register(a, name="y")
    assert r1 == r2
    # meta 被更新
    assert t.get(r1).meta["name"] == "y"


def test_get_missing_returns_none():
    t = RefTable()
    assert t.get(999) is None
    assert t.get_payload(999) is None


def test_remove_and_contains():
    t = RefTable()
    r = t.register(object())
    assert r in t
    t.remove(r)
    assert r not in t
    assert t.get(r) is None


def test_clear_does_not_reuse_ref_numbers():
    """
    clear 只清空内容，**不复位 `_next`**（M-15）。

    这是防「静默串号」的回归：复位后新元素会重新拿到 ref=1、2…，而模型上下文里
    还留着上一轮的 ref —— 旧 ref 就会**指向另一个新元素**（而非报失效），
    模型据此点击会点到不相干的控件。必须保证「旧 ref 查不到」而不是「旧 ref 换了个人」。
    """
    t = RefTable()
    r1 = t.register(object())
    t.register(object())
    t.clear()
    assert len(t) == 0
    r2 = t.register(object())
    assert r2 != r1, f"clear 后不得复用旧 ref 号（旧 {r1} 又出现了），否则模型会点错元素"
    assert t.get(r1) is None, "旧 ref 必须查不到，绝不能指向新元素"


def test_eviction_when_over_capacity():
    """超过容量上限时淘汰最久未用的记录（LRU）。"""
    t = RefTable(max_entries=3)
    objs = [object() for _ in range(5)]
    refs = [t.register(o) for o in objs]
    assert len(t) == 3
    # 全程无重复使用 → LRU 序 == 插入序，最旧的两个被淘汰
    assert t.get(refs[0]) is None
    assert t.get(refs[1]) is None
    # 最新的三个仍在
    for r in refs[2:]:
        assert t.get(r) is not None


def test_eviction_is_lru_not_lowest_ref():
    """
    M-14 回归：淘汰必须按「最久未用」，不是「ref 号最小」。

    实测场景（长会话的常态）：屏幕上稳定显示的元素每轮都会被重新注册，
    a 反复被注册/取用，b 注册后就再没碰过 —— 容量满时应淘汰 b。
    历史实现只在去重命中时 update meta、不做 move_to_end，淘汰的却是 a。
    """
    t = RefTable(max_entries=3)
    a, b, c, d = object(), object(), object(), object()
    ra, rb, rc = t.register(a), t.register(b), t.register(c)
    assert ra < rb < rc

    t.register(a)   # a 是「最近用过的」（模型本轮又读到了它）
    rd = t.register(d)  # 触发一次淘汰

    assert t.get_payload(ra) is a, "刚用过（重复注册）的 ref 不该被淘汰"
    assert t.get_payload(rb) is None, "最久未用的 b 才该被淘汰"
    assert t.get_payload(rc) is c and t.get_payload(rd) is d


def test_get_counts_as_use_for_lru():
    """按 ref 取值（get/get_payload）同样算一次「使用」——模型点击走的就是这条路径。"""
    t = RefTable(max_entries=3)
    a, b, c, d = object(), object(), object(), object()
    ra, rb, _rc = t.register(a), t.register(b), t.register(c)

    t.get_payload(ra)   # 点击 ra → 记一次使用，a 变成最新
    t.register(d)       # 触发淘汰

    assert t.get_payload(ra) is a
    assert t.get_payload(rb) is None
