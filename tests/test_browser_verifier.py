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

    def test_verifier_preference_path_is_stable_but_separate_from_each_run(self):
        from mcrs.browser_verifier import verifier_settings_path
        root = Path.cwd() / ".test_tmp" / "preference-root"
        self.assertEqual(verifier_settings_path(root), verifier_settings_path(root))
        self.assertNotEqual(verifier_settings_path(root), verifier_root(root, "one") / "research-browser-settings.json")

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
                self.profile_dir = Path.cwd() / ".test_tmp" / f"profile-{uuid.uuid4().hex}"
                self.profile_dir.mkdir(parents=True)
                self.session = SimpleNamespace(profile_dir=str(self.profile_dir))
                self.open_count = 0
                self.windows = 0

            def metrics(self):
                return BrowserMetrics(bool(self.session), 777 if self.session else None,
                    3 if self.session else 0, 2048 if self.session else 0,
                    self.windows, (778, 779) if self.session else (),
                    lifecycle_state="OPEN" if self.windows else "USER_CLOSED")

            def profile_ownership_evidence(self):
                if self.session is None:
                    return {"verified": False, "profile_path": None, "owned_pids": []}
                # Simulates independent exact --user-data-dir process evidence.
                return {"verified": True, "profile_path": str(self.profile_dir),
                        "owned_pids": [777], "path_valid": True,
                        "process_profile_match": True, "tracked_process_match": True}

            def open_selected(self, url, *, user_approved=False):
                self.assert_approved = user_approved
                if self.session is None:
                    self.session = SimpleNamespace(profile_dir=str(self.profile_dir))
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
            cycles = [event for event in payload["events"] if event.get("event_stage") == "CYCLE_SUMMARY"]
            self.assertEqual(len(cycles), 5)
            self.assertTrue(all(event["same_isolated_profile"] and event["profile_ownership_verified"]
                                for event in cycles))
            self.assertTrue(all(event["profile_path"] for event in cycles))
        finally:
            app._stop_server()
            # Test profile is disposable test data, not a user/browser profile.
            import shutil
            shutil.rmtree(app._manager.profile_dir)

    def test_auto_test_sets_profile_baseline_after_initial_open_not_before(self):
        from mcrs.browser_verifier import BrowserVerifierApp
        from mcrs.browser_verifier import _canonical_profile_identity
        path = Path("C:/Users/Test/AppData/Local/ZisuSignalLab/BrowserVerification/run/research-browser-profiles/abc").as_posix()
        self.assertEqual(_canonical_profile_identity(path), _canonical_profile_identity(path.replace("/", "\\")))

    def test_browser_preference_defaults_to_chrome_and_persists_explicit_choice(self):
        from mcrs.research_browser import load_browser_preference, save_browser_preference, find_browser
        root = Path.cwd() / ".test_tmp" / f"browser-pref-{uuid.uuid4().hex}"
        path = root / "research-browser-settings.json"
        try:
            self.assertIsNone(load_browser_preference(path))
            save_browser_preference("Microsoft Edge", path)
            self.assertEqual(load_browser_preference(path), "Microsoft Edge")
            with patch("mcrs.research_browser.find_browser_candidates",
                       return_value={"Google Chrome": "chrome.exe", "Microsoft Edge": "edge.exe"}):
                self.assertEqual(find_browser(settings_path=path), "edge.exe")
                path.unlink()
                self.assertEqual(find_browser(settings_path=path), "chrome.exe")
        finally:
            import shutil
            if root.exists():
                shutil.rmtree(root)

    def test_research_browser_default_candidate_order_is_chrome_first(self):
        from mcrs.research_browser import find_browser
        with patch("mcrs.research_browser.find_browser_candidates",
                   return_value={"Google Chrome": "chrome.exe", "Microsoft Edge": "edge.exe"}):
            self.assertEqual(find_browser(), "chrome.exe")

    def test_auto_test_continues_after_ordinary_validation_failure_and_reports_all_rounds(self):
        from mcrs.browser_verifier import BrowserVerifierApp
        # Reuse the existing fake pipeline setup through a compact local manager.
        class FakeManager:
            def __init__(self, path):
                self.profile_dir = path
                Path(path).mkdir(parents=True)
                self.session = SimpleNamespace(profile_dir=path)
                self.windows = 0
                self.opens = 0
            def metrics(self):
                return BrowserMetrics(bool(self.session), 700 if self.session else None,
                    2 if self.session else 0, 1024, self.windows,
                    (701,) if self.session else (), lifecycle_state="OPEN" if self.windows else "USER_CLOSED")
            def profile_ownership_evidence(self):
                return {"verified": bool(self.session), "profile_path": self.profile_dir if self.session else None,
                        "owned_pids": [700] if self.session else []}
            def open_selected(self, url, *, user_approved=False):
                if self.session is None:
                    self.session = SimpleNamespace(profile_dir=self.profile_dir)
                with urlopen(url, timeout=2) as response:
                    response.read()
                self.windows = 1
                self.opens += 1
                return self.metrics()
            def request_close_owned_windows(self):
                self.windows = 0
                return (700, 701)
            def close_owned(self, grace_seconds=5):
                self.windows = 0
                self.session = None
                return SimpleNamespace(closed=True, forced_process_ids=(), residual_process_ids=(),
                    rss_before_bytes=1024, rss_after_bytes=0)
        app = BrowserVerifierApp.__new__(BrowserVerifierApp)
        app._selected_browser = "Google Chrome"
        app._server = app._server_thread = None
        app.test_root = Path.cwd() / ".test_tmp" / f"continue-auto-{uuid.uuid4().hex}"
        app.events = []
        app.messages = __import__("queue").Queue()
        app._seen_paths = set()
        app._baseline_processes = set()
        manager = FakeManager(str(app.test_root / "research-browser-profiles" / "session"))
        app._manager = manager
        app._manager_for_selected = lambda: manager
        await_count = {"n": 0}
        real_await = app._await_page
        def one_ordinary_miss(path, timeout=8.0):
            result = real_await(path, timeout)
            await_count["n"] += 1
            return False if await_count["n"] == 1 else result
        app._await_page = one_ordinary_miss
        try:
            with patch("mcrs.browser_verifier.browser_processes", return_value=set()), \
                    patch("mcrs.browser_verifier._process_handle_count", return_value=3):
                result = app._run_full_auto_test(2)
            self.assertEqual(result["auto_summary"]["completed_cycles"], 2)
            self.assertEqual(result["auto_summary"]["success_count"], 1)
            self.assertEqual(result["auto_summary"]["failure_count"], 1)
            self.assertEqual(manager.opens, 4)  # failed round + complete round, 2 opens each
        finally:
            app._stop_server()
            import shutil
            shutil.rmtree(app.test_root)

    def test_profile_comparison_does_not_depend_on_process_pid(self):
        from mcrs.research_browser import ResearchBrowserManager, BrowserSession
        root = Path.cwd() / ".test_tmp" / f"ownership-{uuid.uuid4().hex}"
        profile_root = root / "research-browser-profiles"
        profile = profile_root / ("a" * 32)
        profile.mkdir(parents=True)
        exe = root / "chrome.exe"
        exe.write_bytes(b"test")
        manager = ResearchBrowserManager(app_data_root=root, executable=str(exe))
        manager.session = BrowserSession("a" * 32, 111, 1, str(exe), str(profile), 1.0,
            owned_processes={222: (2, str(exe))})
        with patch("mcrs.research_browser._profile_owned_processes",
                   return_value={222: (2, str(exe))}), \
             patch.object(manager, "metrics", return_value=BrowserMetrics(True, 222, 1, 10, 1, (222,))):
            evidence = manager.profile_ownership_evidence()
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["owned_pids"], [222])
        import shutil
        shutil.rmtree(root)

    def test_unverified_profile_ownership_stops_automatic_run_immediately(self):
        from mcrs.browser_verifier import BrowserVerifierApp
        class UncertainManager:
            def __init__(self):
                self.session = SimpleNamespace(profile_dir="outside-profile")
                self.close_attempts = 0
            def metrics(self):
                return BrowserMetrics(True, 900, 2, 100, 1, (901,), lifecycle_state="OPEN")
            def profile_ownership_evidence(self):
                return {"verified": False, "profile_path": "outside-profile", "owned_pids": []}
            def open_selected(self, *_args, **_kwargs):
                return self.metrics()
            def close_owned(self, **_kwargs):
                self.close_attempts += 1
                raise AssertionError("unverified browser ownership must never be closed automatically")
        app = BrowserVerifierApp.__new__(BrowserVerifierApp)
        app._selected_browser = "Google Chrome"
        app._server = app._server_thread = None
        app.test_root = Path.cwd() / ".test_tmp" / f"uncertain-auto-{uuid.uuid4().hex}"
        app.events = []
        app.messages = __import__("queue").Queue()
        app._seen_paths = set()
        app._baseline_processes = set()
        app._manager = UncertainManager()
        app._manager_for_selected = lambda: app._manager
        app._await_page = lambda *_args, **_kwargs: False
        with patch("mcrs.browser_verifier.browser_processes", return_value=set()):
            result = app._run_full_auto_test(5)
        try:
            self.assertEqual(result["auto_summary"]["completed_cycles"], 1)
            self.assertEqual(result["auto_summary"]["final_verdict"], "INCOMPLETE_OR_FAIL")
            self.assertIn("소유권", result["auto_summary"]["failure_reason"])
            self.assertEqual(app._manager.close_attempts, 0)
        finally:
            app._stop_server()
            import shutil
            if app.test_root.exists():
                shutil.rmtree(app.test_root)

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
