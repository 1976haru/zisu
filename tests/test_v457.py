import unittest
from unittest.mock import patch

from mcrs import ai_research, comments, genres, store, timers, youtube_api
from tests.support import WorkspaceTemp


class V457AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__()
        self.db=self.root/"isolated.db"
        self.profile=store.create_profile("자동 추천 JP","JP","MORNING_CHILL_RAP",self.db,
            related_genres=["CHILL_RAP","RNB_HIPHOP"])

    def tearDown(self):
        self.temp.__exit__(None,None,None)

    def test_fit_target_modes_accept_5_10_20_and_legacy_candidate_targets(self):
        for target in (5,10,20):
            plan=ai_research.preview_plan(self.profile,target,self.db,target_mode="fit_target")
            self.assertEqual("fit_target",plan["target_mode"])
        for target in (10,30,50):
            plan=ai_research.preview_plan(self.profile,target,self.db)
            self.assertEqual("candidate_count",plan["target_mode"])
        with self.assertRaises(ValueError):
            ai_research.preview_plan(self.profile,5,self.db)

    def test_three_call_plan_keeps_core_first_and_allocates_related_when_available(self):
        plan=ai_research.preview_plan(self.profile,10,self.db,max_search_calls=3,target_mode="fit_target")
        self.assertEqual(2,plan["core_search_calls"])
        self.assertEqual(1,plan["related_search_calls"])
        self.assertLess(plan["search_terms"].index(plan["core_search_terms"][-1]),
                        len(plan["core_search_terms"]))

    def test_search_pages_continue_until_fit_goal_or_exact_call_ceiling(self):
        pages=[{"items":[{"video_id":"uncertain01","video_title":"unknown track"}],"nextPageToken":"p2"},
               {"items":[{"video_id":"morning0001","video_title":"Morning Chill Rap mellow groove R&B",
                           "description":"vocal performance over a steady beat"}],"nextPageToken":""}]
        calls=[]
        def collect(term,market,limit,page_token=None):
            calls.append((term,market,limit,page_token)); return pages.pop(0)
        run=ai_research.run_auto_recommendations(self.profile,collect,target=5,max_search_calls=2,path=self.db)
        self.assertEqual(2,run.calls_used)
        self.assertEqual(1,run.recommended_fits)
        self.assertEqual(4,run.shortfall)
        self.assertEqual(50,calls[0][2])
        self.assertEqual("p2",calls[1][3])

    def test_duplicate_video_ids_across_pages_are_counted_once(self):
        pages=[{"items":[{"video_id":"uncertain01","video_title":"unknown track"}],"nextPageToken":"p2"},
               {"items":[{"video_id":"uncertain01","video_title":"unknown track"}],"nextPageToken":""}]
        def collect(*_args): return pages.pop(0)
        run=ai_research.run_auto_recommendations(self.profile,collect,target=5,max_search_calls=2,path=self.db)
        self.assertEqual(1,len(run.candidates)); self.assertEqual(1,run.duplicate_count)
        self.assertEqual(0,run.recommended_fits); self.assertEqual(5,run.shortfall)

    def test_ai_fit_opinion_never_promotes_rule_review_or_human_label(self):
        provider=ai_research.MockProvider(ai_research.AIOpinion(genres.FIT,(),(),(),"uncertain","listen"))
        row={"video_id":"uncertain01","video_title":"unknown track","description":""}
        store.save_candidates(self.profile,[row],self.db)
        review=ai_research.review_candidate(self.profile,ai_research.Candidate(row["video_id"],row["video_title"]),provider,self.db)
        ai_research.persist_review_decisions(self.profile,[review],[row],self.db)
        audit=store.list_genre_audit(self.profile,path=self.db)[0]
        self.assertEqual(genres.REVIEW,review.final_verdict)
        self.assertEqual(genres.FIT,audit["ai_verdict"])
        self.assertEqual("insufficient_evidence",audit["proposed_verdict"])
        self.assertEqual("",audit["human_label"])
        self.assertEqual("needs_review",audit["review_status"])

    def test_ai_can_suggest_fit_without_rule_conflict_but_not_write_human_truth(self):
        provider=ai_research.MockProvider(ai_research.AIOpinion(genres.FIT,(),(),
            ("soft mellow morning",),"metadata only","please listen"))
        row={"video_id":"uncertain01","video_title":"unknown track","description":"soft mellow morning"}
        store.save_candidates(self.profile,[row],self.db)
        review=ai_research.review_candidate(self.profile,
            ai_research.Candidate(row["video_id"],row["video_title"],row["description"]),provider,self.db)
        self.assertEqual(genres.REVIEW,review.final_verdict)
        self.assertEqual("auto_fit",ai_research.proposed_verdict(store.get_profile(self.profile,self.db),review,row))
        ai_research.persist_review_decisions(self.profile,[review],[row],self.db)
        audit=store.list_genre_audit(self.profile,path=self.db)[0]
        self.assertEqual("auto_fit",audit["proposed_verdict"])
        self.assertEqual("",audit["human_label"])
        self.assertEqual(0,store.genre_quality(self.profile,self.db)["labeled"])

    def test_human_label_and_metrics_remain_separate_from_ai(self):
        row={"video_id":"morning0001","video_title":"Morning Chill Rap mellow groove R&B",
             "description":"vocal performance over a steady beat"}
        store.save_candidates(self.profile,[row],self.db)
        review=ai_research.review_candidate(self.profile,ai_research.Candidate(row["video_id"],row["video_title"],row["description"]),None,self.db)
        ai_research.persist_review_decisions(self.profile,[review],[row],self.db)
        store.label_genre_audit(self.profile,row["video_id"],"적합","직접 듣고 보컬과 리듬을 확인함",self.db)
        audit=store.list_genre_audit(self.profile,path=self.db)[0]
        self.assertEqual(genres.FIT,audit["rule_verdict"])
        self.assertEqual("auto_fit",audit["proposed_verdict"])
        self.assertEqual("적합",audit["human_label"])
        self.assertEqual("human_reviewed",audit["review_status"])
        self.assertEqual(1,store.genre_quality(self.profile,self.db)["labeled"])

    def test_auto_research_session_persists_page_cursor_and_profile_scope(self):
        import threading
        pause=threading.Event()
        def collect(*_args):
            pause.set()
            return {"items":[],"nextPageToken":"p2"}
        run=ai_research.run_auto_recommendations(self.profile,collect,target=20,max_search_calls=2,path=self.db,
            pause_event=pause)
        session=store.get_research_session(run.session_id,self.profile,self.db)
        self.assertEqual("fit_target",session["target_mode"])
        self.assertLessEqual(session["calls_used"],1)
        self.assertEqual("p2",session["page_token"])
        self.assertEqual("paused",session["state"])
        resumed_tokens=[]
        def resumed(_term,_market,_limit,page_token=None):
            resumed_tokens.append(page_token)
            return {"items":[],"nextPageToken":""}
        # Continue the paused session under the same approved ceiling.
        resumed_run=ai_research.run_auto_recommendations(self.profile,resumed,target=20,
            max_search_calls=2,path=self.db,session_id=run.session_id)
        self.assertEqual(["p2"],resumed_tokens)
        self.assertEqual(2,resumed_run.calls_used)

    def test_search_failure_is_counted_without_automatic_retry_and_can_resume(self):
        calls=[]
        def fail(*_args):
            calls.append(1)
            raise RuntimeError("provider failure with private response")
        run=ai_research.run_auto_recommendations(self.profile,fail,target=5,max_search_calls=2,path=self.db)
        self.assertEqual(1,run.calls_used); self.assertEqual(1,len(calls))
        self.assertEqual("paused",run.state); self.assertNotIn("private response",run.warning)
        sessions=store.list_resumable_sessions(self.profile,self.db)
        self.assertEqual(run.session_id,sessions[0]["id"])
        self.assertEqual(1,sessions[0]["calls_used"])

    def test_persona_settings_and_25_example_libraries_are_profile_scoped(self):
        self.assertEqual(25,len(comments.LIBRARY["jp_morning_chill"]))
        self.assertEqual(25,len(comments.LIBRARY["senior_old_pop"]))
        self.assertEqual(25,len(comments.LIBRARY["en_generic"]))
        store.set_comment_persona(self.profile,**comments.PERSONAS["jp_morning_chill"],path=self.db)
        profile=store.get_profile(self.profile,self.db)
        self.assertEqual("ja",profile["comment_language"])
        drafts=comments.suggest_drafts(profile,"朝のプレイリスト",profile["genre"],"リズムが心地よく朝に合っていました",3)
        self.assertEqual(3,len(drafts)); self.assertTrue(all("リズムが心地よく朝に合っていました" in item for item in drafts))
        with self.assertRaises(ValueError): comments.suggest_drafts(profile,"title",profile["genre"],"too short")

    def test_timer_is_random_range_only_and_does_not_track_playback(self):
        self.assertEqual(10,timers.random_analysis_minutes(lambda low,high:low))
        self.assertEqual(20,timers.random_analysis_minutes(lambda low,high:high))
        with self.assertRaises(ValueError): timers.random_analysis_minutes(lambda low,high:9)

    def test_youtube_search_page_token_and_50_result_cap_are_passed(self):
        page={"items":[],"nextPageToken":"next"}
        with patch("mcrs.youtube_api._get",return_value=page) as mocked:
            result=youtube_api.lookup_videos("mock","morning","JP",limit=80,page_token="p2",return_page=True)
        self.assertEqual([],result["items"]); self.assertEqual("next",result["nextPageToken"])
        args=mocked.call_args
        self.assertEqual("p2",args.kwargs["pageToken"])
        self.assertEqual(50,args.kwargs["maxResults"])
        self.assertEqual(0,args.kwargs["retries"])


if __name__ == "__main__":
    unittest.main()
