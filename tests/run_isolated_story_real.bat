@echo off
REM ============================================================================
REM run_isolated_story.bat -- run tests/manual_windows_isolated_story.py
REM                          in an INTERACTIVE session on the guest.
REM
REM NOTE: intentionally ASCII-only (cmd.exe reads .bat in the system ANSI codepage;
REM   UTF-8 Chinese bytes can swallow the following ASCII byte and make cmd execute
REM   garbage). Chinese docs live in the .py docstring.
REM
REM Exit codes: 0 = all assertions passed, 5 = some failed, 3 = session untrusted.
REM ============================================================================

set PY=C:\Users\lf\py312\python.exe
set REPO=Z:\cc-computer-use
set OUTDIR=%REPO%\docs\PR\windows-driver\results
set LOG=%OUTDIR%\_isolated_story_real_console.log
set OUT=%OUTDIR%\isolated_story_real.json

set PYTHONPATH=%REPO%\src
set PYTHONNOUSERSITE=1

REM Reverse control: same story, but with display mode forced to real.
REM Isolation and non-isolation must give OPPOSITE conclusions -- testing only one side
REM proves nothing.
set CC_CU_DISPLAY_MODE=real

echo === RUN %DATE% %TIME% === > "%LOG%"
"%PY%" -c "import sys;print(sys.version);print(sys.executable)" >> "%LOG%" 2>&1

echo --- story --- >> "%LOG%"
"%PY%" "%REPO%\tests\manual_windows_isolated_story.py" --out "%OUT%" >> "%LOG%" 2>&1
echo EXITCODE=%errorlevel% >> "%LOG%"
echo === DONE === >> "%LOG%"
