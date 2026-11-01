"""Guarded release backup adapter around the already-tested WAL/DPAPI core.

This CLI has no database or vault defaults. The release PowerShell script must
pass explicit paths after its process and ACL gates. Fixture mode additionally
requires a marker file and confines both paths to that synthetic root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.qa_wal_backup_restore import (
    BackupBlocked,
    ROLLBACK_HEADER,
    SQLITE_MAGIC,
    WAL_HEADER,
    create_encrypted_image_backup,
    create_isolated_file_snapshot,
    protect_snapshot_directory,
    verify_efs_encrypted_tree,
    validate_wal_snapshot,
    make_memory_snapshot,
    create_locked_memory_encrypted_backup,
    require_exclusive_access,
    sidecar_state,
)


FIXTURE_MARKER = ".zisu-synthetic-backup-fixture"
FIXTURE_MARKER_VALUE = "ZISU_RELEASE_BACKUP_FIXTURE_ONLY_V1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_fingerprint(database: Path) -> dict:
    paths = [database, Path(str(database) + "-wal"), Path(str(database) + "-shm"),
             Path(str(database) + "-journal")]
    result = {}
    for path in paths:
        if path.exists():
            stat = path.stat()
            result[path.name] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": _sha256_file(path),
            }
        else:
            result[path.name] = None
    return result


def _header_and_sidecar_preflight(database: Path, fixture_only: bool = False) -> tuple[bytes, dict]:
    with database.open("rb") as stream:
        header = stream.read(100)
    if len(header) < 100 or header[:16] != SQLITE_MAGIC:
        raise BackupBlocked("invalid SQLite header")
    pair = header[18:20]
    state = sidecar_state(database)
    if pair == WAL_HEADER:
        if state["wal"] is None or state["wal"] <= 0:
            raise BackupBlocked("WAL-mode snapshot is missing a nonempty WAL sidecar")
    elif pair == ROLLBACK_HEADER and not fixture_only:
        if state["wal"] is not None or state["shm"] or state["journal"]:
            raise BackupBlocked("rollback database has unexpected SQLite sidecars")
    elif pair not in (WAL_HEADER, ROLLBACK_HEADER):
        raise BackupBlocked("unexpected SQLite read/write header version")
    return pair, state


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_fixture_scope(database: Path, vault: Path, fixture_root: Path) -> None:
    root = fixture_root.resolve(strict=True)
    marker = root / FIXTURE_MARKER
    if marker.read_text(encoding="ascii").strip() != FIXTURE_MARKER_VALUE:
        raise BackupBlocked("synthetic fixture marker is invalid")
    if not _within(database.resolve(strict=True), root):
        raise BackupBlocked("fixture database is outside its marked root")
    if not _within(vault.resolve(strict=True), root):
        raise BackupBlocked("fixture vault is outside its marked root")
    if database.resolve() == vault.resolve() or _within(database.resolve(), vault.resolve()):
        raise BackupBlocked("fixture database and vault paths overlap")


def run_backup(database: Path, vault: Path, backup_id: str,
               fixture_root: Path | None = None,
               require_storage_encryption: bool | None = None,
               fixture_snapshot_ready_hook=None) -> dict:
    phase = "path_preflight"
    try:
        database = database.resolve(strict=True)
        vault = vault.resolve(strict=True)
        if not database.is_file() or not vault.is_dir():
            raise BackupBlocked("explicit database or vault path is invalid")
        if fixture_root is not None:
            _check_fixture_scope(database, vault, fixture_root)
        elif fixture_snapshot_ready_hook is not None:
            raise BackupBlocked("snapshot crash hook is available only to a marked synthetic fixture")
        if require_storage_encryption is None:
            require_storage_encryption = fixture_root is None
        if fixture_root is None and not require_storage_encryption:
            raise BackupBlocked("production backup cannot disable snapshot storage encryption")

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", backup_id):
            raise BackupBlocked("backup ID contains unsupported characters")
        snapshot_dir = vault / (".snapshot_" + backup_id + "_" + os.urandom(6).hex())
        if snapshot_dir.exists():
            raise BackupBlocked("unique snapshot staging path already exists")
        phase = "protected_snapshot_directory_create"
        snapshot_dir.mkdir()
        efs_support = None
        def remove_plaintext_snapshot():
            if snapshot_dir.exists():
                if require_storage_encryption:
                    verify_efs_encrypted_tree(snapshot_dir)
                shutil.rmtree(snapshot_dir, ignore_errors=False)
        try:
            if require_storage_encryption:
                phase = "protect_snapshot_storage"
                # Production backups fail closed before any source handle is
                # opened unless EFS protects the new, empty staging directory.
                efs_support = protect_snapshot_directory(snapshot_dir)
            phase = "locked_source_snapshot"
            snapshot_db, snapshot_facts = create_isolated_file_snapshot(database, snapshot_dir)
            if require_storage_encryption:
                phase = "verify_snapshot_storage_encryption"
                verify_efs_encrypted_tree(snapshot_dir)
            if fixture_snapshot_ready_hook is not None:
                if fixture_root is None:
                    raise BackupBlocked("snapshot crash hook requires FixtureOnly scope")
                fixture_snapshot_ready_hook(snapshot_dir, snapshot_facts)
            phase = "snapshot_header_sidecar_preflight"
            header_pair, before_sidecars = _header_and_sidecar_preflight(snapshot_db, fixture_only=True)
            if header_pair == WAL_HEADER:
                validate_wal_snapshot(snapshot_db)
            phase = "isolated_snapshot_backup_api"
            uri = "file:" + quote(snapshot_db.as_posix(), safe="/:") + "?mode=ro"
            source = sqlite3.connect(uri, uri=True, timeout=10)
            try:
                source.execute("PRAGMA query_only=ON")
                image, source_info, snapshot_info = make_memory_snapshot(source)
                if source_info != snapshot_info:
                    raise BackupBlocked("SQLite memory snapshot did not match isolated file snapshot")
            finally:
                source.close()

            phase = "dpapi_atomic_restore_verification"
            try:
                verified = create_encrypted_image_backup(
                    image, source_info, vault, backup_id,
                    before_commit=remove_plaintext_snapshot)
            except Exception:
                partial = vault / (backup_id + ".dpapi.partial")
                if partial.parent == vault and partial.is_file():
                    partial.unlink()
                raise
        finally:
            remove_plaintext_snapshot()
        after_sidecars = sidecar_state(database)
    except Exception as exc:
        try:
            if not hasattr(exc, "backup_phase"):
                setattr(exc, "backup_phase", phase)
        except Exception:
            pass
        raise
    final = Path(verified["path"]).resolve(strict=True)
    if final.parent != vault or final.suffix != ".dpapi":
        raise BackupBlocked("encrypted backup escaped the protected vault")
    cipher_hash = _sha256_file(final)
    return {
        "result": "PASS",
        "backup_id": backup_id,
        "backup_path": str(final),
        "cipher_sha256": cipher_hash,
        "cipher_bytes": final.stat().st_size,
        "database_header_before_restore": header_pair.hex(),
        "database_sha256": verified["manifest"]["database_sha256"],
        "schema_sha256": source_info["schema_sha256"],
        "logical_sha256": source_info["logical_sha256"],
        "table_count": len(source_info["table_counts"]),
        "row_count": source_info["row_count"],
        "table_counts": source_info["table_counts"],
        "sqlite_integrity": "ok",
        "foreign_key_integrity": "ok",
        "dpapi_scope": "CurrentUser",
        "restore_verified": True,
        "plaintext_backup_files": 0,
        "sidecars_before": before_sidecars,
        "sidecars_after": after_sidecars,
        "snapshot_files": snapshot_facts["snapshot_files"],
        "source_snapshot_sha256": snapshot_facts["source_files"],
        "source_shm_copied": snapshot_facts["shm_copied"],
        "snapshot_storage_encryption": "EFS" if require_storage_encryption else "synthetic_fixture_only",
        "snapshot_encryption_verified": bool(require_storage_encryption),
        "efs_volume_support": efs_support,
    }


def run_memory_dpapi_backup(database: Path, vault: Path, backup_id: str,
                            fixture_root: Path | None = None,
                            fixture_before_commit_hook=None) -> dict:
    """Release-managed source-lock -> in-memory SQLite -> DPAPI backup route.

    No plaintext DB/WAL snapshot is staged on disk. Production callers must be
    the PowerShell release manager, which verifies a current-user-only vault
    ACL before invoking this CLI and checks the final ciphertext ACL afterward.
    FixtureOnly callers are confined by the synthetic marker and path checks.
    There is intentionally no EFS failure fallback in this route.
    """
    phase = "memory_path_preflight"
    try:
        database = database.resolve(strict=True)
        vault = vault.resolve(strict=True)
        if not database.is_file() or not vault.is_dir():
            raise BackupBlocked("explicit database or vault path is invalid")
        if fixture_root is not None:
            _check_fixture_scope(database, vault, fixture_root)
        elif fixture_before_commit_hook is not None:
            raise BackupBlocked("memory backup failure hooks are FixtureOnly")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", backup_id):
            raise BackupBlocked("backup ID contains unsupported characters")
        phase = "locked_memory_snapshot_dpapi_restore_publish"
        verified = create_locked_memory_encrypted_backup(
            database, vault, backup_id, before_commit=fixture_before_commit_hook)
        final = Path(verified["path"]).resolve(strict=True)
        if final.parent != vault or final.suffix != ".dpapi":
            raise BackupBlocked("encrypted backup escaped the verified vault")
        info = verified["database_info"]
        snapshot = verified["source_snapshot"]
        return {
            "result": "PASS",
            "backup_mode": "MEMORY_DPAPI",
            "backup_id": backup_id,
            "backup_path": str(final),
            "cipher_sha256": _sha256_file(final),
            "cipher_bytes": final.stat().st_size,
            "database_header_before_restore": snapshot["header"],
            "database_sha256": verified["manifest"]["database_sha256"],
            "schema_sha256": info["schema_sha256"],
            "logical_sha256": info["logical_sha256"],
            "table_count": len(info["table_counts"]),
            "row_count": info["row_count"],
            "table_counts": info["table_counts"],
            "sqlite_integrity": info["integrity"],
            "foreign_key_integrity": info["foreign_keys"],
            "dpapi_scope": "CurrentUser",
            "restore_verified": True,
            "plaintext_backup_files": 0,
            "plaintext_snapshot_staging": "none_memory_only",
            "source_file_set_unchanged": snapshot["source_files_unchanged"],
            "source_file_sha256": snapshot["source_files"],
            "source_file_bytes": snapshot["source_file_bytes"],
            "serialized_bytes": snapshot["serialized_bytes"],
            "estimated_peak_bytes": snapshot["estimated_peak_bytes"],
            "source_shm_copied": False,
            "storage_encryption": "DPAPI_CurrentUser_ciphertext",
            "vault_acl_requirement": "verified_by_release_powershell_caller",
        }
    except Exception as exc:
        if not hasattr(exc, "backup_phase"):
            try:
                setattr(exc, "backup_phase", phase)
            except Exception:
                pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--backup-id", required=True)
    parser.add_argument("--fixture-root")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--memory-dpapi", action="store_true",
                      help="Use the zero-plaintext-stage in-memory DPAPI path; never falls back to EFS staging.")
    mode.add_argument("--legacy-efs", action="store_true",
                      help="Use the separate legacy isolated-disk-snapshot path, gated by EFS for production.")
    parser.add_argument("--fixture-crash-after-partial", action="store_true",
                        help="FixtureOnly: terminate this CLI after writing ciphertext partial and before publication.")
    parser.add_argument("--require-efs-fixture", action="store_true",
                        help="Require EFS in the marked synthetic fixture staging path.")
    args = parser.parse_args(argv)
    if args.require_efs_fixture and not args.fixture_root:
        parser.error("--require-efs-fixture requires --fixture-root and a marked synthetic database")
    if args.require_efs_fixture and not args.legacy_efs:
        parser.error("--require-efs-fixture requires the explicit --legacy-efs mode")
    if args.fixture_crash_after_partial and (not args.fixture_root or not args.memory_dpapi):
        parser.error("--fixture-crash-after-partial requires --fixture-root and --memory-dpapi")
    try:
        if args.memory_dpapi:
            crash_hook = (lambda: os._exit(23)) if args.fixture_crash_after_partial else None
            result = run_memory_dpapi_backup(Path(args.database), Path(args.vault), args.backup_id,
                                             Path(args.fixture_root) if args.fixture_root else None,
                                             fixture_before_commit_hook=crash_hook)
        else:
            result = run_backup(Path(args.database), Path(args.vault), args.backup_id,
                                Path(args.fixture_root) if args.fixture_root else None,
                                require_storage_encryption=True if args.require_efs_fixture else None)
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "error_type": type(exc).__name__,
                          "phase": getattr(exc, "backup_phase", "startup"),
                          "winerror": getattr(exc, "winerror", None),
                          "errno": getattr(exc, "errno", None)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
