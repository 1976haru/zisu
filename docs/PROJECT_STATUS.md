# ZISU Integrated Project Status

Last checked: 2026-10-18 11:40 KST (offline schedule GUI integration, recovery and host resource sample)
Current installed version: **V4.5.9**
Development branch: `feature/v4.5-ai-research-manager`
Working source: `D:\03 zisu\.test_tmp\history_audit` (matches the requested branch/SHA; the parent `D:\03 zisu` checkout is a separate, dirty repository and was left untouched)
Latest V4.6.0 scheduler/resource code commit: `bdb20b678e7dcde71389a50f057fbd24ac7b1c16` (local, not pushed)
Remote status: not refreshed; Codex `gh auth status` reports the stored 1976haru credential is invalid, so no push was attempted. PR #4 is expected to remain Draft based on previous user-provided state; not checked live in this run. PR #2 and `main` were not touched.

## Progress

See calculation and row weights in [MASTER_BACKLOG.md](MASTER_BACKLOG.md).

- **Overall ZISU:** 72% estimated (72.2% weighted across the 100-point backlog after this task's estimated row updates). This includes remaining V4.6 scope; it is not a test-pass percentage.
- **Current V4.5.9/release-hardening scope:** 91% estimated. Installed binary and local offline tests pass, but screenshot/DPI, remote authenticated release flow and fresh CI are outstanding.
- **V4.6.0:** 44% estimated (44.25% weighted across its backlog). Schedule state, Autopilot block integration and widget-level schedule UI are present; browser lifecycle, expanded dashboard and actual APIs remain incomplete.
- **Current P0 work (V46-01 + V46-RESOURCE-01, with V46-02 dependency):** 58% estimated (15.75/27 weighted points). Scheduler UI, resource admission and safe same-process recovery pass isolated tests; browser management and long-duration/Windows resource measurements remain.

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

## Latest validation

- `python -m compileall -q main.py mcrs tests`: PASS (this task).
- `python -m unittest discover -s tests -p test_*.py -v`: **161/161 PASS** (157 existing plus 4 new schedule/recovery tests; final rerun this task).
- `python tests/smoke_gui.py`: PASS; schedule selector defaults, low-load default and schedule callbacks are present (this task). No screenshot/DPI evidence.
- `python tests/stress_v43.py`: PASS; 100 profiles, 10,000 candidates, 12 workers, 5.886 s, peak traced memory 2.55 MiB, 0 failures (this task).
- One full-suite rerun exposed a pre-existing order-dependent assertion in the shortfall test; it now selects the expected audit by `video_id`. The isolated test and final 157-test suite pass.
- PowerShell parser: PASS (release script).
- `git diff --check` plus UTF-8/newline/trailing-whitespace checks: PASS for current authored files.
- Release-script Dry Run on the same V4.5.9 CI ZIP: PASS, ZIP SHA `78c9be89c5b2a5183365bf3904655102449b978473ec266d551c6d708d14fff6`, EXE SHA as above; isolated package smoke PASS.
- Not tested: fresh Windows EXE build/install, Windows 11, visible pixel/screen layout or DPI matrix, real OAuth/YouTube/Gemini/Analytics, managed browser lifecycle, actual 6-hour wall-clock run, long-duration memory leak, fresh GitHub CI or sustained system CPU average.

## Immediate constraints and next order

No data-loss or credential issue was found; this task did not open or modify the live user DB, V4.5.9 installation, OAuth tokens or DPAPI store. Three root-level temporary directories returned permission-denied warnings during Git enumeration; they were not opened, changed or deleted. Continue with GUI/DPI and long-run resource validation, then complete dedicated browser ownership tests. Preserve the existing install, user DB/backups, OAuth/DPAPI stores, PR #4 Draft state and branch history. Never merge `main` or PR #2 without explicit approval.
