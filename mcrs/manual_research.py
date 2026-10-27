"""Offline, user-driven YouTube reference research.

Timers measure only active analysis inside ZISU. They never imply playback,
views, YouTube watch time, or Analytics watch time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
import uuid
from urllib.parse import parse_qs, urlparse

from . import store
from .genres import REVIEW

MAX_LINKS = 50
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}


@dataclass(frozen=True)
class LinkRow:
    line_number: int
    original_url: str
    video_id: str
    normalized_url: str


@dataclass(frozen=True)
class LinkPreview:
    valid: tuple[LinkRow, ...]
    duplicates: tuple[tuple[int, int, str], ...]
    errors: tuple[tuple[int, str], ...]
    input_count: int

    @property
    def can_save(self):
        return bool(self.valid) and not self.errors and self.input_count <= MAX_LINKS


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_youtube_url(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("빈 주소입니다.")
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("http 또는 https YouTube 주소만 지원합니다.")
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError("안전하지 않은 YouTube 주소입니다.")
        video_id = ""
        if host in SHORT_HOSTS:
            parts = [part for part in parsed.path.split("/") if part]
            video_id = parts[0] if len(parts) == 1 else ""
        elif host in YOUTUBE_HOSTS:
            if parsed.path == "/watch":
                values = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
                video_id = values[0] if len(values) == 1 else ""
            else:
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 2 and parts[0] in {"shorts", "live"}:
                    video_id = parts[1]
        else:
            raise ValueError("YouTube 공식 도메인이 아닙니다.")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc):
            raise
        raise ValueError("주소 형식을 확인해 주세요.") from None
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("11자리 YouTube video_id를 찾을 수 없습니다.")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def preview_links(text: str) -> LinkPreview:
    lines = [(number, line.strip()) for number, line in enumerate(str(text or "").splitlines(), 1) if line.strip()]
    errors = []
    if len(lines) > MAX_LINKS:
        errors.append((0, f"최대 {MAX_LINKS}개까지 등록할 수 있습니다. 현재 {len(lines)}개입니다."))
    valid, duplicates, first_seen = [], [], {}
    for line_number, raw in lines:
        try:
            video_id, normalized = normalize_youtube_url(raw)
        except ValueError as exc:
            errors.append((line_number, str(exc)))
            continue
        if video_id in first_seen:
            duplicates.append((line_number, first_seen[video_id], video_id))
            continue
        first_seen[video_id] = line_number
        valid.append(LinkRow(line_number, raw, video_id, normalized))
    return LinkPreview(tuple(valid), tuple(duplicates), tuple(errors), len(lines))


def create_batch(profile_id: str, text: str, *, planned_hours=1, analysis_minutes=15,
                 break_minutes=5, path=None) -> str:
    preview = preview_links(text)
    if not preview.can_save:
        raise ValueError("링크 미리보기의 오류를 먼저 해결해 주세요.")
    planned_minutes = int(float(planned_hours) * 60)
    analysis_minutes, break_minutes = int(analysis_minutes), int(break_minutes)
    if not 60 <= planned_minutes <= 1440:
        raise ValueError("전체 연구 시간은 1~24시간이어야 합니다.")
    if not 1 <= analysis_minutes <= 240 or not 0 <= break_minutes <= 240:
        raise ValueError("분석 알림은 1~240분, 휴식 알림은 0~240분이어야 합니다.")
    store.initialize(path)
    batch_id = uuid.uuid4().hex
    with store.connect(path) as db:
        profile = db.execute("SELECT market,genre FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not profile:
            raise ValueError("채널 프로필을 찾을 수 없습니다.")
        if db.execute("SELECT 1 FROM manual_research_batches WHERE profile_id=? AND state IN "
                      "('ready','running','paused','interrupted')", (profile_id,)).fetchone():
            raise ValueError("이 프로필에 이어서 진행할 수 있는 수동 연구가 이미 있습니다.")
        db.execute("INSERT INTO manual_research_batches(id,profile_id,market,core_genre,planned_minutes,"
                   "analysis_minutes,break_minutes,state) VALUES(?,?,?,?,?,?,?,'ready')",
                   (batch_id, profile_id, profile["market"], profile["genre"], planned_minutes,
                    analysis_minutes, break_minutes))
        db.executemany("INSERT INTO manual_research_items(batch_id,profile_id,video_id,original_url,"
                       "normalized_url,position,auto_genre_decision,auto_genre_reason) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       [(batch_id, profile_id, row.video_id, row.original_url, row.normalized_url,
                         position, REVIEW, "제목·채널 메타데이터 미확인: 사람 검토 필요")
                        for position, row in enumerate(preview.valid, 1)])
    return batch_id


def get_batch(batch_id, profile_id, path=None):
    with store.connect(path) as db:
        row = db.execute("SELECT * FROM manual_research_batches WHERE id=? AND profile_id=?",
                         (batch_id, profile_id)).fetchone()
        return dict(row) if row else None


def list_batches(profile_id, path=None):
    with store.connect(path) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM manual_research_batches WHERE profile_id=? ORDER BY created_at DESC,id DESC", (profile_id,))]


def list_items(batch_id, profile_id, path=None):
    with store.connect(path) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM manual_research_items WHERE batch_id=? AND profile_id=? ORDER BY position",
            (batch_id, profile_id))]


def recover_interrupted(path=None, *, now=None):
    """Mark stale running sessions interrupted without adding offline elapsed time."""
    now = now or utc_now()
    with store.connect(path) as db:
        rows = db.execute("SELECT id,profile_id,running_since_utc FROM manual_research_batches WHERE state='running'").fetchall()
        for row in rows:
            db.execute("UPDATE manual_research_batches SET state='interrupted',running_since_utc='',updated_at=? WHERE id=?",
                       (now, row["id"]))
            db.execute("UPDATE manual_research_items SET timer_started_utc='',updated_at=? WHERE batch_id=?",
                       (now, row["id"]))
        db.execute("UPDATE manual_research_batches SET break_started_utc='' WHERE break_started_utc<>''")
        return len(rows)


def _elapsed(started, now):
    if not started:
        return 0
    return max(0, int((datetime.fromisoformat(now) - datetime.fromisoformat(started)).total_seconds()))


def start_or_resume(batch_id, profile_id, video_id, path=None, *, now=None):
    now = now or utc_now()
    with store.connect(path) as db:
        batch = db.execute("SELECT * FROM manual_research_batches WHERE id=? AND profile_id=?",
                           (batch_id, profile_id)).fetchone()
        item = db.execute("SELECT 1 FROM manual_research_items WHERE batch_id=? AND profile_id=? AND video_id=?",
                          (batch_id, profile_id, video_id)).fetchone()
        if not batch or not item or batch["state"] not in {"ready", "paused", "interrupted"}:
            raise ValueError("이어갈 수 있는 수동 연구를 찾을 수 없습니다.")
        break_elapsed = _elapsed(batch["break_started_utc"], now)
        db.execute("UPDATE manual_research_batches SET state='running',running_since_utc=?,break_started_utc='',"
                   "break_seconds=break_seconds+?,current_video_id=?,updated_at=? WHERE id=?",
                   (now, break_elapsed, video_id, now, batch_id))
        db.execute("UPDATE manual_research_items SET research_state='in_progress',started_at_utc=CASE WHEN started_at_utc='' THEN ? ELSE started_at_utc END,timer_started_utc=?,updated_at=? WHERE batch_id=? AND video_id=?",
                   (now, now, now, batch_id, video_id))


def pause(batch_id, profile_id, path=None, *, now=None, as_break=False):
    now = now or utc_now()
    with store.connect(path) as db:
        batch = db.execute("SELECT * FROM manual_research_batches WHERE id=? AND profile_id=?",
                           (batch_id, profile_id)).fetchone()
        if not batch or batch["state"] != "running":
            raise ValueError("진행 중인 수동 연구가 아닙니다.")
        elapsed = _elapsed(batch["running_since_utc"], now)
        db.execute("UPDATE manual_research_batches SET state='paused',active_seconds=active_seconds+?,"
                   "running_since_utc='',break_started_utc=?,updated_at=? WHERE id=?",
                   (elapsed, now if as_break else "", now, batch_id))
        if batch["current_video_id"]:
            db.execute("UPDATE manual_research_items SET active_seconds=active_seconds+?,timer_started_utc='',updated_at=? "
                       "WHERE batch_id=? AND video_id=?", (elapsed, now, batch_id, batch["current_video_id"]))


def start_break(batch_id, profile_id, path=None, *, now=None):
    pause(batch_id, profile_id, path, now=now, as_break=True)


ALLOWED_HUMAN = {"핵심 적합", "관련 참고", "보류", "제외", ""}
EDITABLE_FIELDS = {"human_fit", "human_genre_label", "mood", "intro_notes", "vocal_notes",
                   "rap_melody_notes", "beat_tempo_notes", "hook_structure_notes",
                   "title_thumbnail_notes", "reuse_ideas", "notes", "listened_segments", "research_state"}


def save_evaluation(batch_id, profile_id, video_id, values, path=None):
    unknown = set(values) - EDITABLE_FIELDS
    if unknown:
        raise ValueError("지원하지 않는 평가 항목입니다.")
    if values.get("human_fit", "") not in ALLOWED_HUMAN:
        raise ValueError("사람 판정은 핵심 적합/관련 참고/보류/제외 중 하나여야 합니다.")
    if values.get("research_state", "completed") not in {"queued", "in_progress", "completed"}:
        raise ValueError("지원하지 않는 연구 상태입니다.")
    now = utc_now()
    fields, args = [], []
    for key, value in values.items():
        fields.append(f"{key}=?")
        args.append(str(value)[:10000])
    fields.append("updated_at=?"); args.append(now)
    args.extend((batch_id, profile_id, video_id))
    with store.connect(path) as db:
        result = db.execute("UPDATE manual_research_items SET "+",".join(fields)+
                            " WHERE batch_id=? AND profile_id=? AND video_id=?", args)
        if result.rowcount != 1:
            raise ValueError("수동 연구 항목을 찾을 수 없습니다.")
        if values.get("research_state") == "completed":
            db.execute("UPDATE manual_research_items SET ended_at_utc=CASE WHEN ended_at_utc='' THEN ? ELSE ended_at_utc END "
                       "WHERE batch_id=? AND profile_id=? AND video_id=?", (now, batch_id, profile_id, video_id))
            remaining = db.execute("SELECT 1 FROM manual_research_items WHERE batch_id=? AND profile_id=? "
                                   "AND research_state<>'completed' LIMIT 1", (batch_id, profile_id)).fetchone()
            if not remaining:
                batch = db.execute("SELECT state,running_since_utc,current_video_id FROM manual_research_batches "
                                   "WHERE id=? AND profile_id=?", (batch_id, profile_id)).fetchone()
                elapsed = _elapsed(batch["running_since_utc"], now) if batch and batch["state"] == "running" else 0
                if batch and elapsed and batch["current_video_id"]:
                    db.execute("UPDATE manual_research_items SET active_seconds=active_seconds+?,timer_started_utc='' "
                               "WHERE batch_id=? AND profile_id=? AND video_id=?",
                               (elapsed, batch_id, profile_id, batch["current_video_id"]))
                db.execute("UPDATE manual_research_batches SET state='completed',active_seconds=active_seconds+?,"
                           "running_since_utc='',break_started_utc='',updated_at=? WHERE id=? AND profile_id=?",
                           (elapsed, now, batch_id, profile_id))


def summary(batch_id, profile_id, path=None):
    with store.connect(path) as db:
        row = db.execute("SELECT COUNT(*) total,SUM(research_state='completed') completed,"
                         "SUM(auto_genre_decision=?) reviews FROM manual_research_items "
                         "WHERE batch_id=? AND profile_id=?", (REVIEW, batch_id, profile_id)).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}
