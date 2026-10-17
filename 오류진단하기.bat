@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if defined LOCALAPPDATA (set "LOGDIR=%LOCALAPPDATA%\ZisuResearchStudio\logs") else (set "LOGDIR=%~dp0logs")
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\diagnostic_latest.txt"
>"%LOG%" echo ZISU V3.0 Diagnostic Report
>>"%LOG%" echo Date: %DATE% %TIME%
>>"%LOG%" echo Working path: %CD%
>>"%LOG%" echo Windows version:
ver >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo Python launcher:
where py >>"%LOG%" 2>&1
py --version >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo python executable:
where python >>"%LOG%" 2>&1
python --version >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo Expected files:
dir /b "main.py" "mcrs\gui.py" >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo GUI import check with launcher:
py -3 -c "import sys, tkinter; from mcrs.gui import Studio; print('PASS tkinter and GUI import:',sys.version)" >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo GUI import check with python:
python -c "import sys, tkinter; from mcrs.gui import Studio; print('PASS tkinter and GUI import:',sys.version)" >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo Last startup log:
if exist "%LOGDIR%\startup_latest.log" type "%LOGDIR%\startup_latest.log" >>"%LOG%"
echo The report will open in Notepad.
start "" notepad.exe "%LOG%"
pause
