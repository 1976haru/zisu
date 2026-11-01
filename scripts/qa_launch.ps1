param(
    [Parameter(Mandatory=$true)][string]$QaRoot,
    [string]$Worktree = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [string]$EdgeExe = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath($Worktree)
$root = [IO.Path]::GetFullPath($QaRoot)
$preflight = Join-Path $PSScriptRoot 'qa_preflight.ps1'
$pre = (& $preflight -QaRoot $root -Worktree $repo -PythonExe $PythonExe -EdgeExe $EdgeExe | ConvertFrom-Json)
$blockedChecks = @($pre.checks | Where-Object { $_.status -eq "BLOCKED" })
if ($blockedChecks.Count -gt 0) {
    $blockedResult = [pscustomobject]@{
        result = "BLOCKED"
        reason = "Required QA preflight checks are not ready."
        preflight = $pre
    }
    $blockedResult | ConvertTo-Json -Depth 8
    exit 2
}

# Never stop/reuse a running app automatically. A normal user close is required.
$existing = @(Get-Process -ErrorAction Stop | Where-Object { $_.MainWindowTitle -like "ZISU SIGNAL LAB*" })
if ($existing.Count -gt 0) {
    $summary = @($existing | ForEach-Object {"PID=$($_.Id); path=$($_.Path); started=$($_.StartTime.ToUniversalTime().ToString('o'))"})
    $runningResult = [pscustomobject]@{
        result = "BLOCKED"
        reason = "An existing ZISU window is running; close it normally before launch."
        processes = $summary
    }
    $runningResult | ConvertTo-Json -Depth 5
    exit 3
}

$dirs = @{
    ZISU_DATA_DIR = Join-Path $root 'data'
    LOCALAPPDATA = Join-Path $root 'localappdata'
    APPDATA = Join-Path $root 'appdata'
    TEMP = Join-Path $root 'python-temp'
    TMP = Join-Path $root 'python-temp'
}
foreach ($path in $dirs.Values) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
$scriptPath = Join-Path $repo 'main.py'
$pythonw = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
$guiExe = if (Test-Path -LiteralPath $pythonw -PathType Leaf) {$pythonw} else {$PythonExe}
$envNames = @($dirs.Keys) + @('ZISU_QA_MODE','ZISU_QA_ROOT','ZISU_QA_EDGE_EXE','ZISU_QA_EDGE_PROFILE','PYTHONNOUSERSITE')
$saved = @{}
foreach ($name in $envNames) { $saved[$name] = [Environment]::GetEnvironmentVariable($name,'Process') }
try {
    foreach ($name in $dirs.Keys) { [Environment]::SetEnvironmentVariable($name,$dirs[$name],'Process') }
    [Environment]::SetEnvironmentVariable('ZISU_QA_MODE','1','Process')
    [Environment]::SetEnvironmentVariable('ZISU_QA_ROOT',$root,'Process')
    [Environment]::SetEnvironmentVariable('ZISU_QA_EDGE_EXE',[IO.Path]::GetFullPath($EdgeExe),'Process')
    [Environment]::SetEnvironmentVariable('ZISU_QA_EDGE_PROFILE',(Join-Path $root 'browser-profile'),'Process')
    [Environment]::SetEnvironmentVariable('PYTHONNOUSERSITE','1','Process')
    $process = Start-Process -FilePath $guiExe -ArgumentList ('"'+$scriptPath+'"') -WorkingDirectory $repo -PassThru
    [pscustomobject]@{result='STARTED';pid=$process.Id;executable=$guiExe;python=$PythonExe;qa_root=$root;data_dir=$dirs.ZISU_DATA_DIR;qa_edge_only=$true;started_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Depth 4
} finally {
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name,$saved[$name],'Process') }
}
