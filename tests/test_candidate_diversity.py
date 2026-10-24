import os
import unittest
from datetime import datetime, timezone

from mcrs import ai_research, autopilot, candidate_diversity, genres, research_scheduler, store
from tests.support import WorkspaceTemp


class CandidateDiversityTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp();self.root=self.temp.__enter__()
        self.db=self.root/"diversity.sqlite"
        self.profile=store.create_profile("Diversity JP","JP","MORNING_CHILL_RAP",self.db,
            related_genres=["CHILL_RAP","CAFE_CHILL"])

    def tearDown(self):self.temp.__exit__(None,None,None)

    def fit(self,video_id,channel_id,title=None,views=1000,published="2026-10-01T00:00:00Z"):
        return {"video_id":video_id,"video_title":title or "Morning Chill Rap mellow vocal groove",
            "description":"melodic rap and soft vocals","channel_id":channel_id,
            "channel_title":channel_id,"views":views,"published_at":published}

    def test_query_categories_keep_user_core_related_and_japanese_text_is_not_evidence(self):
        store.set_profile_search_terms(self.profile,"JP",["手動の調査語"],self.db)
        profile=store.get_profile(self.profile,self.db)
        self.assertEqual("user_core",candidate_diversity.classify_search_category("手動の調査語",profile,self.db))
        related=genres.search_terms("CHILL_RAP","JP")[0]
        self.assertEqual("related",candidate_diversity.classify_search_category(related,profile,self.db))
        self.assertEqual("검토",genres.assess(self._genre(),"朝のプレイリスト","落ち着いた雰囲気").decision)

    def _genre(self):return store.get_profile(self.profile,self.db)["genre"]

    def test_page_diversification_is_deterministic_genre_first_and_channel_fair(self):
        rows=[self.fit("vfit0000001","popular",views=10000000),
              self.fit("vfit0000002","popular",views=9000000),
              self.fit("vfit0000003","small",views=20,published="2026-10-02T00:00:00Z"),
              {"video_id":"vreview00001","channel_id":"new","video_title":"朝のプレイリスト","description":"落ち着いた雰囲気"},
              {"video_id":"vexclu000001","channel_id":"jazz","video_title":"Jazz instrumental cafe BGM","description":""},
              self.fit("vfit0000001","duplicate")]
        exposures={"popular":5,"small":0,"new":0}
        first,duplicates=candidate_diversity.diversify_page(rows,genre_id=self._genre(),exposure_counts=exposures,seed="fixed")
        second,_=candidate_diversity.diversify_page(rows,genre_id=self._genre(),exposure_counts=exposures,seed="fixed")
        self.assertEqual(1,duplicates)
        self.assertEqual([row["video_id"] for row in first],[row["video_id"] for row in second])
        decisions=[genres.assess(self._genre(),row["video_title"],row.get("description","")).decision for row in first]
        self.assertEqual(sorted(decisions,key={genres.FIT:0,genres.REVIEW:1,genres.EXCLUDE:2}.get),decisions)
        self.assertEqual("small",first[0]["channel_id"])

    def test_channel_exposure_reduces_repeated_source_order_dominance(self):
        rows=[self.fit(f"div{i:08d}","alpha" if i<8 else ("beta" if i<10 else "gamma"),
                       published="2026-10-02T00:00:00Z") for i in range(12)]
        before=[row["channel_id"] for row in rows[:8]]
        after,_=candidate_diversity.diversify_page(rows,genre_id=self._genre(),seed="repeatable")
        after=after[:8]
        counts={channel:sum(row["channel_id"]==channel for row in after)
                for channel in {row["channel_id"] for row in rows}}
        self.assertEqual(1,len(set(before)))
        # With only four non-alpha rows available inside the input page,
        # balanced ordering halves alpha's baseline dominance (8/8 to 4/8).
        self.assertLessEqual(max(counts.values()),4)
        self.assertGreaterEqual(len({row["channel_id"] for row in after}),3)

    def test_autopilot_deduplicates_previous_video_and_records_discovery_provenance_per_profile(self):
        previous=self.fit("previous001","old-channel")
        store.save_candidates(self.profile,[previous],self.db)
        rows=[previous,self.fit("fresh000001","channel-a"),self.fit("fresh000002","channel-b")]
        outcome=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(self.profile,target=5,max_search_calls=1),
            lambda *_args:{"items":rows,"nextPageToken":""})
        self.assertEqual(1,outcome.run.duplicate_count)
        with store.connect(self.db) as db:
            found=db.execute("SELECT video_id,channel_id,search_category,search_order,rule_verdict,ai_source "
                "FROM research_discoveries WHERE profile_id=? ORDER BY video_id",(self.profile,)).fetchall()
        self.assertEqual({"fresh000001","fresh000002"},{row["video_id"] for row in found})
        self.assertTrue(all(row["search_order"] in {"relevance","date"} for row in found))
        self.assertTrue(all(row["ai_source"]=="rules_only" for row in found))
        summary=store.dashboard_summary(self.profile,self.db)
        self.assertEqual(1,summary["duplicate_candidates_total"])
        self.assertEqual(2,len(found))
        other=store.create_profile("Other JP", "JP", "MORNING_CHILL_RAP", self.db)
        other_result=autopilot.AutopilotController(self.db).run(
            autopilot.AutopilotConfig(other,target=5,max_search_calls=1),
            lambda *_args:{"items":[previous],"nextPageToken":""})
        self.assertEqual(0,other_result.run.duplicate_count)
        self.assertEqual(3,len(store.known_research_video_ids(self.profile,["previous001","fresh000001","fresh000002"],self.db)))
        self.assertEqual(1,len(store.known_research_video_ids(other,["previous001"],self.db)))

    def test_scheduled_autopilot_uses_relevance_then_bounded_recent_order(self):
        now=datetime(2026,10,18,9,0,tzinfo=timezone.utc)
        schedule=research_scheduler.create(research_scheduler.ScheduleConfig(
            profile_id=self.profile,total_hours=2,research_minutes=20,break_minutes=15,
            approved_search_calls=4),self.db,now=now)
        orders=[];counter=iter(range(4))
        def collector(term,market,limit,page_token=None,order="relevance"):
            orders.append(order);index=next(counter)
            return {"items":[self.fit(f"s{index:010d}",f"channel-{index}")],"nextPageToken":""}
        config=autopilot.AutopilotConfig(self.profile,target=5,max_search_calls=4)
        result=research_scheduler.run_autopilot_block(schedule["id"],self.profile,
            autopilot.AutopilotController(self.db),config,collector,self.db,
            now=lambda:now)
        self.assertEqual("paused" if result.state=="paused" else "complete",result.state)
        self.assertEqual(4,len(orders))
        self.assertEqual("date",orders[2])
        self.assertEqual(4,research_scheduler.get(schedule["id"],self.profile,self.db,now=now)["search_calls_used"])

    def test_youtube_adapter_passes_bounded_order_to_read_only_search(self):
        from mcrs import youtube_api
        from unittest.mock import patch
        with patch("mcrs.youtube_api._get",return_value={"items":[]}) as request:
            youtube_api.lookup_videos("mock","morning","JP",limit=50,return_page=True,order="date")
        self.assertEqual("date",request.call_args.kwargs["order"])
        with self.assertRaises(ValueError):
            youtube_api.lookup_videos("mock","morning","JP",order="viewCount")


if __name__=="__main__":unittest.main()
