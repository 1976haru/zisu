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
import webbrowser
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
    lifecycle_state: str = "STARTING"
    last_window_count: int = 0
    last_exit_code: int | None = None
    last_close_reason: str = ""
    last_profile_scan: float = 0.0


@dataclass(frozen=True)
class BrowserMetrics:
    running: bool
    root_pid: int | None
    owned_process_count: int
    owned_rss_bytes: int
    window_count: int
    child_process_ids: tuple[int, ...] = ()
    rss_change_bytes: int = 0
    lifecycle_state: str = "CLOSED"
    last_exit_code: int | None = None
    last_close_reason: str = ""


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
    """Prefer installed Edge, then Chrome; never install or download a browser."""
    candidates = []
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")]
        for root in roots:
            if root:
                candidates.extend((os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                    os.path.join(root, "Google", "Chrome", "Application", "chrome.exe")))
    candidates.extend((shutil.which("msedge"), shutil.which("chrome")))
    seen = set()
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            resolved = str(Path(candidate).resolve())
            key = os.path.normcase(resolved)
            if key not in seen:
                seen.add(key)
                return resolved
    return None


def open_manual_research_url(url: str, *, opener=None) -> bool:
    """Open a user-approved video in the default browser without claiming ownership."""
    safe_url = validate_research_url(url)
    return bool((opener or webbrowser.open)(safe_url, new=2, autoraise=True))


