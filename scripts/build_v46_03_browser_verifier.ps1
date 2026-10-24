param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [string]$OutputRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'dist/v46-03-verifier-onedir'),
    [string]$WorkRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) 'build/v46-03-verifier-onedir')
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = (Resolve-Path -LiteralPath $PythonExe).Path
$entry = Join-Path $repoRoot 'scripts/v46_03_browser_verifier.py'
$icon = Join-Path $repoRoot 'assets/zisu_signal_lab.ico'
$output = [IO.Path]::GetFullPath($OutputRoot)
$work = [IO.Path]::GetFullPath($WorkRoot)

if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'Verifier entry point is missing.' }
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw 'Verifier icon is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $python) 'Scripts/pyinstaller.exe') -PathType Leaf)) {
    throw 'PyInstaller is missing from this Python environment; no package was installed automatically.'
}
if (Test-Path -LiteralPath $output) { throw "Refusing to overwrite existing output: $output" }
if (Test-Path -LiteralPath $work) { throw "Refusing to overwrite existing build work: $work" }
New-Item -ItemType Directory -Path $output | Out-Null
New-Item -ItemType Directory -Path $work | Out-Null

$pyinstaller = Join-Path (Split-Path -Parent $python) 'Scripts/pyinstaller.exe'
$arguments = @(
    '--noconfirm', '--clean', '--onedir', '--windowed',
    '--paths', $repoRoot,
    '--hidden-import', 'mcrs.browser_verifier',
    '--hidden-import', 'mcrs.branding',
    '--hidden-import', 'mcrs.research_browser',
    '--hidden-import', 'mcrs.resource_guard',
    '--icon', $icon,
    '--name', 'ZISU_V46-03_Browser_Verifier',
    '--distpath', $output,
    '--workpath', $work,
    '--specpath', $work,
    $entry
)
& $pyinstaller @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$package = Join-Path $output 'ZISU_V46-03_Browser_Verifier'
$exe = Join-Path $package 'ZISU_V46-03_Browser_Verifier.exe'
$runtime = Join-Path $package '_internal'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Expected verifier executable was not produced.' }
if (-not (Test-Path -LiteralPath $runtime -PathType Container)) { throw 'Expected _internal runtime directory was not produced.' }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLowerInvariant()
"$hash  ZISU_V46-03_Browser_Verifier.exe" | Set-Content -Encoding ascii (Join-Path $package 'SHA256SUMS.txt')
$guide = Join-Path $repoRoot 'docs/V46-03_BROWSER_VERIFIER.md'
Copy-Item -LiteralPath $guide -Destination (Join-Path $package 'Browser-Verifier-Guide.md')
$zip = Join-Path $output 'ZISU_V46-03_Browser_Verifier.zip'
Compress-Archive -Path (Join-Path $package '*') -DestinationPath $zip -CompressionLevel Optimal
Write-Output ([ordered]@{ executable = $exe; sha256 = $hash; archive = $zip;
    archive_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant();
    repo_root = $repoRoot } | ConvertTo-Json -Compress)
