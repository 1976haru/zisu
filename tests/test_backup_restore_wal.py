import hashlib
import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from scripts import qa_wal_backup_restore as backup
from tests.support import WorkspaceTemp


def _make_schema(connection):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE profiles(id TEXT PRIMARY KEY, market TEXT NOT NULL);
        CREATE TABLE candidates(id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id), title TEXT);
        CREATE TABLE human_labels(candidate_id TEXT NOT NULL REFERENCES candidates(id), label TEXT NOT NULL);
        CREATE TABLE notes(candidate_id TEXT NOT NULL REFERENCES candidates(id), note TEXT NOT NULL);
        CREATE TABLE audit_log(id INTEGER PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES candidates(id));
        CREATE INDEX idx_candidates_profile ON candidates(profile_id);
        CREATE TRIGGER note_audit AFTER INSERT ON notes BEGIN
            INSERT INTO audit_log(candidate_id) VALUES(NEW.candidate_id);
        END;
        CREATE VIEW candidate_review AS
            SELECT c.id, c.profile_id, h.label, n.note
            FROM candidates c LEFT JOIN human_labels h ON h.candidate_id=c.id
            LEFT JOIN notes n ON n.candidate_id=c.id;
    """)
    connection.executemany("INSERT INTO profiles VALUES(?,?)", [("jp-fixture", "JP"), ("kr-fixture", "KR")])
    for index in range(50):
        video_id = f"fixture-{index:04d}"
        profile_id = "jp-fixture" if index % 2 == 0 else "kr-fixture"
        connection.execute("INSERT INTO candidates VALUES(?,?,?)", (video_id, profile_id, f"fake title {index}"))
        connection.execute("INSERT INTO human_labels VALUES(?,?)", (video_id, "제외" if index == 0 else "보류"))
        connection.execute("INSERT INTO notes VALUES(?,?)", (video_id, f"synthetic note {index}"))
    connection.commit()


def _create_fixture(path, journal_mode="WAL"):
    connection = sqlite3.connect(path, timeout=2)
    connection.execute(f"PRAGMA journal_mode={journal_mode}")
    if journal_mode.upper() == "WAL":
        connection.execute("PRAGMA wal_autocheckpoint=0")
    _make_schema(connection)
    return connection


class WalDeserializeRootFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def test_wal_serialize_reproduces_cantopen_and_private_header_fix_restores_exactly(self):
        path = self.root / "source_wal.sqlite"
        source = _create_fixture(path, "WAL")
        self.assertEqual(b"\x02\x02", path.read_bytes()[18:20])
        self.assertTrue(Path(str(path) + "-wal").exists())
        self.assertGreater(Path(str(path) + "-wal").stat().st_size, 0)
        source_info = backup.logical_database_info(source)
        snapshot = sqlite3.connect(":memory:")
        source.backup(snapshot)
        self.assertEqual(source_info, backup.logical_database_info(snapshot))
        image = snapshot.serialize()
        self.assertEqual(b"\x02\x02", image[18:20])

        broken = sqlite3.connect(":memory:")
        try:
            broken.deserialize(image)
            with self.assertRaises(sqlite3.OperationalError) as caught:
                broken.execute("PRAGMA integrity_check").fetchall()
            self.assertEqual(sqlite3.SQLITE_CANTOPEN, caught.exception.sqlite_errorcode)
        finally:
            broken.close()

        original_image_hash = hashlib.sha256(image).digest()
        restored, held_buffer, restored_info = backup.restore_memory_image(image)
        try:
            self.assertEqual(b"\x02\x02", image[18:20])
            self.assertEqual(b"\x01\x01", bytes(held_buffer[18:20]))
            self.assertEqual(original_image_hash, hashlib.sha256(image).digest())
            self.assertEqual(source_info, restored_info)
            self.assertEqual(50, restored.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
            self.assertEqual(50, restored.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
        finally:
            restored.close()
            snapshot.close()
            source.close()

    def test_rollback_header_is_unchanged_and_roundtrips(self):
        path = self.root / "source_rollback.sqlite"
        source = _create_fixture(path, "DELETE")
        image, before, snapshot_info = backup.make_memory_snapshot(source)
        self.assertEqual(b"\x01\x01", image[18:20])
        restored, held, after = backup.restore_memory_image(image)
        try:
            self.assertEqual(before, snapshot_info)
            self.assertEqual(before, after)
            self.assertEqual(b"\x01\x01", bytes(held[18:20]))
        finally:
            restored.close()
            source.close()

    def test_only_valid_sqlite_headers_are_accepted(self):
        path = self.root / "valid.sqlite"
        source = _create_fixture(path, "DELETE")
        image, _, _ = backup.make_memory_snapshot(source)
        source.close()
        malformed = (
            b"short",
            b"X" * 16 + image[16:],
            image[:18] + b"\x02\x01" + image[20:],
            image[:18] + b"\x01\x02" + image[20:],
            image[:18] + b"\x03\x03" + image[20:],
            image[:16] + b"\x00\x00" + image[18:],
        )
        for invalid in malformed:
            with self.subTest(header=invalid[:20]):
                with self.assertRaises(ValueError):
                    backup.normalize_restore_image(invalid)

    def test_zero_byte_wal_and_shm_are_observed_but_snapshot_uses_sqlite_api(self):
        path = self.root / "zero_wal.sqlite"
        source = _create_fixture(path, "WAL")
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal = Path(str(path) + "-wal")
        shm = Path(str(path) + "-shm")
        self.assertTrue(wal.exists())
        self.assertEqual(0, wal.stat().st_size)
        self.assertTrue(shm.exists())
        state = backup.sidecar_state(path)
        self.assertEqual(0, state["wal"])
        self.assertTrue(state["shm"])
        image, before, snap = backup.make_memory_snapshot(source)
        restored, held, after = backup.restore_memory_image(image)
        try:
            self.assertEqual(before, snap)
            self.assertEqual(before, after)
            self.assertEqual(b"\x02\x02", image[18:20])
        finally:
            restored.close()
            source.close()

    def test_readonly_access_can_create_synthetic_wal_sidecars(self):
        path = self.root / "readonly_side_effect.sqlite"
        connection = _create_fixture(path, "WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        before_state = backup.sidecar_state(path)
        self.assertIsNone(before_state["wal"])
        self.assertFalse(before_state["shm"])
        main_hash = hashlib.sha256(path.read_bytes()).digest()

        uri = "file:" + path.resolve().as_posix() + "?mode=ro"
        readonly = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            self.assertEqual(50, readonly.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
        finally:
            readonly.close()

        after_state = backup.sidecar_state(path)
        self.assertNotEqual(before_state, after_state)
        self.assertEqual(main_hash, hashlib.sha256(path.read_bytes()).digest())
        self.assertTrue(after_state["wal"] is not None or after_state["shm"])

    def test_missing_wal_state_is_supported_by_backup_api(self):
        path = self.root / "no_wal.sqlite"
        source = _create_fixture(path, "DELETE")
        self.assertIsNone(backup.sidecar_state(path)["wal"])
        self.assertFalse(backup.sidecar_state(path)["shm"])
        image, before, snapshot_info = backup.make_memory_snapshot(source)
        restored, held, after = backup.restore_memory_image(image)
        try:
            self.assertEqual(before, snapshot_info)
            self.assertEqual(before, after)
        finally:
            restored.close()
            source.close()

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics required")
    def test_exclusive_file_probe_blocks_locked_database(self):
        path = self.root / "locked.sqlite"
        path.write_bytes(b"synthetic lock probe only")
        import ctypes
        import ctypes.wintypes as wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotEqual(ctypes.c_void_p(-1).value, ctypes.cast(handle, ctypes.c_void_p).value)
        try:
            with self.assertRaises(backup.BackupBlocked):
                backup.require_exclusive_access(path)
        finally:
            kernel.CloseHandle(handle)

    def test_permission_denied_is_blocked_without_opening_data(self):
        def denied(_path):
            raise PermissionError("synthetic access-denied fixture")
        with self.assertRaisesRegex(backup.BackupBlocked, "access denied"):
            backup.require_exclusive_access(self.root / "not-opened.sqlite", opener=denied)

    def test_dpapi_encrypted_unique_backup_roundtrip_and_invalid_ciphertext_rejected(self):
        if os.name != "nt":
            self.skipTest("DPAPI CurrentUser requires Windows")
        path = self.root / "protected_source.sqlite"
        source = _create_fixture(path, "WAL")
        try:
            backup_dir = self.root / "vault"
            backup_dir.mkdir()
            fake = {"settings/fake.json": b'{"mode":"synthetic","secret":"NOT_REAL"}'}
            result = backup.create_encrypted_backup(source, backup_dir, "fixture_01", fake)
            encrypted = result["path"]
            self.assertTrue(encrypted.is_file())
            self.assertEqual(b"\x02\x02", bytes.fromhex(result["manifest"]["original_header"]))
            self.assertEqual(b"\x01\x01", bytes.fromhex(result["manifest"]["restore_header"]))
            restored = backup.restore_encrypted_backup(encrypted)
            self.assertEqual(result["database_info"], restored["database_info"])
            self.assertEqual(fake, restored["files"])
            ciphertext = encrypted.read_bytes()
            with self.assertRaises(OSError):
                backup.unprotect_current_user(ciphertext[:-1] + bytes([ciphertext[-1] ^ 0x01]))
            before = hashlib.sha256(ciphertext).digest()
            with self.assertRaises(FileExistsError):
                backup.create_encrypted_backup(source, backup_dir, "fixture_01", fake)
            self.assertEqual(before, hashlib.sha256(encrypted.read_bytes()).digest())
            self.assertEqual({"protected_source.sqlite", "protected_source.sqlite-wal", "protected_source.sqlite-shm",
                              "vault"}, {p.name for p in self.root.iterdir()})
            self.assertFalse(any(p.suffix in {".plain", ".restore", ".snapshot"} for p in self.root.rglob("*")))
        finally:
            source.close()

    @unittest.skipUnless(os.name == "nt", "DPAPI CurrentUser requires Windows")
    def test_final_path_restore_failure_withdraws_unverified_ciphertext(self):
        path = self.root / "final_verify_source.sqlite"
        source = _create_fixture(path, "WAL")
        directory = self.root / "final_verify_vault"
        directory.mkdir()
        real_restore = backup.restore_encrypted_backup

        def fail_after_publish(target):
            if Path(target).suffix == ".dpapi":
                raise ValueError("synthetic final-path restore failure")
            return real_restore(target)

        try:
            with mock.patch.object(backup, "restore_encrypted_backup", side_effect=fail_after_publish):
                with self.assertRaisesRegex(ValueError, "final-path restore failure"):
                    backup.create_encrypted_backup(source, directory, "final_verify_01")
            self.assertFalse((directory / "final_verify_01.dpapi").exists())
            self.assertFalse((directory / "final_verify_01.dpapi.partial").exists())
        finally:
            source.close()

    @unittest.skipUnless(os.name == "nt", "DPAPI CurrentUser requires Windows")
    def test_interrupted_ciphertext_write_never_creates_final_backup(self):
        directory = self.root / "interrupted_vault"
        directory.mkdir()
        final = backup.unique_backup_path(directory, "interrupted_01")
        cipher = backup.protect_current_user(b"synthetic-only encrypted payload")
        def interrupt():
            raise RuntimeError("synthetic interruption before atomic commit")
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            backup.store_ciphertext_atomically(final, cipher, before_commit=interrupt)
        self.assertFalse(final.exists())
        partial = final.with_name(final.name + ".partial")
        self.assertTrue(partial.exists())
        self.assertEqual(cipher, partial.read_bytes())
        self.assertFalse(list(directory.glob("*.complete")))
        with self.assertRaises(FileExistsError):
            backup.unique_backup_path(directory, "interrupted_01")
