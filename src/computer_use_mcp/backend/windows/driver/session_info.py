"""
会话与桌面自检（backend.windows.driver.session_info）。

═══ 这个模块回答的唯一问题 ═══

**「这次测出来的数据可不可信？」**

真机 DEMO 最贵的一条教训：通过服务渠道（SSH / `VBoxManage guestcontrol` /
不带 `/IT` 的计划任务）起的进程落在 **Session 0（服务会话）**，那里**没有真桌面**。
表现是 —— UIA 根元素子级恒为 0、`PrintWindow` 返回"成功"但全黑、
`GetForegroundWindow` 拿不到窗口、全屏抓屏抛「句柄无效」……

**而且它不报任何错。** 所以任何 Windows 侧的探针/测试，第一件事都必须是
断言"我在一个有真桌面的会话里"，否则后面所有结论都是在测量环境假象。

═══ 判据 ═══

  · 会话号（`ProcessIdToSessionId`）：0 = 服务会话，1+ = 交互式会话
  · 窗口站名：交互式是 `WinSta0`，服务会话是 `Service-0x0-xxxx$`
  · UIA 根元素至少有一个子级（最强的那条正面证据——服务会话里恒为 0）

三者取「与」：任何一条不过就判不可信。宁可多报一次"环境不对"，
也不要产出一份看起来正常、实则测的是空桌面的报告。
"""

from __future__ import annotations

from . import win32


def check() -> dict:
    """
    返回会话自检报告：

        {
          "session_id": 1,            # 0 = 服务会话（不可用）
          "window_station": "WinSta0",
          "desktop": "Default",
          "interactive": True,        # ★ 总判据
          "reasons": [...],           # 判 False 时逐条说明为什么
          "dpi": {...},
        }

    `interactive=False` 时调用方**必须拒绝出结论**（尖刀脚本会以非 0 退出码结束），
    而不是"照常跑、数据自己看着办"。
    """
    pid = win32.current_process_id()
    sid = win32.session_id_of_process(pid)
    station = win32.process_window_station_name()
    desktop = win32.thread_desktop_name()

    reasons: list[str] = []
    if sid == 0:
        reasons.append(
            "会话号是 0（服务会话）—— 那里**没有真桌面**。"
            "本进程多半是被服务/SSH/非交互式计划任务拉起来的。"
        )
    if station and station != "WinSta0":
        reasons.append(f"窗口站是 {station!r} 而不是 'WinSta0' —— 不是交互式桌面。")

    interactive = not reasons
    return {
        "pid": pid,
        "session_id": sid,
        "window_station": station,
        "desktop": desktop,
        "interactive": interactive,
        "reasons": reasons,
        "dpi": win32.dpi_awareness_now(),
    }


def uia_root_child_count() -> int | dict:
    """
    根元素的直接子级数（应用级）。

    这是「交互式会话」最有力的**正面证据**：实测服务会话里它恒为 0，
    而真桌面上会列出任务栏 / Progman / 各个应用窗口。
    取不到值时返回 `{"error": ...}` 而不是 0 —— "读不到"与"是空的"必须分得开。
    """
    from . import _uia

    try:
        uia = _uia.import_uia()
        root = uia.GetRootControl()
        return len(root.GetChildren())
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
