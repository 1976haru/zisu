# ZISU V4.4.1 Windows 시작 안정화 / 상태 보고 (2026-10-17)

## 최신 상태: V4.4.2 오프라인 배포 검증 (2026-10-17)

아래 V4.4.1 및 이전 절의 상태는 과거 시점 기록이며 현재 작업의 게이트는 아닙니다.
V4.4.2는 ZIP/EXE 해시와 `_internal` 구조 확인, 격리 데이터 경로에서 GUI 정상 실행,
테스트 프로필 생성·전환·검색어 분리, 복사 및 이동 후 실행을 수행하는 PowerShell 도구를
추가합니다. GitHub Actions 아티팩트의 sidecar `RELEASE_SHA256SUMS.txt`가 ZIP 기준이고,
배포 ZIP 내부 `SHA256SUMS.txt`가 EXE 기준입니다. EXE에는 Python/OAuth 모듈이 포함되므로
별도 설치가 필요 없습니다. `OAuth_모듈_설치하기.bat`은 소스 실행에만 해당합니다.

Windows Sandbox 실행 파일이 있고 활성화된 시스템에서는 `검증하기.cmd`가 Sandbox 구성을
생성해 실행하며, 네트워크와 클립보드를 끄고 package 입력을 read-only로 매핑합니다.
Sandbox가 없는 PC에서는 Python이 설치되어 있더라도 PATH를 자식 프로세스에서 제거한
오프라인 검증이 가능하지만, 이를 Python 미설치 독립 PC 검증이라고 부르지 않습니다.

- Actions run `39018125751`의 ZIP/EXE SHA-256: `2ded8196e9800c096a75b9275080f95fa006db40cf7f3d10745cd1d54106f549` / `2cf1b7b05747525d5760c49fb8ca1ddca583f088237ddd0b56c6f18ba80785ea`.
- Windows 10 Pro 22H2 build `19045.4780`: fresh-path GUI-ready 1024.3/974.4/963.3 ms, warm 817.9/763.2/670.9 ms; six exits 1163/1075.1/1060.4/931.6/889.8/769.4 ms, residual processes 0. Six profile create/switch/isolation checks and DB quick_check passed; validation took 24.26 seconds.
- Host Python 3.14.3 is installed (child EXE PATH was removed); this is not an independent Python-free PC test. User DB metadata stayed unchanged. Windows Sandbox was not installed/resolvable; DISM status query required elevation, so feature state was not changed. `.wsb` generation/XML validation passed but Sandbox VM execution remains unverified.
- Gate: 55/55 unit/integration, 216 genre boundaries, 100 profiles/10,000 candidate stress, GUI smoke, compile/ruff, ZIP and EXE hashes passed. V4.4.2 Windows 10 Offline RC candidate only; no Windows 11, real OAuth/API, or real-audio accuracy claim.

## V4.4.1 최종 오프라인 검증 기록 (역사 기록, 2026-10-17)

- 원본 V4.4 onefile ZIP은 Windows 10에서 40초 대기 후에도 Python 시작 보고서/격리 DB를 만들지 못했습니다. 관찰된 창은 Tk 창이 아니었고 `_MEI*` 추출 경로가 비어 있어 실패 구간을 Python 진입 전 onefile 부트로더로 좁혔습니다. Application Error/WER 및 Defender Operational에서 대응 차단 증거는 발견하지 못했습니다. 따라서 한 단계 더 구체적인 근본 트리거는 미확정이며, 보안 설정을 변경하지 않았습니다.
- Actions run `39015700620`에서 다운로드해 검증한 V4.4.1 onedir ZIP/EXE SHA-256: `e4eed1d31f84b4c7971759c1fdb9f250f65e0166e80ade9d9ad1b159a211b062` / `53a13d722945ca53ca6abc8500fa58e7a38ce909453f0505deaf3057e4739724`.
- Windows 10 Pro 22H2 빌드 `19045.4780`, Python PATH 제거, 격리 DB 및 한글/일본어/공백 경로: fresh-path 3회 GUI-ready 918/899.5/881.1 ms, warm 3회 899.8/599.9/624.9 ms. 종료 985.3/954.7/963.2/951.7/654.6/696.2 ms; 잔류 EXE 프로세스 0회. 사용자 DB mtime 전후 동일.
- Actions Windows Server 2025 빌드 `26100.33438`에서도 onedir GUI-ready는 fresh-path 907.1/687.0/708.0 ms, warm 569.6/579.3/585.7 ms였습니다. CI onefile 대조군은 fresh-path 3319.1/1991.1/2127.8 ms 및 warm 2116.1/2161.8/1938.5 ms였습니다. 경로 새 복사는 디스크 캐시 cold-start를 뜻하지 않습니다.
- 상태: V4.4.1 Offline RC 코드 게이트 통과. 실제 Python 미설치 PC, Windows 11, Google OAuth/YouTube, 실음원 장르 정밀도는 여전히 미검증이며 별도 최종 검증 항목입니다.

