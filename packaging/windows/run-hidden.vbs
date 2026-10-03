' ============================================================================
' run-hidden.vbs -- run a command with a HIDDEN window and propagate its exit code.
'
' WHY THIS EXISTS
'   The scheduled task in run-scheduled-task.ps1 runs a .bat, and a .bat always
'   gets a console window. Task Scheduler (InteractiveToken) cannot suppress it,
'   and that console becomes the foreground window of the USER'S DESKTOP.
'
'   That is a test-harness artifact with real cost: the isolation story asserts
'   "the user's foreground window did not change" while our own console is
'   appearing and grabbing focus, so the check goes red for a reason that has
'   nothing to do with the product. Measured 2026-10-02: a plain run passed with
'   the foreground unchanged, while the very next run of identical code failed
'   with 'Program Manager' -> 'C:\WINDOWS\SYSTEM32\cmd.exe' -- the console title
'   of the batch we ourselves launched. (The literal titles are in the guard's
'   docstring, not here: this file must stay pure ASCII.)
'
'   wscript.exe is windowless (unlike cscript.exe, which allocates a console),
'   and WScript.Shell.Run's second argument 0 = SW_HIDE. So the batch and every
'   console its children would inherit stay off the user's screen entirely.
'
' WScript.Quit propagates the child's exit code, which is what the host scripts
' look for (`EXITCODE=<n>` is written by the .bat itself, but a nonzero wrapper
' exit is still a useful belt-and-braces signal in the scheduler log).
'
' ASCII ONLY -- keep it that way, same reason as the .bat files.
' ============================================================================

Option Explicit

Dim shell, target, code

If WScript.Arguments.Count < 1 Then
    WScript.Echo "usage: wscript.exe run-hidden.vbs <command>"
    WScript.Quit 2
End If

target = WScript.Arguments(0)
Set shell = CreateObject("WScript.Shell")

' 0 = hidden window, True = wait for it to finish (so the task's runtime, and
' therefore the host's "task is still running" observation, stays meaningful).
code = shell.Run(target, 0, True)

WScript.Quit code
