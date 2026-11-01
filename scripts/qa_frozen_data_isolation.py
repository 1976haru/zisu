"""Fixture-only frozen EXE data-path and fail-closed integration gate.

Every child receives a minimal environment whose filesystem roots are newly
created below one QA root. No production database or credentials are opened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid


EXPECTED_EXE_SHA256 = "8d579448361a1f574d12665618a4a2f9af63cec4f6ecda3b3074520b0ea2fa19"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(exe: Path, args: list[str], env: dict[str, str], timeout: int = 35,
         *, through_launcher: bool = False) -> dict:
    started = time.monotonic()
    if through_launcher:
        if args:
            raise ValueError("The beginner launcher does not accept QA arguments in this verifier.")
        launcher = exe.parent / "ZISU-SIGNAL-LAB-Run.cmd"
        if not launcher.is_file():
            raise RuntimeError("PACKAGED_BEGINNER_LAUNCHER_MISSING")
        system_root = env.get("SystemRoot", r"C:\Windows")
        command = [str(Path(system_root) / "System32" / "cmd.exe"), "/d", "/c",
                   "call", str(launcher)]
    else:
        command = [str(exe), *args]
    process = subprocess.Popen(command, cwd=exe.parent, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, errors="replace")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill only the process tree created by this fixture's wrapper.
        if through_launcher:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=10)
        else:
            process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"FIXTURE_CHILD_TIMEOUT pid={process.pid}") from None
    return {"pid": process.pid, "exit_code": process.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": stdout[-1000:], "stderr_tail": stderr[-1000:]}


def _environment(root: Path, *, explicit_data: bool, report: Path,
                 smoke_profiles: bool = False) -> dict[str, str]:
    local, roaming, temp = root / "localappdata", root / "appdata", root / "temp"
    user_profile = root / "userprofile"
    for directory in (local, roaming, temp, user_profile, report.parent):
        directory.mkdir(parents=True, exist_ok=True)
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    env = {"SystemRoot": system_root, "WINDIR": system_root,
           "PATH": str(Path(system_root) / "System32"),
           "LOCALAPPDATA": str(local), "APPDATA": str(roaming),
           "USERPROFILE": str(user_profile),
           "HOMEDRIVE": user_profile.drive or "C:",
           "HOMEPATH": str(user_profile)[len(user_profile.drive):] if user_profile.drive else str(user_profile),
           "TEMP": str(temp), "TMP": str(temp),
           "ZISU_EXE_SMOKE_REPORT": str(report),
           "ZISU_EXE_SMOKE_START_COUNTER": str(time.perf_counter_ns()),
           "ZISU_EXE_SMOKE_COUNTER_FREQUENCY": "1000000000"}
    if explicit_data:
        data = root / "explicit-data"
        data.mkdir(parents=True, exist_ok=True)
        env["ZISU_DATA_DIR"] = str(data)
    if smoke_profiles:
        env["ZISU_EXE_SMOKE_PROFILE_TEST"] = "1"
    return env


def _smoke(exe: Path, root: Path, *, explicit_data: bool,
           smoke_profiles: bool) -> tuple[dict, Path, dict]:
    report = root / "reports" / f"startup-{uuid.uuid4().hex}.json"
    env = _environment(root, explicit_data=explicit_data, report=report,
                       smoke_profiles=smoke_profiles)
    result = _run(exe, [], env, through_launcher=True)
    expected_db = ((root / "explicit-data") if explicit_data else
                   (root / "localappdata" / "ZisuResearchStudio")) / "zisu_v2.db"
    if result["exit_code"] != 0:
        raise RuntimeError(f"NORMAL_FIXTURE_START_FAILED exit={result['exit_code']} "
                           f"stdout={result['stdout_tail']!r} stderr={result['stderr_tail']!r}")
    if not expected_db.is_file():
        raise RuntimeError("DATABASE_NOT_CREATED_AT_EXPECTED_FIXTURE_PATH")
    launcher_log = root / "localappdata" / "ZisuResearchStudio" / "logs" / "launcher_latest.log"
    if not launcher_log.is_file():
        raise RuntimeError("BEGINNER_LAUNCHER_LOG_NOT_WRITTEN_TO_SYNTHETIC_LOCALAPPDATA")
    payload = json.loads(report.read_text(encoding="utf-8"))
    if payload.get("stage") != "offline_smoke_complete":
        raise RuntimeError(f"SMOKE_REPORT_INCOMPLETE stage={payload.get('stage')}")
    if (not payload.get("gui_initialized") or payload.get("network_used") or
            payload.get("oauth_connected") or payload.get("database_integrity") != "ok"):
        raise RuntimeError("GUI_NETWORK_OAUTH_OR_DATABASE_SMOKE_GATE_FAILED")
    if smoke_profiles and not (payload.get("profile_test") or {}).get("passed"):
        raise RuntimeError("PROFILE_ISOLATION_SMOKE_GATE_FAILED")
    with sqlite3.connect(f"file:{expected_db.as_posix()}?mode=ro", uri=True) as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        profiles = db.execute("SELECT count(*) FROM profiles").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("FIXTURE_DATABASE_INTEGRITY_FAILED")
    return result, expected_db, {"integrity": integrity, "profile_count": profiles,
                                 "network_used": payload.get("network_used"),
                                 "oauth_connected": payload.get("oauth_connected"),
                                 "smoke_report_stage": payload.get("stage")}


def _run_resume_block(exe: Path, root: Path, *, env_mode: str,
                      relative_root: bool = False, overlap: bool = False) -> dict:
    data, reports = root / "data", root / "reports"
    data.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    report = reports / "resume-startup.json"
    local = root / "synthetic-local"
    roaming, temp = root / "synthetic-appdata", root / "synthetic-temp"
    for directory in (local, roaming, temp):
        directory.mkdir(parents=True, exist_ok=True)

    qa_root = root
    env = {"SystemRoot": os.environ.get("SystemRoot") or r"C:\Windows",
           "WINDIR": os.environ.get("SystemRoot") or r"C:\Windows",
           "PATH": str(Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"),
           "USERPROFILE": str(root / "synthetic-userprofile"),
           "ZISU_EXE_SMOKE_REPORT": str(report)}
    (root / "synthetic-userprofile").mkdir(parents=True, exist_ok=True)
    if env_mode == "missing":
        # The user roots and temp are fixtures, but QA's required data override
        # is absent. This fails before any store initialization.
        (root / "localappdata").mkdir(parents=True, exist_ok=True)
        (root / "appdata").mkdir(parents=True, exist_ok=True)
        (root / "temp").mkdir(parents=True, exist_ok=True)
        env.update({"LOCALAPPDATA": str(root / "localappdata"),
                    "APPDATA": str(root / "appdata"),
                    "TEMP": str(root / "temp"), "TMP": str(root / "temp"),
                    "ZISU_FROZEN_MANUAL_E2E": "1"})
    elif env_mode == "overlap":
        qa_root = local / "ZisuResearchStudio" / "V46_QA_manual_overlap"
        (qa_root / "data").mkdir(parents=True, exist_ok=True)
        (qa_root / "reports").mkdir(parents=True, exist_ok=True)
        report = qa_root / "reports" / "resume-startup.json"
        env["ZISU_EXE_SMOKE_REPORT"] = str(report)
        env["LOCALAPPDATA"] = str(local)
        env["APPDATA"] = str(roaming)
        env["TEMP"] = str(temp)
        env["TMP"] = str(temp)
    elif env_mode == "relative":
        qa_root = local / "ZisuResearchStudio" / "V46_QA_manual_relative"
        (qa_root / "data").mkdir(parents=True, exist_ok=True)
        (qa_root / "reports").mkdir(parents=True, exist_ok=True)
        report = qa_root / "reports" / "resume-startup.json"
        env["ZISU_EXE_SMOKE_REPORT"] = str(report)
        env["LOCALAPPDATA"] = str(local)
        env["APPDATA"] = str(roaming)
        env["TEMP"] = str(temp)
        env["TMP"] = str(temp)
        # Lexical path traverses out and back into the protected fixture root.
        qa_root = qa_root.parent / "unused" / ".." / qa_root.name
    elif env_mode == "junction":
        target = local / "ZisuResearchStudio" / "V46_QA_manual_junction_target"
        (target / "data").mkdir(parents=True, exist_ok=True)
        (target / "reports").mkdir(parents=True, exist_ok=True)
        qa_root = root / "V46_QA_manual_junction_alias"
        link = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                               str(qa_root), str(target)],
                              capture_output=True, text=True, check=False)
        if link.returncode != 0:
            return {"status": "SKIP_JUNCTION_UNAVAILABLE",
                    "exit_code": None, "database_created": False}
        report = qa_root / "reports" / "resume-startup.json"
        env["ZISU_EXE_SMOKE_REPORT"] = str(report)
        env["LOCALAPPDATA"] = str(local)
        env["APPDATA"] = str(roaming)
        env["TEMP"] = str(temp)
        env["TMP"] = str(temp)
    else:
        raise ValueError("unknown env_mode")

    result = _run(exe, ["--qa-manual-research-e2e-resume", "--qa-root", str(qa_root)], env)
    report_file = Path(report)
    if result["exit_code"] != 2:
        raise RuntimeError(f"UNSAFE_QA_FAIL_CLOSED_EXIT_{result['exit_code']}")
    if env_mode == "missing":
        failure_report = reports / "resume-e2e.json"
        if not failure_report.is_file():
            raise RuntimeError("MISSING_ENVIRONMENT_REJECTION_REPORT_NOT_WRITTEN")
        payload = json.loads(failure_report.read_text(encoding="utf-8"))
        if payload.get("failed_checks") != ["ZISU_DATA_DIR"]:
            raise RuntimeError("MISSING_ENVIRONMENT_REJECTION_CHECKS_UNEXPECTED")
    else:
        if not report_file.is_file():
            raise RuntimeError("OVERLAP_REJECTION_STAGE_REPORT_NOT_WRITTEN")
        probe = json.loads(report_file.read_text(encoding="utf-8"))
        stages = [item.get("stage") for item in probe.get("events", [])]
        if "manual_e2e_resume_root_validation_failed" not in stages:
            raise RuntimeError("OVERLAP_REJECTION_STAGE_NOT_RECORDED")
    if list(root.rglob("zisu_v2.db")):
        raise RuntimeError("FAILED_QA_START_CREATED_A_DATABASE")
    return {**result, "qa_root_resolved": str(qa_root.resolve()),
            "report_written": report_file.is_file(), "database_created": False}


def run(exe_path: Path, root: Path) -> dict:
    exe, root = exe_path.resolve(strict=True), root.resolve()
    if _sha(exe) != EXPECTED_EXE_SHA256:
        raise RuntimeError("EXE_HASH_DOES_NOT_MATCH_ACCEPTED_ONEDIR")
    if root.exists() or "qa" not in root.name.casefold():
        raise RuntimeError("REFUSING_TO_REUSE_OR_NON_QA_ROOT")
    root.mkdir(parents=True)

    explicit_root = root / "A_explicit_path"
    explicit, explicit_db, explicit_state = _smoke(
        exe, explicit_root, explicit_data=True, smoke_profiles=True)
    default_root = root / "B_synthetic_user_default"
    first, default_db, first_state = _smoke(
        exe, default_root, explicit_data=False, smoke_profiles=True)
    second, same_db, second_state = _smoke(
        exe, default_root, explicit_data=False, smoke_profiles=False)
    if same_db != default_db or first_state["profile_count"] < 2 or second_state["profile_count"] != first_state["profile_count"]:
        raise RuntimeError("SAME_FIXTURE_RESTART_DID_NOT_PRESERVE_PROFILE_DATA")

    blocked = {}
    for case, mode in (("V46_QA_manual_C_missing_paths", "missing"),
                       ("V46_QA_manual_D_protected_overlap", "overlap"),
                       ("V46_QA_manual_E_relative_traversal", "relative"),
                       ("V46_QA_manual_F_junction", "junction")):
        blocked[case] = _run_resume_block(exe, root / case, env_mode=mode)

    return {"result": "PASS", "exe_sha256": _sha(exe),
            "explicit_isolation": {"exit_code": explicit["exit_code"],
                                   "db_within_fixture": explicit_db.is_relative_to(root), **explicit_state},
            "synthetic_default_path": {"first_exit_code": first["exit_code"],
                                       "restart_exit_code": second["exit_code"],
                                       "db_within_fixture": default_db.is_relative_to(root),
                                       "same_db_path_on_restart": same_db == default_db,
                                       "profiles_before_restart": first_state["profile_count"],
                                       "profiles_after_restart": second_state["profile_count"],
                                       "integrity": second_state["integrity"]},
            "qa_fail_closed": blocked,
            "real_user_data_accessed": False, "network_or_oauth": False,
            "created_utc_epoch": time.time()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True)
    parser.add_argument("--qa-root", required=True)
    args = parser.parse_args()
    result = run(Path(args.exe), Path(args.qa_root))
    destination = Path(args.qa_root) / "user-data-isolation-report.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    print(json.dumps(result, indent=2))
