"""Profile-scoped research/rest schedule state; it never starts playback or API calls."""
from __future__ import annotations

import json
import math
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import store
from .resource_guard import ResourceGuard, ResourceSnapshot, sample_resources

TOTAL_HOURS = (2, 4, 6, 8, 10, 12)
MIN_TOTAL_HOURS = 1
MAX_TOTAL_HOURS = 24
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 180
RESEARCH_MINUTES = (5, 10, 15, 20, 30, 40, 60)
BREAK_MINUTES = (5, 10, 15, 20, 30, 40, 60)
MAX_SEARCH_CALLS = 1000
_FORBIDDEN_KEY = re.compile(r"(?i)(secret|password|credential|api.?key|access.?token|refresh.?token|authorization)")


class ScheduleBusy(RuntimeError):
    """The profile already has a scheduled or unfinished research session."""


class DuplicateAutoStartRequest(RuntimeError):
    """A one-shot automatic start request has already been persisted."""


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
    schedule_mode: str = "fixed"
    research_min_minutes: int | None = None
    research_max_minutes: int | None = None
    break_min_minutes: int | None = None
    break_max_minutes: int | None = None
    auto_start_request_id: str = ""

    def validate(self):
        if not self.profile_id:
            raise ValueError("Choose a research profile.")
        if isinstance(self.total_hours,bool) or not isinstance(self.total_hours,int) or not MIN_TOTAL_HOURS <= self.total_hours <= MAX_TOTAL_HOURS:
            raise ValueError(f"Total time must be between {MIN_TOTAL_HOURS} and {MAX_TOTAL_HOURS} hours.")
        if self.schedule_mode not in {"fixed","random"}:
            raise ValueError("Schedule mode must be fixed or random.")
        for value in (self.research_minutes,self.break_minutes):
            if isinstance(value,bool) or not isinstance(value,int) or not MIN_INTERVAL_MINUTES <= value <= MAX_INTERVAL_MINUTES:
                raise ValueError(f"Research and break durations must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes.")
        limits=(self.research_min_minutes,self.research_max_minutes,self.break_min_minutes,self.break_max_minutes)
        defaults=(self.research_minutes,self.research_minutes,self.break_minutes,self.break_minutes)
        resolved=tuple(value if value is not None else default for value,default in zip(limits,defaults))
        for value in resolved:
            if isinstance(value,bool) or not isinstance(value,int) or not MIN_INTERVAL_MINUTES <= value <= MAX_INTERVAL_MINUTES:
                raise ValueError(f"Research and break durations must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes.")
        if resolved[0]>resolved[1] or resolved[2]>resolved[3]:
            raise ValueError("Minimum schedule duration cannot exceed its maximum.")
        if isinstance(self.approved_search_calls, bool) or not isinstance(self.approved_search_calls, int):
            raise ValueError("Search allowance must be a whole number.")
        if not 1 <= self.approved_search_calls <= MAX_SEARCH_CALLS:
            raise ValueError(f"Search allowance must be between 1 and {MAX_SEARCH_CALLS} calls.")
        if self.auto_start_request_id and (len(self.auto_start_request_id) > 80 or
                not all(char.isalnum() or char in "-_" for char in self.auto_start_request_id)):
            raise ValueError("Automatic start request ID is invalid.")
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


def _phase_at(item: dict, elapsed: int) -> tuple[str, int]:
    """Return phase and time remaining in phase, accounting for one saved extension."""
    checkpoint=json.loads(item.get("checkpoint_json") or "{}")
    plan=checkpoint.get("schedule_plan",[])
    if plan and int(item.get("research_extension_seconds",0) or 0)==0:
        for segment in plan:
            start=int(segment["offset_start_seconds"]); end=int(segment["offset_end_seconds"])
            if start <= elapsed < end:
                return segment["phase"],end-elapsed
        return "complete",0
    research = int(item["research_minutes"]) * 60
    rest = int(item["break_minutes"]) * 60
    cycle = research + rest
    extension = int(item.get("research_extension_seconds", 0) or 0)
    raw_extension_cycle = item.get("research_extension_cycle", -1)
    extension_cycle = int(-1 if raw_extension_cycle is None else raw_extension_cycle)
    if extension > 0 and extension_cycle >= 0:
        start = extension_cycle * cycle
        research_end = start + research + extension
        break_end = research_end + rest
        if elapsed < start:
            remaining = elapsed % cycle
            return ("research", research - remaining) if remaining < research else ("break", cycle - remaining)
        if elapsed < research_end:
            return "research", research_end - elapsed
        if elapsed < break_end:
            return "break", break_end - elapsed
        elapsed -= extension
    offset = elapsed % cycle
    return ("research", research - offset) if offset < research else ("break", cycle - offset)


