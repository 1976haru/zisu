"""Profile-scoped, read-only YouTube view velocity observations."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re

from . import genres, store, youtube_api

MIN_INTERVAL_SECONDS = 3600
DEFAULT_MAX_AGE_SECONDS = 48 * 3600
_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("VPH observation time must include a timezone.")
    return parsed.astimezone(timezone.utc)


def parse_duration(value):
    match = _DURATION.fullmatch(str(value or ""))
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def classify_format(snippet, duration_seconds):
    if str((snippet or {}).get("liveBroadcastContent", "none")) in {"live", "upcoming"}:
        return "live"
    # The Data API has no authoritative isShort field. Never claim short from
    # duration alone; expose it as a reviewable candidate.
    if duration_seconds is not None and duration_seconds <= 180:
        return "short_candidate"
    return "longform" if duration_seconds is not None else "unknown"


def set_thresholds(profile_id, tier_a=100, tier_b=50, path=None):
    tier_a, tier_b = float(tier_a), float(tier_b)
    if not math.isfinite(tier_a) or not math.isfinite(tier_b) or tier_b < 0 or tier_a <= tier_b:
        raise ValueError("Tier A must be greater than non-negative Tier B.")
    store.initialize(path)
    with store.connect(path) as db:
        changed = db.execute("UPDATE profiles SET vph_tier_a=?,vph_tier_b=? WHERE id=?",
                             (tier_a, tier_b, profile_id)).rowcount
    if changed != 1:
        raise ValueError("Profile not found.")


def record_observation(profile_id, video_id, view_count, observed_at_utc=None, *,
                       sample_status="valid", uncertainty_reason="", video_format="unknown",
                       published_at="", duration_seconds=None, metadata=None, path=None,
                       max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Append one independent cumulative-count sample and evaluate its prior pair."""
    store.initialize(path)
    observed = _utc(observed_at_utc or datetime.now(timezone.utc))
    video_id = str(video_id)
    if len(video_id) != 11 or not all(c.isalnum() or c in "-_" for c in video_id):
        raise ValueError("Invalid YouTube video ID.")
    if view_count is not None:
        view_count = int(view_count)
        if view_count < 0:
            raise ValueError("View count cannot be negative.")
    status = sample_status if sample_status in {"valid", "uncertain", "missing"} else "uncertain"
    reason = str(uncertainty_reason or "")[:300]
    if view_count is None:
        status, reason = "missing", reason or "view_count_missing"
    stamp = observed.isoformat()
    with store.connect(path) as db:
        profile = db.execute("SELECT vph_tier_a,vph_tier_b FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not profile:
            raise ValueError("Profile not found.")
        duplicate = db.execute("SELECT id,view_count,sample_status FROM vph_observations WHERE profile_id=? AND video_id=? AND observed_at_utc=? LIMIT 1",
                               (profile_id,video_id,stamp)).fetchone()
        if duplicate:
            if duplicate["view_count"]!=view_count or duplicate["sample_status"]!=status:
                raise ValueError("Conflicting cumulative views for an existing observation timestamp.")
            existing=db.execute("SELECT * FROM vph_evaluations WHERE later_observation_id=?",(duplicate["id"],)).fetchone()
            if existing:
                return dict(existing)
            raise ValueError("An observation already exists for this profile, video and timestamp.")
        latest = db.execute("SELECT observed_at_utc FROM vph_observations WHERE profile_id=? AND video_id=? "
                            "ORDER BY observed_at_utc DESC,id DESC LIMIT 1",(profile_id,video_id)).fetchone()
        if latest and observed < _utc(latest["observed_at_utc"]):
            status,reason="uncertain","out_of_order_observation"
        db.execute("""INSERT INTO vph_observations(profile_id,video_id,observed_at_utc,view_count,
          sample_status,uncertainty_reason,video_format,published_at,duration_seconds,metadata_json)
          VALUES(?,?,?,?,?,?,?,?,?,?)""", (profile_id,video_id,stamp,view_count,status,reason,
          str(video_format)[:30],str(published_at)[:100],duration_seconds,
          json.dumps(metadata or {},ensure_ascii=False,sort_keys=True)))
        later_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        earlier = db.execute("""SELECT * FROM vph_observations WHERE profile_id=? AND video_id=?
          AND observed_at_utc<? ORDER BY observed_at_utc DESC,id DESC LIMIT 1""",
          (profile_id,video_id,stamp)).fetchone()
        state, eval_reason, delta, elapsed, rate, confidence = "UNKNOWN", "first_sample", None, None, None, "none"
        if reason=="out_of_order_observation":
            state, eval_reason = "REVIEW", reason
        elif earlier:
            elapsed = (observed - _utc(earlier["observed_at_utc"])).total_seconds()
            if status != "valid" or earlier["sample_status"] != "valid":
                state, eval_reason = "REVIEW", "uncertain_or_missing_sample"
            elif elapsed < MIN_INTERVAL_SECONDS:
                eval_reason = "interval_under_60_minutes"
            elif elapsed > max_age_seconds:
                state, eval_reason = "REVIEW", "stale_sample_pair"
            else:
                delta = view_count - earlier["view_count"]
                if delta < 0:
                    state, eval_reason = "REVIEW", "cumulative_view_count_decreased"
                else:
                    rate = delta / elapsed * 3600.0
                    state, eval_reason, confidence = "VALID", "independent_samples_60m_or_more", "high"
        tier_a, tier_b = float(profile[0]), float(profile[1])
        tier = "A" if rate is not None and rate >= tier_a else ("B" if rate is not None and rate >= tier_b else ("C" if rate is not None else "UNKNOWN"))
        db.execute("""INSERT INTO vph_evaluations(profile_id,video_id,earlier_observation_id,
          later_observation_id,elapsed_seconds,view_delta,vph,state,reason,tier,tier_a_threshold,
          tier_b_threshold,confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (profile_id,video_id,earlier["id"] if earlier else None,later_id,elapsed,delta,rate,
           state,eval_reason,tier,tier_a,tier_b,confidence))
        row = db.execute("SELECT * FROM vph_evaluations WHERE later_observation_id=?", (later_id,)).fetchone()
    return dict(row)


def list_radar(profile_id, path=None):
    """Return only each video's newest evaluation, ordered by genre fit then VPH."""
    with store.connect(path) as db:
        rows = db.execute("""WITH ranked AS (
          SELECT e.*,ROW_NUMBER() OVER(PARTITION BY e.profile_id,e.video_id
            ORDER BY o.observed_at_utc DESC,o.id DESC,e.id DESC) AS sample_rank
          FROM vph_evaluations e JOIN vph_observations o ON o.id=e.later_observation_id
          WHERE e.profile_id=?)
          SELECT e.*,o.observed_at_utc,o.view_count,o.sample_status,o.uncertainty_reason,o.video_format,
          o.published_at,o.duration_seconds,o.metadata_json,c.video_title,c.channel_title,c.views AS cumulative_views,
          c.genre_score,c.genre_decision,c.country
          FROM ranked e JOIN vph_observations o ON o.id=e.later_observation_id
          LEFT JOIN candidates c ON c.profile_id=e.profile_id AND c.video_id=e.video_id
          WHERE e.sample_rank=1 ORDER BY CASE c.genre_decision WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END,
          COALESCE(c.genre_score,-1) DESC,
          CASE e.state WHEN 'VALID' THEN 0 ELSE 1 END,COALESCE(e.vph,-1) DESC,e.id DESC""",
          (profile_id,genres.FIT,genres.REVIEW)).fetchall()
    return [dict(row) for row in rows]


def list_history(profile_id, video_id, path=None):
    """Return immutable chronological observations/evaluations for one profile/video."""
    with store.connect(path) as db:
        rows = db.execute("""SELECT e.*,o.observed_at_utc,o.view_count,o.sample_status,
          o.uncertainty_reason,o.video_format,o.published_at,o.duration_seconds,o.metadata_json
          FROM vph_evaluations e JOIN vph_observations o ON o.id=e.later_observation_id
          WHERE e.profile_id=? AND e.video_id=? ORDER BY o.observed_at_utc,o.id""",
          (profile_id,video_id)).fetchall()
    return [dict(row) for row in rows]


def collect_video_samples(key, profile_id, video_ids, *, access_token=None, quota_context=None,
                          observed_at_utc=None, path=None, target_metadata=None):
    """Fetch metadata only. No playback or engagement endpoint is used."""
    ids = list(dict.fromkeys(str(value) for value in video_ids if value))[:50]
    if not ids:
        return []
    response = youtube_api._get("videos", key, part="snippet,contentDetails,statistics",
        id=",".join(ids), access_token=access_token, quota_context=quota_context)
    by_id = {item.get("id"): item for item in response.get("items", [])}
    results = []
    for video_id in ids:
        item = by_id.get(video_id)
        if not item:
            results.append(record_observation(profile_id,video_id,None,observed_at_utc,
                sample_status="missing",uncertainty_reason="video_metadata_missing",path=path))
            continue
        raw_count = item.get("statistics", {}).get("viewCount")
        duration = parse_duration(item.get("contentDetails", {}).get("duration"))
        snippet = item.get("snippet", {})
        results.append(record_observation(profile_id,video_id,
            int(raw_count) if raw_count is not None else None,observed_at_utc,
            sample_status="valid" if raw_count is not None else "missing",
            uncertainty_reason="" if raw_count is not None else "view_count_missing",
            video_format=classify_format(snippet,duration),published_at=snippet.get("publishedAt", ""),
            duration_seconds=duration,metadata={"source":"youtube_data_api_v3",
                **((target_metadata or {}).get(video_id) or {})},path=path))
    return results
