import json
import unittest
from pathlib import Path
import uuid
from urllib.request import urlopen

from mcrs.browser_verifier import (
    _safe_error_result,
    build_report,
    create_local_server,
    verifier_root,
)
from mcrs.research_browser import BrowserError


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
