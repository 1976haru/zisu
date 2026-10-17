# 복사본 설치: D:\03 zisu · 기존 파일을 백업하고 Git 정보는 건드리지 않습니다.
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$target = 'D:\03 zisu'
if (-not (Test-Path 'D:\')) { throw 'D: 드라이브가 없습니다. 드라이브 문자를 확인해 주세요.' }
if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
$backup = Join-Path $target ("_backup_before_v3_" + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$items = @('mcrs', 'tests', 'docs', 'main.py', '실행하기.bat', 'EXE_만들기.ps1', 'requirements-oauth.txt', 'OAuth_모듈_설치하기.bat', 'START_ZISU.cmd', '오류진단하기.bat', '설치안내_문제해결.html', 'README.md', 'AGENTS.md', 'CLAUDE.md', '사용설명서_초보자.html', '.gitignore', '.github')
foreach ($item in $items) {
    $from = Join-Path $source $item
    $to = Join-Path $target $item
    if (-not (Test-Path $from)) { continue }
    if (Test-Path $to) {
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        Move-Item -LiteralPath $to -Destination (Join-Path $backup $item)
    }
    Copy-Item -LiteralPath $from -Destination $to -Recurse
}
Write-Host "V3 소스 설치 완료: $target" -ForegroundColor Green
if (Test-Path $backup) { Write-Host "교체 전 원본 백업: $backup" }
Write-Host "GitHub 연결 여부: " -NoNewline
if (Test-Path (Join-Path $target '.git')) { Write-Host 'Git 폴더 보존됨' } else { Write-Host '로컬 Git 미연결 (아래 가이드를 따라 연결)' }
Write-Host "실행은 '실행하기.bat'를 더블클릭하세요."
Read-Host '엔터 키로 닫기'
