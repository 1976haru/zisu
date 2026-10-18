"""Profile-scoped research/rest schedule state; it never starts playback or API calls."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from . import store
from .resource_guard import ResourceGuard, ResourceSnapshot, sample_resources

TOTAL_HOURS = (2, 4, 6, 8)
RESEARCH_MINUTES = (20, 30, 40)
BREAK_MINUTES = (15, 30, 40)
MAX_SEARCH_CALLS = 1000
_FORBIDDEN_KEY = re.compile(r"(?i)(secret|password|credential|api.?key|access.?token|refresh.?token|authorization)")


class ScheduleBusy(RuntimeError):
    """The profile already has a scheduled or unfinished research session."""


class UnresolvedSearchCall(RuntimeError):
    """A request may have reached the API; never repeat it automatically."""


class ResearchWindowClosed(RuntimeError):
    """The scheduled research interval ended; no new request was made."""


@dataclass(frozen=True)
class ScheduleConfig:
    profile_id: str
    total_hours: int = 6
    research_minutes: int = 30
    break_minutes: int = 30
    start_at: datetime | None = None
    approved_search_calls: int = 12

    def validate(self):
        if not self.profile_id:
            raise ValueError("Choose a research profile.")
        if self.total_hours not in TOTAL_HOURS:
            raise ValueError("Total time must be 2, 4, 6, or 8 hours.")
        if self.research_minutes not in RESEARCH_MINUTES:
            raise ValueError("Research interval must be 20, 30, or 40 minutes.")
        if self.break_minutes not in BREAK_MINUTES:
            raise ValueError("Break interval must be 15, 30, or 40 minutes.")
        if isinstance(self.approved_search_calls, bool) or not isinstance(self.approved_search_calls, int):
            raise ValueError("Search allowance must be a whole number.")
        if not 1 <= self.approved_search_calls <= MAX_SEARCH_CALLS:
            raise ValueError(f"Search allowance must be between 1 and {MAX_SEARCH_CALLS} calls.")
        if self.start_at is not None and (self.start_at.tzinfo is None or self.start_at.utcoffset() is None):
            raise ValueError("Start time must include a timezone.")


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock value must include a timezone.")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def create(config: ScheduleConfig, path=None, *, now: datetime | None = None) -> dict:
    """Persist a schedule with immutable profile/genre/channel scope."""
    config.validate()
    store.initialize(path)
    now_utc = _utc(now)
    profile = store.get_profile(config.profile_id, path)
    if not profile:
        raise ValueError("The selected profile no longer exists.")
    start = _utc(config.start_at or now_utc)
    if (start - now_utc).total_seconds() < -60:
        raise ValueError("Start time cannot be more than one minute in the past.")
    related = store.normalize_related_genres(
        profile["genre"], profile.get("related_genres_json", "[]"))
    session_id = uuid.uuid4().hex
    active = start <= now_utc
    with store.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM research_schedules WHERE profile_id=? "
                      "AND state IN ('scheduled','running','paused') LIMIT 1",
                      (config.profile_id,)).fetchone():
            raise ScheduleBusy("This profile already has an unfinished research schedule.")
        db.execute("""INSERT INTO research_schedules(
          id,profile_id,market,core_genre,related_genres_json,channel_id,start_at_utc,
          total_minutes,research_minutes,break_minutes,approved_search_calls,state,phase,
          running_since_utc)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (session_id, config.profile_id, profile["market"], profile["genre"],
           json.dumps(related, ensure_ascii=False), profile.get("youtube_channel_id", ""),
           _stamp(start), config.total_hours * 60, config.research_minutes,
           config.break_minutes, config.approved_search_calls,
           "running" if active else "scheduled", "research" if active else "waiting",
           _stamp(start) if active else ""))
    return get(session_id, config.profile_id, path, now=now_utc)


