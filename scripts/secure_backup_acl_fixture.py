"""Create and verify disposable WAL/DPAPI inputs for the one-click ACL gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.qa_wal_backup_restore import restore_encrypted_backup

MARKER = ".zisu-synthetic-backup-fixture"
MARKER_VALUE = "ZISU_RELEASE_BACKUP_FIXTURE_ONLY_V1"


def create_fixture(root: Path) -> dict:
    root = root.resolve(strict=True)
    if any(root.iterdir()):
        raise ValueError("fixture root must be newly created and empty")
    (root / MARKER).write_text(MARKER_VALUE + "\n", encoding="ascii")
    live_db = root / "writer_only.sqlite"
    db_path = root / "fixture_db.sqlite"
    connection = sqlite3.connect(live_db)
    try:
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise RuntimeError("synthetic SQLite WAL mode unavailable")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.executescript("""
            CREATE TABLE profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, market TEXT NOT NULL);
            CREATE TABLE manual_research_batches(id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE manual_research_items(profile_id TEXT NOT NULL, manual_batch_id TEXT NOT NULL,
                video_id TEXT NOT NULL, position INTEGER NOT NULL, human_fit TEXT, notes TEXT,
                PRIMARY KEY(profile_id,manual_batch_id,video_id));
            CREATE INDEX idx_manual_fixture_position ON manual_research_items(manual_batch_id,position);
            CREATE TRIGGER manual_fixture_note_guard BEFORE UPDATE OF notes ON manual_research_items
                WHEN NEW.notes IS NULL BEGIN SELECT RAISE(ABORT,'fixture notes cannot be null'); END;
        """)
        connection.executemany("INSERT INTO profiles VALUES(?,?,?)", [
            ("fixture-jp", "Synthetic JP", "JP"), ("fixture-kr", "Synthetic KR", "KR")])
        connection.execute("INSERT INTO manual_research_batches VALUES(?,?,?,?)",
                           ("fixture-batch-jp", "fixture-jp", "IN_PROGRESS", "synthetic-time"))
        connection.executemany(
            "INSERT INTO manual_research_items VALUES(?,?,?,?,?,?)",
            [("fixture-jp", "fixture-batch-jp", f"fake_video_{i:02d}", i,
              "EXCLUDE" if i == 0 else None,
              "synthetic human note" if i == 0 else None) for i in range(50)])
        connection.commit()
        wal_path = Path(str(live_db) + "-wal")
        shm_path = Path(str(live_db) + "-shm")
        if not wal_path.is_file() or wal_path.stat().st_size <= 32:
            raise RuntimeError("synthetic committed WAL frames were not created")
        # Copy a detached synthetic DB/WAL/SHM set while the writer is open.
        shutil.copyfile(live_db, db_path)
        shutil.copyfile(wal_path, Path(str(db_path) + "-wal"))
        if shm_path.exists():
            shutil.copyfile(shm_path, Path(str(db_path) + "-shm"))
    finally:
        connection.close()
    # Only the helper's just-created writer files are removed. The detached
    # fixture paths now form the immutable input set for the release script.
    live_db.unlink(missing_ok=True)
    Path(str(live_db) + "-wal").unlink(missing_ok=True)
    Path(str(live_db) + "-shm").unlink(missing_ok=True)
    return {
        "result": "FIXTURE_READY",
        "fixture_only": True,
        "database": str(db_path),
        "database_header": db_path.read_bytes()[18:20].hex(),
        "wal_bytes": Path(str(db_path) + "-wal").stat().st_size,
        "shm_present": Path(str(db_path) + "-shm").exists(),
        "profiles": 2,
        "manual_items": 50,
        "jp_exclude_note": True,
        "kr_items": 0,
    }


def verify_corrupt_ciphertext(root: Path, backup_path: Path) -> dict:
    root = root.resolve(strict=True)
    backup_path = backup_path.resolve(strict=True)
    vault = (root / "vault").resolve(strict=True)
    if (root / MARKER).read_text(encoding="ascii").strip() != MARKER_VALUE:
        raise ValueError("synthetic marker missing")
    if backup_path.parent != vault or backup_path.suffix != ".dpapi":
        raise ValueError("backup path is outside synthetic vault")
    raw = bytearray(backup_path.read_bytes())
    if len(raw) < 32:
        raise ValueError("synthetic ciphertext is unexpectedly short")
    raw[len(raw) // 2] ^= 0x40
    corrupt_path = vault / "fixture_corrupt_ciphertext.dpapi"
    if corrupt_path.exists():
        raise FileExistsError(corrupt_path.name)
    corrupt_path.write_bytes(raw)
    try:
        try:
            restore_encrypted_backup(corrupt_path)
        except Exception:
            rejected = True
        else:
            rejected = False
        if not rejected:
            raise AssertionError("corrupt synthetic ciphertext was accepted")
    finally:
        corrupt_path.unlink(missing_ok=True)
    info = restore_encrypted_backup(backup_path)
    return {
        "result": "CORRUPT_CIPHERTEXT_REJECTED",
        "backup_reopened": True,
        "integrity": info["database_info"]["integrity"],
        "foreign_keys": info["database_info"]["foreign_keys"],
        "row_count": info["database_info"]["row_count"],
        "cipher_sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        "corrupt_probe_removed": not corrupt_path.exists(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--create", type=Path)
    actions.add_argument("--verify-corrupt", nargs=2, metavar=("FIXTURE_ROOT", "BACKUP"))
    args = parser.parse_args()
    try:
        if args.create:
            result = create_fixture(args.create)
        else:
            result = verify_corrupt_ciphertext(Path(args.verify_corrupt[0]), Path(args.verify_corrupt[1]))
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__}, separators=(",", ":")))
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
