param(
    [Parameter(Mandatory=$true)][string]$QaRoot,
    [string]$Worktree = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe,
    [string]$EdgeExe = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    [string]$ReportPath,
    [string]$ExpectedHead = '0e8bf8c36e7b10f0be871fec462a861f462f1209'
)
$ErrorActionPreference = 'Stop'

function Add-Check([string]$Name, [string]$Status, [string]$Detail) {
    $script:checks.Add([pscustomobject]@{name=$Name;status=$Status;detail=$Detail})
}

$checks = [System.Collections.Generic.List[object]]::new()
$root = [IO.Path]::GetFullPath($QaRoot)
$repo = [IO.Path]::GetFullPath($Worktree)
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ([string]::IsNullOrWhiteSpace($PythonExe) -and $pyLauncher) {
    $PythonExe = (& $pyLauncher.Source -3.12 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
}
if ([string]::IsNullOrWhiteSpace($PythonExe)) { throw 'PYTHON312_NOT_FOUND' }
$PythonExe = [IO.Path]::GetFullPath($PythonExe)
$data = Join-Path $root 'data'
$db = Join-Path $data 'zisu_v2.db'
$edgeProfile = Join-Path $root 'browser-profile'

if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw 'QA_ROOT_MISSING' }
if (-not (Test-Path -LiteralPath $repo -PathType Container)) { throw 'WORKTREE_MISSING' }
$gitHead = (& git -C $repo rev-parse HEAD 2>$null | Select-Object -First 1)
$gitStatus = @(& git -C $repo status --short --untracked-files=normal 2>$null)
if ($LASTEXITCODE -eq 0 -and $gitHead -eq $ExpectedHead) {
    Add-Check 'source_revision' 'PASS' ("HEAD=$gitHead; local_changes="+($gitStatus.Count -gt 0)+"; no changes staged by preflight")
} else { Add-Check 'source_revision' 'BLOCKED' '로컬 HEAD가 승인된 기준과 다르거나 Git 확인이 실패했습니다.' }
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) { Add-Check 'python312' 'BLOCKED' '요청한 Python 실행 파일이 없습니다.' }
else {
    $pythonVersion = & $PythonExe -c "import sys; print(sys.version_info.major,sys.version_info.minor,sys.version_info.micro); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $pythonVersion[0] -match '^3 12 ') { Add-Check 'python312' 'PASS' ($pythonVersion -join '; ') }
    else { Add-Check 'python312' 'BLOCKED' 'Python 3.12 실행 또는 버전 확인에 실패했습니다.' }
    $pip = & $PythonExe -m pip --version 2>$null
    $pipVersion = if ($LASTEXITCODE -eq 0) {($pip -join ' ') -replace 'https?://\S+','[URL 숨김]'} else {''}
    if ($LASTEXITCODE -eq 0) { Add-Check 'pip' 'PASS' $pipVersion }
    else { Add-Check 'pip' 'BLOCKED' '선택된 Python에서 pip를 실행할 수 없습니다.' }
    $importProbe = @'
import importlib,sys,tempfile
mods=['google_auth_oauthlib.flow','google.oauth2.credentials','google.auth.transport.requests','requests','tzdata']
missing=[]
for name in mods:
 try: importlib.import_module(name)
 except Exception: missing.append(name)
print('python='+sys.executable)
print('tempdir='+tempfile.gettempdir())
print('imports_missing='+','.join(missing))
try:
 import pathlib
 with tempfile.TemporaryDirectory(prefix='zisu-qa-preflight-') as d:
  p=pathlib.Path(d)/'probe'; p.write_text('ok'); assert p.read_text()=='ok'
 print('tempfile_cleanup=PASS')
except Exception as e:
 print('tempfile_cleanup=FAIL winerror='+str(getattr(e,'winerror','unknown'))+' type='+type(e).__name__)
'@
    $probeOutput = $importProbe | & $PythonExe - 2>$null
    if ($LASTEXITCODE -ne 0) { Add-Check 'oauth_imports' 'BLOCKED' 'Python 의존성 검사 프로세스가 실패했습니다.' }
    else {
        $missingLine = ($probeOutput | Where-Object { $_ -like 'imports_missing=*' } | Select-Object -First 1)
        $missing = $missingLine -replace '^imports_missing=',''
        if ([string]::IsNullOrEmpty($missing)) { Add-Check 'oauth_imports' 'PASS' 'OAuth, requests, tzdata import 성공' }
        else { Add-Check 'oauth_imports' 'BLOCKED' ('누락 모듈: '+$missing) }
        $tempLine = ($probeOutput | Where-Object { $_ -like 'tempfile_cleanup=*' } | Select-Object -First 1)
        $tempStatus = if ($tempLine -like 'tempfile_cleanup=PASS*') {'PASS'} else {'BLOCKED'}
        Add-Check 'python_temp' $tempStatus ($tempLine -replace '^tempfile_cleanup=','')
    }
}

