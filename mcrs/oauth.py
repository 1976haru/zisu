"""User-consented read-only YouTube OAuth for each local channel profile.

No password collection, no engagement actions. Access tokens stay in the user's
local application-data directory and are not synchronized to GitHub.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from . import store

READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
SCOPES = [READONLY_SCOPE, "openid", "email"]

class OAuthError(Exception):
    pass


def _packages():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as RefreshRequest
    except ImportError as e:
        raise OAuthError("구글 계정 연결 모듈이 설치되지 않았습니다. 'OAuth_모듈_설치하기.bat'을 먼저 실행해 주세요.") from e
    return InstalledAppFlow, Credentials, RefreshRequest


def token_path(profile_id):
    if not profile_id or not all(c.isalnum() or c in '-_' for c in profile_id):
        raise OAuthError("올바르지 않은 채널 프로필 ID입니다.")
    return store.data_dir() / 'oauth_tokens' / (profile_id + '.json')


def is_connected(profile_id):
    return token_path(profile_id).is_file()


def connection_state(profile_id):
    """Return non-secret connection health for GUI/diagnostics."""
    path = token_path(profile_id)
    profile = store.get_profile(profile_id)
    if not profile:
        return {"status":"missing_profile","message":"프로필을 찾을 수 없습니다."}
    if not path.is_file():
        return {"status":"disconnected","message":"Google 계정 연결 전"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        scopes = payload.get("scopes") or []
        if READONLY_SCOPE not in scopes:
            return {"status":"invalid_scope","message":"읽기 전용 권한이 없어 다시 연결해야 합니다."}
        if not payload.get("refresh_token"):
            return {"status":"reconnect_required","message":"갱신 정보가 없어 만료 시 다시 연결해야 합니다."}
        return {"status":"connected","message":"Google 읽기 전용 연결됨"}
    except (OSError,ValueError,TypeError):
        return {"status":"corrupt","message":"연결 파일을 읽을 수 없어 다시 연결해야 합니다."}


def get_access_token(profile_id):
    """Return a valid token, refreshing if needed (never expose it to the UI)."""
    _, Credentials, RefreshRequest = _packages()
    path = token_path(profile_id)
    if not path.is_file():
        raise OAuthError("이 채널은 아직 구글 계정에 연결되지 않았습니다.")
    try:
        credentials = Credentials.from_authorized_user_file(str(path), scopes=SCOPES)
        if not credentials.valid:
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(RefreshRequest())
                _write_token(path, credentials.to_json())
            else:
                raise OAuthError("계정 연결이 만료되었습니다. 다시 연결해 주세요.")
        return credentials.token
    except OAuthError:
        raise
    except Exception as exc:
        detail = str(exc).casefold()
        if "invalid_grant" in detail or "revoked" in detail:
            raise OAuthError("Google 접근 권한이 취소되었거나 만료되었습니다. 다시 연결해 주세요.") from exc
        raise OAuthError("계정 인증에 실패했습니다. 인터넷 연결을 확인한 뒤 다시 연결해 주세요.") from exc


def _write_token(path, json_text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        f.write(json_text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _get_my_channels(access_token):
    # Browser user's selected channel (Brand Accounts can be selectable during consent).
    url = 'https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true&maxResults=50'
    try:
        with urlopen(Request(url, headers={'Authorization': 'Bearer ' + access_token,
                                           'User-Agent': 'ZisuResearchStudio/4.2'}), timeout=25) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        raise OAuthError(f"YouTube 연결 확인 실패 (HTTP {exc.code}). YouTube Data API 사용 설정을 확인하세요.") from None
    except (URLError, TimeoutError) as exc:
        raise OAuthError("유튜브 접속에 실패했습니다. 인터넷 연결을 확인하세요.") from exc
    return [{'channel_id': item.get('id',''),
             'channel_title': item.get('snippet',{}).get('title','')}
            for item in data.get('items',[]) if item.get('id')]


def _get_email(access_token):
    url = 'https://openidconnect.googleapis.com/v1/userinfo'
    try:
        with urlopen(Request(url,headers={'Authorization':'Bearer '+access_token,
                                         'User-Agent':'ZisuResearchStudio/4.2'}),timeout=20) as resp:
            return json.load(resp).get('email','')
    except (HTTPError,URLError,TimeoutError,ValueError):
        return ''


def begin_connect(client_file):
    """Run Google consent; caller MUST confirm selected channel before saving token."""
    InstalledAppFlow, _, _ = _packages()
    if not Path(client_file).is_file():
        raise OAuthError("Google Cloud에서 발급한 Desktop OAuth JSON 파일을 선택하세요.")
    try:
        with open(client_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not config.get('installed',{}).get('client_id'):
            raise OAuthError("OAuth 클라이언트 유형이 '데스크톱 앱'이 아닙니다. Google Cloud에서 다시 생성하세요.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_file), scopes=SCOPES, autogenerate_code_verifier=True)
        credentials = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True,
                                            prompt='select_account', access_type='offline')
        email=_get_email(credentials.token)
        channels = _get_my_channels(credentials.token)
        if not channels:
            # A separate research Google account may not have created a YouTube channel.
            channels=[{'channel_id':'','channel_title':'조사 전용 계정 (YouTube 채널 없음)'}]
        for channel in channels:
            channel['email']=email
        return channels, credentials.to_json()
    except OAuthError:
        raise
    except Exception as exc:
        raise OAuthError(f"Google 인증 실패: {type(exc).__name__}. Google Cloud 테스트 사용자, OAuth 설정을 확인하세요.") from exc


def save_connection(profile_id, channel, token_json):
    if not store.get_profile(profile_id):
        raise OAuthError("저장 대상 프로필이 없습니다.")
    if not channel.get('channel_title'):
        raise OAuthError("계정 표시 이름이 없습니다.")
    # Make sure the caller did not pass an invalid authorization payload.
    try:
        data = json.loads(token_json)
        if not data.get('token') or not data.get('client_id'):
            raise ValueError("missing fields")
        scopes = data.get('scopes') or []
        if READONLY_SCOPE not in scopes:
            raise ValueError("read only scope missing")
    except (ValueError, TypeError) as exc:
        raise OAuthError("잘못된 읽기 전용 연결 정보입니다.") from exc
    path = token_path(profile_id)
    previous_token = path.read_text(encoding="utf-8") if path.is_file() else None
    _write_token(path, token_json)
    try:
        store.bind_youtube_channel(profile_id, channel.get('channel_id',''), channel['channel_title'], channel.get('email',''))
    except Exception:
        if previous_token is None:
            path.unlink(missing_ok=True)
        else:
            _write_token(path, previous_token)
        raise


def disconnect(profile_id):
    token_path(profile_id).unlink(missing_ok=True)
    store.unbind_youtube_channel(profile_id)
