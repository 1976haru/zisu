import os
from pathlib import Path
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from mcrs.resource_guard import ResourceGuard, ResourceLimits, ResourceSnapshot
from mcrs import research_browser
from mcrs.browser_verifier import browser_processes
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

    def test_first_run_defaults_to_chrome_and_explicit_edge_choice_restores(self):
        root=self.root / "browser-preference"
        chrome=root / "chrome.exe"; edge=root / "msedge.exe"
        chrome.parent.mkdir(parents=True); chrome.write_bytes(b"mock")
        edge.write_bytes(b"mock")
        choices={"Google Chrome":str(chrome),"Microsoft Edge":str(edge)}
        with patch.object(research_browser,"find_browser_candidates",return_value=choices):
            first=research_browser.ResearchBrowserManager(app_data_root=root)
            self.assertEqual("Google Chrome",first.preferred_browser)
            self.assertEqual(str(chrome.resolve()),first.executable)
            first.select_browser("Microsoft Edge")
            restored=research_browser.ResearchBrowserManager(app_data_root=root)
            self.assertEqual("Microsoft Edge",restored.preferred_browser)
            self.assertEqual(str(edge.resolve()),restored.executable)

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

    def test_manual_browser_fallback_validates_url_without_taking_ownership(self):
        opened=[]
        result=research_browser.open_manual_research_url(
            "https://www.youtube.com/watch?v=abcdefghijk",
            opener=lambda url,**kwargs: opened.append((url,kwargs)) or True)
        self.assertTrue(result)
        self.assertEqual("https://www.youtube.com/watch?v=abcdefghijk",opened[0][0])
        self.assertEqual({"new":2,"autoraise":True},opened[0][1])
        with self.assertRaises(ValueError):
            research_browser.open_manual_research_url("https://example.com/")

    def test_browser_frame_filter_ignores_uac_and_helper_windows(self):
        self.assertTrue(research_browser._is_browser_main_window("Chrome_WidgetWin_1"))
        self.assertFalse(research_browser._is_browser_main_window("UAC_InputIndicatorOverlayWnd"))
        self.assertFalse(research_browser._is_browser_main_window("Chrome_RenderWidgetHostHWND"))

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

    def test_last_window_closed_by_user_x_can_reopen_same_owned_profile(self):
        app_root = self.root / "x-close-reopen"
        app_root.mkdir()
        executable = Path(app_root / "mock-chrome.exe")
        manager = research_browser.ResearchBrowserManager(app_data_root=app_root,
            executable=str(executable), resource_sampler=lambda: ResourceSnapshot(1, 2, 8 * 1024**3, 20, 1),
            startup_stability_seconds=0)
        process_state = {"alive": True, "windows": 1, "launches": 0}
        created = 123457
        def process_identity(pid):
            return (created, str(executable), 32 * 1024**2) if int(pid) == 4242 and process_state["alive"] else None
        def launch(*args, **kwargs):
            process_state["launches"] += 1
            process_state["windows"] = 1  # initial launch or Chromium single-instance handoff
            return FakeProcess(*args, **kwargs)
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(manager, "launcher", side_effect=launch) as launcher, \
             patch.object(research_browser, "_process_identity", side_effect=process_identity), \
             patch.object(research_browser, "_process_tree", side_effect=lambda pid: {4242} if process_state["alive"] else set()), \
             patch.object(research_browser, "_window_count", side_effect=lambda _owned: process_state["windows"]), \
             patch.object(research_browser, "_close_owned_windows"):
            first = manager.open_selected("https://www.youtube.com/watch?v=abcdefghijk", user_approved=True)
            profile = manager.session.profile_dir
            process_state["windows"] = 0  # native WM_CLOSE / browser X, root stays alive
            reopened = manager.open_selected("https://www.youtube.com/watch?v=lmnopqrstuv", user_approved=True)
            self.assertEqual(first.root_pid, reopened.root_pid)
            self.assertEqual(1, reopened.window_count)
            self.assertEqual(profile, manager.session.profile_dir)
            self.assertEqual(2, launcher.call_count)  # one owned tree; second launch only forwards to it

    def test_lifecycle_distinguishes_last_window_close_background_and_clean_exit(self):
        executable = str(self.root / "chrome.exe")
        manager = research_browser.ResearchBrowserManager(app_data_root=self.root / "lifecycle",
            executable=executable)
        fake_process = FakeProcess()
        manager.session = research_browser.BrowserSession("life", 4242, 100, executable,
            str(manager.profile_root / "life"), time.time(), process=fake_process,
            owned_processes={4242: (100, executable)}, lifecycle_state="OPEN", last_window_count=1)
        with patch.object(manager, "_refresh_owned_processes"), \
             patch.object(research_browser, "_process_identity", return_value=(100, executable, 1234)), \
             patch.object(research_browser, "_window_count", side_effect=[0, 0, 0]):
            first = manager.metrics()
            self.assertEqual("USER_CLOSED", first.lifecycle_state)
            self.assertTrue(first.running)
            self.assertEqual("last_window_disappeared", first.last_close_reason)
            second = manager.metrics()
            self.assertEqual("BACKGROUND_PROCESS_ONLY", second.lifecycle_state)
            fake_process.returncode = 0
            # The owned process remains visible in this snapshot; a clean code-0
            # exit is explicitly non-crash and the state machine keeps the
            # background-only observation until all verified PIDs disappear.
            third = manager.metrics()
            self.assertEqual(0, third.last_exit_code)
            self.assertNotEqual("ERROR", third.lifecycle_state)

    def test_reused_pid_is_not_expanded_as_owned_process_root(self):
        executable = str(self.root / "chrome.exe")
        manager = research_browser.ResearchBrowserManager(app_data_root=self.root / "stale-pid",
            executable=executable)
        manager.session = research_browser.BrowserSession("stale", 4242, 100, executable,
            str(manager.profile_root / "stale"), time.time(),
            owned_processes={4242: (100, executable)})
        with patch.object(research_browser, "_process_identity", return_value=(200, executable, 10)), \
             patch.object(research_browser, "_process_tree") as process_tree, \
             patch.object(research_browser, "_profile_owned_processes", return_value={}), \
             patch.object(research_browser.time, "monotonic", return_value=100):
            manager._refresh_owned_processes(manager.session)
        process_tree.assert_not_called()
        self.assertEqual({}, manager.session.owned_processes)

    def test_zero_exit_after_visible_window_is_reported_as_normal_close(self):
        executable = str(self.root / "chrome.exe")
        manager = research_browser.ResearchBrowserManager(app_data_root=self.root / "zero-exit",
            executable=executable)
        fake_process = FakeProcess()
        manager.session = research_browser.BrowserSession("zero", 5151, 300, executable,
            str(manager.profile_root / "zero"), time.time(), process=fake_process,
            owned_processes={5151: (300, executable)}, lifecycle_state="OPEN", last_window_count=1)
        fake_process.returncode = 0
        with patch.object(manager, "_refresh_owned_processes"), \
             patch.object(research_browser, "_process_identity", return_value=None), \
             patch.object(research_browser, "_window_count", return_value=0):
            metrics = manager.metrics()
        self.assertFalse(metrics.running)
        self.assertEqual("CLOSED", metrics.lifecycle_state)
        self.assertEqual(0, metrics.last_exit_code)
        self.assertEqual("normal_exit_code_0", metrics.last_close_reason)

    def test_recovery_without_live_process_preserves_validated_profile_for_reopen(self):
        root = self.root / "closed-recovery"
        root.mkdir()
        executable = root / "edge.exe"
        first = research_browser.ResearchBrowserManager(app_data_root=root, executable=str(executable))
        profile = first.profile_root / "recover-me"
        profile.mkdir(parents=True)
        first._write_manifest(research_browser.BrowserSession("recover-me", 7777, 50,
            str(executable), str(profile), 10, lifecycle_state="CLOSED", last_window_count=1,
            last_exit_code=0, last_close_reason="normal_exit_code_0"))
        resumed = research_browser.ResearchBrowserManager(app_data_root=root, executable=str(executable))
        with patch.object(research_browser, "_process_identity", return_value=None), \
             patch.object(research_browser, "_profile_owned_processes", return_value={}), \
             patch.object(research_browser, "_window_count", return_value=0):
            metrics = resumed.recover_owned_session()
        self.assertEqual("CLOSED", metrics.lifecycle_state)
        self.assertEqual(str(profile), resumed.session.profile_dir)
        self.assertTrue(resumed.manifest_path.exists())

    def test_chromium_profile_argument_match_is_exact(self):
        profile = str(self.root / "isolated profile")
        self.assertTrue(research_browser._command_uses_profile(
            f'chrome.exe --user-data-dir="{profile}" --new-window', profile))
        self.assertFalse(research_browser._command_uses_profile(
            f'chrome.exe --user-data-dir="{profile}-other" --new-window', profile))

    def test_saved_session_manifest_is_reconnected_before_open_after_app_restart(self):
        root = self.root / "manifest-reopen"
        root.mkdir()
        executable = root / "chrome.exe"
        first_manager = research_browser.ResearchBrowserManager(app_data_root=root, executable=str(executable),
            resource_sampler=lambda: ResourceSnapshot(1, 2, 8 * 1024**3, 20, 1), startup_stability_seconds=0)
        process_state = {"alive": True, "windows": 1, "launches": 0}
        created = 123458
        def process_identity(pid):
            return (created, str(executable), 24 * 1024**2) if int(pid) == 4242 and process_state["alive"] else None
        def launch(*args, **kwargs):
            process_state["launches"] += 1
            process_state["windows"] = 1
            return FakeProcess(*args, **kwargs)
        with patch.object(Path, "is_file", return_value=True), \
             patch.object(first_manager, "launcher", side_effect=launch), \
             patch.object(research_browser, "_process_identity", side_effect=process_identity), \
             patch.object(research_browser, "_process_tree", side_effect=lambda pid: {4242} if process_state["alive"] else set()), \
             patch.object(research_browser, "_window_count", side_effect=lambda _owned: process_state["windows"]):
            first_manager.open_selected("https://www.youtube.com/watch?v=abcdefghijk", user_approved=True)
            process_state["windows"] = 0
            restarted_manager = research_browser.ResearchBrowserManager(app_data_root=root, executable=str(executable),
                resource_sampler=lambda: ResourceSnapshot(1, 2, 8 * 1024**3, 20, 1), startup_stability_seconds=0)
            self.assertTrue(restarted_manager.recovery_available)
            with patch.object(restarted_manager, "launcher", side_effect=launch):
                reopened = restarted_manager.open_selected("https://www.youtube.com/watch?v=lmnopqrstuv", user_approved=True)
            self.assertEqual(4242, reopened.root_pid)
            self.assertEqual(1, reopened.window_count)
            self.assertEqual(2, process_state["launches"])


class ResearchBrowserIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("ZISU_RUN_BROWSER_INTEGRATION") == "1",
                         "set ZISU_RUN_BROWSER_INTEGRATION=1 for a real installed-browser local-page smoke")
    def test_installed_browser_opens_local_page_reuses_window_and_closes_owned_tree(self):
        executable = os.environ.get("ZISU_BROWSER_TEST_EXECUTABLE") or research_browser.find_browser()
        if not executable:
            self.skipTest("Edge/Chrome is not installed")
        class Handler(BaseHTTPRequestHandler):
            requested_paths=[]
            def do_GET(self):
                if not self.path.startswith(("/first", "/second", "/reopen")):
                    self.send_error(404)
                    return
                self.requested_paths.append(self.path)
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
            deadline=time.monotonic()+5
            while "/first" not in Handler.requested_paths and time.monotonic()<deadline:
                time.sleep(0.05)
            again = manager.open_selected(base + "/second", user_approved=True)
            deadline=time.monotonic()+5
            while "/second" not in Handler.requested_paths and time.monotonic()<deadline:
                time.sleep(0.05)
            self.assertEqual(first.root_pid, again.root_pid)
            self.assertEqual(first.window_count, again.window_count)
            self.assertEqual(1, again.window_count)
            self.assertIn("/first",Handler.requested_paths)
            self.assertIn("/second",Handler.requested_paths)
            baseline_browsers = browser_processes()
            # Exercise five full reopen cycles, including native WM_CLOSE (the
            # same message path as clicking X) and the ZISU close button.
            for cycle in range(1, 6):
                if cycle % 2:
                    owned = dict(manager.session.owned_processes)
                    research_browser._close_owned_windows(owned)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        current = manager.metrics()
                        if current.window_count == 0 or not manager._session_alive(manager.session):
                            break
                        time.sleep(0.05)
                    self.assertEqual(0, manager.metrics().window_count,
                        f"cycle {cycle}: the dedicated window did not close through WM_CLOSE")
                else:
                    close_result = manager.close_owned(grace_seconds=8)
                    self.assertTrue(close_result.closed, f"cycle {cycle} close left owned processes: {close_result.residual_process_ids}")
                    self.assertEqual((), close_result.residual_process_ids)
                    self.assertEqual(0, close_result.rss_after_bytes)
                opened = manager.open_selected(base + f"/reopen-{cycle}", user_approved=True)
                self.assertEqual(1, opened.window_count)
                deadline = time.monotonic() + 5
                expected = f"/reopen-{cycle}"
                while expected not in Handler.requested_paths and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn(expected, Handler.requested_paths)
                self.assertTrue(baseline_browsers.issubset(browser_processes()))
            close_result = manager.close_owned(grace_seconds=8)
            self.assertTrue(close_result.closed)
            self.assertEqual((), close_result.residual_process_ids)
            self.assertGreater(close_result.rss_before_bytes, 0)
            self.assertEqual(0, close_result.rss_after_bytes)
            self.assertTrue(baseline_browsers.issubset(browser_processes()))
            self.assertTrue(manager.profile_root.exists())
        finally:
            manager.close_owned(grace_seconds=1)
            server.shutdown(); server.server_close(); server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
