"""A user-launched, local-only V46-03 browser verification window."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox

from .branding import DISPLAY_NAME, KOREAN_NAME, SLOGAN, apply_window_branding
from .research_browser import ResearchBrowserManager, BrowserError
from .resource_guard import sample_resources


def verifier_root(local_app_data: str | os.PathLike | None = None, run_id: str | None = None) -> Path:
    """Return a unique app-data directory used only by this verifier."""
    base = Path(local_app_data) if local_app_data else Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    return base / "ZisuSignalLab" / "BrowserVerification" / (run_id or uuid.uuid4().hex)


def installed_browsers() -> dict[str, str]:
    """Find installed browser executables without starting or changing them."""
    candidates: dict[str, list[str]] = {"Microsoft Edge": [], "Google Chrome": []}
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")]
        for root in filter(None, roots):
            candidates["Microsoft Edge"].append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            candidates["Google Chrome"].append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    for label, executable in (("Microsoft Edge", "msedge"), ("Google Chrome", "chrome")):
        located = shutil.which(executable)
        if located:
            candidates[label].append(located)
    found = {}
    for label, paths in candidates.items():
        path = next((Path(item).resolve() for item in paths if Path(item).is_file()), None)
        if path:
            found[label] = str(path)
    return found


def browser_processes() -> set[tuple[str, int]]:
    """Read image names/PIDs only; no command lines, account names or paths."""
    if os.name != "nt":
        return set()
    try:
        result = subprocess.run(["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        rows = csv.reader(result.stdout.splitlines())
        found = set()
        for row in rows:
            if len(row) >= 2 and row[0].casefold() in {"chrome.exe", "msedge.exe"}:
                try:
                    found.add((row[0].casefold(), int(row[1].replace(",", ""))))
                except ValueError:
                    continue
        return found
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return set()


def _snapshot_pair():
    sample_resources()
    time.sleep(0.25)
    return sample_resources()


def _snapshot_json(snapshot):
    if snapshot is None:
        return None
    return {
        "system_cpu_percent_sample": snapshot.system_cpu_percent,
        "system_available_ram_bytes": snapshot.available_ram_bytes,
        "system_memory_percent": snapshot.system_memory_percent,
        "verifier_process_rss_bytes": snapshot.process_rss_bytes,
        "verifier_process_cpu_percent_sample": snapshot.process_cpu_percent,
    }


def build_report(events, browser=""):
    """Create a safe report payload without credentials or user data."""
    return {"product": f"{DISPLAY_NAME} · {KOREAN_NAME}",
        "slogan": SLOGAN, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "result": "PASS" if events and all(item["state"] == "PASS" for item in events) else "INCOMPLETE_OR_FAIL",
        "browser": browser, "events": list(events),
        "lifecycle_checks": ["A_INITIAL_OPEN", "B_REUSE", "C_USER_CLOSE_OBSERVED",
                             "D_REOPEN_AFTER_CLOSE", "E_DEDICATED_CLOSE", "F_RESIDUAL_PROCESS_CHECK"],
        "external_network_used": False, "oauth_or_user_database_accessed": False,
        "notes": ["Local loopback page only; no YouTube or third-party request.",
                  "System CPU values are interval samples; RAM is a point sample."]}


def _safe_error_result(exc):
    result = {"error_type": type(exc).__name__}
    if isinstance(exc, BrowserError):
        result["message"] = str(exc)
        # Browser manager uses a stable, numeric subprocess exit-code phrase.
        import re
        match = re.search(r"(?:exit code|종료 코드)\s*(\d+)", str(exc), flags=re.IGNORECASE)
        if match:
            result["browser_exit_code"] = int(match.group(1))
    else:
        result["message"] = "예기치 않은 오류가 발생했습니다. 결과의 오류 분류를 확인해 주세요."
    return result


class _LocalPageHandler(BaseHTTPRequestHandler):
    server_version = "ZISU-Local-Test"
    sys_version = ""

    def do_GET(self):
        if self.path not in {"/open", "/reuse"}:
            self.send_error(404)
            return
        self.server.requested_paths.put(self.path)
        body = ("<!doctype html><meta charset='utf-8'><title>ZISU SIGNAL LAB 브라우저 검증</title>"
                "<main style='font:24px sans-serif;color:#123b35;margin:12vh auto;max-width:760px'>"
                "<h1>ZISU SIGNAL LAB</h1><p>로컬 브라우저 연결 확인 페이지입니다.</p>"
                "<p>인터넷·YouTube·로그인 정보는 사용하지 않습니다.</p></main>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def create_local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalPageHandler)
    server.daemon_threads = True
    server.requested_paths = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, name="zisu-local-browser-test", daemon=True)
    thread.start()
    return server, thread


class BrowserVerifierApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        apply_window_branding(root)
        root.title(f"{DISPLAY_NAME} · V46-03 실제 Windows 검증")
        root.geometry("720x540")
        root.minsize(560, 430)
        self.test_root = verifier_root()
        self.events: list[dict] = []
        self.messages: queue.Queue = queue.Queue()
        self._busy = False
        self._server = None
        self._server_thread = None
        self._manager = None
        self._baseline_processes: set[tuple[str, int]] = set()
        self._first_pid = None
        self._first_profile = None
        self._x_reopen_cycles = 0
        self._first_window_count = 0
        self._first_page_loaded = False
        self._selected_browser = ""
        self.browsers = installed_browsers()
        self.browser_names = list(self.browsers)
        preferred = next((name for name in ("Microsoft Edge", "Google Chrome") if name in self.browsers), "")
        self.browser_var = tk.StringVar(value=preferred)
        self.status_var = tk.StringVar(value="브라우저 테스트를 시작하면 독립된 테스트 프로필을 사용합니다.")
        self.detail_var = tk.StringVar(value=f"테스트 자료: {self.test_root}  ·  개인 데이터와는 별도 위치")
        self._buttons = []
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(250, self._poll_messages)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="V46-03 전용 브라우저 확인", font=("Malgun Gothic", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text=SLOGAN, font=("Malgun Gothic", 10)).pack(anchor="w", pady=(2, 8))
        ttk.Label(outer, text="5회 반복: 전용 Chrome 창의 X를 눌러 닫은 뒤 ‘X 닫기 확인 후 다시 열기’를 누르세요.",
                  wraplength=670, justify="left").pack(anchor="w", pady=(0, 8))
        ttk.Label(outer, text="이 도구는 로컬 테스트 페이지와 새 브라우저 프로필만 사용합니다.\n"
                  "YouTube, 사용자 DB, OAuth·Gemini 인증정보에는 접근하지 않습니다.",
                  justify="left", wraplength=670).pack(anchor="w", pady=(0, 10))
        choice = ttk.Frame(outer)
        choice.pack(fill="x", pady=4)
        ttk.Label(choice, text="테스트 브라우저:").pack(side="left")
        self.browser_combo = ttk.Combobox(choice, textvariable=self.browser_var,
            values=self.browser_names, state="readonly", width=32)
        self.browser_combo.pack(side="left", padx=8)
        if not self.browser_names:
            self.status_var.set("Edge 또는 Chrome을 찾지 못했습니다. 설치된 브라우저를 확인해 주세요.")
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 10))
        actions = (
            ("브라우저 열기 테스트", self._open_test),
            ("같은 창 재사용 테스트", self._reuse_test),
            ("X 닫기 확인 후 다시 열기", self._reopen_after_x_test),
            ("전용 브라우저 닫기", self._close_test),
            ("결과 보고서 저장", self._save_report),
        )
        for text, command in actions:
            button = ttk.Button(buttons, text=text, command=command)
            button.pack(fill="x", pady=3, ipady=5)
            self._buttons.append(button)
        ttk.Separator(outer).pack(fill="x", pady=8)
        ttk.Label(outer, textvariable=self.status_var, font=("Malgun Gothic", 11, "bold"),
                  wraplength=670).pack(anchor="w", pady=4)
        ttk.Label(outer, textvariable=self.detail_var, wraplength=670, justify="left").pack(anchor="w", pady=4)
        ttk.Label(outer, text="닫기 테스트는 ZISU가 만든 전용 창만 대상으로 합니다. 기존 Chrome·Edge 창은 종료하지 않습니다.",
                  wraplength=670, foreground="#555555").pack(anchor="w", pady=(8, 0))

    def _ensure_server(self):
        server_usable = bool(self._server and self._server_thread and self._server_thread.is_alive()
                             and self._server.fileno() >= 0)
        if not server_usable:
            self._stop_server()
            self._server, self._server_thread = create_local_server()
        return f"http://127.0.0.1:{self._server.server_port}"

    def _stop_server(self):
        server, thread = self._server, self._server_thread
        self._server = None
        self._server_thread = None
        if server is not None:
            try:
                server.shutdown()
            except (OSError, RuntimeError):
                pass
            try:
                server.server_close()
            except OSError:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _run_async(self, label, function, running_message=None):
        if self._busy:
            return
        self._selected_browser = self.browser_var.get()
        self._busy = True
        for button in self._buttons:
            button.configure(state="disabled")
        self.status_var.set(running_message or f"{label} 중입니다. 잠시만 기다려 주세요.")

        def worker():
            try:
                result = function()
                self.messages.put((True, label, result))
            except Exception as exc:
                self.messages.put((False, label, _safe_error_result(exc)))
        threading.Thread(target=worker, name="zisu-browser-verifier", daemon=True).start()

    def _poll_messages(self):
        try:
            while True:
                ok, label, result = self.messages.get_nowait()
                self._busy = False
                for button in self._buttons:
                    button.configure(state="normal")
                event = {"step": label, "state": "PASS" if ok else "FAIL",
                         "timestamp_utc": datetime.now(timezone.utc).isoformat(), **result}
                event.setdefault("diagnostics", self._diagnostic_snapshot())
                event.setdefault("event_stage", self._lifecycle_stage(label, result))
                self.events.append(event)
                if event.get("user_confirmed_x_close_before_action") or "X 닫기" in label:
                    manager_observed = bool(event.get("close_observed_by_manager")
                        or event.get("diagnostics", {}).get("window_count") == 0)
                    self.events.append({
                        "step": "X 닫기 관찰",
                        "event_stage": "C_USER_CLOSE_OBSERVED",
                        "state": "PASS" if manager_observed else "INCOMPLETE",
                        "timestamp_utc": event["timestamp_utc"],
                        "user_confirmed_x_close": True,
                        "manager_observed_window_count_zero": manager_observed,
                        "prior_lifecycle_state": event.get("prior_lifecycle_state"),
                        "diagnostics": event.get("diagnostics", {}),
                    })
                fallback_status = (("확인 완료: " if ok else "확인 실패: ") + label)
                if not ok and "브라우저 열기" in label:
                    fallback_status = "브라우저를 다시 열지 못했습니다. 아래 안내를 확인해 주세요."
                elif not ok and "브라우저 재사용" in label:
                    fallback_status = "열려 있는 전용 창을 확인하지 못했습니다. 안내에 따라 다시 시도해 주세요."
                self.status_var.set(result.get("status_message") or fallback_status)
                prior_text = ""
                if result.get("user_confirmed_x_close_before_action"):
                    prior_text = (f"\nX 닫기 확인: {result.get('close_observed_by_manager')} · "
                        f"이전 상태 {result.get('prior_lifecycle_state')} · "
                        f"이전 종료 코드 {result.get('prior_exit_code')} · "
                        f"이전 종료 정보 {result.get('prior_close_reason')}")
                if "message" in result:
                    diagnostics = event["diagnostics"]
                    self.detail_var.set(
                        f"{result['message']}{prior_text}\n현재 창 {diagnostics['window_count']}개 · "
                        f"전용 프로세스 {diagnostics['owned_process_count']}개 · "
                        f"상태 {diagnostics['lifecycle_state']} · 종료 코드 {diagnostics['exit_code']} · "
                        f"원인 {diagnostics['close_reason'] or '추가 오류 정보 없음'}")
                elif "detail" in result:
                    diagnostics = event["diagnostics"]
                    self.detail_var.set(
                        f"{result['detail']}{prior_text}\n현재 창 {diagnostics['window_count']}개 · "
                        f"전용 프로세스 {diagnostics['owned_process_count']}개 · "
                        f"상태 {diagnostics['lifecycle_state']} · 종료 코드 {diagnostics['exit_code']}")
                if result.get("close_app"):
                    self._destroy()
                    return
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(250, self._poll_messages)

    def _manager_for_selected(self):
        selected = self._selected_browser
        executable = self.browsers.get(selected)
        if not executable:
            raise BrowserError("설치된 Edge 또는 Chrome을 선택해 주세요.")
        self.test_root.mkdir(parents=True, exist_ok=True)
        if self._manager is None:
            self._manager = ResearchBrowserManager(app_data_root=self.test_root,
                executable=executable, allow_local_test=True)
        return self._manager

    def _diagnostic_snapshot(self):
        snapshot = sample_resources()
        metrics = self._manager.metrics() if self._manager and self._manager.session else None
        return {
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "browser": self._selected_browser,
            "lifecycle_state": metrics.lifecycle_state if metrics else "CLOSED",
            "window_count": metrics.window_count if metrics else 0,
            "owned_process_count": metrics.owned_process_count if metrics else 0,
            "root_pid": metrics.root_pid if metrics else None,
            "owned_pids": list(metrics.child_process_ids) + ([metrics.root_pid] if metrics and metrics.root_pid else []),
            "exit_code": metrics.last_exit_code if metrics else None,
            "close_reason": metrics.last_close_reason if metrics else "",
            "system_cpu_percent_sample": snapshot.system_cpu_percent if snapshot else None,
            "system_available_ram_bytes": snapshot.available_ram_bytes if snapshot else None,
            "system_memory_percent": snapshot.system_memory_percent if snapshot else None,
        }

    @staticmethod
    def _lifecycle_stage(label, result):
        if result.get("user_confirmed_x_close_before_action"):
            return "D_REOPEN_AFTER_CLOSE"
        if "재사용" in label:
            return "B_REUSE"
        if "X 닫기" in label:
            return "D_REOPEN_AFTER_CLOSE"
        if "닫기" in label or result.get("normal_close") is not None:
            return "E_DEDICATED_CLOSE_AND_F_RESIDUAL_PROCESS_CHECK"
        if result.get("reopened_after_user_close"):
            return "D_REOPEN_AFTER_CLOSE"
        return "A_INITIAL_OPEN"

    def _await_page(self, path: str, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                requested = self._server.requested_paths.get(timeout=0.15)
                if requested == path:
                    return True
            except queue.Empty:
                continue
        return False

    def _open_test(self, *, require_closed=False):
        def work():
            manager = self._manager_for_selected()
            was_open = False
            reopened_after_x = False
            prior = None
            if manager.session:
                current = manager.metrics()
                prior = current
                was_open = current.window_count == 1
                reopened_after_x = current.window_count == 0 and current.lifecycle_state in {"USER_CLOSED", "BACKGROUND_PROCESS_ONLY", "CLOSED"}
            elif manager.recovery_available:
                try:
                    recovered = manager.recover_owned_session()
                    prior = recovered
                    was_open = recovered.window_count == 1
                    reopened_after_x = recovered.window_count == 0 and recovered.lifecycle_state in {"USER_CLOSED", "BACKGROUND_PROCESS_ONLY", "CLOSED"}
                except BrowserError:
                    # A stale, non-matching PID record is cleared by the manager;
                    # malformed/uncertain ownership remains blocked there.
                    if manager.recovery_available:
                        raise
            if require_closed and was_open:
                raise BrowserError("전용 창이 아직 열려 있습니다. X 버튼으로 닫은 뒤 다시 눌러 주세요.")
            if was_open:
                raise BrowserError("이미 전용 테스트 창이 있습니다. 같은 창 재사용 테스트를 선택해 주세요.")
            self._baseline_processes = browser_processes()
            before = _snapshot_pair()
            server_url = self._ensure_server()
            first = manager.open_selected(server_url + "/open", user_approved=True)
            page_loaded = self._await_page("/open")
            after = sample_resources()
            self._first_pid = first.root_pid
            self._first_profile = manager.session.profile_dir if manager.session else None
            self._first_window_count = first.window_count
            self._first_page_loaded = page_loaded
            detail = {"page_loaded": page_loaded, "window_count": first.window_count,
                "owned_process_count": first.owned_process_count, "owned_rss_bytes": first.owned_rss_bytes,
                "owned_pids": list(first.child_process_ids) + ([first.root_pid] if first.root_pid else []),
                "lifecycle_state": first.lifecycle_state, "exit_code": first.last_exit_code,
                "last_close_reason": first.last_close_reason,
                "user_confirmed_x_close_before_action": bool(require_closed),
                "close_observed_by_manager": bool(prior and prior.window_count == 0),
                "prior_lifecycle_state": prior.lifecycle_state if prior else None,
                "prior_exit_code": prior.last_exit_code if prior else None,
                "prior_close_reason": prior.last_close_reason if prior else "not_observed",
                "prior_owned_process_count": prior.owned_process_count if prior else 0,
                "prior_owned_pids": list(prior.child_process_ids) + ([prior.root_pid] if prior and prior.root_pid else []),
                "x_close_reopen_cycle": self._x_reopen_cycles if require_closed else None,
                "process_cpu_system_before": _snapshot_json(before), "process_cpu_system_after": _snapshot_json(after),
                "baseline_browser_processes": sorted(self._baseline_processes),
                "browser": self._selected_browser}
            if not page_loaded or first.window_count != 1:
                raise BrowserError("브라우저 창은 열렸지만 로컬 페이지 로드 또는 창 1개를 확인하지 못했습니다.")
            return {"status_message": "브라우저 연결 성공",
                    "detail": ("창 닫기 후 다시 열기 PASS · " if reopened_after_x else "")
                    + "로컬 페이지 로드 PASS · 전용 창 1개 확인 · 사용자 브라우저 프로필과 분리됨",
                    "reopened_after_user_close": reopened_after_x,
                    **detail}
        self._run_async("브라우저 열기 테스트", work, running_message="브라우저 실행 중...")

    def _reopen_after_x_test(self):
        self._x_reopen_cycles += 1
        self._open_test(require_closed=True)

    def _reuse_test(self):
        def work():
            manager = self._manager_for_selected()
            if not manager.session or not self._first_page_loaded:
                raise BrowserError("먼저 브라우저 열기 테스트를 성공시켜 주세요.")
            before = manager.metrics()
            if before.window_count != 1:
                raise BrowserError("재사용 전 전용 창이 하나인지 확인되지 않아 새 요청을 보내지 않았습니다.")
            after = manager.open_selected(self._ensure_server() + "/reuse", user_approved=True)
            page_loaded = self._await_page("/reuse")
            same_profile = bool(self._first_profile and manager.session.profile_dir == self._first_profile)
            one_window = after.window_count == 1 and before.window_count == 1
            if not (page_loaded and same_profile and one_window):
                raise BrowserError("같은 창 재사용 또는 두 번째 로컬 페이지 로드를 확인하지 못했습니다.")
            return {"detail": "두 번째 로컬 페이지 PASS · 같은 브라우저 PID · 창 1개 유지",
                "page_loaded": page_loaded, "same_isolated_profile": same_profile,
                "root_pid_before": self._first_pid, "root_pid_after": after.root_pid,
                "root_pid_changed": after.root_pid != self._first_pid,
                "window_count_before": before.window_count, "window_count_after": after.window_count,
                "owned_process_count": after.owned_process_count, "owned_pids": list(after.child_process_ids),
                "lifecycle_state": after.lifecycle_state, "exit_code": after.last_exit_code,
                "owned_rss_bytes": after.owned_rss_bytes}
        self._run_async("같은 창 재사용 테스트", work)

    def _close_test(self):
        def work():
            if self._manager is None or self._manager.session is None:
                raise BrowserError("닫을 전용 테스트 창이 없습니다.")
            before = _snapshot_pair()
            owned_pids = set(self._manager.metrics().child_process_ids)
            if self._manager.metrics().root_pid:
                owned_pids.add(self._manager.metrics().root_pid)
            result = self._manager.close_owned(grace_seconds=10)
            after = sample_resources()
            current_processes = browser_processes()
            baseline_ids = {pid for _, pid in self._baseline_processes}
            current_ids = {pid for _, pid in current_processes}
            preserved = baseline_ids.issubset(current_ids)
            if result.forced_process_ids:
                raise BrowserError("전용 창이 정상 종료되지 않아 소유 프로세스를 강제 종료했습니다. 정상 종료 검증은 실패입니다.")
            if not result.closed or result.residual_process_ids:
                raise BrowserError("전용 창 또는 자식 프로세스가 남아 있습니다. 이 창을 닫지 말고 결과를 저장해 주세요.")
            if not preserved:
                raise BrowserError("테스트 시작 때 있던 일반 브라우저 일부를 확인할 수 없습니다. 테스트 중 사용자가 닫았는지 확인해 주세요.")
            self._first_page_loaded = False
            self._first_pid = None
            self._first_profile = None
            self._first_window_count = 0
            return {"status_message": "브라우저를 안전하게 종료했습니다. 다시 실행할 준비가 완료됐습니다.",
                "detail": "전용 창 정상 종료 PASS · 잔류 프로세스 0 · 시작 전 일반 브라우저 PID 보존",
                "normal_close": not result.forced_process_ids, "residual_process_ids": list(result.residual_process_ids),
                "owned_process_ids": sorted(owned_pids), "user_browsers_preserved": preserved,
                "window_count": 0, "owned_process_count": len(result.residual_process_ids),
                "owned_pids_after": list(result.residual_process_ids), "lifecycle_state": "CLOSED",
                "baseline_browser_count": len(self._baseline_processes), "browser_count_after": len(current_processes),
                "browser_rss_before_bytes": result.rss_before_bytes, "browser_rss_after_bytes": result.rss_after_bytes,
                "system_resource_before": _snapshot_json(before), "system_resource_after": _snapshot_json(after)}
        self._run_async("전용 브라우저 닫기", work, running_message="브라우저를 안전하게 종료하는 중...")

    def _save_report(self):
        try:
            self.test_root.mkdir(parents=True, exist_ok=True)
            path = self.test_root / "browser-verification-report.json"
            payload = build_report(self.events, self._selected_browser or self.browser_var.get())
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            os.replace(temp, path)
            self.detail_var.set(f"보고서 저장 완료: {path}")
            self.events.append({"step": "결과 보고서 저장", "state": "PASS", "path": str(path),
                                "timestamp_utc": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:
            self.events.append({"step": "결과 보고서 저장", "state": "FAIL",
                "error_type": type(exc).__name__, "timestamp_utc": datetime.now(timezone.utc).isoformat()})
            messagebox.showerror("보고서 저장 실패", "테스트 폴더에 보고서를 저장하지 못했습니다. 사용자 문서 폴더 권한을 확인해 주세요.", parent=self.root)

    def _on_close(self):
        if self._busy:
            messagebox.showinfo("검증 진행 중", "현재 확인이 끝난 뒤 창을 닫아 주세요.", parent=self.root)
            return
        if self._manager and self._manager.session:
            if not messagebox.askyesno("전용 브라우저가 열려 있어요",
                    "ZISU가 만든 테스트 창만 닫고 종료할까요? 기존 Chrome·Edge 창은 닫지 않습니다.", parent=self.root):
                return
            def work():
                result = self._manager.close_owned(grace_seconds=10)
                if result.forced_process_ids:
                    self.events.append({"step": "프로그램 종료 정리", "state": "FAIL",
                        "forced_process_ids": list(result.forced_process_ids),
                        "timestamp_utc": datetime.now(timezone.utc).isoformat()})
                if not result.closed:
                    raise BrowserError("전용 브라우저가 남아 있습니다. ZISU 소유권 기록을 보존했습니다.")
                return {"detail": "전용 테스트 창을 닫았습니다.", "close_app": True}
            self._run_async("전용 브라우저 정리", work)
            return
        self._destroy()

    def _destroy(self):
        self._stop_server()
        self.root.destroy()


def main():
    root = tk.Tk()
    BrowserVerifierApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
