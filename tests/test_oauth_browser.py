import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from mcrs import oauth
from support import WorkspaceTemp


class OAuthBrowserIsolationTests(unittest.TestCase):
    def test_qa_mode_fails_closed_before_flow_when_edge_configuration_is_missing(self):
        with patch.dict(os.environ, {"ZISU_QA_MODE": "1"}, clear=False):
            for key in ("ZISU_QA_ROOT", "ZISU_QA_EDGE_EXE", "ZISU_QA_EDGE_PROFILE"):
                os.environ.pop(key, None)
            with patch.object(oauth, "_packages") as packages:
                with self.assertRaises(oauth.OAuthError) as raised:
                    oauth.begin_connect(client_config={"installed": {"client_id": "mock"}})
        self.assertEqual("isolated_browser", raised.exception.code)
        self.assertEqual("browser_preflight", raised.exception.stage)
        packages.assert_not_called()

    def test_qa_root_alone_also_disables_the_default_browser(self):
        with patch.dict(os.environ, {"ZISU_QA_ROOT": "D:/isolated-qa"}, clear=False):
            os.environ.pop("ZISU_QA_MODE", None)
            with patch.object(oauth, "_register_qa_edge_browser",
                              side_effect=oauth.OAuthError(code="isolated_browser", stage="browser_preflight")) as register:
                with self.assertRaises(oauth.OAuthError) as raised:
                    oauth.begin_connect(client_config={"installed": {"client_id": "mock"}})
        self.assertEqual("isolated_browser", raised.exception.code)
        register.assert_called_once_with()

    def test_qa_mode_passes_only_the_registered_browser_to_installed_flow(self):
        captured = {}

        class Credentials:
            token = "mock-token"
            scopes = oauth.SCOPES

            def to_json(self):
                return json.dumps({"token": "mock-token", "client_id": "mock", "scopes": oauth.SCOPES})

        class Flow:
            @classmethod
            def from_client_config(cls, config, **kwargs):
                captured["flow"] = kwargs
                return cls()

            def run_local_server(self, **kwargs):
                captured["server"] = kwargs
                return Credentials()

        with patch.dict(os.environ, {"ZISU_QA_MODE": "1"}, clear=False), \
             patch.object(oauth, "_register_qa_edge_browser", return_value="qa-edge-mock"), \
             patch.object(oauth, "_packages", return_value=(Flow, None, None)), \
             patch.object(oauth, "_get_email", return_value="qa@example.test"), \
             patch.object(oauth, "_get_my_channels", return_value=[{
                 "channel_id": "UC-mock", "channel_title": "QA channel"}]), \
             patch.object(oauth.webbrowser, "open", side_effect=AssertionError("default browser used")):
            channels, token_json = oauth.begin_connect(client_config={"installed": {"client_id": "mock"}})

        self.assertTrue(captured["flow"]["autogenerate_code_verifier"])
        server = captured["server"]
        self.assertEqual("127.0.0.1", server["host"])
        self.assertEqual(0, server["port"])
        self.assertTrue(server["open_browser"])
        self.assertEqual("qa-edge-mock", server["browser"])
        self.assertIsNone(server["authorization_prompt_message"])
        self.assertEqual("select_account", server["prompt"])
        self.assertNotIn("mock-token", repr(channels))
        self.assertIn("mock-token", token_json)

    def test_browser_rejects_non_google_or_non_oauth_urls_without_launch(self):
        with WorkspaceTemp() as root:
            browser = oauth._QAEdgeOAuthBrowser(root, root / "msedge.exe", root / "browser-profile",
                                                 process_factory=lambda *a, **k: self.fail("unexpected launch"))
            for url in ("http://accounts.google.com/o/oauth2/auth", "https://example.test/o/oauth2/auth",
                        "https://accounts.google.com.evil.test/o/oauth2/auth",
                        "https://accounts.google.com/not-oauth",
                        "https://accounts.google.com/o/oauth2/../logout"):
                with self.subTest(url=url), self.assertRaises(oauth.OAuthError):
                    browser.open(url)

    def test_edge_launch_verifies_qa_profile_ownership_before_opening_auth_url(self):
        with WorkspaceTemp() as root:
            exe = root / "msedge.exe"
            profile = root / "browser-profile"
            profile.mkdir()
            calls = []

            class Process:
                pid = 4321

                def poll(self):
                    return None

            process = Process()

            def launch(args, **kwargs):
                calls.append((args, kwargs))
                return process

            with patch("mcrs.research_browser._profile_owned_processes", return_value={}):
                browser = oauth._QAEdgeOAuthBrowser(root, exe, profile, process_factory=launch,
                                                    ownership_probe=lambda pid: pid == 4321, wait_seconds=0.2)
                self.assertTrue(browser.open("https://accounts.google.com/o/oauth2/auth?mock=1"))

            self.assertEqual(2, len(calls))
            self.assertEqual("about:blank", calls[0][0][-1])
            self.assertIn("--user-data-dir=" + str(profile), calls[0][0])
            self.assertTrue(calls[1][0][-1].startswith("https://accounts.google.com/o/oauth2/"))
            self.assertTrue(all(item[1]["shell"] is False for item in calls))

    def test_edge_ownership_failure_never_receives_auth_url(self):
        with WorkspaceTemp() as root:
            profile = root / "browser-profile"
            profile.mkdir()
            calls = []

            class Process:
                pid = 999

                def poll(self):
                    return None

            def launch(args, **kwargs):
                calls.append(args)
                return Process()

            with patch("mcrs.research_browser._profile_owned_processes", return_value={}):
                browser = oauth._QAEdgeOAuthBrowser(root, root / "msedge.exe", profile,
                                                    process_factory=launch,
                                                    ownership_probe=lambda pid: False,
                                                    wait_seconds=0.1)
                with self.assertRaises(oauth.OAuthError) as raised:
                    browser.open("https://accounts.google.com/o/oauth2/auth?secret=must-not-launch")
            self.assertEqual("browser_ownership", raised.exception.stage)
            self.assertEqual(1, len(calls))
            self.assertEqual("about:blank", calls[0][-1])
            self.assertNotIn("secret=must-not-launch", repr(calls))


if __name__ == "__main__":
    unittest.main()
