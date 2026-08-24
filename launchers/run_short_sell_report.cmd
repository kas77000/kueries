@echo off
rem  The short sell report for TODAY, off the realtime order server, mailed to
rem  whoever EMAIL_TO in short_sell_report/local_settings.py names.
rem
rem  Double click it, or let Task Scheduler call it as
rem      run_short_sell_report.cmd scheduled
rem
rem  Extra arguments are passed straight through:
rem      run_short_sell_report.cmd --date 2026-08-21
setlocal
set "SCRIPT=short_sell_report"
call "%~dp0_run.cmd" %*
