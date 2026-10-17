import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class LauncherTests(unittest.TestCase):
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
