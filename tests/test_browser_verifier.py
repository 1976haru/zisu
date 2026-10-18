import json
import unittest
from pathlib import Path
from types import SimpleNamespace
import uuid
from urllib.request import urlopen
from unittest.mock import patch

from mcrs.browser_verifier import (
    _safe_error_result,
    build_report,
    create_local_server,
    verifier_root,
    wait_for_window_state,
)
from mcrs.research_browser import BrowserError, BrowserMetrics


class BrowserVerifierTests(unittest.TestCase):
    def test_test_data_root_is_unique_and_separate(self):
        temp = Path.cwd() / ".test_tmp" / f"browser-verifier-test-{uuid.uuid4().hex}"
        first = verifier_root(temp, "one")
        second = verifier_root(temp, "two")
        self.assertNotEqual(first, second)
        self.assertEqual(first.parts[-3:-1], ("ZisuSignalLab", "BrowserVerification"))
        self.assertNotIn("ZisuResearchStudio", str(first))

    def test_local_page_load_is_loopback_only_and_records_request(self):
        server, thread = create_local_server()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/open", timeout=2) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("로컬 브라우저 연결 확인 페이지", body)
            self.assertEqual(server.requested_paths.get(timeout=1), "/open")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_verifier_replaces_stopped_local_server_before_next_open(self):
        from mcrs.browser_verifier import BrowserVerifierApp
        app = BrowserVerifierApp.__new__(BrowserVerifierApp)
        app._server, app._server_thread = create_local_server()
        old_server = app._server
        old_thread = app._server_thread
        old_server.shutdown()
        old_server.server_close()
        old_thread.join(timeout=2)
        url = app._ensure_server()
        self.assertIsNot(old_server, app._server)
        self.assertTrue(app._server_thread.is_alive())
        try:
            with urlopen(url + "/open", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("ZISU SIGNAL LAB", response.read().decode("utf-8"))
        finally:
            app._stop_server()
        self.assertFalse(app._server_thread)

    def test_report_never_claims_live_network_or_user_data_access(self):
        payload = build_report([{"step": "local", "state": "PASS"}], "Microsoft Edge")
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["result"], "PASS")
        self.assertFalse(payload["external_network_used"])
        self.assertFalse(payload["oauth_or_user_database_accessed"])
        self.assertNotIn("token", encoded.casefold())
        self.assertNotIn("client_secret", encoded.casefold())

    def test_report_does_not_call_incomplete_run_a_pass(self):
        payload = build_report([{"step": "open", "state": "FAIL"}])
        self.assertEqual(payload["result"], "INCOMPLETE_OR_FAIL")

    def test_report_includes_automatic_counts_and_verdict(self):
        summary = {"requested_cycles": 5, "completed_cycles": 4, "success_count": 3,
                   "failure_count": 1, "residual_process_count": 0,
                   "final_verdict": "INCOMPLETE_OR_FAIL"}
        payload = build_report([{"step": "cycle", "state": "FAIL", "owned_rss_bytes": 100}],
                               "Google Chrome", summary)
        self.assertEqual(payload["total_test_rounds"], 4)
        self.assertEqual(payload["success_count"], 3)
        self.assertEqual(payload["failure_count"], 1)
        self.assertEqual(payload["max_owned_rss_bytes"], 100)
        self.assertEqual(payload["final_verdict"], "INCOMPLETE_OR_FAIL")

    def test_wait_for_window_state_tolerates_transient_zero_window_snapshot(self):
        values = iter([BrowserMetrics(True, 1, 4, 100, 0),
                       BrowserMetrics(True, 1, 4, 100, 1),
                       BrowserMetrics(True, 1, 4, 100, 1)])
        with patch("mcrs.browser_verifier.time.sleep", return_value=None):
            result = wait_for_window_state(lambda: next(values), 1,
                timeout=1, stable_for=0, interval=0.02)
        self.assertEqual(result.window_count, 1)

    def test_wait_for_window_state_does_not_pass_unstable_window_snapshot(self):
        metrics = BrowserMetrics(True, 1, 4, 100, 1)
        result = wait_for_window_state(lambda: metrics, 1,
            timeout=0.05, stable_for=5, interval=0.01)
        self.assertIsNone(result)

    def test_local_server_accepts_only_scoped_auto_routes(self):
        server, thread = create_local_server()
        try:
            good = f"http://127.0.0.1:{server.server_port}/auto/0123abcd/2/reopen"
            with urlopen(good, timeout=2) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(server.requested_paths.get(timeout=1), "/auto/0123abcd/2/reopen")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_browser_process_enumeration_denied_is_unknown_not_empty(self):
        from mcrs.browser_verifier import browser_processes
        denied = SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Access is denied")
        with patch("mcrs.browser_verifier.os.name", "nt"), \
                patch("mcrs.browser_verifier.subprocess.run", return_value=denied):
            self.assertIsNone(browser_processes())

    def test_full_automatic_pipeline_mock_runs_and_saves_report(self):
        from mcrs.browser_verifier import BrowserVerifierApp

        class FakeManager:
            def __init__(self):
                self.session = SimpleNamespace(profile_dir="isolated-profile")
                self.open_count = 0
                self.windows = 0

            def metrics(self):
                return BrowserMetrics(bool(self.session), 777 if self.session else None,
                    3 if self.session else 0, 2048 if self.session else 0,
                    self.windows, (778, 779) if self.session else (),
                    lifecycle_state="OPEN" if self.windows else "USER_CLOSED")

            def open_selected(self, url, *, user_approved=False):
                self.assert_approved = user_approved
                with urlopen(url, timeout=2) as response:
                    self.assert_page = response.status == 200
                self.windows = 1
                self.open_count += 1
                return self.metrics()

            def request_close_owned_windows(self):
                self.windows = 0
                return (777, 778, 779)

            def close_owned(self, grace_seconds=5):
                self.windows = 0
                self.session = None
                return SimpleNamespace(closed=True, forced_process_ids=(), residual_process_ids=(),
                    rss_before_bytes=2048, rss_after_bytes=0)

        app = BrowserVerifierApp.__new__(BrowserVerifierApp)
        app._selected_browser = "Google Chrome"
        app.browsers = {"Google Chrome": "mock-browser.exe"}
        app._server, app._server_thread = None, None
        app._manager = FakeManager()
        app._manager_for_selected = lambda: app._manager
        app._baseline_processes = set()
        app._seen_paths = set()
        app.test_root = Path.cwd() / ".test_tmp" / f"auto-verifier-{uuid.uuid4().hex}"
        app.events = []
        app.messages = __import__("queue").Queue()
        with patch("mcrs.browser_verifier.browser_processes", return_value=set()), \
                patch("mcrs.browser_verifier._process_handle_count", return_value=4):
            result = app._run_full_auto_test(5)
        try:
            self.assertEqual(result["state"], "PASS")
            self.assertEqual(result["auto_summary"]["completed_cycles"], 5)
            self.assertEqual(result["auto_summary"]["success_count"], 5)
            self.assertEqual(result["auto_summary"]["failure_count"], 0)
            self.assertEqual(result["auto_summary"]["residual_process_count"], 0)
            report_path = Path(result["auto_summary"]["report_path"])
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["final_verdict"], "PASS")
            self.assertTrue(any(event["event_stage"] == "D_REOPEN_AFTER_CLOSE"
                                for event in payload["events"]))
            self.assertEqual(app._manager.open_count, 15)
        finally:
            app._stop_server()

    def test_report_declares_separate_browser_lifecycle_stages(self):
        payload = build_report([{"step": "open", "state": "PASS", "event_stage": "A_INITIAL_OPEN"}])
        self.assertIn("C_USER_CLOSE_OBSERVED", payload["lifecycle_checks"])
        self.assertIn("F_RESIDUAL_PROCESS_CHECK", payload["lifecycle_checks"])

    def test_lifecycle_stage_classification_is_not_based_on_exit_code_zero_as_crash(self):
        from mcrs.browser_verifier import BrowserVerifierApp
        self.assertEqual("D_REOPEN_AFTER_CLOSE", BrowserVerifierApp._lifecycle_stage(
            "X 닫기 확인 후 다시 열기", {"exit_code": 0}))
        self.assertEqual("E_DEDICATED_CLOSE_AND_F_RESIDUAL_PROCESS_CHECK",
            BrowserVerifierApp._lifecycle_stage("전용 브라우저 닫기", {"normal_close": True}))

    def test_x_close_reopen_cycle_is_user_confirmed_and_incremented(self):
        from mcrs.browser_verifier import BrowserVerifierApp
        app = BrowserVerifierApp.__new__(BrowserVerifierApp)
        app._x_reopen_cycles = 0
        app._open_test = lambda **kwargs: self.assertTrue(kwargs["require_closed"])
        app._reopen_after_x_test()
        self.assertEqual(1, app._x_reopen_cycles)

    def test_browser_failure_preserves_numeric_exit_code_without_raw_payload(self):
        result = _safe_error_result(BrowserError("브라우저 종료 코드 4294930433"))
        self.assertEqual(result["browser_exit_code"], 4294930433)
        self.assertNotIn("token", json.dumps(result).casefold())

    def test_browser_failure_extracts_exit_code_when_localized_prefix_differs(self):
        result = _safe_error_result(BrowserError("Chromium process returned 4294930433"))
        self.assertEqual(result["browser_exit_code"], 4294930433)

    def test_build_script_declares_project_path_and_bundled_modules(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_v46_03_browser_verifier.ps1"
        text = script.read_text(encoding="utf-8")
        self.assertIn("'--paths', $repoRoot", text)
        for module in ("mcrs.browser_verifier", "mcrs.branding", "mcrs.research_browser", "mcrs.resource_guard"):
            self.assertIn(module, text)
        self.assertIn("'--onedir'", text)
        self.assertNotIn("'--onefile'", text)


if __name__ == "__main__":
    unittest.main()