$noIndexProcess = [Environment]::GetEnvironmentVariable('PIP_NO_INDEX','Process')
$noIndexUser = [Environment]::GetEnvironmentVariable('PIP_NO_INDEX','User')
$noIndexMachine = [Environment]::GetEnvironmentVariable('PIP_NO_INDEX','Machine')
$noIndexSource = if ($noIndexProcess -and -not $noIndexUser -and -not $noIndexMachine) {'process/inherited only'} elseif ($noIndexUser) {'user'} elseif ($noIndexMachine) {'machine'} else {'not enabled'}
Add-Check 'pip_no_index' $(if($noIndexProcess -and $noIndexProcess -notmatch '^(0|false|no)$'){'PENDING'}else{'PASS'}) $noIndexSource

if (Test-Path -LiteralPath $data -PathType Container) {
    $probePath = Join-Path $data ('.qa-write-probe-'+[guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($probePath,'probe')
        $ok = [IO.File]::ReadAllText($probePath) -eq 'probe'
        Remove-Item -LiteralPath $probePath -ErrorAction Stop
        Add-Check 'qa_data_acl' $(if($ok){'PASS'}else{'BLOCKED'}) 'QA data 경로 파일 생성/읽기/삭제'
    } catch { Add-Check 'qa_data_acl' 'BLOCKED' 'QA data 경로 쓰기 검증 실패' }
} else { Add-Check 'qa_data_acl' 'BLOCKED' '기존 QA data 디렉터리가 없습니다.' }

if (Test-Path -LiteralPath $db -PathType Leaf) {
    $dbProbe = "import sqlite3,sys; from pathlib import Path; c=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,timeout=2); print(c.execute('pragma integrity_check').fetchone()[0]); c.close()"
    $dbResult = & $PythonExe -c $dbProbe $db 2>$null
    Add-Check 'qa_db_integrity' $(if($LASTEXITCODE -eq 0 -and $dbResult -eq 'ok'){'PASS'}else{'BLOCKED'}) $(if($LASTEXITCODE -eq 0){"integrity_check=$dbResult"}else{'읽기 전용 integrity_check 실패'})
} else { Add-Check 'qa_db_integrity' 'BLOCKED' '기존 QA DB를 찾을 수 없습니다.' }

if (Test-Path -LiteralPath $EdgeExe -PathType Leaf) { Add-Check 'qa_edge_exe' 'PASS' 'Edge 실행 파일 존재' } else { Add-Check 'qa_edge_exe' 'BLOCKED' '격리 Edge 실행 파일을 찾을 수 없습니다.' }
if (Test-Path -LiteralPath $edgeProfile -PathType Container) { Add-Check 'qa_edge_profile' 'PASS' '기존 QA 전용 Edge 프로필 존재' } else { Add-Check 'qa_edge_profile' 'PENDING' 'QA Edge 프로필 디렉터리가 아직 없습니다.' }

$clientIndex = Join-Path $data 'oauth_clients\index.json'
$clientCount = 0
if (Test-Path -LiteralPath $clientIndex -PathType Leaf) {
    try { $clientCount = @((Get-Content -LiteralPath $clientIndex -Raw | ConvertFrom-Json)).Count } catch { Add-Check 'oauth_client_registry' 'BLOCKED' 'OAuth 등록 인덱스를 안전하게 읽을 수 없습니다.' }
}
if (-not ($checks | Where-Object {$_.name -eq 'oauth_client_registry' -and $_.status -eq 'BLOCKED'})) {
    Add-Check 'oauth_client_registry' $(if($clientCount -gt 0){'PASS'}else{'PENDING'}) ("등록 항목 수=$clientCount; 비밀정보는 읽거나 출력하지 않음")
}

try {
    $productWindows = @(Get-Process -ErrorAction Stop | Where-Object {$_.MainWindowTitle -like 'ZISU SIGNAL LAB*'})
    $windowSummary = @($productWindows | ForEach-Object {"PID=$($_.Id); path=$($_.Path); started=$($_.StartTime.ToUniversalTime().ToString('o'))"})
    Add-Check 'existing_gui' $(if($productWindows.Count -eq 0){'PASS'}else{'PENDING'}) $(if($productWindows.Count -eq 0){'동일 제품 창 없음'}else{($windowSummary -join ' | ')+'; 기존 프로세스는 종료하지 않음'})
} catch { Add-Check 'existing_gui' 'BLOCKED' '기존 GUI 프로세스 소유권 확인 실패; 중복 실행 금지' }

$pipNoIndex = [bool]($noIndexProcess -and $noIndexProcess -notmatch '^(0|false|no)$')
$wheelCount = 0
foreach ($candidate in @((Join-Path $repo 'wheelhouse'),(Join-Path $root 'wheelhouse'))) {
    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $wheelCount += @(Get-ChildItem -LiteralPath $candidate -Filter '*.whl' -File -ErrorAction SilentlyContinue).Count
    }
}
$cacheMatches = 0
$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher) {
    $cacheRows = & $pyLauncher.Source -3.12 -m pip cache list 2>$null
    if ($LASTEXITCODE -eq 0) { $cacheMatches = @($cacheRows | Where-Object {$_ -match '(?i)google.auth|oauthlib|requests|tzdata'}).Count }
}
$supplyStatus = if ($wheelCount -gt 0 -or $cacheMatches -gt 0) {'PASS'} elseif ($pipNoIndex) {'BLOCKED'} else {'PENDING'}
Add-Check 'approved_package_source' $supplyStatus ("QA/repo wheel 수=$wheelCount; Python 3.12 pip cache 일치 항목 수=$cacheMatches; 인덱스 우회 없음")

