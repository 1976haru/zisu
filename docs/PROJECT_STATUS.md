# ZISU Integrated Project Status

Last checked: 2026-10-18 (V46-03 dedicated browser lifecycle implementation and scoped regression)
Current installed version: **V4.5.9**
Development branch: `feature/v4.5-ai-research-manager`
Working source: `D:\03 zisu\.test_tmp\history_audit` (matches the requested branch/SHA; the parent `D:\03 zisu` checkout is a separate, dirty repository and was left untouched)
V46-03 implementation parent: `5853f2ca78490d886f947b72967d0f596cd9d8ba`; implementation is committed on the requested feature branch (see current Git HEAD).
Remote status: not refreshed; Codex `gh auth status` reports the stored 1976haru credential is invalid, so no push was attempted. PR #4 is expected to remain Draft based on previous user-provided state; not checked live in this run. PR #2 and `main` were not touched.

## Progress

See calculation and row weights in [MASTER_BACKLOG.md](MASTER_BACKLOG.md).

- **Overall ZISU:** 82% estimated (81.6 weighted points using the existing backlog scoring; this includes remaining V4.6 scope and is not a test-pass percentage).
- **Current V4.5.9/release-hardening scope:** 91% estimated. Installed binary and local offline tests pass, but screenshot/DPI, remote authenticated release flow and fresh CI are outstanding.
- **V4.6.0:** 68% estimated (67.7% weighted across its backlog). Schedule state, offline account/channel registry, resource sampling/admission, widget-level schedule UI and browser lifecycle control are present; browser page navigation/clean process shutdown, expanded dashboard and actual APIs remain incomplete.
- **Current P0 work (V46-01 + V46-02 + V46-RESOURCE-01):** 81% estimated (21.9/27 weighted points). Offline scheduler/resource and account/channel isolation tests pass; live OAuth and wall-clock EXE runtime remain.
- **Current V46-03:** 65% estimated. Edge produced a visible top-level frame, but the local test page did not load and the isolated process needed a verified cleanup fallback; Chrome failed during startup. End-to-end browser use is not complete.

## Verified and unverified state

### V4.5.9 install and user-data evidence

- Installed EXE exists at `D:\03 zisu\ZISU_APP\ZisuResearchStudio.exe`; SHA-256: `bcc2372160ecdc6c0b41789bae88060bf96d1165b4540d3ad023b4abbe03ab1d`.
- Installed onedir package remains in place and was not replaced. Previous install backup remains at `D:\03 zisu\_local_backups\V4.5.9_PreInstall_20261018_103156`.
- CI run `39086304411` and release ZIP SHA `78c9be89c5b2a5183365bf3904655102449b978473ec266d551c6d708d14fff6` are prior user-provided evidence, not a fresh GitHub query in this run.
- Read-only live SQLite metadata and the SQLite Backup-API snapshot both report integrity `ok`, 4 profiles, 40 candidates, 50 genre audits, 18 `auto_fit` proposals, 0 human labels, 9 AI review rows and 1 research session. The snapshot is `D:\03 zisu\_local_backups\V4.5.9_OneClickDryRun_20261018_01\user_database_snapshot.sqlite`; logical equality was verified when created. No user DB writes were made during this task.
- OAuth token contents and DPAPI credentials were not read. No Google, YouTube or Gemini API calls were made.
- Installed EXE was run with isolated `ZISU_DATA_DIR`; Windows 10 Pro 22H2 build 19045.4780 reported V4.5.9 GUI initialized, 2 mock profiles switched without crossing search terms, DB integrity `ok`, network/OAuth false and residual process count 0. A visible-window attempt confirmed the V4.5.9 title and normal close message; screenshot capture failed because this execution context has no valid screen capture handle. Therefore screen layout, wizard interaction and 100/125/150/200% DPI remain unverified.

### Current release automation

- `scripts/zisu_release_oneclick.ps1` defaults to offline Dry Run and uses opt-in gates for release writes/install. It checks branch, secret/data paths, tests, package and matching ZIP/EXE hashes, then performs an isolated Windows EXE smoke. It never calls Google/YouTube/Gemini or merges a PR.
- Latest report: `D:\03 zisu\.test_tmp\history_audit\_local_backups\ZisuReleaseRuns\20261018_111136_1d585c5a\release-report.json`, result `DRY_RUN_PASS`; V4.5.9 ZIP/EXE hashes match and the 150-test suite plus isolated EXE smoke passed. The script detected process proxy variables pointing to `127.0.0.1:9`; Dry Run did not alter them. Its GitHub child-command wrapper temporarily scopes proxy clearing only when a release is explicitly opted in.
- GitHub auth and remote CI/artifact retrieval are not validated in this Codex process. `gh auth status` returned nonzero; no identity/token detail was emitted. No commit/push/install was attempted.
- Release automation, scheduler/resource guard, tests and unified status/backlog documentation were committed locally as `a5eb644381d46e76b8848832515da48defe68104`. The commit is not reported as pushed or remotely available.

