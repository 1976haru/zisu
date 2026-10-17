@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if not exist "main.py" goto missing_files
if not exist "mcrs\gui.py" goto missing_files
if defined LOCALAPPDATA (set "LOGDIR=%LOCALAPPDATA%\ZisuResearchStudio\logs") else (set "LOGDIR=%~dp0logs")
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\startup_latest.log"
>"%LOG%" echo ZISU V4.4 Source Startup Diagnostics
>>"%LOG%" echo Source directory: %CD%
>>"%LOG%" echo Date and time: %DATE% %TIME%
>>"%LOG%" echo Checking Python 3.10+ and tkinter...
set "PYRUN="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYRUN=py"
)
if defined PYRUN goto run_py
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYRUN=python"
)
if defined PYRUN goto run_python
>>"%LOG%" echo ERROR: Python 3.10+ with tkinter was not found.
where py >>"%LOG%" 2>&1
where python >>"%LOG%" 2>&1
echo.
echo [ZISU] Python 3.10 or newer with tkinter is required.
echo [ZISU] Install from https://www.python.org/downloads/windows/
echo [ZISU] Then close and reopen this launcher.
echo.
echo Detailed log: "%LOG%"
start "" "%~dp0설치안내_문제해결.html"
pause
exit /b 10
:run_py
>>"%LOG%" echo Using: py -3
py -3 main.py >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
goto finished
:run_python
>>"%LOG%" echo Using: python
python main.py >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
goto finished
:finished
if "%RC%"=="0" exit /b 0
echo.
echo [ZISU] Startup failed. Exit code: %RC%
echo.
type "%LOG%"
echo.
echo [ZISU] Please copy this log for troubleshooting:
echo "%LOG%"
start "" notepad.exe "%LOG%"
pause
exit /b %RC%
:missing_files
echo [ZISU] main.py or mcrs\gui.py was not found.
echo [ZISU] Extract ALL files from the ZIP to one folder before launching.
pause
exit /b 11
