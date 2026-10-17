import unittest
import os
from pathlib import Path
from unittest.mock import patch
import main
ROOT=Path(__file__).resolve().parents[1]
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
        self.assertIn("ZISU 배포 日本語 path",workflow)
    def test_release_scripts_are_present_and_forbid_sensitive_payloads(self):
        smoke=(ROOT/"scripts/windows_exe_smoke.ps1").read_text(encoding="utf-8")
        package=(ROOT/"scripts/package_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("--offline-smoke-report",smoke)
        self.assertIn("--data-dir",smoke)
        self.assertIn("python_removed_from_path",smoke)
        for marker in ("client_secret","oauth_tokens","token\\.json","\\.db$","\\.log$"):
            self.assertIn(marker,package)
