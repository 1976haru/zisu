# ZISU engineering guardrails
- Target Windows `D:\03 zisu`, GitHub `1976haru/zisu`. Do not overwrite DB or secrets.
- Multi-channel: JP and KR independent profiles and OAuth tokens. Preserve V2 SQLite data and migration tests.
- Main product requirement: strict genre isolation. Distinct Morning Chill Rap, Chill Rap, R&B Hip-Hop etc. Cross-genre results excluded or queued for manual review; never auto-approve ambiguous tracks.
- YouTube Data API and OAuth **read-only** only. No automated artificial views, repeat playback, bulk comments, auto likes/subscriptions, anti-detection evasions.
- Do not commit `client_secret*.json`, tokens, SQLite DB, logs, local backups. Never log access tokens.
- First-screen account setup with Google consent; don't request passwords. Research accounts without a channel supported.
- Report **estimated progress separately from verified test results**; current state, completed tasks, P0/P1/P2 remaining, exactly one next action in every report.
- Minimum gates: `python -m unittest discover -s tests -p test_*.py -v`; Linux GUI smoke if xvfb available; actual Windows/OAuth requires explicit verification.
