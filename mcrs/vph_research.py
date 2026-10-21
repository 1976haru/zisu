"""Bounded, profile-scoped one-click discovery and VPH sampling workflow."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from . import genres, store, vph
from .youtube_api import APIError, lookup_videos

DEFAULT_SEARCH_CALLS = 3
MAX_SEARCH_CALLS = 20
MAX_TARGETS = 20
SECOND_SAMPLE_DELAY_SECONDS = 3900
ACTIVE_STATES = {"READY", "COLLECTING_FIRST", "WAITING_SECOND", "COLLECTING_SECOND", "PAUSED"}


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _read(row):
    if not row:
        return None
    item = dict(row)
    item["used_queries"] = json.loads(item.pop("used_queries_json") or "[]")
    item["targets"] = json.loads(item.pop("targets_json") or "[]")
    return item


def get(job_id, profile_id, path=None):
    store.initialize(path)
    with store.connect(path) as db:
        return _read(db.execute("SELECT * FROM vph_research_jobs WHERE id=? AND profile_id=?",
                                (job_id, profile_id)).fetchone())


def latest_active(profile_id, path=None):
    store.initialize(path)
    with store.connect(path) as db:
        row = db.execute("SELECT * FROM vph_research_jobs WHERE profile_id=? AND state IN "
                         "('READY','COLLECTING_FIRST','WAITING_SECOND','COLLECTING_SECOND','PAUSED') "
                         "ORDER BY created_at DESC LIMIT 1", (profile_id,)).fetchone()
    return _read(row)


def latest_job(profile_id, path=None):
    store.initialize(path)
    with store.connect(path) as db:
        row = db.execute("SELECT * FROM vph_research_jobs WHERE profile_id=? "
                         "ORDER BY created_at DESC,id DESC LIMIT 1", (profile_id,)).fetchone()
    return _read(row)


def active_jobs(path=None):
    store.initialize(path)
    with store.connect(path) as db:
        rows = db.execute("SELECT * FROM vph_research_jobs WHERE state IN "
                          "('READY','COLLECTING_FIRST','WAITING_SECOND','COLLECTING_SECOND','PAUSED') "
                          "ORDER BY created_at").fetchall()
    return [_read(row) for row in rows]


def mark_interrupted(job_id, profile_id, path=None):
    job = get(job_id, profile_id, path)
    if not job or job["state"] not in {"COLLECTING_FIRST", "COLLECTING_SECOND"}:
        return job
    _update(job_id, profile_id, path, state="REVIEW",
            message="앱 종료 중 요청 결과가 불확실할 수 있어 자동 재요청하지 않고 검토 대기")
    return get(job_id, profile_id, path)


def start(profile_id, path=None, *, search_limit=DEFAULT_SEARCH_CALLS,
          observation_slots=None, schedule_id=""):
    if isinstance(search_limit, bool) or not isinstance(search_limit, int) or not 0 <= search_limit <= MAX_SEARCH_CALLS:
        raise ValueError(f"Search calls must be between 0 and {MAX_SEARCH_CALLS}.")
    store.initialize(path)
    profile=store.get_profile(profile_id, path)
    if not profile:
        raise ValueError("Research profile was not found.")
    observation_slots=profile.get("vph_observation_slots",MAX_TARGETS) if observation_slots is None else observation_slots
    if isinstance(observation_slots,bool) or not isinstance(observation_slots,int) or not 1 <= observation_slots <= MAX_TARGETS:
        raise ValueError("VPH observation slots must be between 1 and 20.")
    if not isinstance(schedule_id,str) or len(schedule_id)>128:
        raise ValueError("Research schedule identifier is invalid.")
    job_id = uuid.uuid4().hex
    with store.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT id FROM vph_research_jobs WHERE profile_id=? AND state IN "
                            "('READY','COLLECTING_FIRST','WAITING_SECOND','COLLECTING_SECOND','PAUSED') LIMIT 1",
                            (profile_id,)).fetchone()
        if active:
            raise RuntimeError("This profile already has an active VPH research session.")
        db.execute("INSERT INTO vph_research_jobs(id,profile_id,state,search_limit,observation_slots,schedule_id) "
                   "VALUES(?,?,?,?,?,?)", (job_id, profile_id, "READY", search_limit,observation_slots,schedule_id))
    return get(job_id, profile_id, path)


def _update(job_id, profile_id, path, **values):
    allowed = {"state", "search_calls_used", "used_queries_json", "targets_json", "next_run_utc", "message"}
    if set(values) - allowed:
        raise ValueError("Unsupported VPH job checkpoint field.")
    assignments = ",".join(f"{key}=?" for key in values)
    with store.connect(path) as db:
        changed = db.execute(f"UPDATE vph_research_jobs SET {assignments},updated_at=CURRENT_TIMESTAMP "
                             "WHERE id=? AND profile_id=?", (*values.values(), job_id, profile_id)).rowcount
    if changed != 1:
        raise ValueError("VPH research checkpoint was not saved.")


def _pause_checkpoint(job_id, profile_id, path, resume_state, message):
    _update(job_id,profile_id,path,state="PAUSED",message=json.dumps(
        {"resume_state":resume_state,"message":message},ensure_ascii=False))


def _profile_queries(profile, path):
    config = store.profile_genre_config(profile)
    custom = config["custom_genres"]
    languages = config.get("search_languages") or [profile["market"]]
    terms = []
    for language in languages:
        terms.extend(genres.search_terms(profile["genre"], profile["market"], custom, language))
    terms.extend(store.list_profile_search_terms(profile["id"], profile["market"], path))
    # Preserve configured order while preventing query repetition across jobs.
    prior = set()
    with store.connect(path) as db:
        prior.update(row[0].strip().casefold() for row in db.execute(
            "SELECT DISTINCT keyword FROM candidates WHERE profile_id=? AND keyword<>''", (profile["id"],)))
        for row in db.execute("SELECT used_queries_json FROM vph_research_jobs WHERE profile_id=?", (profile["id"],)):
            prior.update(str(term).strip().casefold() for term in json.loads(row[0] or "[]"))
    return list(dict.fromkeys(term.strip() for term in terms if term.strip() and term.strip().casefold() not in prior))


def _query_language(profile, term):
    config=store.profile_genre_config(profile)
    languages=config.get("search_languages") or [profile["market"]]
    custom=config["custom_genres"]
    for language in languages:
        if term in genres.search_terms(profile["genre"],profile["market"],custom,language):
            return {"KR":"ko","JP":"ja","EN":"en"}.get(language.upper(),language.lower())
    return "ko" if profile["market"]=="KR" else "ja"


def _meaningful_targets(profile, rows, path, *, known_ids=None, limit=MAX_TARGETS):
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= MAX_TARGETS:
        raise ValueError("VPH target limit must be between 1 and 20.")
    config = store.profile_genre_config(profile)
    custom = config["custom_genres"]
    related = store.normalize_related_genres(profile["genre"], profile.get("related_genres_json", "[]"), custom)
    selected = {"FIT": [], "REVIEW": []}
    seen = set()
    for row in rows:
        video_id = str(row.get("video_id", ""))
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        old = store.get_candidate(profile["id"], video_id, path)
        if row.get("country") and row.get("country") != profile["market"]:
            continue
        if video_id in (known_ids or set()) or (old and (old.get("genre_decision") == genres.MANUAL_APPROVAL
                                                        or old.get("status") != "미확인")):
            # Existing records are not re-targeted; human review/exclusion is immutable here.
            continue
        verdict = genres.assess(profile["genre"], row.get("video_title", ""),
                                row.get("description", ""), custom)
        related_result = genres.related_classification(profile["genre"], related,
            row.get("video_title", ""), row.get("description", ""), custom)
        evidence = list(verdict.positive_signals)
        if verdict.decision == genres.FIT:
            selected["FIT"].append((verdict.score, row, evidence, related_result,
                                     list(verdict.conflict_signals),verdict.reason,verdict.uncertainty))
        elif verdict.decision == genres.REVIEW and evidence:
            selected["REVIEW"].append((verdict.score, row, evidence, related_result,
                                       list(verdict.conflict_signals),verdict.reason,verdict.uncertainty))
    selected["FIT"].sort(key=lambda value: value[0], reverse=True)
    selected["REVIEW"].sort(key=lambda value: value[0], reverse=True)
    result = []
    # FIT is always a distinct first tier; review-only candidates are explicitly labeled.
    for group, values in (("CORE_FIT", selected["FIT"]), ("GENRE_UNCONFIRMED_REFERENCE", selected["REVIEW"])):
        for score, row, evidence, related, conflicts, reason, uncertainty in values:
            if len(result) >= limit:
                break
            result.append({"video_id": row["video_id"], "group": group,
                "title": row.get("video_title", ""), "channel": row.get("channel_title", ""),
                "score": score, "positive_signals": evidence,
                "conflict_signals": conflicts,
                "genre_reason": reason,
                "uncertainty": uncertainty,
                "related_category": related.get("category", ""),
                "caution": "REVIEW; not promoted to FIT" if group != "CORE_FIT" else ""})
    return result


def _unresolved_target_ids(profile_id, path):
    blocked=set()
    with store.connect(path) as db:
        rows=db.execute("SELECT targets_json FROM vph_research_jobs WHERE profile_id=? AND state='REVIEW'",
                        (profile_id,)).fetchall()
    for row in rows:
        try:blocked.update(item["video_id"] for item in json.loads(row[0] or "[]") if item.get("video_id"))
        except (TypeError,ValueError):continue
    return blocked


def diagnose_candidate(profile, row):
    """Return metadata-rule reasons only; this is not an audio-quality judgment."""
    config = store.profile_genre_config(profile)
    custom = config["custom_genres"]
    title, description = row.get("video_title", ""), row.get("description", "")
    verdict = genres.assess(profile["genre"], title, description, custom)
    genre = genres.GENRES.get(profile["genre"]) or genres.custom_catalog(custom).get(profile["genre"])
    text = (title + "\n" + description[:1500]).casefold()
    missing_context = [] if not genre else [term for term in genre.required_context
        if term.casefold() not in text]
    support = genres._rap_support(profile["genre"], description)
    has_vocal = any(item.startswith("vocal:") for item in support)
    has_rhythm = any(item.startswith("rhythm:") for item in support)
    reasons = []
    if not verdict.positive_signals:
        reasons.append("핵심 장르 긍정 신호 부족")
    if missing_context:
        reasons.append("아침 맥락 근거 부족")
    if profile["genre"] in {"MORNING_CHILL_RAP", "CHILL_RAP"}:
        if not has_vocal: reasons.append("랩/보컬 근거 부족")
        if not has_rhythm: reasons.append("리듬 근거 부족")
    if verdict.conflict_signals:
        reasons.append("다른 장르 또는 제외 신호 충돌")
    return {"decision": verdict.decision, "score": verdict.score,
            "positive_signals": list(verdict.positive_signals),
            "conflict_signals": list(verdict.conflict_signals),
            "missing_context": missing_context, "rap_support": list(support),
            "reasons": reasons, "basis": "YouTube API metadata rules only; no audio listening performed"}


def run_first_phase(job_id, profile_id, key, *, access_token=None, quota_context=None, path=None,
                    searcher=lookup_videos, should_stop=lambda: False, should_pause=lambda: False):
    job = get(job_id, profile_id, path)
    if not job or job["state"] != "READY":
        raise RuntimeError("VPH session is not ready for first collection.")
    profile = store.get_profile(profile_id, path)
    _update(job_id, profile_id, path, state="COLLECTING_FIRST", message="자동 검색 중")
    used, calls = list(job["used_queries"]), int(job["search_calls_used"])
    diagnosis_totals={}
    known_ids = {row["video_id"] for row in store.list_candidates(profile_id, path=path)}
    with store.connect(path) as db:
        known_ids.update(row[0] for row in db.execute(
            "SELECT DISTINCT video_id FROM vph_observations WHERE profile_id=?", (profile_id,)))
    known_ids.update(_unresolved_target_ids(profile_id,path))
    with store.connect(path) as db:
        observed_ids = {row[0] for row in db.execute(
            "SELECT DISTINCT video_id FROM vph_observations WHERE profile_id=?", (profile_id,))}
    blocked_target_ids=observed_ids | _unresolved_target_ids(profile_id,path)
    existing_rows = [row for row in store.list_candidates(profile_id, path=path)
                     if row.get("status") == "미확인"
                     and row.get("genre_decision") in {genres.FIT, genres.REVIEW}
                     and int(row.get("genre_score") or 0) > 0
                     and row["video_id"] not in blocked_target_ids]
    slot_limit=int(job.get("observation_slots",MAX_TARGETS))
    target_by_id = {target["video_id"]: target for target in
                    _meaningful_targets(profile, existing_rows, path, known_ids=set(),limit=slot_limit)}
    try:
        for term in _profile_queries(profile, path):
            if calls >= job["search_limit"]:
                break
            if should_stop():
                _update(job_id, profile_id, path, state="CANCELLED", message="사용자 중지 요청 확인; 저장 자료 보존")
                return get(job_id, profile_id, path)
            if should_pause():
                _pause_checkpoint(job_id,profile_id,path,"READY","검색 경계에서 일시정지됨; 완료된 검색어는 재사용하지 않음")
                return get(job_id,profile_id,path)
            # Persist the in-flight term before external I/O. If the process dies, do not replay it.
            used.append(term)
            _update(job_id, profile_id, path, used_queries_json=json.dumps(used, ensure_ascii=False),
                    search_calls_used=calls + 1, message="후보 평가 중")
            calls += 1
            rows = searcher(key, term, profile["market"], limit=12, access_token=access_token,
                            relevance_language=_query_language(profile,term),
                            quota_context={**(quota_context or {}), "profile_id": profile_id,
                                           "path": str(path or store.db_path()), "session_id": job_id})
            new_rows = [row for row in rows if row.get("video_id") not in known_ids]
            for row in new_rows:
                for reason in diagnose_candidate(profile,row)["reasons"]:
                    diagnosis_totals[reason]=diagnosis_totals.get(reason,0)+1
            for target in _meaningful_targets(profile, new_rows, path, known_ids=known_ids,limit=slot_limit):
                target_by_id.setdefault(target["video_id"], target)
            if new_rows:
                store.save_candidates(profile_id, new_rows, path)
            known_ids.update(row.get("video_id") for row in new_rows if row.get("video_id"))
        if should_stop():
            _update(job_id, profile_id, path, state="CANCELLED", message="사용자 중지 요청 확인; 후보 및 쿼터 기록 보존")
            return get(job_id, profile_id, path)
        if should_pause():
            _pause_checkpoint(job_id,profile_id,path,"READY","첫 표본 요청 전에 일시정지됨; 저장 후보 체크포인트 유지")
            return get(job_id,profile_id,path)
        targets = sorted(target_by_id.values(),
                         key=lambda item: (item["group"] != "CORE_FIT", -item["score"]))[:slot_limit]
        if not targets:
            _update(job_id, profile_id, path, state="REVIEW", targets_json="[]", next_run_utc="",
                    message=("검색 예산 내 Core FIT 또는 핵심 장르 긍정 신호 REVIEW가 없습니다. "
                             "적합도 0점 REVIEW 및 EXCLUDE는 관측하지 않았습니다. 부족 근거: "
                             +( " / ".join(f"{reason} {count}건" for reason,count in diagnosis_totals.items())
                                or "새 검색 결과 없음 또는 검색어 예산 소진")))
            return get(job_id, profile_id, path)
        ids = [item["video_id"] for item in targets]
        if should_stop():
            _update(job_id, profile_id, path, state="CANCELLED", targets_json=json.dumps(targets,ensure_ascii=False),
                    message="첫 표본 요청 전 중지됨; 관측 대상 체크포인트 보존")
            return get(job_id, profile_id, path)
        # Durable intent precedes network I/O so an interrupted/uncertain videos.list
        # cannot be silently repeated by a later one-click session.
        _update(job_id, profile_id, path, targets_json=json.dumps(targets,ensure_ascii=False),
                message="첫 표본 측정 준비 · 대상 체크포인트 저장됨")
        metadata={item["video_id"]:{"research_group":item["group"],"genre_evidence":item["positive_signals"],
            "genre_conflicts":item["conflict_signals"],"genre_reason":item["genre_reason"],
            "review_caution":item["caution"]} for item in targets}
        samples = vph.collect_video_samples(key, profile_id, ids, access_token=access_token,
            quota_context={**(quota_context or {}), "profile_id": profile_id,
                           "path": str(path or store.db_path()), "session_id": job_id}, path=path,
            target_metadata=metadata)
        if should_stop():
            _update(job_id, profile_id, path, state="CANCELLED", message="첫 표본 응답 저장 후 사용자 중지; 추가 요청 없음")
            return get(job_id, profile_id, path)
        histories = {video_id: vph.list_history(profile_id, video_id, path) for video_id in ids}
        due_candidates = []
        for target in targets:
            history = histories[target["video_id"]]
            if history:
                target["first_observation_id"] = history[-1]["later_observation_id"]
                target["first_observed_at_utc"] = history[-1]["observed_at_utc"]
                target["first_status"] = "valid" if history[-1]["view_count"] is not None else "missing"
                due_candidates.append(datetime.fromisoformat(history[-1]["observed_at_utc"].replace("Z", "+00:00"))
                                      + timedelta(seconds=SECOND_SAMPLE_DELAY_SECONDS))
        if not due_candidates:
            _update(job_id, profile_id, path, state="REVIEW", targets_json=json.dumps(targets, ensure_ascii=False),
                    next_run_utc="", message="첫 표본의 API 메타데이터가 누락되어 자동 재시도 없이 검토 대기")
            return get(job_id, profile_id, path)
        _update(job_id, profile_id, path, state="WAITING_SECOND",
                targets_json=json.dumps(targets, ensure_ascii=False),
                next_run_utc=_stamp(max(due_candidates) + timedelta(seconds=1)),
                message=f"첫 표본 저장 완료 · {len(samples)}개 · 두 번째 측정 예약")
    except APIError as exc:
        state = "REVIEW" if exc.request_uncertain else "FAILED"
        _update(job_id, profile_id, path, state=state,
                message=f"API 오류 {exc.code}; 불확실 요청 자동 재시도 금지")
        raise
    except Exception as exc:
        _update(job_id, profile_id, path, state="FAILED", message=f"연구 단계 실패: {type(exc).__name__}")
        raise
    return get(job_id, profile_id, path)


def due_jobs(path=None, *, now=None):
    store.initialize(path)
    current = _stamp(now or _now())
    with store.connect(path) as db:
        rows = db.execute("SELECT * FROM vph_research_jobs WHERE state='WAITING_SECOND' "
                          "AND next_run_utc<>'' AND next_run_utc<=? ORDER BY next_run_utc", (current,)).fetchall()
    return [_read(row) for row in rows]


def finish_schedule_jobs(profile_id, schedule_id, path=None, *, reason="schedule_ended"):
    """Mark scheduled observations unfinished at the schedule boundary; never fabricate a second sample."""
    if not isinstance(schedule_id, str) or not schedule_id:
        return 0
    message = ("8-hour research schedule ended before the second sample; existing observations retained."
               if reason == "schedule_ended" else
               "Research schedule was stopped before the second sample; existing observations retained.")
    with store.connect(path) as db:
        changed = db.execute(
            "UPDATE vph_research_jobs SET state='REVIEW',message=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE profile_id=? AND schedule_id=? AND state IN ('READY','WAITING_SECOND','PAUSED')",
            (message, profile_id, schedule_id)).rowcount
    return changed


def schedule_window_allows_sample(job, schedule):
    """A schedule-bound API sample may start only in its active research window."""
    schedule_id = (job or {}).get("schedule_id", "")
    return not schedule_id or bool(schedule and schedule.get("id") == schedule_id
                                   and schedule.get("state") == "running"
                                   and schedule.get("phase") == "research")


def run_second_phase(job_id, profile_id, key, *, access_token=None, quota_context=None, path=None,
                     sampler=vph.collect_video_samples, now=None, should_stop=lambda: False):
    job = get(job_id, profile_id, path)
    current = now or _now()
    if not job or job["state"] != "WAITING_SECOND":
        raise RuntimeError("VPH session is not waiting for its second collection.")
    due = datetime.fromisoformat(job["next_run_utc"].replace("Z", "+00:00"))
    if current.astimezone(timezone.utc) < due:
        raise RuntimeError("Minimum real elapsed interval has not passed.")
    if should_stop():
        _update(job_id, profile_id, path, state="CANCELLED", message="두 번째 표본 요청 전 중지; 첫 표본 보존")
        return get(job_id, profile_id, path)
    _update(job_id, profile_id, path, state="COLLECTING_SECOND", message="두 번째 표본 측정 중")
    try:
        ids = [item["video_id"] for item in job["targets"]]
        metadata={item["video_id"]:{"research_group":item["group"],"genre_evidence":item["positive_signals"],
            "genre_conflicts":item["conflict_signals"],"genre_reason":item["genre_reason"],
            "review_caution":item["caution"]} for item in job["targets"]}
        sampler(key, profile_id, ids, access_token=access_token,
                quota_context={**(quota_context or {}), "profile_id": profile_id,
                               "path": str(path or store.db_path()), "session_id": job_id}, path=path,
                target_metadata=metadata)
        if should_stop():
            _update(job_id, profile_id, path, state="CANCELLED", message="두 번째 표본 응답 저장 후 중지; 계산 기록 보존")
            return get(job_id, profile_id, path)
    except APIError as exc:
        state = "REVIEW" if exc.request_uncertain else "FAILED"
        _update(job_id, profile_id, path, state=state,
                message=f"두 번째 표본 API 오류 {exc.code}; 자동 중복 요청 금지")
        raise
    except Exception as exc:
        _update(job_id, profile_id, path, state="FAILED", message=f"두 번째 표본 실패: {type(exc).__name__}")
        raise
    _update(job_id, profile_id, path, state="COMPLETED", message="VPH 계산 완료")
    return get(job_id, profile_id, path)


def pause(job_id, profile_id, path=None):
    job = get(job_id, profile_id, path)
    if not job or job["state"] not in {"READY", "WAITING_SECOND"}:
        raise RuntimeError("Only a checkpointed waiting VPH session can be paused safely.")
    _update(job_id, profile_id, path, state="PAUSED", message=json.dumps(
        {"resume_state": job["state"], "message": job["message"]}, ensure_ascii=False))
    return get(job_id, profile_id, path)


def resume(job_id, profile_id, path=None):
    job = get(job_id, profile_id, path)
    if not job or job["state"] != "PAUSED":
        raise RuntimeError("VPH session is not paused.")
    saved = json.loads(job["message"] or "{}")
    state = saved.get("resume_state")
    if state not in {"READY", "WAITING_SECOND"}:
        raise RuntimeError("Saved VPH checkpoint cannot be resumed safely.")
    _update(job_id, profile_id, path, state=state, message=saved.get("message", "재개됨"))
    return get(job_id, profile_id, path)


def cancel(job_id, profile_id, path=None):
    job = get(job_id, profile_id, path)
    if not job or job["state"] not in ACTIVE_STATES:
        raise RuntimeError("VPH session is no longer active.")
    _update(job_id, profile_id, path, state="CANCELLED", message="사용자가 중지함; 저장된 표본 유지")
    return get(job_id, profile_id, path)
