# ZISU Master Backlog

This is the single prioritized implementation queue. Detailed scheduler requirements remain in [V4.6.0_RESEARCH_SCHEDULER.md](V4.6.0_RESEARCH_SCHEDULER.md); the historical [ROADMAP.md](ROADMAP.md) is retained and linked, not replaced. Current evidence and score are in [PROJECT_STATUS.md](PROJECT_STATUS.md).

## Progress method

Each row has a fixed share of 100 effort points. Overall project progress is the sum of `weight × estimated completion` divided by 100. A percentage is an estimate of implementation plus verification maturity, not a count of passing tests. Version progress is computed over only that version's rows. A task is not complete from documentation alone. A verified test count is reported separately.

## Ordered backlog

| ID | Work | Version | Priority | State | Depends on | Done when | Tests / evidence | Progress | Weight |
|---|---|---|---|---|---|---|---|---:|---:|
| STAB-01 | V4.5.9 install, user-data snapshot and recovery safety | V4.5.9 | P0 | Verified; retain install | Existing V4.5.9 package | Installed build starts from isolated data; SQLite integrity and counts match backup; OAuth/DPAPI untouched | Windows 10 onedir EXE smoke, SQLite Backup API integrity/logical comparison; install SHA in status report | 100% verified | 35 |
| STAB-02 | V4.5.9 easy mode, OAuth helper, UI state and recovery | V4.5.9 | P0 | Implemented; visual pass incomplete | STAB-01 | Required screens usable at supported display scales; recovery never replays uncertain external requests | 150 current code tests include GUI smoke/state tests; no screenshot/DPI automation | 90% estimated | 10 |
| REL-01 | Codex one-command release/build/test/publish/install orchestrator | V4.5.9 → reusable | P0 | Implemented; offline Dry Run passes, remote path blocked | STAB-01, STAB-02 | Safe preflight, tests, commit, authenticated fast-forward push, exact SHA CI/artifact validation, DB backup, isolated smoke, rollback and report; resume is idempotent | `DRY_RUN_PASS` against V4.5.9 CI ZIP; 150 tests, package hashes and isolated Windows EXE smoke; `gh auth status` failed in Codex process | 70% estimated | 15 |
| V46-01 | Profile-scoped research/rest schedule engine, Autopilot blocks and durable checkpoints | V4.6.0 | P0 | In progress: state engine and offline Autopilot block integration implemented | STAB storage and Autopilot checkpoints | 2/4/6/8-hour schedules, selectable research/rest cycles, fake-clock 6-hour simulation, pause/resume/cancel, crash recovery, atomic search allowance reservation, one Autopilot instance, in-flight fail-closed and saved budget | 13 isolated SQLite/fake-clock/concurrency/Autopilot integration tests; no GUI scheduler or live API integration | 65% estimated | 15 |
| V46-02 | Multiple Google accounts, channels and unified isolated profile model | V4.6.0 | P0 | Partial: V4.5 profile-level OAuth exists; shared mapping not audited/complete | V46-01 | A Google account may own multiple channels/profiles; tokens and account/channel selection remain isolated, with explicit profile warnings | Existing OAuth/mock profile tests; V4.6 account/channel matrix not yet tested | 10% estimated | 7 |
| V46-03 | Dedicated research-browser lifecycle | V4.6.0 | P1 | Not started | V46-02, V46-01, V46-RESOURCE-01 | One dedicated browser window by default, manually opened videos, reuse within research block, safe close on break/exit, no cookie/login extraction, preserve existing Chrome profiles, process/memory and crash cleanup checks | Local browser-launch mock and lifecycle tests required; no implementation yet | 0% | 5 |
| V46-04 | Beginner scheduler UI and multi-channel dashboard | V4.6.0 | P1 | Partial: Autopilot has low-load controls; schedule interface absent | V46-01, V46-02, V46-RESOURCE-01 | One prominent next action, schedule/progress/remaining budget, break, resume, review queue and accessible high-DPI layout | Tkinter interaction, keyboard, profile isolation and DPI tests required | 10% estimated | 3 |
| V46-05 | AI genre analysis and candidate diversification in scheduled sessions | V4.6.0 | P1 | Reuse exists; scheduled integration pending | V46-01, V46-02 | Core genre first, related genre contextual, duplicate channel suppression, evidence/uncertainty, AI and human labels separated; comments are review/copy only | Existing V4.5.7 Autopilot/genre tests; session diversity tests pending | 30% estimated | 3 |
| V46-06 | VPN, auth and quota safe-stop integration for schedules | V4.6.0 | P1 | Not started for scheduler | V46-01, V46-02 | Network/VPN/auth/quota errors pause safely; no infinite retry, budget expansion, IP rotation or limit evasion | Mock errors and reserved-call checkpoint tests required | 0% | 1 |
| V46-07 | Actual channel performance reporting, read-only Analytics only | V4.6.0 | P2 | Not started | V46-02, V46-05 | Research activity is separate from observed impressions/CTR/watch time; source/permission is explicit; no invented recommendation score | Mock data provenance tests; actual Analytics remains user-authorized validation | 0% | 1 |
| V46-RESOURCE-01 | Low-resource CPU/RAM admission control and bounded background work | V4.6.0 | P0 | Partial: configurable policy, low-frequency sampling cache, Autopilot search/AI admission gate and status UI added | V46-01, V46-03, V46-04 | Default low-load mode; sustained CPU and available/system/process RAM limits pause only new work; active call finishes and checkpoints; safe recovery; one managed browser; no user-process termination; timer/thread/cache cleanup and long-run stability tests | CPU/RAM threshold, AI deferral, in-flight pause, sample cadence and single-monitor tests pass; automatic recovery, browser lifecycle, GUI long-run and Windows performance measurements pending | 35% estimated | 5 |

## Sequencing rules

1. Finish V4.5.9 visual/DPI gap and get the one-click workflow's authenticated remote path validated; keep the current install unchanged.
2. Complete V46-01 persistence/state engine and Autopilot blocks; finish V46-RESOURCE-01 admission control and recovery before browser/UI work, then audit V46-02 account/channel relationships.
3. Implement V46-03 browser lifecycle and V46-04 beginner scheduler UI using resource policy.
4. Complete scheduled candidate diversification (V46-05), then safe-stop behavior (V46-06).
5. Add Analytics/reporting (V46-07) only after read-only scope and data provenance are established.
6. Run full cross-version, GUI, Windows onedir, security and migration gates before any V4.6.0 installation.

New requests are matched to these IDs first. Existing rows are extended instead of creating parallel implementations. No merge to `main` or PR #2 is authorized; PR #4 remains Draft.
