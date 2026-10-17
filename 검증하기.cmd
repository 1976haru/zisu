@echo off
setlocal
cd /d "%~dp0"
if exist "%WINDIR%\System32\WindowsSandbox.exe" (
  echo Windows Sandbox is available. Launching the isolated offline verification.
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_windows_sandbox_config.ps1" -PackageZip "%~1"
) else (
  echo Windows Sandbox was not found. Running the offline verification on this Windows PC.
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\validate_windows_release.ps1" %*
)
set RESULT=%ERRORLEVEL%
if not "%RESULT%"=="0" (
  echo Validation failed. Keep your existing database and OAuth files unchanged.
) else (
  echo Validation passed. This does not perform Google sign-in or YouTube searches.
)
pause
exit /b %RESULT%
