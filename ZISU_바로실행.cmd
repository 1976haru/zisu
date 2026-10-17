@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0"
set "APPDIR=%ROOT%ZISU_APP"
set "EXE=%APPDIR%\ZisuResearchStudio.exe"
set "RUNTIME=%APPDIR%\_internal"

if defined LOCALAPPDATA (
    set "LOGDIR=%LOCALAPPDATA%\ZisuResearchStudio\logs"
) else (
    set "LOGDIR=%TEMP%\ZisuResearchStudio\logs"
)
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\launcher_latest.log"
set "LOG_PROBE=%LOGDIR%\launcher_probe_%RANDOM%_%RANDOM%.tmp"
>"%LOG_PROBE%" echo writable 2>nul
if not exist "%LOG_PROBE%" goto fallback_log
del "%LOG_PROBE%" >nul 2>&1

>>"%LOG%" echo [%DATE% %TIME%] ZISU V4.5.8 launcher started. 2>nul
goto log_ready

:fallback_log
set "LOGDIR=%TEMP%\ZisuResearchStudio\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
set "LOG=%LOGDIR%\launcher_latest.log"
set "LOG_PROBE=%LOGDIR%\launcher_probe_%RANDOM%_%RANDOM%.tmp"
>"%LOG_PROBE%" echo writable 2>nul
if not exist "%LOG_PROBE%" goto no_log
del "%LOG_PROBE%" >nul 2>&1
>>"%LOG%" echo [%DATE% %TIME%] ZISU V4.5.8 launcher started using temporary log fallback. 2>nul
goto log_ready

:no_log
echo [ZISU] A diagnostic log could not be created. Check folder permissions.
pause
exit /b 1

:log_ready
>>"%LOG%" echo Application directory: "%APPDIR%"

if not exist "%EXE%" goto missing_exe
if not exist "%RUNTIME%\" goto missing_runtime

pushd "%APPDIR%" >nul 2>&1
if errorlevel 1 goto app_directory_error

>>"%LOG%" echo [%DATE% %TIME%] Launching packaged executable.
start "" /wait "%EXE%"
set "RESULT=%ERRORLEVEL%"
popd
>>"%LOG%" echo [%DATE% %TIME%] Application exited with code %RESULT%.

if not "%RESULT%"=="0" goto app_failed
exit /b 0

:missing_exe
>>"%LOG%" echo ERROR: Packaged executable is missing.
echo [ZISU] The V4.5.8 application file was not found.
echo Expected: "%EXE%"
goto show_help

:missing_runtime
>>"%LOG%" echo ERROR: PyInstaller runtime folder is missing.
echo [ZISU] The application runtime folder was not found.
echo Keep the complete extracted package, including _internal, in ZISU_APP.
goto show_help

:app_directory_error
>>"%LOG%" echo ERROR: Could not enter the application directory.
echo [ZISU] Windows could not open the application folder.
goto show_help

:app_failed
echo [ZISU] The application stopped with exit code %RESULT%.
goto show_help

:show_help
echo.
echo Detailed, non-secret startup log:
echo "%LOG%"
echo.
echo Do not delete your database, source files, or OAuth data while troubleshooting.
pause
exit /b 1
