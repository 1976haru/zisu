param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [Parameter(Mandatory = $true)][string]$WorkRoot,
    [ValidateRange(5, 180)][int]$TimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
if (-not ('ZisuSmokeWindowApi' -as [type])) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class ZisuSmokeWindowApi {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr window, StringBuilder className, int maxCount);
}
'@
}
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$root = [IO.Path]::GetFullPath($WorkRoot)
if (Test-Path -LiteralPath $root) { throw "Refusing to reuse existing smoke workspace: $root" }
New-Item -ItemType Directory -Path $root | Out-Null
$data = Join-Path $root 'isolated-data'
$report = Join-Path $root 'exe-smoke-report.json'
$diagnosticPath = Join-Path $root 'exe-smoke-failure.json'
New-Item -ItemType Directory -Force -Path $data | Out-Null
if (Test-Path -LiteralPath $report) { throw "Refusing to overwrite existing smoke report: $report" }
if (Test-Path -LiteralPath $diagnosticPath) { throw "Refusing to overwrite existing diagnostic report: $diagnosticPath" }

$version = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$os = [ordered]@{
    product_name = $version.ProductName
    display_version = $version.DisplayVersion
    os_build = "$($version.CurrentBuild).$($version.UBR)"
}
$originalPath = $env:PATH
$startedAt = Get-Date
$timer = [Diagnostics.Stopwatch]::StartNew()
$launchToGuiMs = $null
$lastReport = $null
$stageTimeline = @()
$process = $null
$startupFailure = $null

try {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $exe
    $start.Arguments = '--data-dir "{0}" --offline-smoke-report "{1}"' -f $data, $report
    $start.WorkingDirectory = Split-Path -Parent $exe
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    # Explicit child environment: no Python installation or project source path
    # is available, and all app data stays in this smoke's isolated workspace.
    $start.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
    $start.EnvironmentVariables['ZISU_DATA_DIR'] = $data
    $start.EnvironmentVariables['ZISU_EXE_SMOKE_REPORT'] = $report
    $start.EnvironmentVariables['ZISU_EXE_SMOKE_PROFILE_TEST'] = '1'
    $start.EnvironmentVariables['ZISU_EXE_SMOKE_START_COUNTER'] = [string][Diagnostics.Stopwatch]::GetTimestamp()
    $start.EnvironmentVariables['ZISU_EXE_SMOKE_COUNTER_FREQUENCY'] = [string][Diagnostics.Stopwatch]::Frequency
    $process = [System.Diagnostics.Process]::Start($start)

    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds -and -not $process.HasExited) {
        if (Test-Path -LiteralPath $report) {
            try {
                $current = Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json
                if ($current.stage -ne $lastReport) {
                    $stageTimeline += [ordered]@{
                        stage = $current.stage
                        elapsed_ms = $current.elapsed_ms
                    }
                    $lastReport = $current.stage
                }
                if ($current.stage -eq 'gui_ready') {
                    $guiEvent = $current.events | Where-Object { $_.stage -eq 'gui_ready' } | Select-Object -Last 1
                    if ($null -eq $launchToGuiMs -and $guiEvent) { $launchToGuiMs = [double]$guiEvent.launch_elapsed_ms }
                }
                if ($current.stage -eq 'startup_failed') {
                    $startupFailure = $current
                    break
                }
            } catch {
                # Atomic report replacement can briefly leave a partial read.
            }
        }
        [void]$process.WaitForExit(100)
    }

    # The process may exit between the last poll and loop condition evaluation.
    # Read the atomic report once more so startup exceptions are never mislabeled
    # as a generic nonzero exit or GUI-close timeout.
    if (-not $startupFailure -and (Test-Path -LiteralPath $report)) {
        try {
            $finalReport = Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json
            if ($finalReport.stage -eq 'startup_failed') { $startupFailure = $finalReport }
        } catch {
            # Preserve the original process/timeout classification if report is unreadable.
        }
    }

    if ($startupFailure) {
        $process.Refresh()
        $sameImage = @()
        Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } |
            ForEach-Object {
                try {
                    if ($_.StartTime -ge $startedAt) {
                        $sameImage += [ordered]@{
                            process_id = $_.Id
                            started_at = $_.StartTime.ToString('o')
                            main_window_handle = [string]$_.MainWindowHandle
                            main_window_title = $_.MainWindowTitle
                        }
                    }
                } catch { }
            }
        $failure = [ordered]@{
            result = 'STARTUP_FAILED'
            elapsed_ms = [math]::Round($timer.Elapsed.TotalMilliseconds, 1)
            process_id = $process.Id
            exit_code = if ($process.HasExited) { $process.ExitCode } else { $null }
            last_python_stage = 'startup_failed'
            startup_error_type = $startupFailure.error_type
            startup_error_location = $startupFailure.error_location
            stage_timeline = $stageTimeline
            python_path_removed = $true
            native_window_handle_present = ($process.MainWindowHandle -ne [IntPtr]::Zero)
            native_window_handle_title = $process.MainWindowTitle
            tracked_image_processes = $sameImage
            report_file_exists = $true
            isolated_data_file_count = @(Get-ChildItem -LiteralPath $data -File -Force -ErrorAction SilentlyContinue).Count
            os = $os
        }
        $failure | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath $diagnosticPath
        # Terminate only processes whose executable path is this exact staged image.
        foreach ($entry in $sameImage) {
            Stop-Process -Id $entry.process_id -Force -ErrorAction SilentlyContinue
        }
        if ($sameImage.Count -eq 0 -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        throw "EXE reported startup_failed at $($startupFailure.error_location.module).$($startupFailure.error_location.function):$($startupFailure.error_location.line) ($($startupFailure.error_type)). Diagnostic: $diagnosticPath"
    }

    if (-not $process.HasExited) {
        $process.Refresh()
        $sameImage = @()
        Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } |
            ForEach-Object {
                try {
                    if ($_.StartTime -ge $startedAt) {
                        $sameImage += [ordered]@{
                            process_id = $_.Id
                            started_at = $_.StartTime.ToString('o')
                            main_window_handle = [string]$_.MainWindowHandle
                            main_window_title = $_.MainWindowTitle
                        }
                    }
                } catch { }
            }
        $stage = if ($lastReport) { $lastReport } else { 'no Python-stage report' }
        $windowClass = New-Object Text.StringBuilder 256
        if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
            [void][ZisuSmokeWindowApi]::GetClassName($process.MainWindowHandle, $windowClass, $windowClass.Capacity)
        }
        $tempRoot = Join-Path $env:LOCALAPPDATA 'Temp'
        $extraction = @(Get-ChildItem -LiteralPath $tempRoot -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue |
            Where-Object { $_.CreationTime -ge $startedAt } |
            ForEach-Object {
                $files = @(Get-ChildItem -LiteralPath $_.FullName -File -Force -Recurse -ErrorAction SilentlyContinue)
                [ordered]@{ directory_name = $_.Name; file_count = $files.Count; bytes = ($files | Measure-Object Length -Sum).Sum }
            })
        $failure = [ordered]@{
            result = 'GUI_CLOSE_TIMEOUT'
            timeout_seconds = $TimeoutSeconds
            elapsed_ms = [math]::Round($timer.Elapsed.TotalMilliseconds, 1)
            process_id = $process.Id
            exit_code = $null
            last_python_stage = $stage
            stage_timeline = $stageTimeline
            python_path_removed = $true
            native_window_handle_present = ($process.MainWindowHandle -ne [IntPtr]::Zero)
            native_window_handle_title = $process.MainWindowTitle
            native_window_class = $windowClass.ToString()
            tracked_image_processes = $sameImage
            pyinstaller_temp_directories = $extraction
            report_file_exists = Test-Path -LiteralPath $report
            isolated_data_file_count = @(Get-ChildItem -LiteralPath $data -File -Force -ErrorAction SilentlyContinue).Count
            os = $os
        }
        $failure | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath $diagnosticPath
        # Stop only this run's exact binary path; onefile builds can leave a child
        # image behind as well as the parent.
        foreach ($entry in $sameImage) {
            Stop-Process -Id $entry.process_id -Force -ErrorAction SilentlyContinue
        }
        if ($sameImage.Count -eq 0) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        throw "EXE did not complete the GUI-close smoke within $TimeoutSeconds seconds (last Python stage: $stage). Diagnostic: $diagnosticPath"
    }

    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "EXE returned exit code $($process.ExitCode)." }
} finally {
    $env:PATH = $originalPath
    $timer.Stop()
}

