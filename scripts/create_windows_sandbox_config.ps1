param(
    [string]$PackageZip,
    [string]$ZipManifestPath,
    [string]$OutputRoot,
    [switch]$GenerateOnly
)
$ErrorActionPreference = 'Stop'
$sandbox = Get-Command WindowsSandbox.exe -ErrorAction SilentlyContinue
if (-not $sandbox -and -not $GenerateOnly) {
    Write-Host 'Windows Sandbox is not available in PATH. No Windows feature or security setting was changed.' -ForegroundColor Yellow
    exit 2
}
if (-not $PackageZip) { $PackageZip = Read-Host 'Enter the full path to the V4.5.9 release ZIP' }
$zip = (Resolve-Path -LiteralPath $PackageZip).Path
$packageRoot = Split-Path -Parent $zip
if (-not $ZipManifestPath) { $ZipManifestPath = Join-Path $packageRoot 'RELEASE_SHA256SUMS.txt' }
if (-not (Test-Path -LiteralPath $ZipManifestPath -PathType Leaf)) { throw 'Place RELEASE_SHA256SUMS.txt beside the release ZIP.' }
if (-not $OutputRoot) { $OutputRoot = Join-Path ([IO.Path]::GetTempPath()) ('ZISU_Sandbox_' + [guid]::NewGuid().ToString('N')) }
New-Item -ItemType Directory -Path $OutputRoot | Out-Null
$toolsRoot = Join-Path $OutputRoot 'tools'
New-Item -ItemType Directory -Path $toolsRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'validate_windows_release.ps1') -Destination $toolsRoot
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows_exe_smoke.ps1') -Destination $toolsRoot
$resultsRoot = Join-Path $OutputRoot 'results'
New-Item -ItemType Directory -Path $resultsRoot | Out-Null
$wsb = Join-Path $OutputRoot 'ZISU-V4.5.9-validation.wsb'
$zipName = [IO.Path]::GetFileName($zip)
$command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\ZISUTools\validate_windows_release.ps1 -PackageZip 'C:\ZISUInput\$zipName' -ZipManifestPath C:\ZISUInput\RELEASE_SHA256SUMS.txt -ReportPath C:\ZISUOutput\ZISU-validation-report.json -PauseAtEnd"
$escape = { param($s) [Security.SecurityElement]::Escape($s) }
$xml = @"
<Configuration>
  <Networking>Disable</Networking>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder><HostFolder>$(& $escape $packageRoot)</HostFolder><SandboxFolder>C:\ZISUInput</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape $toolsRoot)</HostFolder><SandboxFolder>C:\ZISUTools</SandboxFolder><ReadOnly>true</ReadOnly></MappedFolder>
    <MappedFolder><HostFolder>$(& $escape $resultsRoot)</HostFolder><SandboxFolder>C:\ZISUOutput</SandboxFolder><ReadOnly>false</ReadOnly></MappedFolder>
  </MappedFolders>
  <LogonCommand><Command>$(& $escape $command)</Command></LogonCommand>
</Configuration>
"@
Set-Content -LiteralPath $wsb -Value $xml -Encoding utf8
if ($GenerateOnly) {
    Write-Host "Windows Sandbox config created (not launched): $wsb"
    exit 0
}
Write-Host "Launching isolated Windows Sandbox. Network and clipboard are disabled; only the results mapping is writable." -ForegroundColor Cyan
Start-Process -FilePath $sandbox.Source -ArgumentList ('"' + $wsb + '"')
Write-Host "Sandbox report will be written under: $resultsRoot"
