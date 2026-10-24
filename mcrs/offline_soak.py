"""Explicit offline soak mode, driven by the production scheduler and Autopilot.

This mode is intended for the packaged executable. Its short mode accelerates
the schedule clock for CI and is never reported as a wall-clock soak pass.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class _BlockedNetwork:
    """Fail closed and count any attempt to use a network entry point."""
    def __init__(self):
        self.attempts = 0
        self._originals = []

    def _deny(self, *_args, **_kwargs):
        self.attempts += 1
        raise OSError("Network is disabled in offline soak mode")

    def __enter__(self):
        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (socket, "create_connection"), (socket, "getaddrinfo"),
                            (urllib.request, "urlopen")):
            self._originals.append((owner, name, getattr(owner, name)))
            setattr(owner, name, self._deny)
        return self

    def __exit__(self, *_exc):
        for owner, name, original in reversed(self._originals):
            setattr(owner, name, original)
        self._originals.clear()


def _resource_row(snapshot, now_utc, elapsed, schedule, calls, db_errors, exceptions):
    handles = None
    if os.name == "nt":
        try:
            import ctypes
            value = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetProcessHandleCount(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(value)):
                handles = int(value.value)
        except Exception:
            handles = None
    return {
        "timestamp_utc": now_utc.isoformat(), "elapsed_seconds": round(elapsed, 3),
        "cpu_percent": snapshot.system_cpu_percent,
        "process_cpu_percent": snapshot.process_cpu_percent,
        "process_rss_bytes": snapshot.process_rss_bytes,
        "available_ram_bytes": snapshot.available_ram_bytes,
        "system_memory_percent": snapshot.system_memory_percent,
        "thread_count": threading.active_count(), "handle_count": handles,
        # This process is the sole QA-owned process. System process count is not
        # substituted when it is unavailable.
        "qa_owned_process_count": 1, "schedule_state": schedule.get("state"),
        "phase": schedule.get("phase"), "search_calls_used": calls,
        "search_calls_remaining": schedule.get("search_calls_remaining"),
        "checkpoint_present": bool(schedule.get("checkpoint_json")),
        "db_errors": db_errors, "exceptions": exceptions,
    }


def run_offline_soak(*, data_dir, report_dir, duration_hours=6, mode="wallclock",
                     short_duration_seconds=45, monotonic_fn=time.monotonic,
                     sleep_fn=time.sleep, resource_sampler=None):
    """Run a six-hour production scheduler session using a deterministic collector.

    ``mode='short'`` advances fake schedule time by five minutes per sample;
    its result is explicitly ``SHORT_QA_PASS`` and cannot satisfy wall-clock QA.
    Clock/sleeper/resource injection exists solely for unit tests.
    """
    from . import genres, research_scheduler as scheduler, store
    from .autopilot import AutopilotConfig, AutopilotController
    from .resource_guard import ResourceGuard, ResourceSnapshot, sample_resources

    data_dir, report_dir = Path(data_dir).resolve(), Path(report_dir).resolve()
    if duration_hours != 6 or mode not in {"wallclock", "short"}:
        raise ValueError("Offline soak supports only a six-hour schedule and known run modes.")
    if mode == "short" and not 30 <= short_duration_seconds <= 90:
        raise ValueError("Short mode must take between 30 and 90 wall-clock seconds.")
    if not data_dir.is_dir() or not report_dir.is_dir() or data_dir == report_dir:
        raise ValueError("Offline soak requires existing, separate isolated data/report directories.")

    report_path = report_dir / "offline-soak-report.json"
    metrics_path = report_dir / "metrics.csv"
    events_path = report_dir / "events.jsonl"
    started_utc = datetime.now(timezone.utc)
    started_mono = monotonic_fn()
    virtual_start = started_utc
    schedule_now = [virtual_start]
    metrics, events = [], []
    db_errors = exceptions = external_attempts = 0
    missed_intervals = 0
    last_sample_mono = None
    calls = 0
    transitions = {"research": 0, "break": 0}
    research_phase_runs = set()
    schedule_started_mono = None
    db_path = data_dir / "zisu_v2.db"
    run_id = uuid.uuid4().hex
    target_profile = recovery_profile = ""
    main_schedule_id = ""
    custom_id = genres.custom_genre_id("Offline Soak Dream Pop")
    custom = {"id": custom_id, "name": "Offline Soak Dream Pop",
              "include_terms": ["dream pop", "ethereal", "synth textures"],
              "exclude_terms": ["spoken word", "white noise"],
              "jp_queries": ["ドリームポップ"], "kr_queries": ["드림 팝"],
              "en_queries": ["dream pop playlist"]}
    final_verdict = "INTERRUPTED"
    caught_error = None
    exit_code = 2

    def persist_report(status, *, ended=None):
        actual_elapsed = max(0.0, monotonic_fn() - started_mono)
        payload = {
            "schema_version": 1, "run_id": run_id,
            "mode": "accelerated_virtual_6h" if mode == "short" else "wallclock_6h",
            "result": status, "final_verdict": status,
            "started_at_utc": started_utc.isoformat(),
            "ended_at_utc": (ended or datetime.now(timezone.utc)).isoformat(),
            "actual_monotonic_elapsed_seconds": round(actual_elapsed, 3),
            "active_schedule_wall_clock_elapsed_seconds": round(
                max(0.0, monotonic_fn() - schedule_started_mono)
                if schedule_started_mono is not None else 0.0, 3),
            "virtual_schedule_elapsed_seconds": (duration_hours * 3600 if mode == "short"
                                                    and status == "SHORT_QA_PASS" else
                                                    int((schedule_now[0] - virtual_start).total_seconds())),
            "requested_duration_hours": duration_hours,
            "requested_search_calls": 64, "mock_search_calls": calls,
            "external_network_attempts": external_attempts,
            "research_phase_runs": len(research_phase_runs),
            "phase_transition_observations": transitions,
            "profile_isolation_verified": bool(target_profile and recovery_profile
                                               and target_profile != recovery_profile),
            "genre_configuration_preserved": False,
            "checkpoint_verified": any(row["checkpoint_present"] for row in metrics),
            "database_integrity": "not_checked", "database_errors": db_errors,
            "exception_count": exceptions,
            "caught_error_type": caught_error,
            "report_path": str(report_path), "metrics_path": str(metrics_path),
            "events_path": str(events_path), "sample_count": len(metrics),
            "expected_sample_count": duration_hours * 3600 // 300 + 1,
            "missed_sample_intervals": missed_intervals,
            "metrics": metrics, "events": events,
            "process_id": os.getpid(),
            "frozen_executable": bool(getattr(sys, "frozen", False)),
            "pass_criteria": {
                "six_hour_wall_clock": status == "PASS" and schedule_started_mono is not None
                    and monotonic_fn() - schedule_started_mono >= 21600,
                "same_exe_process_active_scheduler": bool(getattr(sys, "frozen", False)),
                "research_and_break_observed": bool(transitions["research"] and transitions["break"]),
                "mock_search_only_in_research": True,
                "search_allowance_not_exceeded": calls <= 64,
                "external_api_calls_zero": external_attempts == 0,
                "database_integrity_ok": False,
                "checkpoint_and_genre_preserved": False,
                "schedule_completed": False,
                "search_quota_not_exceeded": calls <= 64,
                "no_unexpected_exceptions": exceptions == 0,
                "no_missing_sample_intervals": missed_intervals == 0 and
                    len(metrics) == duration_hours * 3600 // 300 + 1,
            },
        }
        _atomic_write(report_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        if metrics:
            columns = list(metrics[0])
            temporary = metrics_path.with_name(metrics_path.name + ".tmp-" + uuid.uuid4().hex)
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(metrics)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, metrics_path)
        _atomic_write(events_path, "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))

    def event(name, **details):
        events.append({"timestamp_utc": schedule_now[0].isoformat(), "event": name, **details})

    def collector(_term, _market, _limit, page_token=None, **_kwargs):
        nonlocal calls
        current = scheduler.get(main_schedule_id, target_profile, db_path, now=schedule_now[0])
        if not current or current["state"] != "running" or current["phase"] != "research":
            raise RuntimeError("Mock collector invoked outside a scheduled research phase")
        calls += 1
        event("mock_search", call=calls, phase=current["phase"], page_token=bool(page_token))
        index = calls
        return {"items": [{
            "video_id": f"Q{index:010d}",
            "video_title": f"Dream Pop ethereal synth textures {index}",
            "description": "Dream pop playlist with ethereal vocals and synth textures.",
            "channel_title": f"Mock Dream Channel {index}", "channel_id": f"mock-channel-{index}",
            "keyword": "dream pop playlist", "published_at": "2026-01-01T00:00:00Z",
            "views": 1,
        }], "nextPageToken": ""}

    blocked_network = _BlockedNetwork()
    blocked_network.__enter__()
    try:
        store.initialize(db_path)
        target_profile = store.create_profile(
            "QA Dream Pop JP", "JP", custom_id, db_path,
            genre_config={"custom_genres": [custom], "subgenres": ["ethereal dream pop"],
                          "facets": {"moods": ["dreamy"], "eras": ["2020s"]},
                          "search_languages": ["JP", "EN"]})
        recovery_profile = store.create_profile(
            "QA Jazz KR", "KR", "JAZZ_SMOOTH", db_path,
            genre_config={"facets": {"moods": ["calm"]}, "search_languages": ["KR"]})
        configured = store.profile_genre_config(store.get_profile(target_profile, db_path))
        if configured["custom_genres"][0]["id"] != custom_id:
            raise RuntimeError("Mock genre configuration did not persist")

        schedule = scheduler.create(scheduler.ScheduleConfig(
            target_profile, total_hours=6, research_minutes=20, break_minutes=15,
            approved_search_calls=64), db_path, now=schedule_now[0])
        main_schedule_id = schedule["id"]
        schedule_started_mono = monotonic_fn()
        # Model an interrupted second profile through the production recovery API.
        recovery = scheduler.create(scheduler.ScheduleConfig(
            recovery_profile, total_hours=6, research_minutes=20, break_minutes=15,
            approved_search_calls=4), db_path, now=schedule_now[0])
        recovered = scheduler.recover_interrupted(recovery_profile, db_path, now=schedule_now[0])
        if not recovered or recovered[0]["state"] != "paused":
            raise RuntimeError("Production interrupted-session recovery was not verified")
        event("recovery_verified", profile_id=recovery_profile, state=recovered[0]["state"])

        controller = AutopilotController(db_path)
        config = AutopilotConfig(target_profile, target=5, max_search_calls=1, provider_id="disabled")
        guard = ResourceGuard(min_sample_interval_seconds=10)
        sampler = resource_sampler or sample_resources
        total_seconds = duration_hours * 3600
        interval = 300
        step_count = total_seconds // interval
        short_sleep = short_duration_seconds / step_count if mode == "short" else 0
        for tick in range(step_count + 1):
            if mode == "short":
                schedule_now[0] = virtual_start + timedelta(seconds=min(tick * interval, total_seconds))
            else:
                schedule_now[0] = datetime.now(timezone.utc)
            current = scheduler.get(main_schedule_id, target_profile, db_path, now=schedule_now[0])
            phase = current["phase"]
            if phase in transitions:
                if not metrics or metrics[-1].get("phase") != phase:
                    transitions[phase] += 1
                    event("phase_transition", phase=phase, state=current["state"])
            if current["state"] == "running" and phase == "research":
                cycle = current["elapsed_seconds"] // ((20 + 15) * 60)
                if cycle not in research_phase_runs:
                    before = calls
                    result = scheduler.run_autopilot_block(
                        main_schedule_id, target_profile, controller, config, collector, db_path,
                        resource_guard=guard, resource_sampler=sampler, now=lambda: schedule_now[0])
                    research_phase_runs.add(cycle)
                    if calls > before:
                        saved = scheduler.get(main_schedule_id, target_profile, db_path, now=schedule_now[0])
                        prior = json.loads(saved.get("checkpoint_json") or "{}")
                        videos = list(prior.get("processed_video_ids", []))
                        if result.run:
                            videos.extend(str(row.get("video_id")) for row in result.run.candidates
                                          if row.get("video_id"))
                        scheduler.checkpoint(main_schedule_id, target_profile, {
                            **prior, "processed_video_ids": list(dict.fromkeys(videos))[-10000:],
                            "last_video_id": videos[-1] if videos else "", "query_index": calls,
                            "phase": phase, "uncertain_request": bool(saved.get("search_in_flight")),
                        }, db_path, now=schedule_now[0])
                    event("autopilot_block", cycle=cycle, state=result.state,
                          mock_calls=calls - before)
                    if result.state != "complete":
                        event("autopilot_stopped", state=result.state,
                              error_type="safe_pause_or_incomplete_block")
                        raise RuntimeError("The scheduled Autopilot block did not complete; no retry was issued")
            if mode == "short" and tick == 1 and current["state"] == "running":
                paused = scheduler.pause(main_schedule_id, target_profile, db_path, now=schedule_now[0])
                resumed = scheduler.resume(main_schedule_id, target_profile, db_path, now=schedule_now[0])
                event("pause_resume", paused_state=paused["state"], resumed_state=resumed["state"])

            try:
                snapshot = sampler()
            except Exception:
                snapshot = ResourceSnapshot(monotonic_fn())
            current = scheduler.get(main_schedule_id, target_profile, db_path, now=schedule_now[0])
            try:
                with store.connect(db_path) as db:
                    db.execute("SELECT 1").fetchone()
            except Exception:
                db_errors += 1
            sampled_mono = monotonic_fn()
            if (mode == "wallclock" and last_sample_mono is not None and
                    sampled_mono - last_sample_mono > interval + 60):
                missed_intervals += 1
            last_sample_mono = sampled_mono
            elapsed = sampled_mono - started_mono
            row = _resource_row(snapshot, datetime.now(timezone.utc), elapsed,
                                current, calls, db_errors, exceptions)
            metrics.append(row)
            persist_report("RUNNING")
            if mode == "short" and tick < step_count:
                sleep_fn(short_sleep)
            elif mode == "wallclock" and tick < step_count:
                remaining = total_seconds - max(0.0, monotonic_fn() - schedule_started_mono)
                if remaining <= 0:
                    break
                sleep_fn(min(interval, remaining))

        schedule_now[0] = (virtual_start + timedelta(seconds=total_seconds) if mode == "short"
                           else datetime.now(timezone.utc))
        external_attempts = blocked_network.attempts
        final_schedule = scheduler.get(main_schedule_id, target_profile, db_path, now=schedule_now[0])
        with store.connect(db_path) as integrity:
            integrity_value = integrity.execute("PRAGMA integrity_check").fetchone()[0]
        restored = store.profile_genre_config(store.get_profile(target_profile, db_path))
        genre_preserved = (restored["custom_genres"][0]["id"] == custom_id and
                           restored["subgenres"] == ["ethereal dream pop"] and
                           store.list_candidates(recovery_profile, path=db_path) == [])
        profile_isolation = (target_profile != recovery_profile and genre_preserved and
                             bool(store.list_candidates(target_profile, path=db_path)))
        mock_only_in_research = all(item.get("event") != "mock_search" or item.get("phase") == "research"
                                    for item in events)
        checkpoint_ok = bool(json.loads(final_schedule.get("checkpoint_json") or "{}").get("processed_video_ids"))
        phases_ok = transitions["research"] >= 1 and transitions["break"] >= 1 and calls > 0
        quota_ok = calls <= int(final_schedule["approved_search_calls"])
        elapsed = monotonic_fn() - started_mono
        active_schedule_elapsed = (monotonic_fn() - schedule_started_mono
                                   if schedule_started_mono is not None else 0.0)
        if mode == "short":
            passed = (phases_ok and quota_ok and external_attempts == 0 and integrity_value == "ok"
                      and genre_preserved and checkpoint_ok and db_errors == 0 and exceptions == 0
                      and profile_isolation and mock_only_in_research and len(metrics) == step_count + 1
                      and missed_intervals == 0
                      and final_schedule["state"] == "completed")
            final_verdict = "SHORT_QA_PASS" if passed else "SHORT_QA_FAIL"
        else:
            passed = (active_schedule_elapsed >= 21600
                      and int(final_schedule.get("elapsed_seconds", 0)) >= 21600
                      and phases_ok and quota_ok and external_attempts == 0
                      and integrity_value == "ok" and genre_preserved and checkpoint_ok
                      and profile_isolation and mock_only_in_research
                      and len(metrics) == step_count + 1 and missed_intervals == 0
                      and db_errors == 0 and exceptions == 0 and final_schedule["state"] == "completed")
            final_verdict = "PASS" if passed else "FAIL"
        final_payload = {
            "active_schedule_wall_clock_seconds": round(active_schedule_elapsed, 3),
            "database_integrity": integrity_value,
            "genre_configuration_preserved": genre_preserved,
            "profile_isolation_verified": profile_isolation,
            "mock_search_only_in_research": mock_only_in_research,
            "missed_sample_intervals": missed_intervals,
            "checkpoint_verified": checkpoint_ok,
            "final_schedule_state": final_schedule["state"],
            "schedule_search_calls_used": final_schedule["search_calls_used"],
            "quota_not_exceeded": quota_ok,
        }
        event("final_checks", **final_payload)
    except KeyboardInterrupt:
        final_verdict, caught_error = "INTERRUPTED", "KeyboardInterrupt"
        exceptions += 1
        event("interrupted", reason="operator_interrupt")
    except Exception as exc:
        final_verdict, caught_error = "FAIL", type(exc).__name__
        exceptions += 1
        if isinstance(exc, sqlite3.Error):
            db_errors += 1
        event("exception", error_type=caught_error)
    finally:
        external_attempts = blocked_network.attempts
        blocked_network.__exit__(None, None, None)
        persist_report(final_verdict)
        # Re-read the atomic report before the mode can claim success.
        try:
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            saved["database_integrity"] = locals().get("integrity_value", "not_checked")
            saved["genre_configuration_preserved"] = bool(locals().get("genre_preserved", False))
            saved["checkpoint_verified"] = bool(locals().get("checkpoint_ok", False))
            saved["external_network_attempts"] = external_attempts
            saved["result"] = final_verdict
            saved["final_verdict"] = final_verdict
            saved["pass_criteria"]["database_integrity_ok"] = saved["database_integrity"] == "ok"
            saved["pass_criteria"]["checkpoint_and_genre_preserved"] = (
                saved["checkpoint_verified"] and saved["genre_configuration_preserved"])
            saved["pass_criteria"]["profile_isolation"] = bool(locals().get("profile_isolation", False))
            saved["pass_criteria"]["mock_search_only_in_research"] = bool(
                locals().get("mock_only_in_research", False))
            saved["pass_criteria"]["no_missing_sample_intervals"] = (
                saved["missed_sample_intervals"] == 0 and
                saved["sample_count"] == saved["expected_sample_count"])
            if "final_schedule" in locals():
                saved["final_schedule_state"] = final_schedule.get("state")
                saved["schedule_search_calls_used"] = final_schedule.get("search_calls_used")
                saved["quota_not_exceeded"] = bool(locals().get("quota_ok", False))
                saved["pass_criteria"]["schedule_completed"] = final_schedule.get("state") == "completed"
                saved["pass_criteria"]["search_quota_not_exceeded"] = bool(locals().get("quota_ok", False))
            saved["profile_isolation_verified"] = bool(locals().get("profile_isolation", False))
            saved["mock_search_only_in_research"] = bool(locals().get("mock_only_in_research", False))
            _atomic_write(report_path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
            checked = json.loads(report_path.read_text(encoding="utf-8"))
            if checked.get("run_id") != run_id or checked.get("result") != final_verdict:
                final_verdict = "FAIL"
        except Exception:
            final_verdict = "FAIL"
        exit_code = 0 if final_verdict in {"PASS", "SHORT_QA_PASS"} else 2
    return exit_code
