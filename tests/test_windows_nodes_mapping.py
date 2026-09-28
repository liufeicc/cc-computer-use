"""
UIA 角色映射的守卫（可在 **Linux** 上跑）。

═══ 为什么这一层值得单独守 ═══

`backend/windows/driver/nodes.py` 把 UIA 的 `ControlTypeName` 翻译成
**AT-SPI 那套角色词表**，好让 `core/serializer.py` 一行都不用改。

翻译**错了不会报错**：只会让某类控件不再落在 `INTERACTIVE_ROLES` /
`ACTIONABLE_ROLES` 里 → 不分配 ref、被 `interactive_only` 滤掉 →
表现是「模型找不到那个按钮」，而排查时完全看不出是角色翻译的问题。

而它**恰好是纯逻辑**：喂假元素就能在 Linux 上把整张表量一遍，不需要 Windows。
所以这里不测"在真机上能不能读到"，只测"翻译规则对不对"。
"""

from __future__ import annotations

import pytest

from computer_use_mcp.backend.base import ACTIONABLE_ROLES, INTERACTIVE_ROLES, NOISE_ROLES
from computer_use_mcp.backend.windows.driver import nodes


# ── 假元素 ──────────────────────────────────────────────────────────────

class _Fake:
    """一个可控的假 UIA 元素：属性由 kwargs 给，没给的一律 `getattr` 失败。"""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Boom:
    """任何属性访问都抛异常 —— 用来验"某个 Pattern 探测失败不该拖垮整份动作表"。"""

    def __getattr__(self, name):  # noqa: ANN001
        raise RuntimeError(f"模拟跨进程调用失败：{name}")


# ── ★ 判据 ①：ClassName 必须优先于 ControlType ───────────────────────────

def test_class_name_wins_over_control_type():
    """
    真机 DEMO 的血证：**同一个「按钮」，经典 Win32 应用（走 MSAA 桥）报 `Pane`，
    UWP 原生 provider 报 `Button`**。

    若按 ControlType 优先，那个 `Pane` 会被翻译成 `"panel"` —— 它在 `NOISE_ROLES` 里，
    无名字时**直接被 serializer 丢掉**，按钮就从树上消失了。
    故判据是"类名能压过类型"，而不是"某个控件能翻译对"。
    """
    # 经典 Win32 按钮：ControlType 退化成 Pane，但类名是 Button
    assert nodes.role_of("PaneControl", "Button") == "push button"
    # 反向：类名认不出时才该退回 ControlType
    assert nodes.role_of("ButtonControl", "Microsoft.UI.Xaml.Controls.Button") == "push button"
    # 但一个真正的 Pane 不该被误判成按钮
    assert nodes.role_of("PaneControl", "Windows.UI.Core.CoreWindow") == "panel"


def test_known_classic_control_classes_are_mapped():
    """经典 Win32 控件类名逐个钉住（它们是"ControlType 会退化"的那批，最需要确定）。"""
    cases = {
        "Button": "push button",
        "Static": "label",
        "Edit": "entry",
        "RichEditD2DPT": "text",      # Win11 记事本的编辑控件（实测类名）
        "SysListView32": "list",
        "SysTreeView32": "tree",
        "ComboBox": "combo box",
        "msctls_trackbar32": "slider",
        "#32770": "dialog",           # winver 那个对话框
    }
    for cls, expect in cases.items():
        assert nodes.role_of(None, cls) == expect, f"{cls} 应翻译成 {expect}"
    # 大小写不敏感（UIA 的 ClassName 大小写不保证）
    assert nodes.role_of(None, "bUtToN") == "push button"


# ── ★ 判据 ②：可操作控件的角色必须真的落在 ACTIONABLE_ROLES 里 ────────────

def test_actionable_control_types_land_in_actionable_roles():
    """
    这是**行为判据**，不是拼写判据 —— 它拦的是"翻译出了一个没人认识的词"。

    `ACTIONABLE_ROLES` 决定节点要不要分配 ref。一个 `ButtonControl` 若被翻成
    比如 `"pushbutton"`（少个空格），它就不在集合里 → 按钮拿不到 ref →
    模型只能退到截图那条最贵的路。而这件事**不会报任何错**。

    ⚠️ 用真机实测过的 `ControlTypeName` 取值（见 `docs/PR/windows-demo/`），
    不是照着想象写的。
    """
    actionable_types = [
        "ButtonControl", "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
        "EditControl", "ListItemControl", "MenuItemControl", "TreeItemControl",
        "TabItemControl", "HyperlinkControl", "SliderControl", "SpinnerControl",
        "ScrollBarControl", "DataItemControl",
    ]
    for ct in actionable_types:
        role = nodes.role_of(ct, None)
        assert role in ACTIONABLE_ROLES, (
            f"{ct} 翻译成了 {role!r}，它不在 ACTIONABLE_ROLES 里 —— "
            f"这类控件将拿不到 ref（模型只能退到截图那条路），且不会有任何报错"
        )

    # 正控：确保上面那个断言不是恒真（`ACTIONABLE_ROLES` 非空、且集合判定有效）
    assert ACTIONABLE_ROLES and "push button" in ACTIONABLE_ROLES
    assert "definitely-not-a-role" not in ACTIONABLE_ROLES


