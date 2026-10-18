"""A single, isolated, explicitly user-controlled research browser session.

The manager launches an installed Edge/Chrome executable with a fresh ZISU
profile and forwards later URLs through that same isolated browser profile.
It never reads browser cookies or automates YouTube playback/engagement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse, parse_qs

from .resource_guard import ResourceGuard, ResourceSnapshot, sample_resources


class BrowserError(RuntimeError):
    """Safe, user-presentable research browser failure."""


class BrowserBusy(BrowserError):
    pass


class BrowserResourceDeferred(BrowserError):
    pass


class BrowserOwnershipUncertain(BrowserError):
    pass


@dataclass
class BrowserSession:
    session_id: str
    pid: int
    creation_time: int
    executable: str
    profile_dir: str
    started_at: float
    initial_rss_bytes: int = 0
    process: subprocess.Popen | None = field(default=None, repr=False)
    owned_processes: dict[int, tuple[int, str]] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class BrowserMetrics:
    running: bool
    root_pid: int | None
    owned_process_count: int
    owned_rss_bytes: int
    window_count: int
    child_process_ids: tuple[int, ...] = ()
    rss_change_bytes: int = 0


@dataclass(frozen=True)
class BrowserCloseResult:
    closed: bool
    rss_before_bytes: int
    rss_after_bytes: int
    forced_process_ids: tuple[int, ...]
    residual_process_ids: tuple[int, ...]


def validate_research_url(url: str, *, allow_local_test: bool = False) -> str:
    """Allow HTTPS YouTube watch URLs; local HTTP is test-only and opt-in."""
    try:
        parsed = urlparse(str(url).strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.username or parsed.password:
            raise ValueError
        if parsed.scheme == "https" and parsed.port in (None, 443) and host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if parsed.path == "/watch" and re.fullmatch(r"[\w-]{11}", parse_qs(parsed.query).get("v", [""])[0]):
                return parsed.geturl()
        if parsed.scheme == "https" and parsed.port in (None, 443) and host in {"youtu.be", "www.youtu.be"}:
            if re.fullmatch(r"[\w-]{11}", parsed.path.strip("/").split("/")[0]):
                return parsed.geturl()
        if (allow_local_test and parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
                and parsed.port is not None):
            return parsed.geturl()
    except (TypeError, ValueError):
        pass
    raise ValueError("연구 창에는 정상적인 YouTube 영상 주소만 열 수 있습니다.")


def find_browser() -> str | None:
    """Find installed Edge or Chrome without installing or downloading a browser."""
    candidates = [shutil.which("msedge"), shutil.which("chrome")]
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")]
        for root in roots:
            if root:
                candidates.extend((os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


class ResearchBrowserManager:
    """Own at most one browser process tree and one isolated profile per session."""

    def __init__(self, *, app_data_root: str | os.PathLike | None = None,
                 executable: str | None = None, resource_guard: ResourceGuard | None = None,
                 resource_sampler=sample_resources, launcher=subprocess.Popen,
                 allow_local_test: bool = False):
        root = app_data_root or _default_app_data_root()
        self.root = Path(root)
        self.profile_root = self.root / "research-browser-profiles"
        self.manifest_path = self.root / "research-browser-session.json"
        self.executable = str(Path(executable).resolve()) if executable else find_browser()
        self.resource_guard = resource_guard or ResourceGuard()
        self.resource_sampler = resource_sampler
        self.launcher = launcher
        self.allow_local_test = bool(allow_local_test)
        self._lock = threading.RLock()
        self.session: BrowserSession | None = None
        self._recovery_record = self._load_recovery_record()

    @property
    def recovery_available(self) -> bool:
        return bool(self._recovery_record)

    def open_selected(self, url: str, *, user_approved: bool = False) -> BrowserMetrics:
        if not user_approved:
            raise BrowserError("연구용 인터넷 창을 열려면 사용자가 먼저 승인해야 합니다.")
        safe_url = validate_research_url(url, allow_local_test=self.allow_local_test)
        with self._lock:
            if self.session and self._session_alive(self.session):
                self._refresh_owned_processes(self.session)
                before = self.metrics()
                if before.window_count != 1:
                    raise BrowserOwnershipUncertain("전용 창을 정확히 하나로 확인하지 못해 다른 창을 열지 않았습니다.")
                helper = self.launcher([self.session.executable,
                    f"--user-data-dir={self.session.profile_dir}", "--no-first-run",
                    "--no-default-browser-check", "--disable-sync", safe_url],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
                helper_identity = _process_identity(helper.pid)
                if helper_identity:
                    self.session.owned_processes.setdefault(helper.pid, helper_identity[:2])
                try:
                    helper.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    helper_identity = _process_identity(helper.pid)
                    if helper_identity:
                        self.session.owned_processes.setdefault(helper.pid, helper_identity[:2])
                self._refresh_owned_processes(self.session)
                after = self.metrics()
                if before.root_pid != after.root_pid or before.window_count != after.window_count or after.window_count != 1:
                    raise BrowserOwnershipUncertain("기존 창을 재사용했는지 확인하지 못해 추가 탐색을 멈췄습니다.")
                return after
            if self.session:
                previous = self.close_owned(grace_seconds=1)
                if not previous.closed:
                    raise BrowserOwnershipUncertain("이전 연구 창의 자식 프로세스가 남아 있어 새 창을 열지 않았습니다.")
            elif self._recovery_record:
                raise BrowserOwnershipUncertain("이전 연구 창을 먼저 확인해야 새 창을 열 수 있습니다.")
            decision = self.resource_guard.browser_allowed(self.resource_sampler())
            if not decision.allow_new_work:
                raise BrowserResourceDeferred("컴퓨터가 바빠 새 연구 창을 열지 않았습니다. 상태가 회복된 뒤 다시 시도하세요.")
            if not self.executable or not Path(self.executable).is_file():
                raise BrowserError("설치된 Microsoft Edge 또는 Google Chrome을 찾지 못했습니다.")
            self.profile_root.mkdir(parents=True, exist_ok=True)
            session_id = uuid.uuid4().hex
            profile = self.profile_root / session_id
            profile.mkdir(parents=True, exist_ok=False)
            command = [self.executable, f"--user-data-dir={profile}",
                       "--no-first-run", "--no-default-browser-check", "--disable-sync",
                       "--disable-background-mode", "--disable-background-networking",
                       "--disable-component-update", "--new-window", safe_url]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                process = self.launcher(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
            except (OSError, ValueError) as exc:
                raise BrowserError("연구용 인터넷 창을 시작하지 못했습니다. Edge/Chrome 설치 상태를 확인하세요.") from None
            identity = _process_identity(process.pid)
            if identity is None or not _same_executable(identity[1], self.executable):
                _close_owned_windows((process.pid,))
                _wait_process_exit(process, timeout=5)
                raise BrowserOwnershipUncertain("브라우저 프로세스의 소유권을 확인하지 못했습니다.")
            self.session = BrowserSession(session_id, process.pid, identity[0], self.executable,
                str(profile), time.time(), process=process,
                owned_processes={process.pid: identity[:2]})
            try:
                self._refresh_owned_processes(self.session)
                self._write_manifest(self.session)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    self._refresh_owned_processes(self.session)
                    metrics = self.metrics()
                    if metrics.window_count == 1:
                        break
                    if not self._alive_owned(self.session):
                        exit_code=process.poll()
                        detail=f" (종료 코드 {exit_code})" if exit_code is not None else ""
                        raise BrowserError("연구용 브라우저가 정상 실행되지 않았습니다."+detail)
                    time.sleep(0.1)
                else:
                    raise BrowserError("연구용 창 표시를 확인할 수 없어 소유한 프로세스만 정리했습니다.")
                metrics=self.metrics()
                self.session.initial_rss_bytes=metrics.owned_rss_bytes
                self._write_manifest(self.session)
                return metrics
            except Exception:
                self.close_owned(grace_seconds=2)
                raise

    def metrics(self) -> BrowserMetrics:
        with self._lock:
            session = self.session
            if not session:
                return BrowserMetrics(False, None, 0, 0, 0)
            self._refresh_owned_processes(session)
            alive = {}
            rss = 0
            for pid, identity in tuple(session.owned_processes.items()):
                current = _process_identity(pid)
                if current and current[0] == identity[0] and _same_executable(current[1], identity[1]):
                    alive[pid] = identity
                    rss += current[2]
            session.owned_processes = alive
            if session.initial_rss_bytes <= 0:
                session.initial_rss_bytes = rss
            return BrowserMetrics(self._session_alive(session), session.pid, len(alive), rss,
                                  _window_count(alive), tuple(sorted(set(alive) - {session.pid})),
                                  rss - session.initial_rss_bytes)

    def close_owned(self, *, grace_seconds: float = 4.0) -> BrowserCloseResult:
        with self._lock:
            session = self.session
            if session is None:
                return BrowserCloseResult(True, 0, 0, (), ())
            if not self._session_alive(session):
                self._refresh_owned_processes(session)
            self._refresh_owned_processes(session)
            before = self.metrics()
            owned = dict(session.owned_processes)
            _close_owned_windows(owned)
            deadline = time.monotonic() + max(0, float(grace_seconds))
            while time.monotonic() < deadline:
                self._refresh_owned_processes(session)
                if not self._alive_owned(session):
                    break
                time.sleep(0.1)
            self._refresh_owned_processes(session)
            forced = []
            if self._alive_owned(session):
                for pid, identity in sorted(session.owned_processes.items(),
                                            key=lambda item: item[0] == session.pid):
                    current = _process_identity(pid)
                    if (current and current[0] == identity[0]
                            and _same_executable(current[1], identity[1])):
                        if _terminate_owned_process(pid, identity):
                            forced.append(pid)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and self._alive_owned(session):
                    self._refresh_owned_processes(session)
                    time.sleep(0.1)
            self._refresh_owned_processes(session)
            residual = tuple(sorted(pid for pid, ident in session.owned_processes.items()
                if (cur := _process_identity(pid)) and cur[0] == ident[0]
                and _same_executable(cur[1], ident[1])))
            after = BrowserMetrics(bool(residual), session.pid if residual else None, len(residual),
                                   sum((_process_identity(pid) or (0, "", 0))[2] for pid in residual),
                                   _window_count({pid: session.owned_processes[pid] for pid in residual}), residual)
            closed = not residual
            if closed:
                if session.process:
                    session.process.poll()  # reap an already exited child without blocking the UI
                self._remove_manifest()
                self.session = None
            else:
                self._write_manifest(session)
            return BrowserCloseResult(closed, before.owned_rss_bytes, after.owned_rss_bytes,
                                      tuple(forced), residual)

    def recover_owned_session(self) -> BrowserMetrics:
        """Attach only to the exact PID+creation-time+image persisted by ZISU."""
        with self._lock:
            if self.session and self._session_alive(self.session):
                return self.metrics()
            record = self._recovery_record or self._load_recovery_record()
            if not record:
                raise BrowserError("이전에 남은 연구용 창이 없습니다.")
            try:
                pid = int(record["pid"])
                creation = int(record["creation_time"])
                executable = str(Path(record["executable"]).resolve())
                profile = str(Path(record["profile_dir"]).resolve())
            except (KeyError, TypeError, ValueError, OSError):
                raise BrowserOwnershipUncertain("이전 연구 창의 소유권 기록이 올바르지 않습니다. 자동 종료하지 않았습니다.") from None
            current = _process_identity(pid)
            if (not current or current[0] != creation or not _same_executable(current[1], executable)
                    or not _within(profile, self.profile_root)):
                self._remove_manifest()
                self._recovery_record = None
                raise BrowserOwnershipUncertain("PID가 바뀌었거나 소유권이 확인되지 않아 창을 건드리지 않았습니다.")
            self.executable = executable
            self.session = BrowserSession(str(record["session_id"]), pid, creation, executable,
                profile, float(record.get("started_at", time.time())),
                process=None, owned_processes={pid: current[:2]})
            self.session.initial_rss_bytes = int(record.get("initial_rss_bytes", 0) or 0)
            self._refresh_owned_processes(self.session)
            return self.metrics()

    def close_recovered_session(self) -> BrowserCloseResult:
        if self.session is None:
            self.recover_owned_session()
        return self.close_owned()

    def _session_alive(self, session: BrowserSession) -> bool:
        current = _process_identity(session.pid)
        return bool(current and current[0] == session.creation_time
                    and _same_executable(current[1], session.executable))

    def _alive_owned(self, session):
        self._refresh_owned_processes(session)
        return any((current := _process_identity(pid)) and current[0] == ident[0]
                   and _same_executable(current[1], ident[1])
                   for pid, ident in session.owned_processes.items())

    def _refresh_owned_processes(self, session):
        roots = tuple(session.owned_processes)
        tree = set()
        for root_pid in roots:
            tree.update(_process_tree(root_pid))
        for pid in tree:
            identity = _process_identity(pid)
            if identity:
                session.owned_processes.setdefault(pid, identity[:2])

    def _write_manifest(self, session):
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session.session_id, "pid": session.pid,
            "creation_time": session.creation_time, "executable": session.executable,
            "profile_dir": session.profile_dir, "started_at": session.started_at,
            "initial_rss_bytes": session.initial_rss_bytes}
        temp = self.manifest_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.manifest_path)
        self._recovery_record = payload

    def _load_recovery_record(self):
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if payload.get("session_id") and int(payload.get("pid", 0)) > 0:
                return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return None

    def _remove_manifest(self):
        try:
            self.manifest_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._recovery_record = None


def _default_app_data_root():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ZisuResearchStudio"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ZisuResearchStudio"


def _wait_devtools(port_file: Path, process: subprocess.Popen, timeout: float, on_sample=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if on_sample:
            try:on_sample()
            except Exception:pass
        try:
            lines = port_file.read_text(encoding="ascii").splitlines()
            port = int(lines[0])
            targets = _devtools_targets(port)
            page = next((target for target in targets if target.get("type") == "page"), None)
            if page and page.get("id"):
                return port, str(page["id"])
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise BrowserError("브라우저 시작 확인 시간이 초과되었습니다. 창 소유권은 확인 후 정리했습니다.")


def _devtools_targets(port: int):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{int(port)}/json/list", timeout=2) as response:
        result = json.loads(response.read(1024 * 1024).decode("utf-8"))
    if not isinstance(result, list):
        raise BrowserError("브라우저의 로컬 상태 응답을 확인하지 못했습니다.")
    return result


def _cdp_command(ws_url: str, method: str, params: dict | None = None):
    parsed = urlparse(ws_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise BrowserOwnershipUncertain("브라우저 제어 주소가 로컬 주소가 아니어서 요청을 차단했습니다.")
    port = parsed.port or 80
    sock = socket.create_connection((parsed.hostname, port), timeout=3)
    try:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        host = f"{parsed.hostname}:{port}"
        request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   "Origin: http://127.0.0.1\r\n\r\n")
        sock.sendall(request.encode("ascii"))
        header = _recv_until(sock, b"\r\n\r\n", 16_384)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise BrowserError("브라우저 제어 연결을 만들지 못했습니다.")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if b"sec-websocket-accept: " + accept.lower() not in header.lower():
            raise BrowserError("브라우저 제어 확인값이 맞지 않습니다.")
        request_id = 1
        frame = json.dumps({"id": request_id, "method": method, "params": params or {}},
                           separators=(",", ":")).encode("utf-8")
        mask = secrets.token_bytes(4)
        length = len(frame)
        prefix = bytearray([0x81])
        if length < 126:
            prefix.append(0x80 | length)
        elif length < 65536:
            prefix.append(0x80 | 126); prefix.extend(struct.pack("!H", length))
        else:
            prefix.append(0x80 | 127); prefix.extend(struct.pack("!Q", length))
        prefix.extend(mask)
        prefix.extend(byte ^ mask[index % 4] for index, byte in enumerate(frame))
        sock.sendall(prefix)
        sock.settimeout(4)
        while True:
            opcode, payload = _recv_ws_frame(sock)
            if opcode == 8:
                raise BrowserError("브라우저 제어 연결이 종료되었습니다.")
            if opcode in (1, 2):
                try:
                    data = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if data.get("id") == request_id:
                    if "error" in data:
                        raise BrowserError("브라우저가 선택한 주소를 열지 못했습니다.")
                    return data.get("result", {})
    finally:
        sock.close()


def _recv_until(sock, ending, limit):
    data = bytearray()
    while ending not in data and len(data) < limit:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _recv_exact(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise BrowserError("브라우저 제어 응답이 중단되었습니다.")
        data.extend(chunk)
    return bytes(data)


def _recv_ws_frame(sock):
    first, second = _recv_exact(sock, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if length > 2 * 1024 * 1024:
        raise BrowserError("브라우저 제어 응답이 예상보다 커서 중단했습니다.")
    mask = _recv_exact(sock, 4) if second & 0x80 else None
    payload = _recv_exact(sock, length)
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _process_tree(root_pid):
    if os.name != "nt":
        return {int(root_pid)} if _process_identity(root_pid) else set()
    try:
        from ctypes import wintypes
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32FirstW.restype = wintypes.BOOL
        kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel.Process32NextW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        invalid = ctypes.c_void_p(-1).value
        if snapshot == invalid:
            return {int(root_pid)} if _process_identity(root_pid) else set()
        entry = PROCESSENTRY32W(); entry.dwSize = ctypes.sizeof(entry)
        parent = {}
        try:
            ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                parent[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        owned = {int(root_pid)}
        changed = True
        while changed:
            changed = False
            for pid, ppid in parent.items():
                if ppid in owned and pid not in owned:
                    owned.add(pid); changed = True
        return owned
    except Exception:
        return {int(root_pid)} if _process_identity(root_pid) else set()


def _process_identity(pid):
    """Return (creation FILETIME ticks, executable path, RSS bytes)."""
    if os.name == "nt":
        try:
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
                def value(self): return (int(self.high) << 32) | int(self.low)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x0400 | 0x0010 | 0x1000, False, int(pid))
            if not handle:
                return None
            try:
                created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                size = wintypes.DWORD(32_768)
                buf = ctypes.create_unicode_buffer(size.value)
                if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return None
                rss = 0
                try:
                    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
                    memory = PROCESS_MEMORY_COUNTERS(); memory.cb = ctypes.sizeof(memory)
                    psapi = ctypes.WinDLL("psapi", use_last_error=True)
                    if psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb):
                        rss = int(memory.WorkingSetSize)
                except Exception:
                    pass
                return created.value(), buf.value, rss
            finally:
                kernel.CloseHandle(handle)
        except Exception:
            return None
    try:
        import psutil
        proc = psutil.Process(int(pid))
        return int(proc.create_time() * 10_000_000), proc.exe(), int(proc.memory_info().rss)
    except Exception:
        return None


def _window_count(owned):
    if os.name != "nt" or not owned:
        return 0
    try:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("user32", use_last_error=True)
        get_pid = kernel.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD
        kernel.IsWindowVisible.argtypes = [wintypes.HWND]
        kernel.IsWindowVisible.restype = wintypes.BOOL
        found = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            pid = wintypes.DWORD()
            get_pid(hwnd, ctypes.byref(pid))
            if int(pid.value) in owned and kernel.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True
        kernel.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        kernel.EnumWindows.restype = wintypes.BOOL
        kernel.EnumWindows(callback_type(visit), 0)
        return len(found)
    except Exception:
        return 0


def _close_owned_windows(pids):
    if os.name != "nt":
        return
    try:
        from ctypes import wintypes
        kernel = ctypes.WinDLL("user32", use_last_error=True)
        get_pid = kernel.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD
        kernel.IsWindowVisible.argtypes = [wintypes.HWND]
        kernel.IsWindowVisible.restype = wintypes.BOOL
        kernel.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        kernel.PostMessageW.restype = wintypes.BOOL
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        pid_set = set(pids) if not isinstance(pids, int) else {pids}
        def visit(hwnd, _lparam):
            pid = wintypes.DWORD()
            get_pid(hwnd, ctypes.byref(pid))
            if int(pid.value) in pid_set and kernel.IsWindowVisible(hwnd):
                kernel.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True
        kernel.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        kernel.EnumWindows.restype = wintypes.BOOL
        kernel.EnumWindows(callback_type(visit), 0)
    except Exception:
        pass


def _terminate_owned_process(pid, expected_identity=None):
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            def value(self): return (int(self.high) << 32) | int(self.low)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x0001 | 0x1000, False, int(pid))
        if not handle:
            return False
        try:
            if expected_identity:
                created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                size = wintypes.DWORD(32_768)
                buf = ctypes.create_unicode_buffer(size.value)
                if (not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                        ctypes.byref(kernel_time), ctypes.byref(user_time))
                        or created.value() != expected_identity[0]
                        or not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                        or not _same_executable(buf.value, expected_identity[1])):
                    return False
            return bool(kernel.TerminateProcess(handle, 1))
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return False


def _wait_process_exit(process, timeout):
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _same_executable(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _within(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False
