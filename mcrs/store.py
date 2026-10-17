"""Backward-compatible, profile-isolated SQLite storage (credentials excluded)."""
from __future__ import annotations
import csv, os, sqlite3, uuid
from contextlib import contextmanager, closing
from datetime import datetime
from pathlib import Path
from .genres import GENRES, EXCLUDE, MANUAL_APPROVAL, REVIEW, assess

MARKETS = ("JP", "KR")

def data_dir() -> Path:
    override = os.environ.get("ZISU_DATA_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()/".local"/"share")/"ZisuResearchStudio"

def db_path() -> Path:
    return data_dir()/"zisu_v2.db"

@contextmanager
def connect(path=None):
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
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
        db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          market TEXT NOT NULL CHECK(market IN ('JP','KR')), genre TEXT NOT NULL,
          schedule TEXT NOT NULL DEFAULT '07:00,20:00',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS candidates (
          profile_id TEXT NOT NULL, video_id TEXT NOT NULL, video_title TEXT NOT NULL,
          channel_title TEXT NOT NULL DEFAULT '', channel_id TEXT NOT NULL DEFAULT '',
          video_url TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          published_at TEXT NOT NULL DEFAULT '', views INTEGER NOT NULL DEFAULT 0,
          country TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
          genre_decision TEXT NOT NULL, genre_score INTEGER NOT NULL,
          genre_reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT '미확인',
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
        CREATE INDEX IF NOT EXISTS idx_candidates_status
          ON candidates(profile_id,genre_decision,status);
        """)
        for definition in (
            "youtube_channel_id TEXT NOT NULL DEFAULT ''",
            "youtube_channel_title TEXT NOT NULL DEFAULT ''",
            "connected_at TEXT NOT NULL DEFAULT ''",
            "connected_email TEXT NOT NULL DEFAULT ''",
        ):
            _add_column(db, "profiles", definition)

def create_profile(name, market, genre, path=None):
    initialize(path)
    name = (name or "").strip()
    if not 2 <= len(name) <= 70:
        raise ValueError("프로필 이름은 2~70자로 입력하세요.")
    if market not in MARKETS or genre not in GENRES:
        raise ValueError("국가와 장르를 확인하세요.")
    profile_id = uuid.uuid4().hex
    with connect(path) as db:
        db.execute("INSERT INTO profiles(id,name,market,genre) VALUES(?,?,?,?)", (profile_id,name,market,genre))
    return profile_id

def list_profiles(path=None):
    initialize(path)
    with connect(path) as db:
        return [dict(row) for row in db.execute("SELECT * FROM profiles ORDER BY created_at,id")]

def get_profile(profile_id, path=None):
    with connect(path) as db:
        row = db.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None

def update_profile(profile_id, *, name, market, genre, path=None):
    name = (name or "").strip()
    if market not in MARKETS or genre not in GENRES:
        raise ValueError("국가와 장르를 확인하세요.")
    if not 2 <= len(name) <= 70:
        raise ValueError("프로필 이름은 2~70자여야 합니다.")
    with connect(path) as db:
        old = db.execute("SELECT market,genre FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not old:
            raise ValueError("프로필을 찾을 수 없습니다.")
        count = db.execute("SELECT COUNT(*) FROM candidates WHERE profile_id=?", (profile_id,)).fetchone()[0]
        if count and (old["market"] != market or old["genre"] != genre):
            raise ValueError("후보가 저장된 프로필의 국가·장르는 변경할 수 없습니다. 새 프로필을 추가하세요.")
        db.execute("UPDATE profiles SET name=?,market=?,genre=? WHERE id=?", (name,market,genre,profile_id))

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
    values, stats = [], {"적합":0, "검토":0, "제외":0}
    for row in rows:
        video_id = str(row.get("video_id", ""))
        if len(video_id) != 11 or not all(char.isalnum() or char in "-_" for char in video_id):
            continue
        verdict = assess(profile["genre"], str(row.get("video_title", "")), str(row.get("description", "")))
        stats[verdict.decision] += 1
        if verdict.decision == EXCLUDE:
            continue
        values.append((profile_id,video_id,str(row.get("video_title","")),str(row.get("channel_title","")),
          str(row.get("channel_id","")),f"https://www.youtube.com/watch?v={video_id}",str(row.get("description","")),
          str(row.get("published_at","")),int(row.get("views",0) or 0),str(row.get("country","")),
          str(row.get("keyword","")),verdict.decision,verdict.score,verdict.reason))
    with connect(path) as db:
        db.executemany("""INSERT INTO candidates(profile_id,video_id,video_title,channel_title,channel_id,
          video_url,description,published_at,views,country,keyword,genre_decision,genre_score,genre_reason)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile_id,video_id) DO UPDATE SET
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

def bind_youtube_channel(profile_id, channel_id, channel_title, email="", path=None):
    if channel_id and not (channel_id.startswith("UC") and len(channel_id) > 4):
        raise ValueError("올바르지 않은 YouTube 채널 ID입니다.")
    if not channel_title.strip():
        raise ValueError("표시할 Google 계정 또는 채널 이름이 필요합니다.")
    with connect(path) as db:
        result = db.execute("UPDATE profiles SET youtube_channel_id=?,youtube_channel_title=?,connected_email=?,connected_at=CURRENT_TIMESTAMP WHERE id=?", (channel_id,channel_title,email,profile_id))
        if result.rowcount != 1:
            raise ValueError("연결할 프로필을 찾을 수 없습니다.")

def unbind_youtube_channel(profile_id, path=None):
    with connect(path) as db:
        db.execute("UPDATE profiles SET youtube_channel_id='',youtube_channel_title='',connected_email='',connected_at='' WHERE id=?", (profile_id,))
