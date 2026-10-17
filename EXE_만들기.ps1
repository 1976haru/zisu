# Windows PowerShell; requires local Python 3.10+.
Set-Location $PSScriptRoot
py -3 -m pip install -r requirements-oauth.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed' }
if (Test-Path -LiteralPath 'dist\ZisuResearchStudio') {
    throw 'dist\ZisuResearchStudio already exists. Preserve it; choose a fresh workspace or move it yourself before building.'
}
py -3 -m PyInstaller --noconfirm --clean --onedir --windowed --collect-all google_auth_oauthlib --name ZisuResearchStudio main.py
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
$release = 'dist\ZisuResearchStudio'
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $release 'ZisuResearchStudio.exe')).Hash.ToLowerInvariant()
"$hash  ZisuResearchStudio.exe" | Set-Content -Encoding ascii (Join-Path $release 'SHA256SUMS.txt')
Write-Host 'Created dist\ZisuResearchStudio\ZisuResearchStudio.exe and _internal runtime files.' -ForegroundColor Green
Write-Host 'Keep the complete ZisuResearchStudio folder together; do not copy the EXE by itself.' -ForegroundColor Yellow
