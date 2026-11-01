"""Synthetic Windows-only tests for the EFS-independent memory backup candidate."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from scripts import qa_wal_backup_restore as backup
from tests.support import WorkspaceTemp


@unittest.skipUnless(os.name == "nt", "Windows file sharing and CurrentUser DPAPI are required")
class MemoryDpapiZeroWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        self.db = self.root / "synthetic_v459.sqlite"
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self._make_detached_wal()

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def _make_detached_wal(self):
        script = r'''import json,os,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]); c=sqlite3.connect(p); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('PRAGMA foreign_keys=ON')
c.executescript("""
CREATE TABLE profiles(id TEXT PRIMARY KEY, market TEXT NOT NULL);
CREATE TABLE genres(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE candidates(profile_id TEXT NOT NULL REFERENCES profiles(id), video_id TEXT NOT NULL, human_fit TEXT, note TEXT, PRIMARY KEY(profile_id,video_id));
CREATE INDEX idx_candidate_profile ON candidates(profile_id);
CREATE TRIGGER keep_note BEFORE UPDATE OF note ON candidates WHEN NEW.note IS NULL BEGIN SELECT RAISE(ABORT,'note required'); END;
""")
c.executemany('INSERT INTO profiles VALUES(?,?)',[('synthetic-jp','JP'),('synthetic-kr','KR')])
c.executemany('INSERT INTO genres VALUES(?,?)',[(f'g{i}',f'fake genre {i}') for i in range(6)])
c.executemany('INSERT INTO candidates VALUES(?,?,?,?)',[("synthetic-jp",f"fake_video_{i:02d}",'EXCLUDE' if i==0 else None,'synthetic note' if i==0 else None) for i in range(50)])
c.commit(); print(json.dumps({'committed':50,'wal_bytes':Path(str(p)+'-wal').stat().st_size,'shm_exists':Path(str(p)+'-shm').exists()}),flush=True); os._exit(0)'''
        completed = subprocess.run([sys.executable, "-c", script, str(self.db)],
                                   capture_output=True, text=True, timeout=20)
        self.assertEqual(0, completed.returncode, completed.stderr)
        facts = json.loads(completed.stdout)
        self.assertEqual(50, facts["committed"])
        self.assertGreater(facts["wal_bytes"], 32)
        self.assertTrue(facts["shm_exists"])

    def _files(self):
        paths = [self.db, Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm")]
        result = {}
        for path in paths:
            if path.exists():
                stat = path.stat()
                result[path.name] = (stat.st_size, stat.st_mtime_ns,
                                     hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                result[path.name] = None
        return result

    def _make_closed_wal_without_sidecars(self, name="closed_wal.sqlite"):
        path = self.root / name
        connection = sqlite3.connect(path)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        connection.executescript("""
        CREATE TABLE profiles(id TEXT PRIMARY KEY, market TEXT NOT NULL);
        CREATE TABLE genres(id TEXT PRIMARY KEY);
        CREATE TABLE candidates(profile_id TEXT NOT NULL REFERENCES profiles(id),
          video_id TEXT NOT NULL, human_fit TEXT, note TEXT,
          PRIMARY KEY(profile_id,video_id));
        """)
        connection.executemany("INSERT INTO profiles VALUES(?,?)",
                               [("fixture-jp", "JP"), ("fixture-kr", "KR")])
        connection.executemany("INSERT INTO genres VALUES(?)", [(f"g{i}",) for i in range(6)])
        connection.execute("INSERT INTO candidates VALUES(?,?,?,?)",
                           ("fixture-jp", "manual-1", "EXCLUDE", "research note"))
        connection.commit()
        connection.close()
        self.assertFalse(Path(str(path) + "-wal").exists())
        self.assertFalse(Path(str(path) + "-shm").exists())
        return path

    def test_closed_wal_without_sidecars_uses_immutable_and_roundtrips(self):
        path = self._make_closed_wal_without_sidecars()
        before = self._files_for(path)
        result = backup.create_locked_memory_encrypted_backup(
            path, self.vault, "immutable_closed_wal_01")
        facts = result["source_snapshot"]
        restored = backup.restore_encrypted_backup(result["path"])["database_info"]
        self.assertEqual("0202", facts["header"])
        self.assertEqual("immutable_main_db_without_sidecars", facts["snapshot_mode"])
        self.assertEqual("not_proven_previous_wal_may_have_been_lost",
                         facts["history_completeness"])
        self.assertEqual(6, restored["table_counts"]["genres"])
        self.assertEqual(2, restored["table_counts"]["profiles"])
        self.assertEqual(1, restored["table_counts"]["candidates"])
        self.assertEqual("ok", restored["integrity"])
        self.assertEqual("ok", restored["foreign_keys"])
        self.assertEqual(before, self._files_for(path))

    def _files_for(self, path):
        paths = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
        return {item.name: ((item.stat().st_size, item.stat().st_mtime_ns,
                             hashlib.sha256(item.read_bytes()).hexdigest())
                            if item.exists() else None) for item in paths}

    def test_wal_snapshot_dpapi_roundtrip_preserves_source_without_plaintext_stage(self):
        before = self._files()
        image, source_info, snapshot_info, facts = backup.create_locked_memory_snapshot(self.db)
        self.assertEqual(source_info, snapshot_info)
        self.assertEqual("0202", facts["header"])
        self.assertTrue(facts["source_files_unchanged"])
        self.assertFalse(facts["shm_copied"])
        self.assertEqual(50, source_info["table_counts"]["candidates"])
        self.assertEqual(before, self._files())

        result = backup.create_locked_memory_encrypted_backup(self.db, self.vault, "memory_wal_01")
        restored = backup.restore_encrypted_backup(result["path"])
        self.assertEqual("CurrentUser", result["dpapi_scope"])
        self.assertEqual(0, result["plaintext_snapshot_files"])
        self.assertEqual(source_info["logical_sha256"], restored["database_info"]["logical_sha256"])
        self.assertEqual("ok", restored["database_info"]["integrity"])
        self.assertEqual("ok", restored["database_info"]["foreign_keys"])
        # Fixture's logical digest includes the EXCLUDE label and human note;
        # exact equality is the preservation assertion for both values.
        self.assertEqual(before, self._files())
        self.assertEqual(["memory_wal_01.dpapi"], sorted(p.name for p in self.vault.iterdir()))

    def test_held_share_deny_write_handles_block_writer(self):
        held = []
        try:
            for path in (self.db, Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm")):
                kernel, handle = backup._open_share_deny_write_read(path)
                held.append((kernel, handle))
            writer = r'''import sqlite3,sys
try:
 c=sqlite3.connect(sys.argv[1],timeout=1); c.execute("INSERT INTO candidates VALUES('synthetic-jp','writer_probe',NULL,NULL)"); c.commit(); sys.exit(0)
except sqlite3.Error: sys.exit(9)'''
            result = subprocess.run([sys.executable, "-c", writer, str(self.db)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(9, result.returncode, result.stdout + result.stderr)
        finally:
            for kernel, handle in reversed(held):
                kernel.CloseHandle(handle)
        self.assertEqual(50, backup.create_locked_memory_snapshot(self.db)[1]["table_counts"]["candidates"])

    def test_existing_sqlite_connection_blocks_snapshot(self):
        source = sqlite3.connect(self.db, timeout=1)
        try:
            with self.assertRaises(backup.BackupBlocked):
                backup.create_locked_memory_snapshot(self.db)
        finally:
            source.close()

    def test_wal_without_existing_shm_is_refused_without_creating_source_sidecars(self):
        shm = Path(str(self.db) + "-shm")
        self.assertTrue(shm.exists())
        shm.unlink()  # only the newly-created synthetic fixture is touched
        self.assertFalse(shm.exists())
        with self.assertRaises(backup.BackupBlocked):
            backup.create_locked_memory_snapshot(self.db)
        self.assertFalse(shm.exists())

    def test_shm_without_existing_wal_is_refused(self):
        wal = Path(str(self.db) + "-wal")
        shm = Path(str(self.db) + "-shm")
        self.assertTrue(wal.exists() and shm.exists())
        wal.unlink()
        before = self._files()
        with self.assertRaisesRegex(backup.BackupBlocked, "incomplete WAL/SHM"):
            backup.create_locked_memory_snapshot(self.db)
        self.assertEqual(before, self._files())

    def test_immutable_path_blocks_writer_and_replacement_while_handle_is_held(self):
        path = self._make_closed_wal_without_sidecars("locked_closed_wal.sqlite")
        kernel, handle = backup._open_share_deny_write_read(path)
        try:
            script = r'''import os,sqlite3,sys
p=sys.argv[1]
try:
 c=sqlite3.connect(p,timeout=.2); c.execute("INSERT INTO profiles VALUES('writer','JP')"); c.commit(); c.close(); sys.exit(1)
except Exception: pass
try:
 os.replace(p,p+'.replaced'); sys.exit(2)
except Exception: sys.exit(0)'''
            child = subprocess.run([sys.executable, "-c", script, str(path)],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(0, child.returncode, child.stdout + child.stderr)
        finally:
            kernel.CloseHandle(handle)
        self.assertTrue(path.exists())
        self.assertFalse(Path(str(path) + ".replaced").exists())

    def test_corrupt_sidecar_free_wal_header_is_blocked(self):
        path = self._make_closed_wal_without_sidecars("corrupt_closed_wal.sqlite")
        data = bytearray(path.read_bytes())
        data[-64:] = b"\xff" * 64
        path.write_bytes(data)
        before = self._files_for(path)
        with self.assertRaises((sqlite3.DatabaseError, ValueError, backup.BackupBlocked)):
            backup.create_locked_memory_snapshot(path)
        self.assertEqual(before, self._files_for(path))

    def test_size_limit_fails_before_publish_and_leaves_sources_unchanged(self):
        before = self._files()
        with self.assertRaises(backup.BackupBlocked):
            backup.create_locked_memory_encrypted_backup(self.db, self.vault, "too_large_01",
                                                          max_source_bytes=1)
        self.assertEqual(before, self._files())
        self.assertEqual([], list(self.vault.iterdir()))

    def test_memory_allocation_failure_fails_closed_without_partial_backup(self):
        before = self._files()
        with mock.patch.object(backup, "make_memory_snapshot", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                backup.create_locked_memory_encrypted_backup(self.db, self.vault, "memory_error_01")
        self.assertEqual(before, self._files())
        self.assertEqual([], list(self.vault.iterdir()))

    def test_dpapi_encryption_failure_fails_closed_without_partial_backup(self):
        before = self._files()
        with mock.patch.object(backup, "protect_current_user", side_effect=OSError("synthetic DPAPI failure")):
            with self.assertRaisesRegex(OSError, "synthetic DPAPI failure"):
                backup.create_locked_memory_encrypted_backup(self.db, self.vault, "dpapi_error_01")
        self.assertEqual(before, self._files())
        self.assertEqual([], list(self.vault.iterdir()))

    def test_rollback_mode_database_is_snapshotted_without_sidecars(self):
        rollback_db = self.root / "rollback.sqlite"
        conn = sqlite3.connect(rollback_db)
        conn.execute("CREATE TABLE synthetic(value TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO synthetic VALUES(?)", [(f"row-{i}",) for i in range(3)])
        conn.commit()
        conn.close()
        image, info, snapshot, facts = backup.create_locked_memory_snapshot(rollback_db)
        self.assertEqual(info, snapshot)
        self.assertEqual("0101", facts["header"])
        self.assertEqual(3, info["table_counts"]["synthetic"])
        self.assertFalse(Path(str(rollback_db) + "-wal").exists())
        self.assertFalse(Path(str(rollback_db) + "-shm").exists())

    def test_handled_prepublication_failure_removes_partial_and_never_posts_final(self):
        before = self._files()
        def fail_before_commit():
            raise RuntimeError("synthetic prepublication stop")
        with self.assertRaisesRegex(RuntimeError, "synthetic prepublication stop"):
            backup.create_locked_memory_encrypted_backup(
                self.db, self.vault, "interrupted_01", before_commit=fail_before_commit)
        self.assertEqual(before, self._files())
        self.assertEqual([], list(self.vault.iterdir()))

    def test_abrupt_child_exit_leaves_only_dpapi_ciphertext_partial(self):
        script = r'''import os,sys
from scripts.qa_wal_backup_restore import create_locked_memory_encrypted_backup
create_locked_memory_encrypted_backup(sys.argv[1],sys.argv[2],"crash_01",before_commit=lambda:os._exit(23))'''
        child = subprocess.run([sys.executable, "-c", script, str(self.db), str(self.vault)],
                               cwd=str(Path(__file__).resolve().parents[1]),
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(23, child.returncode, child.stderr)
        partial = self.vault / "crash_01.dpapi.partial"
        self.assertTrue(partial.is_file())
        raw = partial.read_bytes()
        self.assertGreater(len(raw), 32)
        self.assertNotEqual(b"SQLite format 3\x00", raw[:16])
        self.assertFalse((self.vault / "crash_01.dpapi").exists())
        self.assertEqual({"crash_01.dpapi.partial"}, {p.name for p in self.vault.iterdir()})
        self.assertEqual(50, backup.restore_encrypted_backup(partial)["database_info"]["table_counts"]["candidates"])


if __name__ == "__main__":
    unittest.main()
