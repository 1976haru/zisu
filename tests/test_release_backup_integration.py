"""Windows integration of the real release PowerShell path with fake SQLite only."""
from __future__ import annotations

import json
import io
import os
import contextlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import zipfile
from unittest import mock
from urllib.parse import quote

from mcrs import manual_research, store
from mcrs.genres import EXCLUDE
from scripts import zisu_release_backup_cli as release_backup
from scripts.qa_wal_backup_restore import (
    BackupBlocked, normalize_restore_image, restore_encrypted_backup, unprotect_current_user,
)
from tests.support import WorkspaceTemp


ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which("powershell.exe") or shutil.which("powershell")


@unittest.skipUnless(os.name == "nt" and PS, "Windows PowerShell and DPAPI are required")
class ReleaseBackupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        (self.root / ".zisu-synthetic-backup-fixture").write_text(
            "ZISU_RELEASE_BACKUP_FIXTURE_ONLY_V1\n", encoding="ascii")
        self.db = self.root / "fixture_db.sqlite"
        store.initialize(self.db)
        jp = store.create_profile("Synthetic JP", "JP", "MORNING_CHILL_RAP", self.db)
        kr = store.create_profile("Synthetic KR", "KR", "RNB_HIPHOP", self.db)
        with store.connect(self.db) as db:
            db.execute("INSERT INTO google_accounts(id,email,normalized_email) VALUES('fake-account','','')")
            db.executemany("INSERT INTO google_account_channels(account_id,channel_id,channel_title) VALUES(?,?,?)",
                           [("fake-account", "fake-jp-channel", "Synthetic JP channel"),
                            ("fake-account", "fake-kr-channel", "Synthetic KR channel")])
            db.execute("UPDATE profiles SET google_account_id='fake-account',youtube_channel_id='fake-jp-channel' WHERE id=?", (jp,))
            db.execute("UPDATE profiles SET google_account_id='fake-account',youtube_channel_id='fake-kr-channel' WHERE id=?", (kr,))
        urls = "\n".join(f"https://www.youtube.com/watch?v=v{i:010d}" for i in range(50))
        self.batch = manual_research.create_batch(jp, urls, path=self.db)
        manual_research.save_evaluation(
            self.batch, jp, "v0000000000",
            {"human_fit": EXCLUDE, "notes": "synthetic exclusion note"}, self.db)
        # Repeated additive initialization must leave both profiles, labels,
        # notes and the 50 ordered manual rows intact before backup.
        store.initialize(self.db)
        store.initialize(self.db)
        self.jp, self.kr = jp, kr
        # Keep ordinary CLI fixtures as clean rollback journals. WAL-specific
        # flows opt into an explicit detached DB+WAL image below.
        db = sqlite3.connect(self.db)
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        finally:
            db.close()

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def _invoke_release_fixture(self):
        script = ROOT / "scripts" / "zisu_release_oneclick.ps1"
        return subprocess.run(
            [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-RepoPath", str(ROOT), "-FixtureOnly", "-FixtureRoot", str(self.root)],
            cwd=ROOT, capture_output=True, text=True, timeout=90, check=False)

    def _invoke_fixture_cli(self, memory_dpapi=False, backup_id="manual_fixture_01"):
        vault = self.root / "cli_vault"
        vault.mkdir()
        helper = ROOT / "scripts" / "zisu_release_backup_cli.py"
        args = [sys.executable, str(helper), "--database", str(self.db), "--vault", str(vault),
                "--backup-id", backup_id, "--fixture-root", str(self.root)]
        if memory_dpapi:
            args.append("--memory-dpapi")
        else:
            args.append("--legacy-efs")
        return subprocess.run(
            args,
            cwd=ROOT, capture_output=True, text=True, timeout=90, check=False), vault

    def _convert_to_detached_wal_fixture(self):
        staged = self.root / "detached_wal_build.sqlite"
        shutil.copy2(self.db, staged)
        writer = sqlite3.connect(staged)
        try:
            self.assertEqual("wal", writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE profiles SET name=name WHERE id=?", (self.jp,))
            writer.commit()
            self.assertTrue(Path(str(staged) + "-wal").exists())
            shutil.copy2(staged, self.db)
            shutil.copy2(Path(str(staged) + "-wal"), Path(str(self.db) + "-wal"))
            if Path(str(staged) + "-shm").exists():
                shutil.copy2(Path(str(staged) + "-shm"), Path(str(self.db) + "-shm"))
        finally:
            writer.close()

    def test_shared_release_cli_encrypts_and_restores_migrated_50_item_fixture(self):
        result, vault = self._invoke_fixture_cli()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual("PASS", facts["result"])
        self.assertEqual("0101", facts["database_header_before_restore"])
        self.assertTrue(facts["restore_verified"])
        self.assertEqual("CurrentUser", facts["dpapi_scope"])
        self.assertEqual(0, facts["plaintext_backup_files"])
        self.assertEqual(50, facts["table_counts"]["manual_research_items"])
        backups = list(vault.glob("*.dpapi"))
        self.assertEqual(1, len(backups))
        restored = restore_encrypted_backup(backups[0])
        self.assertEqual(50, restored["database_info"]["table_counts"]["manual_research_items"])
        self.assertEqual(2, restored["database_info"]["table_counts"]["profiles"])
        db = sqlite3.connect(":memory:")
        try:
            with zipfile.ZipFile(io.BytesIO(unprotect_current_user(backups[0].read_bytes()))) as archive:
                db.deserialize(normalize_restore_image(archive.read("database.sqlite")))
                self.assertEqual((EXCLUDE, "synthetic exclusion note"), db.execute(
                    "SELECT human_fit,notes FROM manual_research_items WHERE video_id='v0000000000'").fetchone())
                self.assertEqual(0, db.execute(
                    "SELECT COUNT(*) FROM manual_research_items WHERE profile_id=?", (self.kr,)).fetchone()[0])
                self.assertEqual(50, db.execute(
                    "SELECT COUNT(*) FROM manual_research_items WHERE profile_id=?", (self.jp,)).fetchone()[0])
                self.assertEqual({("Synthetic JP", "fake-jp-channel"), ("Synthetic KR", "fake-kr-channel")},
                                 set(db.execute("SELECT name,youtube_channel_id FROM profiles")))
                self.assertEqual(2, db.execute("SELECT COUNT(*) FROM google_account_channels WHERE account_id='fake-account'").fetchone()[0])
        finally:
            db.close()

    def test_shared_release_cli_rejects_malformed_source_header_without_backup(self):
        with self.db.open("r+b") as stream:
            stream.seek(18)
            stream.write(b"\x02\x01")
        result, vault = self._invoke_fixture_cli()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('"result": "FAIL"', result.stderr)
        self.assertIn('"phase": "locked_source_snapshot"', result.stderr)
        self.assertEqual([], list(vault.glob("*.dpapi")))
        self.assertEqual([], list(vault.glob("*.dpapi.partial")))

    def test_production_wal_snapshot_preserves_uncheckpointed_rows_without_source_writes(self):
        writer_path = self.root / "wal_writer.sqlite"
        source_path = self.root / "production_wal.sqlite"
        observe_path = self.root / "readonly_observer.sqlite"
        connection = sqlite3.connect(writer_path)
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE synthetic_rows(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany("INSERT INTO synthetic_rows(value) VALUES(?)",
                                   [(f"fixture-row-{i}",) for i in range(30)])
            connection.commit()
            wal_path = Path(str(writer_path) + "-wal")
            shm_path = Path(str(writer_path) + "-shm")
            self.assertTrue(wal_path.exists())
            self.assertTrue(shm_path.exists())
            self.assertGreater(wal_path.stat().st_size, 32)
            shutil.copy2(writer_path, source_path)
            shutil.copy2(wal_path, Path(str(source_path) + "-wal"))
            shutil.copy2(shm_path, Path(str(source_path) + "-shm"))
            shutil.copy2(writer_path, observe_path)
            shutil.copy2(wal_path, Path(str(observe_path) + "-wal"))
            shutil.copy2(shm_path, Path(str(observe_path) + "-shm"))
        finally:
            connection.close()

        observed_before = release_backup._source_fingerprint(observe_path)
        uri = "file:" + quote(observe_path.resolve().as_posix(), safe="/:") + "?mode=ro"
        observer = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            self.assertEqual(30, observer.execute("SELECT COUNT(*) FROM synthetic_rows").fetchone()[0])
        finally:
            observer.close()
        observed_after = release_backup._source_fingerprint(observe_path)
        observed_changes = [name for name in observed_before if observed_before[name] != observed_after[name]]
        print("SYNTHETIC_READONLY_SQLITE_CHANGED_FILES", ",".join(observed_changes) or "none")
        self.assertIn("readonly_observer.sqlite-shm", observed_changes)
        shm_fields = [field for field in observed_before["readonly_observer.sqlite-shm"]
                      if observed_before["readonly_observer.sqlite-shm"][field] !=
                      observed_after["readonly_observer.sqlite-shm"][field]]
        print("SYNTHETIC_READONLY_SHM_CHANGED_FIELDS", ",".join(shm_fields))
        self.assertTrue(shm_fields)
        self.assertEqual(observed_before["readonly_observer.sqlite"], observed_after["readonly_observer.sqlite"])
        self.assertEqual(observed_before["readonly_observer.sqlite-wal"], observed_after["readonly_observer.sqlite-wal"])

        # The active writer must be gone before the snapshot; copied files are
        # an isolated synthetic DB/WAL/SHM set with a committed uncheckpointed row set.
        connection.close()
        vault = self.root / "production_vault"
        vault.mkdir()
        before = release_backup._source_fingerprint(source_path)
        result = release_backup.run_backup(
            source_path, vault, "synthetic_production_wal_01", fixture_root=self.root)
        after = release_backup._source_fingerprint(source_path)
        final_files = list(vault.glob("*.dpapi"))
        self.assertEqual([], list(vault.glob("*.dpapi.partial")))
        self.assertEqual("PASS", result["result"])
        self.assertEqual(30, result["table_counts"]["synthetic_rows"])
        self.assertEqual(["production_wal.sqlite", "production_wal.sqlite-wal"],
                         sorted(result["snapshot_files"]))
        self.assertFalse(result["source_shm_copied"])
        self.assertEqual(1, len(final_files))
        self.assertEqual([], list(vault.glob(".snapshot_*")))
        self.assertEqual(before, after)

        restored = restore_encrypted_backup(final_files[0])
        self.assertEqual("ok", restored["database_info"]["integrity"])
        self.assertEqual(30, restored["database_info"]["table_counts"]["synthetic_rows"])

    def test_shared_release_cli_snapshots_detached_wal_without_copying_shm(self):
        self._convert_to_detached_wal_fixture()
        before = release_backup._source_fingerprint(self.db)
        result, vault = self._invoke_fixture_cli()
        after = release_backup._source_fingerprint(self.db)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual("PASS", facts["result"])
        self.assertEqual("0202", facts["database_header_before_restore"])
        self.assertEqual(50, facts["table_counts"]["manual_research_items"])
        self.assertFalse(facts["source_shm_copied"])
        self.assertIn("fixture_db.sqlite-wal", facts["snapshot_files"])
        self.assertNotIn("fixture_db.sqlite-shm", facts["snapshot_files"])
        self.assertEqual(before, after)
        self.assertEqual(1, len(list(vault.glob("*.dpapi"))))
        self.assertEqual([], list(vault.glob(".snapshot_*")))

    def test_memory_dpapi_cli_preserves_detached_wal_without_disk_snapshot(self):
        self._convert_to_detached_wal_fixture()
        before = release_backup._source_fingerprint(self.db)
        result, vault = self._invoke_fixture_cli(memory_dpapi=True, backup_id="memory_fixture_01")
        after = release_backup._source_fingerprint(self.db)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual("PASS", facts["result"])
        self.assertEqual("MEMORY_DPAPI", facts["backup_mode"])
        self.assertEqual("0202", facts["database_header_before_restore"])
        self.assertTrue(facts["source_file_set_unchanged"])
        self.assertFalse(facts["source_shm_copied"])
        self.assertEqual("none_memory_only", facts["plaintext_snapshot_staging"])
        self.assertEqual(50, facts["table_counts"]["manual_research_items"])
        self.assertEqual(before, after)
        self.assertEqual(["memory_fixture_01.dpapi"], sorted(path.name for path in vault.iterdir()))
        restored = restore_encrypted_backup(vault / "memory_fixture_01.dpapi")
        self.assertEqual("ok", restored["database_info"]["integrity"])
        self.assertEqual("ok", restored["database_info"]["foreign_keys"])
        self.assertEqual(facts["logical_sha256"], restored["database_info"]["logical_sha256"])

    def test_memory_dpapi_cli_abrupt_fixture_exit_leaves_only_authenticated_partial(self):
        self._convert_to_detached_wal_fixture()
        vault = self.root / "crash_cli_vault"
        vault.mkdir()
        helper = ROOT / "scripts" / "zisu_release_backup_cli.py"
        child = subprocess.run(
            [sys.executable, str(helper), "--database", str(self.db), "--vault", str(vault),
             "--backup-id", "cli_crash_fixture_01", "--fixture-root", str(self.root),
             "--memory-dpapi", "--fixture-crash-after-partial"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(23, child.returncode, child.stderr)
        partial = vault / "cli_crash_fixture_01.dpapi.partial"
        self.assertTrue(partial.is_file())
        self.assertFalse((vault / "cli_crash_fixture_01.dpapi").exists())
        reopened = restore_encrypted_backup(partial)
        self.assertEqual(50, reopened["database_info"]["table_counts"]["manual_research_items"])

    def test_memory_mode_failure_does_not_fall_back_to_efs_snapshot_path(self):
        vault = self.root / "no_fallback_vault"
        vault.mkdir()
        with mock.patch.object(release_backup, "create_locked_memory_encrypted_backup",
                               side_effect=BackupBlocked("synthetic memory-path stop")):
            with mock.patch.object(release_backup, "run_backup") as efs_path:
                with contextlib.redirect_stderr(io.StringIO()):
                    code = release_backup.main([
                        "--database", str(self.db), "--vault", str(vault),
                        "--backup-id", "no_fallback_01", "--fixture-root", str(self.root),
                        "--memory-dpapi",
                    ])
        self.assertEqual(1, code)
        efs_path.assert_not_called()
        self.assertEqual([], list(vault.iterdir()))

    def test_snapshot_refuses_live_sqlite_connection_and_leaves_no_partial_artifacts(self):
        writer = sqlite3.connect(self.db, timeout=1)
        writer.execute("SELECT COUNT(*) FROM profiles").fetchone()
        vault = self.root / "live_lock_vault"
        vault.mkdir()
        before = release_backup._source_fingerprint(self.db)
        try:
            with self.assertRaises(BackupBlocked):
                release_backup.run_backup(self.db, vault, "live_writer_blocked_01", fixture_root=self.root)
        finally:
            writer.close()
        self.assertEqual(before, release_backup._source_fingerprint(self.db))
        self.assertEqual([], list(vault.glob("*.dpapi*")))
        self.assertEqual([], list(vault.glob(".snapshot_*")))

    def test_snapshot_rejects_missing_wal_and_detected_copy_mutation(self):
        self._convert_to_detached_wal_fixture()
        vault = self.root / "bad_wal_vault"
        vault.mkdir()
        wal = Path(str(self.db) + "-wal")
        wal.unlink()
        with self.assertRaises(BackupBlocked):
            release_backup.run_backup(self.db, vault, "missing_wal_blocked_01", fixture_root=self.root)
        self.assertEqual([], list(vault.glob("*.dpapi*")))
        self.assertEqual([], list(vault.glob(".snapshot_*")))

        good = self.root / "copy_change.sqlite"
        connection = sqlite3.connect(good)
        connection.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO t(value) VALUES('fixture')")
        connection.commit()
        connection.close()
        staging = self.root / "copy_change_stage"
        staging.mkdir()
        real_read = __import__("scripts.qa_wal_backup_restore", fromlist=["_copy_windows_handle"])._copy_windows_handle
        calls = {"count": 0}
        def altered_second_read(kernel, handle, destination=None):
            calls["count"] += 1
            value = real_read(kernel, handle, destination)
            return (value[0] + 8, "0" * 64, value[2]) if calls["count"] == 2 else value
        with mock.patch("scripts.qa_wal_backup_restore._copy_windows_handle", side_effect=altered_second_read):
            with self.assertRaises(BackupBlocked):
                __import__("scripts.qa_wal_backup_restore", fromlist=["create_isolated_file_snapshot"]).create_isolated_file_snapshot(good, staging)
        self.assertEqual([], list(staging.iterdir()))

    def test_release_cli_rejects_corrupt_wal_checksum_without_final_backup(self):
        self._convert_to_detached_wal_fixture()
        wal = Path(str(self.db) + "-wal")
        raw = bytearray(wal.read_bytes())
        self.assertGreater(len(raw), 32)
        raw[-1] ^= 0x01
        wal.write_bytes(raw)
        vault = self.root / "corrupt_wal_vault"
        vault.mkdir()
        with self.assertRaises(BackupBlocked):
            release_backup.run_backup(self.db, vault, "corrupt_wal_blocked_01", fixture_root=self.root)
        self.assertEqual([], list(vault.glob("*.dpapi*")))
        self.assertEqual([], list(vault.glob(".snapshot_*")))

    @unittest.skipIf(os.environ.get("ZISU_SKIP_PROTECTED_ACL_FIXTURE") == "1",
                     "SANDBOX_LIMITATION: current-user-only protected DACL write was already reproduced as denied")
    def test_real_release_script_uses_encrypted_wal_memory_backup_for_50_item_fixture(self):
        self._convert_to_detached_wal_fixture()
        self.assertEqual(b"\x02\x02", self.db.read_bytes()[18:20])
        result = self._invoke_release_fixture()
        if result.returncode:
            failure = json.loads(result.stdout.strip().splitlines()[-1])
            if failure.get("phase") == "user_only_acl_write_probe":
                self.skipTest("managed Windows sandbox denies writes under a current-user-only protected DACL")
            if failure.get("safe_error_type") == "RuntimeException" and failure.get("phase") == "write_ciphertext_partial":
                self.skipTest("managed Windows sandbox denies fixture ciphertext creation under the protected DACL")
            self.fail(f"PowerShell release fixture failed: {failure}")
        facts = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", facts["result"])
        self.assertEqual("0202", facts["database_header_before_restore"])
        self.assertTrue(facts["restore_verified"])
        self.assertEqual("CurrentUser", facts["dpapi_scope"])
        self.assertEqual("MEMORY_DPAPI", facts["backup_mode"])
        self.assertEqual(0, facts["plaintext_backup_files"])
        self.assertTrue(facts["source_file_set_unchanged"])
        self.assertEqual("PASS", facts["crash_partial_acl"])
        self.assertFalse(facts["crash_final_published"])
        vault = self.root / "vault"
        backups = list(vault.glob("*.dpapi"))
        self.assertEqual(1, len(backups))
        self.assertEqual(1, len(list(vault.glob("*.partial"))))
        self.assertTrue(Path(facts["crash_partial_path"]).is_file())
        self.assertEqual([], list(vault.glob("*.sqlite")))

        restored = restore_encrypted_backup(backups[0])
        info = restored["database_info"]
        self.assertEqual("ok", info["integrity"])
        self.assertEqual("ok", info["foreign_keys"])
        self.assertEqual(50, info["table_counts"]["manual_research_items"])
        self.assertEqual(2, info["table_counts"]["profiles"])
        # The helper's authenticated restore has already compared full schema
        # and logical digests; assert the profile-scoped human data explicitly.
        db = sqlite3.connect(":memory:")
        try:
            with zipfile.ZipFile(io.BytesIO(unprotect_current_user(backups[0].read_bytes()))) as archive:
                db.deserialize(normalize_restore_image(archive.read("database.sqlite")))
                labels = db.execute(
                    "SELECT human_fit,notes FROM manual_research_items WHERE video_id='v0000000000'").fetchone()
                self.assertEqual((EXCLUDE, "synthetic exclusion note"), labels)
                self.assertEqual(0, db.execute(
                    "SELECT COUNT(*) FROM manual_research_items WHERE profile_id=?", (self.kr,)).fetchone()[0])
                self.assertEqual(50, db.execute(
                    "SELECT COUNT(*) FROM manual_research_items WHERE profile_id=?", (self.jp,)).fetchone()[0])
        finally:
            db.close()

    @unittest.skipIf(os.environ.get("ZISU_SKIP_PROTECTED_ACL_FIXTURE") == "1",
                     "SANDBOX_LIMITATION: malformed-header PowerShell path requires the same blocked protected vault")
    def test_bad_fixture_header_is_rejected_without_final_backup(self):
        with self.db.open("r+b") as stream:
            stream.seek(18)
            stream.write(b"\x02\x01")
        result = self._invoke_release_fixture()
        if result.returncode:
            failure = json.loads(result.stdout.strip().splitlines()[-1])
            if failure.get("phase") == "user_only_acl_write_probe":
                self.skipTest("managed Windows sandbox denies the protected fixture vault ACL")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], list((self.root / "vault").glob("*.dpapi")))


if __name__ == "__main__":
    unittest.main()