def test_every_mapped_role_is_in_the_documented_vocabulary():
    """
    ★ 映射表产出的每个角色都必须在**已登记的词表**里 —— 这条拦的是**拼写错误**。

    为什么要专门拦拼写：`nodes.py` 里一个手滑（`"pushbuton"` 少个空格、`"page tab list"`
    写成 `"page tablist"`）**不会有任何报错**，只会让那类控件静默地从三个集合里掉出去——
       · 不在 `ACTIONABLE_ROLES` → 不分配 ref；
       · 不在 `INTERACTIVE_ROLES` → `atspi.find()` 那条 `or role in INTERACTIVE_ROLES`
         判据不成立，于是**搜不到**（`find` 的 interactive_only 路径）；
       · 不在 `NOISE_ROLES` → 无名字时也不会被当噪声丢掉，反而以"未知角色"留在树里。
    三个后果都指向同一个症状："模型找不到那个控件"，而看不出是翻译的问题。

    ⚠️ 本用例**不**要求"所有角色都在前三个集合里"——那是我第一版写错的判据。
    `core/serializer.py` 的真实语义是：
      · `is_actionable` = 有动作 **或** 角色在 `ACTIONABLE_ROLES`；
      · `is_noise` = 无名 **且** 无动作 **且** 角色在 `NOISE_ROLES`；
      · 两者都不是 → **照样显示**（有名字就显示，只是不分 ref）。
    所以 `image` / `label` / `dialog` 这类"有内容但不可操作"的角色**本来就该在集合外**，
    AT-SPI 侧也一样。真正该守的是"每个值都是**有意识选定的**"，而不是"都落在某个集合里"。
    """
    produced = set(nodes._CLASS_ROLES.values()) | set(nodes._CONTROL_TYPE_ROLES.values())
    in_sets = produced & (ACTIONABLE_ROLES | INTERACTIVE_ROLES | NOISE_ROLES)
    outside = produced - in_sets

    # 集合外的那些必须逐个登记（下面这张表就是"有意识选定"的凭证）
    registered_outside = {
        # —— 容器/结构：是承载体，不是可操作对象 ——
        "dialog", "frame", "menu bar", "tool bar", "status bar",
        # —— 展示型：有内容，但用户不能对它"操作" ——
        "progress bar", "tool tip", "image", "separator", "calendar",
        # —— 表格/标签页的表头与页签容器（叶子格子在 ACTIONABLE 里）——
        "table column header", "page tab list",
    }
    assert outside == registered_outside, (
        f"映射表产出了未登记的角色：{sorted(outside - registered_outside)}\n"
        f"（若是有意新增的内容/容器角色，请连同理由加进 registered_outside；"
        f"若是拼写错误，请修映射表。）\n"
        f"另有已登记却不该出现的：{sorted(registered_outside - outside)}"
    )

    # 正控：词表与映射都非空，且确实存在"集合内"的角色 ——
    # 否则上面那句 `outside == registered_outside` 可能因为两边都是空集而恒真。
    assert produced and in_sets, "映射表或角色集合是空的，本判据已失去意义"
    assert "push button" in in_sets


def test_container_roles_stay_out_of_actionable_set():
    """
    容器类角色**不该**混进 `ACTIONABLE_ROLES` —— 混进去会让每个 panel 都分到一个 ref，
    把树撑大、把真正可点的东西淹没（token 与可读性双输）。
    """
    for ct in ("PaneControl", "GroupControl", "TitleBarControl", "ImageControl"):
        role = nodes.role_of(ct, None)
        assert role not in ACTIONABLE_ROLES, f"{ct} → {role!r} 不该是可操作角色"
    # 正控：不是"所有角色都不在集合里"才让上面恒真
    assert nodes.role_of("ButtonControl", None) in ACTIONABLE_ROLES


