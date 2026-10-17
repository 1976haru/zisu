# Zisu Research Studio

한국·일본(KR/JP) 음악 유튜브 여러 채널을 위한 **장르 분리형 레퍼런스 조사 도구** 개발 저장소입니다.

**현재 상태 (2026-10-17):** 기존 저장소는 초기화된 상태였으며, V2 Python GUI 소스·초보자 HTML 설명서·Windows 설치/빌드 스크립트를 로컬 전달용 ZIP으로 제작하고 단위 테스트 16개와 GUI 스모크 테스트를 통과했습니다. **V2 전체 소스는 아직 이 저장소에 업로드되지 않았습니다.** 전체 소스는 사용자 PC의 `D:\03 zisu`에 설치한 후 기능 브랜치로 업로드할 예정입니다. 업로드 전 이 저장소를 실행 가능한 V2로 오해하지 마세요.

## 계획
- 여러 채널을 `profile_id`로 격리하고 목표 국가 KR/JP 및 핵심 장르 하나를 설정
- Chill Rap, Morning Chill Rap, R&B Hip-Hop, Cafe Chill, Old Pop, Jazz Lounge 전용 검색어
- 제목·설명 기반 보수적 장르 평가: 적합 / 검토 / 제외, 장르 충돌은 후보 저장 차단
- YouTube Data API v3 읽기 전용 검색, 수동 영상 감상·메모, 댓글 초안 복사, 국가별 실측 성과 기록
- 초보자용 Windows GUI 및 배포 EXE, 테스트, 백업

**제외:** 인위적 참여를 만들 수 있는 자동 반복재생, 댓글 자동 게시, 좋아요·구독 자동화. 유튜브 정책: https://support.google.com/youtube/answer/3399767

개발 진행은 Repository Issues에서 관리합니다.