### V4.6.0 schedule engine

- Added `research_schedules` schema without replacing V2/V3 tables. Scope snapshots market/core/related genre/channel ID and allows one unfinished schedule per profile; a different profile can have its own schedule.
- Added deterministic 2/4/6/8-hour schedule state, research/rest phase computation, scheduled start, pause/resume/cancel, crash-to-paused recovery, profile-scoped non-secret checkpoints and atomic search-call reservations. Uncertain in-flight requests remain consumed and block resume until explicit reconciliation. The module does not start a browser, playback, or external API call.
- Thirteen isolated scheduler/resource tests cover 6-hour fake-clock transitions, research-only Autopilot, break windows, saved session continuation, profile isolation, quota concurrency, resource pressure, rate limiting and safe in-flight completion.
- `mcrs/resource_guard.py` provides configurable CPU/RAM admission rules, 10-second-minimum cached sampling, Windows-native fallback sampling, and no process termination. Autopilot search and AI calls can be deferred while rule evidence, approvals and budgets remain distinct. The easy Autopilot tab now has a default-on low-load option, editable CPU/RAM thresholds and a resource status view.
- At the previous baseline, resource recovery required user resume. The CPU/RAM sampler was measured once on this Windows host over 10.51 seconds: second CPU sample 5.57%, available RAM 13.68 GB, system memory load 59%, current sampler process working set 17.4 MB. This is a point observation, not a sustained idle average or guarantee. Same-process guarded recovery was added in the update below; long-duration load measurement remains pending.

### V4.6.0 schedule UI and recovery update (this task)

- Added Autopilot schedule controls for 2/4/6/8 hours, 20/30/40-minute research, 15/30/40-minute breaks, and optional same-day local start time. Defaults are 6 hours, 30/30 and low-load enabled. The active profile banner remains the research channel selector; the profile is disabled while its scheduled work runs.
- The existing Autopilot worker now runs only during scheduled research windows, leaves breaks API-free, updates phase/elapsed/remaining/search allowance, and supports pause, continue, safe stop, close-time checkpoint, and a user-confirmed recovery prompt after restart. Approval metadata is allow-listed and contains no credentials or tokens.
- Resource recovery now requires a healthy resource decision, an unchanged saved Autopilot approval (or a still-live in-process approval before session creation), remaining search allowance, and no uncertain request. Recovery after app restart is never automatic. Resume preserves a break phase instead of restarting research early.
- A network/collector exception retains its consumed search reservation and marks the request uncertain; it is not refunded or silently repeated. The user must explicitly reconcile it before continuing.
- Host resource observation during this task: 10.01-second sampling interval, system CPU 4.23% at the second sample, available RAM 12.71 GiB, system memory load 59%, sampler Python process working set 16.5 MiB. This is one brief host observation, not ZISU idle-average or extended-run performance.
- Added four schedule tests for exact approval matching, resource recovery, uncertain call retention, and preserving a break across pause/resume. GUI smoke verifies the new controls/defaults and low-load setting. This is widget-level verification, not screenshot or DPI validation.

### V4.6.0 V46-02 multi-account/channel implementation (2026-10-18)

- Added additive `google_accounts` and `google_account_channels` registries plus `profiles.google_account_id`. Existing profile email/channel rows are linked on migration; repeat initialization is idempotent. No OAuth token is moved, copied, or logged.
- OAuth remains separately stored per profile. A Google account can be linked to multiple channel-specific profiles, and multiple Google accounts can coexist. The account screen now summarizes account → authenticated channel(s) → connected profile(s); the existing OAuth consent response remains the source of selectable channel IDs.
- Missing account email fails closed. Rebinding to another account/channel requires explicit confirmation and is blocked when that profile has research material or an active/recoverable scheduler session, so existing candidates, AI decisions, notes, drafts, and reports cannot be silently reattributed. Users should create a separate profile for another channel.
- Added 5 account/channel tests covering same-account multiple channels, separate accounts, per-profile token isolation, candidate/AI/memo separation, identity change safeguards, active-session blocking, and repeatable additive migration. Updated the no-channel OAuth fixture to include its mock email.
- This is mock/offline evidence only. No actual OAuth, Google API, live user DB, V4.5.9 installation, OAuth token or DPAPI credential was accessed. The parent checkout remains untouched.

