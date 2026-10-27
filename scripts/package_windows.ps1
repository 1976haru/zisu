param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ManualPath,
    [Parameter(Mandatory = $true)][string]$QuickStartPath,
    [ValidatePattern('^\d+\.\d+\.\d+$')][string]$ProductVersion = '4.6.0',
    [string]$LauncherPath = (Join-Path $PSScriptRoot '..\ZISU_SIGNAL_LAB_Run.cmd')
)

$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$runtime = Join-Path (Split-Path -Parent $exe) '_internal'
if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
    throw 'Expected PyInstaller onedir runtime folder _internal was not found beside the executable.'
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
$product = 'ZISU SIGNAL LAB'
$packageBase = "ZISU-SIGNAL-LAB-V$ProductVersion-Windows"
$stage = Join-Path $output $packageBase
$zip = Join-Path $output ($packageBase + '.zip')
$exeVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($exe)
if ($exeVersion.ProductName -ne $product -or $exeVersion.ProductVersion -notlike "$ProductVersion.*") {
    throw "EXE Windows metadata does not identify $product version $ProductVersion."
}
if (Test-Path -LiteralPath $stage) { throw "Refusing to overwrite existing package staging directory: $stage" }
if (Test-Path -LiteralPath $zip) { throw "Refusing to overwrite existing package archive: $zip" }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Copy-Item -LiteralPath $exe -Destination (Join-Path $stage 'ZisuResearchStudio.exe')
$stagedRuntime = Join-Path $stage '_internal'
New-Item -ItemType Directory -Path $stagedRuntime | Out-Null
Get-ChildItem -LiteralPath $runtime -Force | Copy-Item -Destination $stagedRuntime -Recurse
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $ManualPath).Path -Destination (Join-Path $stage 'User-Guide-KO.html')
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $QuickStartPath).Path -Destination (Join-Path $stage 'Quick-Start-KO.txt')
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $LauncherPath).Path -Destination (Join-Path $stage 'ZISU-SIGNAL-LAB-Run.cmd')
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stage 'ZisuResearchStudio.exe')).Hash.ToLowerInvariant()
"$hash  ZisuResearchStudio.exe" | Set-Content -Encoding ascii (Join-Path $stage 'SHA256SUMS.txt')
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal

$required = @('ZisuResearchStudio.exe', 'SHA256SUMS.txt', 'User-Guide-KO.html', 'Quick-Start-KO.txt', 'ZISU-SIGNAL-LAB-Run.cmd')
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    $entries = @($archive.Entries | ForEach-Object { $_.FullName })
} finally {
    $archive.Dispose()
}
foreach ($entry in $entries) {
    $normalized = $entry.Replace('\', '/')
    $top = $normalized.Split('/')[0]
    if ($top -notin @('ZisuResearchStudio.exe', 'SHA256SUMS.txt', 'User-Guide-KO.html', 'Quick-Start-KO.txt', 'ZISU-SIGNAL-LAB-Run.cmd', '_internal')) {
        throw "Unexpected ZIP top-level entry: $entry"
    }
    $sensitivePayload = $normalized -match '(?i)(client_secret[^/]*\.json|oauth_tokens|token\.json|(^|/)token[^/]*\.json$|credentials\.json|authorized[_-]?user\.json|\.db$|\.sqlite|\.log$|backup)'
    $sourceOutsideRuntime = $normalized -match '(?i)\.py$' -and $top -ne '_internal'
    if ($normalized -match '(^|/)\.\.?(/|$)' -or $sensitivePayload -or $sourceOutsideRuntime) {
        throw "Sensitive or source-like ZIP entry: $entry"
    }
}
foreach ($item in $required) {
    if ($item -notin $entries) { throw "Required release file missing from ZIP: $item" }
}
$normalizedEntries = @($entries | ForEach-Object { $_.Replace('\', '/') })
if (-not ($normalizedEntries | Where-Object { $_ -like '_internal/*' })) {
    throw 'PyInstaller runtime payload is missing from the release ZIP.'
}

$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
"$zipHash  $packageBase.zip" |
    Set-Content -Encoding ascii (Join-Path $output 'RELEASE_SHA256SUMS.txt')

[ordered]@{
    package = $zip
    sha256 = $zipHash
    exe_sha256 = $hash
    entries = $entries
} | ConvertTo-Json -Depth 3
