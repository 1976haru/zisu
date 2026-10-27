import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OneClickReleaseSafetyTests(unittest.TestCase):
    def test_release_orchestrator_defaults_to_dry_run_and_gates_install(self):
        script = (ROOT / "scripts" / "zisu_release_oneclick.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$ExecuteRelease", script)
        self.assertIn("[switch]$Install", script)
        self.assertIn("$Install -and -not $ExecuteRelease", script)
        self.assertIn("$report.result='DRY_RUN_PASS'", script)
        self.assertIn("$report.external_api_calls=0", script)

    def test_release_orchestrator_has_data_and_package_integrity_gates(self):
        script = (ROOT / "scripts" / "zisu_release_oneclick.ps1").read_text(encoding="utf-8")
        for marker in (
            "SQLite Backup API",
            "integrity_check",
            "logical_digest",
            "SHA256SUMS.txt",
            "RELEASE_SHA256SUMS.txt",
            "_internal",
            "windows_exe_smoke.ps1",
            "completed_steps",
        ):
            self.assertIn(marker, script)

    def test_release_orchestrator_never_force_pushes_or_disables_tls(self):
        script = (ROOT / "scripts" / "zisu_release_oneclick.ps1").read_text(encoding="utf-8").lower()
        self.assertNotIn("--force", script)
        self.assertNotIn("http.sslverify=false", script)
        self.assertNotIn("stop-process", script)
        self.assertIn("merge-base --is-ancestor", script)
        self.assertIn("loopback_port_9", script)
        self.assertIn("process", script)

    def test_release_orchestrator_records_resume_without_storing_secrets(self):
        script = (ROOT / "scripts" / "zisu_release_oneclick.ps1").read_text(encoding="utf-8").lower()
        self.assertIn("$resumerunid", script)
        self.assertIn("checkpoint.json", script)
        self.assertIn("sensitive_values_logged=$false", script)
        self.assertIn("previous_install", script)

    def test_successful_git_scans_tolerate_native_stderr_and_scan_only_change_content(self):
        script = (ROOT / "scripts" / "zisu_release_oneclick.ps1").read_text(encoding="utf-8")
        self.assertIn("if ($LASTEXITCODE -ne 0) { return $null }", script)
        self.assertIn("$contentPaths=@($untracked+$changed", script)
        self.assertIn("permission warning", script)


if __name__ == "__main__":
    unittest.main()
