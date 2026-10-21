import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from mcrs import store, vph
from support import WorkspaceTemp


class VphRadarTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__(); self.db=self.root/"vph.db"
        self.profile=store.create_profile("VPH JP profile","JP","MORNING_CHILL_RAP",self.db)
        self.video="abc123DEF_-"
        self.start=datetime(2026,1,1,tzinfo=timezone.utc)

    def tearDown(self): self.temp.__exit__(None,None,None)

    def test_first_short_decrease_missing_stale_and_valid_are_not_zero_disguised(self):
        first=vph.record_observation(self.profile,self.video,100,self.start,path=self.db)
        self.assertEqual(("UNKNOWN","first_sample",None),(first["state"],first["reason"],first["vph"]))
        short=vph.record_observation(self.profile,self.video,120,self.start+timedelta(minutes=59),path=self.db)
        self.assertEqual(("UNKNOWN",None),(short["state"],short["vph"]))
        valid=vph.record_observation(self.profile,self.video,220,self.start+timedelta(minutes=119),path=self.db)
        self.assertEqual("VALID",valid["state"]); self.assertAlmostEqual(100.0,valid["vph"]); self.assertEqual("A",valid["tier"])
        down=vph.record_observation(self.profile,self.video,219,self.start+timedelta(minutes=179),path=self.db)
        self.assertEqual(("REVIEW",None),(down["state"],down["vph"]))
        missing=vph.record_observation(self.profile,self.video,None,self.start+timedelta(minutes=239),path=self.db)
        self.assertEqual(("REVIEW",None),(missing["state"],missing["vph"]))
        other="other12345_"
        vph.record_observation(self.profile,other,1,self.start,path=self.db)
        stale=vph.record_observation(self.profile,other,100,self.start+timedelta(hours=49),path=self.db)
        self.assertEqual(("REVIEW","stale_sample_pair",None),(stale["state"],stale["reason"],stale["vph"]))

    def test_duplicate_timestamp_and_out_of_order_samples_are_review_not_velocity(self):
        first=vph.record_observation(self.profile,self.video,100,self.start,path=self.db)
        duplicate=vph.record_observation(self.profile,self.video,100,self.start,path=self.db)
        self.assertEqual("UNKNOWN",first["state"])
        self.assertEqual(first["id"],duplicate["id"])
        self.assertEqual(1,len(vph.list_history(self.profile,self.video,self.db)))
        with self.assertRaisesRegex(ValueError,"Conflicting cumulative views"):
            vph.record_observation(self.profile,self.video,101,self.start,path=self.db)
        normal=vph.record_observation(self.profile,self.video,200,self.start+timedelta(hours=1),path=self.db)
        self.assertEqual("VALID",normal["state"])
        reversed_sample=vph.record_observation(self.profile,self.video,150,self.start+timedelta(minutes=30),path=self.db)
        self.assertEqual(("REVIEW","out_of_order_observation",None),
                         (reversed_sample["state"],reversed_sample["reason"],reversed_sample["vph"]))

    def test_threshold_history_and_profile_isolation_are_preserved(self):
        second=store.create_profile("VPH KR profile","KR","CHILL_RAP",self.db)
        vph.set_thresholds(self.profile,120,60,self.db)
        vph.record_observation(self.profile,self.video,0,self.start,path=self.db)
        evaluated=vph.record_observation(self.profile,self.video,100,self.start+timedelta(hours=1),path=self.db)
        self.assertEqual("B",evaluated["tier"]); self.assertEqual(120,evaluated["tier_a_threshold"])
        vph.set_thresholds(self.profile,200,150,self.db)
        with closing(sqlite3.connect(self.db)) as db:
            saved=db.execute("SELECT tier_a_threshold,tier_b_threshold FROM vph_evaluations WHERE id=?",(evaluated["id"],)).fetchone()
        self.assertEqual((120,60),saved)
        self.assertEqual([],vph.list_radar(second,self.db))
        store.initialize(self.db)
        reloaded=store.get_profile(self.profile,self.db)
        self.assertEqual((200,150),(reloaded["vph_tier_a"],reloaded["vph_tier_b"]))
        for invalid in ((50,50),(float("nan"),25),(100,float("inf")),(100,-1)):
            with self.assertRaises(ValueError):vph.set_thresholds(self.profile,*invalid,path=self.db)

    def test_readonly_adapter_uses_metadata_and_quota_context_without_playback(self):
        response={"items":[{"id":self.video,"statistics":{"viewCount":"321"},
            "contentDetails":{"duration":"PT2M"},"snippet":{"publishedAt":"2026-01-01T00:00:00Z","liveBroadcastContent":"none"}}]}
        context={"profile_id":self.profile,"path":str(self.db)}
        with patch("mcrs.vph.youtube_api._get",return_value=response) as request:
            result=vph.collect_video_samples("key",self.profile,[self.video],quota_context=context,
                observed_at_utc=self.start,path=self.db,
                target_metadata={self.video:{"research_group":"GENRE_UNCONFIRMED_REFERENCE"}})
        self.assertEqual("UNKNOWN",result[0]["state"])
        self.assertEqual("videos",request.call_args.args[0]); self.assertEqual(context,request.call_args.kwargs["quota_context"])
        radar=vph.list_radar(self.profile,self.db)[0]
        self.assertEqual("short_candidate",radar["video_format"]); self.assertEqual(120,radar["duration_seconds"])
        self.assertEqual("GENRE_UNCONFIRMED_REFERENCE",json.loads(radar["metadata_json"])["research_group"])


    def test_radar_deduplicates_to_latest_and_genre_fit_precedes_vph(self):
        second_video="other12345_"
        store.save_candidates(self.profile,[
            {"video_id":self.video,"video_title":"Morning Chill Rap instrumental","channel_title":"Low fit","views":20},
            {"video_id":second_video,"video_title":"Morning Chill Rap study mix","channel_title":"High fit","views":20}],self.db)
        for video,base,step in ((self.video,0,1000),(second_video,0,10)):
            vph.record_observation(self.profile,video,base,self.start,path=self.db)
            vph.record_observation(self.profile,video,base+step,self.start+timedelta(hours=1),path=self.db)
        vph.record_observation(self.profile,self.video,2000,self.start+timedelta(hours=2),path=self.db)
        with store.connect(self.db) as db:
            db.execute("UPDATE candidates SET genre_score=10 WHERE video_id=?",(self.video,))
            db.execute("UPDATE candidates SET genre_score=90 WHERE video_id=?",(second_video,))
        rows=vph.list_radar(self.profile,self.db)
        self.assertEqual([second_video,self.video],[row["video_id"] for row in rows])
        self.assertEqual(2,len(rows))
        self.assertEqual(2000,rows[1]["view_count"])
        history=vph.list_history(self.profile,self.video,self.db)
        self.assertEqual(3,len(history))
        self.assertEqual([0,1000,2000],[row["view_count"] for row in history])


if __name__ == "__main__": unittest.main()