## Latest validation

- Prior V46-01 baseline `python -m unittest discover -s tests -p test_*.py -v`: **161/161 PASS** (157 existing plus 4 schedule/recovery tests); not rerun during V46-02 because final QA was explicitly deferred.
- Prior V46-01 `python tests/smoke_gui.py` and `python tests/stress_v43.py`: PASS; schedule UI smoke and 100 profiles/10,000 candidates, 5.886 s, 2.55 MiB traced peak, 0 failures. Not repeated during V46-02.
- Current V46-02 scoped suite: OAuth management **16/16 PASS**, OAuth/migration **11/11 PASS**, scheduler compatibility **17/17 PASS**; GUI smoke **PASS**, `compileall` **PASS**, `git diff --check` **PASS**. These 44 test cases are scoped checks and do not replace the existing full 161-test suite, which was intentionally not run per instruction to defer final QA.
- One full-suite rerun exposed a pre-existing order-dependent assertion in the shortfall test; it now selects the expected audit by `video_id`. The isolated test and final 157-test suite pass.
- PowerShell parser: PASS (release script).
- `git diff --check` plus UTF-8/newline/trailing-whitespace checks: PASS for current authored files.
- Release-script Dry Run on the same V4.5.9 CI ZIP: PASS, ZIP SHA `78c9be89c5b2a5183365bf3904655102449b978473ec266d551c6d708d14fff6`, EXE SHA as above; isolated package smoke PASS.
- Not tested: fresh Windows EXE build/install, Windows 11, visible pixel/screen layout or DPI matrix, real OAuth/YouTube/Gemini/Analytics, production-AppData browser lifecycle, actual 6-hour wall-clock run, long-duration memory leak, fresh GitHub CI or sustained system CPU average.

## Immediate constraints and next order

No data-loss or credential issue was found; this task did not open or modify the live user DB, V4.5.9 installation, OAuth tokens or DPAPI store. Three root-level temporary directories returned permission-denied warnings during Git enumeration; they were not opened, changed or deleted. Next: diagnose why installed Chrome/Edge exited before creating a visible isolated window; then continue V46-04 dashboard/DPI work. V46-RESOURCE-01 long-run validation remains. Preserve the existing install, user DB/backups, OAuth/DPAPI stores, PR #4 Draft state and branch history. Never merge `main` or PR #2 without explicit approval.

### V46-RESOURCE-01 continuation (2026-10-18)

- Extended the native/optional resource sampler to report ZISU process CPU separately from its RSS and system CPU/available RAM/system memory load. The first CPU sample remains unknown until a baseline interval exists; invalid, non-finite, unavailable, or throwing samples now fail closed for new work. Sampling/callback failures no longer terminate the monitor loop.
- Added process-wide single-capacity slots for YouTube search+metadata operations and AI provider HTTP operations. A contending caller waits with a bounded blocking timeout (no polling loop); existing approved call budgets and uncertain-request rules are unchanged. Search contention fails safely without starting another request.
- Updated easy-mode wording for resource waits/recovery and included process CPU in advanced diagnostics. No other application or user process is terminated.
- Scoped regression: scheduler 17/17, Autopilot 12/12, resource lifecycle 7/7 = **36/36 PASS**; compileall PASS; GUI smoke PASS; `git diff --check` PASS. A first run surfaced one old synthetic `ResourceSnapshot(0)` test fixture that represented a CPU-only reading; because missing RAM now correctly fails closed, the fixture was upgraded to provide complete mock readings. The regression then passed.
- Windows host sample (single short observation, not a guarantee): system CPU 2.34%, available RAM 10,461,011,968 bytes, system memory load 69%, Python process RSS 34,902,016 bytes, process CPU 0.0%.
- Isolated virtual stability experiment: 12 simulated six-hour sessions and 12 pause/resume cycles; RSS milestones 34,959,360 → 35,319,808 → 35,323,904 → 35,328,000 bytes (net +368,640 bytes during this bounded test); `tracemalloc` current 56,031 bytes / peak 74,532 bytes; thread count 1→1; Windows process handles 114→114. These virtual-clock cycles do not represent 72 hours of wall-clock operation or a built EXE. The final process CPU field was unavailable on one sampled cycle, so no sustained CPU average is claimed.
- Deferred: full regression suite/final QA, real six-hour Windows EXE operation, high-load contention on a user workstation, GUI visual/DPI verification, and browser lifecycle. V4.5.9 install/user DB/OAuth/DPAPI stores remain untouched; no external APIs were called.
- GitHub `gh auth status` was checked during this continuation and reports the stored credential invalid. Push/CI were not attempted; local source remains on the requested feature branch.

