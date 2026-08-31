@echo off
rem  The dark venue execution report for TODAY, off the realtime order server,
rem  mailed to whoever EMAIL_TO in dark_summary/local_settings.py names.
rem
rem  Double click it, or let Task Scheduler call it as
rem      run_dark_summary.cmd scheduled
rem
rem  Extra arguments are passed straight through:
rem      run_dark_summary.cmd --monthly 2026-07 --csv --raw --no-email
setlocal
set "SCRIPT=dark_summary"
call "%~dp0_run.cmd" %*
