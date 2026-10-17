# ZISU Research Studio V4.5.3 — AI Research Manager 호환성 강화

V4 기능 브랜치는 첫 실행 시작 마법사, 프로필별 Google 읽기 전용 OAuth,
KR/JP 독립 검색어, 6개 장르의 `적합 / 검토 / 제외` 보수 판정과 V2 SQLite
무손실 마이그레이션을 제공합니다. 검색 지역은 채널 운영자의 국적을 증명하지
않으며, 애매하거나 인접 장르 신호가 있는 영상은 자동 승인하지 않습니다.

V4.4는 제외된 영상도 별도 장르 감사 표본으로 보존합니다. 운영자가 실제로 듣고
`적합 / 제외 / 불확실` 정답을 기록하면 추천 정밀도와 제외 오류율을 계산합니다.
표본이 충분하지 않으면 95% 정밀도 달성을 주장하지 않습니다.

> 검증 상태: V4.4의 Windows 10 시간 초과를 재현해 GUI 창 핸들 오탐을 확인했습니다. 실행 프로세스는 Tkinter 창이 아니라 Windows `UAC_InputIndicatorOverlayWnd`만 보였고, Python 시작 보고서가 없었습니다. onefile 압축 해제 폴더가 비어 있고 자식 프로세스가 확인되지 않아 실제 Python/GUI 코드 진입 전 부트로더 단계에서 멈춘 사실까지 확인했습니다. 보안 로그에서 차단 원인은 확인되지 않았습니다. V4.4.1은 onedir 배포를 비교·검증하고 단계별 시작 시간을 기록합니다.

**Windows 10/11용** · 한국/일본 · Chill Rap / Morning Chill Rap 등 장르 분리 · Google 읽기 전용 연결

이 프로젝트는 장르별 유튜브 음악 채널을 찾아 분석하고, 내가 실제로 감상한 영상에 대해 댓글 초안을 작성하며, 채널별 데이터를 분리 관리하기 위한 도구입니다. **자동 조회수 조작, 자동 반복 재생, 자동 댓글/좋아요/구독 게시 기능은 포함하지 않습니다.** 자동 검색은 공식 YouTube Data API 읽기 전용으로만 수행합니다.

## 가장 먼저 할 일

1. 이미 작동하는 **V2.1은 지우지 마세요.** V4.4.2 배포 ZIP을 별도 폴더에 **전체 압축 해제**합니다. EXE와 `_internal` 런타임 폴더를 분리하거나 EXE만 복사하지 마세요. EXE 배포판은 Python 설치가 필요 없습니다.
2. 첫 번째 탭 **① 계정 연결**에서 대상 작업 프로필을 확인합니다. **설정 없이 바로 쓰고 싶다면 'Google Cloud 없이 먼저 유튜브 열기'** 버튼으로 브라우저에 로그인한 후 수동 검색할 수 있습니다. 이 모드는 앱 OAuth 연결은 아닙니다. 기존 V2 DB가 있으면 그대로 읽고 필요한 연결 필드만 자동 추가합니다.
3. 배포 EXE에는 Google OAuth 모듈이 함께 포함되어 있어 별도 모듈 설치가 필요 없습니다. `OAuth_모듈_설치하기.bat`은 Python으로 소스를 실행하는 개발/소스 설치용이며, EXE에서는 실행하지 마세요.
4. 실제 Google Cloud 설정은 이번 오프라인 패키지 검증과 별개이며, 마지막 실전 검증 단계에서 진행합니다. 그때 `YouTube Data API v3` 및 데스크톱 OAuth를 구성하고 테스트 모드라면 Morning Chill 계정을 테스트 사용자로 등록합니다.
5. 앱에서 **[1. Google Cloud OAuth JSON 선택] → [2. 새 계정으로 Google 로그인 및 연결]**을 선택합니다. 브라우저에서 **새 Morning Chill Rap Google 계정**을 선택한 뒤 실제 이메일/채널명을 확인하여 승인합니다.
6. `② 장르별 채널 검색`에서 `JP / Morning Chill Rap` 전용 검색어를 사용합니다. 계정 연결이 완료되면 API 키를 별도로 입력하지 않고도 OAuth 읽기 권한으로 검색할 수 있습니다. 연결 없이 웹 검색도 가능합니다.