def test_unknown_control_type_degrades_to_unknown_role():
    """
    认不出的类型 → `"unknown"`，而它**必须在 `NOISE_ROLES` 里**。

    这条看着像废话，实则是故意设计的降级：不认识的东西不该出现在给模型的树里
    （无名字时会被 serializer 丢掉）。若 `"unknown"` 掉出 `NOISE_ROLES`，
    整棵树会突然多出大量说不清是什么的节点。
    """
    assert nodes.role_of("SomethingBrandNewControl", "NoSuchClass") == "unknown"
    assert "unknown" in NOISE_ROLES


# ── 判据 ③：动作表 ──────────────────────────────────────────────────────

def test_actions_reflect_supported_patterns():
    el = _Fake(IsInvokePatternAvailable=True, IsKeyboardFocusable=True)
    acts = nodes.actions_of(el)
    assert nodes.ACTION_INVOKE in acts
    assert nodes.ACTION_FOCUS in acts
    assert nodes.ACTION_SELECT not in acts, "没声明 SelectionItem 却给出了 select"


def test_expand_implies_collapse():
    """同一个 Pattern 的反向操作要一起给 —— 只给 expand 的话模型无法收起一个展开的节点。"""
    el = _Fake(IsExpandCollapsePatternAvailable=True)
    acts = nodes.actions_of(el)
    assert nodes.ACTION_EXPAND in acts and nodes.ACTION_COLLAPSE in acts


def test_one_failing_pattern_does_not_lose_the_others():
    """
    ⚠️ 某一次 Pattern 探测失败不该让整个元素丢掉动作表。

    UIA 是跨进程调用：目标应用忙、元素刚失效，单次 `getattr` 抛异常是**常态**。
    不兜住的表现是「这个按钮不能点」——而实际上只是那一下没读到。
    """
    class _Partial:
        IsInvokePatternAvailable = True

        def __getattr__(self, name):  # noqa: ANN001
            # 除 invoke 外一律炸
            raise RuntimeError("模拟元素刚失效")

    acts = nodes.actions_of(_Partial())
    assert nodes.ACTION_INVOKE in acts, "一个属性炸了就把已探测到的动作也丢了"


def test_actions_of_all_failing_element_is_empty_not_raising():
    """全炸 → 空表，**不抛**。抛出来会把"读不到动作"升级成"整个工具调用失败"。"""
    assert nodes.actions_of(_Boom()) == []


# ── 判据 ④：状态与值 ─────────────────────────────────────────────────────

def test_states_report_facts_not_judgements():
    """
    状态要报**事实**（disabled / offscreen / focused），不要替模型下结论。

    `IsEnabled=False` → `"disabled"`：注意是取反后报的，因为 UIA 的字段是
    "是否可用"，而人读的是"是否被禁用"。
    """
    el = _Fake(IsEnabled=False, IsOffscreen=True, HasKeyboardFocus=True)
    st = nodes.states_of(el)
    assert "disabled" in st and "offscreen" in st and "focused" in st

    ok = _Fake(IsEnabled=True, IsOffscreen=False, HasKeyboardFocus=False)
    assert nodes.states_of(ok) == [], "正常状态的元素不该报任何状态噪声"

    assert nodes.states_of(_Boom()) == []


def test_value_none_is_distinct_from_empty_string():
    """
    「没有值」与「值是空字符串」必须分得开。

    模型据此判断"这个输入框是空的"还是"这个控件根本没有值"。返回空串会把前者
    伪装成后者 —— 本项目最忌讳的那类静默。
    """
    assert nodes.value_of(_Boom()) is None
    assert nodes.value_of(_Fake(Name="")) == "", "有名字但为空串时应当如实返回空串"


def test_rect_of_accepts_both_shapes():
    """
    `BoundingRectangle` 的两种形状都要认（具名元组 `Rect` 与普通序列）。

    换库版本时退化形状不会报错，只会让 `rect_of` 返回 None ——
    然后被当成"这个元素没有矩形"，把**库的问题**记成**应用的问题**。
    """
    class _Rect:
        left, top, right, bottom = 10, 20, 110, 220

    assert nodes.rect_of(_Fake(BoundingRectangle=_Rect())) == (10, 20, 110, 220)
    assert nodes.rect_of(_Fake(BoundingRectangle=(1, 2, 3, 4))) == (1, 2, 3, 4)
    assert nodes.rect_of(_Fake(BoundingRectangle=None)) is None
    assert nodes.rect_of(_Boom()) is None


@pytest.mark.parametrize("bad", ["", None, "   "])
def test_empty_class_name_does_not_shadow_control_type(bad):
    """空/空白类名不该把 ControlType 的路堵死（`_CLASS_ROLES` 里没有空串键，但要确认）。"""
    assert nodes.role_of("ButtonControl", bad) == "push button"