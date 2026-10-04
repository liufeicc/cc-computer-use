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
REM
REM OUTCOME, ROUND 1 (2026-10-03, five fresh processes):
REM   A      clean room                  first UIA 4.69s
REM   B0     story order                 first ControlFromHandle 2.86s
REM   B-idle 90s idle first              first ControlFromHandle 1.68s
REM   B-work 90s churn first             VOID -- no window appeared, so the UIA
REM                                      call never ran (it printed a fake 0.38s;
REM                                      fixed by polling for the window now)
REM   D1     clean room + DPI V2 first   HUNG, and the whole guest then froze
REM The hang froze the ENTIRE guest: identical screenshots across 3 minutes and
REM guestcontrol could not even start a session. Restart the guest first.
REM
REM OUTCOME, ROUND 2 (after the guest was hard-restarted, same .bat, EXITCODE=0):
REM   A      3.01s     B0  1.24s     B-idle  0.99s     B-work  VOID again     D1  1.23s
REM Two leads this retires:
REM   * DPI V2 is INNOCENT -- the same single-variable D1 froze the guest in round 1
REM     and took 1.23s here (2.76s on 2026-09-30). "2 fast, 1 hung, and the hung
REM     one froze the whole guest" reads as "that guest was dying", not "DPI V2".
REM   * the first UIA touch is SECOND-scale on a healthy guest (B0 1.24s). So
REM     --prime-uia can only ever paper over a tail that shows up as the whole
REM     guest slowing down (a bare `import typing_extensions` has hung here).
REM B-work was VOID again in round 2, but for a DIFFERENT reason -- the probe's own
REM race: its churn loop kills winver.exe BY IMAGE NAME from a fire-and-forget
REM thread, which can land after phase B spawned its own and kill that too. Fixed
REM by joining those threads at the end of _work().
REM
REM ROUND 3 -- the join fix is NOT yet verified. A re-run after that fix was
REM interrupted from the outside (guest powered off) at B-idle, and B-work is the
REM ONLY phase that exercises the fix, so it never got its turn. The two numbers it
REM did produce (A 2.30s, B0 0.34s -- B0 the fastest yet) are consistent with the
REM rest, but the batch did not finish, so do not use it as a full comparison round.
REM WHEN YOU NEXT RUN THIS: check the B-work line FIRST. If it still says
REM `ERR ... no window` then the join did not fix it and something ELSE is killing
REM winver -- go look for it instead of re-reading this comment.
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
