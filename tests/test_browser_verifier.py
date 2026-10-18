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

    def test_browser_failure_preserves_numeric_exit_code_without_raw_payload(self):
        result = _safe_error_result(BrowserError("브라우저 종료 코드 4294930433"))
        self.assertEqual(result["browser_exit_code"], 4294930433)
        self.assertNotIn("token", json.dumps(result).casefold())


if __name__ == "__main__":
    unittest.main()
