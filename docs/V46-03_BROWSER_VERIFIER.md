# V46-03 브라우저 검증 도구

이 도구는 일반 Windows 바탕화면에서 전용 브라우저 동작을 확인하기 위한 별도 프로그램입니다. ZISU 사용자 DB, OAuth/Gemini 자격정보, YouTube에는 접근하지 않습니다.

## 실행

1. 전달받은 `ZISU_V46-03_Browser_Verifier.exe`를 Windows PC에 저장합니다.
2. 파일을 더블클릭합니다. PowerShell 입력이나 관리자 권한은 필요하지 않습니다.
3. Edge 또는 Chrome을 고른 다음 반복 횟수 `5회` 또는 `20회`를 선택합니다.
4. `전체 자동 테스트 시작`을 한 번 누릅니다. 프로그램이 로컬 페이지 열기, 같은 격리 창 재사용, 해당 창에만 WM_CLOSE 전송(X 버튼 동작과 동일), 닫힘 확인, 같은 프로필로 재실행을 순서대로 수행합니다.
5. 마지막 반복 후 전용 창과 소유 자식 프로세스를 정상 종료하고, 테스트 전 일반 Chrome/Edge PID가 유지됐는지 확인합니다. 실패나 소유권 불확실성이 생기면 추가 반복을 중지하고 실패 원인을 기록합니다.
6. 완료 화면에서 총 회수, 성공/실패, 잔류 프로세스, 최대 전용 브라우저 메모리, 최종 판정과 저장 경로를 확인합니다. 보고서는 자동 저장되며 필요하면 `결과 보고서 저장`으로 다시 저장할 수 있습니다.

보고서는 `%LOCALAPPDATA%\ZisuSignalLab\BrowserVerification\<실행 ID>\browser-verification-report.json`에 저장됩니다. 실패하거나 테스트를 끝내기 전에 보고서를 저장하면 PASS가 아니라 미완료/실패로 기록됩니다.

이 프로그램은 임의의 브라우저 프로필을 복사하지 않으며 보안 정책을 바꾸거나 `--no-sandbox`를 사용하지 않습니다. 닫기 검증은 도구가 직접 시작해 실행 경로와 격리 프로필을 확인한 테스트 프로세스의 창에만 요청합니다. X 닫기 이후 재실행은 창이 안정적으로 사라진 것을 확인한 후 진행합니다. 사용자의 일반 브라우저를 일괄 종료하지 않습니다. 테스트 중 Windows 보안 경고를 우회하지 마세요.

판정은 로컬 HTTP 요청 수신과 전용 프로필 소유 창 개수의 안정 구간을 각각 사용합니다. 브라우저 호출이 시간 초과되어도 실제 로컬 페이지가 도착했고, 동일 격리 프로필의 소유 창 1개가 안정적으로 관찰되면 그 증거를 함께 기록해 일시적인 메트릭 지연과 실제 실패를 구분합니다. 페이지 도달만으로는 통과시키지 않습니다.

Codex에서 실행하지 못한 것은 일반 Windows 데스크톱의 실패를 의미하지 않습니다. 현재 제한된 Codex Windows 세션의 Chrome 자동 실행은 로컬 페이지 요청 전에 종료 코드 `4294930433`으로 실패했습니다. 이는 자동화 코드의 Mock 통과나 실제 GUI 시작과 별개인 브라우저 실행 결과이며, 실제 사용자 데스크톱 5회 PASS는 아직 검증되지 않았습니다. 보고서를 전달하면 실제 브라우저 창 검증 게이트를 판정할 수 있습니다. 브라우저가 실행되지 않으면 저장된 FAIL 보고서와 종료 코드만 공유하면 되며, 사용자명이나 비밀정보는 포함되지 않습니다.
## Lifecycle diagnostics update (2026-10-18)

The verifier now records lifecycle states `STARTING`, `OPEN`, `USER_CLOSED`, `BACKGROUND_PROCESS_ONLY`, `CLOSING`, `CLOSED`, and `ERROR`. A normal exit code 0 is recorded as a clean close, not a crash. A missing window with a still-running owned Chromium process is reported separately.

Latest build is an onedir Windows package; extract the complete ZIP and keep `_internal` beside the EXE. Use the current handoff path and SHA-256 from the release report, not historical artifact paths recorded in older status notes.

For the automated check, choose `5회` (recommended) or `20회` and click **전체 자동 테스트 시작** once. The report records every lifecycle stage and actual pass/fail, timestamp, local-page request, window/process count, owned PIDs, exit code, close reason, CPU/RAM, owned-process RSS/handle count, and whether baseline browser PIDs remained. A failure stops further cycles and is never converted into a pass.

PID recovery is accepted only when the process executable matches the selected Edge/Chrome binary and its parsed `--user-data-dir` argument exactly matches the isolated verifier profile. Stale PID identities are discarded before process-tree expansion. No process is selected or terminated by image name alone.

Current user-supplied run summary (not independently reproduced here): 5 Chrome attempts; attempts 1 and 3 passed; 2 and 4 failed the window-retention check; attempt 5 displayed then exited with code 0. This mixed result remains incomplete until the updated verifier records five successful reopen cycles on the user's interactive desktop.
