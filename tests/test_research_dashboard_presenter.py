import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mcrs import genres, research_dashboard_presenter as presenter, store, vph
from mcrs.gui import Studio
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

    def _persist_cancelled_flow(self):
        self._persist_flow(intent="cancelled",schedule="cancelled",phase="cancelled",job="REVIEW")
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET request_id='dashboard_request',targets_json='[]' WHERE id='job_1'")
            db.execute("UPDATE research_schedules SET checkpoint_json=? WHERE id='schedule_1'",
                (json.dumps({"auto_start_request_id":"dashboard_request","vph_job_id":"job_1",
                             "uncertain_request":False,"phase":"cancelled",
                             "processed_video_ids":["video000001"]}),))
            db.execute("UPDATE research_schedules SET search_calls_used=3,elapsed_seconds=1440 WHERE id='schedule_1'")
            db.execute("UPDATE vph_research_jobs SET search_calls_used=3 WHERE id='job_1'")

    def test_cancelled_review_without_inflight_evidence_allows_distinct_new_request(self):
        self._persist_cancelled_flow()
        snapshot=store.research_dashboard_snapshot(self.profile,self.db)
        model=presenter.present(self.profile,self.db,auth_ready=True)
        self.assertTrue(snapshot["safe_new_request_allowed"])
        self.assertTrue(model["can_start"])
        self.assertEqual("BLOCKED",model["state"],"the historical REVIEW job must not be rewritten")
        self.assertTrue(model["historical_cancelled"])
        self.assertEqual("과거 연구 중단됨",model["history_label"])
        self.assertEqual((0,0),(model["counts"]["sample_1"],model["counts"]["sample_2"]))
        self.assertFalse(model["stages"]["sample_1"])
        self.assertFalse(model["stages"]["sample_2"])
        self.assertEqual(4,model["stage_progress_completed"])
        self.assertEqual(5,model["schedule_progress_percent"])

    def test_cancelled_review_blocks_when_search_is_inflight(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db: db.execute("UPDATE research_schedules SET search_in_flight=1 WHERE id='schedule_1'")
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_uncertain_checkpoint(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET checkpoint_json=? WHERE id='schedule_1'",
                (json.dumps({"vph_job_id":"job_1","uncertain_request":True}),))
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_checkpoint_requiring_recovery(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET checkpoint_json=? WHERE id='schedule_1'",
                (json.dumps({"vph_job_id":"job_1","phase":"network_review"}),))
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_mismatched_request_job_link(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET request_id='different_request' WHERE id='job_1'")
        snapshot=store.research_dashboard_snapshot(self.profile,self.db)
        self.assertFalse(snapshot["safe_new_request_allowed"])
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_existing_first_sample(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id='job_1'",
                (json.dumps([{"video_id":"video000001","first_observation_id":1,"first_job_id":"job_1"}]),))
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_existing_second_sample(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id='job_1'",
                (json.dumps([{"video_id":"video000001","second_observation_id":2,"second_job_id":"job_1"}]),))
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_unresolved_targets(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id='job_1'",
                (json.dumps([{"video_id":"video000001","group":"CORE_FIT"}]),))
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_review_blocks_another_open_request(self):
        self._persist_cancelled_flow()
        store.create_vph_auto_start_intent("another_open_request",self.settings,self.db)
        store.claim_vph_auto_start_intent("another_open_request",self.profile,self.settings,self.db)
        self.assertFalse(presenter.present(self.profile,self.db)["can_start"])

    def test_other_profile_open_request_does_not_block_profile_isolation(self):
        self._persist_cancelled_flow()
        other=store.create_profile("Other JP Profile","JP","CHILL_RAP",path=self.db)
        other_settings=dict(self.settings,market="JP",genre="CHILL_RAP")
        store.create_vph_auto_start_intent("other_profile_request",other_settings,self.db)
        store.claim_vph_auto_start_intent("other_profile_request",other,other_settings,self.db)
        self.assertTrue(presenter.present(self.profile,self.db)["can_start"])

    def test_cancelled_history_does_not_render_a_stage_as_running(self):
        self._persist_cancelled_flow()
        model=presenter.present(self.profile,self.db)
        app=Studio.__new__(Studio)
        app.research_stage_cards={key:MagicMock() for key in presenter.STAGE_KEYS}
        app.research_stage_card_labels={key:MagicMock() for key in presenter.STAGE_KEYS}
        app.dashboard_start_button=MagicMock();app.dashboard_pause_button=MagicMock()
        app.dashboard_resume_button=MagicMock();app.dashboard_stop_button=MagicMock()
        app.dashboard_results_button=MagicMock()
        app._set_research_stage_cards(model)
        self.assertTrue(model["historical_cancelled"])
        self.assertNotIn("#e5efff",[call.kwargs.get("background") for label in app.research_stage_card_labels.values()
                                    for call in label.configure.call_args_list])
        start_kwargs=app.dashboard_start_button.configure.call_args.kwargs
        self.assertEqual("normal" if model["can_start"] else "disabled",start_kwargs["state"])

    def test_cancelled_request_id_is_not_reused(self):
        self._persist_cancelled_flow()
        app=Studio.__new__(Studio);app.active_id=self.profile;app._starting_from_easy_dashboard=True
        app._vph_research_error_profile=None;app._vph_research_error_text=""
        app.current_profile=MagicMock(return_value=store.get_profile(self.profile,self.db))
        app._auto_start_settings=MagicMock(return_value=self.settings);app._autopilot_start=MagicMock()
        with patch("mcrs.gui.store.db_path",return_value=str(self.db)):
            app.start_vph_scheduled_research()
        app._autopilot_start.assert_called_once()
        new_request=app._autopilot_start.call_args.kwargs["auto_request_id"]
        self.assertNotEqual("dashboard_request",new_request)

    def test_cancelled_history_keeps_candidate_and_human_label_data(self):
        self._persist_cancelled_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET status='검토 보류',notes='human note' WHERE profile_id=? AND video_id=?",
                       (self.profile,"video000001"))
        before=store.get_candidate(self.profile,"video000001",path=self.db)
        model=presenter.present(self.profile,self.db)
        after=store.get_candidate(self.profile,"video000001",path=self.db)
        self.assertTrue(model["can_start"])
        self.assertEqual(before,after)

    def _bind_first(self,video_id="video000001",when=None,views=100):
        result=vph.record_observation(self.profile,video_id,views,when or datetime.now(timezone.utc),path=self.db)
        with store.connect(self.db) as db:
            row=db.execute("SELECT targets_json FROM vph_research_jobs WHERE id='job_1'").fetchone()
            targets=json.loads(row[0] or "[]")
            target=next((item for item in targets if item["video_id"]==video_id),targets[0])
            target.update(first_observation_id=result["later_observation_id"],first_job_id="job_1",
                first_evaluation_id=result["id"],first_observed_at_utc=when.isoformat() if when else
                datetime.now(timezone.utc).isoformat(),first_status="valid")
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id='job_1'",(json.dumps(targets),))
        return result

    def test_waiting_second_keeps_schedule_and_stage_progress_separate(self):
        self._persist_flow()
        self._bind_first(when=datetime(2026,1,1,tzinfo=timezone.utc))
        model=presenter.present(self.profile,self.db,now=datetime(2026,1,1,0,30,tzinfo=timezone.utc))
        self.assertEqual("WAITING_SAMPLE_2",model["state"])
        self.assertFalse(model["stages"]["auth"],"an existing schedule/job is not proof that credentials are connected")
        self.assertEqual(4,model["schedule_progress_percent"])
        self.assertTrue(model["stages"]["sample_1"])
        self.assertFalse(model["stages"]["sample_2"])
        self.assertTrue(model["insufficient_measurement"])
        self.assertTrue(model["can_pause"])
        self.assertTrue(model["can_stop"])
        self.assertFalse(model["can_resume"])

    def test_auth_stage_requires_explicit_credential_readiness(self):
        self._persist_flow()
        not_ready=presenter.present(self.profile,self.db)
        ready=presenter.present(self.profile,self.db,auth_ready=True)
        self.assertFalse(not_ready["stages"]["auth"])
        self.assertTrue(ready["stages"]["auth"])

    def test_ready_is_not_complete_and_can_start(self):
        model=presenter.present(self.profile,self.db)
        self.assertEqual("READY",model["state"])
        self.assertFalse(model["stages"]["final_result"])
        self.assertEqual(1,model["stage_progress_completed"])
        self.assertTrue(model["can_start"])
        self.assertFalse(model["can_pause"])
        self.assertFalse(model["can_resume"])
        self.assertFalse(model["can_stop"])

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

    def test_running_and_error_states_keep_safe_controls(self):
        self._persist_flow(job="COLLECTING_FIRST")
        running=presenter.present(self.profile,self.db)
        self.assertEqual("RUNNING",running["state"])
        self.assertTrue(running["can_pause"])
        self.assertTrue(running["can_stop"])
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET state='FAILED',message='safe failure' WHERE id='job_1'")
        failed=presenter.present(self.profile,self.db)
        self.assertEqual("ERROR",failed["state"])
        self.assertEqual("vph_failed",failed["error_code"])
        self.assertFalse(failed["stages"]["final_result"])

    def test_historical_candidates_do_not_complete_current_ready_stages(self):
        store.save_candidates(self.profile,[{"video_id":"hist0000001","video_title":"Old candidate",
            "genre_decision":genres.FIT,"genre_score":90}],path=self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET genre_decision=? WHERE profile_id=? AND video_id=?",
                       (genres.FIT,self.profile,"hist0000001"))
        model=presenter.present(self.profile,self.db)
        self.assertEqual("READY",model["state"])
        self.assertFalse(model["stages"]["search"])
        self.assertFalse(model["stages"]["genre_evaluation"])
        self.assertEqual(0,model["counts"]["candidates"])
        self.assertEqual(1,model["counts"]["cumulative_candidates"])

    def test_complete_requires_real_second_sample_and_completed_vph_job(self):
        self._persist_flow(intent="completed",schedule="completed",phase="complete",job="COMPLETED")
        first=datetime(2026,1,1,tzinfo=timezone.utc)
        first_eval=self._bind_first(when=first)
        second_eval=vph.record_observation(self.profile,"video000001",250,first+timedelta(seconds=3901),path=self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET request_id='dashboard_request' WHERE id='job_1'")
            targets=json.loads(db.execute("SELECT targets_json FROM vph_research_jobs WHERE id='job_1'").fetchone()[0])
            targets[0].update(second_observation_id=second_eval["later_observation_id"],second_job_id="job_1",
                second_evaluation_id=second_eval["id"],second_observed_at_utc=(first+timedelta(seconds=3901)).isoformat(),
                second_status="valid")
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id='job_1'",(json.dumps(targets),))
        model=presenter.present(self.profile,self.db,now=first+timedelta(seconds=4000))
        self.assertEqual("COMPLETE",model["state"])
        self.assertTrue(model["stages"]["final_result"])
        self.assertEqual(1,model["counts"]["sample_2"])
        self.assertFalse(model["insufficient_measurement"])
        self.assertFalse(model["can_start"])
        self.assertFalse(model["can_stop"])
        self.assertTrue(model["stages"]["vph"])
        self.assertEqual(1,model["counts"]["vph_a"])
        snapshot=store.research_dashboard_snapshot(self.profile,self.db)
        self.assertTrue(presenter.completed_run_has_linked_vph_evidence(
            snapshot,"dashboard_request","schedule_1"))

    def test_terminal_incomplete_history_allows_only_a_new_explicit_request(self):
        self._persist_flow(intent="completed",schedule="completed",phase="complete",job="REVIEW")
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET targets_json='[]' WHERE id='job_1'")
            db.execute("UPDATE research_schedules SET checkpoint_json=? WHERE id='schedule_1'",
                (json.dumps({"auto_start_request_id":"dashboard_request","vph_job_id":"job_1",
                             "uncertain_request":False}),))
        model=presenter.present(self.profile,self.db)
        self.assertEqual("BLOCKED",model["state"],"the historical run must not be rewritten as complete")
        self.assertEqual("completed_intent_incomplete_run",model["error_code"])
        self.assertEqual((0,0),(model["counts"]["sample_1"],model["counts"]["sample_2"]))
        self.assertTrue(model["can_start"],"the button creates a distinct request after terminal, certain history")
        self.assertIn("새 요청 ID",model["next_action"])
        app=Studio.__new__(Studio)
        app.active_id=self.profile
        app._starting_from_easy_dashboard=True
        app._vph_research_error_profile=None
        app._vph_research_error_text=""
        app.current_profile=MagicMock(return_value=store.get_profile(self.profile,self.db))
        app._auto_start_settings=MagicMock(return_value=self.settings)
        app._autopilot_start=MagicMock()
        with patch("mcrs.gui.store.db_path",return_value=str(self.db)):
            app.start_vph_scheduled_research()
        app._autopilot_start.assert_called_once()
        new_request=app._autopilot_start.call_args.kwargs["auto_request_id"]
        self.assertNotEqual("dashboard_request",new_request)
        self.assertEqual("claimed",store.get_vph_auto_start_intent(new_request,self.db)["status"])
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET search_in_flight=1 WHERE id='schedule_1'")
        uncertain=presenter.present(self.profile,self.db)
        self.assertFalse(uncertain["can_start"],"an unresolved external call must continue to block restart")
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET search_in_flight=0 WHERE id='schedule_1'")
        store.update_vph_auto_start_intent("dashboard_request","blocked",self.db,
            schedule_id="schedule_1",reason="insufficient_vph_measurement")
        recovered=presenter.present(self.profile,self.db)
        self.assertEqual("RUNNING",recovered["state"],"the newly claimed request is the current intent")
        self.assertFalse(recovered["can_start"],"the newly claimed request must block a duplicate start")

    def test_historical_two_samples_do_not_complete_new_ready_job(self):
        when=datetime(2026,1,1,tzinfo=timezone.utc)
        old_first=vph.record_observation(self.profile,"video000001",100,when,path=self.db)
        vph.record_observation(self.profile,"video000001",250,when+timedelta(seconds=3901),path=self.db)
        job=__import__("mcrs.vph_research",fromlist=["start"]).start(self.profile,self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id=?",
                (json.dumps([{"video_id":"video000001","group":"CORE_FIT",
                    "first_observation_id":old_first["later_observation_id"],
                    "first_evaluation_id":old_first["id"]}]),job["id"]))
        model=presenter.present(self.profile,self.db,now=when+timedelta(hours=2))
        self.assertEqual("READY",model["state"])
        self.assertEqual((0,0),(model["counts"]["sample_1"],model["counts"]["sample_2"]))
        self.assertGreaterEqual(model["counts"]["cumulative_second_sample"],1)
        self.assertFalse(model["stages"]["sample_1"])
        self.assertFalse(model["stages"]["sample_2"])

    def test_missing_job_link_blocks_does_not_join_independent_latest_rows(self):
        self._persist_flow()
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET checkpoint_json=? WHERE id='schedule_1'",
                (json.dumps({"vph_job_id":"not-job_1"}),))
        model=presenter.present(self.profile,self.db)
        self.assertEqual("BLOCKED",model["state"])
        self.assertEqual("schedule_job_mismatch",model["error_code"])
        self.assertFalse(model["can_start"])
        self.assertFalse(model["can_pause"])
        self.assertFalse(model["can_resume"])
        self.assertFalse(model["can_stop"])

    def test_profile_and_request_ids_are_part_of_sample_provenance(self):
        self._persist_flow()
        other=store.create_profile("Dashboard JP Evidence","JP","CHILL_RAP",path=self.db)
        observation=vph.record_observation(other,"video000001",100,datetime(2026,1,1,tzinfo=timezone.utc),path=self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET request_id='wrong-request' WHERE id='job_1'")
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id='job_1'",
                (json.dumps([{"video_id":"video000001","first_observation_id":observation["later_observation_id"],
                    "first_evaluation_id":observation["id"],"first_job_id":"job_1","first_status":"valid"}]),))
        model=presenter.present(self.profile,self.db)
        self.assertEqual("BLOCKED",model["state"])
        self.assertEqual("job_request_mismatch",model["error_code"])
        self.assertFalse(model["stages"]["sample_1"])
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET request_id='' WHERE id='job_1'")
        isolated=presenter.present(self.profile,self.db)
        self.assertTrue(isolated["session_consistent"])
        self.assertFalse(isolated["stages"]["sample_1"])

    def test_previous_completed_job_is_not_joined_to_new_schedule(self):
        self._persist_flow()
        with store.connect(self.db) as db:
            db.execute("INSERT INTO research_schedules(id,profile_id,market,core_genre,start_at_utc,total_minutes,"
                "research_minutes,break_minutes,approved_search_calls,state,phase,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",("old_schedule",self.profile,"KR","MORNING_CHILL_RAP",
                "2025-01-01T00:00:00+00:00",480,20,20,3,"completed","complete","2025-01-01 00:00:00"))
            db.execute("INSERT INTO vph_research_jobs(id,profile_id,state,schedule_id,targets_json,created_at) "
                "VALUES(?,?,?,?,?,?)",("old_job",self.profile,"COMPLETED","old_schedule","[]","2025-01-01 00:00:00"))
        model=presenter.present(self.profile,self.db)
        self.assertEqual("job_1",model["job_id"])
        self.assertEqual("schedule_1",model["schedule_id"])
        self.assertNotEqual("COMPLETE",model["state"])

    def test_user_paused_schedule_exposes_resume_only(self):
        self._persist_flow(schedule="paused",phase="paused",job="PAUSED")
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET pause_reason='user_paused' WHERE id='schedule_1'")
        model=presenter.present(self.profile,self.db)
        self.assertEqual("BLOCKED",model["state"])
        self.assertFalse(model["can_pause"])
        self.assertTrue(model["can_resume"])
        self.assertTrue(model["can_stop"])

    def test_profile_isolation(self):
        self._persist_flow()
        other=store.create_profile("Dashboard JP","JP","CHILL_RAP",path=self.db)
        model=presenter.present(other,self.db)
        self.assertEqual("READY",model["state"])
        self.assertEqual(0,model["counts"]["candidates"])
        self.assertEqual("",model["request_id"])

    def test_dashboard_control_rejects_job_from_another_profile(self):
        checkpoint=SimpleNamespace(set=MagicMock())
        target=SimpleNamespace(
            active_id=self.profile,
            _dashboard_auth_ready=MagicMock(return_value=False),
            research_dashboard_checkpoint=checkpoint,
            _current_vph_job=MagicMock(return_value={"id":"other_job","profile_id":"other_profile"}),
            pause_vph_research=MagicMock(),resume_vph_research=MagicMock(),cancel_vph_research=MagicMock(),
            _refresh_research_dashboard=MagicMock(),
        )
        model={"can_pause":True,"can_resume":False,"can_stop":True,"job_id":"job_1"}
        with patch.object(presenter,"present",return_value=model):
            Studio._dashboard_control_research(target,"pause")
        target.pause_vph_research.assert_not_called()
        checkpoint.set.assert_called_once()

    def test_dashboard_control_routes_matching_session_only(self):
        target=SimpleNamespace(
            active_id=self.profile,
            _dashboard_auth_ready=MagicMock(return_value=False),
            research_dashboard_checkpoint=SimpleNamespace(set=MagicMock()),
            _current_vph_job=MagicMock(return_value={"id":"job_1","profile_id":self.profile}),
            pause_vph_research=MagicMock(),resume_vph_research=MagicMock(),cancel_vph_research=MagicMock(),
            _refresh_research_dashboard=MagicMock(),
        )
        model={"can_pause":True,"can_resume":False,"can_stop":True,"job_id":"job_1"}
        with patch.object(presenter,"present",return_value=model):
            Studio._dashboard_control_research(target,"pause")
        target.pause_vph_research.assert_called_once_with()
        target._refresh_research_dashboard.assert_called_once_with(self.profile)


if __name__ == "__main__":
    unittest.main()