## V4.4.1 시간 초과 원인 조사 (완료; 2026-10-17)

- 재현 ZIP SHA-256: `8793e4fa5807b2c543ff5b7e503d0faf608f1ea0fa858bd23ff92bcd8f90fd77`; EXE SHA-256: `0becbea7953eacae293e75aa5d720ffc728faeedd6e5613b2a8f48700311eb4`.
- 재현 PC: Windows 10 Pro 22H2, OS 빌드 `19045.4780`; Python 3.14.3 설치 상태.
- 격리 데이터 경로와 한글/일본어/공백이 포함된 ZIP 해제 경로에서 40초간 실행했지만 프로세스가 종료되지 않았고, 단계 보고서 및 격리 DB 파일은 생성되지 않았습니다. 기존 사용자 DB의 파일 시각은 그대로였습니다.
- 이전 스모크의 `MainWindowHandle != 0`은 Tkinter 창 증거가 아니었습니다. 실제 창 클래스는 `UAC_InputIndicatorOverlayWnd`로, GUI 초기화로 잘못 판정하지 않도록 수정합니다.
- 정확한 ZIP EXE는 프로세스로 생성되었지만 PyInstaller `_MEI*` 압축 해제 경로가 비어 있었고, 같은 이미지의 자식 프로세스가 보이지 않았습니다. 따라서 확인된 실패 지점은 Python 엔트리포인트 이전의 onefile 부트스트랩 단계입니다.
- 최근 Application Error/Windows Error Reporting에서 ZISU 프로세스 오류는, Windows Defender Operational에서 해당 EXE 차단 이벤트는 찾지 못했습니다. 관리형 실행 환경의 제한인지 OS/보안 제품 원인인지는 현재 증거만으로 확정할 수 없습니다.
- 빌드 옵션 비교를 위해 CI에서 onefile을 대조군으로 실행하고, 배포본은 onedir ZIP으로 생성하도록 변경했습니다. 단계별 Python 진입, 모듈 import, Tk 루트, DB 초기화, 위젯 완료, GUI 준비 및 종료 시간을 기록합니다.
- 로컬 Python 3.14에는 PyInstaller와 OAuth 패키지가 없어 독립 로컬 빌드는 불가능했습니다. 대신 최종 CI 산출물을 Windows 10에서 직접 검증했습니다(상세 결과는 위 최종 오프라인 검증 정정 참조). Python 미설치 독립 PC와 Windows 11은 별도 미검증입니다.

## V4.4 최종 배포 안정화

- 빌드 EXE를 격리 데이터 폴더와 Python이 제외된 PATH에서 직접 실행하여 GUI 초기화·프로세스 정상 종료를 검증합니다.
- 배포 ZIP은 EXE, EXE SHA-256, 초보자 설명서, 빠른 시작 파일만 허용하며 민감 파일명을 자동 차단합니다.
- ZIP을 한글·일본어·공백 경로에 다시 풀어 해시와 실행을 확인하고 실제 Windows 제품명·버전·빌드를 기록합니다.
- Python 미설치 독립 Windows PC는 별도 실기기 게이트로 유지합니다.

### V4.4 검증 결과 (2026-10-17)

- 최신 커밋 `ecf9faa58e6621c9804a88311aa63452306dfc45`: Windows push/PR CI 성공.
- CI에서 Python 경로를 제거한 상태로 복사된 EXE의 GUI 초기화·정상 종료, 격리 데이터 폴더 생성, ZIP 4개 파일 검사와 OAuth 패키지 포함 검사가 통과했습니다.
- 배포 EXE SHA-256: `087497963462af0d8c209ded35c20e6b3c1fdc7809787aaeed4a882c066eb93f`.
- 배포 ZIP SHA-256: `79384e0b9bedfbd307d21c7b7b366ac88f11e71b666597158218522585be290f`.
- 개발자 PC는 Windows 10 Pro 22H2, 빌드 19045.4780이며 Python 3.14.3이 설치되어 있습니다. (이전 문서의 “GUI 창이 생김” 표기는 오판이었습니다. 위 V4.4.1 조사 결과에서 바로잡았습니다.) 실제 Windows Sandbox 기능 확인은 관리자 권한을 요구해 수행하지 못했습니다.
- CI의 Python PATH 제거 EXE 스모크, 실제 Windows 10 onedir 실행, Python 미설치 독립 PC 검증은 별도 항목입니다. 전자는 통과했고, 후자는 미검증입니다.
- GUI 회귀 스모크는 검색 중 KR 프로필로 전환해도 완료된 후보가 원래 JP 프로필에만 저장되는지 확인합니다.

## V4.3 오프라인 최종 강화