class ResearchBrowserManager:
    """Own at most one browser process tree and one isolated profile per session."""

    def __init__(self, *, app_data_root: str | os.PathLike | None = None,
                 executable: str | None = None, resource_guard: ResourceGuard | None = None,
                 resource_sampler=sample_resources, launcher=subprocess.Popen,
                 allow_local_test: bool = False, startup_stability_seconds: float = 1.5):
        root = app_data_root or _default_app_data_root()
        self.root = Path(root)
        self.profile_root = self.root / "research-browser-profiles"
        self.manifest_path = self.root / "research-browser-session.json"
        self.executable = str(Path(executable).resolve()) if executable else find_browser()
        self.resource_guard = resource_guard or ResourceGuard()
        self.resource_sampler = resource_sampler
        self.launcher = launcher
        self.allow_local_test = bool(allow_local_test)
        self.startup_stability_seconds = max(0.0, min(5.0, float(startup_stability_seconds)))
        self._lock = threading.RLock()
        self.session: BrowserSession | None = None
        self._recovery_record = self._load_recovery_record()

    @property
    def recovery_available(self) -> bool:
        return bool(self._recovery_record)

    def request_close_owned_windows(self) -> tuple[int, ...]:
        """Ask only this session's verified browser windows to close normally.

        This is the non-forcing equivalent of a user clicking the window's X.
        It never searches for or closes browser processes by image name.
        """
        with self._lock:
            if not self.session:
                return ()
            self._refresh_owned_processes(self.session)
            metrics = self.metrics()
            pids = set(metrics.child_process_ids)
            if metrics.root_pid:
                pids.add(metrics.root_pid)
            _close_owned_windows(pids)
            self.session.last_close_reason = "verifier_requested_wm_close"
            return tuple(sorted(pids))

    def open_selected(self, url: str, *, user_approved: bool = False) -> BrowserMetrics:
        if not user_approved:
            raise BrowserError("연구용 인터넷 창을 열려면 사용자가 먼저 승인해야 합니다.")
        safe_url = validate_research_url(url, allow_local_test=self.allow_local_test)
        with self._lock:
            # A previous application run can leave a manifest behind. Reattach only
            # when the exact recorded PID, creation time and image still match.
            # A stale PID record is cleared by recover_owned_session without ever
            # touching a process that no longer proves ownership.
            if self.session is None and self._recovery_record:
                try:
                    self.recover_owned_session()
                except BrowserError:
                    if self._recovery_record is not None:
                        raise
            reuse_profile = None
            if self.session:
                before = self.metrics()
                if before.window_count > 1:
                    raise BrowserOwnershipUncertain("전용 창이 여러 개 감지되어 중복 실행을 막았습니다. 전용 창을 하나만 남긴 뒤 다시 시도하세요.")
                if before.running:
                    # A root PID can exit while the browser's profile-owned main
                    # process remains. Reuse is allowed only after exact profile
                    # process discovery, never by image name alone.
                    return self._open_in_owned_session(safe_url, before)
                reuse_profile = (self.session.session_id, self.session.profile_dir)
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
            if reuse_profile and _within(reuse_profile[1], self.profile_root):
                session_id, profile_text = reuse_profile
                profile = Path(profile_text)
                profile.mkdir(parents=True, exist_ok=True)
            else:
                session_id = uuid.uuid4().hex
                profile = self.profile_root / session_id
                profile.mkdir(parents=True, exist_ok=False)
            command = [self.executable, f"--user-data-dir={profile}",
                       "--no-first-run", "--no-default-browser-check", "--disable-sync",
                       "--disable-background-mode", "--disable-background-networking",
                       "--disable-component-update", "--new-window"]
            if _is_local_test_url(safe_url):
                command.append("--no-proxy-server")
            command.append(safe_url)
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
                        stable_until = time.monotonic() + self.startup_stability_seconds
                        while time.monotonic() < stable_until:
                            self._refresh_owned_processes(self.session)
                            if not self._alive_owned(self.session):
                                exit_code = process.poll()
                                self.session.last_exit_code = exit_code
                                self.session.lifecycle_state = "CLOSED" if exit_code == 0 else "ERROR"
                                self.session.last_close_reason = "normal_exit" if exit_code == 0 else "process_exit"
                                detail = f" (종료 코드 {exit_code})" if exit_code is not None else ""
                                outcome = "정상 종료 코드 0이며 충돌로 분류하지 않았습니다." if exit_code == 0 else "비정상 종료로 분류했습니다."
                                raise BrowserError("연구용 브라우저 창이 시작 확인 중 닫혔습니다. " + outcome + detail)
                            if self.metrics().window_count != 1:
                                raise BrowserError("연구용 브라우저 창이 계속 표시되는지 확인하지 못했습니다.")
                            time.sleep(0.1)
                        break
                    if not self._alive_owned(self.session):
                        exit_code=process.poll()
                        detail=f" (종료 코드 {exit_code})" if exit_code is not None else ""
                        outcome = "종료 코드 0(정상 종료)" if exit_code == 0 else "비정상 종료"
                        raise BrowserError("연구용 브라우저가 창을 유지하지 못했습니다. " + outcome + detail)
                    time.sleep(0.1)
                else:
                    raise BrowserError("연구용 창 표시를 확인할 수 없어 소유한 프로세스만 정리했습니다.")
                metrics=self.metrics()
                self.session.lifecycle_state = "OPEN"
                self.session.last_window_count = metrics.window_count
                self.session.initial_rss_bytes=metrics.owned_rss_bytes
                self._write_manifest(self.session)
                return metrics
            except Exception:
                self.close_owned(grace_seconds=2)
                raise

    def _open_in_owned_session(self, safe_url: str, before: BrowserMetrics) -> BrowserMetrics:
        session = self.session
        if session is None:
            raise BrowserOwnershipUncertain("전용 브라우저 세션이 더 이상 연결되어 있지 않습니다.")
        args = [session.executable, f"--user-data-dir={session.profile_dir}",
                "--no-first-run", "--no-default-browser-check", "--disable-sync"]
        if _is_local_test_url(safe_url):
            args.append("--no-proxy-server")
        args.append(safe_url)
        try:
            helper = self.launcher(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        except (OSError, ValueError):
            raise BrowserError("전용 브라우저를 다시 연결하지 못했습니다. 창을 닫은 뒤 다시 열거나 수동 브라우저를 사용하세요.") from None
        helper_identity = _process_identity(helper.pid)
        if helper_identity and _same_executable(helper_identity[1], session.executable):
            session.owned_processes.setdefault(helper.pid, helper_identity[:2])
        try:
            helper.wait(timeout=3)
        except subprocess.TimeoutExpired:
            helper_identity = _process_identity(helper.pid)
            if helper_identity and _same_executable(helper_identity[1], session.executable):
                session.owned_processes.setdefault(helper.pid, helper_identity[:2])

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self._refresh_owned_processes(session)
            after = self.metrics()
            if after.window_count == 1 and after.running:
                session.lifecycle_state = "OPEN"
                session.last_window_count = 1
                if after.root_pid and after.root_pid != session.pid:
                    # The old launcher/root can exit after Chromium hands the
                    # profile to another verified process. Record that identity.
                    identity = session.owned_processes.get(after.root_pid)
                    if identity:
                        session.pid, session.creation_time = after.root_pid, identity[0]
                self._write_manifest(session)
                return after
            if not self._alive_owned(session):
                break
            time.sleep(0.1)
        raise BrowserOwnershipUncertain(
            "기존 전용 창을 다시 연결하지 못했습니다. 브라우저 상태를 확인한 뒤 새로 열어 주세요.")

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
            windows = _window_count(alive)
            running = bool(alive)
            exit_code = session.process.poll() if session.process else session.last_exit_code
            if windows:
                session.lifecycle_state = "OPEN"
                session.last_close_reason = ""
            elif session.lifecycle_state == "CLOSING":
                pass
            elif session.last_window_count > 0:
                # Closing the last managed top-level window is a normal user
                # close signal. A still-live Chromium process is then reported
                # separately as background-only.
                if not running and exit_code == 0:
                    session.lifecycle_state = "CLOSED"
                    session.last_close_reason = "normal_exit_code_0"
                elif exit_code not in (None, 0):
                    session.lifecycle_state = "ERROR"
                    session.last_close_reason = "nonzero_process_exit"
                else:
                    session.lifecycle_state = "USER_CLOSED"
                    session.last_close_reason = "last_window_disappeared"
            elif running and windows == 0:
                session.lifecycle_state = "BACKGROUND_PROCESS_ONLY"
            elif not running:
                if exit_code == 0 and session.last_window_count > 0:
                    session.lifecycle_state = "CLOSED"
                    session.last_close_reason = "normal_exit_code_0"
                elif exit_code not in (None, 0):
                    session.lifecycle_state = "ERROR"
                    session.last_close_reason = "nonzero_process_exit"
                else:
                    session.lifecycle_state = "CLOSED"
            session.last_exit_code = exit_code
            session.last_window_count = windows
            active_pid = session.pid if session.pid in alive else (min(alive) if alive else None)
            if active_pid and active_pid != session.pid:
                session.pid = active_pid
                session.creation_time = alive[active_pid][0]
                self._write_manifest(session)
            return BrowserMetrics(running, active_pid, len(alive), rss, windows,
                                  tuple(sorted(set(alive) - {active_pid} if active_pid else alive)),
                                  rss - session.initial_rss_bytes, session.lifecycle_state,
                                  exit_code, session.last_close_reason)

    def close_owned(self, *, grace_seconds: float = 10.0) -> BrowserCloseResult:
        with self._lock:
            session = self.session
            if session is None:
                return BrowserCloseResult(True, 0, 0, (), ())
            if not self._session_alive(session):
                self._refresh_owned_processes(session)
            self._refresh_owned_processes(session)
            before = self.metrics()
            session.lifecycle_state = "CLOSING"
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
                if session.process and session.pid in forced:
                    try:
                        session.process.wait(timeout=2)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
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
            valid_recorded_pid = bool(current and current[0] == creation and _same_executable(current[1], executable))
            profile_processes = _profile_owned_processes(executable, profile) if _within(profile, self.profile_root) else {}
            if not _within(profile, self.profile_root):
                raise BrowserOwnershipUncertain("격리 프로필 위치를 확인할 수 없어 브라우저에 연결하지 않았습니다.")
            if not valid_recorded_pid and not profile_processes:
                # Preserve the validated isolated profile path when the old
                # process is gone. The caller can safely relaunch into this same
                # profile after the normal resource/approval gates.
                self.executable = executable
                self.session = BrowserSession(str(record["session_id"]), pid, creation, executable,
                    profile, float(record.get("started_at", time.time())), process=None,
                    owned_processes={}, lifecycle_state="CLOSED",
                    last_window_count=int(record.get("last_window_count", 0) or 0),
                    last_exit_code=record.get("last_exit_code"),
                    last_close_reason="process_not_found_after_restart")
                self.session.initial_rss_bytes = int(record.get("initial_rss_bytes", 0) or 0)
                return self.metrics()
            owned = dict(profile_processes)
            if valid_recorded_pid:
                owned[pid] = current[:2]
            active_pid = pid if valid_recorded_pid else min(owned)
            active_identity = owned[active_pid]
            self.executable = executable
            self.session = BrowserSession(str(record["session_id"]), active_pid, active_identity[0], executable,
                profile, float(record.get("started_at", time.time())),
                process=None, owned_processes=owned,
                lifecycle_state=str(record.get("lifecycle_state", "STARTING")),
                last_window_count=int(record.get("last_window_count", 0) or 0),
                last_exit_code=record.get("last_exit_code"),
                last_close_reason=str(record.get("last_close_reason", "")))
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
        # Never expand a process tree from a stale PID: Windows may reuse it.
        # Every root must still match its recorded creation time and image.
        valid = {pid: expected for pid, expected in session.owned_processes.items()
                 if (current := _process_identity(pid)) and current[0] == expected[0]
                 and _same_executable(current[1], expected[1])}
        session.owned_processes = valid
        roots = tuple(valid)
        tree_by_root = {}
        for root_pid in roots:
            expected = valid[root_pid]
            tree = _process_tree(root_pid)
            current_root = _process_identity(root_pid)
            if current_root and current_root[0] == expected[0] and _same_executable(current_root[1], expected[1]):
                tree_by_root[root_pid] = tree
        for tree in tree_by_root.values():
            for pid in tree:
                identity = _process_identity(pid)
                if identity and _same_executable(identity[1], session.executable):
                    session.owned_processes.setdefault(pid, identity[:2])
        now = time.monotonic()
        if (now - session.last_profile_scan >= 1.0
                and (not roots or not any(pid == session.pid for pid in roots))):
            session.last_profile_scan = now
            session.owned_processes.update(_profile_owned_processes(session.executable, session.profile_dir))

    def _write_manifest(self, session):
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": session.session_id, "pid": session.pid,
            "creation_time": session.creation_time, "executable": session.executable,
            "profile_dir": session.profile_dir, "started_at": session.started_at,
            "initial_rss_bytes": session.initial_rss_bytes,
            "lifecycle_state": session.lifecycle_state,
            "last_window_count": session.last_window_count,
            "last_exit_code": session.last_exit_code,
            "last_close_reason": session.last_close_reason}
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


