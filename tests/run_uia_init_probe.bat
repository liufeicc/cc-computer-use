@echo off
REM ============================================================================
REM run_uia_init_probe.bat -- run tests/manual_windows_uia_init_probe.py
REM                          in an INTERACTIVE session on the guest.
REM
REM NOTE: intentionally ASCII-only (cmd.exe reads .bat in the system ANSI codepage).
REM   WHY interactive: this probe measures the same call sequence the isolation
REM   story uses (CreateDesktopW + spawn on it + first UIA touch), which is only
REM   meaningful in a real desktop session.
REM
REM Exit codes: 0 = ran through (this probe asserts nothing, it only measures).
REM ============================================================================

set PY=C:\Users\lf\py312\python.exe
set REPO=Z:\cc-computer-use
set OUTDIR=%REPO%\docs\PR\windows-driver\results
set LOG=%OUTDIR%\_uia_init_probe_console.log

set PYTHONPATH=%REPO%\src
set PYTHONNOUSERSITE=1
set RC=0

echo === RUN %DATE% %TIME% === > "%LOG%"
"%PY%" -c "import sys;print(sys.version);print(sys.executable)" >> "%LOG%" 2>&1

echo --- probe A (fresh process: clean room) --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only a >> "%LOG%" 2>&1
echo EXITCODE_A=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

echo --- probe B (fresh process: hidden desktop first) --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only b >> "%LOG%" 2>&1
echo EXITCODE_B=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

echo --- probe C (fresh process: reuse check) --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only c >> "%LOG%" 2>&1
echo EXITCODE_C=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

REM Rolled-up code for run_windows_probe.sh: it parses the LAST "EXITCODE=<n>".
REM Any nonzero = a phase crashed (this probe asserts nothing, so 0 is the norm).
echo EXITCODE=%RC% >> "%LOG%"
echo === DONE === >> "%LOG%"
