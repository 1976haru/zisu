param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [Parameter(Mandatory = $true)][string]$WorkRoot
)

$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$root = [IO.Path]::GetFullPath($WorkRoot)
New-Item -ItemType Directory -Force -Path $root | Out-Null
$data = Join-Path $root 'isolated-data'
$report = Join-Path $root 'exe-smoke-report.json'
New-Item -ItemType Directory -Force -Path $data | Out-Null

$oldPath = $env:PATH
try {
    # Keep only Windows runtime paths. This checks that the EXE does not launch
    # python.exe or import project sources from the runner PATH.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
    $arguments = @(
        '--data-dir', ('"{0}"' -f $data),
        '--offline-smoke-report', ('"{0}"' -f $report)
    )
    $process = Start-Process -FilePath $exe -ArgumentList $arguments -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(30000)) {
        Stop-Process -Id $process.Id -Force
        throw 'EXE did not close itself within 30 seconds.'
    }
    if ($process.ExitCode -ne 0) {
        throw "EXE returned exit code $($process.ExitCode)."
    }
} finally {
    $env:PATH = $oldPath
}

if (-not (Test-Path -LiteralPath $report)) { throw 'EXE smoke report was not created.' }
$result = Get-Content -LiteralPath $report -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $result.gui_initialized) { throw 'Tkinter GUI initialization was not completed.' }
if (-not $result.frozen_executable) { throw 'Process was not a frozen standalone executable.' }
if (-not $result.data_directory_created) { throw 'Isolated data directory was not initialized.' }
if ($result.network_used -or $result.oauth_connected) { throw 'Offline smoke unexpectedly used an external account.' }
if ($result.python_executable -ne 'ZisuResearchStudio.exe') { throw 'Frozen process executable identity is invalid.' }

$os = Get-CimInstance Win32_OperatingSystem
[ordered]@{
    exe_smoke = 'PASS'
    product_name = $os.Caption
    os_version = $os.Version
    os_build = $os.BuildNumber
    gui_initialized = $result.gui_initialized
    frozen_executable = $result.frozen_executable
    source_independent_path = (Split-Path -Parent $exe)
    python_removed_from_path = $true
} | ConvertTo-Json
