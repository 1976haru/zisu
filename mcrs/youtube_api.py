"""Read-only YouTube Data API adapter with safe error and quota accounting."""
from __future__ import annotations

import errno
import hashlib
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import quota_ledger
from .work_slots import SEARCH_SLOT, WorkSlotBusy, occupy


class APIError(Exception):
    SAFE_MESSAGES = {
        "auth_required": "Google 계정 연결이 필요합니다. 이 프로필의 읽기 전용 연결을 확인하세요.",
        "permission_denied": "YouTube API 권한 또는 사용 설정을 확인하세요.",
        "quota_exhausted": "Google Cloud의 YouTube 검색 할당량이 소진되었습니다.",
        "quota_local_limit": "설정한 예상 할당량 한도에 도달했습니다. Google Cloud 사용량을 확인하세요.",
        "rate_limited": "요청이 잠시 제한되었습니다. 자동 재시도하지 않았습니다.",
        "server_error": "Google API 서버가 일시적으로 응답하지 않았습니다.",
        "dns_failure": "인터넷 주소를 찾지 못했습니다. 인터넷 연결을 확인하세요.",
        "network_timeout": "인터넷 응답이 늦어 요청 결과를 확인할 수 없습니다.",
        "network_offline": "인터넷 연결이 끊겼습니다. 연결을 확인하세요.",
        "network_changed": "네트워크 연결이 변경되었습니다. VPN이나 Wi-Fi 연결을 확인하세요.",
        "connection_failed": "Google API에 연결하지 못했습니다. 인터넷 연결을 확인하세요.",
        "invalid_response": "Google API 응답을 읽지 못했습니다. 데이터를 저장하고 중지했습니다.",
        "api_error": "YouTube API 요청을 완료하지 못했습니다.",
        "search_busy": "다른 검색을 마치는 중입니다. 저장된 작업을 잠시 뒤 이어가세요.",
    }

    def __init__(self, message=None, *, code="api_error", http_status=None,
                 endpoint="", request_uncertain=False, retryable=False):
        self.code = code if code in self.SAFE_MESSAGES else "api_error"
        self.http_status = http_status if isinstance(http_status, int) else None
        self.endpoint = endpoint if endpoint in {"search", "videos", "channels"} else ""
        self.request_uncertain = bool(request_uncertain)
        self.retryable = bool(retryable)
        super().__init__(self.SAFE_MESSAGES[self.code])


def quota_project_identity(api_key="", oauth_identity=""):
    """Return a stable, non-secret grouping identity for local quota estimates."""
    if isinstance(oauth_identity, str) and oauth_identity:
        return oauth_identity
    value = (api_key or "").strip()
    return "api-key:" + hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _network_signature():
    """Hash active local interface addresses for this request only; never persist."""
    try:
        import psutil
        interfaces, states = psutil.net_if_addrs(), psutil.net_if_stats()
        rows = []
        for name, addresses in interfaces.items():
            if not states.get(name) or not states[name].isup:
                continue
            ips = sorted(item.address.split("%", 1)[0] for item in addresses
                         if item.family in (socket.AF_INET, socket.AF_INET6)
                         and item.address and not item.address.startswith(("127.", "::1")))
            if ips:
                rows.append((name, ips))
        return hashlib.sha256(repr(sorted(rows)).encode("utf-8")).hexdigest() if rows else ""
    except Exception:
        return ""


def classify_network_exception(exc, *, before="", after=""):
    if before and after and before != after:
        return "network_changed", True
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "dns_failure", False
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "network_timeout", True
    # Winsock exposes WSA* codes (10050+) where POSIX uses errno.ENET*.
    offline_errnos = {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
                      errno.ECONNREFUSED, 10050, 10051, 10061, 10065}
    if isinstance(reason, OSError) and reason.errno in offline_errnos:
        return "network_offline", False
    if isinstance(reason, (ConnectionResetError, BrokenPipeError)):
        return "network_timeout", True
    return "connection_failed", True


def _api_error(exc):
    """Normalize HTTP errors using safe status/reason codes only."""
    try:
        raw = exc.read(65536)
        payload = json.loads(raw.decode("utf-8")).get("error", {})
        reasons = {item.get("reason", "") for item in payload.get("errors", [])
                   if isinstance(item, dict)}
        status = int(exc.code)
    except (ValueError, UnicodeError, TypeError, AttributeError):
        reasons = set()
        status = int(getattr(exc, "code", 0) or 0)
    try:
        exc.close()
    except Exception:
        pass
    if status == 401:
        code = "auth_required"
    elif status == 403 and reasons & {"quotaExceeded", "dailyLimitExceeded"}:
        code = "quota_exhausted"
    elif status == 403 and reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        code = "rate_limited"
    elif status == 403:
        code = "permission_denied"
    elif status == 429:
        code = "rate_limited"
    elif status >= 500:
        code = "server_error"
    else:
        code = "api_error"
    uncertain = status >= 500
    return APIError(code=code, http_status=status, request_uncertain=uncertain,
                    retryable=status in {429, 500, 502, 503, 504})