> **특히 중요:** 앱의 Google 계정 연결은 **유튜브 홈 추천·IP·국가 시청자 비율·'지수'를 바꾸지 않습니다.** 공식 API를 통해 **해당 계정에 읽기 전용으로 접근하고, 작업 프로필을 식별하기 위한 기능**입니다. API `regionCode=JP`는 일본에서 볼 수 있는 결과이며 일본 운영 채널 인증은 아닙니다.

## 계정/권한/보안

- Google OAuth 사용자는 본인 브라우저에서 로그인합니다. 프로그램이 비밀번호를 입력받거나 읽지 않습니다.
- 요청 범위: `youtube.readonly`, `openid`, `email`. 조회/계정 확인만 허용하며 **댓글·좋아요·구독을 수정할 권한 없음**.
- 토큰은 **각 프로필별로 PC의 `%LOCALAPPDATA%\ZisuResearchStudio\oauth_tokens\`**에 보관합니다. `D:\03 zisu` 소스/ZIP/GitHub에는 포함하지 않습니다. 여러 채널 간 파일이 구분됩니다.
- 앱에서 연결 해제하면 해당 프로필의 로컬 토큰이 지워집니다. **Google 계정의 앱 접근권한까지 철회하려면 Google 계정의 서드파티 연결 관리에서도 취소**해야 합니다.
- V2 DB는 `%LOCALAPPDATA%\ZisuResearchStudio\zisu_v2.db`에 그대로 두고 컬럼만 추가합니다. 기존 데이터 보존 테스트가 있습니다. 그래도 최초 사용 전 앱의 백업 메뉴를 사용하세요.
- 처음 Google Cloud **테스트 모드**에서 사용 시 테스트 사용자 계정 등록이 필요합니다. OAuth 승인·갱신에는 Google의 테스트 앱 정책이 적용될 수 있습니다.
- **지인 배포**: 각 사용자가 자기 Google Cloud OAuth 앱의 데스크톱 JSON을 생성하는 것이 권장됩니다. 개발자 계정의 개인 JSON, 실제 OAuth 토큰은 공유하지 마세요. 널리 배포하려면 동의 화면 검증 등 별도 작업이 필요할 수 있습니다.

## 주요 기능

| 기능 | V4.4.2 상태 |
|---|---|
| JP/KR 다채널 프로필 분리 | 구현 |
| 프로필별 Google OAuth 계정 연결·해제 | 구현 / 실제 Google 승인 미검증 |
| 새 계정에 YouTube 채널이 없는 조사 전용 모드 | 구현 / 실제 Google 승인 미검증 |
| 계정 이메일·채널 ID 확인 | 구현 / 실제 API 미검증 |
| OAuth 또는 별도 API 키로 조회 전용 검색 | 구현 / 실제 API 미검증 |
| 총 6개 장르 × 2개 시장 × 12개 전용 키워드 = 144개 | 구현 |
| 충돌 장르 제외, 불확실한 후보 보류, 수동 승인 기록 | 구현 |
| 긍정·충돌·불확실성 판정 근거 및 사람 정답 라벨 | 구현 / 실제 음원 표본 미검증 |
| 추천 정밀도·제외 오류율 계산 | 구현 / 라벨 표본 필요 |
| 10/15/20분 감상 분석 알림 및 실제 청취 메모 | 구현 |
| 국가별 지표 수동 입력, CSV/DB 백업 | 구현 |
| 앱 실행 중 예약 검색 | 구현 (로그인 연결 또는 별도 API 키 필요) |
| 반복 자동재생/자동 댓글·좋아요·구독 | 구현하지 않음 (정책 리스크) |
| Python 설치가 필요 없는 onedir EXE | Windows 10에서 Python PATH 제거 검증 통과 / Python 미설치 독립 PC는 별도 확인 |

## Windows 개발/실행

- `실행하기.bat` : 기존 Python으로 실행 (Python 3.10 이상 필요)
- `OAuth_모듈_설치하기.bat` : **Python 소스 실행 전용**, EXE 배포판에는 불필요
- `오류진단하기.bat` : 에러 메시지 저장
- `EXE_만들기.ps1` : Windows에서 PyInstaller EXE 생성
- `로컬설치하기.ps1` : `D:\03 zisu`로 소스 복사(이전 프로그램 폴더 백업, Git 파일 보존)
- `사용설명서_초보자.html` : 사용자 가이드
- `.github/workflows/windows-build.yml` : CI 테스트와 EXE 빌드 작업 정의

단위 테스트: `python -m unittest discover -s tests -p 'test_*.py' -v`
독립 스트레스 게이트: `python tests/stress_v43.py` (100개 프로필, 합성 후보 10,000건)
GUI 스모크: `xvfb-run -a python tests/smoke_gui.py` (Linux 테스트 환경)

`오류진단하기.bat`은 OAuth 모듈 포함 여부, DB 무결성 및 테이블별 행 수만 기록합니다.
이메일·채널 ID·토큰 값·DB 경로는 진단 보고서에 기록하지 않습니다.

## V4.4.1 검증 기준선 (이전 버전, 2026-10-17)

CI 배포 ZIP에는 onedir EXE와 동봉 런타임 `_internal` 폴더, `SHA256SUMS.txt`,
초보자 설명서와 빠른 시작 안내만 포함합니다. 소스, DB, OAuth JSON, 토큰, 로그와
백업은 포함하지 않습니다. CI는 onefile과 onedir의 시작·종료 시간을 같은 Windows
러너에서 비교하고, 배포 ZIP을 한글·일본어·공백이 있는 경로에 풀어 Python 경로를
제거한 상태로 GUI 초기화와 정상 종료를 검증합니다. Windows 10 Pro 22H2
(19045.4780)에서도 최종 CI 산출물의 해시를 확인하고 3회 fresh-path 및 3회 warm
실행을 완료했습니다. onedir GUI 준비 시간은 각각 972.2/931.3/919.3 ms 및
599.1/599.0/599.6 ms였고 6회 모두 정상 종료되어 EXE 잔류 프로세스가 없었습니다.
V4.4.1 Offline RC 코드 검증 게이트는 통과했습니다. 이는 Python 미설치 독립 PC,
Windows 11, 실제 OAuth/API 및 실제 음악 분류 검증을 의미하지 않습니다. fresh-path는
새 복사 경로 실행이며 OS 디스크 캐시를 비운 cold-disk 실행은 아닙니다.

> V4.4.2 Windows 배포 아티팩트는 GitHub Actions에서 생성됩니다. 사용자 PC의 DB, 토큰, OAuth JSON 또는 로컬 로그는 업로드하지 않습니다.

## 정책·공식 문서

- https://developers.google.com/youtube/data/guides/auth/installed-apps
- https://developers.google.com/youtube/v3/docs/search/list
- https://support.google.com/youtube/answer/3399767

## V4.4.1 실제 Windows 10 기준선의 해시 및 측정값

정확한 GitHub Actions onedir ZIP을 Windows 10 Pro 22H2 (OS 빌드 19045.4780)에서
검증했습니다 (Actions run `39015700620`). ZIP SHA-256은 `e4eed1d31f84b4c7971759c1fdb9f250f65e0166e80ade9d9ad1b159a211b062`,
EXE SHA-256은 `53a13d722945ca53ca6abc8500fa58e7a38ce909453f0505deaf3057e4739724`입니다.
Python PATH를 제거하고 한글·일본어·공백 경로에서 GUI 준비는 fresh-path 918/899.5/881.1 ms,
warm 899.8/599.9/624.9 ms였습니다. 6회 전부 GUI 준비, 정상 종료, 잔류 프로세스 0건을
확인했습니다. 여기서 fresh-path는 새 복사 경로 실행이며 디스크 캐시를 비운 cold boot가 아닙니다.
오프라인 코드 검증 게이트는 통과했으나, Python 미설치 독립 PC, Windows 11, 실제 OAuth/API,
실제 음원 장르 정밀도는 미검증입니다. 기존 onefile 시간 초과는 Python 진입 전 부트로더 단계에서
발생한 사실까지 확인했으며, 해당 환경에서의 더 근본적인 트리거는 로그 증거가 없어 미확정입니다.

## V4.4.2 독립 배포 검증 방법

GitHub Actions 아티팩트에는 `검증하기.cmd`, PowerShell 검증 스크립트 및
`RELEASE_SHA256SUMS.txt`가 포함됩니다. ZIP 해시를 외부 sidecar와 대조하고,
ZIP 내부 `SHA256SUMS.txt`로 EXE를 다시 대조합니다. 빌드마다 해시가 달라질 수 있으므로
항상 ZIP과 **같은 Actions 아티팩트의** sidecar를 사용하세요. 검증기는 임시 격리 데이터에서
프로필 생성·전환·검색어 분리 및 GUI 정상 종료를 3회 fresh-path + 3회 warm으로 검사하며,
OAuth/네트워크는 사용하지 않습니다. Windows Sandbox가 활성화된 PC에서는 원클릭 CMD가
네트워크·클립보드를 끈 Sandbox를 실행하고, 그렇지 않으면 현재 PC의 Python PATH를 제외한
오프라인 검증을 수행합니다. 이 PATH 검사는 Python 미설치 PC의 증명은 아닙니다.

## V4.4.2 Windows 10 검증 결과 (2026-10-17)

GitHub Actions run `39018403464`의 V4.4.2 아티팩트를 Windows 10 Pro 22H2
(19045.4780)에서 검증했습니다. 해당 ZIP SHA-256은
`64c19071eb10e649e08ba27961fc20e46d9cc593834e7b870f1a61ea022c0c6a`, EXE SHA-256은
`776e1860acc26d7e336cb072ada6fa31bb55fc547ea6a9fa719c715a7345e408`입니다.
ZIP sidecar와 내부 EXE 해시가 모두 일치했고, 전체 `_internal` 런타임 구조, 한국어 HTML 및
빠른 시작 문서를 확인했습니다. 한글·일본어·공백이 포함된 복사/이동 경로에서 fresh-path
GUI 준비 시간은 fresh-path 1093.8/1009.7/1010.5 ms, warm은 678.8/778.3/703.5 ms였습니다.
여섯 번 모두 0.79~1.23초 내 정상 종료, 잔류 프로세스 0건, JP/KR 가상 프로필 전환 및
검색어 격리 PASS, SQLite quick_check `ok`였습니다. 원클릭 CMD에서 실행한 전체 검증은
26.79초 걸렸고 실제 사용자
DB의 크기·수정 시각은 전후 동일했습니다. 이 PC에는 Python 3.14.3이 설치되어 있으므로
자식 EXE에서 Python PATH를 제거해 실행한 결과이지, Python 미설치 PC 시험은 아닙니다.

현재 PC에서는 `WindowsSandbox.exe`가 확인되지 않았고 기능 상태 조회는 관리자 권한을
요구해 Sandbox를 실행하지 않았습니다. 기능 설정·재시작·보안 옵션은 변경하지 않았습니다.
V4.4.2용 `.wsb` 구성은 네트워크/클립보드 비활성화와 입력 read-only 매핑을 포함해 생성 및
XML 검사까지 했으나, Sandbox VM 자체 실행은 미검증입니다. 따라서 오프라인 Windows 10
RC 게이트는 통과했지만 Python 미설치 독립 PC, Windows 11, 실계정 OAuth/API 및 실제 음악
정밀도는 남아 있습니다. Actions 재빌드의 해시는 달라질 수 있으니 항상 같은 run의 sidecar를
사용하세요.

## V4.5 개발: AI 연구 도우미

V4.5는 기존 장르 규칙을 먼저 적용하고 `검토` 후보만 선택형 AI 공급자가 보조하도록
분리합니다. AI `적합` 의견만으로 후보를 자동 승인하지 않으며, 규칙 `제외`는 AI가
뒤집을 수 없습니다. AI 응답에는 출처와 메타데이터 한계, 불확실성 및 직접 감상 권고가
표시됩니다. YouTube API/OAuth는 읽기 전용이며 자동 댓글·좋아요·구독·반복 재생은 없습니다.

새 `AI 연구 도우미` 탭의 조사 계획 미리보기 → 오프라인 Demo는 가상 사례와 Mock AI를
사용합니다. 네트워크, OAuth, 실제 계정, AI API 키, 사용자 DB를 쓰지 않습니다.
OpenAI, Gemini, Ollama 공급자 어댑터는 선택형이며 기본 동작은 모델 없이 유지됩니다.
공급자 설정과 실제 JP/KR 검색은 마지막 실전 승인 단계에서 별도 검증해야 합니다.

V4.5.2 API 호환성: Gemini 2.5 계열은 `generationConfig.responseMimeType`과
`responseJsonSchema`를 사용하고, 지정된 Gemini 3 계열은 `responseFormat.text.mimeType`
에 `APPLICATION_JSON` enum을 사용합니다. 알 수 없는 모델은 네트워크 요청 전에 차단합니다.
이 포맷은 Mock HTTP 계약으로 검증했으며 실제 API 키 연결은 하지 않았습니다.

V4.5.3에는 YouTube/OAuth 없이 가상 일본어 자료 한 건만 보내는 명시 승인형
Gemini 실전 연결 테스트가 추가되었습니다. DPAPI 저장 키만 내부에서 읽고, 별도 임시 DB,
요청 1회 상한, 자동 재시도 없음, 비밀정보/응답 원문 비노출을 적용합니다.

V4.5.1 보안 강화: OpenAI/Gemini 키는 Windows 현재 사용자 DPAPI로 암호화해
`%LOCALAPPDATA%\ZisuResearchStudio\provider_credentials`에 저장하며 키 자체는 다시
화면에 표시하지 않습니다. 공급자 키는 AI 탭에서 등록·변경·삭제할 수 있습니다.
유료 모델 호출은 기본 차단이며, 공급자의 현재 단가를 사용자가 확인한 뒤 1회 추정 비용,
1일 USD 상한 및 매 실행의 확인을 제공해야 합니다. 호출은 공급자별 작업당 10회,
프로필별 일 30회로 제한됩니다. 모델 단가·무료 할당량을 앱에서 고정값으로 가정하지 않습니다.
입력한 비용은 앱이 호출을 차단하는 추정 상한이며 공급자 청구액을 보장하는 계량값은 아닙니다.
키/비용 입력은 사용자가 수행할 때까지 비어 있으며, 이번 코드 검증에는 실제 API를 호출하지 않습니다.

조사 세션은 프로필별 SQLite 체크포인트로 보존됩니다. 일시 정지 후 같은 프로필에서 이어갈 수
있고, 앱이 검색 중 종료되어 요청 완료 여부가 모호한 경우 같은 읽기 검색을 재시도할 수 있어
YouTube 할당량이 한 번 더 사용될 수 있습니다. 후보 ID는 중복 제거됩니다.

설계 결정과 보안 경계: [V4.5 AI Research ADR](docs/ADR-004-v4.5-ai-research.md).
