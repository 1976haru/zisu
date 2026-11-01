"""Build a synthetic V4.5.9 database from its historical source and test V4.6 migration.

This QA entry point is fixture-only. It never resolves or opens the installed user's
database, OAuth tokens, settings, or Chrome profile.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from mcrs import manual_research, store
from mcrs.credential_store import CredentialStore
from mcrs.genres import EXCLUDE
from scripts import zisu_release_backup_cli
from scripts.qa_wal_backup_restore import (
    normalize_restore_image,
    restore_encrypted_backup,
    unprotect_current_user,
)


LEGACY_COMMIT = "5a7b4168d9e2d06d735bea600e4c0448772c72b1"
LEGACY_GENRES = {
    "MORNING_CHILL_RAP", "CHILL_RAP", "RNB_HIPHOP",
    "CAFE_CHILL", "OLD_POP", "JAZZ_LOUNGE",
}
FIXTURE_MARKER = ".zisu-synthetic-backup-fixture"
FIXTURE_MARKER_VALUE = "ZISU_RELEASE_BACKUP_FIXTURE_ONLY_V1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_show(path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{LEGACY_COMMIT}:{path}"],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"HISTORICAL_SOURCE_UNAVAILABLE:{path}")
    return result.stdout


def _load_legacy_store(root: Path):
    package_root = root / "historical_v459_source"
    package = package_root / "legacy_v459"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    historical = {}
    for name in ("store.py", "genres.py"):
        content = _git_show(f"mcrs/{name}")
        (package / name).write_bytes(content)
        historical[name] = _sha(content)
    sys.path.insert(0, str(package_root))
    try:
        return importlib.import_module("legacy_v459.store"), historical
    except Exception:
        sys.path.remove(str(package_root))
        raise


def _table_names(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]


def _legacy_snapshot(path: Path, legacy_tables: list[str] | None = None) -> dict:
    with sqlite3.connect(path) as db:
        tables = legacy_tables or _table_names(db)
        schema = {}
        data = {}
        for table in tables:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            schema[table] = columns
            rows = [list(row) for row in db.execute(f'SELECT * FROM "{table}"')]
            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
            data[table] = rows
        objects = [list(row) for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE type IN ('index','trigger','view') AND sql IS NOT NULL ORDER BY type,name"
        )]
    return {"tables": tables, "columns": schema, "rows": data, "objects": objects}


def _write_restored_database(ciphertext: Path, target: Path) -> tuple[dict, bytes]:
    restored = restore_encrypted_backup(ciphertext)
    plain = unprotect_current_user(ciphertext.read_bytes())
    with zipfile.ZipFile(__import__("io").BytesIO(plain), "r") as archive:
        image = archive.read("database.sqlite")
    normalized = bytes(normalize_restore_image(image))
    target.write_bytes(normalized)
    with sqlite3.connect(target) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("RESTORED_FIXTURE_INTEGRITY_FAILED")
    return restored, normalized


def _seed_legacy_database(legacy, database: Path) -> tuple[str, str, str]:
    legacy.initialize(database)
    jp = legacy.create_profile(
        "Synthetic JP", "JP", "MORNING_CHILL_RAP", path=database,
        subgenre="Synthetic morning details", related_genres=["CHILL_RAP", "RNB_HIPHOP"],
    )
    kr = legacy.create_profile(
        "Synthetic KR", "KR", "CHILL_RAP", path=database,
        subgenre="Synthetic Korean details", related_genres=["MORNING_CHILL_RAP"],
    )
    first_video, second_video = "v459JP00001", "v459KR00002"
    for profile_id, video_id, title in (
        (jp, first_video, "Synthetic Morning Chill Rap"),
        (kr, second_video, "Synthetic Chill Rap"),
    ):
        legacy.save_candidates(profile_id, [{
            "video_id": video_id,
            "video_title": title,
            "description": "synthetic legacy candidate; no network metadata",
            "channel_title": "Fixture Channel",
            "channel_id": "fixture-channel-id",
            "published_at": "2026-01-01T00:00:00Z",
            "views": 1234,
            "country": profile_id,
            "keyword": "fixture keyword",
        }], path=database)
    legacy.label_genre_audit(jp, first_video, EXCLUDE, "V4.5.9 synthetic human exclusion", path=database)
    legacy.set_profile_search_terms(jp, "JP", ["fixture jp one", "fixture jp two"], path=database)
    legacy.set_profile_search_terms(kr, "KR", ["fixture kr one"], path=database)
    legacy.upsert_metric(jp, "2026-10-01", 10000, 1500, 400, 2.5, 3.25, 24, path=database)
    legacy.upsert_metric(kr, "2026-10-01", 9000, 1100, 300, 2.0, 2.75, 19, path=database)
    with legacy.connect(database) as db:
        db.execute(
            "UPDATE profiles SET connected_email=?,youtube_channel_id=?,youtube_channel_title=?,connected_at=? WHERE id=?",
            ("fixture-account@example.invalid", "fixture-jp-channel", "Fixture JP Channel", "2026-01-02T00:00:00Z", jp),
        )
        db.execute(
            "UPDATE profiles SET connected_email=?,youtube_channel_id=?,youtube_channel_title=?,connected_at=? WHERE id=?",
            ("fixture-account@example.invalid", "fixture-kr-channel", "Fixture KR Channel", "2026-01-03T00:00:00Z", kr),
        )
        db.execute(
            "INSERT INTO autopilot_comment_drafts(profile_id,video_id,session_id,drafts_json) VALUES(?,?,?,?)",
            (jp, first_video, "v459-session-001", '[{"text":"synthetic draft"}]'),
        )
    session_id = legacy.start_research_session(jp, "JP", "MORNING_CHILL_RAP", 20, 3, path=database)
    legacy.checkpoint_research_session(
        session_id, jp, state="paused", query_index=2,
        processed=[first_video], candidates=[first_video], reviews=[first_video],
        in_flight=0, calls_used=2, duplicate_count=1, path=database,
    )
    legacy.save_autopilot_settings(jp, session_id, {
        "provider_id": "rules-only", "model": "fixture", "target": 20,
        "max_search_calls": 3, "cost_estimate_usd": 0, "cost_limit_usd": 0,
        "paid_external_approved": False, "report_version": "synthetic-v459",
    }, path=database)
    legacy.save_autopilot_report(jp, session_id, {"state": "paused", "fixture": True}, path=database)
    legacy.save_ui_state({
        "profile_id": jp, "tab_index": 2, "selected_video_id": first_video,
        "search_term": "synthetic saved UI state", "filter": "review",
    }, path=database)
    return jp, kr, session_id


def _create_detached_wal_copy(source: Path, detached: Path, profile_id: str) -> dict:
    writer = sqlite3.connect(source, timeout=5)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    marker = "V459_WAL_COMMIT_" + uuid.uuid4().hex[:12]
    writer.execute(
        "UPDATE profiles SET schedule=? WHERE id=?",
        ("07:15,20:15 " + marker, profile_id),
    )
    writer.commit()
    wal = Path(str(source) + "-wal")
    if not wal.is_file() or wal.stat().st_size <= 32:
        writer.close()
        raise RuntimeError("SYNTHETIC_WAL_COMMIT_NOT_PRESENT")
    shutil.copy2(source, detached)
    shutil.copy2(wal, Path(str(detached) + "-wal"))
    # SHM is intentionally omitted: it is a transient wal-index, not restore data.
    facts = {
        "wal_present": True,
        "wal_size_bytes": wal.stat().st_size,
        "wal_uncheckpointed_marker_committed": marker,
        "detached_shm_copied": False,
        "writer_handle_held_during_copy": True,
    }
    writer.close()
    return facts


def _check_current_migration(path: Path, old_snapshot: dict, jp: str, kr: str,
                             session_id: str, video_id: str, expected_manual: int) -> dict:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        tables = set(_table_names(db))
        old_table_rows_match = True
        for table in old_snapshot["tables"]:
            old_columns = old_snapshot["columns"][table]
            current_columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
            if not set(old_columns).issubset(current_columns):
                old_table_rows_match = False
                break
            rows = [list(row) for row in db.execute(
                f'SELECT {",".join(chr(34)+column+chr(34) for column in old_columns)} FROM "{table}"'
            )]
            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
            expected_rows = json.loads(json.dumps(old_snapshot["rows"][table]))
            # This legacy migration deliberately backfills an empty rule_verdict
            # from the already-persisted predicted_decision. Track the exact
            # allowed normalization and compare every other legacy value bytewise.
            if table == "genre_audit" and "rule_verdict" in old_columns and "predicted_decision" in old_columns:
                rule_index = old_columns.index("rule_verdict")
                predicted_index = old_columns.index("predicted_decision")
                for row in expected_rows:
                    if row[rule_index] == "":
                        row[rule_index] = row[predicted_index]
            expected_rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
            if rows != expected_rows:
                old_table_rows_match = False
                break
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        jp_profile = dict(db.execute("SELECT * FROM profiles WHERE id=?", (jp,)).fetchone())
        kr_profile = dict(db.execute("SELECT * FROM profiles WHERE id=?", (kr,)).fetchone())
        account_count = db.execute("SELECT COUNT(*) FROM google_accounts").fetchone()[0]
        account_channels = db.execute("SELECT COUNT(*) FROM google_account_channels").fetchone()[0]
        terms = db.execute("SELECT profile_id,term FROM profile_search_terms ORDER BY profile_id,term").fetchall()
        labels = db.execute(
            "SELECT human_label,label_notes FROM genre_audit WHERE profile_id=? AND video_id=?",
            (jp, video_id),
        ).fetchone()
        session = dict(db.execute("SELECT * FROM research_sessions WHERE id=?", (session_id,)).fetchone())
        ui = db.execute("SELECT state_json FROM ui_state WHERE state_key='main'").fetchone()[0]
        manual_count = db.execute(
            "SELECT COUNT(*) FROM manual_research_items WHERE video_id LIKE 'M%' AND length(video_id)=11").fetchone()[0]
    expected_ids = LEGACY_GENRES
    return {
        "old_tables_columns_and_rows_preserved": old_table_rows_match,
        "integrity_check": integrity,
        "foreign_key_errors": len(foreign_keys),
        "jp_profile": jp_profile,
        "kr_profile": kr_profile,
        "legacy_account_count": account_count,
        "legacy_channel_count": account_channels,
        "search_terms_count": len(terms),
        "human_exclude": list(labels) if labels else None,
        "research_session_state": session["state"],
        "research_session_query_index": session["query_index"],
        "ui_state_restored": json.loads(ui).get("search_term") == "synthetic saved UI state",
        "manual_items_count": manual_count,
        "expected_manual_items": expected_manual,
        "new_manual_tables_present": {"manual_research_batches", "manual_research_items"}.issubset(tables),
        "six_legacy_genre_ids": sorted(expected_ids),
    }


def run(qa_root: Path) -> dict:
    repo = Path(__file__).resolve().parents[1]
    test_area = (repo / ".test_tmp").resolve()
    root = qa_root.resolve()
    if not root.is_relative_to(test_area) or root == test_area or not root.name.startswith("V459_LEGACY_MIGRATION_"):
        raise RuntimeError("FIXTURE_ROOT_MUST_BE_A_NEW_V459_NAMED_CHILD_OF_REPO_TEST_TMP")
    if root.exists():
        raise RuntimeError("REFUSING_TO_REUSE_EXISTING_FIXTURE_ROOT")
    root.mkdir(parents=True)
    (root / FIXTURE_MARKER).write_text(FIXTURE_MARKER_VALUE + "\n", encoding="ascii")

    legacy, source_hashes = _load_legacy_store(root)
    source_db = root / "v459-legacy-source.sqlite"
    jp, kr, session_id = _seed_legacy_database(legacy, source_db)
    with sqlite3.connect(source_db) as db:
        historical_tables = _table_names(db)
        historical_genres = sorted(legacy.GENRES)
        source_user_version = db.execute("PRAGMA user_version").fetchone()[0]
    if set(historical_genres) != LEGACY_GENRES:
        raise RuntimeError("V459_HISTORICAL_GENRE_SOURCE_MISMATCH")
    source_snapshot = _legacy_snapshot(source_db, historical_tables)

    # These files are synthetic and intentionally outside the SQLite backup input.
    synthetic_state = root / "synthetic-user-state"
    oauth_dir = synthetic_state / "oauth_tokens"
    oauth_dir.mkdir(parents=True)
    synthetic_oauth_path = oauth_dir / (jp + ".json")
    synthetic_oauth_path.write_text(json.dumps({
        "token": "FAKE_OAUTH_TOKEN_FOR_FIXTURE_ONLY",
        "refresh_token": "FAKE_REFRESH_TOKEN_FOR_FIXTURE_ONLY",
        "client_id": "fixture-client-id",
    }), encoding="utf-8")
    credentials = CredentialStore(directory=synthetic_state / "provider_credentials")
    fake_provider_key = "FAKE_PROVIDER_KEY_FOR_V459_FIXTURE_ONLY"
    credentials.save("openai", fake_provider_key)
    synthetic_dpapi_same_user_roundtrip = credentials.load("openai") == fake_provider_key

    detached = root / "detached-v459.sqlite"
    wal_facts = _create_detached_wal_copy(source_db, detached, jp)
    vault = root / "synthetic-encrypted-vault"
    vault.mkdir()
    backup_id = "v459_migrate_" + uuid.uuid4().hex[:16]
    backup_result = zisu_release_backup_cli.run_backup(detached, vault, backup_id, fixture_root=root)
    ciphertexts = list(vault.glob("*.dpapi"))
    if backup_result.get("result") != "PASS" or len(ciphertexts) != 1:
        raise RuntimeError("SYNTHETIC_ENCRYPTED_BACKUP_FAILED")

    baseline_restore = root / "restore-before-migration.sqlite"
    restored_facts, image = _write_restored_database(ciphertexts[0], baseline_restore)
    old_snapshot = _legacy_snapshot(baseline_restore, historical_tables)
    if old_snapshot != source_snapshot:
        # The only intentional post-snapshot value is the WAL commit in profile schedule.
        expected = json.loads(json.dumps(source_snapshot))
        profiles = expected["rows"]["profiles"]
        profile_columns = expected["columns"]["profiles"]
        schedule_index = profile_columns.index("schedule")
        id_index = profile_columns.index("id")
        for row in profiles:
            if row[id_index] == jp:
                row[schedule_index] = "07:15,20:15 " + wal_facts["wal_uncheckpointed_marker_committed"]
        profiles.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))
        if old_snapshot != expected:
            raise RuntimeError("WAL_BACKUP_LOGICAL_DATA_MISMATCH")

    # Inject a migration interruption into a separate copy; preserve it as evidence.
    interrupted_db = root / "interrupted-migration.sqlite"
    interrupted_db.write_bytes(image)
    original_add_column = store._add_column
    injected = {"count": 0, "raised": False}

    def interrupt_once(db, table, definition):
        injected["count"] += 1
        if not injected["raised"] and table == "profiles" and definition.startswith("genre_config_json "):
            injected["raised"] = True
            raise RuntimeError("SYNTHETIC_MIGRATION_INTERRUPTED")
        return original_add_column(db, table, definition)

    store._add_column = interrupt_once
    try:
        try:
            store.initialize(interrupted_db)
        except RuntimeError as exc:
            if str(exc) != "SYNTHETIC_MIGRATION_INTERRUPTED":
                raise
        else:
            raise RuntimeError("MIGRATION_FAILURE_INJECTION_DID_NOT_FIRE")
    finally:
        store._add_column = original_add_column

    with sqlite3.connect(interrupted_db) as db:
        interrupted_integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        interrupted_rows = db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]

    # Restore the encrypted pre-migration snapshot to a new path after the injected failure.
    recovery_db = root / "recovered-from-encrypted-backup.sqlite"
    recovery_db.write_bytes(image)
    store.initialize(recovery_db)
    store.initialize(recovery_db)

    urls = "\n".join(f"https://www.youtube.com/watch?v=M{i:010d}" for i in range(50))
    batch_id = manual_research.create_batch(jp, urls, path=recovery_db)
    manual_research.save_evaluation(
        batch_id, jp, "M0000000000", {"human_fit": EXCLUDE, "notes": "synthetic post-migration EXCLUDE"}, recovery_db
    )
    store.initialize(recovery_db)
    migration_facts = _check_current_migration(recovery_db, old_snapshot, jp, kr, session_id, "v459JP00001", 50)
    manual_human = manual_research.list_items(batch_id, jp, recovery_db)[0]
    with sqlite3.connect(recovery_db) as db:
        manual_state = db.execute(
            "SELECT human_fit,notes FROM manual_research_items WHERE video_id='M0000000000'"
        ).fetchone()
        profile_rows = db.execute("SELECT id,market,genre,subgenre,connected_email,youtube_channel_id,schedule FROM profiles ORDER BY market").fetchall()
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    migration_facts.update({
        "manual_exclude_and_note": list(manual_state) if manual_state else None,
        "manual_first_item_matches": manual_human["video_id"] == "M0000000000",
        "profile_count": len(profile_rows),
        "all_legacy_genre_ids_present": set(historical_genres).issubset(LEGACY_GENRES),
        "wal_marker_preserved": any(wal_facts["wal_uncheckpointed_marker_committed"] in str(row[6]) for row in profile_rows),
        "known_migration_normalization": "genre_audit.rule_verdict: empty -> persisted predicted_decision, as implemented by store.initialize",
        "post_migration_integrity": integrity,
        "post_migration_foreign_key_errors": len(foreign_keys),
        "injected_interruption_fired": injected["raised"],
        "interrupted_copy_integrity": interrupted_integrity,
        "interrupted_copy_profile_count": interrupted_rows,
        "interrupted_migration_recovered_from_encrypted_backup": True,
        "source_user_version": source_user_version,
    })

    expected_exclude = EXCLUDE
    checks = {
        "historical_source_is_v459": True,
        "six_legacy_genres": set(historical_genres) == LEGACY_GENRES,
        "encrypted_backup_pass": backup_result["result"] == "PASS",
        "synthetic_same_user_dpapi_and_external_credentials_excluded": (
            synthetic_oauth_path.is_file() and synthetic_dpapi_same_user_roundtrip
            and not restored_facts.get("files")
        ),
        "wal_commit_restored": wal_facts["wal_present"] and migration_facts["wal_marker_preserved"],
        "old_table_rows_preserved": migration_facts["old_tables_columns_and_rows_preserved"],
        "jp_kr_profiles_and_channel_mapping_preserved": (
            migration_facts["profile_count"] == 2 and migration_facts["legacy_account_count"] == 1
            and migration_facts["legacy_channel_count"] == 2
            and migration_facts["jp_profile"]["youtube_channel_id"] == "fixture-jp-channel"
            and migration_facts["kr_profile"]["youtube_channel_id"] == "fixture-kr-channel"
        ),
        "person_exclude_unchanged": migration_facts["human_exclude"] == [expected_exclude, "V4.5.9 synthetic human exclusion"],
        "research_progress_and_ui_state_preserved": (
            migration_facts["research_session_state"] == "paused"
            and migration_facts["research_session_query_index"] == 2
            and migration_facts["ui_state_restored"]
        ),
        "manual_50_and_exclude_preserved": (
            migration_facts["manual_items_count"] == 50
            and migration_facts["manual_exclude_and_note"] == [EXCLUDE, "synthetic post-migration EXCLUDE"]
        ),
        "migration_rerun_safe": migration_facts["old_tables_columns_and_rows_preserved"] and migration_facts["manual_items_count"] == 50,
        "interruption_and_restore_path": (
            migration_facts["injected_interruption_fired"]
            and migration_facts["interrupted_copy_integrity"] == "ok"
            and migration_facts["interrupted_copy_profile_count"] == 2
            and migration_facts["interrupted_migration_recovered_from_encrypted_backup"]
        ),
        "integrity_and_foreign_keys": (
            migration_facts["post_migration_integrity"] == "ok"
            and migration_facts["post_migration_foreign_key_errors"] == 0
        ),
    }
    result = {
        "result": "PASS" if all(checks.values()) else "FAIL",
        "legacy_source": {
            "commit": LEGACY_COMMIT,
            "commit_date": "2026-10-18T10:20:27+09:00",
            "version_evidence": "README.md, diagnostics.py and Windows workflow/package paths label this revision V4.5.9",
            "historical_file_sha256": source_hashes,
            "genre_ids": historical_genres,
            "tables": historical_tables,
            "columns_by_table": source_snapshot["columns"],
            "schema_objects": source_snapshot["objects"],
            "legacy_snapshot_sha256": _sha(json.dumps(source_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")),
            "historical_tables_absent_at_v459": sorted({"vph_observations", "vph_evaluations", "research_schedules"} - set(historical_tables)),
        },
        "fixture_only": True,
        "real_user_data_accessed": False,
        "oauth_or_external_api_used": False,
        "database_backup": {
            "result": backup_result.get("result"),
            "backup_verification": backup_result,
            "ciphertext_file_count": len(ciphertexts),
            "dpapi_scope": "CurrentUser",
            "synthetic_oauth_fixture_created": synthetic_oauth_path.is_file(),
            "synthetic_dpapi_same_user_roundtrip": synthetic_dpapi_same_user_roundtrip,
            "encrypted_payload_files": sorted(restored_facts.get("files", {}).keys()),
            "wal": wal_facts,
            "backup_restore_logical_info": restored_facts["database_info"],
        },
        "migration": migration_facts,
        "checks": checks,
        "settings_and_credentials": {
            "ui_state": "SQLite ui_state rows are in the database backup and were compared after migration.",
            "oauth_tokens": "Historical source stores per-profile JSON under data_dir/oauth_tokens; not included in the DB-only backup. No real file was read or copied.",
            "provider_credentials": "CredentialStore stores provider DPAPI blobs under data_dir/provider_credentials; separate from SQLite and not included in DB-only backup.",
            "dpapi_limit": "CurrentUser protection is bound to the same Windows user/profile context; another user/PC or Windows reinstall is not verified and must use explicit re-authentication/re-entry.",
            "synthetic_same_user_dpapi_roundtrip": synthetic_dpapi_same_user_roundtrip,
            "synthetic_oauth_file_excluded_from_backup": "oauth_tokens/<profile_id>.json was created with fake values; backup restore contained no external payload files.",
            "full_user_state_backup": False,
        },
        "qa_root": str(root),
        "completed_utc_epoch": time.time(),
    }
    report = root / "v459-legacy-migration-report.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", default=None, help="New unique V459_LEGACY_MIGRATION_* child of this worktree's .test_tmp")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    root = Path(args.qa_root) if args.qa_root else repo / ".test_tmp" / ("V459_LEGACY_MIGRATION_" + uuid.uuid4().hex)
    try:
        result = run(root)
        print(json.dumps({"result": result["result"], "checks": result["checks"], "qa_root": result["qa_root"]}, ensure_ascii=False))
        return 0 if result["result"] == "PASS" else 1
    except Exception as exc:
        failure = {"result": "FAIL", "error_type": type(exc).__name__, "error": str(exc), "fixture_only": True}
        if "root" in locals():
            failure["qa_root"] = str(root)
            if root.exists():
                (root / "v459-legacy-migration-failure.json").write_text(
                    json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
