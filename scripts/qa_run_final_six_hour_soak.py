"""Run and supervise the accepted Windows onedir EXE in a fresh offline fixture.

This harness never attaches to or terminates an unrelated process. It retains
the exact child process handle until natural exit and writes progress atomically.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

EXPECTED_EXE = "8d579448361a1f574d12665618a4a2f9af63cec4f6ecda3b3074520b0ea2fa19"
EXPECTED_ZIP = "86dcd767dfa2dcf64cbfd0b3e3f95152453519bfabf43b1d8e5d0853b1decf81"
SIX_HOURS = 21600


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".partial-" + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _inside(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left in right.parents or right in left.parents


def _check_sqlite(db_path: Path) -> tuple[str, int]:
    uri = db_path.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as db:
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(db.execute("PRAGMA foreign_key_check").fetchall())
    return integrity, foreign_keys


def _final_checks(exe: Path, qa_root: Path, process_id: int,
                  child_exit: int, supervisor_elapsed: float) -> dict:
    reports = qa_root / "reports"
    report_path = reports / "offline-soak-report.json"
    metrics_path = reports / "metrics.csv"
    events_path = reports / "events.jsonl"
    db_path = qa_root / "data" / "zisu_v2.db"
    if not report_path.is_file() or not metrics_path.is_file() or not events_path.is_file():
        raise RuntimeError("FINAL_REPORT_OR_SAMPLE_FILES_MISSING")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    criteria = report.get("pass_criteria", {})
    true_criteria = [name for name, value in criteria.items() if value is True]
    with metrics_path.open("r", encoding="utf-8", newline="") as stream:
        metrics = list(csv.DictReader(stream))
    with events_path.open("r", encoding="utf-8") as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    integrity, fk_errors = _check_sqlite(db_path)
    expected_count = int(report.get("expected_sample_count", -1))
    actual_elapsed = float(report.get("actual_monotonic_elapsed_seconds", 0))
    same_exe = Path(report.get("executable_path", str(exe))).resolve() == exe.resolve()
    same_pid = int(report.get("process_id", -1)) == process_id
    checks = {
        "same_exe_sha256": _sha(exe) == EXPECTED_EXE,
        "same_frozen_process_pid": same_pid,
        "frozen_executable": bool(report.get("frozen_executable")),
        "natural_exit_code_zero": child_exit == 0,
        "product_final_pass": report.get("result") == "PASS" and report.get("final_verdict") == "PASS",
        "monotonic_at_least_21600_seconds": actual_elapsed >= SIX_HOURS,
        "supervisor_observed_child_for_at_least_21600_seconds": supervisor_elapsed >= SIX_HOURS,
        "actual_samples_73_of_73": report.get("sample_count") == 73 == len(metrics) == expected_count,
        "product_pass_criteria_13_of_13": len(criteria) == 13 and len(true_criteria) == 13,
        "schedule_completed": report.get("final_schedule_state") == "completed",
        "database_integrity_ok": integrity == "ok" and report.get("database_integrity") == "ok",
        "foreign_key_check_zero": fk_errors == 0,
        "network_attempts_zero": report.get("external_network_attempts") == 0,
        "metrics_and_events_nonempty": bool(metrics) and bool(events),
        "active_scheduler_process_was_frozen_exe": bool(criteria.get("same_exe_process_active_scheduler")),
    }
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "exe_sha256": _sha(exe),
        "zip_sha256": EXPECTED_ZIP,
        "process_id": process_id,
        "process_exit_code": child_exit,
        "child_monotonic_seconds": actual_elapsed,
        "supervisor_monotonic_seconds": round(supervisor_elapsed, 3),
        "sample_count": len(metrics),
        "expected_sample_count": expected_count,
        "pass_criteria_true_count": len(true_criteria),
        "pass_criteria_count": len(criteria),
        "database_integrity": integrity,
        "foreign_key_errors": fk_errors,
        "external_network_attempts": report.get("external_network_attempts"),
        "network_or_oauth_env_present": False,
        "final_schedule_state": report.get("final_schedule_state"),
        "metrics_path": str(metrics_path),
        "events_path": str(events_path),
        "product_report_path": str(report_path),
        "started_at_utc": report.get("started_at_utc"),
        "ended_at_utc": report.get("ended_at_utc"),
        "cpu_first_last": [metrics[0].get("process_cpu_percent"), metrics[-1].get("process_cpu_percent")],
        "rss_first_last_bytes": [metrics[0].get("process_rss_bytes"), metrics[-1].get("process_rss_bytes")],
        "handles_first_last": [metrics[0].get("handle_count"), metrics[-1].get("handle_count")],
        "metrics_csv_rows": len(metrics),
        "event_jsonl_rows": len(events),
    }


def run(exe_path: Path, zip_path: Path, qa_parent: Path) -> int:
    exe = exe_path.resolve(strict=True)
    zip_file = zip_path.resolve(strict=True)
    parent = qa_parent.resolve()
    worktree = Path(__file__).resolve().parents[1]
    if _sha(exe) != EXPECTED_EXE or _sha(zip_file) != EXPECTED_ZIP:
        raise RuntimeError("ACCEPTED_PACKAGE_HASH_MISMATCH")
    if not parent.is_relative_to(worktree / ".test_tmp"):
        raise RuntimeError("QA_ROOT_MUST_BE_NEW_FOLDER_UNDER_WORKTREE_TEST_TMP")
    if parent.exists() or "qa" not in parent.name.casefold() or "soak" not in parent.name.casefold():
        raise RuntimeError("REFUSING_TO_REUSE_OR_MISLABEL_QA_ROOT")
    parent.mkdir(parents=True)
    qa_root = parent / "V46_QA_SOAK_RUN"
    data_dir, report_dir = qa_root / "data", qa_root / "reports"
    if qa_root.exists():
        raise RuntimeError("REFUSING_TO_REUSE_SOAK_RUN_ROOT")

    # Initial host variables are synthetic siblings. The frozen QA entry point
    # performs its own overlap checks, then redirects all five paths into qa_root.
    host_local = parent / "host-localappdata"
    host_appdata = parent / "host-appdata"
    host_profile = parent / "host-userprofile"
    host_temp = parent / "host-temp"
    for path in (host_local, host_appdata, host_profile, host_temp):
        path.mkdir()
    for protected in (Path(r"D:\03 zisu\ZISU_APP"), host_local / "ZisuResearchStudio",
                      host_appdata / "ZisuResearchStudio"):
        if _inside(qa_root, protected):
            raise RuntimeError("QA_ROOT_OVERLAPS_PROTECTED_PATH")

    windows = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    env = {
        "SystemRoot": str(windows), "WINDIR": str(windows),
        "PATH": str(windows / "System32"),
        "LOCALAPPDATA": str(host_local), "APPDATA": str(host_appdata),
        "USERPROFILE": str(host_profile),
        "HOMEDRIVE": host_profile.drive or "C:",
        "HOMEPATH": str(host_profile)[len(host_profile.drive):] if host_profile.drive else str(host_profile),
        "TEMP": str(host_temp), "TMP": str(host_temp),
    }
    if any(name in env for name in ("YOUTUBE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                                    "ZISU_DATA_DIR", "ZISU_QA_ROOT")):
        raise RuntimeError("UNEXPECTED_CREDENTIAL_OR_DATA_OVERRIDE_IN_CHILD_ENV")
    args = [str(exe), "--qa-offline-soak", "--duration-hours", "6",
            "--data-dir", str(data_dir), "--report-dir", str(report_dir)]
    created = datetime.now(timezone.utc).isoformat()
    initial = {
        "status": "STARTING", "started_at_utc": created,
        "exe_path": str(exe), "exe_sha256": _sha(exe), "zip_sha256": _sha(zip_file),
        "qa_root": str(qa_root), "data_dir": str(data_dir), "report_dir": str(report_dir),
        "arguments": args[1:], "short_qa": False,
        "continuous_live_schedule_requested_seconds": SIX_HOURS,
        "fixture_environment_only": True, "credentials_in_child_env": False,
    }
    _atomic_json(parent / "supervisor-start.json", initial)
    output_path = parent / "frozen-exe-output.log"
    start_mono = time.monotonic()
    start_utc = datetime.now(timezone.utc)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with output_path.open("xb") as output:
        process = subprocess.Popen(args, cwd=exe.parent, env=env,
                                   stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, creationflags=creationflags)
        pid = process.pid
        _atomic_json(parent / "supervisor-start.json", {
            **initial, "status": "RUNNING", "process_id": pid,
            "process_started_at_utc": start_utc.isoformat(),
        })
        last_heartbeat = 0.0
        while process.poll() is None:
            elapsed = time.monotonic() - start_mono
            if elapsed - last_heartbeat >= 60:
                last_heartbeat = elapsed
                product_report = report_dir / "offline-soak-report.json"
                try:
                    current = json.loads(product_report.read_text(encoding="utf-8"))
                    sample_count = int(current.get("sample_count", 0))
                    stage = current.get("result", "RUNNING")
                    same_pid = current.get("process_id") == pid
                except (OSError, ValueError, TypeError):
                    sample_count, stage, same_pid = 0, "REPORT_PENDING", False
                heartbeat = {
                    "status": "RUNNING", "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "process_id": pid, "process_handle_alive": True,
                    "supervisor_monotonic_seconds": round(elapsed, 3),
                    "product_report_stage": stage, "product_sample_count": sample_count,
                    "product_report_pid_matches": same_pid,
                }
                _atomic_json(parent / "supervisor-heartbeat.json", heartbeat)
                print(json.dumps(heartbeat), flush=True)
            time.sleep(1)
        exit_code = process.wait()
    elapsed = time.monotonic() - start_mono
    try:
        result = _final_checks(exe, qa_root, pid, exit_code, elapsed)
    except Exception as exc:
        result = {"pass": False, "failure_type": type(exc).__name__,
                  "failure_stage": "post_process_evidence_validation",
                  "process_id": pid, "process_exit_code": exit_code,
                  "supervisor_monotonic_seconds": round(elapsed, 3),
                  "exe_sha256": _sha(exe),
                  "qa_root": str(qa_root)}
    result["status"] = "FULL_SOAK_PASS" if result["pass"] else (
        "FAIL" if exit_code != 0 or result.get("sample_count", 0) else "INCOMPLETE")
    result["started_at_utc"] = start_utc.isoformat()
    result["supervisor_ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["qa_root"] = str(qa_root)
    _atomic_json(parent / "final-soak-supervisor.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True)
    parser.add_argument("--zip", required=True)
    parser.add_argument("--qa-parent", required=True)
    args = parser.parse_args()
    raise SystemExit(run(Path(args.exe), Path(args.zip), Path(args.qa_parent)))
