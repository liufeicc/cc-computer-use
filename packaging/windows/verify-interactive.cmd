@echo off
rem ============================================================================
rem Entry point for the installer acceptance run on the guest (launched by a
rem scheduled task with /IT, i.e. as the interactive session user).
rem
rem WHY this extra .cmd layer instead of pointing the task straight at the .ps1:
rem   `/tr "<command line>"` nests a quoted path inside another quoted string and
rem   cmd eats the inner quotes. Measured: it reports
rem   `invalid argument/option - '-NoProfile'` and the task never gets created.
rem   With a single space-free .cmd path in /tr the nesting problem disappears.
rem
rem Parameters are passed via a FILE (written here, paths patched by the wrapper):
rem   Z:\cc-computer-use\dist\windows\verify-params.txt
rem   contents like: Setup=...\xx-setup.exe  /  Result=...\verify-result.txt
rem
rem WARNING: THIS FILE MUST STAY PURE ASCII. cmd.exe reads batch files in the
rem   system ANSI codepage, so UTF-8 bytes get mis-decoded and can swallow the
rem   following byte (a mangled lead byte can even eat the newline, which would
rem   fold the powershell line below into this comment). Guarded by
rem   test_guest_batch_files_are_pure_ascii. Chinese notes for this file live in
rem   verify-on-windows.ps1 (a BOM'd .ps1 can hold them safely).
rem
rem WARNING: output MUST go to a file. The task's window is hidden, so if
rem   PowerShell fails to PARSE (measured: the script used the PS7-only `??`
rem   operator while the guest ships PS 5.1) the error vanishes into the void --
rem   the only symptom is "the result file never appeared".
rem ============================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "Z:\cc-computer-use\packaging\windows\verify-on-windows.ps1" -ParamsFile "Z:\cc-computer-use\dist\windows\verify-params.txt" > "Z:\cc-computer-use\dist\windows\verify-interactive.log" 2>&1