def _profile_owned_processes(executable, profile_dir):
    """Discover live processes only when image and exact isolated profile match."""
    if os.name != "nt":
        return {}
    expected_profile = os.path.normcase(os.path.abspath(str(profile_dir)))
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
        if snapshot == ctypes.c_void_p(-1).value:
            return {}
        pids = []
        expected_image = Path(executable).name.casefold()
        entry = PROCESSENTRY32W(); entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if str(entry.szExeFile).casefold() == expected_image:
                    pids.append(int(entry.th32ProcessID))
                ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        matched = {}
        for pid in pids:
            identity = _process_identity(pid)
            if not identity or not _same_executable(identity[1], executable):
                continue
            command_line = _process_command_line(pid)
            if command_line and _command_uses_profile(command_line, expected_profile):
                matched[pid] = identity[:2]
        return matched
    except Exception:
        return {}


def _process_command_line(pid):
    """Read a process command line on Windows; access failures fail closed."""
    if os.name != "nt":
        return None
    handle = None
    try:
        from ctypes import wintypes
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        class PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [("ExitStatus", ctypes.c_long), ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_long),
                ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p)]
        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        kernel.ReadProcessMemory.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, int(pid))
        if not handle:
            return None
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        basic = PROCESS_BASIC_INFORMATION()
        returned = wintypes.ULONG()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(basic),
                ctypes.sizeof(basic), ctypes.byref(returned)) != 0 or not basic.PebBaseAddress:
            return None
        peb = int(basic.PebBaseAddress)
        parameters_offset = 0x20 if pointer_size == 8 else 0x10
        command_line_offset = 0x70 if pointer_size == 8 else 0x40
        pointer = ctypes.c_void_p()
        read = ctypes.c_size_t()
        if not kernel.ReadProcessMemory(handle, ctypes.c_void_p(peb + parameters_offset),
                ctypes.byref(pointer), pointer_size, ctypes.byref(read)) or not pointer.value:
            return None
        remote_unicode = UNICODE_STRING()
        if not kernel.ReadProcessMemory(handle, ctypes.c_void_p(int(pointer.value) + command_line_offset),
                ctypes.byref(remote_unicode), ctypes.sizeof(remote_unicode), ctypes.byref(read)):
            return None
        if remote_unicode.Length <= 0 or remote_unicode.Length > 32768:
            return None
        buffer = ctypes.create_string_buffer(remote_unicode.Length)
        if not kernel.ReadProcessMemory(handle, remote_unicode.Buffer, buffer,
                remote_unicode.Length, ctypes.byref(read)) or read.value != remote_unicode.Length:
            return None
        return buffer.raw.decode("utf-16-le", errors="strict")
    except Exception:
        return None
    finally:
        if handle:
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            except Exception:
                pass


