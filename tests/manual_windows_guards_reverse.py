# -*- coding: utf-8 -*-
"""反证：把 test_packaging_windows.py 里的每条守卫在**被破坏的源码**上跑一遍。

为什么需要这个：这些守卫的判据都是「没有坏东西」，而"守卫坏了"与"代码没问题"
在结果上长得一模一样（都是通过）。本项目有血证 —— 拆 mixin 那次两条守卫静默变成
空集合、全绿但零覆盖。所以每次加完守卫，都要**把坏东西种进去**看它红不红。

做法：把 packaging/ 拷到临时目录，按用例逐个打补丁，再让原测试函数在改写后的
REPO 上跑一遍，断言它**必须抛异常**。

⚠️ 这个脚本本身也会**过期**：生产代码一改，这里的「替换目标串」就可能对不上。
对不上时它会**硬失败**（不是静默跳过），提醒你同步更新 —— 否则反证会悄悄退化成
「什么都没种进去，然后全绿」。

跑法（宿主，Linux 即可，不需要客体；`manual_` 前缀故 pytest 不收集）：
    PYTHONNOUSERSITE=1 $PY tests/manual_windows_guards_reverse.py
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

import pytest

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "tests"))
sys.path.insert(0, str(_REPO / "src"))

import test_capture_scope as CS  # noqa: E402
import test_packaging_windows as G  # noqa: E402
import test_windows_driver_guards as WG  # noqa: E402

# 用例名可以写成 `模块::用例`（模块 ∈ {"packaging", "capture"}）；不写模块 = packaging。
# 为什么要有这个：本脚本原先只覆盖 `test_packaging_windows.py` 一个文件，而
# 「按窗口截图」那批守卫住在 `test_capture_scope.py` —— 不给它们做反证，
# 就是又一批"坏了也看不出来"的判据（那正是本脚本存在的理由）。
_MODULES = {"packaging": G, "capture": CS, "windows": WG}

# 每条：(用例名, 要改的文件, 找什么, 换成什么, 为什么这么种, 用例的位置参数)
CASES: list[tuple[str, str, str | None, str | None, str, tuple, bool]] = [
    (
        "test_guest_scripts_are_utf8_with_bom",
        "packaging/windows/installer.iss", None, None,
        "installer.iss 的 BOM 被去掉（Inno 会按 ANSI 读，中文变乱码）",
        (("packaging", "windows", "installer.iss"),),
        False,
    ),
    (
        "test_guest_scripts_are_utf8_with_bom",
        "packaging/windows/build-on-windows.ps1", None, None,
        "build-on-windows.ps1 的 BOM 被去掉",
        (("packaging", "windows", "build-on-windows.ps1"),),
        False,
    ),
    (
        "test_installer_deletes_runtime_files_on_uninstall",
        "packaging/windows/installer.iss",
        'Type: filesandordirs; Name: "{app}"', 'Type: files; Name: "{app}\nothing"',
        "卸载不再删 {app} 整棵树（运行期产生的 setup.log 会留下，目录不消失）",
        (),
        False,
    ),
    (
        "test_installer_uninstallrun_has_run_once_id",
        "packaging/windows/installer.iss",
        '; RunOnceId: "CcCuUnregister"', '; (no RunOnceId)',
        "[UninstallRun] 的 RunOnceId 被去掉（Inno 会告警，语义也不明确）",
        (),
        False,
    ),
    (
        "test_guest_scripts_do_not_rely_on_start_process_exit_code",
        "packaging/windows/verify-on-windows.ps1",
        "$inst = Invoke-Exe -Exe $Setup `\n"
        "    -Arguments @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-') -TimeoutSec 600",
        "$inst = Start-Process -FilePath $Setup -PassThru -Wait `\n"
        "    -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-')",
        "验收又退回 `Start-Process -PassThru` 取退出码（PS 5.1 + 重定向下恒为空）",
        (),
        False,
    ),
    (
        "test_process_runners_default_to_a_nonzero_sentinel_on_timeout",
        "packaging/windows/verify-on-windows.ps1",
        "ExitCode = if ($finished) { [int]$p.ExitCode } else { -1 }",
        "ExitCode = [int]$p.ExitCode",
        "退出码改成无条件读（进程还活着时读它会抛 InvalidOperationException）",
        (),
        False,
    ),
    (
        "test_process_runners_default_to_a_nonzero_sentinel_on_timeout",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "ExitCode = if ($finished) { [int]$p.ExitCode } else { 124 }",
        "ExitCode = if ($finished) { [int]$p.ExitCode } else { 0 }",
        "超时兜底值写成 0（一次卡住的 mcp get 会被读成「已注册」）",
        (),
        False,
    ),
    (
        "test_guest_scripts_never_start_a_ps1_directly",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "$Path.ToLower().EndsWith('.ps1')",
        "$false",
        "setup 不再对 .ps1 分流 —— Start-Process 跑不了 .ps1，注册会卡在第一步",
        (),
        False,
    ),
    (
        "test_guest_scripts_avoid_powershell_7_only_syntax",
        "packaging/windows/verify-on-windows.ps1",
        "$claudeDesc = if ($claude) { $claude } else { '(未找到)' }",
        "$claudeDesc = $claude ?? '(未找到)'",
        "验收脚本里又用了 PS 7 才有的 `??`（客体的 PS 5.1 解析不了，脚本静默不执行）",
        (("packaging", "windows", "verify-on-windows.ps1"),),
        False,
    ),
    (
        "test_windows_build_logs_uncaught_exceptions",
        "packaging/windows/build-on-windows.ps1",
        "trap {", "if ($false) {",
        "兜底 trap 被拿掉（未捕获异常会让日志断在半截、不留原因）",
        (),
        False,
    ),
    (
        "test_windows_build_does_not_redirect_both_streams_to_one_file",
        "packaging/windows/build-on-windows.ps1",
        "-RedirectStandardOutput $genOut -RedirectStandardError $genErr",
        "-RedirectStandardOutput $genOut -RedirectStandardError $genOut",
        "两个重定向又指向同一个文件（PowerShell 直接抛错，日志不留原因）",
        (),
        False,
    ),
    (
        "test_windows_build_verifies_the_python_archive_not_just_directories",
        "packaging/windows/build-on-windows.ps1",
        "'PyInstaller.utils.cliutils.archive_viewer'",
        "'PyInstaller.utils.cliutils.not_a_viewer'",
        "产物校验退回「只查 _internal 目录」（对纯 .py 的包恒判缺失，会误杀构建）",
        (),
        False,
    ),
    (
        "test_windows_build_collects_the_dynamically_imported_packages",
        "packaging/windows/build-on-windows.ps1",
        "'--collect-all', 'uiautomation'", "'--collect-submodules', 'uiautomation'",
        "--collect-all uiautomation 被降级（bin/ 下的 DLL 就收不到了）",
        (),
        False,
    ),
    (
        "test_windows_build_collects_the_dynamically_imported_packages",
        "packaging/windows/build-on-windows.ps1",
        "'--collect-submodules', 'comtypes'", "'--collect-submodules', 'comtypesX'",
        "comtypes 的收集被改坏（GetModule 是运行时生成的模块）",
        (),
        False,
    ),
    (
        "test_windows_build_collects_the_dynamically_imported_packages",
        "packaging/windows/build-on-windows.ps1",
        "'--exclude-module', 'Xlib'", "'--exclude-module', 'XlibX'",
        "Linux 专属的 Xlib 没被排除",
        (),
        False,
    ),
    (
        "test_guest_scripts_point_at_the_actual_frozen_exe_name",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "'server\\computer-use-mcp-bin.exe'", "'server\\computer-use-mcp.exe'",
        "setup 注册进 Claude Code 的入口名与产物名对不上",
        ("cc-computer-use-setup.ps1",),
        False,
    ),
    (
        "test_guest_scripts_point_at_the_actual_frozen_exe_name",
        "packaging/windows/payload/bin/cc-computer-use-doctor.ps1",
        "'computer-use-mcp-bin.exe'", "'computer-use-mcp.exe'",
        "doctor 拼的入口名与产物名对不上",
        ("cc-computer-use-doctor.ps1",),
        False,
    ),
    (
        "test_installer_uninstall_icon_matches_the_frozen_exe",
        "packaging/windows/installer.iss",
        "UninstallDisplayIcon={app}\\server\\computer-use-mcp-bin.exe",
        "UninstallDisplayIcon={app}\\server\\computer-use-mcp.exe",
        "卸载图标的 exe 名与产物名对不上",
        (),
        False,
    ),
    (
        "test_installer_registers_and_unregisters_through_the_real_script",
        "packaging/windows/installer.iss",
        '"; Flags: runhidden waituntilterminated\n\n[UninstallRun]',
        ' postinstall"; Flags: runhidden waituntilterminated\n\n[UninstallRun]',
        "[Run] 加回了 postinstall（静默安装会跳过它，验收就测不到注册了）",
        (),
        False,
    ),
    (
        "test_installer_registers_and_unregisters_through_the_real_script",
        "packaging/windows/installer.iss",
        "-Unregister", "-NoSuchFlag",
        "[UninstallRun] 不再摘注册项",
        (),
        False,
    ),
    (
        "test_installer_is_per_user",
        "packaging/windows/installer.iss",
        # ⚠️ 必须**带上后一行**才定位得到那条真指令：文件里 `PrivilegesRequired=lowest`
        #    在**注释**里也出现过一次（第 17 行讲设计理由那段）。不带锚点的话
        #    `replace(..., 1)` 会命中注释那处，等于什么都没种 —— 正是 _live_lines
        #    存在的理由（注释里写一句「本该如此」挡不住任何事）。
        "PrivilegesRequired=lowest\nOutputDir=",
        "PrivilegesRequired=admin\nOutputDir=",
        "改成了需要管理员（会弹 UAC，静默验收走不通）",
        (),
        False,
    ),
    (
        "test_setup_and_doctor_search_the_same_claude_paths_in_the_same_order",
        "packaging/windows/payload/bin/cc-computer-use-doctor.ps1",
        "            (Join-Path $env:APPDATA 'npm\\claude.cmd')\n"
        "            (Join-Path $env:LOCALAPPDATA 'Programs\\claude\\claude.exe')",
        "            (Join-Path $env:LOCALAPPDATA 'Programs\\claude\\claude.exe')\n"
        "            (Join-Path $env:APPDATA 'npm\\claude.cmd')",
        "doctor 的 claude 候选表顺序与 setup 不一致（两边会给出互相打架的结论）",
        (),
        False,
    ),
    (
        "test_build_windows_sh_does_not_wipe_the_whole_dist",
        "packaging/windows/build-windows.sh",
        'mkdir -p "$OUT" "$TOOLS"', 'rm -rf dist\nmkdir -p "$OUT" "$TOOLS"',
        "总入口写回了 rm -rf dist（会抹掉 .deb 与组装目录）",
        (),
        False,
    ),
    (
        "test_build_windows_sh_never_reads_a_password_from_a_file",
        "packaging/windows/build-windows.sh",
        '--exe "$exe" --username "$VM_USER" --password "$VM_PASS" \\',
        '--exe "$exe" --username "$VM_USER" \\',
        "某一处 guestcontrol 漏了 --password（报错会像「密码错」，实为变量空）",
        (),
        False,
    ),
    (
        "test_setup_reports_outcome_by_exit_code_not_by_optimism",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "if ($add.ExitCode -eq 0) {", "if ($true) {",
        "注册结果不再按退出码判断（退化成无条件报成功）",
        (),
        False,
    ),
    (
        "test_setup_is_idempotent_and_has_unregister",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "@('mcp', 'get', $McpName)", "@('mcp', 'list')",
        "注册前不再先探一次（不幂等，每次都会动用户配置）",
        (),
        # ⚠️ 必须**全局替换**：这个探测在脚本里出现两次（注册路径与卸载路径各一次），
        #    只换第一处的话，守卫仍然能在另一处找到它 —— 那是**正确的**判断
        #    （两处都是合法用法），不能算漏。要种的是「整个探测被拿掉」。
        True,
    ),
    (
        "test_setup_recognizes_the_shell_prerequisite_error",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "if ($Text -match 'requires either Git for Windows') {",
        "if ($false) {",
        "运行期不再识别「Claude Code 拒绝执行」那条报错（注释里那份留着）",
        (),
        False,
    ),
    (
        "test_setup_recognizes_the_shell_prerequisite_error",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "Write-Warn '    · Git for Windows: https://git-scm.com/downloads/win'",
        "Write-Warn '    · 装个 Git 吧'",
        "认出了报错但不给下载地址（用户不知道去哪装）",
        (),
        False,
    ),
    (
        "test_guest_scripts_point_claude_at_a_bash",
        "packaging/windows/verify-on-windows.ps1",
        "$psi.EnvironmentVariables['CLAUDE_CODE_GIT_BASH_PATH'] = $GitBash",
        "$null = $GitBash",
        "找到了 bash 却不交给 claude（没设 CLAUDE_CODE_GIT_BASH_PATH）",
        (("packaging", "windows", "verify-on-windows.ps1"),),
        False,
    ),
    (
        "test_guest_scripts_point_claude_at_a_bash",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "function Find-GitBash {",
        "function Find-NoSuchThing {",
        "setup 不再去找 bash（claude 必然拒绝执行，而报错指向别处）",
        (("packaging", "windows", "payload", "bin", "cc-computer-use-setup.ps1"),),
        False,
    ),
    (
        "test_setup_and_doctor_and_verify_search_the_same_git_bash_paths",
        "packaging/windows/payload/bin/cc-computer-use-doctor.ps1",
        "            (Join-Path $env:LOCALAPPDATA 'Programs\\Git\\bin\\bash.exe')\n"
        "            (Join-Path $env:ProgramFiles 'Git\\bin\\bash.exe')",
        "            (Join-Path $env:ProgramFiles 'Git\\bin\\bash.exe')\n"
        "            (Join-Path $env:LOCALAPPDATA 'Programs\\Git\\bin\\bash.exe')",
        "doctor 的 bash 候选表顺序与 setup 不一致（两边结论会打架）",
        (),
        False,
    ),
    (
        "test_guest_scripts_do_not_use_try_as_an_expression",
        "packaging/windows/payload/bin/cc-computer-use-doctor.ps1",
        "    $outText = ''\n"
        "    try { $outText = [string]$tOut.Result } catch { }\n"
        "    $errText = ''\n"
        "    try { $errText = [string]$tErr.Result } catch { }",
        "    $outText = (try { [string]$tOut.Result } catch { '' })\n"
        "    $errText = (try { [string]$tErr.Result } catch { '' })",
        "又把 try 当表达式用（PS 7 语法，客体 5.1 连解析都过不了）",
        (),
        False,
    ),
    (
        "test_guest_scripts_do_not_assign_to_powershell_automatic_variables",
        "packaging/windows/verify-on-windows.ps1",
        "    param([string]$Exe, [string[]]$Arguments, [int]$TimeoutSec = 30, [string]$GitBash = '')",
        "    param([string]$Exe, [string[]]$Args, [int]$TimeoutSec = 30, [string]$GitBash = '')",
        "参数命名成自动变量 $Args（PowerShell 静默忽略它，命令行会是空的）",
        (),
        False,
    ),
    (
        "test_claude_health_check_subcommands_get_a_generous_timeout",
        "packaging/windows/verify-on-windows.ps1",
        "-Arguments @('mcp', 'list') -TimeoutSec 120 -GitBash $bash",
        "-Arguments @('mcp', 'list') -TimeoutSec 30 -GitBash $bash",
        "健康检查类子命令的超时被改小（冷启动冻结产物 >30s，会偶发假红）",
        (),
        False,
    ),
    (
        "test_claude_health_check_subcommands_get_a_generous_timeout",
        "packaging/windows/payload/bin/cc-computer-use-setup.ps1",
        "-Arguments @('mcp', 'get', $McpName) -TimeoutSec 120",
        "-Arguments @('mcp', 'get', $McpName) -TimeoutSec 20",
        "setup 的注册前探测超时被改小（超时=判定「没注册」，可能反复去改用户配置）",
        (),
        # ⚠️ 全局替换：同一个探测在注册与卸载两条路径各写了一遍，只换第一处
        #    守卫仍能在另一处找到合规值 —— 那**不是**漏，要种的是「整个都改小」。
        True,
    ),
]


# ══════════════════════════════════════════════════════════════════════════
# 运行期反证：直接在**已导入的生产对象**上种坏东西
#
# ═══ 为什么不能沿用上面那套「拷源码 → 改文件 → 跑守卫」 ═══
#
# 上面那套只对**读源码文本**的守卫有效（`test_packaging_windows.py` 全是那种）。
# 而「按窗口截图」这批守卫是**行为型**的：它们 import 生产模块、调它的函数、看返回值。
# 那种情况下把源码拷到临时目录再改，**改的根本不是同一个对象** —— 守卫照样绿，
# 反证却"通过"了。那比没有反证更坏：它会让人以为这件事已经被证明过。
#
# 所以这里换成：破坏方在内存里把生产对象换掉 → 跑守卫 → 断言它红 → 还原。
# ⚠️ 诚实说明其局限：对"返回文案"这类守卫，破坏方是**包一层**把文案掐掉，
# 而不是把生产代码改坏。它证明的是「该判据对文案缺失敏感」，不是「生产代码
# 改坏了会被发现」——后者由上面那套源码替换覆盖（那里是直接改被测文件）。
# ══════════════════════════════════════════════════════════════════════════

def _runtime_cases():
    """返回 `[(破坏函数, 用例名, 位置参数), …]`；破坏函数自己往 undo 里挂还原动作。"""
    def break_win_default_scope(undo):  # noqa: ANN001
        from computer_use_mcp.backend.windows.backend import WindowsBackend
        old = WindowsBackend.DEFAULT_CAPTURE_SCOPE
        WindowsBackend.DEFAULT_CAPTURE_SCOPE = "screen"
        undo.append(lambda: setattr(WindowsBackend, "DEFAULT_CAPTURE_SCOPE", old))

    def break_remembered_target(undo):  # noqa: ANN001
        from computer_use_mcp.core.coordinator.base import CoordinatorBase
        old = CoordinatorBase._remember_target_window
        CoordinatorBase._remember_target_window = lambda self, wid: None
        undo.append(lambda: setattr(CoordinatorBase, "_remember_target_window", old))

    def break_foreground_notice(undo):  # noqa: ANN001
        from computer_use_mcp.core.coordinator.base import CoordinatorBase
        old = CoordinatorBase._capture_target

        def no_notice(self, scope="auto", window_id=None, app=None):  # noqa: ANN001
            mode, wid, _note = old(self, scope, window_id, app)
            return mode, wid, ""          # ← 掐掉"退用了用户前台窗口"那句挑明
        CoordinatorBase._capture_target = no_notice
        undo.append(lambda: setattr(CoordinatorBase, "_capture_target", old))

    def break_unknown_app_raise(undo):  # noqa: ANN001
        from computer_use_mcp.core.coordinator.base import CoordinatorBase
        old = CoordinatorBase._capture_target

        def no_raise(self, scope="auto", window_id=None, app=None):  # noqa: ANN001
            try:
                return old(self, scope, window_id, app)
            except Exception:             # ← 找不到应用时悄悄退回"最近的窗口/前台"
                return "window", self._target_window_id or "0xffff", ""
        CoordinatorBase._capture_target = no_raise
        undo.append(lambda: setattr(CoordinatorBase, "_capture_target", old))

    def break_capture_supported_hasattr(undo):  # noqa: ANN001
        from computer_use_mcp.core.coordinator import screens as scr
        old = scr.ScreenMixin._window_capture_supported
        scr.ScreenMixin._window_capture_supported = lambda self: hasattr(
            self.backend, "capture_window")     # ← 基类有默认实现，恒为真
        undo.append(lambda: setattr(scr.ScreenMixin, "_window_capture_supported", old))

    def break_black_threshold(undo):  # noqa: ANN001
        from computer_use_mcp.backend.windows.driver import capture as cap
        old = cap._BLACK_FRACTION_FAIL
        cap._BLACK_FRACTION_FAIL = 0.5
        undo.append(lambda: setattr(cap, "_BLACK_FRACTION_FAIL", old))

    def break_window_for_app_pick(undo):  # noqa: ANN001
        from computer_use_mcp.backend.windows.backend import WindowsBackend
        old = WindowsBackend.window_for_app

        def first_with_area(self, app):  # noqa: ANN001
            from computer_use_mcp.backend.windows.driver import win32
            needle = (app or "").strip().lower()
            for hwnd in win32.enum_top_level_windows():
                base = (win32.process_basename(hwnd) or "").lower()
                title = (win32.window_text(hwnd) or "").lower()
                if needle in base or needle in title:
                    return f"0x{hwnd:x}"        # ← 取第一个，不挑面积最大的
            return None
        WindowsBackend.window_for_app = first_with_area
        undo.append(lambda: setattr(WindowsBackend, "window_for_app", old))

    def break_mode_parse(undo):  # noqa: ANN001
        from computer_use_mcp.backend.windows.driver import desktop
        old = desktop.display_mode
        desktop.display_mode = lambda: (
            "real" if (os.environ.get("CC_CU_DISPLAY_MODE") or "").strip().lower()
            != "isolated" else "isolated")   # ← 反了：拼错/未设都落到 real
        undo.append(lambda: setattr(desktop, "display_mode", old))

    def break_isolated_foreground(undo):  # noqa: ANN001
        from computer_use_mcp.backend.windows.driver import apps
        old = apps._target_foreground
        apps._target_foreground = lambda: __import__(
            "computer_use_mcp.backend.windows.driver.win32",
            fromlist=["x"]).foreground_window()   # ← 隔离时也返回用户桌面的前台
        undo.append(lambda: setattr(apps, "_target_foreground", old))

    def break_enumerate_target(undo):  # noqa: ANN001
        from computer_use_mcp.backend.windows.driver import desktop
        old = desktop.enumerate_target
        desktop.enumerate_target = lambda visible_only=True: __import__(
            "computer_use_mcp.backend.windows.driver.win32",
            fromlist=["x"]).enum_top_level_windows(visible_only=visible_only)
        undo.append(lambda: setattr(desktop, "enumerate_target", old))

    return [
        (break_win_default_scope, "capture::test_default_capture_scope_is_a_platform_difference", ()),
        (break_mode_parse, "windows::test_display_mode_defaults_to_isolated_and_only_recognises_real", None),
        (break_isolated_foreground, "windows::test_isolated_foreground_is_never_the_users_window", None),
        (break_enumerate_target, "windows::test_isolated_window_enumeration_uses_the_desktop_api", None),
        (break_remembered_target, "capture::test_recently_touched_window_beats_the_foreground", ()),
        (break_foreground_notice, "capture::test_foreground_fallback_is_loud_not_silent", ()),
        (break_unknown_app_raise, "capture::test_unknown_app_raises_instead_of_silently_using_another_window", ()),
        (break_capture_supported_hasattr, "capture::test_window_capture_supported_is_decided_by_override_not_hasattr", ()),
        (break_black_threshold, "windows::test_black_frame_is_refused_but_a_dark_theme_is_accepted", ()),
        # args=None ⇒ 这条用例要 `monkeypatch` 夹具，由 harness 现造一个传进去。
        # ⚠️ 不这么写的话它会以 `TypeError: missing 1 required positional argument`
        #    收场 —— 那**看起来也是"被拦下"**，于是反证假通过（本轮实测踩到）。
        (break_window_for_app_pick, "windows::test_window_for_app_picks_the_largest_window", None),
    ]


def _split(case: str) -> tuple[str, str]:
    """`"capture::test_x"` → `("capture", "test_x")`；没有前缀则默认 packaging。"""
    if "::" in case:
        mod, name = case.split("::", 1)
        return mod, name
    return "packaging", case


def main() -> int:
    original = {k: getattr(m, "REPO", None) for k, m in _MODULES.items()}
    bad = 0
    for i, (case, relpath, old, new, why, args, replace_all) in enumerate(CASES, 1):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            shutil.copytree(_REPO / "packaging", tmp / "packaging")
            target = tmp / relpath
            raw = target.read_bytes()

            if old is None:
                assert raw.startswith(b"\xef\xbb\xbf"), f"[{i}] {relpath} 本来就没有 BOM，反证无效"
                target.write_bytes(raw[3:])
            else:
                text = raw.decode("utf-8")
                assert old in text, (
                    f"[{i}] 反证样本**过期**：在 {relpath} 里找不到\n  {old!r}\n"
                    f"生产代码已经改了 —— 请同步更新本脚本（否则这条反证会静默退化成空转）"
                )
                target.write_text(text.replace(old, new) if replace_all
                                  else text.replace(old, new, 1), encoding="utf-8")

            for _m in _MODULES.values():
                if hasattr(_m, "REPO"):
                    _m.REPO = str(tmp)
            _mod, _name = _split(case)
            try:
                getattr(_MODULES[_mod], _name)(*args)
            except AssertionError as exc:
                first = (str(exc).splitlines() or [""])[0][:88] or "(裸断言，无消息)"
                print(f"  ✅ [{i}] {why}\n        → 被拦下：{first}")
            except Exception as exc:  # noqa: BLE001
                # 抛别的异常也算"发现了问题"，但打印类型便于分辨
                print(f"  ✅ [{i}] {why}\n        → 报错（{type(exc).__name__}）：{exc}")
            else:
                bad += 1
                print(f"  ❌ [{i}] {why}\n        → **守卫没拦住**：{case} 在被破坏的源码上仍然通过")
            finally:
                for _k, _v in original.items():
                    if _v is not None:
                        _MODULES[_k].REPO = _v

    # ── 运行期那批（行为型守卫）────────────────────────────────────────
    rt = _runtime_cases()
    for j, (breaker, case, args) in enumerate(rt, 1):
        _mod, _name = _split(case)
        undo: list = []
        try:
            breaker(undo)
            call_args = args
            if args is None:
                # `None` ⇒ 这条用例需要 `monkeypatch` 夹具（pytest 之外没有现成的，
                # 自己造一个，并记进 undo —— 不还原会污染后面所有用例）。
                mp = pytest.MonkeyPatch()
                undo.append(mp.undo)
                call_args = (mp,)
            try:
                getattr(_MODULES[_mod], _name)(*call_args)
            except AssertionError as exc:
                first = (str(exc).splitlines() or [""])[0][:88] or "(裸断言，无消息)"
                print(f"  ✅ [R{j}] {case}\n        → 被拦下：{first}")
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001
                # ⚠️ 这里必须捕获 `BaseException` 而**不只是** `Exception`：
                #    `pytest.raises` 在"没抛出来"时抛的是 `_pytest.outcomes.Failed`，
                #    而它继承自 `BaseException` —— 只抓 Exception 会让这一条**直接
                #    把脚本打崩**（实测踩到），于是后面几条反证根本没跑就被判失败。
                #
                # ★ 但"拦下"只认**判据本身**变红（`AssertionError` 或 pytest 的
                #   `Failed`）。别的异常类型（`AttributeError` / `TypeError` …）说明
                #   **破坏方自己写坏了**，不是守卫在起作作用 —— 那种"报错"看起来
                #   和"被拦下"一模一样，正是本脚本存在的理由所要防的假通过。
                #   （本轮实测踩到过两次：夹具没传、monkeypatch 打空。）
                if isinstance(exc, AssertionError) or type(exc).__name__ == "Failed":
                    first = (str(exc).splitlines() or [""])[0][:88] or "(裸断言，无消息)"
                    print(f"  ✅ [R{j}] {case}\n        → 被拦下：{first}")
                else:
                    bad += 1
                    import traceback
                    tb = traceback.format_exc().strip().splitlines()[-4:]
                    print(f"  ❌ [R{j}] {case} —— **破坏方自己坏了**："
                          f"{type(exc).__name__}: {str(exc)[:110]}\n"
                          f"        （这不是守卫在起作用；把它当通过就是假通过）\n"
                          + "\n".join(f"        {ln}" for ln in tb))
            else:
                bad += 1
                print(f"  ❌ [R{j}] {case} —— 种了坏东西，守卫**仍然通过**")
        finally:
            for fn in reversed(undo):
                fn()

    total = len(CASES) + len(rt)
    print()
    if bad:
        print(f"❌ {bad}/{total} 条反证失败 —— 这些守卫起不到作用")
        return 1
    print(f"✅ 全部 {total} 条反证通过（每条守卫都真的抓得到它该抓的东西）"
          f"（源码替换 {len(CASES)} 条 + 运行期 {len(rt)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
