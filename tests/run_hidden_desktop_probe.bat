@echo off
REM ============================================================================
REM run_hidden_desktop_probe.bat -- run tests/manual_windows_hidden_desktop_probe.py
REM                                in an INTERACTIVE session on the guest.
REM
REM NOTE: this file is intentionally ASCII-only (cmd.exe reads .bat in the system
REM   ANSI codepage; UTF-8 Chinese bytes can swallow the following ASCII byte and
REM   make cmd execute garbage). Chinese docs live in the .py docstring.
REM
REM WHY the interactive session: in Session 0 the UIA root has zero children and
REM   PrintWindow returns black -- and NOTHING reports an error. The probe detects
REM   this and exits 3, but that wastes a whole run.
REM
REM Exit codes: 0 = all assertions passed, 5 = some failed, 3 = session untrusted.
REM ============================================================================

set PY=C:\Users\lf\py312\python.exe
set REPO=Z:\cc-computer-use
set OUTDIR=%REPO%\docs\PR\windows-driver\results
set LOG=%OUTDIR%\_hidden_desktop_console.log
set OUT=%OUTDIR%\hidden_desktop_probe.json

set PYTHONPATH=%REPO%\src
set PYTHONNOUSERSITE=1

echo === RUN %DATE% %TIME% === > "%LOG%"
"%PY%" -c "import sys;print(sys.version);print(sys.executable)" >> "%LOG%" 2>&1

echo --- probe --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_hidden_desktop_probe.py" --out "%OUT%" >> "%LOG%" 2>&1
echo EXITCODE=%errorlevel% >> "%LOG%"
echo === DONE === >> "%LOG%"
