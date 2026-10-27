"""Profile-isolated, read-only YouTube Analytics reporting.

This module deliberately keeps Analytics consent separate from the existing
YouTube Data API OAuth token. It never writes to YouTube and never fabricates
missing metrics. HTTP is injectable so contract and isolation tests stay offline.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import hashlib
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import store
from .work_slots import SEARCH_SLOT, occupy, WorkSlotBusy
from .youtube_api import classify_network_exception, _network_signature

READONLY_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
ALLOWED_SCOPES = {READONLY_SCOPE, YOUTUBE_READONLY_SCOPE, EMAIL_SCOPE, "openid", "email"}
API_ROOT = "https://youtubeanalytics.googleapis.com/v2/reports"
LOCAL_PROFILE_DAILY_CALL_LIMIT = 40
LOCAL_PROJECT_DAILY_CALL_LIMIT = 200
_PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{8,}$")


class AnalyticsError(Exception):
    """Allowlisted Analytics failure; the raw provider payload is never retained."""
    MESSAGES = {
        "consent_required": "성과 분석을 위해 Google의 읽기 전용 동의가 필요합니다.",
        "auth_required": "Google 연결이 만료되었습니다. 해당 채널에서 다시 동의해 주세요.",
        "permission_denied": "이 Google 계정에 선택한 채널의 성과 조회 권한이 없습니다.",
        "quota_exhausted": "공유 Google API 프로젝트의 할당량 한도에 도달했습니다. 사용량을 확인한 뒤 기다려 주세요.",
        "rate_limited": "Google이 잠시 요청을 제한했습니다. 저장된 결과를 유지하고 기다려 주세요.",
        "server_error": "Google 성과 서비스를 일시적으로 사용할 수 없습니다.",
        "network_error": "인터넷 연결 문제로 성과를 가져오지 못했습니다. 기존 결과는 보존했습니다.",
        "dns_failure": "인터넷 주소를 찾지 못했습니다. 연결 상태를 확인해 주세요.",
        "network_timeout": "Google 응답이 늦어 요청 결과를 확인할 수 없습니다. 자동으로 다시 요청하지 않았습니다.",
        "network_offline": "인터넷 연결이 끊겼습니다. 조사 기록은 저장되어 있습니다.",
        "network_changed": "네트워크 연결이 바뀌어 요청을 멈췄습니다. VPN 또는 Wi-Fi 연결을 확인해 주세요.",
        "connection_failed": "Google 성과 서비스에 연결하지 못했습니다. 연결 상태를 확인해 주세요.",
        "invalid_response": "Google 성과 응답 형식을 확인할 수 없어 저장하지 않았습니다.",
        "request_limit": "ZISU의 성과 조회 안전 한도에 도달했습니다.",
        "resource_busy": "컴퓨터가 바쁘거나 다른 요청이 진행 중이라 잠시 기다립니다.",
        "account_mismatch": "연결된 Google 계정 또는 채널이 현재 프로필과 달라 조회를 중단했습니다.",
        "storage_error": "성과 결과를 안전하게 저장하지 못했습니다. 이전 기록은 유지했습니다.",
    }

    def __init__(self, code: str):
        self.code = code if code in self.MESSAGES else "invalid_response"
        super().__init__(self.MESSAGES[self.code])


REPORTS = {
    "daily": {"dimensions": "day", "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained,subscribersLost"},
    "videos": {"dimensions": "video", "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage"},
    "countries": {"dimensions": "country", "metrics": "views,estimatedMinutesWatched"},
    "traffic": {"dimensions": "insightTrafficSourceType", "metrics": "views,estimatedMinutesWatched"},
}


def date_window(days: int, *, today: date | None = None) -> tuple[str, str]:
    if isinstance(days, bool) or days not in (7, 28, 90):
        raise ValueError("Choose a 7, 28 or 90 day report window.")
    end = today or date.today()
    # YouTube reporting data is not guaranteed for the current day. Use the
    # previous complete day so no incomplete current-day totals are implied.
    end -= timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def previous_date_window(days: int, *, today: date | None = None) -> tuple[str, str]:
    current_start, _ = date_window(days, today=today)
    end = date.fromisoformat(current_start) - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def build_query(channel_id: str, report: str, start_date: str, end_date: str) -> dict[str, str]:
    if not isinstance(channel_id, str) or not _CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("A valid, explicitly linked YouTube channel is required.")
    if report not in REPORTS:
        raise ValueError("Unsupported Analytics report.")
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except (TypeError, ValueError):
        raise ValueError("Invalid report dates.") from None
    if start > end or end >= date.today():
        raise ValueError("Analytics dates must end before today.")
    spec = REPORTS[report]
    return {"ids": f"channel=={channel_id}", "startDate": start.isoformat(),
            "endDate": end.isoformat(), "dimensions": spec["dimensions"],
            "metrics": spec["metrics"], "sort": "day" if report == "daily" else "-views",
            "maxResults": "200", "alt": "json"}


def parse_report(report: str, payload: object) -> dict:
    """Validate response headers/rows while retaining missing values as None."""
    if report not in REPORTS or not isinstance(payload, dict):
        raise AnalyticsError("invalid_response")
    headers = payload.get("columnHeaders")
    rows = payload.get("rows", [])
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise AnalyticsError("invalid_response")
    names = [item.get("name") for item in headers if isinstance(item, dict)]
    expected = [REPORTS[report]["dimensions"], *REPORTS[report]["metrics"].split(",")]
    if names != expected:
        raise AnalyticsError("invalid_response")
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            raise AnalyticsError("invalid_response")
        parsed.append(dict(zip(names, row)))
    return {"state": "data" if parsed else "no_data", "rows": parsed,
            "source": "youtube_analytics_reports_query", "metric_names": expected}


def classify_http(status: int, response_body: bytes | str | None = None) -> str:
    if status in (401,):
        return "auth_required"
    if status == 403:
        try:
            raw = response_body.decode("utf-8") if isinstance(response_body, bytes) else response_body
            payload = json.loads(raw or "{}")
            reasons = {item.get("reason", "") for item in payload.get("error", {}).get("errors", [])
                       if isinstance(item, dict)}
            if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                return "quota_exhausted"
            if reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
                return "rate_limited"
        except (ValueError, UnicodeError, TypeError, AttributeError):
            pass
        return "permission_denied"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "invalid_response"


def _normalize_scopes(scopes):
    if isinstance(scopes, str):
        scopes = scopes.split()
    normalized = set()
    for scope in scopes or ():
        value = str(scope).strip()
        if value == "email":
            value = EMAIL_SCOPE
        if value:
            normalized.add(value)
    return normalized


def _recover_scope_warning(warning, client_config, credentials_class):
    """Accept only the known OIDC email alias; fail closed on scope changes."""
    requested = {READONLY_SCOPE, YOUTUBE_READONLY_SCOPE, "openid", EMAIL_SCOPE}
    if _normalize_scopes(getattr(warning, "old_scope", ())) != requested:
        raise AnalyticsError("consent_required") from None
    granted = _normalize_scopes(getattr(warning, "new_scope", ()))
    token_payload = getattr(warning, "token", None)
    if granted != requested or READONLY_SCOPE not in granted or not isinstance(token_payload, dict):
        raise AnalyticsError("consent_required") from None
    installed = client_config.get("installed", {})
    token = token_payload.get("access_token") or token_payload.get("token")
    if not token or not installed.get("client_id") or not installed.get("client_secret"):
        raise AnalyticsError("consent_required") from None
    info = {"token": token, "refresh_token": token_payload.get("refresh_token"),
            "token_uri": installed.get("token_uri"), "client_id": installed.get("client_id"),
            "client_secret": installed.get("client_secret"), "scopes": sorted(granted)}
    try:
        return credentials_class.from_authorized_user_info(info, scopes=sorted(granted)), sorted(granted)
    except Exception:
        raise AnalyticsError("consent_required") from None


def _safe_json_response(response):
    try:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise AnalyticsError("invalid_response")
        return json.loads(raw.decode("utf-8"))
    except AnalyticsError:
        raise
    except Exception:
        raise AnalyticsError("invalid_response") from None


class AnalyticsRepository:
    """Additive, profile/channel-scoped cache and conservative call ledger."""
    def __init__(self, path=None, *, clock=None):
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with store.connect(path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS youtube_analytics_cache(
              profile_id TEXT NOT NULL, channel_id TEXT NOT NULL, report_type TEXT NOT NULL,
              window_days INTEGER NOT NULL, fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL,
              PRIMARY KEY(profile_id,channel_id,report_type,window_days),
              FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE)""")
            db.execute("""CREATE TABLE IF NOT EXISTS youtube_analytics_calls(
              request_id TEXT PRIMARY KEY, project_hash TEXT NOT NULL, profile_id TEXT NOT NULL,
              channel_id TEXT NOT NULL, report_type TEXT NOT NULL, local_day TEXT NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_analytics_call_day ON youtube_analytics_calls(project_hash,local_day,profile_id)")

    def get(self, profile_id, channel_id, report, days, *, max_age_seconds=21600):
        with store.connect(self.path) as db:
            row = db.execute("SELECT fetched_at,payload_json FROM youtube_analytics_cache WHERE profile_id=? AND channel_id=? AND report_type=? AND window_days=?",
                             (profile_id, channel_id, report, days)).fetchone()
        if not row:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if (self.clock() - fetched).total_seconds() > max_age_seconds:
                return None
            data = json.loads(row["payload_json"])
            data["fetched_at"] = fetched.isoformat()
            return data
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def put(self, profile_id, channel_id, report, days, payload):
        safe = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        fetched = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with store.connect(self.path) as db:
                db.execute("INSERT INTO youtube_analytics_cache(profile_id,channel_id,report_type,window_days,fetched_at,payload_json) VALUES(?,?,?,?,?,?) ON CONFLICT(profile_id,channel_id,report_type,window_days) DO UPDATE SET fetched_at=excluded.fetched_at,payload_json=excluded.payload_json",
                           (profile_id, channel_id, report, days, fetched, safe))
        except Exception:
            raise AnalyticsError("storage_error") from None

    def reserve(self, profile_id, channel_id, report, project_hash, *, profile_limit=LOCAL_PROFILE_DAILY_CALL_LIMIT,
                project_limit=LOCAL_PROJECT_DAILY_CALL_LIMIT):
        if not _PROFILE_ID.fullmatch(str(profile_id or "")) or not project_hash:
            raise AnalyticsError("account_mismatch")
        today = self.clock().date().isoformat()
        request_id = uuid.uuid4().hex
        with store.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            project_used = db.execute("SELECT COUNT(*) FROM youtube_analytics_calls WHERE project_hash=? AND local_day=?", (project_hash, today)).fetchone()[0]
            profile_used = db.execute("SELECT COUNT(*) FROM youtube_analytics_calls WHERE profile_id=? AND local_day=?", (profile_id, today)).fetchone()[0]
            if project_used >= project_limit or profile_used >= profile_limit:
                raise AnalyticsError("request_limit")
            db.execute("INSERT INTO youtube_analytics_calls VALUES(?,?,?,?,?,?,?,?)",
                       (request_id, project_hash, profile_id, channel_id, report, today, "reserved", self.clock().isoformat()))
        return request_id

    def finish(self, request_id, status):
        if status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("Invalid Analytics call state.")
        with store.connect(self.path) as db:
            return db.execute("UPDATE youtube_analytics_calls SET status=? WHERE request_id=? AND status='reserved'", (status, request_id)).rowcount == 1


class AnalyticsClient:
    def __init__(self, token_provider, *, transport=None, repository=None,
                 resource_guard=None, project_hash="", profile_daily_limit=LOCAL_PROFILE_DAILY_CALL_LIMIT,
                 project_daily_limit=LOCAL_PROJECT_DAILY_CALL_LIMIT):
        self.token_provider = token_provider
        self.transport = transport or self._http_get
        self.repository = repository or AnalyticsRepository()
        self.resource_guard = resource_guard
        self.project_hash = project_hash
        self.profile_daily_limit = profile_daily_limit
        self.project_daily_limit = project_daily_limit

    @staticmethod
    def _http_get(url, headers):
        request = Request(url, headers=headers, method="GET")
        return urlopen(request, timeout=20)

    def _query(self, token, profile_id, channel_id, report, days, start, end):
        params = build_query(channel_id, report, start, end)
        request_id = self.repository.reserve(profile_id, channel_id, report, self.project_hash,
                    profile_limit=self.profile_daily_limit, project_limit=self.project_daily_limit)
        try:
            if self.resource_guard is not None:
                decision = self.resource_guard.check()
                if not decision.allow_new_work:
                    self.repository.finish(request_id, "failed")
                    raise AnalyticsError("resource_busy")
            with occupy(SEARCH_SLOT, timeout=0.1):
                url = API_ROOT + "?" + urlencode(params)
                network_before = _network_signature()
                try:
                    response = self.transport(url, {"Authorization": "Bearer " + token,
                                                     "Accept": "application/json",
                                                     "User-Agent": "ZisuResearchStudio/4.6"})
                    with response:
                        payload = _safe_json_response(response)
                except HTTPError as exc:
                    try:
                        # Parse only allowlisted reason codes; never retain or expose provider text.
                        safe_code = classify_http(exc.code, exc.read(65_536))
                    except Exception:
                        safe_code = classify_http(exc.code)
                    self.repository.finish(request_id, "failed")
                    exc.close()
                    raise AnalyticsError(safe_code) from None
                except (TimeoutError, URLError, OSError) as exc:
                    code, uncertain = classify_network_exception(
                        exc, before=network_before, after=_network_signature())
                    self.repository.finish(request_id, "uncertain" if uncertain else "failed")
                    raise AnalyticsError(code) from None
                parsed = parse_report(report, payload)
                self.repository.put(profile_id, channel_id, report, days, parsed)
                self.repository.finish(request_id, "succeeded")
                return parsed
        except WorkSlotBusy:
            self.repository.finish(request_id, "failed")
            raise AnalyticsError("resource_busy") from None
        except AnalyticsError:
            self.repository.finish(request_id, "failed")
            raise
        except Exception:
            self.repository.finish(request_id, "uncertain")
            raise AnalyticsError("invalid_response") from None

    def fetch(self, profile: dict, days: int, *, today: date | None = None, use_cache=True) -> dict:
        if not isinstance(profile, dict) or not profile.get("id"):
            raise AnalyticsError("account_mismatch")
        channel_id = profile.get("youtube_channel_id") or ""
        email = str(profile.get("connected_email") or "").strip().casefold()
        if not email or not _CHANNEL_ID.fullmatch(channel_id):
            raise AnalyticsError("account_mismatch")
        start, end = date_window(days, today=today)
        output = {"profile_id": profile["id"], "channel_id": channel_id, "window_days": days,
                  "start_date": start, "end_date": end, "source": "YouTube Analytics API reports.query",
                  "state": "data", "reports": {}, "suggestions": []}
        for report in REPORTS:
            cached = self.repository.get(profile["id"], channel_id, report, days) if use_cache else None
            if cached is not None:
                output["reports"][report] = {**cached, "cache_state": "reused"}
                continue
            try:
                token_data = self.token_provider(profile["id"])
                token = token_data.get("access_token", "")
                scopes = set(token_data.get("scopes", ()))
                token_email = str(token_data.get("email", "")).strip().casefold()
                if not token or READONLY_SCOPE not in scopes or not scopes.issubset(ALLOWED_SCOPES):
                    raise AnalyticsError("consent_required")
                if token_email != email or token_data.get("channel_id") != channel_id:
                    raise AnalyticsError("account_mismatch")
                output["reports"][report] = {**self._query(token, profile["id"], channel_id, report, days, start, end),
                                             "cache_state": "fresh"}
            except AnalyticsError as exc:
                output["state"] = exc.code
                output["message"] = str(exc)
                break
        if output["state"] == "data":
            prior_start, prior_end = previous_date_window(days, today=today)
            prior = self.repository.get(profile["id"], channel_id, "daily", -days) if use_cache else None
            prior_cache_state = prior is not None
            if prior is None:
                try:
                    token_data = self.token_provider(profile["id"])
                    token = token_data.get("access_token", "")
                    scopes = set(token_data.get("scopes", ()))
                    if not token or READONLY_SCOPE not in scopes or not scopes.issubset(ALLOWED_SCOPES):
                        raise AnalyticsError("consent_required")
                    if (str(token_data.get("email", "")).strip().casefold() != email or
                            token_data.get("channel_id") != channel_id):
                        raise AnalyticsError("account_mismatch")
                    prior = self._query(token, profile["id"], channel_id, "daily", -days,
                                        prior_start, prior_end)
                except AnalyticsError as exc:
                    output["state"] = exc.code
                    output["message"] = str(exc)
            if prior is not None:
                output["reports"]["daily_previous"] = {**prior,
                    "cache_state": "reused" if prior_cache_state else "fresh"}
        output["suggestions"] = build_suggestions(output["reports"])
        return output


def build_suggestions(reports: dict) -> list[dict]:
    """Create transparent hypotheses only from observed report rows."""
    suggestions = []
    videos = reports.get("videos", {}).get("rows", [])
    scored = []
    for row in videos:
        value = row.get("views")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scored.append((float(value), str(row.get("video", ""))))
    if len(scored) >= 2:
        scored.sort(reverse=True)
        suggestions.append({"kind": "measured_fact", "text": f"조회 데이터가 있는 영상 {len(scored)}개를 조회수 기준으로 비교했습니다."})
        suggestions.append({"kind": "hypothesis", "text": "조회수가 높은 영상과 낮은 영상의 제목·주제를 사람이 비교해 볼 후보입니다. 이 차이만으로 원인을 단정할 수 없습니다."})
        suggestions.append({"kind": "unavailable", "text": "썸네일 노출·클릭률은 별도 Reach 보고서를 연결하지 않아 개선 방향을 판단할 수 없습니다."})
        suggestions.append({"kind": "unavailable", "text": "업로드 시각과 장르별 성과는 현재 보고서 표본에 없어 비교하지 않았습니다."})
    daily = reports.get("daily", {}).get("rows", [])
    if daily:
        suggestions.append({"kind": "hypothesis", "text": "일별 시청시간과 평균 시청 지속시간을 함께 살펴볼 검토 가설입니다. 특정 이탈 구간은 별도 상세 보고 없이는 단정할 수 없습니다."})
    return suggestions


class AnalyticsCredentialManager:
    """Dedicated per-profile DPAPI vault, separate from Data API OAuth tokens."""
    PROVIDER = "youtube-analytics"

    def __init__(self, directory=None, credential_store_factory=None):
        from .credential_store import CredentialStore
        self.directory = directory or (store.data_dir() / "analytics_credentials")
        self.credential_store_factory = credential_store_factory or CredentialStore

    def _store(self, profile_id):
        if not _PROFILE_ID.fullmatch(str(profile_id or "")):
            raise AnalyticsError("account_mismatch")
        return self.credential_store_factory(directory=self.directory / profile_id)

    def configured(self, profile_id):
        try:
            return self._store(profile_id).configured(self.PROVIDER)
        except Exception:
            return False

    def read(self, profile_id):
        try:
            raw = self._store(profile_id).load(self.PROVIDER)
            return json.loads(raw) if raw else None
        except Exception:
            raise AnalyticsError("auth_required") from None

    def save(self, profile_id, credentials_json, *, email="", channel_id="", scopes=()):
        try:
            credentials = json.loads(credentials_json)
            envelope = {"credentials": credentials, "email": str(email).strip().casefold(),
                        "channel_id": str(channel_id), "scopes": sorted(set(scopes))}
            self._store(profile_id).save(self.PROVIDER, json.dumps(envelope, separators=(",", ":")))
        except AnalyticsError:
            raise
        except Exception:
            raise AnalyticsError("storage_error") from None

    def token_data(self, profile):
        """Load/refresh only this profile's DPAPI token after a user starts fetch."""
        envelope = self.read(profile.get("id"))
        if not isinstance(envelope, dict):
            raise AnalyticsError("consent_required")
        if (envelope.get("email") != str(profile.get("connected_email") or "").strip().casefold()
                or envelope.get("channel_id") != profile.get("youtube_channel_id")):
            raise AnalyticsError("account_mismatch")
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request as RefreshRequest
            scopes = set(envelope.get("scopes") or ())
            if READONLY_SCOPE not in scopes or not scopes.issubset(ALLOWED_SCOPES):
                raise AnalyticsError("consent_required")
            credentials = Credentials.from_authorized_user_info(envelope["credentials"], scopes=sorted(scopes))
            if not credentials.valid:
                if not credentials.expired or not credentials.refresh_token:
                    raise AnalyticsError("auth_required")
                credentials.refresh(RefreshRequest())
                envelope["credentials"] = json.loads(credentials.to_json())
                self._store(profile["id"]).save(self.PROVIDER, json.dumps(envelope, separators=(",", ":")))
            return {"access_token": credentials.token, "scopes": sorted(scopes),
                    "email": envelope["email"], "channel_id": envelope["channel_id"]}
        except AnalyticsError:
            raise
        except Exception:
            raise AnalyticsError("auth_required") from None


def begin_analytics_consent(profile: dict, client_config: dict, *, approved: bool,
                            credential_manager: AnalyticsCredentialManager,
                            identity_loader=None, flow_factory=None):
    """Obtain incremental Analytics read-only consent after explicit user action.

    This function is never called automatically at startup. Tests inject both
    the OAuth package and identity loader and therefore perform no network IO.
    """
    if not approved:
        return {"state": "cancelled"}
    if not profile or not profile.get("connected_email") or not profile.get("youtube_channel_id"):
        raise AnalyticsError("account_mismatch")
    try:
        if flow_factory is None:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow_factory = InstalledAppFlow.from_client_config
        from .oauth import _get_email, _get_my_channels, _packages
        scopes = [READONLY_SCOPE, YOUTUBE_READONLY_SCOPE, "openid", EMAIL_SCOPE]
        flow = flow_factory(client_config, scopes=scopes, autogenerate_code_verifier=True)
        try:
            credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True,
                                                 prompt="select_account", access_type="offline",
                                                 include_granted_scopes="true")
            granted = sorted(_normalize_scopes(getattr(credentials, "granted_scopes", None)
                              or getattr(credentials, "scopes", None) or ()))
        except Warning as warning:
            _, Credentials, _ = _packages()
            credentials, granted = _recover_scope_warning(warning, client_config, Credentials)
        expected = sorted({READONLY_SCOPE, YOUTUBE_READONLY_SCOPE, "openid", EMAIL_SCOPE})
        if granted != expected or READONLY_SCOPE not in granted or not set(granted).issubset(ALLOWED_SCOPES):
            raise AnalyticsError("consent_required")
        if identity_loader is None:
            email = _get_email(credentials.token)
            channels = _get_my_channels(credentials.token)
        else:
            email, channels = identity_loader(credentials.token)
        expected_email = str(profile["connected_email"]).strip().casefold()
        channel_id = profile["youtube_channel_id"]
        if str(email or "").strip().casefold() != expected_email or channel_id not in {x.get("channel_id") for x in channels}:
            raise AnalyticsError("account_mismatch")
        credential_manager.save(profile["id"], credentials.to_json(), email=email,
                                channel_id=channel_id, scopes=granted)
        return {"state": "connected", "profile_id": profile["id"], "channel_id": channel_id,
                "scope": READONLY_SCOPE}
    except AnalyticsError:
        raise
    except Exception:
        raise AnalyticsError("auth_required") from None


