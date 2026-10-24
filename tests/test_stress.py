import json, os, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from mcrs import oauth, store
from mcrs.genres import assess
from support import WorkspaceTemp

class StressAndCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__(); self.db=self.root/"한글 日本語 space 데이터.db"
    def tearDown(self): self.temp.__exit__(None,None,None)

    def test_metadata_regression_fixture(self):
        fixture=Path(__file__).parent/"fixtures"/"genre_metadata.json"
        for case in json.loads(fixture.read_text(encoding="utf8")):
            with self.subTest(case=case): self.assertEqual(case["expected"],assess(case["genre"],case["title"]).decision)

    def test_twenty_profiles_keep_candidates_metrics_and_terms_isolated(self):
        profiles=[]
        for index in range(20):
            market="JP" if index%2==0 else "KR"
            genre="MORNING_CHILL_RAP" if market=="JP" else "CHILL_RAP"
            profile=store.create_profile(f"채널 日本 {index:02d}",market,genre,self.db,subgenre="여성 보컬")
            profiles.append(profile)
            store.set_profile_search_terms(profile,market,[f"독립 검색어 {index}"],self.db)
            video_id=f"V{index:010d}"
            title="朝 チルラップ" if market=="JP" else "감성 힙합 칠랩"
            store.save_candidates(profile,[{"video_id":video_id,"video_title":title}],self.db)
            store.upsert_metric(profile,"2026-10-17",index,index,100,1.0,2.0,index,self.db)
        for index,profile in enumerate(profiles):
            self.assertEqual(1,len(store.list_candidates(profile,path=self.db)))
            self.assertEqual([f"독립 검색어 {index}"],store.list_profile_search_terms(profile,"JP" if index%2==0 else "KR",self.db))
            self.assertEqual(index,store.list_metrics(profile,self.db)[0]["all_views"])

    def test_concurrent_profile_reads(self):
        profiles=[store.create_profile(f"동시 읽기 {index}","KR","CHILL_RAP",self.db) for index in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda profile: store.get_profile(profile,self.db)["id"],profiles))
        self.assertEqual(set(profiles),set(results))

    def test_mock_tokens_survive_restart_and_never_cross_profiles(self):
        previous=os.environ.get("ZISU_DATA_DIR"); os.environ["ZISU_DATA_DIR"]=str(self.root)
        try:
            first=store.create_profile("OAuth JP","JP","MORNING_CHILL_RAP")
            second=store.create_profile("OAuth KR","KR","CHILL_RAP")
            payload=lambda token: json.dumps({"token":token,"refresh_token":"refresh","client_id":"test","scopes":oauth.SCOPES})
            oauth.save_connection(first,{"channel_id":"UCfirst","channel_title":"First","email":"first@example.test"},payload("first-secret"))
            oauth.save_connection(second,{"channel_id":"UCsecond","channel_title":"Second","email":"second@example.test"},payload("second-secret"))
            self.assertEqual("connected",oauth.connection_state(first)["status"])
            self.assertEqual("connected",oauth.connection_state(second)["status"])
            self.assertNotEqual(oauth.token_path(first).read_bytes(),oauth.token_path(second).read_bytes())
            oauth.disconnect(first)
            self.assertFalse(oauth.is_connected(first)); self.assertTrue(oauth.is_connected(second))
        finally:
            if previous is None: os.environ.pop("ZISU_DATA_DIR",None)
            else: os.environ["ZISU_DATA_DIR"]=previous

    def test_repeated_migration_is_idempotent(self):
        profile=store.create_profile("반복 이전","JP","MORNING_CHILL_RAP",self.db)
        for _ in range(10): store.initialize(self.db)
        self.assertEqual(profile,store.get_profile(profile,self.db)["id"])

if __name__ == "__main__": unittest.main()
