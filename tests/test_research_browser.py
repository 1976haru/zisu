import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from mcrs.resource_guard import ResourceGuard, ResourceLimits, ResourceSnapshot
from mcrs import research_browser
from tests.support import WorkspaceTemp


class FakeProcess:
    pid = 4242
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = None
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class ResearchBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__()

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def test_url_validation_blocks_non_youtube_and_non_https(self):
        self.assertEqual("https://www.youtube.com/watch?v=abcdefghijk",
            research_browser.validate_research_url("https://www.youtube.com/watch?v=abcdefghijk"))
        self.assertEqual("https://youtu.be/abcdefghijk",
            research_browser.validate_research_url("https://youtu.be/abcdefghijk"))
        for url in ("https://youtube.com.evil.test/watch?v=abcdefghijk",
                    "http://www.youtube.com/watch?v=abcdefghijk",
                    "https://www.youtube.com/watch?v=short",
                    "https://example.com/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                research_browser.validate_research_url(url)
        with self.assertRaises(ValueError):
            research_browser.validate_research_url("http://127.0.0.1:1234/", allow_local_test=False)

    def test_local_http_requires_explicit_integration_test_mode(self):
        self.assertEqual("http://127.0.0.1:8765/test",
            research_browser.validate_research_url("http://127.0.0.1:8765/test", allow_local_test=True))

    def test_explicit_approval_and_resource_gate_precede_launch(self):
        executable = self.root / "mock-edge.exe"
        guard = ResourceGuard(ResourceLimits(min_available_ram_bytes=2 * 1024**3))
        manager = research_browser.ResearchBrowserManager(app_data_root=self.root / "resource-test",
            executable=str(executable), resource_guard=guard,
            resource_sampler=lambda: ResourceSnapshot(1, 1, 1, 95, 1024))
        with patch.object(Path, "is_file", return_value=True), patch.object(manager, "launcher") as launcher:
            with self.assertRaises(research_browser.BrowserError):
                manager.open_selected("https://www.youtube.com/watch?v=abcdefghijk")
            with self.assertRaises(research_browser.BrowserResourceDeferred):
                manager.open_selected("https://www.youtube.com/watch?v=abcdefghijk", user_approved=True)
            launcher.assert_not_called()

    def test_single_owned_session_reuses_tab_and_closes_only_owned_processes(self):
        app_root = self.root / "owned-session"
        app_root.mkdir()
        executable = Path(app_root / "mock-edge.exe")
        manager = research_browser.ResearchBrowserManager(app_data_root=app_root,
            executable=str(executable), resource_sampler=lambda: ResourceSnapshot(1, 2, 8 * 1024**3, 20, 1))
        process_state = {"alive": True}
        created = 123456
        def process_identity(pid):
            return (created, str(executable), 32 * 1024**2) if int(pid) == 4242 and process_state["alive"] else None
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(manager, "launcher", side_effect=lambda *a, **k: FakeProcess(*a, **k)) as launcher, \
             patch.object(research_browser, "_process_identity", side_effect=process_identity), \
             patch.object(research_browser, "_process_tree", side_effect=lambda pid: {4242} if process_state["alive"] else set()), \
             patch.object(research_browser, "_window_count", return_value=1), \
             patch.object(research_browser, "_close_owned_windows"), \
             patch.object(research_browser, "_terminate_owned_process", side_effect=lambda pid, *_: process_state.update(alive=False) or True):
            first = manager.open_selected("https://www.youtube.com/watch?v=abcdefghijk", user_approved=True)
            second = manager.open_selected("https://www.youtube.com/watch?v=lmnopqrstuv", user_approved=True)
            self.assertEqual(2, launcher.call_count)  # initial window + same-profile URL forwarding request
            self.assertEqual(4242, first.root_pid)
            self.assertEqual((first.root_pid, first.window_count), (second.root_pid, second.window_count))
            forwarded_args=launcher.call_args_list[1].args[0]
            self.assertIn(f"--user-data-dir={manager.session.profile_dir}",forwarded_args)
            self.assertNotIn("--new-window",forwarded_args)
            self.assertTrue(manager.manifest_path.is_file())
            closed = manager.close_owned(grace_seconds=0)
            self.assertTrue(closed.closed)
            self.assertFalse(closed.residual_process_ids)
            self.assertEqual(32 * 1024**2, closed.rss_before_bytes)
            self.assertEqual(0, closed.rss_after_bytes)
            self.assertFalse(manager.manifest_path.exists())
            self.assertTrue(Path(manager.session.profile_dir if manager.session else manager.profile_root).exists())

    def test_startup_recovery_checks_pid_creation_executable_and_profile_boundary(self):
        root = self.root / "recovery-session"
        root.mkdir()
        executable = root / "edge.exe"
        manager = research_browser.ResearchBrowserManager(app_data_root=root, executable=str(executable))
        profile = manager.profile_root / "kept-session"
        profile.mkdir(parents=True)
        manager._write_manifest(research_browser.BrowserSession("s1", 4242, 77, str(executable),
            str(profile), 1, 0, "", owned_processes={4242: (77, str(executable))}))
        recovered = research_browser.ResearchBrowserManager(app_data_root=root, executable=str(executable))
        with patch.object(research_browser, "_process_identity", return_value=(77, str(executable), 99)), \
             patch.object(research_browser, "_process_tree", return_value={4242}), \
             patch.object(research_browser, "_window_count", return_value=1):
            self.assertTrue(recovered.recovery_available)
            metrics = recovered.recover_owned_session()
            self.assertEqual(4242, metrics.root_pid)
            self.assertTrue(profile.exists())


class ResearchBrowserIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("ZISU_RUN_BROWSER_INTEGRATION") == "1",
                         "set ZISU_RUN_BROWSER_INTEGRATION=1 for a real installed-browser local-page smoke")
    def test_installed_browser_opens_local_page_reuses_window_and_closes_owned_tree(self):
        executable = os.environ.get("ZISU_BROWSER_TEST_EXECUTABLE") or research_browser.find_browser()
        if not executable:
            self.skipTest("Edge/Chrome is not installed")
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html><head><title>ZISU local browser test</title></head><body>offline</body></html>"
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        test_root = Path(__file__).resolve().parents[1] / ".test_tmp" / ("browser_integration_" + os.urandom(6).hex())
        manager = research_browser.ResearchBrowserManager(app_data_root=test_root, executable=executable,
            allow_local_test=True)
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            first = manager.open_selected(base + "/first", user_approved=True)
            self.assertTrue(first.running)
            self.assertGreaterEqual(first.owned_process_count, 1)
            self.assertGreater(first.owned_rss_bytes, 0)
            again = manager.open_selected(base + "/second", user_approved=True)
            self.assertEqual(first.root_pid, again.root_pid)
            self.assertEqual(first.window_count, again.window_count)
            self.assertEqual(1, again.window_count)
            closed = manager.close_owned()
            self.assertTrue(closed.closed)
            self.assertEqual((), closed.residual_process_ids)
            self.assertGreater(closed.rss_before_bytes, 0)
            self.assertEqual(0, closed.rss_after_bytes)
            self.assertTrue(manager.profile_root.exists())
        finally:
            manager.close_owned(grace_seconds=1)
            server.shutdown(); server.server_close(); server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
