# ZISU one-command release workflow

`scripts/zisu_release_oneclick.ps1` is the reusable Windows release orchestrator for V4.6.0 and later. It defaults to a **dry run**. The dry run checks the expected feature branch, scans Git scope for secret/data files, compiles Python, runs the unit suite and source GUI smoke, checks whitespace, and can validate a supplied Windows release ZIP plus its same-build manifest in an isolated folder. If run on Windows, the packaged EXE smoke removes Python and project paths from the child process and uses a temporary data directory.

Example dry run from Codex (no GitHub writes and no install):

```powershell
./scripts/zisu_release_oneclick.ps1 `
  -PackageZip 'D:\path\ZISU-Research-Studio-V4.6.0-Windows.zip' `
  -ZipManifestPath 'D:\path\RELEASE_SHA256SUMS.txt'
```

Reports and resumable checkpoints are written beneath the repository's ignored `_local_backups/ZisuReleaseRuns/<run-id>/` directory. Supply `-ResumeRunId <run-id>` to revalidate and resume a prior run. Secrets and API response bodies are not stored there.

GitHub operations and real installation are deliberately opt-in. `-ExecuteRelease` enables guarded commit/push, exact-commit Actions polling and artifact download. `-Install` additionally opts into installation and requires `-ExecuteRelease`; the script refuses to replace a running ZISU process, makes a SQLite Backup-API snapshot, retains the prior install/launcher, verifies hashes, performs an isolated GUI smoke, compares the database afterward, and rolls back on failure. It does not call Google, YouTube, or Gemini APIs. It never force-pushes, merges a PR, changes global proxy/TLS settings, or auto-approves a release.

Run this only from `feature/v4.5-ai-research-manager` and review the generated report before any opt-in release. Draft PR #4 is updated by the existing branch/Actions workflow; merging remains a human decision.
