"""Read-only YouTube Data API adapter. Does not log in or engage."""
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .work_slots import SEARCH_SLOT, WorkSlotBusy, occupy


class APIError(Exception):
    pass


def _api_error(exc):
    """Convert an HTTP response to a beginner-safe message without secrets."""
    try:
        payload = json.load(exc).get("error", {})
        error = payload.get("message", "검색 실패")
        reasons = {item.get("reason", "") for item in payload.get("errors", [])}
    except (ValueError, UnicodeError):
        error = f"HTTP {exc.code}"
        reasons = set()
    if exc.code == 401:
        message = "Google 연결이 만료되었거나 취소되었습니다. 계정을 다시 연결하세요."
    elif exc.code == 403 and reasons & {"quotaExceeded", "dailyLimitExceeded"}:
        message = "YouTube API 일일 할당량을 초과했습니다. 할당량이 갱신된 뒤 다시 시도하세요."
    elif exc.code == 403:
        message = "YouTube Data API 권한 또는 사용 설정을 확인하세요."
    elif exc.code == 429:
        message = "요청이 너무 많습니다. 잠시 뒤 다시 시도하세요."
    else:
        message = f"API 요청 실패: {error}"
    exc.close()
    return APIError(message)


def _get(endpoint, key, access_token=None, retries=2, retry_delay=0.25, **kwargs):
    if not access_token and not (key or "").strip():
        raise APIError("계정을 연결하거나 검색 API 키를 입력하세요.")
    params = dict(kwargs)
    if not access_token:
        params["key"] = key.strip()
    url = "https://www.googleapis.com/youtube/v3/" + endpoint + "?" + urlencode(params)
    headers = {"User-Agent": "ZisuResearchStudio/4.3"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    for attempt in range(retries + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                exc.close()
                time.sleep(retry_delay * (2 ** attempt))
                continue
            raise _api_error(exc) from None
        except (URLError, TimeoutError):
            if attempt < retries:
                time.sleep(retry_delay * (2 ** attempt))
                continue
            raise APIError("인터넷 연결을 확인하세요.") from None
    raise APIError("API 요청을 완료하지 못했습니다.")


def _lookup_videos(key, keyword, market, limit=12, access_token=None, page_token=None, return_page=False):
    if market not in ("KR", "JP"):
        raise ValueError("시장 코드는 KR 또는 JP입니다.")
    lang = "ko" if market == "KR" else "ja"
    credentials = {"access_token": access_token} if access_token else {}
    results = _get(
        "search", key, part="snippet", type="video", q=keyword,
        regionCode=market, relevanceLanguage=lang, order="relevance",
        maxResults=max(1, min(50, limit)), retries=0,
        **({"pageToken": page_token} if page_token else {}), **credentials,
    )
    items = [item for item in results.get("items", []) if item.get("id", {}).get("videoId")]
    if not items:
        return {"items": [], "nextPageToken": results.get("nextPageToken", "")} if return_page else []
    video_ids = [item["id"]["videoId"] for item in items]
    channel_ids = list(dict.fromkeys(item.get("snippet", {}).get("channelId", "") for item in items))
    videos = _get("videos", key, part="statistics", id=",".join(video_ids), **credentials)
    channels = _get(
        "channels", key, part="snippet", id=",".join(value for value in channel_ids if value), **credentials
    )
    counts = {
        item["id"]: int(item.get("statistics", {}).get("viewCount", 0) or 0)
        for item in videos.get("items", [])
    }
    countries = {
        item["id"]: item.get("snippet", {}).get("country", "")
        for item in channels.get("items", [])
    }
    rows = [
        {
            "video_id": item["id"]["videoId"],
            "video_title": item.get("snippet", {}).get("title", ""),
            "channel_id": item.get("snippet", {}).get("channelId", ""),
            "channel_title": item.get("snippet", {}).get("channelTitle", ""),
            "description": item.get("snippet", {}).get("description", ""),
            "published_at": item.get("snippet", {}).get("publishedAt", ""),
            "views": counts.get(item["id"]["videoId"], 0),
            "country": countries.get(item.get("snippet", {}).get("channelId", ""), ""),
            "keyword": keyword,
        }
        for item in items
    ]
    return {"items": rows, "nextPageToken": results.get("nextPageToken", "")} if return_page else rows


def lookup_videos(key, keyword, market, limit=12, access_token=None, page_token=None, return_page=False):
    """Serialize a complete search + metadata-enrichment operation process-wide."""
    try:
        with occupy(SEARCH_SLOT):
            return _lookup_videos(key, keyword, market, limit, access_token, page_token, return_page)
    except WorkSlotBusy:
        raise APIError("다른 검색을 정리하는 중입니다. 잠시 뒤 저장된 작업을 이어가세요.") from None
