@echo off
setlocal
set "CHECKER=%~dp0Run_SecureBackup_Fixture_Only.ps1"
if not exist "%CHECKER%" (
  echo Secure backup fixture checker is missing.
  pause
  exit /b 2
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CHECKER%"
set "RESULT=%ERRORLEVEL%"
echo.
echo Secure backup fixture result: %RESULT%
echo Close this window after noting the JSON result above.
pause
exit /b %RESULT%
