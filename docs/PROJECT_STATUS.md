# ZISU Integrated Project Status

Last checked: 2026-10-18 11:26 KST (offline implementation, isolated tests and resource sampling)
Current installed version: **V4.5.9**
Development branch: `feature/v4.5-ai-research-manager`
Working source: `D:\03 zisu\.test_tmp\history_audit` (matches the requested branch/SHA; the parent `D:\03 zisu` checkout is a separate, dirty repository and was left untouched)
Current HEAD: `5a7b4168d9e2d06d735bea600e4c0448772c72b1`
Remote status: not refreshed; Codex `gh auth status` failed. No commit or push was attempted. PR #4 is expected to remain Draft based on previous user-provided state; not checked live in this run. PR #2 and `main` were not touched.

## Progress

See calculation and row weights in [MASTER_BACKLOG.md](MASTER_BACKLOG.md).

- **Overall ZISU:** 68% estimated (67.9% weighted across the 100-point backlog). This includes remaining V4.6 scope; it is not a test-pass percentage.
- **Current V4.5.9/release-hardening scope:** 91% estimated. Installed binary and local offline tests pass, but screenshot/DPI, remote authenticated release flow and fresh CI are outstanding.
- **V4.6.0:** 34% estimated (33.5% weighted across its backlog). Schedule state and Mockable Autopilot block integration are present; scheduler UI, dedicated browser lifecycle and real API integration are incomplete.
- **Current P0 work (V46-01 + V46-RESOURCE-01):** 58% estimated. Schedule/Autopilot integration, resource admission gates and Mock recovery tests pass; automatic recovery, browser management and live GUI scheduler remain.

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
- Local worktree contains uncommitted changes for the release script/docs/tests, scheduler engine/schema/tests, and status/backlog docs. These are not reported as merged or remotely available.

### V4.6.0 schedule engine

- Added `research_schedules` schema without replacing V2/V3 tables. Scope snapshots market/core/related genre/channel ID and allows one unfinished schedule per profile; a different profile can have its own schedule.
- Added deterministic 2/4/6/8-hour schedule state, research/rest phase computation, scheduled start, pause/resume/cancel, crash-to-paused recovery, profile-scoped non-secret checkpoints and atomic search-call reservations. Uncertain in-flight requests remain consumed and block resume until explicit reconciliation. The module does not start a browser, playback, or external API call.
- Thirteen isolated scheduler/resource tests cover 6-hour fake-clock transitions, research-only Autopilot, break windows, saved session continuation, profile isolation, quota concurrency, resource pressure, rate limiting and safe in-flight completion.
- `mcrs/resource_guard.py` provides configurable CPU/RAM admission rules, 10-second-minimum cached sampling, Windows-native fallback sampling, and no process termination. Autopilot search and AI calls can be deferred while rule evidence, approvals and budgets remain distinct. The easy Autopilot tab now has a default-on low-load option, editable CPU/RAM thresholds and a resource status view.
- Resource recovery currently requires the user to resume; automatic restart is not implemented. The CPU/RAM sampler was measured once on this Windows host over 10.51 seconds: second CPU sample 5.57%, available RAM 13.68 GB, system memory load 59%, current sampler process working set 17.4 MB. This is a point observation, not a sustained idle average or guarantee.

## Latest validation

- `python -m compileall -q main.py mcrs tests`: PASS.
- `python -m unittest discover -s tests -p test_*.py -v`: **157/157 PASS**.
- `python tests/smoke_gui.py`: PASS.
- `python tests/stress_v43.py`: PASS; 100 profiles, 10,000 candidates, 12 workers, 5.854 s, peak traced memory 2.55 MiB, 0 failures.
- One full-suite rerun exposed a pre-existing order-dependent assertion in the shortfall test; it now selects the expected audit by `video_id`. The isolated test and final 157-test suite pass.
- PowerShell parser: PASS (release script).
- `git diff --check` plus UTF-8/newline/trailing-whitespace checks: PASS for current authored files.
- Release-script Dry Run on the same V4.5.9 CI ZIP: PASS, ZIP SHA `78c9be89c5b2a5183365bf3904655102449b978473ec266d551c6d708d14fff6`, EXE SHA as above; isolated package smoke PASS.
- Not tested: Windows 11, visible pixel/screen layout or DPI matrix, real OAuth/YouTube/Gemini/Analytics, managed browser lifecycle, scheduler GUI, automatic resource recovery, long-duration memory leak, fresh GitHub CI or sustained system CPU average.

## Immediate constraints and next order

No data-loss or credential issue was found. Three root-level temporary directories returned permission-denied warnings during Git enumeration; they were not opened, changed or deleted. Continue V46-01 by adding the schedule controls and automatic resource-recovery/resume coordination, then complete dedicated browser ownership tests. Preserve the existing install, user DB/backups, OAuth/DPAPI stores, PR #4 Draft state and branch history. Never merge `main` or PR #2 without explicit approval.
