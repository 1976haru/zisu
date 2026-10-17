"""Read-only YouTube Data API adapter. Does not log in or engage."""
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
import json

class APIError(Exception):
    pass

def _get(endpoint,key,access_token=None,**kwargs):
    if not access_token and not (key or "").strip():
        raise APIError("계정을 연결하거나 검색 API 키를 입력하세요.")
    params = dict(kwargs)
    if not access_token:
        params["key"] = key.strip()
    url="https://www.googleapis.com/youtube/v3/"+endpoint+"?"+urlencode(params)
    headers={"User-Agent":"ZisuResearchStudio/4.2"}
    if access_token:
        headers["Authorization"]="Bearer " + access_token
    try:
        with urlopen(Request(url,headers=headers),timeout=20) as res:
            return json.load(res)
    except HTTPError as exc:
        try:
            payload=json.load(exc).get("error",{})
            error=payload.get("message","검색 실패")
            reasons={item.get("reason","") for item in payload.get("errors",[])}
        except (ValueError, UnicodeError):
            error=f"HTTP {exc.code}"
            reasons=set()
        if exc.code == 401:
            message="Google 연결이 만료되었거나 취소되었습니다. 계정을 다시 연결하세요."
        elif exc.code == 403 and reasons & {"quotaExceeded","dailyLimitExceeded"}:
            message="YouTube API 일일 할당량을 초과했습니다. 할당량이 갱신된 뒤 다시 시도하세요."
        elif exc.code == 403:
            message="YouTube Data API 권한 또는 사용 설정을 확인하세요."
        elif exc.code == 429:
            message="요청이 너무 많습니다. 잠시 후 다시 시도하세요."
        else:
            message=f"API 요청 실패: {error}"
        exc.close()
        raise APIError(message) from None
    except (URLError,TimeoutError) as exc:
        raise APIError("인터넷 연결을 확인하세요.") from None

def lookup_videos(key,keyword,market,limit=12,access_token=None):
    if market not in ("KR","JP"):
        raise ValueError("시장 코드는 KR 또는 JP입니다.")
    lang="ko" if market=="KR" else "ja"
    credentials = {"access_token":access_token} if access_token else {}
    results=_get("search",key,part="snippet",type="video",q=keyword,regionCode=market,
                 relevanceLanguage=lang,order="relevance",maxResults=max(1,min(20,limit)),**credentials)
    items=[v for v in results.get("items",[]) if v.get("id",{}).get("videoId")]
    if not items:
        return []
    video_ids=[v["id"]["videoId"] for v in items]
    channel_ids=list(dict.fromkeys(v.get("snippet",{}).get("channelId","") for v in items))
    videos=_get("videos",key,part="statistics",id=",".join(video_ids),**credentials)
    channels=_get("channels",key,part="snippet",id=",".join(x for x in channel_ids if x),**credentials)
    counts={v["id"]:int(v.get("statistics",{}).get("viewCount",0) or 0) for v in videos.get("items",[])}
    countries={c["id"]:c.get("snippet",{}).get("country","") for c in channels.get("items",[])}
    return [{"video_id":v["id"]["videoId"], "video_title":v.get("snippet",{}).get("title",""),
             "channel_id":v.get("snippet",{}).get("channelId",""),
             "channel_title":v.get("snippet",{}).get("channelTitle",""),
             "description":v.get("snippet",{}).get("description",""),
             "published_at":v.get("snippet",{}).get("publishedAt",""),
             "views":counts.get(v["id"]["videoId"],0),
             "country":countries.get(v.get("snippet",{}).get("channelId",""),""),"keyword":keyword} for v in items]