if (-not (Test-Path -LiteralPath $report)) { throw 'EXE smoke report was not created; Python entrypoint was not verified.' }
$result = Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json
if ($result.stage -ne 'offline_smoke_complete' -or -not $result.gui_initialized) { throw "Tkinter GUI and offline profile smoke did not complete (last stage: $($result.stage))." }
if (-not $result.frozen_executable) { throw 'Process was not a frozen standalone executable.' }
if (-not $result.data_directory_created) { throw 'Isolated data directory was not initialized.' }
if ($result.network_used -or $result.oauth_connected) { throw 'Offline smoke unexpectedly used an external account.' }
if ($result.python_executable -ne 'ZisuResearchStudio.exe') { throw 'Frozen process executable identity is invalid.' }
if (-not $result.profile_test -or -not $result.profile_test.passed) { throw 'Offline profile creation/switch/isolation smoke failed.' }
if ($result.database_integrity -ne 'ok') { throw 'Isolated smoke database integrity check failed.' }

$residual = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
if ($residual.Count -gt 0) { throw "EXE process(es) remained after normal exit: $($residual.Id -join ', ')" }
$guiEvent = $result.events | Where-Object { $_.stage -eq 'gui_ready' } | Select-Object -Last 1
if ($null -eq $launchToGuiMs -and $guiEvent) { $launchToGuiMs = [double]$guiEvent.launch_elapsed_ms }
if ($null -eq $launchToGuiMs -or $launchToGuiMs -lt 0) { throw 'GUI-ready monotonic timing was not captured.' }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
[ordered]@{
    exe_smoke = 'PASS'
    os = $os
    exe_sha256 = $hash
    launch_to_gui_ready_ms = [math]::Round($launchToGuiMs, 1)
    gui_ready_to_exit_ms = [math]::Round($timer.Elapsed.TotalMilliseconds - [double]$launchToGuiMs, 1)
    total_exit_ms = [math]::Round($timer.Elapsed.TotalMilliseconds, 1)
    stage_events = $result.events
    frozen_executable = $result.frozen_executable
    python_removed_from_path = $true
    isolated_data_directory_created = $result.data_directory_created
    network_used = $result.network_used
    oauth_connected = $result.oauth_connected
    profile_test = $result.profile_test
    database_integrity = $result.database_integrity
    residual_executable_processes = $residual.Count
} | ConvertTo-Json -Depth 6 | Tee-Object -FilePath (Join-Path $root 'exe-smoke-success.json')
