import sqlite3
import unittest
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

    def test_threshold_history_and_profile_isolation_are_preserved(self):
        second=store.create_profile("VPH KR profile","KR","CHILL_RAP",self.db)
        vph.set_thresholds(self.profile,120,60,self.db)
        vph.record_observation(self.profile,self.video,0,self.start,path=self.db)
        evaluated=vph.record_observation(self.profile,self.video,100,self.start+timedelta(hours=1),path=self.db)
        self.assertEqual("B",evaluated["tier"]); self.assertEqual(120,evaluated["tier_a_threshold"])
        vph.set_thresholds(self.profile,200,150,self.db)
        with sqlite3.connect(self.db) as db:
            saved=db.execute("SELECT tier_a_threshold,tier_b_threshold FROM vph_evaluations WHERE id=?",(evaluated["id"],)).fetchone()
        self.assertEqual((120,60),saved)
        self.assertEqual([],vph.list_radar(second,self.db))

    def test_readonly_adapter_uses_metadata_and_quota_context_without_playback(self):
        response={"items":[{"id":self.video,"statistics":{"viewCount":"321"},
            "contentDetails":{"duration":"PT2M"},"snippet":{"publishedAt":"2026-01-01T00:00:00Z","liveBroadcastContent":"none"}}]}
        context={"profile_id":self.profile,"path":str(self.db)}
        with patch("mcrs.vph.youtube_api._get",return_value=response) as request:
            result=vph.collect_video_samples("key",self.profile,[self.video],quota_context=context,
                observed_at_utc=self.start,path=self.db)
        self.assertEqual("UNKNOWN",result[0]["state"])
        self.assertEqual("videos",request.call_args.args[0]); self.assertEqual(context,request.call_args.kwargs["quota_context"])
        radar=vph.list_radar(self.profile,self.db)[0]
        self.assertEqual("short_candidate",radar["video_format"]); self.assertEqual(120,radar["duration_seconds"])


if __name__ == "__main__": unittest.main()