def _get(endpoint, key, access_token=None, retries=0, retry_delay=0.25,
         quota_context=None, **kwargs):
    retries = max(0, min(2, int(retries)))
    retry_delay = max(0.0, min(2.0, float(retry_delay)))
    if not access_token and not (key or "").strip():
        raise APIError(code="auth_required")
    params = dict(kwargs)
    if not access_token:
        params["key"] = key.strip()
    url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urlencode(params)
    headers = {"User-Agent": "ZisuResearchStudio/4.6"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    for attempt in range(retries + 1):
        reservation = ""
        if quota_context:
            identity = quota_context.get("project_identity") or (
                "api-key:" + hashlib.sha256((key or "").strip().encode("utf-8")).hexdigest()
                if key else "profile:" + str(quota_context.get("profile_id", "")))
            try:
                reservation = quota_ledger.reserve(identity, quota_context.get("profile_id", ""),
                    endpoint, quota_context.get("path"), session_id=quota_context.get("session_id", ""),
                    search_limit=quota_context.get("search_limit", quota_ledger.DEFAULT_SEARCH_CALL_LIMIT),
                    unit_limit=quota_context.get("unit_limit", quota_ledger.DEFAULT_OTHER_UNIT_LIMIT))
            except quota_ledger.QuotaLimitExceeded as exc:
                raise APIError(code="quota_local_limit", request_uncertain=False) from exc
        network_before = _network_signature()
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                try:
                    payload = json.load(response)
                except (ValueError, UnicodeError):
                    if reservation:
                        quota_ledger.finish(reservation, "uncertain", quota_context.get("path"),
                                            error_code="invalid_response")
                    raise APIError(code="invalid_response", endpoint=endpoint,
                                   request_uncertain=True) from None
                if reservation:
                    quota_ledger.finish(reservation, "succeeded", quota_context.get("path"))
                return payload
        except HTTPError as exc:
            failure = _api_error(exc)
            failure.endpoint = endpoint
            if reservation:
                quota_ledger.finish(reservation, "uncertain" if failure.request_uncertain else "failed",
                                    quota_context.get("path"), error_code=failure.code)
            if failure.retryable and not failure.request_uncertain and attempt < retries:
                time.sleep(max(0.0, min(2.0, retry_delay)) * (2 ** attempt))
                continue
            raise failure from None
        except (URLError, TimeoutError, OSError) as exc:
            code, uncertain = classify_network_exception(
                exc, before=network_before, after=_network_signature())
            if reservation:
                quota_ledger.finish(reservation, "uncertain" if uncertain else "failed",
                                    quota_context.get("path"), error_code=code)
            if not uncertain and attempt < retries:
                time.sleep(max(0.0, min(2.0, retry_delay)) * (2 ** attempt))
                continue
            raise APIError(code=code, endpoint=endpoint, request_uncertain=uncertain,
                           retryable=not uncertain) from None
    raise APIError(code="api_error", endpoint=endpoint, request_uncertain=True)


def _lookup_videos(key, keyword, market, limit=12, access_token=None, page_token=None,
                   return_page=False, order="relevance", quota_context=None):
    if market not in ("KR", "JP"):
        raise ValueError("시장 코드는 KR 또는 JP입니다.")
    if order not in {"relevance", "date"}:
        raise ValueError("Search order must be relevance or date.")
    lang = "ko" if market == "KR" else "ja"
    credentials = {"access_token": access_token} if access_token else {}
    context = {"quota_context": quota_context} if quota_context else {}
    results = _get("search", key, part="snippet", type="video", q=keyword,
        regionCode=market, relevanceLanguage=lang, order=order,
        maxResults=max(1, min(50, limit)), retries=0,
        **({"pageToken": page_token} if page_token else {}), **credentials, **context)
    items = [item for item in results.get("items", []) if item.get("id", {}).get("videoId")]
    if not items:
        return {"items": [], "nextPageToken": results.get("nextPageToken", "")} if return_page else []
    video_ids = [item["id"]["videoId"] for item in items]
    channel_ids = list(dict.fromkeys(item.get("snippet", {}).get("channelId", "") for item in items))
    try:
        videos = _get("videos", key, part="statistics", id=",".join(video_ids),
                      **credentials, **context)
        channels = _get("channels", key, part="snippet", id=",".join(value for value in channel_ids if value),
                        **credentials, **context)
    except APIError as exc:
        exc.request_uncertain = True
        raise
    counts = {item["id"]: int(item.get("statistics", {}).get("viewCount", 0) or 0)
              for item in videos.get("items", [])}
    countries = {item["id"]: item.get("snippet", {}).get("country", "")
                 for item in channels.get("items", [])}
    rows = [{"video_id": item["id"]["videoId"],
             "video_title": item.get("snippet", {}).get("title", ""),
             "channel_id": item.get("snippet", {}).get("channelId", ""),
             "channel_title": item.get("snippet", {}).get("channelTitle", ""),
             "description": item.get("snippet", {}).get("description", ""),
             "published_at": item.get("snippet", {}).get("publishedAt", ""),
             "views": counts.get(item["id"]["videoId"], 0),
             "country": countries.get(item.get("snippet", {}).get("channelId", ""), ""),
             "keyword": keyword} for item in items]
    return {"items": rows, "nextPageToken": results.get("nextPageToken", "")} if return_page else rows


def lookup_videos(key, keyword, market, limit=12, access_token=None, page_token=None,
                  return_page=False, order="relevance", quota_context=None):
    """Serialize a complete read-only search + metadata operation process-wide."""
    try:
        with occupy(SEARCH_SLOT):
            return _lookup_videos(key, keyword, market, limit, access_token, page_token,
                                  return_page, order, quota_context)
    except WorkSlotBusy:
        raise APIError(code="search_busy", request_uncertain=False) from None
