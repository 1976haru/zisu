"""Low-load admission, process metrics, concurrency slots and bounded-run checks."""
from __future__ import annotations

import gc
import math
import threading
import sys
import time
import tracemalloc
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mcrs import ai_research, research_scheduler, store, youtube_api
from mcrs.resource_guard import ResourceGuard, ResourceLimits, ResourceMonitor, ResourceSnapshot, sample_resources
from mcrs.work_slots import occupy
from tests.support import WorkspaceTemp


class ResourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        self.db = self.root / "resource-isolated.sqlite"
        store.initialize(self.db)
        self.profile = store.create_profile("Resource isolated", "JP", "MORNING_CHILL_RAP", path=self.db)

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def test_unavailable_or_invalid_samples_fail_closed_then_recover_after_cadence(self):
        clock = [0.0]
        guard = ResourceGuard(clock=lambda: clock[0], min_sample_interval_seconds=10)
        failed = guard.check(lambda: (_ for _ in ()).throw(OSError("private sampler details")))
        self.assertFalse(failed.allow_new_work)
        self.assertEqual(("unavailable", ("resource_sample_unavailable",)), (failed.status, failed.reasons))
        samples = []
        cached = guard.check(lambda: (samples.append(True) or ResourceSnapshot(clock[0], 10, 4*1024**3, 40, 100)))
        self.assertFalse(cached.allow_new_work)
        self.assertEqual([], samples)
        clock[0] = 10
        healthy = guard.check(lambda: (samples.append(True) or ResourceSnapshot(clock[0], 10, 4*1024**3, 40, 100)))
        self.assertTrue(healthy.allow_new_work)
        self.assertEqual([True], samples)
        for bad in (ResourceSnapshot(11, float("nan"), 4*1024**3, 40, 100),
                    ResourceSnapshot(11, 10, -1, 40, 100),
                    ResourceSnapshot(float("inf"), 10, 4*1024**3, 40, 100)):
            self.assertFalse(guard.evaluate(bad).allow_new_work)

    def test_resource_threshold_edges_and_cpu_sustain_boundary(self):
        guard = ResourceGuard(ResourceLimits(cpu_percent=80, cpu_sustain_seconds=60,
            min_available_ram_bytes=2*1024**3, max_system_memory_percent=90,
            max_process_rss_bytes=1024**3, max_process_growth_bytes=512*1024**2))
        snap = lambda t, cpu=20, ram=2*1024**3, memory=89.9, rss=100: ResourceSnapshot(t,cpu,ram,memory,rss,0)
        self.assertTrue(guard.evaluate(snap(0)).allow_new_work)
        self.assertTrue(guard.evaluate(snap(1,cpu=80)).allow_new_work)
        self.assertTrue(guard.evaluate(snap(60,cpu=80)).allow_new_work)
        self.assertFalse(guard.evaluate(snap(61,cpu=80)).allow_new_work)
        self.assertFalse(guard.evaluate(snap(62,cpu=20,memory=90)).allow_new_work)
        self.assertFalse(guard.evaluate(snap(63,cpu=20,ram=2*1024**3-1)).allow_new_work)

    def test_live_process_sampler_reports_rss_and_process_cpu_separately(self):
        sample_resources()  # Establish the native Windows CPU counter baseline.
        time.sleep(1.0)  # Low-frequency observation; this is not a load test.
        snapshot = sample_resources()
        self.assertIsInstance(snapshot, ResourceSnapshot)
        self.assertIsNotNone(snapshot.process_rss_bytes, "current process RSS should be sampled on this host")
        self.assertGreater(snapshot.process_rss_bytes, 0)
        self.assertIsNotNone(snapshot.process_cpu_percent, "second process sample should report CPU on this Windows host")
        self.assertTrue(math.isfinite(snapshot.process_cpu_percent))
        self.assertGreaterEqual(snapshot.process_cpu_percent, 0)
        print("RESOURCE_SAMPLE", {
            "system_cpu_percent": snapshot.system_cpu_percent,
            "system_available_ram_bytes": snapshot.available_ram_bytes,
            "system_memory_percent": snapshot.system_memory_percent,
            "python_process_rss_bytes": snapshot.process_rss_bytes,
            "python_process_cpu_percent": snapshot.process_cpu_percent,
        })

    def test_process_wide_search_slot_prevents_overlapping_search_adapters(self):
        entered, release = threading.Event(), threading.Event()
        original_occupy = occupy
        calls = []

        @contextmanager
        def short_slot(slot):
            with original_occupy(slot, timeout=0.02):
                yield

        def blocked_lookup(*_args, **_kwargs):
            calls.append(True)
            entered.set()
            if not release.wait(2):
                raise TimeoutError("mock release timeout")
            return []

        errors = []
        with patch("mcrs.youtube_api.occupy", short_slot), patch("mcrs.youtube_api._lookup_videos", blocked_lookup):
            worker = threading.Thread(target=lambda: youtube_api.lookup_videos("mock", "term", "JP"))
            worker.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(youtube_api.APIError) as caught:
                youtube_api.lookup_videos("mock", "term2", "JP")
            errors.append(str(caught.exception))
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([True], calls)
        self.assertIn("다른 검색", errors[0])

    def test_process_wide_ai_slot_prevents_overlapping_provider_http_calls(self):
        entered, release = threading.Event(), threading.Event()
        original_occupy = occupy
        calls = []

        @contextmanager
        def short_slot(slot):
            with original_occupy(slot, timeout=0.02):
                yield

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, _limit):
                calls.append(True)
                entered.set()
                if not release.wait(2): raise TimeoutError("mock release timeout")
                return b"{}"

        errors = []
        with patch("mcrs.ai_research.occupy", short_slot), patch("mcrs.ai_research.urlopen", lambda *_a, **_k: Response()):
            first = ai_research.JsonHttpProvider()
            second = ai_research.JsonHttpProvider()
            worker = threading.Thread(target=lambda: first._get("https://mock.invalid", {}))
            worker.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ai_research.ProviderFailure) as caught:
                second._get("https://mock.invalid", {})
            errors.append(caught.exception.code)
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([True], calls)
        self.assertEqual(["busy"], errors)

    def test_resource_monitor_survives_sampler_failure_and_repeated_start_stop(self):
        baseline_threads=threading.active_count()
        class FastEvent:
            def __init__(self): self.calls=0; self.stopped=False
            def clear(self): self.calls=0; self.stopped=False
            def set(self): self.stopped=True
            def wait(self,_timeout):
                self.calls+=1
                return self.stopped or self.calls>1
        events=[]
        monitor=ResourceMonitor(sampler=lambda: (_ for _ in ()).throw(OSError("private sampler failure")),
                                interval_seconds=5,on_decision=events.append)
        monitor._stop=FastEvent()
        for _ in range(4):
            self.assertTrue(monitor.start())
            deadline=time.monotonic()+1
            while monitor.running and time.monotonic()<deadline: time.sleep(0.005)
            monitor.stop()
            self.assertFalse(monitor.running)
        self.assertEqual(4,len(events))
        self.assertTrue(all(not event.allow_new_work and event.status=="unavailable" for event in events))
        self.assertLessEqual(threading.active_count(),baseline_threads)

    def test_six_hour_virtual_runs_and_repeated_pause_resume_do_not_accumulate_threads_or_handles(self):
        def rss():
            return sample_resources().process_rss_bytes

        thread_count = threading.active_count
        def handle_count():
            if sys.platform != "win32":
                return None
            import ctypes
            from ctypes import wintypes
            kernel32=ctypes.WinDLL("kernel32",use_last_error=True)
            kernel32.GetCurrentProcess.restype=wintypes.HANDLE
            kernel32.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetProcessHandleCount.restype=wintypes.BOOL
            value=wintypes.DWORD()
            if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(),ctypes.byref(value)):
                return None
            return int(value.value)

        baseline_rss, baseline_threads, baseline_handles = rss(), thread_count(), handle_count()
        self.assertIsNotNone(baseline_rss)
        tracemalloc.start()
        clock = datetime(2026,10,18,0,0,tzinfo=timezone.utc)
        rss_samples = [baseline_rss]
        for index in range(12):
            now = clock + timedelta(hours=index*6)
            schedule = research_scheduler.create(research_scheduler.ScheduleConfig(
                self.profile,total_hours=6,research_minutes=30,break_minutes=30,approved_search_calls=3),
                self.db,now=now)
            for minute in (15,30,45,60,90,120,180,240,300,359):
                item = research_scheduler.get(schedule["id"],self.profile,self.db,now=now+timedelta(minutes=minute))
                self.assertIn(item["phase"],{"research","break","complete"})
                if minute == 45:
                    research_scheduler.pause(schedule["id"],self.profile,self.db,now=now+timedelta(minutes=45))
                    research_scheduler.resume(schedule["id"],self.profile,self.db,now=now+timedelta(minutes=45))
            completed = research_scheduler.get(schedule["id"],self.profile,self.db,now=now+timedelta(hours=6))
            self.assertEqual("completed",completed["state"])
            if index in (3,7,11):
                gc.collect()
                rss_samples.append(rss())
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        final_snapshot = sample_resources()
        final_rss, final_threads, final_handles = final_snapshot.process_rss_bytes, thread_count(), handle_count()
        self.assertLessEqual(final_threads, baseline_threads+1)
        if baseline_handles is not None and final_handles is not None:
            self.assertLessEqual(abs(final_handles-baseline_handles),8)
        self.assertLessEqual(max(rss_samples)-min(rss_samples),64*1024**2)
        with store.connect(self.db) as db:
            self.assertEqual("ok",db.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(12,db.execute("SELECT COUNT(*) FROM research_schedules").fetchone()[0])
        print("VIRTUAL_LONG_RUN_METRICS", {
            "six_hour_sessions":12,"pause_resume_cycles":12,"rss_milestones_bytes":rss_samples,
            "final_rss_bytes":final_rss,"rss_growth_bytes":final_rss-baseline_rss,
            "final_process_cpu_percent":final_snapshot.process_cpu_percent,
            "threads_before_after":(baseline_threads,final_threads),
            "handles_before_after":(baseline_handles,final_handles),
            "tracemalloc_current_bytes":current_bytes,"tracemalloc_peak_bytes":peak_bytes,
        })


if __name__ == "__main__":
    unittest.main()
