import threading
import time
import unittest

from mcrs import ai_research, autopilot, comments, genres, store
from tests.support import WorkspaceTemp


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp(); self.root = self.temp.__enter__()
        self.db = self.root / "autopilot.db"
        self.profile = store.create_profile("Autopilot isolated JP", "JP", "MORNING_CHILL_RAP",
            self.db, related_genres=["CHILL_RAP", "RNB_HIPHOP"])

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    @staticmethod
    def fit_row(index):
        video_id = f"ap{index:09d}"  # exactly 11 ASCII characters
        return {"video_id": video_id, "video_title": f"Morning Chill Rap mellow groove {index}",
                "description": "", "channel_title": f"JP Channel {index % 3}",
                "video_url": f"https://www.youtube.com/watch?v={video_id}", "keyword": "morning chill rap"}

    def test_one_click_pipeline_reaches_ten_fit_target_and_persists_report(self):
        rows = [self.fit_row(i) for i in range(10)]
        calls = []
        def collector(term, market, limit, page_token=None):
            calls.append((term, market, limit, page_token))
            return {"items": rows, "nextPageToken": ""}
        events = []
        result = autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile, target=10, max_search_calls=3),
            collector, on_event=events.append)
        self.assertEqual("complete", result.state)
        self.assertEqual(10, result.report["counts"]["ai_recommended_fit"])
        self.assertTrue(result.report["target_met"])
        self.assertEqual(1, result.report["search_calls_used"])
        self.assertEqual(2, result.report["search_calls_remaining"])
        self.assertEqual(50, calls[0][2])
        self.assertEqual(10, result.report["candidate_total"])
        self.assertEqual(0, result.report["counts"]["human_verified"])
        self.assertFalse(result.report["actual_audio_listening_claimed"])
        self.assertTrue(any(event["stage"] == "complete" for event in events))
        saved = store.get_autopilot_report(self.profile, result.session_id, self.db)
        self.assertEqual(result.report, saved["report"])
        self.assertEqual("", store.list_genre_audit(self.profile, path=self.db)[0]["human_label"])

    def test_shortfall_is_exact_and_does_not_promote_related_or_ambiguous_candidates(self):
        rows = [self.fit_row(0), {"video_id":"review00001", "video_title":"朝のプレイリスト",
                 "description":"落ち着いた雰囲気", "channel_title":"review channel"}]
        result = autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile, target=5, max_search_calls=1),
            lambda *_args: {"items":rows,"nextPageToken":""})
        self.assertEqual(4, result.report["shortfall"])
        self.assertFalse(result.report["target_met"])
        self.assertEqual(1, result.report["counts"]["ai_recommended_fit"])
        self.assertNotEqual("auto_fit", store.list_genre_audit(self.profile,path=self.db)[-1]["proposed_verdict"])
        self.assertIn("goal_not_met", [item["type"] for item in result.exceptions])

    def test_search_ceiling_and_duplicate_pages_are_accounted_exactly(self):
        calls=[]
        row=self.fit_row(0)
        def collector(_term,_market,_limit,page_token=None):
            calls.append(page_token)
            return {"items":[row],"nextPageToken":"p2" if page_token is None else "p3"}
        result=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile,target=20,max_search_calls=2),collector)
        self.assertEqual(2,len(calls))
        self.assertEqual([None,"p2"],calls)
        self.assertEqual(2,result.report["search_calls_used"])
        self.assertEqual(1,result.report["duplicate_removed"])
        self.assertEqual(19,result.report["shortfall"])

    def test_profile_scoped_comment_suggestions_need_actual_note_and_keep_old_draft(self):
        row=self.fit_row(4)
        store.save_candidates(self.profile,[row],self.db)
        store.update_candidate(self.profile,row["video_id"],"미확인","실제 감상 메모: 리듬이 부드러워 아침에 잘 어울렸습니다.","기존 초안은 보존합니다.",self.db)
        result=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile,target=5,max_search_calls=1),
            lambda *_args:{"items":[row],"nextPageToken":""})
        saved=store.get_candidate(self.profile,row["video_id"],self.db)
        drafts=store.list_autopilot_comment_drafts(self.profile,row["video_id"],self.db)
        self.assertEqual("기존 초안은 보존합니다.",saved["draft"])
        self.assertEqual(1,len(drafts)); self.assertEqual(3,len(drafts[0]["drafts"]))
        self.assertEqual(1,result.report["comment_draft_videos_prepared"])
        self.assertTrue(all("리듬이 부드러워" in draft for draft in drafts[0]["drafts"]))

    def test_profile_without_real_observation_never_gets_fabricated_comment(self):
        row=self.fit_row(5)
        result=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile,target=5,max_search_calls=1),
            lambda *_args:{"items":[row],"nextPageToken":""})
        self.assertEqual(0,result.report["comment_draft_videos_prepared"])
        self.assertEqual(1,result.report["comment_draft_waiting_for_real_listening_note"])
        self.assertEqual([],store.list_autopilot_comment_drafts(self.profile,row["video_id"],self.db))

    def test_paid_provider_requires_explicit_budget_approval_before_collector(self):
        called=[]
        config=autopilot.AutopilotConfig(self.profile,provider_id="gemini",
            allow_paid_external=False,cost_estimate_usd=0.01,cost_limit_usd=1.0)
        with self.assertRaises(PermissionError):
            autopilot.AutopilotController(self.db).run(config,lambda *_args:called.append(1))
        self.assertEqual([],called)
        with self.assertRaises(ValueError):
            autopilot.AutopilotConfig(self.profile,provider_id="gemini",allow_paid_external=True,
                cost_estimate_usd=float("nan"),cost_limit_usd=1).validate()

    def test_first_ai_provider_failure_opens_circuit_and_never_retries_candidates(self):
        class FailingProvider:
            name="gemini"; model="mock-model"; calls=0
            def review(inner,candidate,genre_id,related_genres=()):
                inner.calls += 1
                raise RuntimeError("sensitive provider response must not display")
        underlying=FailingProvider()
        rows=[{"video_id":f"unc{n:08d}","video_title":f"Morning playlist {n}","description":"uncertain metadata"} for n in range(4)]
        session=[]
        config=autopilot.AutopilotConfig(self.profile,target=20,max_search_calls=1,provider_id="gemini",
            allow_paid_external=True,cost_estimate_usd=0.01,cost_limit_usd=2.0)
        # A provider used by the test is mock-only; no HTTP implementation is involved.
        result=autopilot.AutopilotController(self.db).run(config,
            lambda *_args:{"items":rows,"nextPageToken":""},provider=underlying)
        self.assertEqual(1,underlying.calls)
        self.assertEqual(1,result.report["ai_requests_used"])
        self.assertIn("ai_provider_failure",[item["type"] for item in result.exceptions])
        self.assertNotIn("sensitive provider response",str(result.report))

    def test_resume_reuses_saved_scope_and_skips_uncertain_inflight_page(self):
        config=autopilot.AutopilotConfig(self.profile,target=10,max_search_calls=2)
        pause=threading.Event(); reached=threading.Event(); release=threading.Event()
        rows=[{"video_id":"review00001","video_title":"unknown music","description":""}]
        controller=autopilot.AutopilotController(self.db)
        outcome=[]
        def collector(*_args):
            reached.set(); release.wait(2); pause.set()
            return {"items":rows,"nextPageToken":"p2"}
        worker=threading.Thread(target=lambda:outcome.append(controller.run(config,collector)))
        worker.start(); self.assertTrue(reached.wait(2)); self.assertTrue(controller.pause()); release.set(); worker.join(4)
        self.assertFalse(worker.is_alive())
        first=outcome[0]
        self.assertEqual("paused",first.state)
        saved=store.get_autopilot_settings(self.profile,first.session_id,self.db)
        self.assertEqual(2,saved["max_search_calls"])
        session=store.get_research_session(first.session_id,self.profile,self.db)
        store.checkpoint_research_session(first.session_id,self.profile,state="running",in_flight=True,
            query_index=session["query_index"],page_token=session["page_token"],calls_used=session["calls_used"],path=self.db)
        calls=[]
        def resumed_collector(term, market, max_results, page_token=None):
            calls.append((term,market,max_results,page_token))
            return {"items":[],"nextPageToken":""}
        resumed=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile,target=10,max_search_calls=2,session_id=first.session_id),
            resumed_collector)
        self.assertEqual(1,len(calls))
        self.assertIsNone(calls[0][3])
        self.assertEqual(2,resumed.report["search_calls_used"])
        self.assertIn("inflight_page_skipped",[item["type"] for item in resumed.exceptions])

    def test_same_controller_rejects_concurrent_duplicate_job(self):
        entered=threading.Event(); release=threading.Event(); controller=autopilot.AutopilotController(self.db)
        config=autopilot.AutopilotConfig(self.profile,target=5,max_search_calls=1)
        first=[]
        thread=threading.Thread(target=lambda:first.append(controller.run(config,
            lambda *_args:(entered.set(),release.wait(2),{"items":[],"nextPageToken":""})[2])))
        thread.start(); self.assertTrue(entered.wait(2))
        with self.assertRaises(autopilot.AutopilotBusy):
            controller.run(config,lambda *_args:{"items":[],"nextPageToken":""})
        second_calls=[]
        cross_process_guard=autopilot.AutopilotController(self.db).run(config,
            lambda *_args:(second_calls.append(True) or {"items":[],"nextPageToken":""}))
        self.assertEqual([],second_calls)
        self.assertEqual("active_job",cross_process_guard.exceptions[0]["type"])
        release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_report_and_settings_are_profile_isolated(self):
        second=store.create_profile("Autopilot other KR","KR","CHILL_RAP",self.db)
        result=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile,target=5,max_search_calls=1),
            lambda *_args:{"items":[],"nextPageToken":""})
        self.assertIsNone(store.get_autopilot_report(second,result.session_id,self.db))
        self.assertIsNone(store.get_autopilot_settings(second,result.session_id,self.db))
        self.assertEqual(result.session_id,store.get_latest_autopilot_report(self.profile,self.db)["report"]["session_id"])
        self.assertIsNone(store.get_latest_autopilot_report(second,self.db))
        self.assertFalse(store.list_autopilot_comment_drafts(second,path=self.db))

    def test_legacy_database_migration_preserves_candidate_and_person_label(self):
        row=self.fit_row(8); store.save_candidates(self.profile,[row],self.db)
        store.label_genre_audit(self.profile,row["video_id"],genres.FIT,"실제 청취 후 보컬과 리듬을 확인했습니다.",self.db)
        before=store.get_candidate(self.profile,row["video_id"],self.db)
        store.initialize(self.db); store.initialize(self.db)
        after=store.get_candidate(self.profile,row["video_id"],self.db)
        audit=store.list_genre_audit(self.profile,path=self.db)[0]
        self.assertEqual(before["video_id"],after["video_id"])
        self.assertEqual(genres.FIT,audit["human_label"])
        self.assertTrue(store.get_autopilot_report(self.profile,"missing",self.db) is None)


if __name__ == "__main__":
    unittest.main()
