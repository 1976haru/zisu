@echo off
setlocal
cd /d "%~dp0"
set "ZIP=%~1"
if not defined ZIP (
  echo Drag the V4.4.2 release ZIP onto this CMD, or enter its full path below.
  set /p "ZIP=Release ZIP path: "
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_windows_sandbox_config.ps1" -PackageZip "%ZIP%"
if not "%ERRORLEVEL%"=="0" echo Windows Sandbox is unavailable or could not be launched. No system feature was enabled or changed.
pause
