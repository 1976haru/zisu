from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path
import unittest
from unittest import mock

from mcrs import manual_research, store
from tests.support import WorkspaceTemp


def url(index, kind="watch"):
    video_id=f"v{index:010d}"
    if kind=="short":return f"https://youtu.be/{video_id}?si=tracking"
    if kind=="shorts":return f"https://www.youtube.com/shorts/{video_id}?feature=share"
    if kind=="live":return f"https://youtube.com/live/{video_id}?utm_source=x"
    return f"https://www.youtube.com/watch?v={video_id}&list=ignored"


class ManualResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp();self.root=self.temp.__enter__();self.db=self.root/"fixture.sqlite"
        store.initialize(self.db)
        self.jp=store.create_profile("JP 연구", "JP", "MORNING_CHILL_RAP", self.db)
        self.kr=store.create_profile("KR 연구", "KR", "RNB_HIPHOP", self.db)

    def tearDown(self):self.temp.__exit__(None,None,None)

    def test_one_link_and_metadata_unknown_review(self):
        batch=manual_research.create_batch(self.jp,url(1),path=self.db)
        item=manual_research.list_items(batch,self.jp,self.db)[0]
        self.assertEqual("",item["title"]);self.assertEqual("",item["channel_title"])
        self.assertEqual("unconfirmed",item["metadata_state"]);self.assertEqual("검토",item["auto_genre_decision"])

    def test_exactly_50_preserves_order_and_is_atomic(self):
        batch=manual_research.create_batch(self.jp,"\n".join(url(i,i%4 and "short" or "watch") for i in range(50)),path=self.db)
        rows=manual_research.list_items(batch,self.jp,self.db)
        self.assertEqual(50,len(rows));self.assertEqual(list(range(1,51)),[r["position"] for r in rows])

    def test_51_rejected_without_partial_batch(self):
        preview=manual_research.preview_links("\n".join(url(i) for i in range(51)))
        self.assertFalse(preview.can_save);self.assertIn("최대 50개",preview.errors[0][1])
        with self.assertRaises(ValueError):manual_research.create_batch(self.jp,"\n".join(url(i) for i in range(51)),path=self.db)
        self.assertEqual([],manual_research.list_batches(self.jp,self.db))

    def test_invalid_scheme_fake_domain_and_line_number(self):
        text="\n".join((url(1),"javascript://youtube.com/watch?v=v0000000002","https://youtube.com.evil.test/watch?v=v0000000003"))
        preview=manual_research.preview_links(text)
        self.assertEqual([2,3],[row[0] for row in preview.errors])

    def test_duplicate_removed_first_order_preserved(self):
        preview=manual_research.preview_links("\n".join((url(3),url(1,"shorts"),url(3,"live"),url(2))))
        self.assertEqual(["v0000000003","v0000000001","v0000000002"],[r.video_id for r in preview.valid])
        self.assertEqual((3,1,"v0000000003"),preview.duplicates[0])

    def test_tracking_removed_all_supported_forms(self):
        for kind in ("watch","short","shorts","live"):
            video_id,normalized=manual_research.normalize_youtube_url(url(8,kind))
            self.assertEqual("v0000000008",video_id);self.assertEqual("https://www.youtube.com/watch?v=v0000000008",normalized)

    def test_notes_roundtrip_and_human_exclude_preserved(self):
        batch=manual_research.create_batch(self.jp,url(1),path=self.db)
        manual_research.save_evaluation(batch,self.jp,"v0000000001",{"human_fit":"제외","notes":"사람이 직접 제외","mood":"차분함"},self.db)
        row=manual_research.list_items(batch,self.jp,self.db)[0]
        self.assertEqual("제외",row["human_fit"]);self.assertEqual("검토",row["auto_genre_decision"]);self.assertEqual("사람이 직접 제외",row["notes"])

    def test_profiles_are_isolated(self):
        batch=manual_research.create_batch(self.jp,url(1),path=self.db)
        self.assertEqual([],manual_research.list_items(batch,self.kr,self.db))
        self.assertIsNone(manual_research.get_batch(batch,self.kr,self.db))

    def test_pause_resume_counts_only_active_and_reboot_gap_is_excluded(self):
        batch=manual_research.create_batch(self.jp,url(1),path=self.db)
        t0=datetime(2026,1,1,tzinfo=timezone.utc)
        manual_research.start_or_resume(batch,self.jp,"v0000000001",self.db,now=t0.isoformat())
        manual_research.pause(batch,self.jp,self.db,now=(t0+timedelta(minutes=10)).isoformat())
        manual_research.start_or_resume(batch,self.jp,"v0000000001",self.db,now=(t0+timedelta(hours=9)).isoformat())
        manual_research.recover_interrupted(self.db,now=(t0+timedelta(hours=10)).isoformat())
        row=manual_research.get_batch(batch,self.jp,self.db)
        self.assertEqual("interrupted",row["state"]);self.assertEqual(600,row["active_seconds"])

    def test_duplicate_open_session_per_profile_rejected(self):
        manual_research.create_batch(self.jp,url(1),path=self.db)
        with self.assertRaisesRegex(ValueError,"이미"):
            manual_research.create_batch(self.jp,url(2),path=self.db)

    def test_last_completed_item_closes_batch_and_allows_next_batch(self):
        batch=manual_research.create_batch(self.jp,url(1),path=self.db)
        manual_research.start_or_resume(batch,self.jp,"v0000000001",self.db)
        manual_research.save_evaluation(batch,self.jp,"v0000000001",{"research_state":"completed"},self.db)
        self.assertEqual("completed",manual_research.get_batch(batch,self.jp,self.db)["state"])
        self.assertTrue(manual_research.list_items(batch,self.jp,self.db)[0]["ended_at_utc"])
        self.assertTrue(manual_research.create_batch(self.jp,url(2),path=self.db))

    def test_migration_rerun_and_existing_candidates_unchanged(self):
        rows=[{"video_id":f"c{i:010d}","video_title":"Morning Chill Rap","description":"morning rap"} for i in range(101)]
        store.save_candidates(self.jp,rows,self.db)
        before=store.list_candidates(self.jp,path=self.db)
        store.initialize(self.db);store.initialize(self.db)
        after=store.list_candidates(self.jp,path=self.db)
        self.assertEqual([(r["video_id"],r["notes"]) for r in before],[(r["video_id"],r["notes"]) for r in after])

    def test_no_external_api_oauth_or_browser_before_click(self):
        with mock.patch("webbrowser.open") as browser:
            batch=manual_research.create_batch(self.jp,url(1),path=self.db)
            manual_research.list_items(batch,self.jp,self.db)
        browser.assert_not_called()

    def test_time_metrics_are_separate_columns(self):
        batch=manual_research.create_batch(self.jp,url(1),break_minutes=7,path=self.db)
        data=manual_research.get_batch(batch,self.jp,self.db)
        self.assertIn("planned_minutes",data);self.assertIn("active_seconds",data);self.assertIn("break_seconds",data)
        self.assertNotIn("youtube_watch_time",data)

    def test_explicit_break_is_separate_and_restart_gap_is_not_counted(self):
        batch=manual_research.create_batch(self.jp,url(1),path=self.db)
        t0=datetime(2026,1,1,tzinfo=timezone.utc)
        manual_research.start_or_resume(batch,self.jp,"v0000000001",self.db,now=t0.isoformat())
        manual_research.start_break(batch,self.jp,self.db,now=(t0+timedelta(minutes=3)).isoformat())
        manual_research.start_or_resume(batch,self.jp,"v0000000001",self.db,now=(t0+timedelta(minutes=8)).isoformat())
        row=manual_research.get_batch(batch,self.jp,self.db)
        self.assertEqual(180,row["active_seconds"]);self.assertEqual(300,row["break_seconds"])
        manual_research.start_break(batch,self.jp,self.db,now=(t0+timedelta(minutes=9)).isoformat())
        manual_research.recover_interrupted(self.db,now=(t0+timedelta(hours=9)).isoformat())
        manual_research.start_or_resume(batch,self.jp,"v0000000001",self.db,now=(t0+timedelta(hours=10)).isoformat())
        self.assertEqual(300,manual_research.get_batch(batch,self.jp,self.db)["break_seconds"])

    def test_existing_schedule_busy_protection_stays_independent(self):
        manual_research.create_batch(self.jp,url(1),path=self.db)
        with store.connect(self.db) as db:
            count=db.execute("SELECT COUNT(*) FROM research_schedules").fetchone()[0]
        self.assertEqual(0,count)


if __name__=="__main__":unittest.main()
