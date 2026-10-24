import csv, unittest
from mcrs import store
from mcrs.genres import EXCLUDE, FIT, REVIEW, GENRES, LEGACY_GENRE_IDS, assess, search_terms
from mcrs.gui import extract_video_id
from mcrs.youtube_api import lookup_videos
from support import WorkspaceTemp
from unittest.mock import patch

class GenreTests(unittest.TestCase):
    def test_six_genres_and_local_queries(self):
        self.assertEqual(6,len(LEGACY_GENRE_IDS))
        for genre_id in LEGACY_GENRE_IDS:
            self.assertGreaterEqual(len(search_terms(genre_id,"JP")),10)
            self.assertGreaterEqual(len(search_terms(genre_id,"KR")),10)
    def test_market_queries_are_independent(self):
        for genre_id in LEGACY_GENRE_IDS: self.assertTrue(set(search_terms(genre_id,"JP")).isdisjoint(search_terms(genre_id,"KR")))
    def test_morning_needs_time_and_genre(self):
        self.assertEqual(REVIEW,assess("MORNING_CHILL_RAP","아침 Morning Chill Rap 플레이리스트").decision)
        self.assertEqual(REVIEW,assess("MORNING_CHILL_RAP","Chill Rap playlist").decision)
        self.assertEqual(REVIEW,assess("MORNING_CHILL_RAP","아침 음악 모음").decision)
    def test_morning_excludes_wrong_genres(self):
        for title in ("아침 카페 재즈","수면용 Lo-Fi","ASMR 빗소리","클래식 BGM"):
            self.assertEqual(EXCLUDE,assess("MORNING_CHILL_RAP",title).decision,title)
    def test_keyword_does_not_promote(self): self.assertEqual(REVIEW,assess("CHILL_RAP","Morning Flowers","No genre evidence").decision)
    def test_rap_fit_requires_genre_title_and_vocal_rhythm_description_support(self):
        self.assertEqual(REVIEW, assess("CHILL_RAP", "Chill Rap playlist", "").decision)
        self.assertEqual(REVIEW, assess("CHILL_RAP", "Chill Rap playlist", "Relaxed vocal performance.").decision)
        supported = assess("CHILL_RAP", "Chill Rap playlist",
                           "Relaxed vocal performance over a clear beat and hip-hop rhythm.")
        self.assertEqual(FIT, supported.decision)
        self.assertIn("vocal: vocal", supported.positive_signals)
        self.assertIn("rhythm: beat", supported.positive_signals)
        self.assertIn("아님", supported.reason)
    def test_morning_core_allows_compatible_rnb_only_with_context_and_two_support_groups(self):
        title = "모닝 칠랩 R&B vocal mix"
        weak = assess("MORNING_CHILL_RAP", title,
                      "Melodic rap and neo soul vocals centered in a morning playlist.")
        self.assertEqual(REVIEW, weak.decision)
        strong = assess("MORNING_CHILL_RAP", title,
                        "Melodic rap and neo soul vocals in a morning playlist with a clear beat.")
        self.assertEqual(FIT, strong.decision)
        self.assertFalse(strong.conflict_signals)
        self.assertIn("vocal: vocal", strong.positive_signals)
    def test_explicit_absence_of_rap_or_vocals_is_excluded_for_both_rap_genres(self):
        cases = (
            ("MORNING_CHILL_RAP", "Morning Chill Rap", "No vocals, instrumental beat only."),
            ("MORNING_CHILL_RAP", "朝のChill Rap", "ボーカルなし、ラップなし。"),
            ("CHILL_RAP", "Chill Rap playlist", "ボーカルなし。"),
            ("CHILL_RAP", "Chill Rap playlist", "No rap, vocals absent."),
        )
        for genre_id, title, description in cases:
            with self.subTest(genre=genre_id, title=title, description=description):
                self.assertEqual(EXCLUDE, assess(genre_id, title, description).decision)
    def test_cross_genre_is_review(self): self.assertEqual(REVIEW,assess("CHILL_RAP","감성 힙합 R&B 힙합 플레이리스트").decision)
    def test_japanese_signals(self): self.assertEqual(REVIEW,assess("MORNING_CHILL_RAP","朝 チルラップ プレイリスト").decision)
    def test_verdict_exposes_structured_evidence(self):
        verdict=assess("MORNING_CHILL_RAP","아침 melodic rap playlist")
        self.assertEqual(REVIEW,verdict.decision)
        self.assertIn("melodic rap",verdict.positive_signals)
        self.assertEqual((),verdict.conflict_signals)
    def test_instrumental_morning_is_never_fit(self):
        for title in ("아침 무보컬 카페 BGM","Morning Chill Rap instrumental only","朝 アンビエント チルラップ"):
            self.assertNotEqual(FIT,assess("MORNING_CHILL_RAP",title).decision,title)
    def test_short_latin_boundaries(self): self.assertEqual(REVIEW,assess("OLD_POP","Popular new music").decision)
    def test_invalid_market(self):
        with self.assertRaises(ValueError): search_terms("CHILL_RAP","US")
    def test_url_restrictions(self):
        self.assertEqual("aBcDeF12345",extract_video_id("https://youtu.be/aBcDeF12345"))
        self.assertEqual("",extract_video_id("https://evil.test/watch?v=aBcDeF12345"))

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__(); self.db=self.root/"test.db"
        self.jp=store.create_profile("일본 모닝 조사","JP","MORNING_CHILL_RAP",self.db)
        self.kr=store.create_profile("한국 칠랩 조사","KR","CHILL_RAP",self.db)
    def tearDown(self): self.temp.__exit__(None,None,None)
    def video(self,video_id="aBcDeF12345"):
        return {"video_id":video_id,"video_title":"아침 Morning Chill Rap","description":"vocal performance over a steady beat","channel_title":"Example"}
    def test_profile_isolation_same_video(self):
        store.save_candidates(self.jp,[self.video()],self.db); store.save_candidates(self.kr,[self.video()],self.db)
        store.update_candidate(self.jp,"aBcDeF12345","완료","일본 메모","",self.db)
        self.assertEqual("",store.get_candidate(self.kr,"aBcDeF12345",self.db)["notes"])
    def test_excluded_candidate_not_saved(self):
        row=self.video(); row["video_title"]="아침 카페 재즈"
        self.assertEqual(1,store.save_candidates(self.jp,[row],self.db)[EXCLUDE]); self.assertEqual([],store.list_candidates(self.jp,path=self.db))
    def test_manual_approval_requires_reason(self):
        row=self.video(); row["video_title"]="알 수 없는 플레이리스트"; store.save_candidates(self.jp,[row],self.db)
        with self.assertRaises(ValueError): store.approve_review(self.jp,"aBcDeF12345","짧음",self.db)
        store.approve_review(self.jp,"aBcDeF12345","직접 감상했고 아침 칠랩 기준에 맞는 충분한 근거가 있습니다.",self.db)
        self.assertEqual("수동 승인",store.get_candidate(self.jp,"aBcDeF12345",self.db)["genre_decision"])
    def test_profile_genre_locked_after_candidate(self):
        store.save_candidates(self.jp,[self.video()],self.db)
        with self.assertRaises(ValueError): store.update_profile(self.jp,name="변경 이름",market="KR",genre="CHILL_RAP",path=self.db)
    def test_csv_only_current_profile(self):
        store.save_candidates(self.jp,[self.video()],self.db); store.save_candidates(self.kr,[self.video("bBcDeF12345")],self.db)
        target=self.root/"out.csv"; self.assertEqual(1,store.export_csv(self.jp,"candidates",target,self.db))
        with target.open(encoding="utf-8-sig") as stream: self.assertEqual(2,len(list(csv.reader(stream))))
    def test_backup(self):
        target=self.root/"backup.db"; store.backup(target,self.db); self.assertEqual(2,len(store.list_profiles(target)))
    def test_schedule_validation(self):
        store.update_schedule(self.jp,"07:00,20:00",self.db)
        with self.assertRaises(ValueError): store.update_schedule(self.jp,"25:00",self.db)
    def test_read_only_api_params(self):
        calls=[]
        with patch("mcrs.youtube_api._get",side_effect=lambda endpoint,key,**kwargs: calls.append((endpoint,kwargs)) or {"items":[]}): lookup_videos("key","칠랩","KR")
        self.assertEqual("video",calls[0][1]["type"])
    def test_excluded_rows_remain_in_genre_audit_only(self):
        row=self.video(); row["video_title"]="수면용 Lo-Fi ASMR"
        store.save_candidates(self.jp,[row],self.db)
        self.assertEqual([],store.list_candidates(self.jp,path=self.db))
        audit=store.list_genre_audit(self.jp,path=self.db)
        self.assertEqual(1,len(audit)); self.assertEqual("제외",audit[0]["predicted_decision"])
    def test_human_labels_and_quality_metrics(self):
        good=self.video(); bad=self.video("bBcDeF12345"); bad["video_title"]="수면용 Lo-Fi ASMR"
        store.save_candidates(self.jp,[good,bad],self.db)
        store.label_genre_audit(self.jp,"aBcDeF12345","적합","사람이 직접 감상하고 적합 확인",self.db)
        store.label_genre_audit(self.jp,"bBcDeF12345","적합","잘못 제외된 실제 칠랩 영상",self.db)
        quality=store.genre_quality(self.jp,self.db)
        self.assertEqual(1.0,quality["precision"])
        self.assertEqual(1.0,quality["exclusion_error_rate"])
    def test_profile_search_terms_are_isolated(self):
        store.set_profile_search_terms(self.jp,"JP",["朝 チルラップ","朝 メロウラップ"],self.db)
        store.set_profile_search_terms(self.kr,"KR",["아침 칠랩"],self.db)
        self.assertEqual(["朝 チルラップ","朝 メロウラップ"],store.list_profile_search_terms(self.jp,"JP",self.db))
        self.assertEqual(["아침 칠랩"],store.list_profile_search_terms(self.kr,"KR",self.db))
    def test_subgenre_migrates_and_updates(self):
        store.update_profile(self.jp,name="일본 모닝 조사",market="JP",genre="MORNING_CHILL_RAP",subgenre="여성 보컬",path=self.db)
        self.assertEqual("여성 보컬",store.get_profile(self.jp,self.db)["subgenre"])

    def test_related_genres_persist_validate_and_preserve_subgenre(self):
        import json
        store.update_profile(self.jp,name="일본 모닝 조사",market="JP",genre="MORNING_CHILL_RAP",
            subgenre="legacy value",related_genres=["CHILL_RAP","CHILL_RAP","MORNING_CHILL_RAP"],path=self.db)
        profile=store.get_profile(self.jp,self.db)
        self.assertEqual(["CHILL_RAP"],json.loads(profile["related_genres_json"]))
        self.assertEqual("legacy value",profile["subgenre"])
        with self.assertRaises(ValueError): store.update_profile(self.jp,name="JP test",market="JP",genre="MORNING_CHILL_RAP",
            related_genres=["BAD_GENRE"],path=self.db)
        self.assertEqual(5, len(store.normalize_related_genres("MORNING_CHILL_RAP", list(LEGACY_GENRE_IDS))))

    def test_legacy_profile_migration_adds_empty_related_without_data_loss(self):
        import sqlite3
        legacy=self.root/"legacy-v3.db"
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE profiles(id TEXT PRIMARY KEY,name TEXT UNIQUE,market TEXT,genre TEXT,subgenre TEXT)")
            db.execute("INSERT INTO profiles VALUES('legacy','Legacy JP','JP','MORNING_CHILL_RAP','kept')")
        store.initialize(legacy)
        profile=store.get_profile("legacy",legacy)
        self.assertEqual("kept",profile["subgenre"])
        self.assertEqual("[]",profile["related_genres_json"])
        self.assertEqual(["kept"],store.profile_genre_config(profile)["subgenres"])
        store.update_profile("legacy",name="Legacy JP",market="JP",genre="MORNING_CHILL_RAP",
            genre_config={"subgenres":[]},path=legacy)
        store.initialize(legacy)
        self.assertEqual([],store.profile_genre_config(store.get_profile("legacy",legacy))["subgenres"])
        self.assertEqual("kept",store.get_profile("legacy",legacy)["subgenre"])

    def test_candidate_keeps_decision_genre_snapshot_after_profile_related_edit(self):
        import json
        store.update_profile(self.jp,name="일본 모닝 조사",market="JP",genre="MORNING_CHILL_RAP",
            related_genres=["CHILL_RAP"],path=self.db)
        row=self.video(); store.save_candidates(self.jp,[row],self.db)
        original=store.get_candidate(self.jp,"aBcDeF12345",self.db)
        self.assertEqual("MORNING_CHILL_RAP",original["classified_genre"])
        self.assertEqual(["CHILL_RAP"],json.loads(original["related_genres_json"]))
        store.update_profile(self.jp,name="일본 모닝 조사",market="JP",genre="MORNING_CHILL_RAP",
            related_genres=["RNB_HIPHOP"],path=self.db)
        unchanged=store.get_candidate(self.jp,"aBcDeF12345",self.db)
        self.assertEqual(original["genre_decision"],unchanged["genre_decision"])
        self.assertEqual(original["related_genres_json"],unchanged["related_genres_json"])

if __name__ == "__main__": unittest.main()
