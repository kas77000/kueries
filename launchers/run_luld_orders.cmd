@echo off
rem  The LULD order report for TODAY, off the realtime servers, mailed to
rem  whoever EMAIL_TO in luld_orders/local_settings.py names.
rem
rem  Double click it, or let Task Scheduler call it as
rem      run_luld_orders.cmd scheduled
rem
rem  Extra arguments are passed straight through:
rem      run_luld_orders.cmd --date 2026-08-21 --no-email
setlocal
set "SCRIPT=luld_orders"
call "%~dp0_run.cmd" %*
