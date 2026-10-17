param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ManualPath,
    [Parameter(Mandatory = $true)][string]$QuickStartPath
)

$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$runtime = Join-Path (Split-Path -Parent $exe) '_internal'
if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
    throw 'Expected PyInstaller onedir runtime folder _internal was not found beside the executable.'
}
$output = [IO.Path]::GetFullPath($OutputDirectory)
$stage = Join-Path $output 'ZISU-Research-Studio-V4.4.1-Windows'
$zip = Join-Path $output 'ZISU-Research-Studio-V4.4.1-Windows.zip'
if (Test-Path -LiteralPath $stage) { throw "Refusing to overwrite existing package staging directory: $stage" }
if (Test-Path -LiteralPath $zip) { throw "Refusing to overwrite existing package archive: $zip" }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Copy-Item -LiteralPath $exe -Destination (Join-Path $stage 'ZisuResearchStudio.exe')
$stagedRuntime = Join-Path $stage '_internal'
New-Item -ItemType Directory -Path $stagedRuntime | Out-Null
Get-ChildItem -LiteralPath $runtime -Force | Copy-Item -Destination $stagedRuntime -Recurse
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $ManualPath).Path -Destination (Join-Path $stage 'User-Guide-KO.html')
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $QuickStartPath).Path -Destination (Join-Path $stage 'Quick-Start-KO.txt')
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stage 'ZisuResearchStudio.exe')).Hash.ToLowerInvariant()
"$hash  ZisuResearchStudio.exe" | Set-Content -Encoding ascii (Join-Path $stage 'SHA256SUMS.txt')
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal

$required = @('ZisuResearchStudio.exe', 'SHA256SUMS.txt', 'User-Guide-KO.html', 'Quick-Start-KO.txt')
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
    if ($top -notin @('ZisuResearchStudio.exe', 'SHA256SUMS.txt', 'User-Guide-KO.html', 'Quick-Start-KO.txt', '_internal')) {
        throw "Unexpected ZIP top-level entry: $entry"
    }
    if ($normalized -match '(^|/)\.\.?(/|$)|(?i)(client_secret[^/]*\.json|oauth_tokens|token\.json|(^|/)token[^/]*\.json$|credentials\.json|authorized[_-]?user\.json|\.db$|\.sqlite|\.log$|backup|\.py$)') {
        throw "Sensitive or source-like ZIP entry: $entry"
    }
}
foreach ($item in $required) {
    if ($item -notin $entries) { throw "Required release file missing from ZIP: $item" }
}
if (-not ($entries | Where-Object { $_ -like '_internal/*' })) {
    throw 'PyInstaller runtime payload is missing from the release ZIP.'
}

$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
"$zipHash  ZISU-Research-Studio-V4.4.1-Windows.zip" |
    Set-Content -Encoding ascii (Join-Path $output 'RELEASE_SHA256SUMS.txt')

[ordered]@{
    package = $zip
    sha256 = $zipHash
    exe_sha256 = $hash
    entries = $entries
} | ConvertTo-Json -Depth 3
