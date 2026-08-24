@echo off
rem =============================================================================
rem  _run.cmd - the one launcher.  Every run_*.cmd beside it sets SCRIPT and
rem  calls this; nothing else here knows which report it is running.
rem
rem  TWO MODES, picked by the first word of the command line:
rem
rem    (nothing)   DOUBLE CLICKED.  The output stays on screen and the window
rem                waits at the end, so a traceback can actually be read.
rem    scheduled   TASK SCHEDULER.  There is no console to print to, so
rem                everything goes to logs\<script>_<stamp>.log and the exit
rem                code is passed back - that is what the scheduler shows as
rem                Last Run Result.
rem
rem  Anything else on the line is handed to the python script untouched:
rem
rem    run_luld_orders.cmd --date 2026-08-21
rem    run_luld_orders.cmd scheduled --no-email
rem
rem  The python to use and where the logs go come from local_settings.cmd
rem  beside this file, which git ignores.  See README.md.
rem =============================================================================

setlocal EnableExtensions

if not defined SCRIPT (
    echo(
    echo   _run.cmd is not the one to double click - use one of the run_*.cmd
    echo   files beside it.
    echo(
    pause
    exit /b 2
)

rem --- where things are.  Everything is resolved from THIS file, so the folder
rem     can be moved with the repo and neither mode depends on a working
rem     directory the scheduler happens to hand us.
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "REPO=%%~fI"
set "PY_FILE=%REPO%\scripts\%SCRIPT%\%SCRIPT%.py"

rem --- machine settings, then defaults for what they did not set.
set "PYTHON="
set "LOG_DIR=%HERE%logs"
set "KEEP_LOG_DAYS=30"
if exist "%HERE%local_settings.cmd" call "%HERE%local_settings.cmd"

if not defined PYTHON (
    where py >nul 2>&1 && (set "PYTHON=py -3") || (set "PYTHON=python")
)

rem --- the first word decides the mode; the rest is forwarded.  Built by hand
rem     rather than with %*, because %* does not notice `shift`.
set "MODE=interactive"
set "ARGS="
:collect
if "%~1"=="" goto collected
if /i "%~1"=="scheduled" (set "MODE=scheduled") else (set "ARGS=%ARGS% %1")
shift
goto collect
:collected

rem --- python writes UTF-8 and does not sit on its output.  Without the first
rem     an em dash in a table is a UnicodeEncodeError; without the second the
rem     log arrives in the wrong order when it is redirected.
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%PY_FILE%" (
    echo(
    echo   cannot find %PY_FILE%
    echo   Is this folder still beside the repo it was copied out of?
    echo(
    if /i not "%MODE%"=="scheduled" pause
    exit /b 2
)

for /f "usebackq delims=" %%I in (
    `powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`
) do set "STAMP=%%I"

cd /d "%REPO%"

if /i "%MODE%"=="scheduled" goto scheduled

rem -----------------------------------------------------------------------------
rem  DOUBLE CLICKED
rem -----------------------------------------------------------------------------
title %SCRIPT%
chcp 65001 >nul
echo(
echo   %SCRIPT%   %STAMP%
echo   %PYTHON% "%PY_FILE%"%ARGS%
echo(
%PYTHON% "%PY_FILE%"%ARGS%
set "RC=%ERRORLEVEL%"
echo(
if "%RC%"=="0" (
    echo   done.
) else (
    echo   FAILED - exit code %RC%.  The lines above are the whole of it.
)
echo(
pause
exit /b %RC%

rem -----------------------------------------------------------------------------
rem  TASK SCHEDULER
rem -----------------------------------------------------------------------------
:scheduled
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG=%LOG_DIR%\%SCRIPT%_%STAMP%.log"

echo === %SCRIPT% %STAMP% ===> "%LOG%"
echo %PYTHON% "%PY_FILE%"%ARGS%>> "%LOG%"
echo(>> "%LOG%"
%PYTHON% "%PY_FILE%"%ARGS% >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo(>> "%LOG%"
echo === exit code %RC% ===>> "%LOG%"

rem  Old logs go.  A folder that only ever grows is a folder nobody opens.
forfiles /p "%LOG_DIR%" /m "%SCRIPT%_*.log" /d -%KEEP_LOG_DAYS% /c "cmd /c del @file" >nul 2>&1

exit /b %RC%
