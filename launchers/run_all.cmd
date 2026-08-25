@echo off
rem  Both daily reports, one after the other rather than at once - they read the
rem  same order server, and a report that is late is better than two that fought
rem  over the same process.
rem
rem  The exit code is the WORST of the two, so one failure is still a failure
rem  even when the other one worked.
rem
rem  DOUBLE CLICKED, the window waits ONCE, at the end, rather than after each
rem  report: `nopause` goes to the children and the hold happens here, so both
rem  reports run through without anyone sitting between them pressing a key.
rem  Scroll back for the first report's output - it is still all on screen.
rem
rem  Called with `scheduled` it forwards that instead, and then nothing pauses
rem  and nothing prints: each report writes its own log, as it does on its own.
setlocal EnableExtensions
set "WORST=0"

rem  Any argument being `scheduled` decides it; the word can sit anywhere on the
rem  line, the same way _run.cmd reads it.
set "HOLD=1"
for %%A in (%*) do if /i "%%~A"=="scheduled" set "HOLD="
if defined HOLD (set "PASS=nopause") else (set "PASS=")

call "%~dp0run_luld_orders.cmd" %PASS% %*
if errorlevel 1 set "WORST=%ERRORLEVEL%"

call "%~dp0run_short_sell_report.cmd" %PASS% %*
if errorlevel 1 set "WORST=%ERRORLEVEL%"

if defined HOLD (
    echo(
    if "%WORST%"=="0" (
        echo   both reports done.
    ) else (
        echo   at least one report FAILED - worst exit code %WORST%.
        echo   Scroll up: each report printed its own result above.
    )
    echo(
    pause
)

exit /b %WORST%
