import json
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mcrs.offline_soak import _BlockedNetwork, run_offline_soak
from mcrs.resource_guard import ResourceSnapshot
from mcrs import store
from support import WorkspaceTemp


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class OfflineSoakTests(unittest.TestCase):
    def test_accelerated_soak_uses_production_scheduler_autopilot_and_isolated_db(self):
        with WorkspaceTemp() as root:
            data, reports = root / "data", root / "reports"
            data.mkdir()
            reports.mkdir()
            clock = FakeClock()
            rc = run_offline_soak(
                data_dir=data, report_dir=reports, mode="short", short_duration_seconds=45,
                monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
                resource_sampler=lambda: ResourceSnapshot(clock.monotonic(), 10,
                    8 * 1024**3, 40, 100 * 1024**2, 1),
            )
            report = json.loads((reports / "offline-soak-report.json").read_text(encoding="utf-8"))
            self.assertEqual(0, rc, report)
            self.assertEqual("SHORT_QA_PASS", report["final_verdict"])
            self.assertEqual("accelerated_virtual_6h", report["mode"])
            self.assertEqual(21600, report["virtual_schedule_elapsed_seconds"])
            self.assertEqual(45, report["actual_monotonic_elapsed_seconds"])
            self.assertGreater(report["mock_search_calls"], 0)
            self.assertEqual(0, report["external_network_attempts"])
            self.assertEqual("ok", report["database_integrity"])
            self.assertTrue(report["checkpoint_verified"])
            self.assertTrue(report["genre_configuration_preserved"])
            self.assertTrue(report["profile_isolation_verified"])
            self.assertTrue(report["mock_search_only_in_research"])
            self.assertEqual("completed", report["final_schedule_state"])
            self.assertTrue(report["quota_not_exceeded"])
            self.assertEqual(0, report["missed_sample_intervals"])
            self.assertTrue(report["pass_criteria"]["no_missing_sample_intervals"])
            self.assertGreater(report["phase_transition_observations"]["break"], 0)
            self.assertEqual(report["run_id"], json.loads(
                (reports / "offline-soak-report.json").read_text(encoding="utf-8"))["run_id"])
            rows = store.list_profiles(data / "zisu_v2.db")
            self.assertEqual(2, len(rows))
            main_profile = next(row for row in rows if row["genre"] != "JAZZ_SMOOTH")
            secondary_profile = next(row for row in rows if row["genre"] == "JAZZ_SMOOTH")
            self.assertNotEqual(main_profile["id"], secondary_profile["id"])
            self.assertGreater(len(store.list_candidates(main_profile["id"], path=data / "zisu_v2.db")), 0)
            self.assertEqual(0, len(store.list_candidates(secondary_profile["id"], path=data / "zisu_v2.db")))
            self.assertTrue((reports / "metrics.csv").is_file())
            self.assertTrue((reports / "events.jsonl").is_file())

    def test_short_mode_never_reports_wall_clock_six_hour_pass(self):
        with WorkspaceTemp() as root:
            data, reports = root / "data", root / "reports"
            data.mkdir()
            reports.mkdir()
            clock = FakeClock()
            run_offline_soak(data_dir=data, report_dir=reports, mode="short",
                short_duration_seconds=30, monotonic_fn=clock.monotonic, sleep_fn=clock.sleep,
                resource_sampler=lambda: ResourceSnapshot(clock.monotonic(), 10,
                    8 * 1024**3, 40, 100 * 1024**2))
            report = json.loads((reports / "offline-soak-report.json").read_text(encoding="utf-8"))
            self.assertEqual("SHORT_QA_PASS", report["result"])
            self.assertFalse(report["pass_criteria"]["six_hour_wall_clock"])
            self.assertFalse(report["pass_criteria"]["same_exe_process_active_scheduler"])

    def test_offline_network_guard_blocks_and_counts_network_attempt(self):
        guard = _BlockedNetwork()
        with guard:
            with self.assertRaises(OSError):
                socket.create_connection(("example.invalid", 443), timeout=0.1)
        self.assertEqual(1, guard.attempts)

    def test_cli_qa_validation_requires_frozen_exe_and_new_qa_root(self):
        import main
        with WorkspaceTemp() as parent:
            root = parent / "qa-run"
            args = ["--qa-offline-soak", "--qa-short", "--short-duration-seconds", "30",
                    "--data-dir", str(root / "data"), "--report-dir", str(root / "reports"),
                    "--duration-hours", "6"]
            with patch.object(sys, "frozen", False, create=True):
                with self.assertRaises(SystemExit):
                    main._run_offline_qa(args)
            self.assertFalse(root.exists())

    def test_ordinary_launch_does_not_select_qa_mode(self):
        import main
        self.assertFalse(main._is_offline_qa_requested([]))
        self.assertFalse(main._is_offline_qa_requested(["--offline-smoke-report", "report.json"]))
        self.assertTrue(main._is_offline_qa_requested(["--qa-offline-soak"]))

    def test_cli_refuses_reused_and_user_data_overlapping_qa_roots(self):
        import main
        with WorkspaceTemp() as parent:
            reused = parent / "qa-existing"
            reused.mkdir()
            arguments = ["--qa-offline-soak", "--qa-short", "--short-duration-seconds", "30",
                         "--data-dir", str(reused / "data"), "--report-dir", str(reused / "reports")]
            with patch.object(sys, "frozen", True, create=True):
                with self.assertRaises(SystemExit):
                    main._run_offline_qa(arguments)
            protected = parent / "protected-local" / "ZisuResearchStudio"
            overlapping = protected / "qa-run"
            arguments = ["--qa-offline-soak", "--qa-short", "--short-duration-seconds", "30",
                         "--data-dir", str(overlapping / "data"), "--report-dir", str(overlapping / "reports")]
            previous = {key: os.environ.get(key) for key in ("LOCALAPPDATA", "APPDATA", "ZISU_DATA_DIR")}
            try:
                os.environ["LOCALAPPDATA"] = str(parent / "protected-local")
                os.environ.pop("APPDATA", None)
                os.environ.pop("ZISU_DATA_DIR", None)
                with patch.object(sys, "frozen", True, create=True):
                    with self.assertRaises(SystemExit):
                        main._run_offline_qa(arguments)
                self.assertFalse(overlapping.exists())
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_interruption_keeps_a_non_successful_atomic_report(self):
        with WorkspaceTemp() as root:
            data, reports = root / "data", root / "reports"
            data.mkdir()
            reports.mkdir()
            clock = FakeClock()

            def stop(_seconds):
                raise KeyboardInterrupt

            rc = run_offline_soak(data_dir=data, report_dir=reports, mode="short",
                short_duration_seconds=30, monotonic_fn=clock.monotonic, sleep_fn=stop,
                resource_sampler=lambda: ResourceSnapshot(clock.monotonic(), 10,
                    8 * 1024**3, 40, 100 * 1024**2))
            report = json.loads((reports / "offline-soak-report.json").read_text(encoding="utf-8"))
            self.assertEqual(2, rc)
            self.assertEqual("INTERRUPTED", report["result"])
            self.assertFalse(report["pass_criteria"]["six_hour_wall_clock"])
            self.assertTrue((reports / "metrics.csv").is_file())

    def test_cli_short_path_redirects_product_storage_before_runner(self):
        import main
        with WorkspaceTemp() as parent:
            root = parent / "qa-offline-run"
            args = ["--qa-offline-soak", "--qa-short", "--short-duration-seconds", "30",
                    "--data-dir", str(root / "data"), "--report-dir", str(root / "reports"),
                    "--duration-hours", "6"]
            previous = {key: __import__("os").environ.get(key) for key in
                        ("ZISU_DATA_DIR", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "YOUTUBE_API_KEY")}
            try:
                with patch.object(sys, "frozen", True, create=True), \
                     patch("mcrs.offline_soak.run_offline_soak", return_value=0) as runner:
                    self.assertEqual(0, main._run_offline_qa(args))
                kwargs = runner.call_args.kwargs
                self.assertEqual(root / "data", kwargs["data_dir"])
                self.assertEqual(root / "reports", kwargs["report_dir"])
                self.assertEqual(str(root / "data"), __import__("os").environ["ZISU_DATA_DIR"])
                self.assertFalse((root / "data" / "zisu_v2.db").exists())
            finally:
                for key, value in previous.items():
                    if value is None:
                        __import__("os").environ.pop(key, None)
                    else:
                        __import__("os").environ[key] = value


if __name__ == "__main__":
    unittest.main()
