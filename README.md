# ZISU Research Studio V4 — 다채널 온보딩·엄격 장르 분리

V4 기능 브랜치는 첫 실행 시작 마법사, 프로필별 Google 읽기 전용 OAuth,
KR/JP 독립 검색어, 6개 장르의 `적합 / 검토 / 제외` 보수 판정과 V2 SQLite
무손실 마이그레이션을 제공합니다. 검색 지역은 채널 운영자의 국적을 증명하지
않으며, 애매하거나 인접 장르 신호가 있는 영상은 자동 승인하지 않습니다.

> 검증 상태: 자동화 테스트와 로컬 Tkinter 스모크는 통과했습니다. 실제 Google
> OAuth, 실제 KR/JP 영상 표본의 95% 정밀도, Python 미설치 Windows PC의 V4 EXE
> 실행은 별도 실사용 검증 전까지 완료로 간주하지 않습니다.

**Windows 10/11용** · 한국/일본 · Chill Rap / Morning Chill Rap 등 장르 분리 · Google 읽기 전용 연결

이 프로젝트는 장르별 유튜브 음악 채널을 찾아 분석하고, 내가 실제로 감상한 영상에 대해 댓글 초안을 작성하며, 채널별 데이터를 분리 관리하기 위한 도구입니다. **자동 조회수 조작, 자동 반복 재생, 자동 댓글/좋아요/구독 게시 기능은 포함하지 않습니다.** 자동 검색은 공식 YouTube Data API 읽기 전용으로만 수행합니다.

## 가장 먼저 할 일

1. 이미 작동하는 **V2.1은 지우지 마세요.** V3 ZIP을 별도 폴더에 압축 해제하고 `실행하기.bat`으로 V3 동작을 확인합니다.
2. 첫 번째 탭 **① 계정 연결**에서 대상 작업 프로필을 확인합니다. **설정 없이 바로 쓰고 싶다면 'Google Cloud 없이 먼저 유튜브 열기'** 버튼으로 브라우저에 로그인한 후 수동 검색할 수 있습니다. 이 모드는 앱 OAuth 연결은 아닙니다. 기존 V2 DB가 있으면 그대로 읽고 필요한 연결 필드만 자동 추가합니다.
3. `OAuth_모듈_설치하기.bat`을 한번 실행합니다. Google OAuth용 공개 Python 패키지 3개가 설치됩니다. 기본 기능은 패키지 없이 동작합니다.
4. Google Cloud에서 `YouTube Data API v3`를 켜고 `Google Auth Platform`의 앱 정보를 구성한 뒤, **데스크톱 앱** OAuth 클라이언트를 만들어 JSON 파일을 받습니다. 테스트 모드라면 연결할 새 Google 계정을 테스트 사용자로 등록합니다.
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

| 기능 | V3.0 상태 |
|---|---|
| JP/KR 다채널 프로필 분리 | 구현 |
| 프로필별 Google OAuth 계정 연결·해제 | 구현 / 실제 Google 승인 미검증 |
| 새 계정에 YouTube 채널이 없는 조사 전용 모드 | 구현 / 실제 Google 승인 미검증 |
| 계정 이메일·채널 ID 확인 | 구현 / 실제 API 미검증 |
| OAuth 또는 별도 API 키로 조회 전용 검색 | 구현 / 실제 API 미검증 |
| 총 6개 장르 × 2개 시장 × 12개 전용 키워드 = 144개 | 구현 |
| 충돌 장르 제외, 불확실한 후보 보류, 수동 승인 기록 | 구현 |
| 10/15/20분 감상 분석 알림 및 실제 청취 메모 | 구현 |
| 국가별 지표 수동 입력, CSV/DB 백업 | 구현 |
| 앱 실행 중 예약 검색 | 구현 (로그인 연결 또는 별도 API 키 필요) |
| 반복 자동재생/자동 댓글·좋아요·구독 | 구현하지 않음 (정책 리스크) |
| Python 미설치 PC용 EXE | Windows 빌드 스크립트 포함, 실기기 빌드 미검증 |

## Windows 개발/실행

- `실행하기.bat` : 기존 Python으로 실행 (Python 3.10 이상 필요)
- `OAuth_모듈_설치하기.bat` : 최초 한 번 구글 계정 연동 모듈 설치
- `오류진단하기.bat` : 에러 메시지 저장
- `EXE_만들기.ps1` : Windows에서 PyInstaller EXE 생성
- `로컬설치하기.ps1` : `D:\03 zisu`로 소스 복사(이전 프로그램 폴더 백업, Git 파일 보존)
- `사용설명서_초보자.html` : 사용자 가이드
- `.github/workflows/windows-build.yml` : CI 테스트와 EXE 빌드 작업 정의

단위 테스트: `python -m unittest discover -s tests -p 'test_*.py' -v`
GUI 스모크: `xvfb-run -a python tests/smoke_gui.py` (Linux 테스트 환경)

> 이 배포본은 로컬에서 구현한 소스 ZIP입니다. 사용자의 `D:\03 zisu`나 GitHub `1976haru/zisu`에 자동 업로드하지 않았습니다.

## 정책·공식 문서

- https://developers.google.com/youtube/data/guides/auth/installed-apps
- https://developers.google.com/youtube/v3/docs/search/list
- https://support.google.com/youtube/answer/3399767
