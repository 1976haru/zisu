import json
import unittest
from pathlib import Path

from mcrs import ai_research, store
from tests.support import WorkspaceTemp


class V459RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp(); self.root = self.temp.__enter__()
        self.db = self.root / "isolated.sqlite"
        store.initialize(self.db)
        self.profile = store.create_profile("복구 테스트", "JP", "MORNING_CHILL_RAP", path=self.db)

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def test_ui_state_round_trip_only_allowlisted_presentation_values(self):
        state = {"profile_id": self.profile, "tab_index": 4, "selected_video_id": "abcdefghijk",
                 "search_term": "朝 の音楽 🎧", "filter": "요코하마", "decision_filter": "검토",
                 "sort": ["title", False], "ai_target": "10", "ai_max_calls": "3"}
        # Sort is intentionally not an allowed arbitrary structure; state must stay scalar.
        state.pop("sort")
        store.save_ui_state(state, self.db)
        loaded = store.load_ui_state(self.db)
        self.assertEqual(loaded["state"], state)
        with self.assertRaises(ValueError):
            store.save_ui_state({"api_key": "not persisted"}, self.db)
        with store.connect(self.db) as db:
            payload = db.execute("SELECT state_json FROM ui_state").fetchone()[0]
        self.assertNotIn("api_key", payload)

    def test_ui_state_rejects_nested_or_non_finite_values(self):
        with self.assertRaises(ValueError):
            store.save_ui_state({"filter": {"nested": "secret"}}, self.db)
        with self.assertRaises(ValueError):
            store.save_ui_state({"tab_index": float("nan")}, self.db)

    def test_repeated_migration_preserves_existing_profile_and_candidates(self):
        store.save_candidates(self.profile, [{"video_id": "abcdefghijk", "video_title": "朝のプレイリスト"}], self.db)
        before = store.get_candidate(self.profile, "abcdefghijk", self.db)
        store.initialize(self.db)
        store.initialize(self.db)
        after = store.get_candidate(self.profile, "abcdefghijk", self.db)
        self.assertEqual(before["video_title"], after["video_title"])
        self.assertEqual(store.get_profile(self.profile, self.db)["name"], "복구 테스트")

    def test_ai_success_cache_and_budget_block_are_distinct(self):
        candidate = ai_research.Candidate("abcdefghijk", "朝のプレイリスト", "朝に聴きたい落ち着いた音楽。")
        provider = ai_research.MockProvider()
        first = ai_research.review_candidate(self.profile, candidate, provider, self.db, session_id="source-test")
        second = ai_research.review_candidate(self.profile, candidate, provider, self.db, session_id="source-test")
        self.assertEqual("success", first.ai_source)
        self.assertEqual("cache", second.ai_source)
        restored = ai_research._review_from_payload(ai_research._review_to_payload(first))
        self.assertEqual("success", restored.ai_source)

        class PaidProvider:
            name = "gemini"
            model = "mock-model"
            def review(self, *_args, **_kwargs):
                raise AssertionError("provider must not be called without explicit consent")
        blocked = ai_research.review_candidate(self.profile,
            ai_research.Candidate("lmnopqrstuv", "朝のプレイリスト", "朝に聴きたい落ち着いた音楽。"),
            PaidProvider(), self.db, session_id="blocked-test")
        self.assertEqual("budget_blocked", blocked.ai_source)


if __name__ == "__main__":
    unittest.main()
