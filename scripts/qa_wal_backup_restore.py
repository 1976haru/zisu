"""Shared WAL-safe SQLite/DPAPI core, validated with synthetic fixtures.

The guarded one-click release backup path calls these functions only after its
process, file-lock, sidecar, destination ACL, and path preflights have passed.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
import uuid
from pathlib import Path
from urllib.parse import quote


SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_MEMORY_BACKUP_SOURCE_BYTES = 64 * 1024 * 1024
MAX_MEMORY_BACKUP_IMAGE_BYTES = 32 * 1024 * 1024
MEMORY_BACKUP_PEAK_FACTOR = 12
MAX_MEMORY_BACKUP_ESTIMATED_PEAK_BYTES = 384 * 1024 * 1024
ROLLBACK_HEADER = b"\x01\x01"
WAL_HEADER = b"\x02\x02"
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_ENCRYPTED = 0x4000
FILE_SUPPORTS_ENCRYPTION = 0x00020000


class BackupBlocked(RuntimeError):
    """A filesystem probe could not prove that a synthetic DB is available."""


def _windows_file_attributes(path: os.PathLike) -> int:
    if os.name != "nt":
        raise BackupBlocked("EFS snapshot protection requires Windows NTFS")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    attributes = get_attributes(str(Path(path)))
    invalid = 0xFFFFFFFF
    if attributes == invalid:
        error = ctypes.get_last_error()
        exc = BackupBlocked("Could not verify encrypted snapshot storage")
        exc.winerror = error
        raise exc
    return int(attributes)


def efs_volume_support(path: os.PathLike) -> dict:
    """Read-only filesystem capability check; does not enable or alter EFS."""
    if os.name != "nt":
        return {"supported": False, "reason": "Windows required"}
    drive, _tail = os.path.splitdrive(str(Path(path).resolve(strict=True)))
    if not drive or not drive.endswith(":"):
        return {"supported": False, "reason": "local drive path required"}
    root = drive + "\\"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    drive_type = int(get_drive_type(root))
    get_volume = kernel.GetVolumeInformationW
    get_volume.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                           ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    get_volume.restype = wintypes.BOOL
    fs_name = ctypes.create_unicode_buffer(64)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not get_volume(root, None, 0, ctypes.byref(serial), ctypes.byref(max_component),
                      ctypes.byref(flags), fs_name, len(fs_name)):
        error = ctypes.get_last_error()
        return {"supported": False, "reason": "GetVolumeInformationW failed", "winerror": error}
    supported = drive_type == 3 and fs_name.value.upper() == "NTFS" and bool(flags.value & FILE_SUPPORTS_ENCRYPTION)
    return {"supported": supported, "filesystem": fs_name.value,
            "drive_type": drive_type, "supports_encryption_flag": bool(flags.value & FILE_SUPPORTS_ENCRYPTION)}


def require_local_ntfs_plain_path(path: os.PathLike) -> Path:
    """Return a stable source path only when every component is local NTFS.

    The memory backup holds a share-deny-write/delete handle as the actual
    stability boundary.  Rejecting reparse components prevents the pathname
    used by SQLite from being redirected around that boundary.
    """
    if os.name != "nt":
        raise BackupBlocked("zero-source-write snapshots require Windows NTFS")
    supplied = Path(path).absolute()
    resolved = supplied.resolve(strict=True)
    if os.path.normcase(str(supplied)) != os.path.normcase(str(resolved)):
        raise BackupBlocked("SQLite source path resolves through a link or reparse point")
    current = resolved
    while True:
        if _windows_file_attributes(current) & FILE_ATTRIBUTE_REPARSE_POINT:
            raise BackupBlocked("SQLite source path contains a reparse point")
        if current.parent == current:
            break
        current = current.parent
    support = efs_volume_support(resolved)
    if support.get("drive_type") != 3 or str(support.get("filesystem", "")).upper() != "NTFS":
        raise BackupBlocked("SQLite source must be on a local NTFS volume")
    return resolved


def protect_snapshot_directory(path: os.PathLike) -> dict:
    """Enable EFS on an empty staging directory before plaintext is written.

    Windows creates new files in an EFS-encrypted directory with EFS enabled.
    This is required for the disk-backed SQLite WAL snapshot; failure blocks
    the release backup before opening the source DB or copying any bytes.
    EFS is at-rest protection under the current user's certificate; it is not
    protection from code already running as that user and is not secure erase.
    """
    directory = Path(path).resolve(strict=True)
    support = efs_volume_support(directory)
    if not support.get("supported"):
        exc = BackupBlocked("EFS_UNSUPPORTED_OR_UNVERIFIED: snapshot volume does not report local NTFS encryption support")
        if "winerror" in support:
            exc.winerror = support["winerror"]
        raise exc
    attributes = _windows_file_attributes(directory)
    if not attributes & FILE_ATTRIBUTE_DIRECTORY or attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise BackupBlocked("Snapshot staging path is not a plain directory")
    if attributes & FILE_ATTRIBUTE_ENCRYPTED:
        return support
    if os.name != "nt":
        raise BackupBlocked("EFS snapshot protection requires Windows NTFS")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    encrypt_file = advapi.EncryptFileW
    encrypt_file.argtypes = [wintypes.LPCWSTR]
    encrypt_file.restype = wintypes.BOOL
    if not encrypt_file(str(directory)):
        error = ctypes.get_last_error()
        exc = BackupBlocked(f"Could not enable EFS for snapshot staging (WinError {error})")
        exc.winerror = error
        raise exc
    if not (_windows_file_attributes(directory) & FILE_ATTRIBUTE_ENCRYPTED):
        raise BackupBlocked("EFS staging directory attribute was not verified")
    return support


def require_efs_encrypted_file(path: os.PathLike) -> None:
    """Fail closed unless a staged snapshot file has EFS encryption enabled."""
    attributes = _windows_file_attributes(path)
    if attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT):
        raise BackupBlocked("Snapshot entry is not a regular file")
    if not attributes & FILE_ATTRIBUTE_ENCRYPTED:
        raise BackupBlocked("Snapshot file did not inherit EFS encryption")


def verify_efs_encrypted_tree(directory: os.PathLike) -> None:
    """Verify every staged file is EFS-encrypted before cleanup/publication."""
    root = Path(directory)
    for path in root.rglob("*"):
        if path.is_file():
            require_efs_encrypted_file(path)
        elif path.is_dir() and path != root:
            attributes = _windows_file_attributes(path)
            if not attributes & FILE_ATTRIBUTE_ENCRYPTED:
                raise BackupBlocked("Snapshot subdirectory did not inherit EFS encryption")


def _json_value(value):
    if isinstance(value, bytes):
        return {"blob": value.hex()}
    return value


def logical_database_info(connection: sqlite3.Connection) -> dict:
    """Return integrity, complete user schema, table counts, and logical digest."""
    checks = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if checks != ["ok"]:
        raise ValueError("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("SQLite foreign_key_check failed")

    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    schema_digest = hashlib.sha256(
        json.dumps(objects, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    counts = {}
    logical = hashlib.sha256()
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in connection.execute("PRAGMA table_info(" + quoted + ")")]
        rows = [
            json.dumps([_json_value(v) for v in row], ensure_ascii=False,
                       separators=(",", ":"), sort_keys=True).encode("utf-8")
            for row in connection.execute("SELECT * FROM " + quoted).fetchall()
        ]
        rows.sort()
        counts[name] = len(rows)
        logical.update(name.encode("utf-8") + b"\0")
        logical.update(json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\0")
        for row in rows:
            logical.update(row + b"\n")
    return {
        "integrity": "ok",
        "foreign_keys": "ok",
        "schema_objects": [list(row) for row in objects],
        "schema_sha256": schema_digest,
        "table_counts": counts,
        "row_count": sum(counts.values()),
        "logical_sha256": logical.hexdigest(),
    }


def make_memory_snapshot(source: sqlite3.Connection) -> tuple[bytes, dict, dict]:
    """Use SQLite Backup API to an independent memory DB, then serialize it."""
    if source.in_transaction:
        raise BackupBlocked("source connection has an active transaction")
    source_info = logical_database_info(source)
    memory = sqlite3.connect(":memory:")
    try:
        source.backup(memory)
        memory_info = logical_database_info(memory)
        if source_info != memory_info:
            raise ValueError("SQLite Backup API snapshot differs from source")
        image = memory.serialize()
        if not isinstance(image, bytes) or not image:
            raise ValueError("SQLite serialize returned an empty image")
        return image, source_info, memory_info
    finally:
        memory.close()


def _open_share_deny_write_read(path: os.PathLike):
    """Hold a readable Windows handle while denying later writes/deletes.

    Unlike the older exclusive-copy helper this permits other read handles,
    which is required for SQLite's mode=ro reader. The handle is acquired for
    the DB first, then its sidecars, so an active SQLite writer (or a writer
    starting later) cannot coexist with the complete protected file set.
    """
    if os.name != "nt":
        raise BackupBlocked("zero-source-write snapshots require Windows file sharing locks")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # FILE_SHARE_READ only: deny write and delete/rename while allowing SQLite
    # to open a read-only connection. OPEN_EXISTING; no file is created here.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if ctypes.cast(handle, ctypes.c_void_p).value == invalid:
        error = ctypes.get_last_error()
        message = "source file set is in use" if error in (32, 33) else "source file set cannot be safely locked"
        exc = BackupBlocked(message)
        exc.winerror = error
        raise exc
    return kernel, handle


def create_locked_memory_snapshot(database_path: os.PathLike,
                                  max_source_bytes: int = MAX_MEMORY_BACKUP_SOURCE_BYTES,
                                  max_image_bytes: int = MAX_MEMORY_BACKUP_IMAGE_BYTES
                                  ) -> tuple[bytes, dict, dict, dict]:
    """Snapshot a SQLite file set to memory without a plaintext disk copy.

    Windows sharing handles are held across the read-only SQLite Backup API
    call. The database handle is acquired first and denies future writes and
    replacement; existing and newly opened WAL/SHM/journal files are similarly
    pinned. A WAL-header database with neither WAL nor SHM uses immutable=1 so
    SQLite cannot create source sidecars. Exactly one sidecar remains ambiguous
    and is refused. Immutable mode proves the bytes that remain in the main DB,
    but cannot recover or prove transactions from a previously lost WAL.
    A bounded size estimate fails closed before Backup API allocation.
    """
    source_path = require_local_ntfs_plain_path(database_path)
    if not source_path.is_file():
        raise BackupBlocked("explicit SQLite source is not a regular file")
    candidates = [Path(str(source_path) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    present_before = {item.name: item.exists() for item in candidates}
    if not present_before[source_path.name]:
        raise BackupBlocked("SQLite source disappeared before lock acquisition")
    held: list[tuple[Path, object, object]] = []
    source = None
    try:
        # DB first: prevents a concurrent SQLite writer from entering while
        # the remaining sidecar handles are acquired.
        for item in candidates:
            if present_before[item.name]:
                kernel, handle = _open_share_deny_write_read(item)
                held.append((item, kernel, handle))
        before = {item.name: _copy_windows_handle(kernel, handle)[:2]
                  for item, kernel, handle in held}
        metadata_before = {item.name: (item.stat().st_size, item.stat().st_mtime_ns)
                           for item, _kernel, _handle in held}
        db_size, _db_hash, header = _copy_windows_handle(
            *next((kernel, handle) for item, kernel, handle in held if item == source_path))
        if db_size < 100 or header[:16] != SQLITE_MAGIC:
            raise BackupBlocked("locked source has an invalid SQLite header")
        pair = header[18:20]
        wal = Path(str(source_path) + "-wal")
        shm = Path(str(source_path) + "-shm")
        journal = Path(str(source_path) + "-journal")
        immutable_without_sidecars = False
        if pair == WAL_HEADER:
            wal_present = wal.name in before
            shm_present = shm.name in before
            if wal_present != shm_present:
                raise BackupBlocked("WAL source has an incomplete WAL/SHM sidecar pair")
            if wal_present:
                if before[wal.name][0] <= 32:
                    raise BackupBlocked("WAL source has no committed WAL frames")
            else:
                if present_before[journal.name]:
                    raise BackupBlocked("sidecar-free WAL source has an unexpected rollback journal")
                immutable_without_sidecars = True
        elif pair == ROLLBACK_HEADER and any(present_before[p.name] for p in (wal, shm, journal)):
            raise BackupBlocked("rollback database has unexpected SQLite sidecars")
        elif pair not in (ROLLBACK_HEADER, WAL_HEADER):
            raise BackupBlocked("locked source has an unsupported SQLite header")
        source_bytes = sum(length for length, _digest in before.values())
        if source_bytes > max_source_bytes:
            raise BackupBlocked("source database and sidecars exceed the memory backup limit")

        uri = "file:" + quote(source_path.as_posix(), safe="/:") + "?mode=ro"
        if immutable_without_sidecars:
            uri += "&immutable=1"
        source = sqlite3.connect(uri, uri=True, timeout=5)
        source.execute("PRAGMA query_only=ON")
        page_count = int(source.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(source.execute("PRAGMA page_size").fetchone()[0])
        predicted_image = page_count * page_size
        if predicted_image > max_image_bytes:
            raise BackupBlocked("SQLite image exceeds the memory backup limit")
        if predicted_image * MEMORY_BACKUP_PEAK_FACTOR > MAX_MEMORY_BACKUP_ESTIMATED_PEAK_BYTES:
            raise BackupBlocked("estimated memory peak exceeds the backup budget")
        image, source_info, snapshot_info = make_memory_snapshot(source)
        if len(image) > max_image_bytes or len(image) * MEMORY_BACKUP_PEAK_FACTOR > MAX_MEMORY_BACKUP_ESTIMATED_PEAK_BYTES:
            raise BackupBlocked("serialized SQLite image exceeds the memory backup budget")
        after = {}
        for item, kernel, handle in held:
            after[item.name] = _copy_windows_handle(kernel, handle)[:2]
        metadata_after = {item.name: (item.stat().st_size, item.stat().st_mtime_ns)
                          for item, _kernel, _handle in held}
        present_after = {item.name: item.exists() for item in candidates}
        if (before != after or metadata_before != metadata_after
                or present_before != present_after
                or source_path != require_local_ntfs_plain_path(database_path)):
            raise BackupBlocked("SQLite source file set changed during memory snapshot")
        return image, source_info, snapshot_info, {
            "source_files": {name: digest for name, (_length, digest) in before.items()},
            "source_file_bytes": source_bytes,
            "serialized_bytes": len(image),
            "estimated_peak_bytes": len(image) * MEMORY_BACKUP_PEAK_FACTOR,
            "header": pair.hex(),
            "snapshot_mode": ("immutable_main_db_without_sidecars"
                              if immutable_without_sidecars else "locked_sqlite_file_set"),
            "history_completeness": ("not_proven_previous_wal_may_have_been_lost"
                                     if immutable_without_sidecars else "present_file_set_verified"),
            "shm_copied": False,
            "source_files_unchanged": True,
        }
    finally:
        if source is not None:
            source.close()
        for _item, kernel, handle in reversed(held):
            kernel.CloseHandle(handle)


def normalize_restore_image(image: bytes) -> bytearray:
    """Copy and validate an image; only the documented WAL header is changed.

    SQLite documents that sqlite3_deserialize() cannot use a database whose
    file-format read/write versions identify WAL (2/2). It recommends setting
    bytes 18 and 19 of the input image to 1 before deserializing. This function
    changes a private copy only; it never receives or edits a source file.
    Rollback-mode 1/1 images pass unchanged. Every other header is rejected.
    """
    if not isinstance(image, (bytes, bytearray, memoryview)):
        raise TypeError("SQLite restore image must be bytes-like")
    if len(image) < 100 or bytes(image[:16]) != SQLITE_MAGIC:
        raise ValueError("Malformed SQLite database header")
    page_size = int.from_bytes(image[16:18], "big")
    if page_size == 1:
        page_size = 65536
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise ValueError("Unexpected SQLite page size")
    if len(image) % page_size:
        raise ValueError("SQLite image length is not page aligned")
    versions = bytes(image[18:20])
    if versions not in (ROLLBACK_HEADER, WAL_HEADER):
        raise ValueError("Unexpected SQLite read/write version pair")
    restored = bytearray(image)
    if versions == WAL_HEADER:
        restored[18:20] = ROLLBACK_HEADER
    return restored


def restore_memory_image(image: bytes) -> tuple[sqlite3.Connection, bytearray, dict]:
    """Deserialize a validated private image and fully verify it in memory."""
    buffer = normalize_restore_image(image)
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(bytes(buffer))
        info = logical_database_info(connection)
    except Exception:
        connection.close()
        raise
    return connection, buffer, info


def sidecar_state(database_path: os.PathLike) -> dict:
    """Inspect filesystem metadata only; never reads SQLite or sidecar bytes."""
    db = Path(database_path)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    journal = Path(str(db) + "-journal")
    return {
        "wal": None if not wal.exists() else wal.stat().st_size,
        "shm": shm.exists(),
        "journal": journal.exists(),
    }


def _open_exclusive(path: os.PathLike):
    """Open without sharing so a conflicting Windows file handle is detected."""
    if os.name != "nt":
        raise NotImplementedError("the exclusive Windows sharing probe is Windows-only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if ctypes.cast(handle, ctypes.c_void_p).value == invalid:
        error = ctypes.get_last_error()
        if error in (32, 33):
            raise BackupBlocked("database is in use")
        if error == 5:
            raise BackupBlocked("database access denied")
        if error == 2:
            raise FileNotFoundError(path)
        raise OSError(error, "exclusive SQLite file probe failed", str(path))
    return kernel, handle


def require_exclusive_access(path: os.PathLike, opener=None) -> None:
    """Fail closed for a held lock or access denial; do not read file bytes."""
    open_handle = opener or _open_exclusive
    try:
        opened = open_handle(path)
    except PermissionError as exc:
        raise BackupBlocked("database access denied") from exc
    except OSError as exc:
        if getattr(exc, "winerror", None) in (32, 33) or exc.errno in (32, 33):
            raise BackupBlocked("database is in use") from exc
        raise
    if isinstance(opened, tuple):
        kernel, handle = opened
        kernel.CloseHandle(handle)
    elif hasattr(opened, "close"):
        opened.close()


def _copy_windows_handle(kernel, handle, destination=None) -> tuple[int, str, bytes]:
    """Stream a held Windows handle, optionally into a staged snapshot file."""
    read_file = kernel.ReadFile
    read_file.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    read_file.restype = wintypes.BOOL
    set_pointer = kernel.SetFilePointerEx
    set_pointer.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                            ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    set_pointer.restype = wintypes.BOOL
    zero = ctypes.c_longlong(0)
    if not set_pointer(handle, 0, ctypes.byref(zero), 0):
        raise OSError(ctypes.get_last_error(), "Could not seek locked source")
    length = 0
    digest = hashlib.sha256()
    prefix = bytearray()
    while True:
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = wintypes.DWORD()
        if not read_file(handle, buffer, len(buffer), ctypes.byref(count), None):
            error = ctypes.get_last_error()
            if error == 38:  # ERROR_HANDLE_EOF
                break
            raise OSError(error, "Could not read locked source")
        if not count.value:
            break
        block = buffer.raw[:count.value]
        length += len(block)
        digest.update(block)
        if len(prefix) < 100:
            prefix.extend(block[:100 - len(prefix)])
        if destination is not None:
            destination.write(block)
    return length, digest.hexdigest(), bytes(prefix)


def _open_locked_read(path: Path):
    if os.name != "nt":
        raise BackupBlocked("consistent source snapshots require Windows share-deny handles")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if ctypes.cast(handle, ctypes.c_void_p).value == invalid:
        error = ctypes.get_last_error()
        if error in (32, 33):
            exc = BackupBlocked("database file set is in use")
            exc.winerror = error
            raise exc
        if error == 5:
            exc = BackupBlocked("database file set access denied")
            exc.winerror = error
            raise exc
        if error == 2:
            exc = BackupBlocked("database file set changed during lock acquisition")
            exc.winerror = error
            raise exc
        raise OSError(error, "Could not lock source file")
    return kernel, handle


def create_isolated_file_snapshot(database_path: os.PathLike,
                                  snapshot_dir: os.PathLike) -> tuple[Path, dict]:
    """Copy a SQLite file set under simultaneous Windows no-sharing handles.

    The source is never opened by SQLite. DB and present sidecars are locked
    together, copied to the caller-provided protected staging directory, then
    re-read from the held handles to detect unexpected mutation. SHM is only
    copied for fingerprint evidence and is deliberately omitted from the
    restore snapshot so SQLite can rebuild its private wal-index there.
    """
    source = Path(database_path).resolve(strict=True)
    target_dir = Path(snapshot_dir).resolve(strict=True)
    if not target_dir.is_dir():
        raise BackupBlocked("snapshot staging path is not a directory")
    candidates = [Path(str(source) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    present_before = {p.name: p.exists() for p in candidates}
    if not present_before[source.name]:
        raise BackupBlocked("database disappeared before snapshot")
    handles = []
    copied = []
    try:
        for path in candidates:
            if present_before[path.name]:
                handles.append((path, *_open_locked_read(path)))
        by_name = {path.name: (kernel, handle) for path, kernel, handle in handles}
        source_facts = {}
        for path, kernel, handle in handles:
            source_facts[path.name] = _copy_windows_handle(kernel, handle)
        db_size, _db_hash, db_header = source_facts[source.name]
        if db_size < 100 or db_header[:16] != SQLITE_MAGIC:
            raise BackupBlocked("locked source has an invalid SQLite header")
        pair = db_header[18:20]
        wal_name = Path(str(source) + "-wal").name
        if pair == WAL_HEADER and (wal_name not in source_facts or not source_facts[wal_name][0]):
            raise BackupBlocked("WAL-mode source is missing a usable WAL file")
        if pair == ROLLBACK_HEADER and any(
                Path(str(source) + suffix).name in source_facts
                for suffix in ("-wal", "-shm", "-journal")):
            # A hot rollback journal can contain committed pages. Do not
            # produce a DB-only snapshot when rollback recovery is pending.
            raise BackupBlocked("rollback-mode source has unexpected SQLite sidecars")
        if pair not in (ROLLBACK_HEADER, WAL_HEADER):
            raise BackupBlocked("locked source has an unsupported SQLite header")

        for suffix in ("", "-wal"):
            original = Path(str(source) + suffix)
            if original.name not in source_facts:
                continue
            destination = target_dir / (source.name + suffix)
            if destination.exists():
                raise BackupBlocked("snapshot destination already exists")
            with destination.open("xb") as stream:
                kernel, handle = by_name[original.name]
                copied_facts = _copy_windows_handle(kernel, handle, stream)
                stream.flush()
                os.fsync(stream.fileno())
            copied.append(destination)
            if copied_facts[:2] != source_facts[original.name][:2]:
                raise BackupBlocked("isolated snapshot copy hash mismatch")

        # Re-read through the still-held handles and verify that the complete
        # locked file set and its bytes remained stable until copy completion.
        for path, kernel, handle in handles:
            current = _copy_windows_handle(kernel, handle)
            if current[:2] != source_facts[path.name][:2]:
                raise BackupBlocked("source file changed during locked snapshot")
        present_after = {p.name: p.exists() for p in candidates}
        if present_after != present_before:
            raise BackupBlocked("SQLite sidecar set changed during locked snapshot")
        return target_dir / source.name, {
            "header": pair,
            "source_files": {name: facts[1] for name, facts in source_facts.items()},
            "snapshot_files": [p.name for p in copied],
            "shm_copied": False,
        }
    except Exception:
        for path in copied:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        for _path, kernel, handle in reversed(handles):
            kernel.CloseHandle(handle)


def validate_wal_snapshot(database_path: os.PathLike) -> None:
    """Validate WAL framing, salts and rolling checksums in an isolated copy."""
    database = Path(database_path)
    wal_path = Path(str(database) + "-wal")
    with database.open("rb") as db_stream:
        db_header = db_stream.read(100)
    with wal_path.open("rb") as wal:
        header = wal.read(32)
        if len(header) != 32 or header[:4] not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"):
            raise BackupBlocked("isolated WAL header is malformed")
        if int.from_bytes(header[4:8], "big") != 3007000:
            raise BackupBlocked("isolated WAL version is unsupported")
        page_size = int.from_bytes(header[8:12], "big")
        db_page_size = int.from_bytes(db_header[16:18], "big")
        if db_page_size == 1:
            db_page_size = 65536
        if page_size != db_page_size or page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
            raise BackupBlocked("isolated WAL page size does not match database")
        frame_bytes = 24 + page_size
        if (wal_path.stat().st_size - 32) % frame_bytes:
            raise BackupBlocked("isolated WAL has a truncated frame")
        byte_order = "little" if header[:4] == b"\x37\x7f\x06\x82" else "big"
        s0 = s1 = 0

        def checksum(block: bytes, current0: int, current1: int) -> tuple[int, int]:
            if len(block) % 8:
                raise BackupBlocked("isolated WAL checksum block is misaligned")
            for offset in range(0, len(block), 8):
                x0 = int.from_bytes(block[offset:offset + 4], byte_order)
                x1 = int.from_bytes(block[offset + 4:offset + 8], byte_order)
                current0 = (current0 + x0 + current1) & 0xffffffff
                current1 = (current1 + x1 + current0) & 0xffffffff
            return current0, current1

        s0, s1 = checksum(header[:24], s0, s1)
        if (s0, s1) != (int.from_bytes(header[24:28], "big"), int.from_bytes(header[28:32], "big")):
            raise BackupBlocked("isolated WAL header checksum failed")
        salts = header[16:24]
        while True:
            frame_header = wal.read(24)
            if not frame_header:
                break
            if len(frame_header) != 24 or frame_header[8:16] != salts:
                raise BackupBlocked("isolated WAL frame header or salt is invalid")
            s0, s1 = checksum(frame_header[:8], s0, s1)
            remaining = page_size
            while remaining:
                block = wal.read(min(1024 * 1024, remaining))
                if not block:
                    raise BackupBlocked("isolated WAL frame is truncated")
                s0, s1 = checksum(block, s0, s1)
                remaining -= len(block)
            if (s0, s1) != (int.from_bytes(frame_header[16:20], "big"),
                            int.from_bytes(frame_header[20:24], "big")):
                raise BackupBlocked("isolated WAL frame checksum failed")


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI CurrentUser is available only on Windows")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    source_buffer = ctypes.create_string_buffer(data)
    source = DATA_BLOB(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    output = DATA_BLOB()
    if protect:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
                             ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID,
                             wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        function.restype = wintypes.BOOL
        ok = function(ctypes.byref(source), "ZISU synthetic WAL backup", None, None,
                      None, 1, ctypes.byref(output))
    else:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                             ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID,
                             wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
        function.restype = wintypes.BOOL
        ok = function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))


def protect_current_user(data: bytes) -> bytes:
    return _dpapi(data, True)


def unprotect_current_user(data: bytes) -> bytes:
    return _dpapi(data, False)


def unique_backup_path(directory: os.PathLike, backup_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", backup_id):
        raise ValueError("Backup ID must be a simple unique identifier")
    path = Path(directory) / (backup_id + ".dpapi")
    if path.exists() or path.with_name(path.name + ".partial").exists():
        raise FileExistsError(path)
    return path


def write_ciphertext_partial(final_path: os.PathLike, ciphertext: bytes, before_commit=None) -> Path:
    """Stage ciphertext under a partial name; callers must verify before commit."""
    final = Path(final_path)
    partial = final.with_name(final.name + ".partial")
    if final.exists() or partial.exists():
        raise FileExistsError(final)
    with open(partial, "xb") as stream:
        stream.write(ciphertext)
        stream.flush()
        os.fsync(stream.fileno())
    if before_commit is not None:
        before_commit()
    return partial


def commit_ciphertext(final_path: os.PathLike, partial_path: os.PathLike) -> None:
    """Atomically publish a previously verified ciphertext without replacement."""
    final = Path(final_path)
    partial = Path(partial_path)
    if final.exists():
        raise FileExistsError(final)
    # Hard-link creation is atomic and fails instead of replacing an existing ID.
    os.link(partial, final)
    try:
        partial.unlink()
    except Exception:
        # If staging cleanup fails, withdraw the final hard link created above.
        # Keep it only if it no longer identifies the staged encrypted bytes.
        try:
            if os.path.samefile(final, partial):
                final.unlink()
        except Exception:
            pass
        raise


def store_ciphertext_atomically(final_path: os.PathLike, ciphertext: bytes, before_commit=None) -> None:
    """Store only ciphertext; no final name exists until the complete write."""
    partial = write_ciphertext_partial(final_path, ciphertext, before_commit)
    commit_ciphertext(final_path, partial)


def create_encrypted_image_backup(image: bytes, source_info: dict, directory: os.PathLike,
                                  backup_id: str, payload_files: dict[str, bytes] | None = None,
                                  before_commit=None) -> dict:
    """Encrypt an already-created SQLite image and publish only after restore checks."""
    phase = "restore_image_check"
    try:
        final = unique_backup_path(directory, backup_id)
        repaired = normalize_restore_image(image)
        restored, held_buffer, restored_info = restore_memory_image(bytes(repaired))
        if source_info != restored_info:
            restored.close()
            raise ValueError("SQLite logical/schema comparison failed")
        restored.close()

        files = payload_files or {}
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in files):
            raise ValueError("Backup entry path is unsafe")
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
        manifest = {
            "format": "ZISU-DPAPI-WAL-v1",
            "backup_id": backup_id,
            "original_header": bytes(image[18:20]).hex(),
            "restore_header": bytes(repaired[18:20]).hex(),
            "database_sha256": hashlib.sha256(repaired).hexdigest(),
            "database_info": source_info,
            "files": hashes,
        }
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("database.sqlite", bytes(repaired))
            for name, value in files.items():
                zf.writestr("payload/" + name, value)
            zf.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
        phase = "dpapi_encrypt"
        ciphertext = protect_current_user(archive.getvalue())
        phase = "write_ciphertext_partial"
        partial = write_ciphertext_partial(final, ciphertext, before_commit=before_commit)
        phase = "verify_ciphertext_before_publish"
        result = restore_encrypted_backup(partial)
        if result["manifest"] != manifest or result["database_info"] != source_info:
            raise ValueError("Encrypted restore did not match the source snapshot")
        phase = "atomic_publish"
        commit_ciphertext(final, partial)
        phase = "verify_published_ciphertext"
        try:
            result = restore_encrypted_backup(final)
            if result["manifest"] != manifest or result["database_info"] != source_info:
                raise ValueError("Committed encrypted restore did not match the source snapshot")
        except Exception:
            # The final pathname was created by this call. Remove it only while
            # it still contains the exact ciphertext that passed staged checks.
            if final.is_file() and hashlib.sha256(final.read_bytes()).digest() == hashlib.sha256(ciphertext).digest():
                final.unlink()
            raise
        return {
            "path": final,
            "ciphertext_bytes": final.stat().st_size,
            "manifest": manifest,
            "database_info": source_info,
        }
    except Exception as exc:
        try:
            setattr(exc, "backup_phase", phase)
        except Exception:
            pass
        raise


def create_encrypted_backup(source: sqlite3.Connection, directory: os.PathLike,
                            backup_id: str, synthetic_files: dict[str, bytes] | None = None,
                            before_commit=None) -> dict:
    """SQLite Backup API + DPAPI roundtrip; retained for the synthetic QA suite."""
    image, source_info, snapshot_info = make_memory_snapshot(source)
    if source_info != snapshot_info:
        raise ValueError("SQLite Backup API snapshot differs from source")
    return create_encrypted_image_backup(image, source_info, directory, backup_id,
                                         synthetic_files, before_commit)


def create_locked_memory_encrypted_backup(database_path: os.PathLike, directory: os.PathLike,
                                          backup_id: str,
                                          max_source_bytes: int = MAX_MEMORY_BACKUP_SOURCE_BYTES,
                                          max_image_bytes: int = MAX_MEMORY_BACKUP_IMAGE_BYTES,
                                          before_commit=None) -> dict:
    """Fixture-validated candidate: locked source -> memory -> DPAPI ciphertext.

    No SQLite plaintext snapshot is written to disk. On a handled failure,
    remove only this call's own partial ciphertext; an abrupt process death may
    leave that encrypted partial behind, but cannot publish it as a final
    backup. The caller must still provide a verified user-only vault ACL.
    """
    image, source_info, snapshot_info, source_facts = create_locked_memory_snapshot(
        database_path, max_source_bytes=max_source_bytes, max_image_bytes=max_image_bytes)
    if source_info != snapshot_info:
        raise BackupBlocked("memory snapshot differs from the locked source")
    partial = Path(directory) / (backup_id + ".dpapi.partial")
    try:
        result = create_encrypted_image_backup(image, source_info, directory, backup_id,
                                               before_commit=before_commit)
    except Exception:
        # The naming/validation path is constrained to a single leaf under the
        # caller's vault. Never remove an existing final file.
        if partial.parent.resolve() == Path(directory).resolve() and partial.is_file():
            partial.unlink()
        raise
    result["source_snapshot"] = source_facts
    result["plaintext_snapshot_files"] = 0
    result["dpapi_scope"] = "CurrentUser"
    return result


def restore_encrypted_backup(path: os.PathLike) -> dict:
    ciphertext = Path(path).read_bytes()
    plain = unprotect_current_user(ciphertext)
    with zipfile.ZipFile(io.BytesIO(plain), "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        image = zf.read("database.sqlite")
        if hashlib.sha256(image).hexdigest() != manifest["database_sha256"]:
            raise ValueError("Encrypted database image hash mismatch")
        restored, held_buffer, info = restore_memory_image(image)
        try:
            if info != manifest["database_info"]:
                raise ValueError("Restored database schema/data mismatch")
            restored_files = {}
            for name, expected in manifest["files"].items():
                value = zf.read("payload/" + name)
                if hashlib.sha256(value).hexdigest() != expected:
                    raise ValueError("Encrypted payload hash mismatch")
                restored_files[name] = value
            return {"manifest": manifest, "database_info": info, "files": restored_files}
        finally:
            restored.close()
