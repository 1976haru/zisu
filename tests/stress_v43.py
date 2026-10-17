"""Standalone V4.3 stress gate; always uses a disposable database."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcrs import store
from support import WorkspaceTemp


PROFILE_COUNT = 100
CANDIDATES_PER_PROFILE = 100


def candidate(profile_index, video_index, title):
    return {
        "video_id": f"{profile_index:03d}{video_index:08d}",
        "video_title": f"{title} 한글 日本語 English 🎧 <>& {video_index}",
        "description": "합성 회귀 데이터이며 실제 음악 정밀도 표본이 아닙니다.",
        "channel_title": f"테스트 채널 日本 {profile_index}",
        "keyword": "오프라인 fixture",
        "views": video_index,
    }


def run():
    failures = []
    started = time.perf_counter()
    tracemalloc.start()
    with WorkspaceTemp() as root:
        database = root / "공백 日本語 stress.db"
        profiles = []
        for index in range(PROFILE_COUNT):
            market = "JP" if index % 2 == 0 else "KR"
            genre = "MORNING_CHILL_RAP" if market == "JP" else "CHILL_RAP"
            profile_id = store.create_profile(f"가상 프로필 {index:03d} 🎧", market, genre, database)
            title = "朝 morning chill rap" if market == "JP" else "감성 힙합 chill rap"
            rows = [candidate(index, item, title) for item in range(CANDIDATES_PER_PROFILE)]
            store.save_candidates(profile_id, rows, database)
            store.set_profile_search_terms(profile_id, market, [f"격리 검색어 {index}"], database)
            profiles.append(profile_id)

        # Restart/idempotent migration and duplicate request behavior.
        for _ in range(5):
            store.initialize(database)
        duplicate = candidate(0, 0, "朝 morning chill rap")
        store.save_candidates(profiles[0], [duplicate, duplicate], database)

        def verify(index):
            profile_id = profiles[index]
            rows = store.list_candidates(profile_id, query="🎧", path=database)
            market = "JP" if index % 2 == 0 else "KR"
            terms = store.list_profile_search_terms(profile_id, market, database)
            return len(rows) == CANDIDATES_PER_PROFILE and terms == [f"격리 검색어 {index}"]

        with ThreadPoolExecutor(max_workers=12) as pool:
            isolated = list(pool.map(verify, range(PROFILE_COUNT)))
        if not all(isolated):
            failures.append("profile isolation/read verification")

        # Concurrent independent writers exercise busy-timeout/WAL handling.
        def writer(index):
            profile_id = profiles[index]
            store.upsert_metric(profile_id, "2026-10-17", index, index, 100, 1.0, 2.0, index, database)
            return len(store.list_metrics(profile_id, database))

        with ThreadPoolExecutor(max_workers=12) as pool:
            write_counts = list(pool.map(writer, range(PROFILE_COUNT)))
        if write_counts != [1] * PROFILE_COUNT:
            failures.append("concurrent writes")

        # Hold an actual write lock briefly; the second writer must wait and
        # complete instead of surfacing "database is locked" to the user.
        lock = sqlite3.connect(database, timeout=15)
        lock.execute("PRAGMA busy_timeout=15000")
        lock.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting_write = pool.submit(
                store.upsert_metric,
                profiles[0], "2026-10-18", 1, 1, 1, 1.0, 1.0, 1, database,
            )
            time.sleep(0.1)
            lock.commit()
            lock.close()
            try:
                waiting_write.result(timeout=5)
            except Exception as exc:  # result is reported, never hidden
                failures.append(f"lock recovery: {type(exc).__name__}")

        backup = root / "backup 복원.db"
        store.backup(backup, database)
        with closing(sqlite3.connect(backup)) as restored:
            restored_integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
            restored_profiles = restored.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            restored_candidates = restored.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        if (restored_integrity, restored_profiles, restored_candidates) != (
            "ok", PROFILE_COUNT, PROFILE_COUNT * CANDIDATES_PER_PROFILE
        ):
            failures.append("backup/restore equality")

        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = {
            "profiles": PROFILE_COUNT,
            "candidates": PROFILE_COUNT * CANDIDATES_PER_PROFILE,
            "concurrent_workers": 12,
            "elapsed_seconds": round(elapsed, 3),
            "peak_memory_mib": round(peak / 1024 / 1024, 2),
            "failures": len(failures),
            "failure_rate": len(failures) / 6,
            "details": failures,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    run()
