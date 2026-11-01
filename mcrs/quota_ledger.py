"""Conservative, local estimate of YouTube Data API quota use.

The ledger stores request metadata and a one-way hash of a supplied project
identity/API credential, never the credential itself. Defaults follow the
official YouTube Data API quota calculator (search.list 100 calls/day in its
own bucket; videos.list/channels.list 1 unit each in the other-endpoint
default bucket; midnight Pacific reset), but remain configurable estimates and
are not authoritative Google Cloud quota telemetry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import uuid

from . import store

PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_SEARCH_CALL_LIMIT = 100
DEFAULT_OTHER_UNIT_LIMIT = 10_000
REQUESTS = {
    "search": ("search_queries", 1),
    "videos": ("other_units", 1),
    "channels": ("other_units", 1),
}


class QuotaLimitExceeded(RuntimeError):
    def __init__(self, bucket: str, used: int, requested: int, limit: int):
        self.code = "quota_local_limit"
        self.bucket = bucket
        self.used = used
        self.requested = requested
        self.limit = limit
        super().__init__("The locally configured YouTube quota estimate would be exceeded.")


def ensure_schema(path=None):
    with store.connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS youtube_quota_ledger(
          request_id TEXT PRIMARY KEY, project_scope_hash TEXT NOT NULL,
          profile_id TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
          request_type TEXT NOT NULL, bucket TEXT NOT NULL, estimated_units INTEGER NOT NULL,
          quota_day_pt TEXT NOT NULL, status TEXT NOT NULL,
          error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          finished_at TEXT NOT NULL DEFAULT '')""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_youtube_quota_project_day "
                   "ON youtube_quota_ledger(project_scope_hash,quota_day_pt,bucket,status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_youtube_quota_profile_session "
                   "ON youtube_quota_ledger(profile_id,session_id,created_at)")
        # A reservation left behind by a crashed process remains charged and is
        # shown as uncertain after a grace period longer than HTTP timeouts.
        db.execute("UPDATE youtube_quota_ledger SET status='uncertain',"
                   "error_code='unknown_network',finished_at=CURRENT_TIMESTAMP "
                   "WHERE status='reserved' AND datetime(created_at)<datetime('now','-3 minutes')")


def scope_hash(project_identity: str) -> str:
    value = (project_identity or "").strip()
    if not value:
        raise ValueError("A non-secret quota project identity is required.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def quota_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(PACIFIC).date().isoformat()


def reserve(project_identity: str, profile_id: str, request_type: str, path=None, *,
            session_id="", search_limit=DEFAULT_SEARCH_CALL_LIMIT,
            unit_limit=DEFAULT_OTHER_UNIT_LIMIT, now=None) -> str:
    request_type = (request_type or "").strip().lower()
    if request_type not in REQUESTS:
        raise ValueError("Unsupported YouTube API request type.")
    if not profile_id:
        raise ValueError("A profile is required for quota accounting.")
    for value in (search_limit, unit_limit):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("Quota limits must be positive integers.")
    bucket, units = REQUESTS[request_type]
    project_hash = scope_hash(project_identity)
    day = quota_day(now)
    request_id = uuid.uuid4().hex
    ensure_schema(path)
    with store.connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        if bucket == "search_queries":
            used = int(db.execute("SELECT COUNT(*) FROM youtube_quota_ledger "
                "WHERE project_scope_hash=? AND quota_day_pt=? AND bucket=?",
                (project_hash, day, bucket)).fetchone()[0])
            limit = search_limit
        else:
            used = int(db.execute("SELECT COALESCE(SUM(estimated_units),0) FROM youtube_quota_ledger "
                "WHERE project_scope_hash=? AND quota_day_pt=? AND bucket=?",
                (project_hash, day, bucket)).fetchone()[0])
            limit = unit_limit
        if used + units > limit:
            raise QuotaLimitExceeded(bucket, used, units, limit)
        db.execute("INSERT INTO youtube_quota_ledger(request_id,project_scope_hash,profile_id,session_id,"
            "request_type,bucket,estimated_units,quota_day_pt,status) VALUES(?,?,?,?,?,?,?,?, 'reserved')",
            (request_id, project_hash, profile_id, str(session_id or "")[:128], request_type,
             bucket, units, day))
    return request_id


def finish(request_id: str, status: str, path=None, *, error_code=""):
    if status not in {"succeeded", "failed", "uncertain"}:
        raise ValueError("Invalid quota request status.")
    safe_code = error_code if error_code in {
        "auth_required", "permission_denied", "quota_exhausted", "rate_limited",
        "server_error", "dns_failure", "network_timeout", "network_offline",
        "network_changed", "connection_failed", "invalid_response", "unknown_network",
    } else ""
    ensure_schema(path)
    with store.connect(path) as db:
        changed = db.execute("UPDATE youtube_quota_ledger SET status=?,error_code=?,finished_at=CURRENT_TIMESTAMP "
            "WHERE request_id=? AND status='reserved'", (status, safe_code, request_id)).rowcount
    return changed == 1


def summary(project_identity: str, path=None, *, profile_id="", session_id="", now=None,
            search_limit=DEFAULT_SEARCH_CALL_LIMIT, unit_limit=DEFAULT_OTHER_UNIT_LIMIT):
    project_hash = scope_hash(project_identity)
    day = quota_day(now)
    ensure_schema(path)
    filters = ["project_scope_hash=?", "quota_day_pt=?"]
    args: list[object] = [project_hash, day]
    if profile_id:
        filters.append("profile_id=?"); args.append(profile_id)
    where = " AND ".join(filters)
    with store.connect(path) as db:
        rows = db.execute(f"SELECT bucket,status,COUNT(*) AS calls,SUM(estimated_units) AS units "
            f"FROM youtube_quota_ledger WHERE {where} GROUP BY bucket,status", args).fetchall()
        session_rows = []
        if session_id:
            session_filters = ["project_scope_hash=?", "profile_id=?", "session_id=?"]
            session_args: list[object] = [project_hash, profile_id, session_id]
            session_rows = db.execute("SELECT bucket,COUNT(*) AS calls,SUM(estimated_units) AS units "
                "FROM youtube_quota_ledger WHERE " + " AND ".join(session_filters) + " GROUP BY bucket",
                session_args).fetchall()
    result = {"quota_day_pt": day, "authoritative": False,
              "search_calls_used_estimate": 0, "other_units_used_estimate": 0,
              "session_search_calls_used_estimate": 0, "session_other_units_used_estimate": 0,
              "reserved": 0, "succeeded": 0, "failed": 0, "uncertain": 0,
              "search_calls_limit_configured": search_limit,
              "other_units_limit_configured": unit_limit}
    for row in rows:
        if row["bucket"] == "search_queries":
            result["search_calls_used_estimate"] += int(row["calls"])
        else:
            result["other_units_used_estimate"] += int(row["units"] or 0)
        result[row["status"]] += int(row["calls"])
    for row in session_rows:
        if row["bucket"] == "search_queries":
            result["session_search_calls_used_estimate"] += int(row["calls"])
        else:
            result["session_other_units_used_estimate"] += int(row["units"] or 0)
    result["search_calls_remaining_estimate"] = max(0, search_limit - result["search_calls_used_estimate"])
    result["other_units_remaining_estimate"] = max(0, unit_limit - result["other_units_used_estimate"])
    return result
