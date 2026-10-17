# Windows + GitHub + Claude Code / Codex

대상 GitHub: https://github.com/1976haru/zisu/
로컬 경로: `D:\03 zisu`

## 1. GitHub repo를 처음 복제할 때 (빈 경로 필요)

PowerShell에서 다음을 실행하세요.

```powershell
git clone https://github.com/1976haru/zisu.git "D:\03 zisu"
```

`D:\03 zisu`가 이미 존재해 다른 파일이 있으면 `git clone` 대신 아래를 검토합니다. 절대로 기존 파일을 삭제하거나 `git init`을 중복 실행하지 마세요.

```powershell
Set-Location 'D:\03 zisu'
git status
```

`not a git repository`라고 나오면 기존 파일을 보존한 뒤 별도 폴더에 clone하고 비교 후 합치세요. `로컬설치하기.ps1`은 기존 파일을 백업하지만 Git repo를 자동 연결하지는 않습니다.

## 2. 소스 배치

V2 ZIP을 압축 해제하고 `로컬설치하기.ps1`을 실행하거나, 압축을 푼 파일을 GitHub 로컬 clone에 직접 복사하세요. 소스가 `D:\03 zisu\main.py`와 `D:\03 zisu\mcrs\`에 있어야 합니다.

## 3. 테스트 / 커밋

```powershell
cd 'D:\03 zisu'
py -3 -m unittest discover -s tests -p 'test_*.py' -v
py -3 main.py
git status
git switch -c feature/zisu-v2-multichannel
git add .gitignore .github AGENTS.md CLAUDE.md README.md main.py mcrs tests docs '실행하기.bat' '로컬설치하기.ps1' 'EXE_만들기.ps1' '사용설명서_초보자.html'
git commit -m "feat: KR/JP multichannel genre-safe research studio v2"
git push -u origin feature/zisu-v2-multichannel
```

그다음 GitHub에서 `Compare & pull request`를 눌러 검토용 PR을 만드세요. GitHub Actions의 `Windows build and tests`가 통과하면 `ZisuResearchStudio-Windows` 아티팩트를 다운로드할 수 있습니다. Windows 실사용 검증 전에는 main 병합을 권하지 않습니다.

## 4. Claude Code / Codex 공통 첫 지시

```text
D:\03 zisu 프로젝트를 읽고 AGENTS.md, CLAUDE.md, docs/ROADMAP.md 지침을 따르세요.
현재 코드를 먼저 검증하고 Windows GUI / EXE / 크롬 브라우저 검색 경로를 테스트하세요.
제일 중요한 요구사항은 JP/KR 다채널의 완전 분리와 세부 장르 혼합 방지입니다.
YouTube 자동 댓글·좋아요·구독·반복 자동재생 등 인위적 참여 기능은 추가하지 마세요.
테스트 없이 완료를 주장하지 말고, 변경 내용을 작은 커밋으로 관리하세요.
보고에는 전체 진행률(검증된 부분과 추정), 현재 상태, 완료, P0/P1/P2 남은 과제, 다음 행동 한 개를 포함하세요.
```