def _build_schedule_plan(config, start, *, total_seconds=None, offset_start=0):
    total=config.total_hours*3600 if total_seconds is None else int(total_seconds)
    final_offset=int(offset_start)+total
    research_min=config.research_min_minutes if config.research_min_minutes is not None else config.research_minutes
    research_max=config.research_max_minutes if config.research_max_minutes is not None else config.research_minutes
    break_min=config.break_min_minutes if config.break_min_minutes is not None else config.break_minutes
    break_max=config.break_max_minutes if config.break_max_minutes is not None else config.break_minutes
    if config.schedule_mode=="fixed":
        research_min=research_max=config.research_minutes
        break_min=break_max=config.break_minutes
    chooser=random.SystemRandom().randint if config.schedule_mode=="random" else lambda low,high:low
    plan=[]; elapsed=int(offset_start); phase="research"
    while elapsed < final_offset:
        low,high=(research_min,research_max) if phase=="research" else (break_min,break_max)
        duration=min(final_offset-elapsed,chooser(low,high)*60)
        end=elapsed+duration
        plan.append({"phase":phase,"duration_seconds":duration,
            "offset_start_seconds":elapsed,"offset_end_seconds":end,
            "planned_start_utc":_stamp(start+timedelta(seconds=elapsed-int(offset_start))),
            "planned_end_utc":_stamp(start+timedelta(seconds=end-int(offset_start)))})
        elapsed=end
        phase="break" if phase=="research" else "research"
    return plan


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
    genre_config = store.profile_genre_config(profile)
    related = store.normalize_related_genres(
        profile["genre"], profile.get("related_genres_json", "[]"), genre_config["custom_genres"])
    session_id = uuid.uuid4().hex
    active = start <= now_utc
    schedule_plan=_build_schedule_plan(config,start)
    checkpoint={"schedule_plan":schedule_plan,"schedule_mode":config.schedule_mode,
        "research_min_minutes":config.research_min_minutes if config.research_min_minutes is not None else config.research_minutes,
        "research_max_minutes":config.research_max_minutes if config.research_max_minutes is not None else config.research_minutes,
        "break_min_minutes":config.break_min_minutes if config.break_min_minutes is not None else config.break_minutes,
        "break_max_minutes":config.break_max_minutes if config.break_max_minutes is not None else config.break_minutes}
    with store.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if config.auto_start_request_id and db.execute(
                "SELECT 1 FROM research_schedules WHERE auto_start_request_id=? LIMIT 1",
                (config.auto_start_request_id,)).fetchone():
            raise DuplicateAutoStartRequest("This one-shot automatic start request was already used.")
        if db.execute("SELECT 1 FROM research_schedules WHERE profile_id=? "
                      "AND state IN ('scheduled','running','paused') LIMIT 1",
                      (config.profile_id,)).fetchone():
            raise ScheduleBusy("This profile already has an unfinished research schedule.")
        db.execute("""INSERT INTO research_schedules(
          id,profile_id,market,core_genre,related_genres_json,genre_config_snapshot_json,channel_id,start_at_utc,
          total_minutes,research_minutes,break_minutes,approved_search_calls,state,phase,
          running_since_utc,checkpoint_json,auto_start_request_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (session_id, config.profile_id, profile["market"], profile["genre"],
           json.dumps(related, ensure_ascii=False), json.dumps(genre_config,ensure_ascii=False,sort_keys=True),
           profile.get("youtube_channel_id", ""),
           _stamp(start), config.total_hours * 60, config.research_minutes,
           config.break_minutes, config.approved_search_calls,
           "running" if active else "scheduled", "research" if active else "waiting",
           _stamp(start) if active else "",json.dumps(checkpoint,ensure_ascii=False,sort_keys=True),
           config.auto_start_request_id))
    return get(session_id, config.profile_id, path, now=now_utc)


def get_auto_start_request(request_id: str, path=None) -> dict | None:
    """Return a durable one-shot schedule by its non-secret request ID."""
    if not request_id:
        return None
    with store.connect(path) as db:
        row = db.execute("SELECT * FROM research_schedules WHERE auto_start_request_id=?",
                         (request_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["checkpoint"] = json.loads(item.get("checkpoint_json") or "{}")
    return item


def list_auto_start_schedules(profile_id: str, path=None) -> list[dict]:
    """List only one-shot schedules owned by a single profile."""
    with store.connect(path) as db:
        rows = db.execute("SELECT * FROM research_schedules WHERE profile_id=? "
                          "AND auto_start_request_id<>'' ORDER BY created_at DESC",
                          (profile_id,)).fetchall()
    items=[]
    for row in rows:
        item=dict(row)
        item["checkpoint"] = json.loads(item.get("checkpoint_json") or "{}")
        items.append(item)
    return items


def automatic_resume_block_reason(schedule: dict, profile: dict, settings: dict,
                                 preferences: dict | None = None) -> str:
    """Explain why a persisted one-shot schedule is or is not safe to resume silently."""
    if not schedule or not profile or not schedule.get("auto_start_request_id"):
        return "not_an_automatic_one_shot"
    if schedule.get("profile_id") != profile.get("id"):
        return "profile_mismatch"
    if schedule.get("state") not in {"paused", "scheduled"} or schedule.get("search_in_flight"):
        return "not_safely_paused"
    pause_reason=str(schedule.get("pause_reason", ""))
    if schedule.get("state") == "paused" and pause_reason not in {"", "process_interrupted"} and not pause_reason.startswith("system_"):
        return "pause_reason_requires_review"
    if (schedule.get("market") != profile.get("market") or
            schedule.get("core_genre") != profile.get("genre") or
            schedule.get("channel_id", "") != profile.get("youtube_channel_id", "")):
        return "profile_settings_changed"
    if not settings or settings.get("provider_id") != "disabled" or settings.get("paid_external_approved"):
        return "approval_scope_changed"
    if isinstance(settings.get("max_search_calls"), bool) or not 1 <= int(settings.get("max_search_calls", 0)) <= 3:
        return "search_budget_changed"
    if int(schedule.get("approved_search_calls", 0)) > 3:
        return "schedule_budget_exceeds_one_shot_limit"
    if preferences is not None:
        if (int(preferences.get("total_hours", 0)) * 60 != int(schedule.get("total_minutes", -1)) or
                int(preferences.get("research_minutes", 0)) != int(schedule.get("research_minutes", -1)) or
                int(preferences.get("break_minutes", 0)) != int(schedule.get("break_minutes", -1))):
            return "schedule_preferences_changed"
        checkpoint=json.loads(schedule.get("checkpoint_json") or "{}")
        for key in ("schedule_mode","research_min_minutes","research_max_minutes",
                    "break_min_minutes","break_max_minutes"):
            if key in preferences and preferences[key]!=checkpoint.get(key):
                return "schedule_preferences_changed"
    return ""


def recover_interrupted_schedule(session_id: str, profile_id: str, path=None,
                                 *, now: datetime | None = None) -> dict | None:
    """Recover exactly one persisted schedule; leave every unrelated schedule untouched."""
    current=_utc(now)
    with store.connect(path) as db:
        row=db.execute("SELECT id FROM research_schedules WHERE id=? AND profile_id=? AND state='running'",
                       (session_id,profile_id)).fetchone()
        if not row: return get(session_id,profile_id,path,now=current)
        changed=db.execute("UPDATE research_schedules SET state='paused',phase='interrupted',"
            "pause_reason='process_interrupted',running_since_utc='',updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND profile_id=? AND state='running'",(session_id,profile_id)).rowcount
    return get(session_id,profile_id,path,now=current) if changed else None


def get(session_id: str, profile_id: str, path=None, *, now: datetime | None = None) -> dict | None:
    """Load only within the owning profile and advance its wall-clock phase."""
    with store.connect(path) as db:
        row = db.execute("SELECT * FROM research_schedules WHERE id=? AND profile_id=?",
                         (session_id, profile_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        checkpoint=json.loads(item.get("checkpoint_json") or "{}")
        item.update(schedule_mode=checkpoint.get("schedule_mode","fixed"),
            schedule_plan=checkpoint.get("schedule_plan",[]),
            research_min_minutes=checkpoint.get("research_min_minutes",item["research_minutes"]),
            research_max_minutes=checkpoint.get("research_max_minutes",item["research_minutes"]),
            break_min_minutes=checkpoint.get("break_min_minutes",item["break_minutes"]),
            break_max_minutes=checkpoint.get("break_max_minutes",item["break_minutes"]))
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
                item["phase_remaining_seconds"] = item["remaining_seconds"]
                return item
            state = "running"
            elapsed = 0
            running_since = _stamp(start)
        elif state == "running" and item["running_since_utc"]:
            elapsed += max(0, int((current - _parse(running_since)).total_seconds()))
        elif state == "running" and old_state == "scheduled":
            elapsed = max(0, int((current - _parse(running_since)).total_seconds()))
        phase_remaining = 0
        extension = int(item.get("research_extension_seconds", 0) or 0)
        total = int(item["total_minutes"]) * 60 + extension
        elapsed = min(elapsed, total)
        if elapsed >= total:
            state, phase = "completed", "complete"
            elapsed = total
            running_since = ""
        elif state == "running":
            phase, phase_remaining = _phase_at(item, elapsed)
        else:
            phase_remaining = 0
        item.update(state=state, phase=phase, elapsed_seconds=elapsed,
                    remaining_seconds=max(0, total - elapsed),
                    phase_remaining_seconds=phase_remaining,
                    search_calls_remaining=max(0, int(item["approved_search_calls"]) - int(item["search_calls_used"])))
        if state == "running":
            # Persist the active process checkpoint; a restart must not count
            # time spent with the app closed as completed research schedule.
            running_since = _stamp(current)
        if state != old_state or phase != old_phase or state == "running":
            db.execute("UPDATE research_schedules SET state=?,phase=?,elapsed_seconds=?,"
                       "running_since_utc=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?",
                       (state, phase, elapsed if state != "running" or old_state == "running" else 0,
                        running_since, session_id, profile_id))
        return item


def update_next_cycle(session_id: str, profile_id: str, settings: dict, path=None, *, now=None) -> dict:
    """Apply edited durations only at the next research-cycle boundary."""
    current=_utc(now)
    item=get(session_id,profile_id,path,now=current)
    if not item or item["state"]!="running" or item["search_in_flight"]:
        raise ValueError("Schedule settings can only change safely while running between API requests.")
    required={"total_hours","research_minutes","break_minutes","schedule_mode",
        "research_min_minutes","research_max_minutes","break_min_minutes","break_max_minutes"}
    if not isinstance(settings,dict) or set(settings)!=required:
        raise ValueError("A complete schedule configuration is required.")
    config=ScheduleConfig(profile_id,total_hours=settings["total_hours"],
        research_minutes=settings["research_minutes"],break_minutes=settings["break_minutes"],
        schedule_mode=settings["schedule_mode"],research_min_minutes=settings["research_min_minutes"],
        research_max_minutes=settings["research_max_minutes"],break_min_minutes=settings["break_min_minutes"],
        break_max_minutes=settings["break_max_minutes"],approved_search_calls=max(1,item["approved_search_calls"]))
    config.validate()
    elapsed=int(item["elapsed_seconds"]); plan=list(item.get("schedule_plan",[]))
    boundary=next((int(segment["offset_start_seconds"]) for segment in plan
                   if segment["phase"]=="research" and int(segment["offset_start_seconds"])>elapsed),None)
    if boundary is None:
        raise ValueError("No future research cycle remains in this schedule.")
    new_total=config.total_hours*3600
    if boundary>=new_total:
        raise ValueError("New total duration ends before the next safe cycle boundary.")
    start_segment=next(segment for segment in plan if int(segment["offset_start_seconds"])==boundary)
    new_start=_parse(start_segment["planned_start_utc"])
    prefix=[segment for segment in plan if int(segment["offset_end_seconds"])<=boundary]
    tail=_build_schedule_plan(config,new_start,total_seconds=new_total-boundary,offset_start=boundary)
    checkpoint=json.loads(item.get("checkpoint_json") or "{}")
    checkpoint.update(schedule_plan=prefix+tail,schedule_mode=config.schedule_mode,**{
        "research_min_minutes":settings["research_min_minutes"],"research_max_minutes":settings["research_max_minutes"],
        "break_min_minutes":settings["break_min_minutes"],"break_max_minutes":settings["break_max_minutes"]})
    with store.connect(path) as db:
        changed=db.execute("UPDATE research_schedules SET total_minutes=?,research_minutes=?,break_minutes=?,"
            "checkpoint_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=? AND state='running' AND search_in_flight=0",
            (config.total_hours*60,config.research_minutes,config.break_minutes,
             json.dumps(checkpoint,ensure_ascii=False,sort_keys=True),session_id,profile_id)).rowcount
    if changed!=1:raise ValueError("Schedule changed while applying the next-cycle settings.")
    return get(session_id,profile_id,path,now=current)


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


def resume(session_id: str, profile_id: str, path=None, *, now: datetime | None = None,
           external_recovery_confirmed: bool = False) -> dict:
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if not item or item["state"] != "paused":
        raise ValueError("Only a paused research session can be resumed.")
    if item["search_in_flight"]:
        raise UnresolvedSearchCall("A search may have reached YouTube; it must be reconciled before resuming.")
    confirmation_reasons = {"auth_required", "permission_denied", "quota_exhausted",
                            "quota_local_limit", "rate_limited"}
    if item.get("pause_reason") in confirmation_reasons and not external_recovery_confirmed:
        raise ValueError("External authentication or quota state must be confirmed before resume.")
    profile = store.get_profile(profile_id, path)
    if not profile:
        raise ValueError("The scheduled profile no longer exists.")
    saved_config = store._normalize_genre_config(item.get("genre_config_snapshot_json") or "{}")
    current_config = store.profile_genre_config(profile)
    saved_related = store.normalize_related_genres(item["core_genre"], item.get("related_genres_json", "[]"),
                                                   saved_config["custom_genres"])
    current_related = store.normalize_related_genres(profile["genre"], profile.get("related_genres_json", "[]"),
                                                     current_config["custom_genres"])
    if (item["market"], item["core_genre"], saved_related, saved_config) != (
            profile["market"], profile["genre"], current_related, current_config):
        raise ValueError("Saved research scope differs from current profile genre settings; review or start a new session.")
    phase, _ = _phase_at(item, item["elapsed_seconds"])
    checkpoint=json.loads(item.get("checkpoint_json") or "{}")
    plan=checkpoint.get("schedule_plan",[])
    if plan:
        elapsed=int(item["elapsed_seconds"])
        current_segment=next((segment for segment in plan
            if int(segment["offset_start_seconds"])<=elapsed<int(segment["offset_end_seconds"])),None)
        if current_segment:
            expected=_parse(current_segment["planned_start_utc"])+timedelta(
                seconds=elapsed-int(current_segment["offset_start_seconds"]))
        else:
            expected=_parse(item["start_at_utc"])+timedelta(seconds=elapsed)
        shift=current-expected
        for segment in plan:
            if int(segment["offset_end_seconds"])>elapsed:
                segment["planned_start_utc"]=_stamp(_parse(segment["planned_start_utc"])+shift)
                segment["planned_end_utc"]=_stamp(_parse(segment["planned_end_utc"])+shift)
        checkpoint["schedule_plan"]=plan
    with store.connect(path) as db:
        db.execute("UPDATE research_schedules SET state='running',phase=?,pause_reason='',running_since_utc=?,checkpoint_json=?,"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=? AND state='paused'",
                   (phase, _stamp(current),json.dumps(checkpoint,ensure_ascii=False,sort_keys=True),session_id, profile_id))
    return get(session_id, profile_id, path, now=current)


def extend_current_research(session_id: str, profile_id: str, path=None, *,
                            extension_seconds: int = 600, now: datetime | None = None) -> dict:
    """Persist one user-approved ten-minute extension before a research block ends."""
    if isinstance(extension_seconds, bool) or extension_seconds != 600:
        raise ValueError("A single research extension is fixed to ten minutes.")
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if not item or item["state"] != "running" or item["phase"] != "research":
        raise ValueError("Only an active research phase can be extended.")
    if item.get("schedule_mode")=="random":
        raise ValueError("Random schedules use their saved cycle plan; use next-cycle settings to change later intervals.")
    if item.get("research_extension_used"):
        raise ValueError("This schedule has already used its one-time extension.")
    if int(item.get("phase_remaining_seconds", 0)) > 60:
        raise ValueError("A research phase can only be extended during its final minute.")
    if item.get("search_in_flight"):
        raise UnresolvedSearchCall("Wait for the active search to finish before extending.")
    cycle = (int(item["research_minutes"]) + int(item["break_minutes"])) * 60
    cycle_index = int(item["elapsed_seconds"]) // cycle
    with store.connect(path) as db:
        changed = db.execute("UPDATE research_schedules SET research_extension_cycle=?,"
            "research_extension_seconds=600,research_extension_used=1,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND profile_id=? AND research_extension_used=0 AND state='running'",
            (cycle_index, session_id, profile_id)).rowcount
        if changed != 1:
            raise ValueError("The schedule changed before the extension was saved.")
    return get(session_id, profile_id, path, now=current)


def resume_after_resource_recovery(session_id: str, profile_id: str, path=None, *,
                                   resource_guard: ResourceGuard,
                                   resource_sampler=sample_resources,
                                   expected_autopilot_settings: dict | None = None,
                                   allow_new_autopilot_session: bool = False,
                                   now: datetime | None = None) -> dict:
    """Fail-closed recovery for a same-process resource pause.

    Never auto-resumes crash/interruption or uncertain API requests. A saved
    Autopilot approval must still match exactly; a brand-new Autopilot session
    is allowed only when the caller still holds this process's fresh approval.
    """
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if item and item["search_in_flight"]:
        raise UnresolvedSearchCall("A search result is uncertain; it will not be repeated automatically.")
    if not item or item["state"] != "paused" or not str(item.get("pause_reason", "")).startswith((
            "system_", "process_")):
        raise ValueError("Only a resource-paused schedule can be automatically resumed.")
    decision = resource_guard.check(resource_sampler)
    if not decision.allow_new_work:
        raise ResourceWorkDeferred(decision.status, decision.reasons)
    if item["search_calls_remaining"] <= 0:
        raise ValueError("The approved search allowance is exhausted.")
    saved = {}
    checkpoint_data = json.loads(item.get("checkpoint_json") or "{}")
    autopilot_session_id = checkpoint_data.get("autopilot_session_id")
    if autopilot_session_id:
        saved = store.get_autopilot_settings(profile_id, autopilot_session_id, path)
        if not saved or expected_autopilot_settings is None or saved != expected_autopilot_settings:
            raise ValueError("The saved approval or profile settings changed; review before resuming.")
    elif not allow_new_autopilot_session or expected_autopilot_settings is None:
        raise ValueError("A fresh in-process approval is required before resuming this schedule.")
    return resume(session_id, profile_id, path, now=current)


def complete(session_id: str, profile_id: str, path=None, *, now: datetime | None = None) -> dict:
    """Mark an early-finished schedule complete without changing its history."""
    current = _utc(now)
    item = get(session_id, profile_id, path, now=current)
    if not item or item["state"] not in {"running", "paused", "scheduled"}:
        raise ValueError("This research schedule is no longer active.")
    if item["search_in_flight"]:
        raise UnresolvedSearchCall("An external search is unresolved; the schedule cannot be completed yet.")
    with store.connect(path) as db:
        db.execute("UPDATE research_schedules SET state='completed',phase='complete',running_since_utc='',"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?",
                   (session_id, profile_id))
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
               "autopilot_session_id", "notes_draft_id", "uncertain_request",
               "approved_provider_id", "approved_model", "approved_target",
               "approved_max_search_calls", "paid_external_approved",
               "cost_estimate_usd", "cost_limit_usd", "vph_job_id",
               "vph_observation_slots", "vph_discovery_complete", "schedule_plan",
               "schedule_mode", "research_min_minutes", "research_max_minutes",
               "break_min_minutes", "break_max_minutes", "auto_start_request_id",
               "auto_start_intent", "one_shot_settings"}
    if set(values) - allowed:
        raise ValueError("Checkpoint contains unsupported fields.")
    for key, value in values.items():
        if key in {"processed_video_ids"}:
            if not isinstance(value, list) or len(value) > 10000 or any(not isinstance(v, str) or len(v) > 128 for v in value):
                raise ValueError("Processed video IDs are invalid.")
        elif key == "query_index":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Query position must be a non-negative whole number.")
        elif key in {"uncertain_request", "paid_external_approved"}:
            if not isinstance(value, bool):
                raise ValueError("Request uncertainty must be true or false.")
        elif key == "vph_discovery_complete":
            if not isinstance(value, bool):
                raise ValueError("VPH discovery state must be true or false.")
        elif key == "auto_start_request_id":
            if not isinstance(value,str) or len(value)>80 or not all(ch.isalnum() or ch in "-_" for ch in value):
                raise ValueError("Automatic start request ID is invalid.")
        elif key == "auto_start_intent":
            if value is not True:
                raise ValueError("Automatic start intent must be true.")
        elif key == "one_shot_settings":
            store._validate_auto_start_settings(value)
        elif key == "vph_observation_slots":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
                raise ValueError("VPH observation slots must be between 1 and 20.")
        elif key == "schedule_plan":
            if not isinstance(value,list) or len(value)>2000:
                raise ValueError("Schedule plan is invalid.")
            last_end=0
            for segment in value:
                if not isinstance(segment,dict) or segment.get("phase") not in {"research","break"}:
                    raise ValueError("Schedule plan segment is invalid.")
                start,end=segment.get("offset_start_seconds"),segment.get("offset_end_seconds")
                if (isinstance(start,bool) or not isinstance(start,int) or isinstance(end,bool)
                    or not isinstance(end,int) or start!=last_end or end<=start or end-start>10800
                    or not isinstance(segment.get("planned_start_utc"),str)
                    or not isinstance(segment.get("planned_end_utc"),str)):
                    raise ValueError("Schedule plan timing is invalid.")
                last_end=end
        elif key == "schedule_mode":
            if value not in {"fixed","random"}:
                raise ValueError("Schedule mode is invalid.")
        elif key in {"research_min_minutes","research_max_minutes","break_min_minutes","break_max_minutes"}:
            if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=180:
                raise ValueError("Schedule range is invalid.")
        elif key in {"approved_target", "approved_max_search_calls"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Approved research limits must be positive whole numbers.")
            if key == "approved_max_search_calls" and value > MAX_SEARCH_CALLS:
                raise ValueError("Approved search allowance is above the supported maximum.")
        elif key in {"cost_estimate_usd", "cost_limit_usd"}:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError("Approved cost values must be finite non-negative numbers.")
        elif key in {"approved_provider_id", "approved_model"}:
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("Approved provider details are invalid.")
            if key == "approved_provider_id" and value not in {"disabled", "gemini", "openai", "ollama"}:
                raise ValueError("Approved provider is not supported.")
        elif key == "vph_job_id":
            if not isinstance(value, str) or len(value) > 128:
                raise ValueError("VPH checkpoint identifier is invalid.")
        elif not isinstance(value, str) or len(value) > 128:
            raise ValueError("Checkpoint text is invalid.")
    with store.connect(path) as db:
        row=db.execute("SELECT checkpoint_json FROM research_schedules WHERE id=? AND profile_id=?",
                       (session_id,profile_id)).fetchone()
        if not row:
            raise ValueError("Research schedule does not match the selected profile.")
        merged=json.loads(row[0] or "{}")
        merged.update(values)
        payload = json.dumps(merged, ensure_ascii=False, sort_keys=True, allow_nan=False)
        result = db.execute("UPDATE research_schedules SET checkpoint_json=?,updated_at=CURRENT_TIMESTAMP "
                            "WHERE id=? AND profile_id=?", (payload, session_id, profile_id))
        if result.rowcount != 1:
            raise ValueError("Research schedule does not match the selected profile.")
    return get(session_id, profile_id, path, now=now)


def request_resource_pause(session_id: str, profile_id: str, reason: str, path=None, *,
                           now: datetime | None = None) -> dict:
    """Finish an in-flight call safely, then prevent admission of new work."""
    allowed = {"system_cpu_sustained", "system_ram_low", "system_memory_high",
               "process_memory_high", "process_memory_growth"}
    if reason not in allowed:
        raise ValueError("Unsupported resource pause reason.")
    with store.connect(path) as db:
        row = db.execute("SELECT elapsed_seconds,state,search_in_flight FROM research_schedules "
                         "WHERE id=? AND profile_id=?", (session_id, profile_id)).fetchone()
        if not row or row["state"] not in {"running", "scheduled", "paused"}:
            raise ValueError("Research schedule does not match the active profile.")
        state = "running" if row["search_in_flight"] else "paused"
        db.execute("UPDATE research_schedules SET state=?,phase='resource_pause',pause_reason=?,"
                   "running_since_utc=CASE WHEN search_in_flight=1 THEN running_since_utc ELSE '' END,"
                   "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=?",
                   (state, reason, session_id, profile_id))
    return get(session_id, profile_id, path, now=now)


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
            request_resource_pause(session_id, profile_id, reason, path, now=clock())
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


def pause_for_api_error(session_id: str, profile_id: str, error, path=None) -> dict:
    """Pause only the owning profile and retain uncertain requests for review."""
    code = getattr(error, "code", "unknown_network")
    if error.__class__.__name__ == "OAuthError" and code == "unknown":
        code = "access_denied"
    allowed = {
        "auth_required": "auth_required", "permission_denied": "permission_denied",
        "access_denied": "auth_required", "scope_mismatch": "permission_denied",
        "api_permission": "permission_denied", "token_storage": "auth_required",
        "quota_exhausted": "quota_exhausted", "quota_local_limit": "quota_local_limit",
        "rate_limited": "rate_limited", "server_error": "server_error",
        "dns_failure": "network_wait", "network_timeout": "network_wait",
        "network_offline": "network_wait", "network_changed": "network_wait",
        "connection_failed": "network_wait", "invalid_response": "uncertain_request",
    }
    reason = allowed.get(code, "uncertain_request")
    default_uncertain = code not in {"access_denied", "scope_mismatch", "api_permission", "token_storage"}
    uncertain = bool(getattr(error, "request_uncertain", default_uncertain))
    with store.connect(path) as db:
        result = db.execute(
            "UPDATE research_schedules SET state='paused',phase=?,pause_reason=?,"
            "search_in_flight=CASE WHEN ? THEN search_in_flight ELSE 0 END,"
            "running_since_utc='',updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND profile_id=? AND state IN ('running','paused')",
            (reason, reason, int(uncertain), session_id, profile_id))
        if result.rowcount != 1:
            raise ValueError("Research schedule does not match the selected profile.")
    return get(session_id, profile_id, path)


def recover_interrupted(profile_id: str, path=None, *, now: datetime | None = None) -> list[dict]:
    """Convert crashed running rows to paused checkpoints; never resume requests."""
    with store.connect(path) as db:
        rows = db.execute("SELECT id FROM research_schedules WHERE profile_id=? AND state='running'",
                          (profile_id,)).fetchall()
        recovered_ids = []
        for row in rows:
            session_id = row["id"]
            changed = db.execute("UPDATE research_schedules SET state='paused',phase='interrupted',"
                "pause_reason='process_interrupted',running_since_utc='',updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND profile_id=? AND state='running'", (session_id, profile_id)).rowcount
            if changed:
                recovered_ids.append(session_id)
    return [item for session_id in recovered_ids
            if (item := get(session_id, profile_id, path, now=now))]


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
            request_resource_pause(session_id, profile_id, decision.reasons[0], path, now=clock())
            return False
        return True

    def scheduled_collector(term, market, limit, page_token=None, order="relevance", quota_context=None):
        claim_search_call(session_id, profile_id, path, now=clock())
        try:
            try:
                import inspect
                params = inspect.signature(collector).parameters
            except (TypeError, ValueError):
                params = {}
            accepts_order = "order" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            accepts_page = "page_token" in params or len(params) >= 4
            accepts_quota = "quota_context" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            extra = {"order": order} if accepts_order else {}
            if accepts_quota:
                extra["quota_context"] = {**(quota_context or {}), "schedule_id": session_id}
            if accepts_page:
                result = collector(term, market, limit, page_token, **extra)
            elif extra:
                result = collector(term, market, limit, **extra)
            else:
                result = collector(term, market, limit)
        except Exception as exc:
            try:
                from .youtube_api import APIError
                if isinstance(exc, APIError):
                    pause_for_api_error(session_id, profile_id, exc, path)
                else:
                    with store.connect(path) as db:
                        db.execute("UPDATE research_schedules SET state='paused',phase='uncertain_request',"
                            "pause_reason='uncertain_request',running_since_utc='',"
                            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND profile_id=? AND search_in_flight=1",
                            (session_id, profile_id))
            except Exception:
                # Preserve the original failure; a persisted in-flight reservation
                # remains fail-closed if diagnostics could not be written.
                pass
            raise
        else:
            finish_search_call(session_id, profile_id, path)
            return result

    def schedule_event(event):
        if event.get("stage") in {"session", "resume"} and event.get("session_id"):
            current = get(session_id, profile_id, path, now=clock())
            progress = json.loads(current.get("checkpoint_json") or "{}") if current else {}
            progress.update(autopilot_session_id=event["session_id"], uncertain_request=False)
            checkpoint(session_id, profile_id, progress, path, now=clock())
        if on_event:
            on_event(event)

    result = controller.run(config, scheduled_collector, provider=provider,
                            on_event=schedule_event, continue_check=can_continue,
                            resource_check=can_continue)
    latest = get(session_id, profile_id, path, now=clock())
    progress = json.loads(latest.get("checkpoint_json") or "{}") if latest else {}
    previous_ids = progress.get("processed_video_ids", [])
    new_ids = [str(row.get("video_id", "")) for row in
               (result.run.candidates if result.run else ()) if row.get("video_id")]
    progress.update({
        "autopilot_session_id": result.session_id,
        "processed_video_ids": list(dict.fromkeys(previous_ids + new_ids))[-10000:],
        "uncertain_request": bool(latest and latest["search_in_flight"]),
    })
    checkpoint(session_id, profile_id, progress, path, now=clock())
    return result