def _command_uses_profile(command_line, expected_profile):
    """Match Chromium's --user-data-dir argument without substring ambiguity."""
    expected_profile = os.path.normcase(os.path.abspath(str(expected_profile)))
    if os.name == "nt":
        try:
            shell = ctypes.WinDLL("shell32", use_last_error=True)
            shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
            shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            count = ctypes.c_int()
            argv = shell.CommandLineToArgvW(str(command_line), ctypes.byref(count))
            if not argv:
                return False
            try:
                args = [argv[i] for i in range(count.value)]
            finally:
                kernel.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
        except Exception:
            return False
    else:
        import shlex
        try:
            args = shlex.split(command_line)
        except ValueError:
            return False
    for index, arg in enumerate(args):
        value = None
        if arg.casefold().startswith("--user-data-dir="):
            value = arg.split("=", 1)[1]
        elif arg.casefold() == "--user-data-dir" and index + 1 < len(args):
            value = args[index + 1]
        if value and os.path.normcase(os.path.abspath(value)) == expected_profile:
            return True
    return False


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
        kernel.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        kernel.GetClassNameW.restype = ctypes.c_int
        kernel.IsWindowVisible.argtypes = [wintypes.HWND]
        kernel.IsWindowVisible.restype = wintypes.BOOL
        found = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            pid = wintypes.DWORD()
            get_pid(hwnd, ctypes.byref(pid))
            class_name = ctypes.create_unicode_buffer(128)
            kernel.GetClassNameW(hwnd, class_name, len(class_name))
            if (int(pid.value) in owned and kernel.IsWindowVisible(hwnd)
                    and _is_browser_main_window(class_name.value)):
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
        kernel.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        kernel.GetClassNameW.restype = ctypes.c_int
        kernel.IsWindowVisible.argtypes = [wintypes.HWND]
        kernel.IsWindowVisible.restype = wintypes.BOOL
        kernel.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        kernel.PostMessageW.restype = wintypes.BOOL
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        pid_set = set(pids) if not isinstance(pids, int) else {pids}
        def visit(hwnd, _lparam):
            pid = wintypes.DWORD()
            get_pid(hwnd, ctypes.byref(pid))
            class_name = ctypes.create_unicode_buffer(128)
            kernel.GetClassNameW(hwnd, class_name, len(class_name))
            if (int(pid.value) in pid_set and kernel.IsWindowVisible(hwnd)
                    and _is_browser_main_window(class_name.value)):
                kernel.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            return True
        kernel.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        kernel.EnumWindows.restype = wintypes.BOOL
        kernel.EnumWindows(callback_type(visit), 0)
    except Exception:
        pass


def _is_browser_main_window(class_name: str) -> bool:
    # Chromium's top-level browser frame; excludes UAC/input overlays and helper windows.
    return class_name == "Chrome_WidgetWin_1"


def _is_local_test_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}


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
