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
        self.assertIn("package_windows.ps1",workflow)
        self.assertIn("--onedir",workflow)
        self.assertIn("--onefile",workflow)
        self.assertIn("--collect-submodules google_auth_oauthlib",workflow)
        self.assertIn("--collect-data google_auth_oauthlib",workflow)
        self.assertIn("fresh_path_launches",workflow)
        self.assertIn("warm_process_launches",workflow)
        self.assertIn("startup-comparison.json",workflow)
        self.assertIn("ZISU 배포 日本語 path",workflow)
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
        self.assertIn("_internal",package)
        self.assertIn("Refusing to overwrite existing package",package)
        for marker in ("client_secret","oauth_tokens","token\\.json","\\.db$","\\.log$"):
            self.assertIn(marker,package)
