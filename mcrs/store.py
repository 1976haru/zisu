"""Backward-compatible, profile-isolated SQLite storage (credentials excluded)."""
from __future__ import annotations
import csv
import math
import os
import sqlite3
import uuid
import json
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from .genres import GENRES, EXCLUDE, MANUAL_APPROVAL, REVIEW, assess

MARKETS = ("JP", "KR")


class ActiveResearchSessionError(RuntimeError):
    """A profile already has an in-flight fit-target research job."""

def normalize_related_genres(primary, related=()):
    """Validate and canonicalize related genre IDs; legacy subgenre stays untouched."""
    if primary not in GENRES:
        raise ValueError("Choose a registered core genre.")
    if related is None:
        related = ()
    if isinstance(related, str):
        try:
            related = json.loads(related)
        except (TypeError, json.JSONDecodeError):
            raise ValueError("Related genre settings are invalid.") from None
    if not isinstance(related, (list, tuple)):
        raise ValueError("Related genres must be a list.")
    result = []
    for genre in related:
        if genre not in GENRES:
            raise ValueError("Only registered genres can be selected.")
        if genre != primary and genre not in result:
            result.append(genre)
    if len(result) > 5:
        raise ValueError("Choose at most five related genres.")
    return result

def data_dir() -> Path:
    override = os.environ.get("ZISU_DATA_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()/".local"/"share")/"ZisuResearchStudio"

def db_path() -> Path:
    return data_dir()/"zisu_v2.db"

def save_ui_state(state, path=None):
    """Persist allow-listed, non-secret presentation state independently of research rows."""
    allowed = {"profile_id", "tab_index", "selected_video_id", "search_term", "filter",
               "decision_filter", "sort", "ai_target", "ai_max_calls", "ai_provider",
               "ai_cost_estimate", "ai_cost_limit", "last_report_session"}
    if not isinstance(state, dict) or set(state) - allowed:
        raise ValueError("Invalid UI state fields.")
    clean = {}
    for key, value in state.items():
        if value is None:
            clean[key] = None
        elif isinstance(value, (str, int, float, bool)):
            clean[key] = str(value)[:500] if isinstance(value, str) else value
        else:
            raise ValueError("UI state must contain simple values only.")
    payload = json.dumps(clean, ensure_ascii=False, allow_nan=False, sort_keys=True)
    with connect(path) as db:
        db.execute("INSERT INTO ui_state(state_key,state_json) VALUES('main',?) "
                   "ON CONFLICT(state_key) DO UPDATE SET state_json=excluded.state_json,updated_at=CURRENT_TIMESTAMP",
                   (payload,))

def load_ui_state(path=None):
    with connect(path) as db:
        row = db.execute("SELECT state_json,updated_at FROM ui_state WHERE state_key='main'").fetchone()
    if not row:
        return None
    try:
        value = json.loads(row["state_json"])
        if not isinstance(value, dict):
            return None
        return {"state": value, "updated_at": row["updated_at"]}
    except (TypeError, json.JSONDecodeError):
        return None

@contextmanager
def connect(path=None):
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    # Keep concurrent GUI/search operations from failing immediately while a
    # short writer transaction is in progress.  This setting is per-connection.
    db.execute("PRAGMA busy_timeout=15000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def _add_column(db, table, definition):
    name = definition.split()[0]
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

def initialize(path=None):
    """Create or migrate in place. Existing rows are never dropped or replaced."""
    with connect(path) as db:
        # WAL permits readers during writes and is safe to request repeatedly.
        # SQLite keeps existing user rows intact when changing journal mode.
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          market TEXT NOT NULL CHECK(market IN ('JP','KR')), genre TEXT NOT NULL,
          schedule TEXT NOT NULL DEFAULT '07:00,20:00',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS google_accounts (
          id TEXT PRIMARY KEY, email TEXT NOT NULL DEFAULT '', normalized_email TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_google_accounts_email
          ON google_accounts(normalized_email) WHERE normalized_email<>'';
        CREATE TABLE IF NOT EXISTS google_account_channels (
          account_id TEXT NOT NULL, channel_id TEXT NOT NULL DEFAULT '', channel_title TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(account_id,channel_id),
          FOREIGN KEY(account_id) REFERENCES google_accounts(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS candidates (
          profile_id TEXT NOT NULL, video_id TEXT NOT NULL, video_title TEXT NOT NULL,
          channel_title TEXT NOT NULL DEFAULT '', channel_id TEXT NOT NULL DEFAULT '',
          video_url TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          published_at TEXT NOT NULL DEFAULT '', views INTEGER NOT NULL DEFAULT 0,
          country TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
          genre_decision TEXT NOT NULL, genre_score INTEGER NOT NULL,
          genre_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT '미확인',
          classified_genre TEXT NOT NULL DEFAULT '', related_genres_json TEXT NOT NULL DEFAULT '[]',
          notes TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(profile_id,video_id),
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS metrics (
          profile_id TEXT NOT NULL, day TEXT NOT NULL, all_views INTEGER NOT NULL,
          target_views INTEGER NOT NULL, impressions INTEGER NOT NULL, ctr REAL NOT NULL,
          avg_minutes REAL NOT NULL, subscribers INTEGER NOT NULL,
          PRIMARY KEY(profile_id,day),
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS genre_audit (
          profile_id TEXT NOT NULL, video_id TEXT NOT NULL, video_title TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', market TEXT NOT NULL, target_genre TEXT NOT NULL,
          predicted_decision TEXT NOT NULL, predicted_score INTEGER NOT NULL,
          reason TEXT NOT NULL, positive_signals TEXT NOT NULL DEFAULT '',
          conflict_signals TEXT NOT NULL DEFAULT '', uncertainty TEXT NOT NULL DEFAULT '',
          human_label TEXT NOT NULL DEFAULT '', label_notes TEXT NOT NULL DEFAULT '',
          rule_verdict TEXT NOT NULL DEFAULT '', ai_verdict TEXT NOT NULL DEFAULT '',
          proposed_verdict TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
          model_version TEXT NOT NULL DEFAULT '', decision_at TEXT NOT NULL DEFAULT '',
          review_status TEXT NOT NULL DEFAULT 'needs_review',
          reviewed_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(profile_id,video_id),
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS profile_search_terms (
          profile_id TEXT NOT NULL, market TEXT NOT NULL, term TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(profile_id,market,term),
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ai_reviews (
          profile_id TEXT NOT NULL, video_id TEXT NOT NULL, metadata_hash TEXT NOT NULL,
          genre TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
          prompt_version TEXT NOT NULL, ai_verdict TEXT NOT NULL,
          positive_signals TEXT NOT NULL, conflict_signals TEXT NOT NULL,
          evidence TEXT NOT NULL, uncertainty TEXT NOT NULL, human_check TEXT NOT NULL,
          latency_ms INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(profile_id,video_id,metadata_hash,genre,provider,model,prompt_version),
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ai_usage (
          id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL,
          provider TEXT NOT NULL, model TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL, cost_estimate REAL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS idx_ai_usage_day ON ai_usage(profile_id,provider,created_at);
        CREATE TABLE IF NOT EXISTS research_sessions (
          id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, market TEXT NOT NULL, genre TEXT NOT NULL,
          state TEXT NOT NULL, target INTEGER NOT NULL, max_search_calls INTEGER NOT NULL,
          query_index INTEGER NOT NULL DEFAULT 0, processed_json TEXT NOT NULL DEFAULT '[]',
          candidates_json TEXT NOT NULL DEFAULT '[]', reviews_json TEXT NOT NULL DEFAULT '[]', in_flight INTEGER NOT NULL DEFAULT 0,
          target_mode TEXT NOT NULL DEFAULT 'candidate_count', page_token TEXT NOT NULL DEFAULT '',
          calls_used INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS research_schedules (
          id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
          market TEXT NOT NULL CHECK(market IN ('JP','KR')),
          core_genre TEXT NOT NULL, related_genres_json TEXT NOT NULL DEFAULT '[]',
          channel_id TEXT NOT NULL DEFAULT '', start_at_utc TEXT NOT NULL,
          total_minutes INTEGER NOT NULL, research_minutes INTEGER NOT NULL,
          break_minutes INTEGER NOT NULL, approved_search_calls INTEGER NOT NULL,
          search_calls_used INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL CHECK(state IN ('scheduled','running','paused','completed','cancelled')),
          phase TEXT NOT NULL DEFAULT 'waiting', elapsed_seconds INTEGER NOT NULL DEFAULT 0,
          running_since_utc TEXT NOT NULL DEFAULT '', search_in_flight INTEGER NOT NULL DEFAULT 0,
          pause_reason TEXT NOT NULL DEFAULT '',
          checkpoint_json TEXT NOT NULL DEFAULT '{}',
          autopilot_session_id TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_research_schedules_one_active
          ON research_schedules(profile_id) WHERE state IN ('scheduled','running','paused');
        CREATE INDEX IF NOT EXISTS idx_research_schedules_recovery
          ON research_schedules(profile_id,state,updated_at);
        CREATE TABLE IF NOT EXISTS autopilot_run_settings (
          session_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, settings_json TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS autopilot_reports (
          session_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, report_json TEXT NOT NULL,
          exceptions_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS autopilot_comment_drafts (
          profile_id TEXT NOT NULL, video_id TEXT NOT NULL, session_id TEXT NOT NULL,
          drafts_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(profile_id,video_id,session_id),
          FOREIGN KEY(profile_id,video_id) REFERENCES candidates(profile_id,video_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ui_state (
          state_key TEXT PRIMARY KEY, state_json TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_candidates_status
          ON candidates(profile_id,genre_decision,status);
        """)
        for definition in (
            "youtube_channel_id TEXT NOT NULL DEFAULT ''",
            "youtube_channel_title TEXT NOT NULL DEFAULT ''",
            "connected_at TEXT NOT NULL DEFAULT ''",
            "connected_email TEXT NOT NULL DEFAULT ''",
            "google_account_id TEXT NOT NULL DEFAULT ''",
            "subgenre TEXT NOT NULL DEFAULT ''",
            "related_genres_json TEXT NOT NULL DEFAULT '[]'",
            "comment_persona_name TEXT NOT NULL DEFAULT ''",
            "comment_language TEXT NOT NULL DEFAULT ''",
            "comment_tone TEXT NOT NULL DEFAULT ''",
            "comment_emoji_level TEXT NOT NULL DEFAULT 'light'",
            "comment_length TEXT NOT NULL DEFAULT 'short'",
        ):
            _add_column(db, "profiles", definition)
        _add_column(db, "research_sessions", "reviews_json TEXT NOT NULL DEFAULT '[]'")
        _add_column(db, "research_sessions", "settings_hash TEXT NOT NULL DEFAULT ''")
        _add_column(db, "research_schedules", "pause_reason TEXT NOT NULL DEFAULT ''")
        _add_column(db, "research_schedules", "research_extension_cycle INTEGER NOT NULL DEFAULT -1")
        _add_column(db, "research_schedules", "research_extension_seconds INTEGER NOT NULL DEFAULT 0")
        _add_column(db, "research_schedules", "research_extension_used INTEGER NOT NULL DEFAULT 0")
        _add_column(db, "candidates", "classified_genre TEXT NOT NULL DEFAULT ''")
        _add_column(db, "candidates", "related_genres_json TEXT NOT NULL DEFAULT '[]'")
        for definition in (
            "rule_verdict TEXT NOT NULL DEFAULT ''", "ai_verdict TEXT NOT NULL DEFAULT ''",
            "proposed_verdict TEXT NOT NULL DEFAULT ''", "evidence TEXT NOT NULL DEFAULT ''",
            "model_version TEXT NOT NULL DEFAULT ''", "decision_at TEXT NOT NULL DEFAULT ''",
            "review_status TEXT NOT NULL DEFAULT 'needs_review'",
        ):
            _add_column(db, "genre_audit", definition)
        for definition in (
            "target_mode TEXT NOT NULL DEFAULT 'candidate_count'", "page_token TEXT NOT NULL DEFAULT ''",
            "calls_used INTEGER NOT NULL DEFAULT 0", "duplicate_count INTEGER NOT NULL DEFAULT 0",
        ):
            _add_column(db, "research_sessions", definition)
        db.execute("UPDATE genre_audit SET rule_verdict=predicted_decision WHERE rule_verdict=''")
        db.execute("UPDATE genre_audit SET review_status='human_reviewed' WHERE human_label<>'' AND review_status='needs_review'")
        # Additive migration: link existing profile identities to an account and
        # channel registry without moving, copying, or rewriting OAuth tokens.
        rows = db.execute("SELECT id,connected_email,youtube_channel_id,youtube_channel_title,google_account_id "
                          "FROM profiles WHERE connected_email<>''").fetchall()
        for row in rows:
            normalized = (row["connected_email"] or "").strip().casefold()
            account = db.execute("SELECT id FROM google_accounts WHERE normalized_email=?", (normalized,)).fetchone()
            account_id = account["id"] if account else uuid.uuid4().hex
            if not account:
                db.execute("INSERT INTO google_accounts(id,email,normalized_email) VALUES(?,?,?)",
                           (account_id,row["connected_email"],normalized))
            db.execute("UPDATE profiles SET google_account_id=? WHERE id=? AND google_account_id=''",
                       (account_id,row["id"]))
            db.execute("INSERT INTO google_account_channels(account_id,channel_id,channel_title) VALUES(?,?,?) "
                       "ON CONFLICT(account_id,channel_id) DO UPDATE SET "
                       "channel_title=CASE WHEN excluded.channel_title='' THEN channel_title ELSE excluded.channel_title END",
                       (account_id,row["youtube_channel_id"] or "",row["youtube_channel_title"] or ""))

def create_profile(name, market, genre, path=None, subgenre="", related_genres=None):
    initialize(path)
    name = (name or "").strip()
    if not 2 <= len(name) <= 70:
        raise ValueError("프로필 이름은 2~70자로 입력하세요.")
    if market not in MARKETS or genre not in GENRES:
        raise ValueError("국가와 장르를 확인하세요.")
    related = normalize_related_genres(genre, related_genres)
    profile_id = uuid.uuid4().hex
    with connect(path) as db:
        db.execute("INSERT INTO profiles(id,name,market,genre,subgenre,related_genres_json) VALUES(?,?,?,?,?,?)",
                   (profile_id,name,market,genre,(subgenre or "").strip()[:100],json.dumps(related)))
    return profile_id

def list_profiles(path=None):
    initialize(path)
    with connect(path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY created_at,id")]

def set_comment_persona(profile_id, *, name, language, tone, emoji_level="light", length="short", path=None):
    if language not in {"ja", "ko", "en"} or emoji_level not in {"none", "light", "moderate"} or length not in {"short", "medium", "long"}:
        raise ValueError("Invalid comment persona settings.")
    if len(name) > 80 or len(tone) > 80:
        raise ValueError("Comment persona settings are too long.")
    with connect(path) as db:
        result = db.execute("""UPDATE profiles SET comment_persona_name=?,comment_language=?,
          comment_tone=?,comment_emoji_level=?,comment_length=? WHERE id=?""",
          (name.strip(), language, tone.strip(), emoji_level, length, profile_id))
        if result.rowcount != 1:
            raise ValueError("Profile not found.")

def get_profile(profile_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None

def list_google_accounts(path=None):
    """List account/channel relationships without exposing credentials."""
    initialize(path)
    with connect(path) as db:
        accounts = [dict(row) for row in db.execute(
            "SELECT * FROM google_accounts ORDER BY lower(email),id")]
        for account in accounts:
            account["channels"] = [dict(row) for row in db.execute(
                "SELECT channel_id,channel_title FROM google_account_channels WHERE account_id=? "
                "ORDER BY channel_title,channel_id", (account["id"],))]
            account["profiles"] = [dict(row) for row in db.execute(
                "SELECT id,name,market,genre,youtube_channel_id,youtube_channel_title,connected_email "
                "FROM profiles WHERE google_account_id=? ORDER BY name,id", (account["id"],))]
    return accounts

def list_account_profiles(account_id, path=None):
    with connect(path) as db:
        return [dict(row) for row in db.execute(
            "SELECT id,name,market,genre,youtube_channel_id,youtube_channel_title,connected_email "
            "FROM profiles WHERE google_account_id=? ORDER BY name,id", (account_id,))]

def dashboard_summary(profile_id, path=None):
    """Return small, profile-scoped dashboard counts without loading candidate rows."""
    with connect(path) as db:
        profile = db.execute(
            "SELECT id,name,market,genre,connected_email,youtube_channel_id,"
            "youtube_channel_title,schedule,google_account_id FROM profiles WHERE id=?",
            (profile_id,)).fetchone()
        if not profile:
            return None
        counts = db.execute("""SELECT
            (SELECT COUNT(*) FROM candidates WHERE profile_id=?) AS candidates,
            (SELECT COUNT(*) FROM candidates WHERE profile_id=? AND date(updated_at)=date('now')) AS today_candidates,
            (SELECT COUNT(*) FROM ai_reviews WHERE profile_id=?) AS ai_reviews,
            (SELECT COUNT(*) FROM ai_reviews WHERE profile_id=? AND date(created_at)=date('now')) AS today_ai_reviews,
            (SELECT COUNT(*) FROM genre_audit WHERE profile_id=? AND human_label<>'') AS human_labels,
            (SELECT COUNT(*) FROM genre_audit WHERE profile_id=? AND review_status='needs_review') AS needs_review""",
            (profile_id, profile_id, profile_id, profile_id, profile_id, profile_id)).fetchone()
        account = None
        if profile["google_account_id"]:
            row = db.execute("SELECT email FROM google_accounts WHERE id=?", (profile["google_account_id"],)).fetchone()
            account = row["email"] if row else None
        active_schedule = db.execute("SELECT id,state,phase,start_at_utc,total_minutes,research_minutes,"
            "break_minutes,elapsed_seconds,approved_search_calls,search_calls_used "
            "FROM research_schedules WHERE profile_id=? AND state IN ('scheduled','running','paused') "
            "ORDER BY created_at DESC LIMIT 1", (profile_id,)).fetchone()
    result = dict(profile)
    result.update(dict(counts))
    result["account_email"] = account or profile["connected_email"] or ""
    result["schedule_state"] = dict(active_schedule) if active_schedule else None
    return result

def bind_google_account_channel(profile_id, channel_id, channel_title, email="", *, allow_account_change=False, path=None):
    """Bind a profile to an account and one authenticated channel in one DB transaction."""
    email = (email or "").strip()
    normalized = email.casefold()
    if not normalized:
        raise ValueError("Google 계정 이메일을 확인하지 못했습니다. 계정 확인 전에는 연결을 저장하지 않았습니다.")
    if channel_id and not (channel_id.startswith("UC") and len(channel_id) > 4):
        raise ValueError("올바르지 않은 YouTube 채널 ID입니다.")
    if not (channel_title or "").strip():
        raise ValueError("표시할 Google 계정 또는 채널 이름이 필요합니다.")
    with connect(path) as db:
        profile = db.execute("SELECT google_account_id,connected_email,youtube_channel_id FROM profiles WHERE id=?",
                             (profile_id,)).fetchone()
        if not profile:
            raise ValueError("연결할 프로필을 찾을 수 없습니다.")
        account = db.execute("SELECT id FROM google_accounts WHERE normalized_email=?", (normalized,)).fetchone()
        account_id = account["id"] if account else uuid.uuid4().hex
        if not account:
            db.execute("INSERT INTO google_accounts(id,email,normalized_email) VALUES(?,?,?)",
                       (account_id,email,normalized))
        old_account = profile["google_account_id"] or ""
        old_email = (profile["connected_email"] or "").strip().casefold()
        old_channel = profile["youtube_channel_id"] or ""
        changed = ((old_account and old_account != account_id) or
                   (old_email and old_email != normalized) or
                   (old_channel and old_channel != (channel_id or "")))
        if changed and not allow_account_change:
            raise ValueError("이미 연결된 Google 계정 또는 채널과 다릅니다. 기존 연결 변경을 명시적으로 확인하세요.")
        if changed:
            has_history = any(db.execute(sql,(profile_id,)).fetchone() for sql in (
                "SELECT 1 FROM candidates WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM genre_audit WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM ai_reviews WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM metrics WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM research_sessions WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM research_schedules WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM autopilot_reports WHERE profile_id=? LIMIT 1",
                "SELECT 1 FROM autopilot_comment_drafts WHERE profile_id=? LIMIT 1",
            ))
            if has_history:
                raise ValueError("이 프로필에는 기존 조사 자료가 있어 다른 계정/채널로 바꾸지 않았습니다. 자료 격리를 위해 새 프로필을 사용하세요.")
            active_session = db.execute(
                "SELECT 1 FROM research_sessions WHERE profile_id=? AND state IN ('running','paused','interrupted') LIMIT 1",
                (profile_id,)).fetchone()
            active_schedule = db.execute(
                "SELECT 1 FROM research_schedules WHERE profile_id=? AND state IN ('scheduled','running','paused') LIMIT 1",
                (profile_id,)).fetchone()
            if active_session or active_schedule:
                raise ValueError("진행 중이거나 이어하기 대기 중인 조사가 있어 계정 연결을 바꾸지 않았습니다. 먼저 해당 작업을 안전하게 중단하거나 완료하세요.")
        db.execute("INSERT INTO google_account_channels(account_id,channel_id,channel_title) VALUES(?,?,?) "
                   "ON CONFLICT(account_id,channel_id) DO UPDATE SET channel_title=excluded.channel_title, "
                   "updated_at=CURRENT_TIMESTAMP", (account_id,channel_id or "",channel_title.strip()))
        db.execute("UPDATE google_accounts SET email=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (email,account_id))
        db.execute("UPDATE profiles SET google_account_id=?,youtube_channel_id=?,youtube_channel_title=?, "
                   "connected_email=?,connected_at=CURRENT_TIMESTAMP WHERE id=?",
                   (account_id,channel_id or "",channel_title.strip(),email,profile_id))
    return account_id

def update_profile(profile_id, *, name, market, genre, path=None, subgenre=None, related_genres=None):
    name = (name or "").strip()
    if market not in MARKETS or genre not in GENRES:
        raise ValueError("국가와 장르를 확인하세요.")
    if not 2 <= len(name) <= 70:
        raise ValueError("프로필 이름은 2~70자여야 합니다.")
    with connect(path) as db:
        old = db.execute("SELECT market,genre,related_genres_json FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not old:
            raise ValueError("프로필을 찾을 수 없습니다.")
        related = normalize_related_genres(genre, old["related_genres_json"] if related_genres is None else related_genres)
        count = db.execute("SELECT COUNT(*) FROM candidates WHERE profile_id=?", (profile_id,)).fetchone()[0]
        if count and (old["market"] != market or old["genre"] != genre):
            raise ValueError("후보가 저장된 프로필의 국가·장르는 변경할 수 없습니다. 새 프로필을 추가하세요.")
        old_related = json.loads(old["related_genres_json"] or "[]")
        if (old["genre"] != genre or old_related != related) and db.execute(
                "SELECT 1 FROM research_sessions WHERE profile_id=? AND state IN ('paused','interrupted','running') LIMIT 1",
                (profile_id,)).fetchone():
            raise ValueError("An active research session uses the current genre settings. Finish or cancel it before changing genres.")
        assignments = "name=?,market=?,genre=?,related_genres_json=?"
        values = [name, market, genre, json.dumps(related, ensure_ascii=False)]
        if subgenre is not None:
            assignments += ",subgenre=?"; values.append((subgenre or "").strip()[:100])
        db.execute("UPDATE profiles SET " + assignments + " WHERE id=?", (*values, profile_id))

def update_schedule(profile_id, schedule, path=None):
    import re
    parts = [value.strip() for value in schedule.split(",") if value.strip()]
    if not parts or len(parts) > 6 or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in parts):
        raise ValueError("예약 시간은 07:00,20:00 형식으로 최대 6개 입력하세요.")
    with connect(path) as db:
        db.execute("UPDATE profiles SET schedule=? WHERE id=?", (",".join(dict.fromkeys(parts)),profile_id))

def save_candidates(profile_id, rows, path=None):
    profile = get_profile(profile_id, path)
    if not profile:
        raise ValueError("선택한 프로필을 찾을 수 없습니다.")
    values, audit_values, stats = [], [], {"적합":0, "검토":0, "제외":0}
    related_snapshot = json.dumps(normalize_related_genres(profile["genre"], profile.get("related_genres_json", "[]")), ensure_ascii=False)
    for row in rows:
        video_id = str(row.get("video_id", ""))
        if len(video_id) != 11 or not all(char.isalnum() or char in "-_" for char in video_id):
            continue
        title = str(row.get("video_title", ""))[:1000]
        description = str(row.get("description", ""))[:10000]
        verdict = assess(profile["genre"], title, description)
        stats[verdict.decision] += 1
        audit_values.append((profile_id,video_id,title,description,
          profile["market"],profile["genre"],verdict.decision,verdict.score,verdict.reason,
          " | ".join(verdict.positive_signals)," | ".join(verdict.conflict_signals),verdict.uncertainty))
        if verdict.decision == EXCLUDE:
            continue
        values.append((profile_id,video_id,title,str(row.get("channel_title",""))[:500],
          str(row.get("channel_id",""))[:200],f"https://www.youtube.com/watch?v={video_id}",description,
          str(row.get("published_at",""))[:100],int(row.get("views",0) or 0),str(row.get("country",""))[:20],
          str(row.get("keyword",""))[:200],verdict.decision,verdict.score,verdict.reason,
          profile["genre"],related_snapshot))
    with connect(path) as db:
        db.executemany("""INSERT INTO genre_audit(profile_id,video_id,video_title,description,market,
          target_genre,predicted_decision,predicted_score,reason,positive_signals,conflict_signals,uncertainty)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id,video_id) DO UPDATE SET
          video_title=excluded.video_title,description=excluded.description,market=excluded.market,
          target_genre=excluded.target_genre,predicted_decision=excluded.predicted_decision,
          predicted_score=excluded.predicted_score,reason=excluded.reason,
          positive_signals=excluded.positive_signals,conflict_signals=excluded.conflict_signals,
          uncertainty=excluded.uncertainty,updated_at=CURRENT_TIMESTAMP""", audit_values)
        db.executemany("""INSERT INTO candidates(profile_id,video_id,video_title,channel_title,channel_id,
          video_url,description,published_at,views,country,keyword,genre_decision,genre_score,genre_reason,
          classified_genre,related_genres_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id,video_id) DO UPDATE SET
          video_title=excluded.video_title,channel_title=excluded.channel_title,channel_id=excluded.channel_id,
          views=excluded.views,description=excluded.description,country=excluded.country,
          keyword=excluded.keyword,published_at=excluded.published_at,
          genre_reason=CASE WHEN candidates.genre_decision=? THEN candidates.genre_reason ELSE excluded.genre_reason END,
          genre_score=CASE WHEN candidates.genre_decision=? THEN candidates.genre_score ELSE excluded.genre_score END,
          genre_decision=CASE WHEN candidates.genre_decision=? THEN candidates.genre_decision ELSE excluded.genre_decision END,
          updated_at=CURRENT_TIMESTAMP""", [value + (MANUAL_APPROVAL,MANUAL_APPROVAL,MANUAL_APPROVAL) for value in values])
    return stats

def list_candidates(profile_id, query="", decision="전체", path=None):
    if not profile_id:
        return []
    sql, args = "SELECT * FROM candidates WHERE profile_id=?", [profile_id]
    if decision != "전체":
        sql += " AND genre_decision=?"; args.append(decision)
    if query.strip():
        sql += " AND (video_title LIKE ? OR channel_title LIKE ? OR keyword LIKE ?)"; args.extend(["%"+query.strip()+"%"]*3)
    sql += " ORDER BY genre_score DESC,updated_at DESC,video_id DESC"
    with connect(path) as db:
        return [dict(row) for row in db.execute(sql,args)]

def get_candidate(profile_id, video_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT * FROM candidates WHERE profile_id=? AND video_id=?", (profile_id,video_id)).fetchone()
        return dict(row) if row else None

def update_candidate(profile_id, video_id, status, notes, draft, path=None):
    if status not in ("미확인", "검토 중", "보류", "완료"):
        raise ValueError("올바르지 않은 후보 상태입니다.")
    with connect(path) as db:
        result = db.execute("UPDATE candidates SET status=?,notes=?,draft=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=? AND video_id=?", (status,notes,draft,profile_id,video_id))
        return result.rowcount == 1

def approve_review(profile_id, video_id, reason, path=None):
    if len((reason or "").strip()) < 20:
        raise ValueError("장르 예외 승인 사유를 20자 이상 적어주세요.")
    with connect(path) as db:
        row = db.execute("SELECT genre_decision FROM candidates WHERE profile_id=? AND video_id=?", (profile_id,video_id)).fetchone()
        if not row or row[0] != REVIEW:
            raise ValueError("검토 상태의 후보만 수동 승인할 수 있습니다.")
        db.execute("UPDATE candidates SET genre_decision=?,genre_reason=?,updated_at=CURRENT_TIMESTAMP WHERE profile_id=? AND video_id=?", (MANUAL_APPROVAL,"수동 승인: "+reason.strip(),profile_id,video_id))

def upsert_metric(profile_id, day, all_views, target_views, impressions, ctr, avg_minutes, subscribers, path=None):
    datetime.strptime(day, "%Y-%m-%d")
    values = (all_views,target_views,impressions,subscribers)
    if any(int(value) < 0 for value in values) or not 0 <= float(ctr) <= 100 or float(avg_minutes) < 0:
        raise ValueError("성과 값은 0 이상이고 CTR은 0~100이어야 합니다.")
    with connect(path) as db:
        db.execute("""INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(profile_id,day)
          DO UPDATE SET all_views=excluded.all_views,target_views=excluded.target_views,
          impressions=excluded.impressions,ctr=excluded.ctr,avg_minutes=excluded.avg_minutes,
          subscribers=excluded.subscribers""", (profile_id,day,all_views,target_views,impressions,ctr,avg_minutes,subscribers))

def list_metrics(profile_id, path=None):
    with connect(path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM metrics WHERE profile_id=? ORDER BY day DESC", (profile_id,))]

def export_csv(profile_id, kind, target, path=None):
    if kind == "candidates":
        headers = ("video_id","video_title","channel_title","video_url","genre_decision","genre_score","status","notes","draft")
        table = "candidates"
    elif kind == "metrics":
        headers = ("day","all_views","target_views","impressions","ctr","avg_minutes","subscribers")
        table = "metrics"
    else:
        raise ValueError("내보내기 종류가 잘못되었습니다.")
    with connect(path) as db:
        rows = db.execute(f"SELECT {','.join(headers)} FROM {table} WHERE profile_id=?", (profile_id,)).fetchall()
    with open(target, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream); writer.writerow(headers); writer.writerows([tuple(row) for row in rows])
    return len(rows)

def backup(target, path=None):
    initialize(path)
    with connect(path) as source, closing(sqlite3.connect(target)) as destination:
        source.backup(destination)

def bind_youtube_channel(profile_id, channel_id, channel_title, email="", path=None, *, allow_account_change=False):
    return bind_google_account_channel(profile_id,channel_id,channel_title,email,
                                       allow_account_change=allow_account_change,path=path)

def unbind_youtube_channel(profile_id, path=None):
    with connect(path) as db:
        db.execute("UPDATE profiles SET google_account_id='',youtube_channel_id='',youtube_channel_title='',connected_email='',connected_at='' WHERE id=?", (profile_id,))

def list_genre_audit(profile_id, decision="전체", labeled=None, path=None):
    sql, args = "SELECT * FROM genre_audit WHERE profile_id=?", [profile_id]
    if decision != "전체":
        sql += " AND predicted_decision=?"; args.append(decision)
    if labeled is True:
        sql += " AND human_label<>''"
    elif labeled is False:
        sql += " AND human_label=''"
    sql += " ORDER BY updated_at DESC,video_id"
    with connect(path) as db:
        return [dict(row) for row in db.execute(sql,args)]

def label_genre_audit(profile_id, video_id, label, notes="", path=None):
    if label not in ("적합", "제외", "불확실"):
        raise ValueError("사람 판정은 적합, 제외 또는 불확실이어야 합니다.")
    if label != "불확실" and len((notes or "").strip()) < 5:
        raise ValueError("판정 근거를 5자 이상 기록하세요.")
    with connect(path) as db:
        result = db.execute("""UPDATE genre_audit SET human_label=?,label_notes=?,review_status='human_reviewed',
          reviewed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
          WHERE profile_id=? AND video_id=?""", (label,(notes or "").strip(),profile_id,video_id))
        if result.rowcount != 1:
            raise ValueError("장르 검토 표본을 찾을 수 없습니다.")

def record_ai_decision(profile_id, video_id, *, rule_verdict, ai_verdict="", proposed_verdict="",
                       evidence="", uncertainty="", model_version="", path=None):
    """Persist machine opinion separately; never changes a human label or rule verdict."""
    allowed = {"auto_fit", "cross_genre_review", "related_reference", "exclude", "insufficient_evidence"}
    if proposed_verdict not in allowed:
        raise ValueError("Invalid proposed verdict.")
    with connect(path) as db:
        result = db.execute("""UPDATE genre_audit SET rule_verdict=?,ai_verdict=?,proposed_verdict=?,
          evidence=?,uncertainty=?,model_version=?,decision_at=CURRENT_TIMESTAMP,
          review_status=CASE WHEN human_label='' THEN 'needs_review' ELSE review_status END,
          updated_at=CURRENT_TIMESTAMP WHERE profile_id=? AND video_id=?""",
          (rule_verdict, ai_verdict, proposed_verdict, evidence, uncertainty, model_version,
           profile_id, video_id))
        if result.rowcount != 1:
            raise ValueError("Audit row does not exist.")

def genre_quality(profile_id, path=None):
    rows = list_genre_audit(profile_id, labeled=True, path=path)
    determined = [row for row in rows if row["human_label"] in ("적합","제외")]
    accepted = [row for row in determined if row["predicted_decision"] in ("적합","수동 승인")]
    excluded = [row for row in determined if row["predicted_decision"] == "제외"]
    precision = (sum(row["human_label"] == "적합" for row in accepted) / len(accepted)) if accepted else None
    exclusion_error = (sum(row["human_label"] == "적합" for row in excluded) / len(excluded)) if excluded else None
    return {"labeled":len(rows),"determined":len(determined),"accepted":len(accepted),
            "excluded":len(excluded),"precision":precision,"exclusion_error_rate":exclusion_error}

def set_profile_search_terms(profile_id, market, terms, path=None):
    if market not in MARKETS:
        raise ValueError("시장 코드는 JP 또는 KR이어야 합니다.")
    cleaned = list(dict.fromkeys(str(term).strip() for term in terms if str(term).strip()))
    if not cleaned or len(cleaned) > 50 or any(len(term) > 100 for term in cleaned):
        raise ValueError("검색어는 1~50개, 각 100자 이하로 입력하세요.")
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM profiles WHERE id=?",(profile_id,)).fetchone():
            raise ValueError("프로필을 찾을 수 없습니다.")
        db.execute("DELETE FROM profile_search_terms WHERE profile_id=? AND market=?",(profile_id,market))
        db.executemany("INSERT INTO profile_search_terms(profile_id,market,term) VALUES(?,?,?)",
                       [(profile_id,market,term) for term in cleaned])

def list_profile_search_terms(profile_id, market, path=None):
    with connect(path) as db:
        return [row[0] for row in db.execute("""SELECT term FROM profile_search_terms
          WHERE profile_id=? AND market=? AND enabled=1 ORDER BY rowid""",(profile_id,market))]

def get_ai_review(profile_id, video_id, metadata_hash, genre, provider, model, path=None, cache_scope=""):
    """Read a cached AI opinion only inside the exact profile and provenance scope."""
    with connect(path) as db:
        row = db.execute("""SELECT * FROM ai_reviews WHERE profile_id=? AND video_id=? AND metadata_hash=?
          AND genre=? AND provider=? AND model=? AND prompt_version=?""",
          (profile_id,video_id,metadata_hash,genre,provider,model,"genre-review-v1" + cache_scope)).fetchone()
        return dict(row) if row else None

def save_ai_review(profile_id, video_id, metadata_hash, genre, provider, model, opinion, latency_ms=0, path=None, cache_scope=""):
    """Add/update an AI cache row; never touches human labels or candidate decisions."""
    import json
    if opinion.verdict not in ("적합", "검토", "제외"):
        raise ValueError("AI 판정 값이 올바르지 않습니다.")
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM profiles WHERE id=?",(profile_id,)).fetchone():
            raise ValueError("선택한 프로필을 찾을 수 없습니다.")
        db.execute("""INSERT INTO ai_reviews(profile_id,video_id,metadata_hash,genre,provider,model,prompt_version,
          ai_verdict,positive_signals,conflict_signals,evidence,uncertainty,human_check,latency_ms)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(profile_id,video_id,metadata_hash,genre,provider,model,prompt_version) DO UPDATE SET
          ai_verdict=excluded.ai_verdict,positive_signals=excluded.positive_signals,
          conflict_signals=excluded.conflict_signals,evidence=excluded.evidence,
          uncertainty=excluded.uncertainty,human_check=excluded.human_check,latency_ms=excluded.latency_ms""",
          (profile_id,video_id,metadata_hash,genre,provider,model,"genre-review-v1" + cache_scope,opinion.verdict,
           json.dumps(opinion.positive_signals,ensure_ascii=False),json.dumps(opinion.conflict_signals,ensure_ascii=False),
           json.dumps(opinion.evidence_from_metadata,ensure_ascii=False),opinion.uncertainty,
           opinion.recommended_human_check,max(0,int(latency_ms))))

def reserve_ai_usage(profile_id, provider, model, session_id="", *,
                     max_job_requests=10, max_daily_requests=30, cost_estimate=None,
                     cost_limit=None, path=None):
    """Atomically account an outbound attempt before dispatch; unknown cost stays unknown."""
    if provider not in ("openai", "gemini", "ollama", "mock"):
        raise ValueError("AI 공급자를 확인하세요.")
    if provider in ("openai", "gemini"):
        try:
            cost_estimate, cost_limit = float(cost_estimate), float(cost_limit)
        except (TypeError, ValueError, OverflowError):
            return False
        if (not math.isfinite(cost_estimate) or not math.isfinite(cost_limit) or
                cost_estimate <= 0 or cost_limit <= 0 or cost_estimate > cost_limit):
            return False
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone():
            raise ValueError("조사 프로필을 확인하세요.")
        # SQLite CURRENT_TIMESTAMP stores UTC. Use the same calendar basis for
        # daily request/cost limits or UTC/local midnight can bypass the cap.
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = db.execute("SELECT COUNT(*) FROM ai_usage WHERE profile_id=? AND provider=? AND created_at LIKE ?",
                           (profile_id, provider, day + "%")).fetchone()[0]
        job = db.execute("SELECT COUNT(*) FROM ai_usage WHERE profile_id=? AND provider=? AND session_id=?",
                         (profile_id, provider, session_id)).fetchone()[0] if session_id else 0
        if daily >= max_daily_requests or job >= max_job_requests:
            return False
        if provider in ("openai", "gemini"):
            # Do not invent vendor prices. Require the operator's own conservative
            # per-call estimate and ceiling; unknown historic spend fails closed.
            spent = db.execute("SELECT SUM(cost_estimate) FROM ai_usage WHERE profile_id=? AND provider=? AND created_at LIKE ?",
                               (profile_id, provider, day + "%")).fetchone()[0]
            if spent is None and daily:
                return False
            spent = spent or 0.0
            job_spent = db.execute("SELECT SUM(cost_estimate) FROM ai_usage WHERE profile_id=? AND provider=? AND session_id=?",
                                   (profile_id, provider, session_id)).fetchone()[0] if session_id else 0.0
            if spent + cost_estimate > cost_limit or (job_spent or 0.0) + cost_estimate > cost_limit:
                return False
        db.execute("INSERT INTO ai_usage(profile_id,provider,model,session_id,status,cost_estimate) VALUES(?,?,?,?,?,?)",
                   (profile_id, provider, model, session_id or "", "attempted", cost_estimate))
        return True

def ai_usage_summary(profile_id, path=None):
    with connect(path) as db:
        rows = db.execute("SELECT provider,COUNT(*) AS requests,SUM(CASE WHEN provider IN ('openai','gemini') AND cost_estimate IS NULL THEN 1 ELSE 0 END) AS unknown_cost "
                          "FROM ai_usage WHERE profile_id=? AND created_at LIKE ? GROUP BY provider",
                          (profile_id, datetime.now(timezone.utc).strftime("%Y-%m-%d") + "%")).fetchall()
        return {row["provider"]: {"requests": row["requests"], "unknown_cost": row["unknown_cost"]} for row in rows}

def ai_usage_for_session(profile_id, session_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT COUNT(*) AS requests,SUM(cost_estimate) AS estimated_cost_usd,"
                         "SUM(CASE WHEN cost_estimate IS NULL THEN 1 ELSE 0 END) AS unknown_cost "
                         "FROM ai_usage WHERE profile_id=? AND session_id=?", (profile_id, session_id)).fetchone()
    return {"requests": int(row["requests"] or 0),
            "estimated_cost_usd": float(row["estimated_cost_usd"] or 0.0),
            "unknown_cost": int(row["unknown_cost"] or 0)}

def start_research_session(profile_id, market, genre, target, max_search_calls, path=None, settings_hash="", target_mode="candidate_count"):
    session_id = uuid.uuid4().hex
    with connect(path) as db:
        # Serialize the check + insert across processes. The in-process GUI lock
        # alone cannot prevent a second app instance from spending the same budget.
        db.execute("BEGIN IMMEDIATE")
        if target_mode == "fit_target" and db.execute(
            "SELECT 1 FROM research_sessions WHERE profile_id=? AND target_mode='fit_target' AND state='running' LIMIT 1",
            (profile_id,),
        ).fetchone():
            raise ActiveResearchSessionError("A fit-target research session is already running for this profile.")
        row = db.execute("SELECT market,genre FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row or row["market"] != market or row["genre"] != genre:
            raise ValueError("조사 프로필이 변경되었습니다. 새 계획을 만드세요.")
        _add_column(db, "research_sessions", "settings_hash TEXT NOT NULL DEFAULT ''")
        db.execute("INSERT INTO research_sessions(id,profile_id,market,genre,state,target,max_search_calls,settings_hash,target_mode) VALUES(?,?,?,?,?,?,?,?,?)",
                   (session_id, profile_id, market, genre, "running", target, max_search_calls, settings_hash,target_mode))
    return session_id

def get_research_session(session_id, profile_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT * FROM research_sessions WHERE id=? AND profile_id=?", (session_id, profile_id)).fetchone()
        return dict(row) if row else None

def list_resumable_sessions(profile_id, path=None):
    with connect(path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM research_sessions WHERE profile_id=? AND state IN ('paused','interrupted','running') ORDER BY updated_at DESC", (profile_id,))]

def checkpoint_research_session(session_id, profile_id, *, state=None, query_index=None,
                                processed=None, candidates=None, reviews=None, in_flight=None,
                                page_token=None, calls_used=None, duplicate_count=None, path=None):
    import json
    fields, values = [], []
    for key, value in (("state", state), ("query_index", query_index),
                       ("processed_json", json.dumps(processed, ensure_ascii=False) if processed is not None else None),
                       ("candidates_json", json.dumps(candidates, ensure_ascii=False) if candidates is not None else None),
                       ("reviews_json", json.dumps(reviews, ensure_ascii=False) if reviews is not None else None),
                       ("in_flight", int(in_flight) if in_flight is not None else None),
                       ("page_token", page_token), ("calls_used", calls_used),
                       ("duplicate_count", duplicate_count)):
        if value is not None:
            fields.append(key + "=?"); values.append(value)
    fields.append("updated_at=CURRENT_TIMESTAMP")
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM research_sessions WHERE id=? AND profile_id=?", (session_id, profile_id)).fetchone():
            raise ValueError("조사 세션과 프로필이 일치하지 않습니다.")
        db.execute("UPDATE research_sessions SET " + ",".join(fields) + " WHERE id=? AND profile_id=?",
                   (*values, session_id, profile_id))

def save_autopilot_settings(profile_id, session_id, settings, path=None):
    """Persist non-secret resume settings for an Autopilot session."""
    allowed = {"provider_id", "model", "target", "max_search_calls", "cost_estimate_usd",
               "cost_limit_usd", "paid_external_approved", "report_version"}
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError("Invalid Autopilot settings.")
    payload = json.dumps(settings, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM research_sessions WHERE id=? AND profile_id=?",
                          (session_id, profile_id)).fetchone():
            raise ValueError("Autopilot session does not match the profile.")
        db.execute("INSERT INTO autopilot_run_settings(session_id,profile_id,settings_json) VALUES(?,?,?) "
                   "ON CONFLICT(session_id) DO UPDATE SET settings_json=excluded.settings_json,updated_at=CURRENT_TIMESTAMP",
                   (session_id, profile_id, payload))

def get_autopilot_settings(profile_id, session_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT settings_json FROM autopilot_run_settings WHERE profile_id=? AND session_id=?",
                         (profile_id, session_id)).fetchone()
    return json.loads(row["settings_json"]) if row else None

def save_autopilot_report(profile_id, session_id, report, exceptions=(), path=None):
    report_json = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
    exceptions_json = json.dumps(list(exceptions), ensure_ascii=False, sort_keys=True, allow_nan=False)
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM research_sessions WHERE id=? AND profile_id=?",
                          (session_id, profile_id)).fetchone():
            raise ValueError("Autopilot report does not match the profile session.")
        db.execute("INSERT INTO autopilot_reports(session_id,profile_id,report_json,exceptions_json) VALUES(?,?,?,?) "
                   "ON CONFLICT(session_id) DO UPDATE SET report_json=excluded.report_json,"
                   "exceptions_json=excluded.exceptions_json,updated_at=CURRENT_TIMESTAMP",
                   (session_id, profile_id, report_json, exceptions_json))

def get_autopilot_report(profile_id, session_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT report_json,exceptions_json FROM autopilot_reports WHERE profile_id=? AND session_id=?",
                         (profile_id, session_id)).fetchone()
    if not row:
        return None
    return {"report": json.loads(row["report_json"]), "exceptions": json.loads(row["exceptions_json"])}

def get_latest_autopilot_report(profile_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT report_json,exceptions_json FROM autopilot_reports WHERE profile_id=? "
                         "ORDER BY updated_at DESC,session_id DESC LIMIT 1", (profile_id,)).fetchone()
    if not row:
        return None
    return {"report": json.loads(row["report_json"]), "exceptions": json.loads(row["exceptions_json"])}

def save_autopilot_comment_drafts(profile_id, video_id, session_id, drafts, path=None):
    if not isinstance(drafts, (list, tuple)) or len(drafts) != 3 or any(not isinstance(x, str) or len(x) > 400 for x in drafts):
        raise ValueError("Autopilot requires exactly three bounded comment drafts.")
    with connect(path) as db:
        if not db.execute("SELECT 1 FROM candidates WHERE profile_id=? AND video_id=?", (profile_id, video_id)).fetchone():
            raise ValueError("Comment draft candidate does not belong to this profile.")
        db.execute("INSERT INTO autopilot_comment_drafts(profile_id,video_id,session_id,drafts_json) VALUES(?,?,?,?) "
                   "ON CONFLICT(profile_id,video_id,session_id) DO UPDATE SET drafts_json=excluded.drafts_json",
                   (profile_id, video_id, session_id, json.dumps(list(drafts), ensure_ascii=False)))

def list_autopilot_comment_drafts(profile_id, video_id=None, path=None):
    sql = "SELECT * FROM autopilot_comment_drafts WHERE profile_id=?"
    args = [profile_id]
    if video_id is not None:
        sql += " AND video_id=?"; args.append(video_id)
    sql += " ORDER BY created_at DESC,video_id"
    with connect(path) as db:
        return [{**dict(row), "drafts": json.loads(row["drafts_json"])} for row in db.execute(sql, args)]
