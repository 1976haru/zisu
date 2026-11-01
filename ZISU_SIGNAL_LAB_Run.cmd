@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "ROOT=%~dp0"
set "EXE=%ROOT%ZisuResearchStudio.exe"
set "RUNTIME=%ROOT%_internal"
if defined LOCALAPPDATA (set "LOGDIR=%LOCALAPPDATA%\ZisuResearchStudio\logs") else (set "LOGDIR=%TEMP%\ZisuResearchStudio\logs")
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\launcher_latest.log"
>>"%LOG%" echo [%DATE% %TIME%] ZISU SIGNAL LAB V4.6.0 package launcher.
if not exist "%EXE%" goto missing_exe
if not exist "%RUNTIME%\" goto missing_runtime
pushd "%ROOT%" >nul 2>&1
if errorlevel 1 goto folder_error
start "" /wait "%EXE%"
set "RESULT=%ERRORLEVEL%"
popd
>>"%LOG%" echo [%DATE% %TIME%] App exit code %RESULT%.
if "%RESULT%"=="0" exit /b 0
echo ZISU SIGNAL LAB closed with exit code %RESULT%.
goto failed
:missing_exe
echo ZISU SIGNAL LAB executable is missing.
goto failed
:missing_runtime
echo Keep the full package together; the _internal folder is missing.
goto failed
:folder_error
echo Windows could not open the package folder.
:failed
echo Check this non-secret diagnostic log: "%LOG%"
echo Do not delete your database or Google connection files while troubleshooting.
pause
exit /b 1
