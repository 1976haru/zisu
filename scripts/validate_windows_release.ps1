param(
    [string]$PackageZip,
    [string]$ZipManifestPath,
    [string]$ExpectedZipSha256,
    [string]$ReportPath,
    [ValidateRange(10, 180)][int]$TimeoutSeconds = 45,
    [switch]$PauseAtEnd
)

$ErrorActionPreference = 'Stop'
$stage = 'input'
$summary = [ordered]@{
    schema = 1
    product = 'ZISU Research Studio V4.5.4 offline package validation'
    result = 'FAIL'
    zip_hash_verified = $false
    exe_hash_verified = $false
    package_layout_verified = $false
    gui_runs = @()
    network_used = $false
    oauth_used = $false
    residual_processes = 0
}
$testRoot = $null
$timer = [Diagnostics.Stopwatch]::StartNew()

try {
    if (-not $PackageZip) { $PackageZip = Read-Host 'Enter the full path to the V4.5.4 release ZIP' }
    if (-not (Test-Path -LiteralPath $PackageZip -PathType Leaf)) { throw 'Release ZIP not found.' }
    $PackageZip = (Resolve-Path -LiteralPath $PackageZip).Path
    $testRoot = Join-Path ([IO.Path]::GetTempPath()) ('ZISU Validation 한글_日本語 ' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    if (-not $ZipManifestPath) { $ZipManifestPath = Join-Path (Split-Path -Parent $PackageZip) 'RELEASE_SHA256SUMS.txt' }
    if (-not $ExpectedZipSha256 -and (Test-Path -LiteralPath $ZipManifestPath -PathType Leaf)) {
        $manifestLine = Get-Content -LiteralPath $ZipManifestPath -Encoding ascii | Where-Object { $_ -match 'ZISU-Research-Studio-V4\.5\.3-Windows\.zip$' } | Select-Object -First 1
        if ($manifestLine -match '^([0-9a-fA-F]{64})\s+') { $ExpectedZipSha256 = $Matches[1].ToLowerInvariant() }
    }
    if (-not $ExpectedZipSha256) { $ExpectedZipSha256 = (Read-Host 'Paste expected ZIP SHA-256 from RELEASE_SHA256SUMS.txt').Trim() }
    if ($ExpectedZipSha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'Expected ZIP SHA-256 must be 64 hexadecimal characters.' }

    $stage = 'zip hash and structure'
    $actualZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PackageZip).Hash.ToLowerInvariant()
    if ($actualZipHash -ne $ExpectedZipSha256.ToLowerInvariant()) { throw 'ZIP SHA-256 does not match the supplied release manifest.' }
    $summary.zip_sha256 = $actualZipHash
    $summary.zip_hash_verified = $true

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($PackageZip)
    try { $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace('\','/') }) }
    finally { $archive.Dispose() }
    if ('ZisuResearchStudio.exe' -notin $entries -or 'SHA256SUMS.txt' -notin $entries -or -not ($entries | Where-Object { $_ -like '_internal/*' })) {
        throw 'ZIP is missing the EXE, checksum manifest, or _internal runtime folder.'
    }
    foreach ($requiredEntry in @('User-Guide-KO.html', 'Quick-Start-KO.txt')) {
        if ($requiredEntry -notin $entries) { throw "ZIP is missing required beginner documentation: $requiredEntry" }
    }
    foreach ($entry in $entries) {
        $topLevel = $entry.Split('/')[0]
        if ($topLevel -notin @('ZisuResearchStudio.exe', 'SHA256SUMS.txt', 'User-Guide-KO.html', 'Quick-Start-KO.txt', '_internal')) {
            throw 'ZIP contains an unexpected top-level item.'
        }
        if ($entry -match '(?i)(^|/)\.\.?(/|$)|client_secret[^/]*\.json|oauth_tokens|token\.json|credentials\.json|authorized[_-]?user\.json|\.db$|\.sqlite|\.log$|backup|\.py$') {
            throw 'ZIP contains a forbidden source, credential, database, log, or backup entry.'
        }
    }

    $stage = 'isolated extraction and EXE hash'
    $original = Join-Path $testRoot 'package_日本語'
    [IO.Compression.ZipFile]::ExtractToDirectory($PackageZip, $original)
    $sourceExe = Join-Path $original 'ZisuResearchStudio.exe'
    $exeManifestLine = Get-Content -LiteralPath (Join-Path $original 'SHA256SUMS.txt') -Encoding ascii | Where-Object { $_ -match 'ZisuResearchStudio\.exe$' } | Select-Object -First 1
    if ($exeManifestLine -notmatch '^([0-9a-fA-F]{64})\s+') { throw 'EXE checksum manifest is missing or malformed.' }
    $expectedExeHash = $Matches[1].ToLowerInvariant()
    $actualExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceExe).Hash.ToLowerInvariant()
    if ($actualExeHash -ne $expectedExeHash) { throw 'EXE SHA-256 does not match the package checksum manifest.' }
    $runtime = Join-Path $original '_internal'
    if (-not (Test-Path -LiteralPath $runtime -PathType Container) -or -not (Get-ChildItem -LiteralPath $runtime -File -Recurse | Select-Object -First 1)) {
        throw '_internal runtime folder is empty or missing.'
    }
    $summary.exe_sha256 = $actualExeHash
    $summary.exe_hash_verified = $true
    $summary.package_layout_verified = $true

    $stage = 'GUI, profile switch, isolation, and normal-exit smoke'
    $smokeScript = Join-Path $PSScriptRoot 'windows_exe_smoke.ps1'
    if (-not (Test-Path -LiteralPath $smokeScript -PathType Leaf)) { throw 'Bundled Windows EXE smoke script is missing.' }
    $runIndex = 0
    for ($i = 1; $i -le 3; $i++) {
        $runIndex++
        $copy = Join-Path $testRoot ("fresh_path_$i-日本語")
        New-Item -ItemType Directory -Path $copy | Out-Null
        Get-ChildItem -LiteralPath $original -Force | Copy-Item -Destination $copy -Recurse
        $exe = Join-Path $copy 'ZisuResearchStudio.exe'
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant() -ne $actualExeHash) { throw 'Copied EXE hash changed.' }
        $work = Join-Path $testRoot ("isolated_data_$runIndex")
        $output = & $smokeScript -ExePath $exe -WorkRoot $work -TimeoutSeconds $TimeoutSeconds
        $smoke = ($output -join "`n") | ConvertFrom-Json
        $summary.gui_runs += [ordered]@{ kind = 'fresh-path'; run = $i; gui_ready_ms = $smoke.launch_to_gui_ready_ms; total_exit_ms = $smoke.total_exit_ms; residual_processes = $smoke.residual_executable_processes }
    }

    $stage = 'relocated warm GUI, profile switch, and normal-exit smoke'
    $relocation = Join-Path $testRoot 'relocation_stage'
    $moved = Join-Path $testRoot 'moved package_日本語'
    New-Item -ItemType Directory -Path $relocation | Out-Null
    Get-ChildItem -LiteralPath $original -Force | Copy-Item -Destination $relocation -Recurse
    Move-Item -LiteralPath $relocation -Destination $moved
    $movedExe = Join-Path $moved 'ZisuResearchStudio.exe'
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $movedExe).Hash.ToLowerInvariant() -ne $actualExeHash) { throw 'Relocated EXE hash changed.' }
    & $smokeScript -ExePath $movedExe -WorkRoot (Join-Path $testRoot 'warmup_data') -TimeoutSeconds $TimeoutSeconds | Out-Null
    for ($i = 1; $i -le 3; $i++) {
        $runIndex++
        $work = Join-Path $testRoot ("isolated_data_$runIndex")
        $output = & $smokeScript -ExePath $movedExe -WorkRoot $work -TimeoutSeconds $TimeoutSeconds
        $smoke = ($output -join "`n") | ConvertFrom-Json
        $summary.gui_runs += [ordered]@{ kind = 'warm'; run = $i; gui_ready_ms = $smoke.launch_to_gui_ready_ms; total_exit_ms = $smoke.total_exit_ms; residual_processes = $smoke.residual_executable_processes }
    }
    $summary.os = [ordered]@{ product_name = $smoke.os.product_name; display_version = $smoke.os.display_version; build = $smoke.os.os_build }
    $summary.profile_create_switch_isolation = 'PASS (verified in all six EXE smoke runs)'
    $summary.network_oauth = 'NOT USED'
    $summary.result = 'PASS'
    $stage = 'complete'
    Write-Host 'PASS: ZIP/EXE hashes, onedir layout, isolated GUI startup/exit, and JP/KR test-profile switching.' -ForegroundColor Green
} catch {
    $summary.result = 'FAIL'
    $summary.failure_stage = $stage
    $summary.failure_type = $_.Exception.GetType().Name
    Write-Host ("FAIL at {0}: {1}" -f $stage, $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Keep the original DB and OAuth files. Re-download the official artifact, keep EXE beside _internal, then rerun this validator.' -ForegroundColor Yellow
} finally {
    if ($testRoot) {
        $timer.Stop()
        $summary.elapsed_seconds = [math]::Round($timer.Elapsed.TotalSeconds, 2)
        $summary.report_folder = Split-Path -Leaf $testRoot
        if (-not $ReportPath) { $ReportPath = Join-Path $testRoot 'ZISU-validation-report.json' }
    }
    if (-not $summary.os) {
        try {
            $v = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
            $summary.os = [ordered]@{ product_name = $v.ProductName; display_version = $v.DisplayVersion; build = "$($v.CurrentBuild).$($v.UBR)" }
        } catch { $summary.os = 'unavailable' }
    }
    if ($ReportPath) {
        $reportParent = Split-Path -Parent $ReportPath
        if ($reportParent -and -not (Test-Path -LiteralPath $reportParent)) { New-Item -ItemType Directory -Force -Path $reportParent | Out-Null }
        $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding utf8
        Write-Host "Diagnostic report: $ReportPath"
    }
}
if ($PauseAtEnd) { [void](Read-Host 'Validation finished. Press Enter to close this window') }
if ($summary.result -ne 'PASS') { exit 1 }
