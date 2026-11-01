"""Tests for the disposable one-click protected-ACL backup fixture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

from scripts import secure_backup_acl_fixture as fixture
from scripts import zisu_release_backup_cli as release_backup
from tests.support import WorkspaceTemp


ROOT = Path(__file__).resolve().parents[1]


class SecureBackupAclFixtureTests(unittest.TestCase):
    def test_one_click_entrypoint_is_explicitly_fixture_only_and_overrides_real_db_default(self):
        cmd = (ROOT / "scripts" / "Run_SecureBackup_Fixture_Only.cmd").read_text(encoding="utf-8")
        ps = (ROOT / "scripts" / "Run_SecureBackup_Fixture_Only.ps1").read_text(encoding="utf-8")
        self.assertIn("Run_SecureBackup_Fixture_Only.ps1", cmd)
        self.assertIn("-FixtureOnly", ps)
        self.assertIn("-FixtureRoot", ps)
        self.assertIn("-DatabasePath',$fixtureDb", ps)
        self.assertIn("-InstallDirectory',$safeInstallTarget", ps)
        self.assertIn("fixture_only=$true", ps)
        self.assertIn("real_database_argument_used=$false", ps)
        self.assertIn("MANAGED_SANDBOX_MUST_NOT_RETRY_DACL", ps)
        self.assertIn("acl-fixture-report.json", ps)
        self.assertIn("cross_user_access='PENDING_NO_SECOND_USER_CREDENTIALS'", ps)
        self.assertIn("if($finalResult.result -ne 'MEMORY_DPAPI_ACL_FIXTURE_PASS'){exit 1}", ps)
        self.assertNotIn("-ExecuteRelease", ps)
        self.assertNotIn("-Install'", ps)

        release = (ROOT / "scripts" / "zisu_release_oneclick.ps1").read_text(encoding="utf-8")
        self.assertIn("'--memory-dpapi'", release)
        self.assertIn("none_memory_only", release)

        cli = (ROOT / "scripts" / "zisu_release_backup_cli.py").read_text(encoding="utf-8")
        self.assertIn('"--memory-dpapi"', cli)
        self.assertIn('"--legacy-efs"', cli)
        self.assertIn("create_locked_memory_encrypted_backup", cli)

    @unittest.skipUnless(os.name == "nt", "Windows share locks and DPAPI are required")
    def test_new_fixture_wal_passes_release_cli_and_corrupt_ciphertext_is_rejected(self):
        with WorkspaceTemp() as root:
            fixture_root = root / "fresh_fixture"
            fixture_root.mkdir()
            facts = fixture.create_fixture(fixture_root)
            self.assertEqual("FIXTURE_READY", facts["result"])
            self.assertEqual("0202", facts["database_header"])
            self.assertEqual(50, facts["manual_items"])
            self.assertTrue(facts["jp_exclude_note"])
            self.assertEqual(0, facts["kr_items"])
            db_path = Path(facts["database"])
            self.assertGreater(Path(str(db_path) + "-wal").stat().st_size, 32)

            before = release_backup._source_fingerprint(db_path)
            vault = fixture_root / "vault"
            vault.mkdir()
            result = release_backup.run_backup(db_path, vault, "oneclick_fixture_backup_01", fixture_root)
            after = release_backup._source_fingerprint(db_path)
            self.assertEqual("PASS", result["result"])
            self.assertEqual(before, after)
            self.assertEqual(50, result["table_counts"]["manual_research_items"])
            self.assertEqual(["fixture_db.sqlite", "fixture_db.sqlite-wal"], sorted(result["snapshot_files"]))
            self.assertFalse(result["source_shm_copied"])
            verified = fixture.verify_corrupt_ciphertext(fixture_root, Path(result["backup_path"]))
            self.assertEqual("CORRUPT_CIPHERTEXT_REJECTED", verified["result"])
            self.assertTrue(verified["backup_reopened"])
            self.assertEqual("ok", verified["integrity"])
            self.assertTrue(verified["corrupt_probe_removed"])
            self.assertEqual([], list(vault.glob(".snapshot_*")))


if __name__ == "__main__":
    unittest.main()
