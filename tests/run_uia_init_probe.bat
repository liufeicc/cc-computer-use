@echo off
REM ============================================================================
REM run_uia_init_probe.bat -- run tests/manual_windows_uia_init_probe.py
REM                          in an INTERACTIVE session on the guest.
REM
REM NOTE: intentionally ASCII-only (cmd.exe reads .bat in the system ANSI codepage).
REM
REM Each phase runs in a FRESH process on purpose: keeping the UIA singleton cold
REM is the whole point of phase B -- if an earlier phase warms it up, B measures
REM nothing. See the .py module docstring.
REM
REM Exit codes: 0 = all phases ran (this probe asserts nothing, it only measures).
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

echo --- A: clean room (first UIA touch is here) --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only a >> "%LOG%" 2>&1
echo EXITCODE_A=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

echo --- B0: hidden desktop + app, no warm-up --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only b >> "%LOG%" 2>&1
echo EXITCODE_B0=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

echo --- B-idle: 90s idle first (process age only) --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only b --idle-before 90 >> "%LOG%" 2>&1
echo EXITCODE_BIDLE=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

echo --- B-work: 90s of story-like process churn first --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only b --work-before 90 >> "%LOG%" 2>&1
echo EXITCODE_BWORK=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

echo --- D1: clean room, dpi V2 pre-set (story does this) --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_uia_init_probe.py" --only a --set-dpi >> "%LOG%" 2>&1
echo EXITCODE_D1=%errorlevel% >> "%LOG%"
if errorlevel 1 set RC=1

REM Rolled-up code for run_windows_probe.sh: it parses the LAST "EXITCODE=<n>".
echo EXITCODE=%RC% >> "%LOG%"
echo === DONE === >> "%LOG%"
