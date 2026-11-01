"""Per-user DPAPI storage for optional AI provider API keys.

Only provider identifiers are persisted as filenames; plaintext keys are never
written. The protector interface is injectable so tests need no Windows APIs.
Google OAuth token storage is intentionally separate and unchanged.
"""
from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path
import tempfile

_PROVIDERS = {"openai", "gemini"}
_ENTROPY = b"ZisuResearchStudio/provider-key/v1"


class CredentialError(Exception):
    """Safe, user-displayable credential store failure (never includes a key)."""


class WindowsDPAPI:
    """Current-user Windows DPAPI wrapper; no third-party package required."""

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    @classmethod
    def _blob(cls, value: bytes):
        buf = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return cls._Blob(len(value), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf

    @classmethod
    def _call(cls, function: str, value: bytes) -> bytes:
        if os.name != "nt":
            raise CredentialError("Windows 보안 저장소는 Windows에서만 사용할 수 있습니다.")
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        source, source_buf = cls._blob(value)
        entropy, entropy_buf = cls._blob(_ENTROPY)
        output = cls._Blob()
        call = getattr(crypt, function)
        blob_ptr = ctypes.POINTER(cls._Blob)
        if function == "CryptProtectData":
            call.argtypes = [blob_ptr, ctypes.c_wchar_p, blob_ptr, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_uint32, blob_ptr]
            call.restype = ctypes.c_int
            ok = call(ctypes.byref(source), "ZISU provider credential", ctypes.byref(entropy),
                      None, None, 0x1, ctypes.byref(output))
        else:
            call.argtypes = [blob_ptr, ctypes.POINTER(ctypes.c_wchar_p), blob_ptr,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob_ptr]
            call.restype = ctypes.c_int
            ok = call(ctypes.byref(source), None, ctypes.byref(entropy),
                      None, None, 0x1, ctypes.byref(output))
        _ = source_buf, entropy_buf
        if not ok:
            raise CredentialError("Windows 사용자 보안 저장소를 사용할 수 없습니다.")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            kernel.LocalFree(output.pbData)

    def protect(self, value: bytes) -> bytes:
        return self._call("CryptProtectData", value)

    def unprotect(self, value: bytes) -> bytes:
        return self._call("CryptUnprotectData", value)


class CredentialStore:
    def __init__(self, directory: str | Path | None = None, protector=None):
        if directory is None:
            from .store import data_dir
            directory = data_dir() / "provider_credentials"
        self.directory = Path(directory)
        self.protector = protector or WindowsDPAPI()

    @staticmethod
    def _provider(provider: str) -> str:
        # OAuth client JSON and public YouTube API keys reuse the existing
        # per-user DPAPI vault without changing the separate OAuth token files.
        if provider not in _PROVIDERS | {"youtube-data", "youtube-analytics"}:
            raise CredentialError("지원하지 않는 보안 저장 항목입니다.")
        return provider

    def _path(self, provider: str) -> Path:
        return self.directory / (self._provider(provider) + ".dpapi")

    def save(self, provider: str, secret: str) -> None:
        target = self._path(provider)
        if not isinstance(secret, str) or not 8 <= len(secret) <= 8192 or "\x00" in secret:
            raise CredentialError("API 키 형식을 확인하세요. 키는 8~8192자여야 합니다.")
        try:
            payload = base64.b64encode(self.protector.protect(secret.encode("utf-8")))
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".credential-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, target)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("API 키를 안전하게 저장하지 못했습니다.") from None

    def load(self, provider: str) -> str | None:
        target = self._path(provider)
        try:
            if not target.exists():
                return None
            protected = base64.b64decode(target.read_bytes(), validate=True)
            value = self.protector.unprotect(protected).decode("utf-8")
            if not 8 <= len(value) <= 8192 or "\x00" in value:
                raise ValueError("invalid protected value")
            return value
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("저장된 API 키를 복구할 수 없습니다. 키를 다시 등록하세요.") from None

    def delete(self, provider: str) -> None:
        target = self._path(provider)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            raise CredentialError("저장된 API 키를 삭제하지 못했습니다.") from None

    def configured(self, provider: str) -> bool:
        return self._path(provider).is_file()
