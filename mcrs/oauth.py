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
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
SCOPES = [READONLY_SCOPE, "openid", EMAIL_SCOPE]

_EMAIL_SCOPE_ALIASES = {"email", EMAIL_SCOPE}
_SAFE_FAILURES = {
    "access_denied": "Google 로그인이 취소되었거나 권한 승인이 거부되었습니다. 다시 연결하려면 실제 연결할 계정으로 승인하세요.",
    "test_user_required": "테스트 모드에서는 Google Auth Platform > Audience > Test users에 연결할 계정을 추가해야 할 수 있습니다.",
    "scope_mismatch": "요청한 권한과 발급된 권한이 다릅니다. YouTube 읽기 권한이 포함되는지 확인하고 다시 연결하세요.",
    "redirect_uri_mismatch": "로컬 127.0.0.1 콜백이 승인되지 않았습니다. Desktop 앱 OAuth JSON을 다시 확인하세요.",
    "invalid_client": "OAuth 클라이언트 설정이 유효하지 않습니다. Google Cloud의 Desktop 앱 클라이언트를 확인하세요.",
    "api_permission": "YouTube Data API v3 사용 설정 또는 읽기 권한을 확인하세요. OAuth 연결 자체는 저장되지 않았습니다.",
    "local_callback": "브라우저 인증 후 로컬 127.0.0.1 콜백에 연결할 수 없습니다. 방화벽/VPN/로컬 보안 정책을 확인하세요.",
    "network_timeout": "Google 인증 요청이 시간 초과되었습니다. 네트워크를 확인하고 다시 시도하세요.",
    "token_storage": "인증 토큰을 로컬 사용자 폴더에 저장하지 못했습니다. 폴더 권한과 사용 가능한 공간을 확인하세요.",
    "account_mismatch": "기존 Google 계정/채널 연결과 다르거나 이 프로필에 진행·저장 자료가 있어 변경하지 않았습니다. 자료를 분리하려면 새 프로필을 사용하세요.",
    "account_identity": "Google 계정 이메일을 확인하지 못했습니다. 계정 확인이 끝날 때까지 연결을 저장하지 않았습니다.",
    "client_config": "Desktop OAuth JSON 형식이 올바르지 않습니다. 원본 파일을 확인하고 다시 선택하세요.",
    "unknown": "Google 인증을 완료하지 못했습니다. 계정, 네트워크, 테스트 사용자 및 OAuth 설정을 확인하세요.",
}

class OAuthError(Exception):
    def __init__(self, message=None, *, code="unknown", stage="oauth"):
        self.code = code
        self.stage = stage
        super().__init__(message or _SAFE_FAILURES.get(code, _SAFE_FAILURES["unknown"]))


def normalize_scope(scope):
    """Normalize only Google's documented OIDC email alias; preserve all others."""
    if isinstance(scope, str):
        values = scope.split()
    elif isinstance(scope, (list, tuple, set, frozenset)):
        values = list(scope)
    else:
        return set()
    normalized = set()
    for item in values:
        value = str(item).strip()
        if value in _EMAIL_SCOPE_ALIASES:
            value = EMAIL_SCOPE
        if value:
            normalized.add(value)
    return normalized


def validate_granted_scopes(granted_scopes, requested_scopes=SCOPES):
    granted = normalize_scope(granted_scopes)
    requested = normalize_scope(requested_scopes)
    # Exact equality rejects both weakened permissions and unexpected extras.
    if granted != requested or READONLY_SCOPE not in granted:
        raise OAuthError(code="scope_mismatch", stage="token_exchange")
    return sorted(granted)


def _oauth_failure(exc, stage):
    """Map structured OAuth/network errors to safe codes; never surface raw text."""
    if isinstance(exc, OAuthError):
        return exc
    if isinstance(exc, Warning):
        try:
            validate_granted_scopes(getattr(exc, "new_scope", ()))
        except OAuthError:
            return OAuthError(code="scope_mismatch", stage=stage)
        # This exact alias-only difference is the sole warning accepted. Caller
        # must rebuild credentials from the warning token and still validate it.
        return None
    code = str(getattr(exc, "error", "") or "").casefold()
    description = str(getattr(exc, "description", "") or "").casefold()
    if code == "access_denied":
        if "test user" in description or "testing" in description and "user" in description:
            return OAuthError(code="test_user_required", stage=stage)
        return OAuthError(code="access_denied", stage=stage)
    if code == "redirect_uri_mismatch":
        return OAuthError(code="redirect_uri_mismatch", stage=stage)
    if code == "invalid_client":
        return OAuthError(code="invalid_client", stage=stage)
    if isinstance(exc, PermissionError):
        return OAuthError(code="token_storage", stage=stage)
    if isinstance(exc, (TimeoutError,)):
        return OAuthError(code="network_timeout", stage=stage)
    if isinstance(exc, HTTPError) and stage == "youtube_api":
        return OAuthError(code="api_permission", stage=stage)
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return OAuthError(code="network_timeout", stage=stage)
        if stage == "oauth_callback":
            return OAuthError(code="local_callback", stage=stage)
        return OAuthError(code="unknown", stage=stage)
    if isinstance(exc, OSError) and stage == "oauth_callback":
        return OAuthError(code="local_callback", stage=stage)
    return OAuthError(code="unknown", stage=stage)


