@echo off
REM ============================================================================
REM run_iso_ab.bat -- A/B REPEAT for the "hang at first UIA touch" investigation.
REM
REM Runs the isolation story twice WITHOUT --prime-uia and twice WITH it, back to
REM back, same machine and same session.
REM
REM WHY repeat: a single primed run not hanging proves nothing (one observation is
REM not evidence). We need a repeatable difference, otherwise "priming fixes it"
REM is luck wearing a conclusion's clothes.
REM
REM OUTCOME OF THE 2026-10-02 RUN -- read this before trusting the output:
REM   The question is STILL UNANSWERED, because NO round ever reached the priming
REM   step. Three of four rounds hung, two of them during IMPORT (before main()),
REM   which is before priming is even reachable:
REM     plain1  hung in CoCreateInstance(CUIAutomation)      (the tree read)
REM     prime1  hung in module-level platform.system()       (import time, WMI/COM)
REM     plain2  hung importing typing_extensions             (import time)
REM     prime2  INVALID -- a half-applied edit of src/ broke that one run
REM   So do not read a "prime failed again" out of this. Re-run it on a day when
REM   the guest is behaving, and record the hang points again; they move.
REM
REM Each run gets its OWN directory (plain1/prime1/plain2/prime2) because the story
REM writes its trace as `_isolated_story_trace.log` NEXT TO --out -- a fixed name.
REM Sharing one directory would make each run overwrite the previous run's trace,
REM and the trace is the whole point when a run hangs.
REM
REM The rollup line `EXITCODE=<sum>` exists because run_windows_probe.sh reads the
REM guest's verdict from the log text (a scheduled task's exit code is not
REM retrievable). Summing keeps it straight-line: every run returns 0 on success,
REM so a nonzero sum means at least one run failed.
REM
REM Straight-line only: no CALL subroutines, no IF blocks. An earlier version used
REM them and produced no output at all, which cost a full wait cycle.
REM
REM ASCII only (cmd.exe reads .bat in the system ANSI codepage).
REM ============================================================================

set PY=C:\Users\lf\py312\python.exe
set REPO=Z:\cc-computer-use
set T=%REPO%\tests\manual_windows_isolated_story.py
set L=C:\ccu_runs
set D=%REPO%\docs\PR\windows-driver\results

if not exist "%L%" mkdir "%L%"
if not exist "%L%\plain1" mkdir "%L%\plain1"
if not exist "%L%\prime1" mkdir "%L%\prime1"
if not exist "%L%\plain2" mkdir "%L%\plain2"
if not exist "%L%\prime2" mkdir "%L%\prime2"
set PYTHONPATH=%REPO%\src
set PYTHONNOUSERSITE=1
set RC=0

echo === RUN %DATE% %TIME% === > "%L%\ab.txt"
"%PY%" -c "import sys;print(sys.version);print(sys.executable)" >> "%L%\ab.txt" 2>&1

echo %TIME% plain1-start >> "%L%\ab.txt"
taskkill /F /IM winver.exe > NUL 2>&1
"%PY%" "%T%" --out "%L%\plain1\result.json" > "%L%\plain1\console.log" 2>&1
echo %TIME% plain1-exit=%errorlevel% >> "%L%\ab.txt"
set /a RC=RC+%errorlevel%

echo %TIME% prime1-start >> "%L%\ab.txt"
taskkill /F /IM winver.exe > NUL 2>&1
"%PY%" "%T%" --prime-uia --out "%L%\prime1\result.json" > "%L%\prime1\console.log" 2>&1
echo %TIME% prime1-exit=%errorlevel% >> "%L%\ab.txt"
set /a RC=RC+%errorlevel%

echo %TIME% plain2-start >> "%L%\ab.txt"
taskkill /F /IM winver.exe > NUL 2>&1
"%PY%" "%T%" --out "%L%\plain2\result.json" > "%L%\plain2\console.log" 2>&1
echo %TIME% plain2-exit=%errorlevel% >> "%L%\ab.txt"
set /a RC=RC+%errorlevel%

echo %TIME% prime2-start >> "%L%\ab.txt"
taskkill /F /IM winver.exe > NUL 2>&1
"%PY%" "%T%" --prime-uia --out "%L%\prime2\result.json" > "%L%\prime2\console.log" 2>&1
echo %TIME% prime2-exit=%errorlevel% >> "%L%\ab.txt"
set /a RC=RC+%errorlevel%

echo %TIME% all-runs-finished >> "%L%\ab.txt"
echo EXITCODE=%RC% >> "%L%\ab.txt"
echo === DONE === >> "%L%\ab.txt"

REM The small verdict file goes first so the host's poll can stop here; the bulk
REM copy of per-run logs is best-effort and must not gate the verdict.
copy /Y "%L%\ab.txt" "%D%\_iso_ab_result.txt" > NUL 2>&1
xcopy /E /I /Y "%L%" "%D%\_iso_ab" > NUL 2>&1