def get(session_id: str, profile_id: str, path=None, *, now: datetime | None = None) -> dict | None:
    """Load only within the owning profile and advance its wall-clock phase."""
    with store.connect(path) as db:
        row = db.execute("SELECT * FROM research_schedules WHERE id=? AND profile_id=?",
                         (session_id, profile_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        current = _utc(now)
        old_state, old_phase = item["state"], item["phase"]
        state, phase = old_state, old_phase
        elapsed = int(item["elapsed_seconds"])
        running_since = item["running_since_utc"]
        if state == "scheduled":
            start = _parse(item["start_at_utc"])
            if current < start:
                item["phase"] = "waiting"
                item["elapsed_seconds"] = 0
                item["remaining_seconds"] = max(0, int((start - current).total_seconds()))
                return item
            state = "running"
            elapsed = 0
            running_since = _stamp(start)
        elif state == "running" and item["running_since_utc"]:
            elapsed += max(0, int((current - _parse(running_since)).total_seconds()))
        elif state == "running" and old_state == "scheduled":
            elapsed = max(0, int((current - _parse(running_since)).total_seconds()))
        total = int(item["total_minutes"]) * 60
        elapsed = min(elapsed, total)
        if elapsed >= total:
            state, phase = "completed", "complete"
            elapsed = total
            running_since = ""
        elif state == "running":
            cycle = (int(item["research_minutes"]) + int(item["break_minutes"])) * 60
            phase = "research" if elapsed % cycle < int(item["research_minutes"]) * 60 else "break"
        item.update(state=state, phase=phase, elapsed_seconds=elapsed,
                    remaining_seconds=max(0, total - elapsed),
                    search_calls_remaining=max(0, int(item["approved_search_calls"]) - int(item["search_calls_used"])))
        if state != old_state or phase != old_phase:
            # Re-anchor the monotonic wall-clock accounting at phase changes;
            # otherwise a later read would add already-persisted elapsed time twice.
            if state == "running" and old_state == "running":
                running_since = _stamp(current)
            db.execute("UPDATE research_schedules SET state=?,phase=?,elapsed_seconds=?,"
                       "running_since_utc=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?",
                       (state, phase, elapsed if state != "running" or old_state == "running" else 0,
                        running_since, session_id, profile_id))
        return item


def list_active(profile_id: str, path=None, *, now: datetime | None = None) -> list[dict]:
    with store.connect(path) as db:
        ids = [row[0] for row in db.execute("SELECT id FROM research_schedules WHERE profile_id=? "
               "AND state IN ('scheduled','running','paused') ORDER BY created_at DESC", (profile_id,))]
    return [item for session_id in ids if (item := get(session_id, profile_id, path, now=now))]


def pause(session_id: str, profile_id: str, path=None, *, now: datetime | None = None) -> dict:
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if not item or item["state"] != "running":
        raise ValueError("Only a running research session can be paused.")
    with store.connect(path) as db:
        db.execute("UPDATE research_schedules SET state='paused',elapsed_seconds=?,running_since_utc='',"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=? AND state='running'",
                   (item["elapsed_seconds"], session_id, profile_id))
    return get(session_id, profile_id, path, now=current)


def resume(session_id: str, profile_id: str, path=None, *, now: datetime | None = None) -> dict:
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if not item or item["state"] != "paused":
        raise ValueError("Only a paused research session can be resumed.")
    if item["search_in_flight"]:
        raise UnresolvedSearchCall("A search may have reached YouTube; it must be reconciled before resuming.")
    with store.connect(path) as db:
        db.execute("UPDATE research_schedules SET state='running',phase='research',pause_reason='',running_since_utc=?,"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=? AND state='paused'",
                   (_stamp(current), session_id, profile_id))
    return get(session_id, profile_id, path, now=current)


def cancel(session_id: str, profile_id: str, path=None, *, now: datetime | None = None) -> dict:
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if not item or item["state"] not in {"scheduled", "running", "paused"}:
        raise ValueError("This research schedule is no longer active.")
    with store.connect(path) as db:
        db.execute("UPDATE research_schedules SET state='cancelled',phase='cancelled',elapsed_seconds=?,"
                   "running_since_utc='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?",
                   (item["elapsed_seconds"], session_id, profile_id))
    return get(session_id, profile_id, path, now=current)


def checkpoint(session_id: str, profile_id: str, values: dict, path=None, *, now: datetime | None = None) -> dict:
    """Persist non-secret scheduler progress; reject tokens and arbitrary payloads."""
    if not isinstance(values, dict) or any(not isinstance(k, str) or _FORBIDDEN_KEY.search(k) for k in values):
        raise ValueError("Checkpoint must contain non-secret, allow-listed progress fields.")
    allowed = {"processed_video_ids", "last_video_id", "query_index", "phase",
               "autopilot_session_id", "notes_draft_id", "uncertain_request"}
    if set(values) - allowed:
        raise ValueError("Checkpoint contains unsupported fields.")
    for key, value in values.items():
        if key in {"processed_video_ids"}:
            if not isinstance(value, list) or len(value) > 10000 or any(not isinstance(v, str) or len(v) > 128 for v in value):
                raise ValueError("Processed video IDs are invalid.")
        elif key == "query_index":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Query position must be a non-negative whole number.")
        elif key == "uncertain_request":
            if not isinstance(value, bool):
                raise ValueError("Request uncertainty must be true or false.")
        elif not isinstance(value, str) or len(value) > 128:
            raise ValueError("Checkpoint text is invalid.")
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with store.connect(path) as db:
        result = db.execute("UPDATE research_schedules SET checkpoint_json=?,updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=? AND profile_id=?", (payload, session_id, profile_id))
        if result.rowcount != 1:
            raise ValueError("Research schedule does not match the selected profile.")
    return get(session_id, profile_id, path, now=now)


def request_resource_pause(session_id: str, profile_id: str, reason: str, path=None) -> dict:
    """Finish an in-flight call safely, then prevent admission of new work."""
    allowed = {"system_cpu_sustained", "system_ram_low", "system_memory_high",
               "process_memory_high", "process_memory_growth"}
    if reason not in allowed:
        raise ValueError("Unsupported resource pause reason.")
    with store.connect(path) as db:
        row = db.execute("SELECT elapsed_seconds,state,search_in_flight FROM research_schedules "
                         "WHERE id=? AND profile_id=?", (session_id, profile_id)).fetchone()
        if not row or row["state"] not in {"running", "scheduled"}:
            raise ValueError("Research schedule does not match the active profile.")
        state = "running" if row["search_in_flight"] else "paused"
        db.execute("UPDATE research_schedules SET state=?,phase='resource_pause',pause_reason=?,"
                   "running_since_utc=CASE WHEN search_in_flight=1 THEN running_since_utc ELSE '' END,"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?",
                   (state, reason, session_id, profile_id))
    return get(session_id, profile_id, path)


def claim_search_call(session_id: str, profile_id: str, path=None, *, now: datetime | None = None,
                      resource_guard: ResourceGuard | None = None,
                      resource_snapshot: ResourceSnapshot | None = None) -> int:
    """Atomically reserve quota before an API call; a crash never refunds it."""
    clock = now if callable(now) else (lambda: now)
    item = get(session_id, profile_id, path, now=clock())
    if not item or item["state"] != "running" or item["phase"] != "research":
        raise ValueError("Search is allowed only during the active research interval.")
    if resource_guard is not None:
        decision = resource_guard.evaluate(resource_snapshot or ResourceSnapshot(_utc(clock()).timestamp()))
        if not decision.allow_new_work:
            reason = decision.reasons[0]
            request_resource_pause(session_id, profile_id, reason, path)
            raise ResourceWorkDeferred(decision.status, decision.reasons)
    with store.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT search_calls_used,approved_search_calls,search_in_flight,state,phase "
                         "FROM research_schedules WHERE id=? AND profile_id=?", (session_id, profile_id)).fetchone()
        if not row:
            raise ValueError("Research schedule does not match the selected profile.")
        if row["search_in_flight"]:
            raise UnresolvedSearchCall("The previous search result is uncertain; it will not be repeated.")
        if row["state"] != "running" or row["phase"] != "research":
            raise ValueError("The research interval ended before a new search could be reserved.")
        if row["search_calls_used"] >= row["approved_search_calls"]:
            raise ValueError("The approved search allowance has been reached.")
        calls = int(row["search_calls_used"]) + 1
        db.execute("UPDATE research_schedules SET search_calls_used=?,search_in_flight=1,"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?", (calls, session_id, profile_id))
        return calls


def finish_search_call(session_id: str, profile_id: str, path=None) -> None:
    with store.connect(path) as db:
        result = db.execute("UPDATE research_schedules SET search_in_flight=0,"
                            "state=CASE WHEN pause_reason<>'' THEN 'paused' ELSE state END,"
                            "running_since_utc=CASE WHEN pause_reason<>'' THEN '' ELSE running_since_utc END,"
                            "updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=? AND profile_id=? AND search_in_flight=1", (session_id, profile_id))
        if result.rowcount != 1:
            raise ValueError("There is no reserved search call to finish.")


class ResourceWorkDeferred(RuntimeError):
    def __init__(self, status: str, reasons: tuple[str, ...]):
        super().__init__(status)
        self.status = status
        self.reasons = reasons


def recover_interrupted(profile_id: str, path=None, *, now: datetime | None = None) -> list[dict]:
    """Convert crashed running rows to paused checkpoints; never resume requests."""
    current = _utc(now)
    with store.connect(path) as db:
        ids = [row[0] for row in db.execute("SELECT id FROM research_schedules WHERE profile_id=? "
               "AND state='running'", (profile_id,))]
    recovered = []
    for session_id in ids:
        item = get(session_id, profile_id, path, now=current)
        if not item or item["state"] == "completed":
            continue
        with store.connect(path) as db:
            db.execute("UPDATE research_schedules SET state='paused',phase='interrupted',elapsed_seconds=?,"
                       "running_since_utc='',updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=? AND state='running'",
                       (item["elapsed_seconds"], session_id, profile_id))
        recovered.append(get(session_id, profile_id, path, now=current))
    return recovered


def acknowledge_uncertain_search_consumed(session_id: str, profile_id: str, path=None) -> dict:
    """Resolve a crash-window request conservatively without issuing it again."""
    with store.connect(path) as db:
        result = db.execute("UPDATE research_schedules SET search_in_flight=0,updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=? AND profile_id=? AND state='paused' AND search_in_flight=1",
                            (session_id, profile_id))
        if result.rowcount != 1:
            raise ValueError("No uncertain search request is awaiting confirmation.")
    return get(session_id, profile_id, path)


def run_autopilot_block(session_id: str, profile_id: str, controller, config, collector,
                        path=None, *, provider=None, on_event=None,
                        resource_guard: ResourceGuard | None = None,
                        resource_sampler=sample_resources, now: datetime | None = None):
    """Run the existing bounded Autopilot only within this schedule's research phase.

    The schedule's atomic allowance is reserved before each collector call and
    consumed even on an uncertain network failure. Existing Autopilot settings,
    Gemini approval and cost ceiling remain authoritative.
    """
    clock = now if callable(now) else (lambda: now)
    item = get(session_id, profile_id, path, now=clock())
    if not item:
        raise ValueError("Research schedule does not match the selected profile.")
    if config.profile_id != profile_id:
        raise ValueError("Autopilot profile and schedule profile must match.")
    if config.max_search_calls > item["approved_search_calls"]:
        raise ValueError("Autopilot search allowance cannot exceed the schedule's approved allowance.")
    if item["state"] not in {"running", "scheduled"}:
        raise ValueError("Resume the saved schedule before continuing Autopilot.")

    guard = resource_guard or ResourceGuard()
    def can_continue():
        current = get(session_id, profile_id, path, now=clock())
        if not current or current["state"] != "running" or current["phase"] != "research":
            return False
        decision = guard.check(resource_sampler)
        if not decision.allow_new_work:
            request_resource_pause(session_id, profile_id, decision.reasons[0], path)
            return False
        return True

    def scheduled_collector(term, market, limit, page_token=None):
        claim_search_call(session_id, profile_id, path, now=clock())
        try:
            try:
                import inspect
                params = inspect.signature(collector).parameters
            except (TypeError, ValueError):
                params = {}
            if "page_token" in params or len(params) >= 4:
                return collector(term, market, limit, page_token)
            return collector(term, market, limit)
        finally:
            finish_search_call(session_id, profile_id, path)

    result = controller.run(config, scheduled_collector, provider=provider,
                            on_event=on_event, continue_check=can_continue,
                            resource_check=can_continue)
    checkpoint(session_id, profile_id, {
        "autopilot_session_id": result.session_id,
        "processed_video_ids": [str(row.get("video_id", "")) for row in
                                (result.run.candidates if result.run else ()) if row.get("video_id")],
        "uncertain_request": False,
    }, path, now=clock())
    return result
