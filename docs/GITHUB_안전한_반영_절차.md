# ZISU GitHub 연동 · 사용자 PC 작업 경로

저장소: https://github.com/1976haru/zisu/ · 지정 로컬 경로: `D:\03 zisu`

이 ZIP은 사용자의 PC에 자동 접근하지 않습니다. `D:\03 zisu`에 설치할 때는 **V2.1을 백업**하고, 이어서 GitHub의 `feature/` 브랜치에서 테스트하는 방식을 권장합니다.

1. V3 소스 ZIP을 새 폴더에 전체 압축 해제.
2. `로컬설치하기.ps1`을 PowerShell에서 실행해 `D:\03 zisu`에 복사 (동명 기존 파일은 `_backup_before_v3_...`로 이동, `.git`은 보존). 실행 전 파일 내용을 확인하세요.
3. 개발 도구(GitHub Desktop 또는 Git)가 있다면 `D:\03 zisu`를 로컬 저장소로 열어 Git remote가 `https://github.com/1976haru/zisu.git`인지 확인합니다. 기존 Git 프로젝트라면 절대 `git init`, `git push --force`를 실행하지 마세요.
4. 기존 repo가 없다면 **GitHub Desktop**에서 `1976haru/zisu`를 별도의 신규 폴더에 Clone하고, V3 소스를 `.git` 폴더를 건드리지 않고 복사합니다. 기존 D 경로 파일을 먼저 백업하세요.
5. 새 feature 브랜치 `feature/v3-oauth-onboarding`를 만들고 단위 테스트 실행 후 변경 파일을 커밋합니다.
6. GitHub Push 후 Pull Request를 통해 `main`으로 병합합니다. GitHub Actions Windows EXE 빌드 성공 여부를 확인합니다.
7. **업로드 금지**: OAuth JSON/액세스 토큰, 사용자 데이터베이스, 개인 이메일, 로그, 개인 로컬 백업.

아직 이 답변만으로 GitHub 저장소에 소스가 업로드되거나 사용자의 D 드라이브가 변경된 것은 아닙니다.
