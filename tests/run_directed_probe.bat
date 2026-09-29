@echo off
REM ============================================================================
REM run_directed_probe.bat -- run tests/manual_windows_directed_probe.py
REM                           in an INTERACTIVE session on the guest.
REM
REM NOTE: this file is intentionally ASCII-only.
REM   cmd.exe reads .bat in the system ANSI codepage (GBK on a zh-CN box). A UTF-8
REM   Chinese comment is 3 bytes per char, and some GBK lead bytes swallow the
REM   following ASCII byte -- which can eat a quote or a REM and make cmd try to
REM   execute garbage. Chinese docs live in the .py docstring instead.
REM
REM WHY a .bat rather than `cmd /c "long command"`: nested quoting through
REM   VBoxManage guestcontrol gets eaten one level. See windows-demo README #8.
REM
REM WHY the interactive session: in Session 0 (service session) the UIA root has
REM   zero children and PrintWindow returns black -- and NOTHING reports an error.
REM   The probe detects this and exits 3, but that wastes a whole run.
REM
REM Exit codes come from the .py: 0 = all assertions passed, 5 = some failed,
REM   3 = session untrusted. Log goes to the results dir (UTF-8, written by Python).
REM ============================================================================

set PY=C:\Users\lf\py312\python.exe
set REPO=Z:\cc-computer-use
set OUTDIR=%REPO%\docs\PR\windows-driver\results
set LOG=%OUTDIR%\_directed_probe_console.log
set OUT=%OUTDIR%\directed_probe.json

set PYTHONPATH=%REPO%\src
set PYTHONNOUSERSITE=1

echo === RUN %DATE% %TIME% === > "%LOG%"
"%PY%" -c "import sys;print(sys.version);print(sys.executable)" >> "%LOG%" 2>&1

echo --- probe --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_directed_probe.py" --out "%OUT%" >> "%LOG%" 2>&1
echo EXITCODE=%errorlevel% >> "%LOG%"
echo === DONE === >> "%LOG%"
