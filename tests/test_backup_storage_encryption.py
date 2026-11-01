"""Storage-at-rest requirements for release backup staging."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from scripts import zisu_release_backup_cli as release_backup
from scripts.qa_wal_backup_restore import (
    BackupBlocked,
    FILE_ATTRIBUTE_ENCRYPTED,
    _windows_file_attributes,
    protect_snapshot_directory,
)
from tests.support import WorkspaceTemp


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows EFS is required")
class BackupStorageEncryptionTests(unittest.TestCase):
    def test_production_gate_blocks_before_snapshot_when_storage_protection_fails(self):
        with WorkspaceTemp() as root:
            database = root / "synthetic.sqlite"
            database.write_bytes(b"synthetic fixture only")
            vault = root / "vault"
            vault.mkdir()
            with mock.patch.object(
                    release_backup, "protect_snapshot_directory",
                    side_effect=BackupBlocked("fixture EFS denied")) as efs:
                with mock.patch.object(release_backup, "create_isolated_file_snapshot") as snapshot:
                    with self.assertRaises(BackupBlocked) as raised:
                        release_backup.run_backup(database, vault, "efs_denied_01")
            self.assertEqual("protect_snapshot_storage", raised.exception.backup_phase)
            efs.assert_called_once()
            snapshot.assert_not_called()
            self.assertEqual([], list(vault.iterdir()))

    def test_efs_directory_encrypts_crash_residue_in_owned_fixture_child(self):
        if os.environ.get("ZISU_SKIP_EFS_FIXTURE") == "1":
            self.skipTest("PENDING: prior managed-session EFS probe returned WinError 5; do not repeat here")
        with WorkspaceTemp() as root:
            probe_dir = root / "efs_capability_probe"
            probe_dir.mkdir()
            try:
                protect_snapshot_directory(probe_dir)
            except BackupBlocked as exc:
                if getattr(exc, "winerror", None) in (5, 50, 95) or "EFS_UNSUPPORTED" in str(exc):
                    self.skipTest(
                        f"SANDBOX_LIMITATION: Windows denied EFS fixture setup (WinError {exc.winerror})")
                raise
            self.assertTrue(_windows_file_attributes(probe_dir) & FILE_ATTRIBUTE_ENCRYPTED)

            stage = root / "crash_child_stage"
            ready = root / "child_ready.json"
            child_code = r'''
import json, sqlite3, sys, time
from pathlib import Path
from scripts.qa_wal_backup_restore import protect_snapshot_directory, require_efs_encrypted_file
stage=Path(sys.argv[1]); ready=Path(sys.argv[2]); stage.mkdir()
protect_snapshot_directory(stage)
db=stage/'fixture.sqlite'; con=sqlite3.connect(db)
con.execute('CREATE TABLE synthetic_rows(value TEXT NOT NULL)')
con.execute('INSERT INTO synthetic_rows VALUES(?)',('synthetic only',)); con.commit(); con.close()
require_efs_encrypted_file(db)
ready.write_text('ready',encoding='ascii')
time.sleep(120)
'''
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(stage), str(ready)],
                cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not ready.exists() and child.poll() is None:
                    time.sleep(0.05)
                if child.poll() is not None:
                    stderr = child.stderr.read() if child.stderr else ""
                    if "WinError 5" in stderr or "Access is denied" in stderr:
                        self.skipTest("SANDBOX_LIMITATION: child process cannot create EFS fixture")
                    self.fail(f"Owned synthetic child exited before crash point: {stderr[:300]}")
                self.assertTrue(ready.exists(), "synthetic child did not reach the pre-crash checkpoint")
                child.kill()  # only the child process created and owned by this test
                self.assertIsNotNone(child.wait(timeout=10))
                self.assertTrue(_windows_file_attributes(stage) & FILE_ATTRIBUTE_ENCRYPTED)
                self.assertTrue(_windows_file_attributes(stage / "fixture.sqlite") & FILE_ATTRIBUTE_ENCRYPTED)
                self.assertFalse((root / "fixture.dpapi").exists(),
                                 "unverified crash residue must never appear as a completed backup")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)
                if child.stderr:
                    child.stderr.close()

    def test_production_backup_cannot_disable_storage_encryption(self):
        with WorkspaceTemp() as root:
            database = root / "synthetic.sqlite"
            database.write_bytes(b"synthetic fixture only")
            vault = root / "vault"
            vault.mkdir()
            with mock.patch.object(release_backup, "create_isolated_file_snapshot") as snapshot:
                with self.assertRaises(BackupBlocked):
                    release_backup.run_backup(database, vault, "no_efs_bypass_01",
                                              require_storage_encryption=False)
            snapshot.assert_not_called()
            self.assertEqual([], list(vault.iterdir()))


if __name__ == "__main__":
    unittest.main()
