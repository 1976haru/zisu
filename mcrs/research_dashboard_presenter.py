"""Read-only operational view model for one-click research.

The scheduler, Autopilot, and VPH state machines remain authoritative.  This
module only maps their persisted state into the seven states shown to users.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import genres, store

USER_STATES = (
    "READY", "RUNNING", "BREAK", "WAITING_SAMPLE_2", "BLOCKED", "ERROR", "COMPLETE",
)

STAGE_KEYS = (
    "setup", "auth", "search", "genre_evaluation", "target_selection",
    "sample_1", "sample_2", "vph", "final_result",
)


def _utc_now(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Dashboard time must include a timezone.")
    return value.astimezone(timezone.utc)


def _remaining(next_run_utc, now):
    if not next_run_utc:
        return None
    due = datetime.fromisoformat(str(next_run_utc).replace("Z", "+00:00")).astimezone(timezone.utc)
    return max(0, int((due - now).total_seconds()))


def _state(snapshot):
    intent, schedule, job = snapshot.get("intent"), snapshot.get("schedule"), snapshot.get("job")
    if not snapshot.get("session_consistent", True):
        return "BLOCKED"
    if job and job.get("state") == "FAILED":
        return "ERROR"
    if intent and intent.get("status") == "blocked":
        return "BLOCKED"
    if job and job.get("state") == "REVIEW":
        return "BLOCKED"
    if schedule and schedule.get("search_in_flight"):
        return "BLOCKED"
    if schedule and schedule.get("state") == "paused" and not str(schedule.get("pause_reason", "")).startswith("system_"):
        return "BLOCKED"
    if (intent and intent.get("status") == "completed" and schedule and
            schedule.get("state") == "completed" and job and job.get("state") == "COMPLETED" and
            snapshot.get("second_sample_count", 0) > 0 and
            snapshot.get("vph_valid_count", 0) > 0):
        return "COMPLETE"
    if job and job.get("state") == "WAITING_SECOND":
        return "WAITING_SAMPLE_2"
    if schedule and schedule.get("state") == "running" and schedule.get("phase") == "break":
        return "BREAK"
    if ((intent and intent.get("status") in {"claimed", "started"}) or
            (schedule and schedule.get("state") in {"running", "scheduled"}) or
            (job and job.get("state") in {"COLLECTING_FIRST", "COLLECTING_SECOND"})):
        return "RUNNING"
    return "READY"


def present(profile_id, path=None, *, now=None, auth_ready=None):
    """Return a safe, read-only dashboard model for exactly one profile."""
    current = _utc_now(now)
    snapshot = store.research_dashboard_snapshot(profile_id, path)
    profile = snapshot.get("profile") or {}
    intent, schedule, job = snapshot.get("intent"), snapshot.get("schedule"), snapshot.get("job")
    state = _state(snapshot)
    if auth_ready is None:
        auth_ready=bool(profile.get("google_account_id") or profile.get("connected_email"))
    elapsed = int((schedule or {}).get("elapsed_seconds", 0) or 0)
    total = int((schedule or {}).get("total_minutes", 0) or 0) * 60
    schedule_progress = min(100, int(elapsed * 100 / total)) if total else 0
    counts = snapshot.get("session_candidate_counts", snapshot.get("candidate_counts", {}))
    cumulative = snapshot.get("cumulative_candidate_counts", {})
    first_count = int(snapshot.get("first_sample_count", 0))
    second_count = int(snapshot.get("second_sample_count", 0))
    search_calls=int(snapshot.get("search_calls_current",0))
    session_candidates=int(counts.get("total",0))
    searched = search_calls > 0
    stages = {
        "setup": bool(profile),
        "auth": bool(auth_ready),
        "search": searched,
        "genre_evaluation": session_candidates > 0 or bool(job and job.get("targets") and searched),
        "target_selection": bool(job and job.get("targets")),
        "sample_1": first_count > 0,
        "sample_2": second_count > 0,
        "vph": int(snapshot.get("vph_valid_count", 0)) > 0,
        "final_result": state == "COMPLETE",
    }
    completed = sum(bool(stages[key]) for key in STAGE_KEYS)
    last_success = next((key for key in reversed(STAGE_KEYS) if stages[key]), "setup" if profile else "none")
    error_code = ""
    reason = ""
    if not snapshot.get("session_consistent", True):
        error_code=str(snapshot.get("session_error") or "session_link_mismatch")
        reason="저장된 요청·일정·VPH 작업 식별자가 서로 맞지 않아 안전하게 결합하지 않았습니다."
    elif intent and intent.get("status") == "blocked":
        error_code, reason = str(intent.get("reason") or "blocked"), str(intent.get("reason") or "")
    elif job and job.get("state") in {"FAILED", "REVIEW"}:
        error_code, reason = "vph_" + job["state"].lower(), str(job.get("message") or "")
    elif schedule and schedule.get("search_in_flight"):
        error_code, reason = "uncertain_external_request", "외부 요청 완료 여부가 불확실하여 자동 재전송하지 않습니다."
    elif schedule and schedule.get("state") == "paused":
        error_code, reason = str(schedule.get("pause_reason") or "paused"), str(schedule.get("pause_reason") or "")
    next_action = {
        "READY": "설정을 확인한 뒤 시작하세요.",
        "RUNNING": "자동 연구가 진행 중입니다.",
        "BREAK": "다음 연구 구간까지 기다립니다.",
        "WAITING_SAMPLE_2": "두 번째 표본 예정 시각까지 기다립니다.",
        "BLOCKED": "표시된 차단 사유를 확인하세요.",
        "ERROR": "오류 단계와 안전한 오류 코드를 확인하세요.",
        "COMPLETE": "최종 결과를 확인하세요.",
    }[state]
    return {
        "state": state,
        "profile_id": profile_id,
        "channel": profile.get("name", ""),
        "market": profile.get("market", ""),
        "genre": profile.get("genre", ""),
        "request_id": (intent or {}).get("request_id", ""),
        "schedule_id": (schedule or {}).get("id", ""),
        "job_id": (job or {}).get("id", ""),
        "session_consistent": bool(snapshot.get("session_consistent", True)),
        "schedule_progress_percent": schedule_progress,
        "stage_progress_completed": completed,
        "stage_progress_total": len(STAGE_KEYS),
        "stages": stages,
        "current_phase": (schedule or {}).get("phase", "ready"),
        "next_run_utc": (job or {}).get("next_run_utc", ""),
        "remaining_seconds": _remaining((job or {}).get("next_run_utc", ""), current),
        "last_success_stage": last_success,
        "error_stage": ("checkpoint" if error_code.endswith("mismatch") or error_code.endswith("invalid") or
                        error_code.startswith("completed_") else
                        "external_request" if error_code == "uncertain_external_request" else
                        "vph" if error_code.startswith("vph_") else (schedule or {}).get("phase", "")),
        "error_code": error_code,
        "stop_reason": reason,
        "auto_resume": state in {"RUNNING", "BREAK", "WAITING_SAMPLE_2"} or (state == "BLOCKED" and error_code.startswith("system_")),
        "next_action": next_action,
        "can_start": bool(
            snapshot.get("session_consistent", True) and
            state == "READY" and
            (not intent or intent.get("status") in {"completed", "cancelled"}) and
            (not schedule or schedule.get("state") in {"completed", "cancelled"}) and
            (not job or job.get("state") in {"COMPLETED", "CANCELLED"})
        ),
        "can_pause": snapshot.get("session_consistent", True) and state in {"RUNNING", "WAITING_SAMPLE_2"} and bool(
            (schedule and schedule.get("state") == "running" and not schedule.get("search_in_flight")) or
            (job and job.get("state") in {"READY", "COLLECTING_FIRST", "WAITING_SECOND", "COLLECTING_SECOND"})
        ),
        "can_resume": bool(snapshot.get("session_consistent", True) and (
            (schedule and schedule.get("state") == "paused" and not schedule.get("search_in_flight")) or
            (job and job.get("state") == "PAUSED")
        )),
        "can_stop": bool(snapshot.get("session_consistent", True) and (
            (schedule and schedule.get("state") in {"scheduled", "running", "paused"} and
             not schedule.get("search_in_flight")) or
            (job and job.get("state") in {"READY", "COLLECTING_FIRST", "WAITING_SECOND", "COLLECTING_SECOND", "PAUSED"})
        )),
        "counts": {
            "candidates": int(counts.get("total", 0)),
            "search_calls": search_calls,
            "fit": int(counts.get(genres.FIT, 0)),
            "review": int(counts.get(genres.REVIEW, 0)),
            "exclude": int(counts.get(genres.EXCLUDE, 0)),
            "observing": int(snapshot.get("observing_count", 0)),
            "sample_1": first_count,
            "sample_2": second_count,
            "vph_a": int(snapshot.get("vph_a_count", 0)),
            "vph_b": int(snapshot.get("vph_b_count", 0)),
            "unknown": int(snapshot.get("vph_unknown_count", 0)),
            "vph_review": int(snapshot.get("vph_review_count", 0)),
            "cumulative_candidates": int(cumulative.get("total", 0)),
            "cumulative_observing": int(snapshot.get("cumulative_observing_count", 0)),
            "cumulative_second_sample": int(snapshot.get("cumulative_second_sample_count", 0)),
            "cumulative_vph_a": int(snapshot.get("cumulative_vph_a_count", 0)),
            "cumulative_vph_b": int(snapshot.get("cumulative_vph_b_count", 0)),
        },
        "insufficient_measurement": bool(job) and not (
            state == "COMPLETE" and second_count > 0 and int(snapshot.get("vph_valid_count", 0)) > 0),
    }
