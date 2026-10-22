import json
import unittest
from datetime import datetime, timedelta, timezone

from mcrs import genres, research_dashboard_presenter as presenter, store, vph
from tests.support import WorkspaceTemp


class ResearchDashboardPresenterTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp();self.root=self.temp.__enter__()
        self.db=self.root/"dashboard.sqlite"
        store.initialize(self.db)
        self.profile=store.create_profile("Dashboard KR","KR","MORNING_CHILL_RAP",path=self.db)
        self.settings={"total_hours":8,"research_minutes":20,"break_minutes":20,
            "observation_slots":10,"search_calls":3,"provider_id":"disabled","paid_ai":False,"ai_calls":0,
            "market":"KR","genre":"MORNING_CHILL_RAP","channel_id":"","schedule_mode":"fixed",
            "research_min_minutes":20,"research_max_minutes":20,"break_min_minutes":20,"break_max_minutes":20}

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def _persist_flow(self,*,intent="started",schedule="running",phase="research",job="WAITING_SECOND"):
        store.create_vph_auto_start_intent("dashboard_request",self.settings,self.db)
        store.claim_vph_auto_start_intent("dashboard_request",self.profile,self.settings,self.db)
        store.update_vph_auto_start_intent("dashboard_request",intent,self.db,schedule_id="schedule_1")
        with store.connect(self.db) as db:
            db.execute("""INSERT INTO research_schedules(id,profile_id,market,core_genre,start_at_utc,total_minutes,
                research_minutes,break_minutes,approved_search_calls,state,phase,elapsed_seconds,auto_start_request_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",("schedule_1",self.profile,"KR","MORNING_CHILL_RAP",
                "2026-01-01T00:00:00+00:00",480,20,20,3,schedule,phase,1200,"dashboard_request"))
            db.execute("""INSERT INTO vph_research_jobs(id,profile_id,state,search_limit,observation_slots,schedule_id,
                targets_json,next_run_utc,message) VALUES(?,?,?,?,?,?,?,?,?)""",("job_1",self.profile,job,3,10,
                "schedule_1",json.dumps([{"video_id":"video000001","group":"FIT"}]),
                "2026-01-01T01:05:01+00:00","checkpoint"))
        store.save_candidates(self.profile,[{"video_id":"video000001","video_title":"Morning chill rap",
            "genre_decision":genres.FIT,"genre_score":95}],path=self.db)

    def test_waiting_second_keeps_schedule_and_stage_progress_separate(self):
        self._persist_flow()
        vph.record_observation(self.profile,"video000001",100,datetime(2026,1,1,tzinfo=timezone.utc),path=self.db)
        model=presenter.present(self.profile,self.db,now=datetime(2026,1,1,0,30,tzinfo=timezone.utc))
        self.assertEqual("WAITING_SAMPLE_2",model["state"])
        self.assertEqual(4,model["schedule_progress_percent"])
        self.assertTrue(model["stages"]["sample_1"])
        self.assertFalse(model["stages"]["sample_2"])
        self.assertTrue(model["insufficient_measurement"])

    def test_uncertain_request_is_blocked_and_never_complete(self):
        self._persist_flow(job="READY")
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET search_in_flight=1 WHERE id='schedule_1'")
        model=presenter.present(self.profile,self.db)
        self.assertEqual("BLOCKED",model["state"])
        self.assertEqual("uncertain_external_request",model["error_code"])
        self.assertFalse(model["stages"]["final_result"])

    def test_break_is_distinct_from_schedule_progress(self):
        self._persist_flow(phase="break",job="READY")
        model=presenter.present(self.profile,self.db)
        self.assertEqual("BREAK",model["state"])
        self.assertGreater(model["schedule_progress_percent"],0)
        self.assertFalse(model["stages"]["sample_1"])

    def test_complete_requires_real_second_sample_and_completed_vph_job(self):
        self._persist_flow(intent="completed",schedule="completed",phase="complete",job="COMPLETED")
        first=datetime(2026,1,1,tzinfo=timezone.utc)
        vph.record_observation(self.profile,"video000001",100,first,path=self.db)
        vph.record_observation(self.profile,"video000001",250,first+timedelta(seconds=3901),path=self.db)
        model=presenter.present(self.profile,self.db,now=first+timedelta(seconds=4000))
        self.assertEqual("COMPLETE",model["state"])
        self.assertTrue(model["stages"]["final_result"])
        self.assertEqual(1,model["counts"]["sample_2"])
        self.assertFalse(model["insufficient_measurement"])

    def test_profile_isolation(self):
        self._persist_flow()
        other=store.create_profile("Dashboard JP","JP","CHILL_RAP",path=self.db)
        model=presenter.present(other,self.db)
        self.assertEqual("READY",model["state"])
        self.assertEqual(0,model["counts"]["candidates"])
        self.assertEqual("",model["request_id"])


if __name__ == "__main__":
    unittest.main()
