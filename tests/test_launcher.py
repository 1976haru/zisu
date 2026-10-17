import unittest
import os
import json
import time
from pathlib import Path
from unittest.mock import patch
import main
ROOT=Path(__file__).resolve().parents[1]
from mcrs.startup_probe import write_stage
from support import WorkspaceTemp
class LauncherTests(unittest.TestCase):
    def test_offline_smoke_arguments_are_explicit_and_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            main._apply_offline_smoke_arguments([
                "--data-dir", "isolated", "--offline-smoke-report", "report.json"
            ])
            self.assertEqual("isolated",os.environ["ZISU_DATA_DIR"])
            self.assertEqual("report.json",os.environ["ZISU_EXE_SMOKE_REPORT"])
            with self.assertRaises(SystemExit):
                main._apply_offline_smoke_arguments(["--data-dir"])
    def test_exe_smoke_report_records_timed_startup_events_without_identity(self):
        with WorkspaceTemp() as root:
            report=root/"日本語 startup report.json"
            with patch.dict(os.environ, {
                "ZISU_EXE_SMOKE_REPORT": str(report),
                "ZISU_EXE_SMOKE_START_COUNTER": str(time.perf_counter_ns()),
                "ZISU_EXE_SMOKE_COUNTER_FREQUENCY": "1000000000",
            }):
                main._write_smoke_report("python_entrypoint")
                write_stage("database_initialized")
                payload=json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual("database_initialized",payload["stage"])
            self.assertEqual(["python_entrypoint","database_initialized"],[e["stage"] for e in payload["events"]])
            self.assertGreaterEqual(payload["elapsed_ms"],0)
            self.assertIn("timestamp_utc",payload["events"][-1])
            self.assertGreaterEqual(payload["events"][-1]["launch_elapsed_ms"],0)
            self.assertNotIn("email",payload)
            self.assertNotIn("token",report.read_text(encoding="utf-8").lower())
    def test_launcher_and_diagnostics_are_present(self):
        for f in ("실행하기.bat","START_ZISU.cmd","오류진단하기.bat","설치안내_문제해결.html","main.py","mcrs/gui.py"):
            self.assertTrue((ROOT/f).exists(),f)
    def test_launcher_logs_errors(self):
        s=(ROOT/"START_ZISU.cmd").read_text(encoding="utf-8")
        self.assertIn("startup_latest.log",s)
        self.assertIn("pause",s)
        self.assertIn("tkinter",s)
        self.assertIn("sys.version_info",s)
    def test_powershell_scripts_have_bom_for_windows_51(self):
        for p in ROOT.glob("*.ps1"):
            self.assertTrue(p.read_bytes().startswith(b"\xef\xbb\xbf"))
    def test_ci_builds_packages_and_runs_frozen_exe(self):
        workflow=(ROOT/".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        self.assertIn("actions/checkout@v7",workflow)
        self.assertIn("actions/setup-python@v7",workflow)
        self.assertIn("actions/upload-artifact@v7",workflow)
        self.assertIn("windows_exe_smoke.ps1",workflow)
        self.assertIn("python tests/smoke_gui.py",workflow)
        self.assertIn("package_windows.ps1",workflow)
        self.assertIn("--onedir",workflow)
        self.assertIn("--onefile",workflow)
        self.assertIn("--collect-submodules google_auth_oauthlib",workflow)
        self.assertIn("--collect-data google_auth_oauthlib",workflow)
        self.assertIn("fresh_path_launches",workflow)
        self.assertIn("warm_process_launches",workflow)
        self.assertIn("startup-comparison.json",workflow)
        self.assertIn("ZISU 배포 日本語 path",workflow)
        self.assertIn("V4.5.1-Windows.zip",workflow)
        self.assertIn("validate_windows_release.ps1",workflow)
        self.assertIn("create_windows_sandbox_config.ps1",workflow)
    def test_release_scripts_are_present_and_forbid_sensitive_payloads(self):
        smoke=(ROOT/"scripts/windows_exe_smoke.ps1").read_text(encoding="utf-8")
        package=(ROOT/"scripts/package_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("--offline-smoke-report",smoke)
        self.assertIn("--data-dir",smoke)
        self.assertIn("ZISU_EXE_SMOKE_REPORT",smoke)
        self.assertIn("exe-smoke-failure.json",smoke)
        self.assertIn("last_python_stage",smoke)
        self.assertIn("native_window_handle_present",smoke)
        self.assertNotIn("gui_window_detected",smoke)
        self.assertIn("python_removed_from_path",smoke)
        self.assertIn("launch_to_gui_ready_ms",smoke)
        self.assertIn("ZISU_EXE_SMOKE_START_COUNTER",smoke)
        self.assertIn("EXE process(es) remained",smoke)
        self.assertIn("ZISU_EXE_SMOKE_PROFILE_TEST",smoke)
        self.assertIn("profile_test",smoke)
        self.assertIn("database_integrity",smoke)
        self.assertIn("_internal",package)
        self.assertIn("Refusing to overwrite existing package",package)
        for marker in ("client_secret","oauth_tokens","token\\.json","\\.db$","\\.log$"):
            self.assertIn(marker,package)

    def test_offline_release_validator_checks_hashes_isolation_and_profiles(self):
        validator=(ROOT/"scripts/validate_windows_release.ps1").read_text(encoding="utf-8")
        sandbox=(ROOT/"scripts/create_windows_sandbox_config.ps1").read_text(encoding="utf-8")
        command=(ROOT/"검증하기.cmd").read_text(encoding="utf-8")
        for marker in ("Get-FileHash", "RELEASE_SHA256SUMS.txt", "SHA256SUMS.txt", "_internal", "fresh-path", "profile_create_switch_isolation", "network_oauth", "ZISU Validation 한글_日本語"):
            self.assertIn(marker,validator)
        self.assertIn("WindowsSandbox.exe",sandbox)
        self.assertIn("<Networking>Disable</Networking>",sandbox)
        self.assertIn("validate_windows_release.ps1",command)
        self.assertIn("security setting was changed",sandbox)

    def test_exe_smoke_creates_and_switches_isolated_test_profiles(self):
        gui=(ROOT/"mcrs/gui.py").read_text(encoding="utf-8")
        self.assertIn("ZISU_EXE_SMOKE_PROFILE_TEST",gui)
        self.assertIn("profile_switch_verified",gui)
        self.assertIn("search_terms_isolated",gui)
