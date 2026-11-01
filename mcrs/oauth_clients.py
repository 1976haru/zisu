"""Explicitly registered, per-Windows-user OAuth desktop clients.

Client JSON is held in DPAPI-backed storage; the registry index contains only
non-secret labels and identifiers. Existing user-selected JSON paths remain
supported by :mod:`mcrs.oauth`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .credential_store import CredentialStore, CredentialError
from . import store


class OAuthClientRegistryError(Exception):
    """Safe message for OAuth client registry failures."""


def default_registry_dir() -> Path:
    return store.data_dir() / "oauth_clients"


class OAuthClientRegistry:
    def __init__(self, directory: str | Path | None = None, credential_store_factory=None):
        self.directory = Path(directory) if directory else default_registry_dir()
        self.index_path = self.directory / "index.json"
        self.credential_store_factory = credential_store_factory or CredentialStore

    def _read_index(self) -> list[dict[str, str]]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8")) if self.index_path.exists() else []
        except (OSError, ValueError, TypeError):
            raise OAuthClientRegistryError("등록된 OAuth 클라이언트 목록을 읽지 못했습니다.") from None
        if not isinstance(raw, list):
            raise OAuthClientRegistryError("OAuth 클라이언트 목록 형식이 올바르지 않습니다.")
        return [item for item in raw if isinstance(item, dict) and item.get("id") and item.get("label")]

    def list_clients(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._read_index()]

    def register_file(self, path: str | Path, *, approved: bool = False) -> dict[str, str]:
        if not approved:
            raise OAuthClientRegistryError("사용자가 저장을 승인해야 OAuth 클라이언트를 등록할 수 있습니다.")
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            installed = data.get("installed") if isinstance(data, dict) else None
            client_id = installed.get("client_id") if isinstance(installed, dict) else None
            client_secret = installed.get("client_secret") if isinstance(installed, dict) else None
            if not client_id or not client_secret or not installed.get("auth_uri") or not installed.get("token_uri"):
                raise ValueError("invalid desktop client")
        except (OSError, ValueError, TypeError):
            raise OAuthClientRegistryError("데스크톱 앱 OAuth JSON을 읽지 못했습니다. 원본은 변경하지 않았습니다.") from None

        client_key = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:32]
        raw_label = str(data.get("project_id") or "Google Cloud 데스크톱 클라이언트")
        label = "".join(character for character in raw_label if character.isprintable())[:80] or "Google Cloud 데스크톱 클라이언트"
        vault = self.credential_store_factory(directory=self.directory / "vault" / client_key)
        try:
            vault.save("youtube-data", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            rows = [item for item in self._read_index() if item.get("id") != client_key]
            rows.append({"id": client_key, "label": label, "client_id": client_id})
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".oauth-index-", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(rows, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, self.index_path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        except CredentialError:
            raise OAuthClientRegistryError("OAuth 클라이언트를 Windows 사용자 보안 저장소에 보관하지 못했습니다.") from None
        except OSError:
            raise OAuthClientRegistryError("OAuth 클라이언트 목록을 안전하게 갱신하지 못했습니다.") from None
        return {"id": client_key, "label": label, "client_id": client_id}

    def load_config(self, client_id: str) -> dict:
        if not isinstance(client_id, str) or len(client_id) != 32 or any(ch not in "0123456789abcdef" for ch in client_id):
            raise OAuthClientRegistryError("선택한 OAuth 클라이언트 항목이 올바르지 않습니다.")
        if not any(item.get("id") == client_id for item in self._read_index()):
            raise OAuthClientRegistryError("선택한 OAuth 클라이언트를 찾을 수 없습니다.")
        try:
            vault = self.credential_store_factory(directory=self.directory / "vault" / client_id)
            data = json.loads(vault.load("youtube-data") or "")
            if data.get("installed", {}).get("client_id") != next(
                item["client_id"] for item in self._read_index() if item["id"] == client_id
            ):
                raise ValueError("client mismatch")
            return data
        except (CredentialError, ValueError, TypeError, StopIteration):
            raise OAuthClientRegistryError("저장된 OAuth 클라이언트를 복구하지 못했습니다. 원본 JSON을 다시 등록하세요.") from None

    def remove(self, client_id: str) -> None:
        rows = self._read_index()
        if not any(item.get("id") == client_id for item in rows):
            return
        # Keep the encrypted blob recoverable until the index update succeeds.
        try:
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps([item for item in rows if item.get("id") != client_id], ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.index_path)
        except OSError:
            raise OAuthClientRegistryError("OAuth 클라이언트 목록을 갱신하지 못했습니다.") from None
        try:
            self.credential_store_factory(directory=self.directory / "vault" / client_id).delete("youtube-data")
        except CredentialError:
            # Index is already removed; the orphan is encrypted and can be cleaned later.
            pass
