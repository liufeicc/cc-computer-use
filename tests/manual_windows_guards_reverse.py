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

import pathlib
import shutil
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve()
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "tests"))
sys.path.insert(0, str(_REPO / "src"))

import test_packaging_windows as G  # noqa: E402

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


def main() -> int:
    original_repo = G.REPO
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

            G.REPO = str(tmp)
            try:
                getattr(G, case)(*args)
            except AssertionError as exc:
                print(f"  ✅ [{i}] {why}\n        → 被拦下：{str(exc).splitlines()[0][:88]}")
            except Exception as exc:  # noqa: BLE001
                # 抛别的异常也算"发现了问题"，但打印类型便于分辨
                print(f"  ✅ [{i}] {why}\n        → 报错（{type(exc).__name__}）：{exc}")
            else:
                bad += 1
                print(f"  ❌ [{i}] {why}\n        → **守卫没拦住**：{case} 在被破坏的源码上仍然通过")
            finally:
                G.REPO = original_repo

    print()
    if bad:
        print(f"❌ {bad}/{len(CASES)} 条反证失败 —— 这些守卫起不到作用")
        return 1
    print(f"✅ 全部 {len(CASES)} 条反证通过（每条守卫都真的抓得到它该抓的东西）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
