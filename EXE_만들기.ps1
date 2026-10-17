# Windows PowerShell; requires local Python 3.10+.
Set-Location $PSScriptRoot
py -3 -m pip install -r requirements-oauth.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed' }
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --collect-all google_auth_oauthlib --name ZisuResearchStudio main.py
if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
Write-Host 'Created dist\ZisuResearchStudio.exe' -ForegroundColor Green