- 100개 가상 프로필과 합성 후보 10,000건으로 격리·중복요청·동시 읽기/쓰기·재시작·반복 마이그레이션·백업 복원 일치를 측정합니다.
- KR/JP 및 6개 장르에 대해 적합/검토/제외가 균형인 합성 경계 메타데이터 216건을 회귀 검증합니다.
- 네트워크·429·5xx만 제한적으로 재시도하고 인증·할당량 오류는 즉시 안내합니다.
- 실제 OAuth, 실제 YouTube 검색/청취 라벨, Python 미설치 PC 실행은 코드 기반 RC와 분리된 최종 실사용 게이트입니다.

## V4.2 자동화 완성 작업

- OAuth: PKCE, 만료·취소 상태, 채널 없는 계정, 재연결 실패 복구를 Mock으로 검증.
- 다채널: 하위 장르와 프로필별 검색어를 추가하고 OAuth·후보·메모·성과 격리를 강제.
- 장르 품질: 제외 후보 감사 테이블, 구조화된 긍정/충돌/불확실성 근거,
  사람 정답 라벨 화면, 추천 정밀도와 제외 오류율 계산을 추가.
- 스트레스: 한글·일본어·공백 경로, 20개 프로필, 반복 마이그레이션,
  동시 읽기, 합성 메타데이터 Fixture를 검증.
- 실제 Google OAuth와 실제 음원 정답 라벨은 계속 미검증으로 구분.

## V4 통합 작업 기록 (2026-10-17)

- 안전 백업: V3 소스와 `%LOCALAPPDATA%\ZisuResearchStudio\zisu_v2.db`를
  `_local_backups/`에 복사하고 SHA-256을 확인했습니다. 이 경로는 Git에서 제외됩니다.
- 기능 브랜치: `feature/v4-multichannel-onboarding-genre`.
- 완료: 첫 실행 5단계 안내, 채널 없는 조사 계정 지원, 프로필별 OAuth 토큰 경로,
  6개 장르 KR/JP 독립 검색어, 인접 장르 검토 보류, V2 사본 마이그레이션 보존.
- 자동화 검증: 28개 단위 테스트 통과, Windows Tkinter GUI 스모크 통과,
  실제 V2 DB 백업 사본의 행 수 보존 확인.
- 미검증: 실제 Google OAuth, 실제 KR/JP 영상 표본 정밀도, Python 미설치 PC의 V4 EXE.
- 빌드 차단: 현재 로컬 Python 3.14 환경에 PyInstaller가 설치되어 있지 않습니다.

## 정의
- **프로젝트 전체 진행률 = 추정치**. 점수처럼 표시해도 시험 완료를 뜻하지 않음.
- **검증된 완료**: 단위 테스트와 GUI smoke 성공, SQLite 이전버전 마이그레이션 테스트, OAuth 가짜 응답 테스트.
- **검증 안 됨**: 실제 Morning Chill Google 계정 OAuth 승인·YouTube API 호출, Windows 10/11 PC 구동 및 EXE 빌드, 친구 대상 설치 테스트.

## 단계와 우선순위
1. P0 사용자 PC에서 V3 GUI 및 기존 DB 업데이트 후 백업 검증
2. P0 실제 새 Morning Chill Google 계정: Cloud desktop client 설정, OAuth 연결, Google 인증된 이메일 확인, API 검색 성공
3. P0 여러 Google 계정 / JP-KR 동시 운영 시험; 연결 해제, 재인증, 토큰 갱신 경계 테스트
4. P0 일본/한국 실제 영상 100~200건 기반 장르 오판정 점검, 필터 개선 (장르 혼합 방지 최우선)
5. P1 GitHub `1976haru/zisu` 기능 브랜치로 소스 동기화, 코드 리뷰 및 CI 실행
6. P1 Windows 11 EXE 빌드 및 초보 사용자 2명 이상 사용성 테스트
7. P1 사용자 친화형 Google Cloud OAuth 설정/오류 처리 간소화, 도움말·백업 복원 기능 개선
8. P2 YouTube Analytics API 읽기 전용 연동으로 JP/KR 조회수와 실제 유입 자동 분석 (추가 API/Scope 필요)
9. P2 검색 예약 독립 서비스화, 종료 상태에서도 예약 실행(Windows 작업 스케줄러)

## 차단/제외
- 조회수 부풀리기를 위한 자동·반복 재생, 무인 댓글·좋아요·구독 매크로는 YouTube 참여 조작 정책상 구현 대상 아님.
- 개인 추천 홈피드 재현은 YouTube Data API에 동일 기능이 없으며 API 읽기 권한만으로 만들 수 없음.

## 보고 형식
모든 개발 보고에 전체 프로젝트 진행률(추정·검증 구분) / 지금 작업 중 / 완료 검증 / 남은 P0·P1·P2 / 다음 행동 한 개를 포함.