def _credentials_from_scope_warning(exc, config, credentials_class):
    """Recover only an alias-equivalent oauthlib scope Warning, otherwise fail closed."""
    if normalize_scope(getattr(exc, "old_scope", ())) != normalize_scope(SCOPES):
        raise OAuthError(code="scope_mismatch", stage="token_exchange") from None
    scopes = validate_granted_scopes(getattr(exc, "new_scope", ()))
    token_payload = getattr(exc, "token", None)
    if not isinstance(token_payload, dict):
        raise OAuthError(code="scope_mismatch", stage="token_exchange") from None
    installed = config.get("installed", {})
    token = token_payload.get("access_token") or token_payload.get("token")
    if not token:
        raise OAuthError(code="scope_mismatch", stage="token_exchange") from None
    authorized_info = {
        "token": token,
        "refresh_token": token_payload.get("refresh_token"),
        "token_uri": installed.get("token_uri"),
        "client_id": installed.get("client_id"),
        "client_secret": installed.get("client_secret"),
        "scopes": scopes,
    }
    try:
        return credentials_class.from_authorized_user_info(authorized_info, scopes=scopes)
    except Exception:
        raise OAuthError(code="scope_mismatch", stage="token_exchange") from None


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
        try:
            validate_granted_scopes(scopes)
        except OAuthError:
            return {"status":"invalid_scope","message":"요청 권한 범위가 바뀌어 다시 연결해야 합니다."}
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
        error_code = str(getattr(exc, "error", "") or "").casefold()
        # Inspect only the allowlisted error marker; never display/log the
        # provider exception string, which can contain response diagnostics.
        safe_marker = str(exc).casefold() if isinstance(exc, RuntimeError) else ""
        if error_code == "invalid_grant" or "invalid_grant" in safe_marker:
            raise OAuthError("Google 접근 권한이 취소되었거나 만료되었습니다. 다시 연결해 주세요.",
                             code="access_denied", stage="token_refresh") from None
        raise OAuthError(code="unknown", stage="token_refresh") from None


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
        raise _oauth_failure(exc, "youtube_api") from None
    except (URLError, TimeoutError) as exc:
        raise _oauth_failure(exc, "youtube_api") from None
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


def begin_connect(client_file=None, *, client_config=None):
    """Run Google consent; caller MUST confirm selected channel before saving token."""
    InstalledAppFlow, Credentials, _ = _packages()
    if client_config is None and (not client_file or not Path(client_file).is_file()):
        raise OAuthError("Google Cloud에서 발급한 Desktop OAuth JSON 파일을 선택하세요.", code="client_config")
    try:
        if client_config is None:
            try:
                with open(client_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except (OSError, ValueError, TypeError):
                raise OAuthError(code="client_config", stage="client_config") from None
        else:
            config = client_config
        if not config.get('installed',{}).get('client_id'):
            raise OAuthError("OAuth 클라이언트 유형이 '데스크톱 앱'이 아닙니다. Google Cloud에서 다시 생성하세요.", code="client_config")
        if client_config is not None:
            flow = InstalledAppFlow.from_client_config(config, scopes=SCOPES, autogenerate_code_verifier=True)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_file), scopes=SCOPES, autogenerate_code_verifier=True)
        try:
            credentials = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True,
                                                prompt='select_account', access_type='offline')
        except Warning as warning:
            failure = _oauth_failure(warning, "token_exchange")
            if failure:
                raise failure from None
            credentials = _credentials_from_scope_warning(warning, config, Credentials)
        granted = getattr(credentials, "scopes", None) or getattr(credentials, "granted_scopes", None)
        if not granted:
            raise OAuthError(code="scope_mismatch", stage="token_exchange")
        validate_granted_scopes(granted)
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
        raise _oauth_failure(exc, "oauth_callback") from None


def save_connection(profile_id, channel, token_json, *, allow_account_change=False):
    profile = store.get_profile(profile_id)
    if not profile:
        raise OAuthError("저장 대상 프로필이 없습니다.")
    if not channel.get('channel_title'):
        raise OAuthError("계정 표시 이름이 없습니다.")
    # Make sure the caller did not pass an invalid authorization payload.
    try:
        data = json.loads(token_json)
        if not data.get('token') or not data.get('client_id'):
            raise ValueError("missing fields")
        scopes = data.get('scopes') or []
        validate_granted_scopes(scopes)
    except (ValueError, TypeError) as exc:
        raise OAuthError("잘못된 읽기 전용 연결 정보입니다.") from exc
    path = token_path(profile_id)
    previous_token = path.read_text(encoding="utf-8") if path.is_file() else None
    old_email=(profile.get("connected_email") or "").strip().casefold()
    new_email=(channel.get("email") or "").strip().casefold()
    old_channel=(profile.get("youtube_channel_id") or "").strip()
    new_channel=(channel.get("channel_id") or "").strip()
    if not new_email:
        raise OAuthError(code="account_identity", stage="account_binding")
    if ((old_email and old_email != new_email) or
        (old_channel and old_channel != new_channel)) and not allow_account_change:
        raise OAuthError(code="account_mismatch", stage="account_binding")
    try:
        _write_token(path, token_json)
    except OSError:
        raise OAuthError(code="token_storage", stage="token_storage") from None
    try:
        store.bind_youtube_channel(profile_id, channel.get('channel_id',''), channel['channel_title'],
                                   channel.get('email',''), allow_account_change=allow_account_change)
    except ValueError:
        if previous_token is None:
            path.unlink(missing_ok=True)
        else:
            _write_token(path, previous_token)
        raise OAuthError(code="account_mismatch", stage="account_binding") from None
    except Exception:
        if previous_token is None:
            path.unlink(missing_ok=True)
        else:
            _write_token(path, previous_token)
        raise OAuthError("계정 연결 정보를 갱신하지 못했습니다. 기존 연결과 프로필 자료는 보존했습니다.",
                         code="unknown", stage="account_binding") from None


def disconnect(profile_id):
    try:
        token_path(profile_id).unlink(missing_ok=True)
        store.unbind_youtube_channel(profile_id)
    except OSError:
        raise OAuthError(code="token_storage", stage="disconnect") from None
