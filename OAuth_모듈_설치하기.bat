@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 goto pyfound
where python >nul 2>&1
if errorlevel 1 goto no_python
python -m pip install -r requirements-oauth.txt
if errorlevel 1 goto failed
goto success
:pyfound
py -3 -m pip install -r requirements-oauth.txt
if errorlevel 1 goto failed
:success
echo [OK] Google OAuth modules installed.
pause
exit /b 0
:no_python
echo [ERROR] Python not installed. See official python.org.
pause
exit /b 1
:failed
echo [ERROR] Could not install modules. Check internet, permission, and Python version.
pause
exit /b 2