$pipNoIndex = [bool]($noIndexProcess -and $noIndexProcess -notmatch '^(0|false|no)$')
$missingCritical = @($checks | Where-Object {$_.status -eq 'BLOCKED'}).Count
$pending = @($checks | Where-Object {$_.status -eq 'PENDING'}).Count
$result = if($missingCritical -gt 0){'BLOCKED'}elseif($pending -gt 0){'PENDING'}else{'READY'}
$userProfile = [Environment]::GetFolderPath('UserProfile')
$roaming = [Environment]::GetFolderPath('ApplicationData')
$pythonPrefix = Split-Path -Parent (Split-Path -Parent $PythonExe)
$configPaths = @((Join-Path $env:ProgramData 'pip\pip.ini'),(Join-Path $pythonPrefix 'pip.ini'),(Join-Path $userProfile 'pip\pip.ini'),(Join-Path $roaming 'pip\pip.ini'))
$configFiles = $configPaths | Select-Object -Unique | ForEach-Object { [pscustomobject]@{path=$_;exists=(Test-Path -LiteralPath $_ -PathType Leaf)} }
$tempEnv = [pscustomobject]@{TEMP=[Environment]::GetEnvironmentVariable('TEMP','Process');TMP=[Environment]::GetEnvironmentVariable('TMP','Process');TMPDIR=[Environment]::GetEnvironmentVariable('TMPDIR','Process')}
$actualTemp = ($probeOutput | Where-Object {$_ -like 'tempdir=*'} | Select-Object -First 1) -replace '^tempdir=',''
$report = [pscustomobject]@{result=$result;python_exe=$PythonExe;python_version=($pythonVersion -join '; ');pip_version=$pipVersion;worktree=$repo;qa_root=$root;temp_environment=$tempEnv;python_tempfile_dir=$actualTemp;pip_no_index_process=$pipNoIndex;pip_no_index_user=[bool]$noIndexUser;pip_no_index_machine=[bool]$noIndexMachine;pip_no_index_source=$noIndexSource;pip_config_files=$configFiles;checks=$checks}
$json = $report | ConvertTo-Json -Depth 8
if ($ReportPath) {
    $fullReport = [IO.Path]::GetFullPath($ReportPath)
    if (-not $fullReport.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)) { throw 'REPORT_MUST_STAY_IN_QA_ROOT' }
    $stream = [IO.File]::Open($fullReport,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
}
$json