def project_scope_hash(project_identity: str) -> str:
    """One-way local bucket key; never persist the OAuth client identity."""
    value = str(project_identity or "unknown-google-project").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def dashboard_view(profile_id: str, channel_id: str, days: int, repository: AnalyticsRepository) -> dict:
    """Profile-scoped cached data for the beginner dashboard; absent stays unavailable."""
    reports = {name: repository.get(profile_id, channel_id, name, days) for name in REPORTS}
    reports["daily_previous"] = repository.get(profile_id, channel_id, "daily", -days)
    daily = reports.get("daily")
    videos = reports.get("videos")
    countries = reports.get("countries")
    traffic = reports.get("traffic")
    result = {"state": "cached" if any(reports.values()) else "not_collected",
              "window_days": days, "source": "YouTube Analytics API reports.query",
              "metrics": {}, "top_videos": [], "countries": [], "traffic_sources": [],
              "suggestions": build_suggestions({k: v for k, v in reports.items() if v})}
    if daily and daily.get("state") == "data":
        rows = daily.get("rows", [])
        previous = reports.get("daily_previous")
        previous_rows = previous.get("rows", []) if previous and previous.get("state") == "data" else []
        current_views = _sum_numeric(rows, "views")
        previous_views = _sum_numeric(previous_rows, "views")
        current_watch = _sum_numeric(rows, "estimatedMinutesWatched")
        previous_watch = _sum_numeric(previous_rows, "estimatedMinutesWatched")
        result["metrics"] = {
            "views": current_views,
            "views_change": current_views - previous_views if current_views is not None and previous_views is not None else None,
            "views_change_percent": (100.0 * (current_views - previous_views) / previous_views) if current_views is not None and previous_views else None,
            "watch_minutes": current_watch,
            "watch_minutes_change": current_watch - previous_watch if current_watch is not None and previous_watch is not None else None,
            "avg_view_duration_seconds": _weighted_mean(rows, "averageViewDuration", "views"),
            "avg_view_percentage": _weighted_mean(rows, "averageViewPercentage", "views"),
            "subscriber_change": (_sum_numeric(rows, "subscribersGained") - _sum_numeric(rows, "subscribersLost"))
                                  if _sum_numeric(rows, "subscribersGained") is not None and _sum_numeric(rows, "subscribersLost") is not None else None,
        }
        result["state"] = "data" if rows else "no_data"
    elif daily and daily.get("state") == "no_data":
        result["state"] = "no_data"
    if videos and videos.get("state") == "data":
        result["top_videos"] = sorted(videos["rows"], key=lambda r: r.get("views", -1), reverse=True)[:3]
        result["low_videos"] = sorted(videos["rows"], key=lambda r: r.get("views", -1))[:3]
    if countries and countries.get("state") == "data":
        result["countries"] = sorted(countries["rows"], key=lambda r: r.get("views", -1), reverse=True)[:5]
    if traffic and traffic.get("state") == "data":
        result["traffic_sources"] = sorted(traffic["rows"], key=lambda r: r.get("views", -1), reverse=True)[:5]
    result["impressions_ctr"] = {"state": "not_collected",
        "message": "노출수와 썸네일 클릭률은 reports.query에서 가져오지 않았습니다. 별도 YouTube Reporting API Reach 연동은 아직 없습니다."}
    return result


def _weighted_mean(rows, metric, weight_key):
    if not rows or any(not isinstance(row.get(metric), (int, float)) or isinstance(row.get(metric), bool)
                       or not isinstance(row.get(weight_key), (int, float)) or isinstance(row.get(weight_key), bool)
                       for row in rows):
        return None
    pairs = [(float(row[metric]), float(row.get(weight_key))) for row in rows]
    denominator = sum(weight for _, weight in pairs)
    return sum(value * weight for value, weight in pairs) / denominator if denominator > 0 else None


def _sum_numeric(rows, field):
    if not rows or any(not isinstance(row.get(field), (int, float)) or isinstance(row.get(field), bool)
                       for row in rows):
        return None
    return sum(row[field] for row in rows)
