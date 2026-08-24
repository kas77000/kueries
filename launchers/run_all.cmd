@echo off
rem  Both daily reports, one after the other rather than at once - they read the
rem  same order server, and a report that is late is better than two that fought
rem  over the same process.
rem
rem  The exit code is the WORST of the two, so one failure is still a failure
rem  even when the other one worked.
setlocal
set "WORST=0"

call "%~dp0run_luld_orders.cmd" %*
if errorlevel 1 set "WORST=%ERRORLEVEL%"

call "%~dp0run_short_sell_report.cmd" %*
if errorlevel 1 set "WORST=%ERRORLEVEL%"

exit /b %WORST%
