import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mcrs import genres, store, vph, vph_research
from mcrs.youtube_api import APIError
from support import WorkspaceTemp


class VphResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp(); self.root = self.temp.__enter__(); self.db = self.root / "research.db"
        self.profile = store.create_profile("Research KR", "KR", "MORNING_CHILL_RAP", self.db)

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def _sample_first(self, key, profile_id, ids, **kwargs):
        when = datetime.now(timezone.utc)
        result = []
        for video_id in ids:
            result.append(vph.record_observation(profile_id, video_id, 100, when,
                metadata={"source": "offline-fixture"}, path=self.db))
        return result

    def _schedule(self, schedule_id, request_id=""):
        with store.connect(self.db) as db:
            db.execute("""INSERT INTO research_schedules(id,profile_id,market,core_genre,start_at_utc,
                total_minutes,research_minutes,break_minutes,approved_search_calls,state,phase,
                auto_start_request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (schedule_id,self.profile,"KR","MORNING_CHILL_RAP","2026-01-01T00:00:00+00:00",
                 480,20,20,3,"running","research",request_id))

    def _bind_first_sample(self, job, video_id, views=100, when=None, profile_id=None):
        profile_id=profile_id or self.profile
        result=vph.record_observation(profile_id,video_id,views,when or datetime.now(timezone.utc),path=self.db)
        if profile_id!=self.profile:
            return result
        with store.connect(self.db) as db:
            row=db.execute("SELECT targets_json FROM vph_research_jobs WHERE id=?",(job["id"],)).fetchone()
            targets=json.loads(row[0] or "[]")
            target=next((item for item in targets if item["video_id"]==video_id),None)
            if target is None:
                target={"video_id":video_id,"group":"CORE_FIT","positive_signals":[],
                        "conflict_signals":[],"genre_reason":"fixture","caution":""}
                targets.append(target)
            observation=db.execute("SELECT observed_at_utc,view_count,sample_status FROM vph_observations WHERE id=?",
                (result["later_observation_id"],)).fetchone()
            target.update(first_observation_id=result["later_observation_id"],first_job_id=job["id"],
                first_evaluation_id=result["id"],first_observed_at_utc=observation["observed_at_utc"],
                first_status="valid" if observation["sample_status"]=="valid" and observation["view_count"] is not None else "missing")
            db.execute("UPDATE vph_research_jobs SET targets_json=? WHERE id=?",
                       (json.dumps(targets),job["id"]))
        return result

    def test_zero_fit_uses_meaningful_review_but_not_zero_review_or_exclude(self):
        rows = [
            {"video_id":"rev12345678", "video_title":"Korean Chill Rap Neo-Soul Lo-fi mix",
             "description":"chill rap playlist"},
            {"video_id":"zero1234567", "video_title":"Korean playlist", "description":""},
            {"video_id":"excl1234567", "video_title":"Morning chill rap ASMR no rap", "description":""},
        ]
        targets = vph_research._meaningful_targets(store.get_profile(self.profile,self.db),rows,self.db)
        self.assertTrue(targets)
        self.assertTrue(all(item["group"]=="GENRE_UNCONFIRMED_REFERENCE" for item in targets))
        self.assertTrue(all(item["positive_signals"] for item in targets))
        self.assertTrue(all(item["caution"]=="REVIEW; not promoted to FIT" for item in targets))

    def test_bounded_search_dedup_first_sample_and_real_due_checkpoint(self):
        job = vph_research.start(self.profile,self.db,search_limit=2)
        calls=[]
        row={"video_id":"abc123DEF_-", "video_title":"Morning Chill Rap playlist",
             "channel_title":"Fixture channel", "description":"vocal performance and steady beat",
             "published_at":"2026-01-01T00:00:00Z", "views":0, "country":"KR", "keyword":"fixture"}
        def searcher(key,term,market,**kwargs):
            calls.append((term,market,kwargs["relevance_language"],kwargs["quota_context"]["session_id"]))
            return [row]
        with patch("mcrs.vph_research._profile_queries",return_value=["term-one","term-two","term-three"]), \
             patch("mcrs.vph_research.vph.collect_video_samples",side_effect=self._sample_first) as collect:
            result=vph_research.run_first_phase(job["id"],self.profile,"",access_token="mock-token",
                quota_context={"project_identity":"offline-test"},path=self.db,searcher=searcher)
        self.assertEqual(2,len(calls)); self.assertEqual("WAITING_SECOND",result["state"])
        self.assertEqual(["term-one","term-two"],result["used_queries"])
        self.assertEqual("CORE_FIT",result["targets"][0]["group"])
        self.assertEqual(1,collect.call_count); self.assertEqual(1,len(vph.list_history(self.profile,row["video_id"],self.db)))
        self.assertEqual(result["targets"][0]["first_job_id"],job["id"])
        self.assertIsNotNone(result["targets"][0]["first_observation_id"])
        self.assertEqual("ko",calls[0][2])
        due=datetime.fromisoformat(result["next_run_utc"])
        self.assertGreaterEqual((due-datetime.fromisoformat(result["targets"][0]["first_observed_at_utc"])).total_seconds(),3901)
        first_id=result["targets"][0]["first_observation_id"]
        paused=vph_research.pause(job["id"],self.profile,self.db)
        resumed=vph_research.resume(job["id"],self.profile,self.db)
        self.assertEqual("WAITING_SECOND",resumed["state"])
        self.assertEqual(first_id,resumed["targets"][0]["first_observation_id"])
        self.assertEqual([],vph_research.due_jobs(self.db,now=due-timedelta(seconds=1)))
        self.assertEqual(1,len(vph_research.due_jobs(self.db,now=due)))
        def second_sampler(key,profile_id,ids,**kwargs):
            return [vph.record_observation(profile_id,video_id,200,due,
                metadata={"source":"offline-fixture"},path=self.db) for video_id in ids]
        with self.assertRaisesRegex(RuntimeError,"Minimum real elapsed"):
            vph_research.run_second_phase(job["id"],self.profile,"",path=self.db,
                sampler=second_sampler,now=due-timedelta(seconds=1))
        completed=vph_research.run_second_phase(job["id"],self.profile,"",path=self.db,
            sampler=second_sampler,now=due)
        self.assertEqual("COMPLETED",completed["state"])
        radar=vph.list_radar(self.profile,self.db)
        self.assertEqual("VALID",radar[0]["state"])
        self.assertGreaterEqual(radar[0]["elapsed_seconds"],3900)
        self.assertGreater(radar[0]["vph"],92.0)
        self.assertLess(radar[0]["vph"],93.0)
        self.assertEqual("B",radar[0]["tier"])
        self.assertEqual(job["id"],completed["targets"][0]["first_job_id"])
        self.assertEqual(job["id"],completed["targets"][0]["second_job_id"])

    def test_successful_second_response_without_saved_observation_stays_review(self):
        self._schedule("schedule-empty-second")
        store.set_profile_vph_observation_slots(self.profile,1,self.db)
        store.save_candidates(self.profile,[{"video_id":"emptysec001","video_title":"Morning Chill Rap",
            "description":"vocal performance and steady beat"}],self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET genre_decision=?,genre_score=90 WHERE profile_id=? AND video_id=?",
                       (genres.FIT,self.profile,"emptysec001"))
        job=vph_research.start(self.profile,self.db,search_limit=0,observation_slots=1,
                               schedule_id="schedule-empty-second")
        with patch("mcrs.vph_research.vph.collect_video_samples",side_effect=self._sample_first):
            first=vph_research.run_first_phase(job["id"],self.profile,"",path=self.db,
                searcher=lambda *args,**kwargs:[])
        due=datetime.fromisoformat(first["next_run_utc"])
        result=vph_research.run_second_phase(job["id"],self.profile,"",path=self.db,
            sampler=lambda *args,**kwargs:[],now=due)
        self.assertEqual("REVIEW",result["state"])
        self.assertNotIn("second_observation_id",result["targets"][0])

    def test_first_sample_response_without_saved_observation_stays_review(self):
        self._schedule("schedule-empty-first")
        store.set_profile_vph_observation_slots(self.profile,1,self.db)
        store.save_candidates(self.profile,[{"video_id":"emptyfir001","video_title":"Morning Chill Rap",
            "description":"vocal performance and steady beat"}],self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET genre_decision=?,genre_score=90 WHERE profile_id=? AND video_id=?",
                       (genres.FIT,self.profile,"emptyfir001"))
        job=vph_research.start(self.profile,self.db,search_limit=0,observation_slots=1,
                               schedule_id="schedule-empty-first")
        with patch("mcrs.vph_research.vph.collect_video_samples",return_value=[]):
            result=vph_research.run_first_phase(job["id"],self.profile,"",path=self.db,
                searcher=lambda *args,**kwargs:[])
        self.assertEqual("REVIEW",result["state"])
        self.assertEqual(0,len(vph.list_history(self.profile,"emptyfir001",self.db)))
        self.assertFalse(result["targets"][0].get("first_observation_id"))

    def test_uncertain_request_is_checkpointed_and_never_replayed(self):
        job=vph_research.start(self.profile,self.db,search_limit=1)
        attempts=[]
        def uncertain(*args,**kwargs):
            attempts.append(1); raise APIError(code="network_changed",request_uncertain=True)
        with patch("mcrs.vph_research._profile_queries",return_value=["one query"]):
            with self.assertRaises(APIError):
                vph_research.run_first_phase(job["id"],self.profile,"",path=self.db,searcher=uncertain)
        saved=vph_research.get(job["id"],self.profile,self.db)
        self.assertEqual("REVIEW",saved["state"]); self.assertEqual(["one query"],saved["used_queries"])
        self.assertEqual([1],attempts)
        with self.assertRaises(RuntimeError):
            vph_research.run_first_phase(job["id"],self.profile,"",path=self.db,searcher=uncertain)

    def test_pause_requested_at_search_boundary_does_not_replay_completed_query(self):
        job=vph_research.start(self.profile,self.db,search_limit=2)
        def searcher(*args,**kwargs):return []
        with patch("mcrs.vph_research._profile_queries",return_value=["one","two"]):
            result=vph_research.run_first_phase(job["id"],self.profile,"",path=self.db,
                searcher=searcher,should_pause=lambda:True)
        self.assertEqual("PAUSED",result["state"])
        self.assertEqual([],result["used_queries"])
        resumed=vph_research.resume(job["id"],self.profile,self.db)
        self.assertEqual("READY",resumed["state"])

    def test_uncertain_first_sample_target_is_not_selected_by_a_new_job(self):
        video="abc123DEF_-"
        store.save_candidates(self.profile,[{"video_id":video,"video_title":"Morning Chill Rap playlist",
            "description":"vocal performance and steady beat","channel_title":"Saved","views":10}],self.db)
        job=vph_research.start(self.profile,self.db)
        with patch("mcrs.vph_research._profile_queries",return_value=[]), \
             patch("mcrs.vph_research.vph.collect_video_samples",side_effect=APIError(code="network_changed",request_uncertain=True)):
            with self.assertRaises(APIError):
                vph_research.run_first_phase(job["id"],self.profile,"",path=self.db)
        failed=vph_research.get(job["id"],self.profile,self.db)
        self.assertEqual("REVIEW",failed["state"])
        self.assertEqual(video,failed["targets"][0]["video_id"])
        next_job=vph_research.start(self.profile,self.db)
        with patch("mcrs.vph_research._profile_queries",return_value=[]), \
             patch("mcrs.vph_research.vph.collect_video_samples") as collect:
            result=vph_research.run_first_phase(next_job["id"],self.profile,"",path=self.db)
        self.assertEqual("REVIEW",result["state"])
        collect.assert_not_called()

    def test_existing_candidate_is_reused_without_rewrite_or_research(self):
        video="saved123456"
        store.save_candidates(self.profile,[{"video_id":video,"video_title":"Korean Chill Rap Neo-Soul Lo-fi mix",
            "description":"chill rap playlist","channel_title":"Saved","views":7,"keyword":"old query"}],self.db)
        before=store.get_candidate(self.profile,video,self.db)
        job=vph_research.start(self.profile,self.db,search_limit=1)
        with patch("mcrs.vph_research._profile_queries",return_value=[]), \
             patch("mcrs.vph_research.vph.collect_video_samples",side_effect=self._sample_first) as collect:
            result=vph_research.run_first_phase(job["id"],self.profile,"",path=self.db)
        after=store.get_candidate(self.profile,video,self.db)
        self.assertEqual(before,after); self.assertEqual(1,collect.call_count)
        self.assertEqual(video,result["targets"][0]["video_id"])
        self.assertEqual("GENRE_UNCONFIRMED_REFERENCE",result["targets"][0]["group"])

    def test_pause_resume_duplicate_start_and_profile_isolation(self):
        other=store.create_profile("Research JP","JP","CHILL_RAP",self.db)
        job=vph_research.start(self.profile,self.db)
        with self.assertRaises(RuntimeError):vph_research.start(self.profile,self.db)
        paused=vph_research.pause(job["id"],self.profile,self.db)
        self.assertEqual("PAUSED",paused["state"])
        self.assertEqual([],vph_research.due_jobs(self.db))
        resumed=vph_research.resume(job["id"],self.profile,self.db)
        self.assertEqual("READY",resumed["state"])
        self.assertIsNone(vph_research.latest_active(other,self.db))
        self.assertEqual("READY",vph_research.latest_active(self.profile,self.db)["state"])

    def test_profile_scoped_observation_slots_persist_and_validate(self):
        other=store.create_profile("Slots JP","JP","CHILL_RAP",self.db)
        self.assertEqual(10,store.get_profile(self.profile,self.db)["vph_observation_slots"])
        store.set_profile_vph_observation_slots(self.profile,3,self.db)
        self.assertEqual(3,store.get_profile(self.profile,self.db)["vph_observation_slots"])
        self.assertEqual(10,store.get_profile(other,self.db)["vph_observation_slots"])
        with self.assertRaises(ValueError):store.set_profile_vph_observation_slots(self.profile,0,self.db)
        with self.assertRaises(ValueError):store.set_profile_vph_observation_slots(self.profile,21,self.db)
        with self.assertRaises(ValueError):store.set_profile_vph_observation_slots(self.profile,True,self.db)
        self._schedule("sched-one")
        job=vph_research.start(self.profile,self.db,search_limit=0,schedule_id="sched-one")
        self.assertEqual(3,job["observation_slots"])
        self.assertEqual("sched-one",job["schedule_id"])
        self.assertEqual(0,job["search_limit"])

    def test_profile_schedule_preferences_and_presets_are_isolated(self):
        other=store.create_profile("Preferences JP","JP","CHILL_RAP",self.db)
        settings={"total_hours":10,"research_minutes":30,"break_minutes":15,"schedule_mode":"random",
            "research_min_minutes":20,"research_max_minutes":30,"break_min_minutes":10,"break_max_minutes":20}
        store.save_profile_research_preferences(self.profile,settings,self.db)
        store.save_research_schedule_preset(self.profile,"10h random",settings,self.db)
        self.assertEqual(settings,store.profile_research_preferences(self.profile,self.db))
        self.assertEqual(["10h random"],store.list_research_schedule_presets(self.profile,self.db))
        self.assertIsNone(store.get_research_schedule_preset(other,"10h random",self.db))
        self.assertEqual({},store.profile_research_preferences(other,self.db))
        with self.assertRaises(ValueError):store.save_profile_research_preferences(self.profile,{**settings,"total_hours":25},self.db)

    def test_additive_migration_keeps_legacy_profile_and_vph_job(self):
        legacy=self.root/"legacy-vph.db"
        db=sqlite3.connect(legacy)
        with db:
            db.execute("CREATE TABLE profiles(id TEXT PRIMARY KEY,name TEXT,market TEXT,genre TEXT,subgenre TEXT)")
            db.execute("INSERT INTO profiles VALUES('legacy','Legacy KR','KR','MORNING_CHILL_RAP','old subgenre')")
            db.execute("CREATE TABLE vph_research_jobs(id TEXT PRIMARY KEY,profile_id TEXT,state TEXT,"
                "search_limit INTEGER,search_calls_used INTEGER,used_queries_json TEXT,targets_json TEXT,"
                "next_run_utc TEXT,message TEXT,created_at TEXT,updated_at TEXT)")
            db.execute("INSERT INTO vph_research_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("old-job","legacy","WAITING_SECOND",3,1,'["query"]','[]',
                 "2026-01-01T00:00:00+00:00","kept","2026-01-01","2026-01-01"))
        db.close()
        store.initialize(legacy)
        profile=store.get_profile("legacy",legacy)
        migrated=vph_research.get("old-job","legacy",legacy)
        self.assertEqual("old subgenre",profile["subgenre"])
        self.assertEqual(10,profile["vph_observation_slots"])
        self.assertEqual("WAITING_SECOND",migrated["state"])
        self.assertEqual(["query"],migrated["used_queries"])
        self.assertEqual(10,migrated["observation_slots"])
        self.assertEqual("",migrated["schedule_id"])
        self.assertEqual("",migrated["request_id"])

    def test_schedule_request_job_identity_is_persisted_and_not_reused(self):
        request="request-one"
        self._schedule("schedule-one",request)
        with store.connect(self.db) as db:
            db.execute("INSERT INTO vph_auto_start_intents(request_id,profile_id,settings_json,status,schedule_id) "
                "VALUES(?,?,?,?,?)",(request,self.profile,"{}","started","schedule-one"))
        with self.assertRaisesRegex(ValueError,"does not match"):
            vph_research.start(self.profile,self.db,schedule_id="schedule-one",request_id="wrong-request")
        job=vph_research.start(self.profile,self.db,schedule_id="schedule-one",request_id=request)
        self.assertEqual((request,"schedule-one",self.profile),
            (job["request_id"],job["schedule_id"],job["profile_id"]))
        with self.assertRaises(RuntimeError):
            vph_research.start(self.profile,self.db,schedule_id="schedule-one",request_id=request)

    def test_saved_candidates_feed_one_batched_first_sample_without_search(self):
        store.set_profile_vph_observation_slots(self.profile,1,self.db)
        store.save_candidates(self.profile,[
            {"video_id":"fit12345678","video_title":"Morning Chill Rap playlist",
             "description":"vocal performance and steady beat","channel_title":"FIT"},
            {"video_id":"review12345","video_title":"Korean Chill Rap Neo-Soul Lo-fi mix",
             "description":"chill rap playlist","channel_title":"REVIEW"}],self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET genre_decision=?,genre_score=80 WHERE video_id=?",
                       (genres.FIT,"fit12345678"))
            db.execute("UPDATE candidates SET genre_decision=?,genre_score=10 WHERE video_id=?",
                       (genres.REVIEW,"review12345"))
        self._schedule("sched")
        job=vph_research.start(self.profile,self.db,search_limit=0,observation_slots=1,schedule_id="sched")
        def collect(key,profile_id,ids,**kwargs):
            self.assertEqual(["fit12345678"],ids)
            self.assertEqual("sched",kwargs["quota_context"]["schedule_id"])
            self.assertEqual(str(self.db),kwargs["quota_context"]["path"])
            return self._sample_first(key,profile_id,ids,**kwargs)
        with patch("mcrs.vph_research.lookup_videos",side_effect=AssertionError("search must not run")), \
             patch("mcrs.vph_research.vph.collect_video_samples",side_effect=collect) as sampled:
            result=vph_research.run_first_phase(job["id"],self.profile,"key",
                quota_context={"schedule_id":"sched"},path=self.db)
        self.assertEqual("WAITING_SECOND",result["state"])
        self.assertEqual(["fit12345678"],[item["video_id"] for item in result["targets"]])
        self.assertEqual("CORE_FIT",result["targets"][0]["group"])
        self.assertEqual(1,sampled.call_count)
        self.assertEqual(1,len(vph.list_history(self.profile,"fit12345678",self.db)))

    def test_schedule_window_and_end_preserve_unfinished_samples(self):
        self._schedule("sched")
        job=vph_research.start(self.profile,self.db,search_limit=0,schedule_id="sched")
        self.assertFalse(vph_research.schedule_window_allows_sample(job,None))
        self.assertFalse(vph_research.schedule_window_allows_sample(job,{"id":"sched","state":"running","phase":"break"}))
        self.assertFalse(vph_research.schedule_window_allows_sample(job,{"id":"sched","state":"paused","phase":"research"}))
        self.assertTrue(vph_research.schedule_window_allows_sample(job,{"id":"sched","state":"running","phase":"research"}))
        vph.record_observation(self.profile,"finish12345",50,datetime.now(timezone.utc),path=self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET state='WAITING_SECOND',targets_json='[]' WHERE id=?",(job["id"],))
        self.assertEqual(1,vph_research.finish_schedule_jobs(self.profile,"sched",self.db))
        self.assertEqual("REVIEW",vph_research.get(job["id"],self.profile,self.db)["state"])
        self.assertEqual(1,len(vph.list_history(self.profile,"finish12345",self.db)))

    def test_target_conflicts_are_per_video_not_last_loop_verdict(self):
        profile=store.get_profile(self.profile,self.db)
        rows=[{"video_id":"fit12345678","video_title":"Morning Chill Rap playlist",
               "description":"vocal performance and steady beat"},
              {"video_id":"excl1234567","video_title":"Morning chill rap ASMR no rap",
               "description":""}]
        targets=vph_research._meaningful_targets(profile,rows,self.db)
        self.assertEqual(1,len(targets))
        direct=genres.assess(profile["genre"],rows[0]["video_title"],rows[0]["description"],
                             store.profile_genre_config(profile)["custom_genres"])
        self.assertEqual(list(direct.conflict_signals),targets[0]["conflict_signals"])

    def test_restart_recovery_marks_inflight_request_review_without_replay(self):
        job=vph_research.start(self.profile,self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE vph_research_jobs SET state='COLLECTING_FIRST' WHERE id=?",(job["id"],))
        recovered=vph_research.mark_interrupted(job["id"],self.profile,self.db)
        self.assertEqual("REVIEW",recovered["state"])
        self.assertIn("불확실",recovered["message"])
        self.assertIsNone(vph_research.latest_active(self.profile,self.db))

    def test_metadata_diagnostic_names_distinct_evidence_gaps(self):
        profile=store.get_profile(self.profile,self.db)
        diag=vph_research.diagnose_candidate(profile,{"video_title":"Korean Chill Rap Neo-Soul Lo-fi",
            "description":"neo soul mix"})
        self.assertIn("metadata rules only",diag["basis"])
        self.assertIn("아침 맥락 근거 부족",diag["reasons"])
        self.assertIn("랩/보컬 근거 부족",diag["reasons"])
        self.assertIn("리듬 근거 부족",diag["reasons"])

    def test_missing_search_view_count_is_not_represented_as_known_zero(self):
        video="missi123456"
        store.save_candidates(self.profile,[{"video_id":video,"video_title":"Chill rap"}],self.db)
        saved=store.get_candidate(self.profile,video,self.db)
        self.assertEqual(0,saved["views"])
        self.assertEqual(0,saved["views_known"])

    def test_radar_keeps_fit_above_review_and_sample_group_in_history(self):
        fit,review="fit12345678","review12345"
        store.save_candidates(self.profile,[
            {"video_id":fit,"video_title":"Morning Chill Rap playlist","description":"vocal performance and steady beat"},
            {"video_id":review,"video_title":"Korean Chill Rap Neo-Soul Lo-fi mix","description":"chill rap playlist"}],self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET genre_decision=?,genre_score=1 WHERE video_id=?",(genres.FIT,fit))
            db.execute("UPDATE candidates SET genre_decision=?,genre_score=99 WHERE video_id=?",(genres.REVIEW,review))
        start=datetime.now(timezone.utc)
        for video,delta,group in ((fit,1,"CORE_FIT"),(review,500,"GENRE_UNCONFIRMED_REFERENCE")):
            vph.record_observation(self.profile,video,100,start,metadata={"research_group":group},path=self.db)
            vph.record_observation(self.profile,video,200+delta,start+timedelta(hours=1),
                metadata={"research_group":group},path=self.db)
        rows=vph.list_radar(self.profile,self.db)
        self.assertEqual([fit,review],[row["video_id"] for row in rows])
        self.assertEqual("CORE_FIT",json.loads(rows[0]["metadata_json"])["research_group"])
        self.assertEqual("GENRE_UNCONFIRMED_REFERENCE",json.loads(vph.list_history(self.profile,review,self.db)[-1]["metadata_json"])["research_group"])


if __name__ == "__main__": unittest.main()