### V46-03 dedicated research browser (2026-10-18)

- Compared Playwright-managed Chromium with installed Edge/Chrome launched as an independent process. Chose installed Edge/Chrome with a fresh per-session ZISU profile: avoids downloading/bundling a second browser runtime and keeps existing browser profiles/cookies separate. No cookies, credentials, or user profile data are copied. Browser opens only after explicit user approval and a resource-admission check; only HTTPS YouTube watch/short URLs are accepted in normal mode. Local HTTP is enabled solely for explicit local integration tests.
- Added `mcrs/research_browser.py`: single owned session, isolated profile, same-profile reuse, PID + process creation-time + executable ownership checks, startup recovery manifest, process-tree/RSS/window metrics, graceful `WM_CLOSE` followed by identity-revalidated owned-tree cleanup, and residual-process reporting. Existing user Chrome/Edge windows are never targeted. Browser profiles are not deleted after close.
- Wired browser open/close/recovery into the Autopilot tab and scheduler lifecycle. The selected video and active research phase are required; pause, rest, stop, completion and app shutdown close only the owned browser session. Added a one-time user-approved 10-minute extension option in the final minute; ZISU notes are checkpointed and it does not claim to preserve unpublished YouTube page input.
- Added two lifecycle safeguards and manual fallback coverage. Targeted browser/scheduler/Autopilot/resource suite: **45 total, 44 PASS, 1 SKIP, 0 failures**. `compileall`, `tests/smoke_gui.py` (`GUI_SMOKE_PASS`) and `git diff --check` pass. The opt-in actual-browser test remains skipped in routine tests; direct launch probes below are separate. This is scoped verification, not final QA.
- Session diagnostics: `SESSIONNAME=Console`, `UserInteractive=True`, Explorer present, Windows Session ID 1; the session is interactive. Browser paths resolved to installed Edge (`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe`) and Chrome (`C:\Program Files\Google\Chrome\Application\chrome.exe`). Subprocess used argv arrays (no shell); isolated profiles under writable repo `.test_tmp` were created. Existing Chrome/Edge processes were present and untouched. `Get-CimInstance Win32_OperatingSystem` was denied; Application log had no matching browser crash event; Defender Operational log was unreadable. HKCU WinINET proxy is disabled; process environment has proxy variables `127.0.0.1:9` and NO_PROXY includes loopback. No proxy or security setting was changed; local test invocation alone passed `--no-proxy-server`.
- Root-cause evidence: Chrome stderr reported Crashpad `CreateFile` access denied (WinError 5), fatal Mojo platform-channel access denied (0x5), then Crashpad self-termination (exit `4294930433`). Edge displayed a real `Chrome_WidgetWin_1` top-level frame, but the local HTTP fixture received no navigation request; stderr reported failure granting Chromium sandbox access to its cache directory. One stable probe confirmed the frame appeared; the owned Edge process later required the identity-checked cleanup fallback and then had zero reported residual processes. Thus the immediate failure is browser sandbox/Crashpad/cache IPC access denial in this Codex workspace run; the underlying Windows ACL/EDR/Codex policy source is not proven. No sandbox/security policy was disabled. End-to-end page navigation and clean Edge shutdown are **FAIL / unverified here**.
- Fixed window counting to include only Chromium's top-level `Chrome_WidgetWin_1` frame, avoiding UAC/input overlay false positives. Added a 1.5-second stable-process check so transient frames cannot be reported as successful startup, and moved periodic process metrics off the Tkinter main thread. Edge is preferred when installed. Added explicit user-confirmed manual default-browser mode; it never claims ownership or closes the user's browser and continues to save ZISU notes. Manual browser navigation is unit-mocked only.
- Latest bounded resource experiment: system CPU 3.17%, available RAM 8,682,815,488 bytes, system memory load 74%, Python process RSS 37,183,488 bytes, process CPU 0.0%. Virtual 12 six-hour sessions/12 pause-resume cycles: RSS decreased 1,114,112 bytes; `tracemalloc` current 56,304 / peak 75,082 bytes; threads 1→1, handles 114→114. This is not a real six-hour browser/EXE run or sustained CPU benchmark.
- No full suite, final QA, EXE build/install, user DB access, OAuth/Gemini/YouTube request, or write to the V4.5.9 installation was performed. `gh auth status` remains invalid in this Codex process; no push/CI attempt was made. Three inaccessible temporary directories still cause harmless Git status enumeration warnings and were not modified.
- Next work remains in the backlog sequence: verify browser navigation and graceful close in a normal user AppData-backed Windows session, then complete V46-04 dashboard/DPI work. Keep V4.5.9 install and all credentials/data untouched.
