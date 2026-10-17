param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][string]$ManualPath,
    [Parameter(Mandatory = $true)][string]$QuickStartPath
)

$ErrorActionPreference = 'Stop'
$exe = (Resolve-Path -LiteralPath $ExePath).Path
$output = [IO.Path]::GetFullPath($OutputDirectory)
$stage = Join-Path $output 'ZISU-Research-Studio-V4.4-Windows'
$zip = Join-Path $output 'ZISU-Research-Studio-V4.4-Windows.zip'
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Copy-Item -LiteralPath $exe -Destination (Join-Path $stage 'ZisuResearchStudio.exe')
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $ManualPath).Path -Destination (Join-Path $stage 'User-Guide-KO.html')
Copy-Item -LiteralPath (Resolve-Path -LiteralPath $QuickStartPath).Path -Destination (Join-Path $stage 'Quick-Start-KO.txt')
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stage 'ZisuResearchStudio.exe')).Hash.ToLowerInvariant()
"$hash  ZisuResearchStudio.exe" | Set-Content -Encoding ascii (Join-Path $stage 'SHA256SUMS.txt')
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal

$allowed = @('ZisuResearchStudio.exe', 'SHA256SUMS.txt', 'User-Guide-KO.html', 'Quick-Start-KO.txt')
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    $entries = @($archive.Entries | ForEach-Object { $_.FullName })
} finally {
    $archive.Dispose()
}
if (@($entries).Count -ne $allowed.Count) { throw "Unexpected ZIP entry count: $($entries.Count)" }
foreach ($entry in $entries) {
    if ($entry -notin $allowed) { throw "Unexpected or unsafe ZIP entry: $entry" }
    if ($entry -match '(?i)(client_secret|oauth_tokens|token\.json|\.db$|\.sqlite|\.log$|backup|\.py$)') {
        throw "Sensitive or source-like ZIP entry: $entry"
    }
}

$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
"$zipHash  ZISU-Research-Studio-V4.4-Windows.zip" |
    Set-Content -Encoding ascii (Join-Path $output 'RELEASE_SHA256SUMS.txt')

[ordered]@{
    package = $zip
    sha256 = $zipHash
    exe_sha256 = $hash
    entries = $entries
} | ConvertTo-Json -Depth 3
