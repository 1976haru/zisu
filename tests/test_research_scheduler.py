import concurrent.futures
import unittest
from datetime import datetime, timedelta, timezone

from mcrs import autopilot, research_scheduler as scheduler, store
from mcrs.resource_guard import ResourceGuard, ResourceLimits, ResourceMonitor, ResourceSnapshot
from tests.support import WorkspaceTemp


class ResearchSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        self.db = self.root / "schedule.sqlite"
        store.initialize(self.db)
        self.jp = store.create_profile(
            "Japan Morning Research", "JP", "MORNING_CHILL_RAP",
            path=self.db, related_genres=["CHILL_RAP", "RNB_HIPHOP"])
        self.kr = store.create_profile("Korea Research", "KR", "OLD_POP", path=self.db)
        self.now = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def test_six_hour_schedule_switches_research_and_break_and_completes(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp), self.db, now=self.now)
        self.assertEqual(("running", "research", 12),
                         (session["state"], session["phase"], session["search_calls_remaining"]))
        rest = scheduler.get(session["id"], self.jp, self.db, now=self.now + timedelta(minutes=30))
        self.assertEqual(("break", 19800), (rest["phase"], rest["remaining_seconds"]))
        next_round = scheduler.get(session["id"], self.jp, self.db, now=self.now + timedelta(hours=1))
        self.assertEqual(("research", 300 * 60), (next_round["phase"], next_round["remaining_seconds"]))
        done = scheduler.get(session["id"], self.jp, self.db, now=self.now + timedelta(hours=6))
        self.assertEqual(("completed", "complete", 0),
                         (done["state"], done["phase"], done["remaining_seconds"]))

    def test_pause_resume_freezes_elapsed_time_and_reanchors_without_double_count(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp), self.db, now=self.now)
        t25 = self.now + timedelta(minutes=25)
        paused = scheduler.pause(session["id"], self.jp, self.db, now=t25)
        self.assertEqual(("paused", 1500), (paused["state"], paused["elapsed_seconds"]))
        frozen = scheduler.get(session["id"], self.jp, self.db, now=self.now + timedelta(hours=3))
        self.assertEqual(1500, frozen["elapsed_seconds"])
        resumed_at = self.now + timedelta(hours=3)
        scheduler.resume(session["id"], self.jp, self.db, now=resumed_at)
        at_boundary = scheduler.get(session["id"], self.jp, self.db,
                                    now=resumed_at + timedelta(minutes=5))
        self.assertEqual(("break", 1800), (at_boundary["phase"], at_boundary["elapsed_seconds"]))
        one_second_later = scheduler.get(session["id"], self.jp, self.db,
                                         now=resumed_at + timedelta(minutes=5, seconds=1))
        self.assertEqual(1801, one_second_later["elapsed_seconds"])

    def test_future_start_and_profile_snapshot_are_isolated(self):
        start = self.now + timedelta(hours=1)
        waiting = scheduler.create(scheduler.ScheduleConfig(self.jp, start_at=start), self.db, now=self.now)
        self.assertEqual(("scheduled", "waiting", 3600),
                         (waiting["state"], waiting["phase"], waiting["remaining_seconds"]))
        self.assertIsNone(scheduler.get(waiting["id"], self.kr, self.db, now=self.now))
        started = scheduler.get(waiting["id"], self.jp, self.db, now=start)
        self.assertEqual(("running", "research", 0),
                         (started["state"], started["phase"], started["elapsed_seconds"]))
        self.assertEqual("JP", started["market"])
        self.assertEqual("MORNING_CHILL_RAP", started["core_genre"])
        self.assertEqual(["CHILL_RAP", "RNB_HIPHOP"], __import__("json").loads(started["related_genres_json"]))

    def test_profile_allows_only_one_active_schedule_and_validation_is_bounded(self):
        first = scheduler.create(scheduler.ScheduleConfig(self.jp), self.db, now=self.now)
        with self.assertRaises(scheduler.ScheduleBusy):
            scheduler.create(scheduler.ScheduleConfig(self.jp), self.db, now=self.now)
        second = scheduler.create(scheduler.ScheduleConfig(self.kr), self.db, now=self.now)
        self.assertNotEqual(first["id"], second["id"])
        for config in (
            scheduler.ScheduleConfig(self.kr, total_hours=3),
            scheduler.ScheduleConfig(self.kr, research_minutes=15),
            scheduler.ScheduleConfig(self.kr, break_minutes=20),
            scheduler.ScheduleConfig(self.kr, approved_search_calls=1001),
            scheduler.ScheduleConfig(self.kr, approved_search_calls=float("inf")),
        ):
            with self.assertRaises(ValueError):
                config.validate()

    def test_search_allowance_claim_is_atomic_break_aware_and_crash_safe(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, total_hours=2,
            research_minutes=20, break_minutes=15, approved_search_calls=2), self.db, now=self.now)
        self.assertEqual(1, scheduler.claim_search_call(session["id"], self.jp, self.db, now=self.now))
        with self.assertRaises(scheduler.UnresolvedSearchCall):
            scheduler.claim_search_call(session["id"], self.jp, self.db, now=self.now)
        recovered = scheduler.recover_interrupted(self.jp, self.db, now=self.now + timedelta(minutes=2))[0]
        self.assertEqual(("paused", True, 1),
                         (recovered["state"], bool(recovered["search_in_flight"]), recovered["search_calls_used"]))
        with self.assertRaises(scheduler.UnresolvedSearchCall):
            scheduler.resume(session["id"], self.jp, self.db, now=self.now + timedelta(hours=1))
        scheduler.acknowledge_uncertain_search_consumed(session["id"], self.jp, self.db)
        scheduler.resume(session["id"], self.jp, self.db, now=self.now + timedelta(hours=1))
        with self.assertRaises(ValueError):
            scheduler.claim_search_call(session["id"], self.jp, self.db,
                                        now=self.now + timedelta(hours=1, minutes=20))
        self.assertEqual(2, scheduler.claim_search_call(session["id"], self.jp, self.db,
                              now=self.now + timedelta(hours=1, minutes=35)))
        scheduler.finish_search_call(session["id"], self.jp, self.db)
        with self.assertRaises(ValueError):
            scheduler.claim_search_call(session["id"], self.jp, self.db,
                                         now=self.now + timedelta(hours=1, minutes=70))

    def test_concurrent_search_reservations_cannot_double_spend_one_slot(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, approved_search_calls=1),
                                   self.db, now=self.now)
        def claim(_):
            try:
                return scheduler.claim_search_call(session["id"], self.jp, self.db, now=self.now)
            except scheduler.UnresolvedSearchCall:
                return "already_reserved"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual([1, "already_reserved"], sorted(results, key=str))
        final = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        self.assertEqual((1, 0, 1), (final["search_calls_used"], final["search_calls_remaining"], final["search_in_flight"]))

    def test_checkpoint_rejects_secrets_and_migration_preserves_schedule(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp), self.db, now=self.now)
        safe = scheduler.checkpoint(session["id"], self.jp,
            {"processed_video_ids": ["abcdefghijk"], "query_index": 1}, self.db, now=self.now)
        self.assertIn('"query_index": 1', safe["checkpoint_json"])
        with self.assertRaises(ValueError):
            scheduler.checkpoint(session["id"], self.jp, {"access_token": "do-not-store"}, self.db)
        with self.assertRaises(ValueError):
            scheduler.checkpoint(session["id"], self.jp, {"api_key": "do-not-store"}, self.db)
        safe = scheduler.checkpoint(session["id"], self.jp, {
            "approved_provider_id":"gemini", "approved_model":"mock-model",
            "approved_target":10, "approved_max_search_calls":8,
            "paid_external_approved":True, "cost_estimate_usd":0.02,
            "cost_limit_usd":0.10, "processed_video_ids":[],
            "uncertain_request":False,
        }, self.db)
        self.assertIn('"approved_model": "mock-model"', safe["checkpoint_json"])
        self.assertNotIn("api_key", safe["checkpoint_json"])
        with self.assertRaises(ValueError):
            scheduler.checkpoint(session["id"], self.jp,
                {"cost_limit_usd":float("inf")}, self.db)
        store.initialize(self.db)
        store.initialize(self.db)
        restored = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        self.assertEqual(safe["checkpoint_json"], restored["checkpoint_json"])
        with store.connect(self.db) as db:
            self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(1, db.execute("SELECT count(*) FROM research_schedules").fetchone()[0])

    def test_resource_pause_allows_inflight_request_to_finish_then_blocks_new_search(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, approved_search_calls=3),
                                   self.db, now=self.now)
        scheduler.claim_search_call(session["id"], self.jp, self.db, now=self.now)
        pending = scheduler.request_resource_pause(session["id"], self.jp, "system_ram_low", self.db, now=self.now)
        self.assertEqual(("running", 1), (pending["state"], pending["search_in_flight"]))
        scheduler.finish_search_call(session["id"], self.jp, self.db)
        paused = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        self.assertEqual(("paused", "system_ram_low", 1),
                         (paused["state"], paused["pause_reason"], paused["search_calls_used"]))
        with self.assertRaises(ValueError):
            scheduler.claim_search_call(session["id"], self.jp, self.db, now=self.now)
        resumed = scheduler.resume(session["id"], self.jp, self.db, now=self.now + timedelta(minutes=1))
        self.assertEqual("", resumed["pause_reason"])

    def test_resource_recovery_requires_clear_guard_matching_approval_and_quota(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, approved_search_calls=2),
                                   self.db, now=self.now)
        config = autopilot.AutopilotConfig(self.jp, target=5, max_search_calls=2)
        research_id = store.start_research_session(self.jp, "JP", "MORNING_CHILL_RAP", 5, 2,
                                                   self.db, settings_hash="schedule-test")
        approved = autopilot.AutopilotController._settings(config, None)
        store.save_autopilot_settings(self.jp, research_id, approved, self.db)
        scheduler.checkpoint(session["id"], self.jp,
            {"autopilot_session_id": research_id, "processed_video_ids": []}, self.db)
        scheduler.request_resource_pause(session["id"], self.jp, "system_ram_low", self.db, now=self.now)
        guard = ResourceGuard()
        healthy = lambda: ResourceSnapshot(100, 20, 4*1024**3, 40, 100)
        with self.assertRaises(ValueError):
            scheduler.resume_after_resource_recovery(session["id"], self.jp, self.db,
                resource_guard=guard, resource_sampler=healthy,
                expected_autopilot_settings={**approved, "target": 20}, now=self.now)
        resumed = scheduler.resume_after_resource_recovery(session["id"], self.jp, self.db,
            resource_guard=guard, resource_sampler=healthy,
            expected_autopilot_settings=approved, now=self.now)
        self.assertEqual(("running", 2), (resumed["state"], resumed["search_calls_remaining"]))

    def test_resource_recovery_never_resumes_uncertain_or_crash_request(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp), self.db, now=self.now)
        scheduler.claim_search_call(session["id"], self.jp, self.db, now=self.now)
        scheduler.request_resource_pause(session["id"], self.jp, "system_ram_low", self.db, now=self.now)
        healthy_guard = ResourceGuard()
        with self.assertRaises(scheduler.UnresolvedSearchCall):
            scheduler.resume_after_resource_recovery(session["id"], self.jp, self.db,
                resource_guard=healthy_guard,
                resource_sampler=lambda: ResourceSnapshot(1, 10, 4*1024**3, 40, 100),
                expected_autopilot_settings={"provider_id":"disabled"},
                allow_new_autopilot_session=True, now=self.now)
        saved = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        self.assertEqual(("running", 1, 1),
                         (saved["state"], saved["search_in_flight"], saved["search_calls_used"]))

    def test_schedule_call_exception_keeps_uncertainty_and_never_refunds_quota(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, approved_search_calls=2),
                                   self.db, now=self.now)
        result = scheduler.run_autopilot_block(session["id"], self.jp,
            autopilot.AutopilotController(self.db),
            autopilot.AutopilotConfig(self.jp, target=5, max_search_calls=2),
            lambda *_args: (_ for _ in ()).throw(TimeoutError("private transport detail")),
            self.db, resource_sampler=lambda: ResourceSnapshot(0, 10, 4*1024**3, 40, 100), now=self.now)
        saved = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        payload = __import__("json").loads(saved["checkpoint_json"])
        self.assertEqual("paused", result.state)
        self.assertEqual(("paused", "uncertain_request", 1, 1),
                         (saved["state"], saved["pause_reason"], saved["search_in_flight"], saved["search_calls_used"]))
        self.assertTrue(payload["uncertain_request"])
        with self.assertRaises(scheduler.UnresolvedSearchCall):
            scheduler.resume(session["id"], self.jp, self.db, now=self.now)

    def test_resume_keeps_rest_phase_after_user_pause(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, research_minutes=20,
            break_minutes=15), self.db, now=self.now)
        at_break = self.now + timedelta(minutes=25)
        scheduler.pause(session["id"], self.jp, self.db, now=at_break)
        resumed = scheduler.resume(session["id"], self.jp, self.db, now=at_break+timedelta(hours=1))
        self.assertEqual(("running", "break"), (resumed["state"], resumed["phase"]))

    def test_autopilot_runs_only_in_research_windows_and_resumes_one_checkpoint(self):
        clock = [self.now]
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, total_hours=6,
            research_minutes=30, break_minutes=30, approved_search_calls=8), self.db, now=self.now)
        controller = autopilot.AutopilotController(self.db)
        session_id = None
        requests = []
        def collector(*_args):
            requests.append(len(requests))
            index = len(requests)
            clock[0] += timedelta(minutes=30)
            video = f"sched{index:06d}"
            return {"items": [{"video_id": video, "video_title": f"Morning Chill Rap mellow groove {index}",
                               "description": "", "channel_title": "mock"}], "nextPageToken": ""}
        for hour in range(5):
            clock[0] = self.now + timedelta(hours=hour)
            config = autopilot.AutopilotConfig(self.jp, target=5, max_search_calls=8,
                                                session_id=session_id)
            result = scheduler.run_autopilot_block(session["id"], self.jp, controller, config,
                collector, self.db, now=lambda: clock[0], resource_sampler=lambda: ResourceSnapshot(0))
            session_id = result.session_id
            self.assertIn(result.state, {"paused", "complete"})
            saved_schedule = scheduler.get(session["id"], self.jp, self.db, now=clock[0])
            self.assertEqual(hour + 1, saved_schedule["search_calls_used"])
            self.assertEqual(session_id, __import__("json").loads(saved_schedule["checkpoint_json"])["autopilot_session_id"])
        self.assertEqual(5, len(requests))
        self.assertEqual("complete", result.state)
        self.assertEqual(5, result.report["counts"]["ai_recommended_fit"])
        self.assertEqual("completed", scheduler.get(session["id"], self.jp, self.db,
            now=self.now + timedelta(hours=6))["state"])

    def test_low_ram_prevents_new_api_request_and_keeps_approved_allowance(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, approved_search_calls=2),
                                   self.db, now=self.now)
        guard = ResourceGuard(ResourceLimits(min_available_ram_bytes=2 * 1024**3))
        calls = []
        result = scheduler.run_autopilot_block(session["id"], self.jp,
            autopilot.AutopilotController(self.db),
            autopilot.AutopilotConfig(self.jp, target=5, max_search_calls=2),
            lambda *_args: calls.append(True), self.db, resource_guard=guard,
            resource_sampler=lambda: ResourceSnapshot(0, 10, 1024, 20, 100), now=self.now)
        saved = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        self.assertEqual([], calls)
        self.assertEqual(("paused", 0, 2),
                         (saved["state"], saved["search_calls_used"], saved["approved_search_calls"]))
        self.assertEqual("paused", result.state)

    def test_sustained_cpu_rise_during_request_finishes_then_pauses_before_next_page(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.jp, approved_search_calls=3),
                                   self.db, now=self.now)
        simulated_seconds = [0.0]
        guard = ResourceGuard(ResourceLimits(cpu_percent=80, cpu_sustain_seconds=60),
                              clock=lambda: simulated_seconds[0], min_sample_interval_seconds=5)
        calls = []
        def collector(*_args):
            calls.append(True)
            simulated_seconds[0] = 61.0
            return {"items": [{"video_id":"load0000001", "video_title":"朝のプレイリスト",
                               "description":"声やラップがあるか不明"}], "nextPageToken":"more"}
        result = scheduler.run_autopilot_block(session["id"], self.jp,
            autopilot.AutopilotController(self.db),
            autopilot.AutopilotConfig(self.jp, target=10, max_search_calls=3),
            collector, self.db, resource_guard=guard,
            resource_sampler=lambda: ResourceSnapshot(simulated_seconds[0], 95, 4*1024**3, 50, 100),
            now=self.now)
        saved = scheduler.get(session["id"], self.jp, self.db, now=self.now)
        self.assertEqual(1, len(calls))
        self.assertEqual(("paused", "system_cpu_sustained", 1),
                         (saved["state"], saved["pause_reason"], saved["search_calls_used"]))
        self.assertEqual("paused", result.state)

    def test_cpu_and_memory_limits_fail_closed_for_new_work_only(self):
        guard = ResourceGuard(ResourceLimits(cpu_percent=80, cpu_sustain_seconds=60,
            min_available_ram_bytes=2 * 1024**3, max_system_memory_percent=90,
            max_process_rss_bytes=1024**3, max_process_growth_bytes=512 * 1024**2))
        def snap(t, cpu=20, ram=4*1024**3, memory=50, rss=100*1024**2):
            return ResourceSnapshot(t, cpu, ram, memory, rss)
        self.assertTrue(guard.evaluate(snap(0)).allow_new_work)
        self.assertTrue(guard.evaluate(snap(1, cpu=85)).allow_new_work)
        self.assertFalse(guard.evaluate(snap(61, cpu=85)).allow_new_work)
        self.assertEqual("low_memory", guard.evaluate(snap(62, cpu=20, ram=1024)).status)
        self.assertFalse(guard.browser_allowed(snap(63, memory=92)).allow_new_work)
        growth_guard = ResourceGuard(ResourceLimits(max_process_growth_bytes=1024))
        growth_guard.evaluate(snap(0, rss=2048))
        self.assertIn("process_memory_growth", growth_guard.evaluate(snap(1, rss=4096)).reasons)

    def test_resource_sampling_is_rate_limited_and_monitor_is_singleton(self):
        ticks = [0.0]
        calls = []
        guard = ResourceGuard(clock=lambda: ticks[0], min_sample_interval_seconds=10)
        sampler = lambda: (calls.append(ticks[0]) or ResourceSnapshot(ticks[0], 10, 4*1024**3, 40, 50))
        guard.check(sampler)
        ticks[0] = 5
        guard.check(sampler)
        self.assertEqual([0.0], calls)
        ticks[0] = 10
        guard.check(sampler)
        self.assertEqual([0.0, 10], calls)

        monitor = ResourceMonitor(interval_seconds=5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            starts = list(pool.map(lambda _n: monitor.start(), range(4)))
        self.assertEqual(1, sum(starts))
        monitor.stop()
        self.assertFalse(monitor.running)


if __name__ == "__main__":
    unittest.main()
