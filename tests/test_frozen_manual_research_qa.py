import os
from pathlib import Path
import subprocess
import socket
import unittest
from unittest.mock import patch

import main
from mcrs import frozen_manual_research_qa as qa
from support import WorkspaceTemp


class FrozenManualResearchQaTests(unittest.TestCase):
    def test_frozen_manual_flags_do_not_preseed_product_data_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            main._apply_offline_smoke_arguments([
                "--qa-manual-research-e2e", "--data-dir", "fixture-data",
                "--report-dir", "fixture-reports",
            ])
            self.assertNotIn("ZISU_DATA_DIR", os.environ)

    def test_qa_root_requires_new_manual_e2e_layout_and_refuses_reuse(self):
        with WorkspaceTemp() as parent:
            good = parent / "V46_QA_manual_fixture"
            root, data, reports = qa._validate_root(good, fresh=True)
            self.assertEqual(good, root)
            self.assertEqual(good / "data", data)
            self.assertEqual(good / "reports", reports)
            with self.assertRaises(ValueError):
                qa._validate_root(parent / "ordinary-run", fresh=True)
            good.mkdir()
            with self.assertRaises(ValueError):
                qa._validate_root(good, fresh=True)

    def test_qa_root_rejects_relative_path_that_resolves_into_fixture_user_data(self):
        with WorkspaceTemp() as parent:
            local = parent / "synthetic-local"
            protected = local / "ZisuResearchStudio"
            protected.mkdir(parents=True)
            candidate = protected / ".." / "ZisuResearchStudio" / "V46_QA_manual_overlap"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=True):
                with self.assertRaises(ValueError):
                    qa._validate_root(candidate, fresh=True)

    def test_qa_root_rejects_junction_alias_into_fixture_user_data(self):
        with WorkspaceTemp() as parent:
            local = parent / "synthetic-local"
            target = local / "ZisuResearchStudio" / "V46_QA_manual_target"
            (target / "data").mkdir(parents=True)
            (target / "reports").mkdir()
            alias = parent / "V46_QA_manual_alias"
            junction = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                                       str(alias), str(target)],
                                      capture_output=True, text=True, check=False)
            if junction.returncode != 0:
                self.skipTest("NTFS junction unavailable in this user session.")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=True):
                with self.assertRaises(ValueError):
                    qa._validate_root(alias, fresh=False)

    def test_fixture_environment_redirects_data_credentials_and_temp(self):
        with WorkspaceTemp() as parent:
            root = parent / "V46_QA_manual_fixture"
            data, reports = root / "data", root / "reports"
            with patch.dict(os.environ, {
                "YOUTUBE_API_KEY": "fake-fixture-key",
                "GOOGLE_APPLICATION_CREDENTIALS": "fake-client.json",
                "ZISU_AUTO_RESEARCH_REQUEST_ID": "fake-request",
            }, clear=True):
                env = qa._prepare_environment(root, data, reports)
            self.assertEqual(str(data), env["ZISU_DATA_DIR"])
            self.assertEqual(str(root / "localappdata"), env["LOCALAPPDATA"])
            self.assertEqual(str(root / "appdata"), env["APPDATA"])
            self.assertEqual(str(root / "temp"), env["TEMP"])
            self.assertNotIn("YOUTUBE_API_KEY", env)
            self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", env)
            self.assertFalse((root / "ZisuResearchStudio").exists())

    def test_resume_root_allows_only_qa_redirected_local_and_roaming_paths(self):
        with WorkspaceTemp() as parent:
            root = parent / "V46_QA_manual_fixture"
            env = qa._prepare_environment(root, root / "data", root / "reports")
            with patch.dict(os.environ, env, clear=True):
                checked_root, data, reports = qa._validate_root(root, fresh=False)
                self.assertEqual(root, checked_root)
                self.assertEqual(root / "data", data)
                self.assertEqual(root / "reports", reports)

    def test_resume_fails_closed_and_reports_fixture_path_mismatch(self):
        with WorkspaceTemp() as parent:
            root = parent / "V46_QA_manual_fixture"
            (root / "data").mkdir(parents=True)
            (root / "reports").mkdir()
            env = {
                "ZISU_DATA_DIR": str(parent / "wrong-data"),
                "LOCALAPPDATA": str(root / "localappdata"),
                "APPDATA": str(root / "appdata"),
                "TEMP": str(root / "temp"), "TMP": str(root / "temp"),
                "ZISU_FROZEN_MANUAL_E2E": "1",
            }
            with patch.dict(os.environ, env, clear=True):
                result = qa.run_cli(["--qa-manual-research-e2e-resume", "--qa-root", str(root)])
            self.assertEqual(2, result)
            report = __import__("json").loads((root / "reports" / "resume-e2e.json").read_text(encoding="utf-8"))
            self.assertEqual("FixtureEnvironmentMismatch", report["error_type"])
            self.assertEqual(["ZISU_DATA_DIR"], report["failed_checks"])

    def test_network_guard_blocks_and_counts_socket_resolution_and_connect(self):
        with qa._block_network() as attempts:
            with self.assertRaises(OSError):
                socket.getaddrinfo("example.invalid", 443)
            self.assertEqual(1, attempts["count"])

    def test_manual_e2e_entrypoint_fails_closed_without_frozen_windows_exe(self):
        with patch.object(main.sys, "frozen", False, create=True):
            with self.assertRaises(SystemExit):
                main._run_manual_research_e2e(["--qa-manual-research-e2e"])


if __name__ == "__main__":
    unittest.main()
