import json
import unittest
from datetime import datetime, timezone

from mcrs import ai_research, candidate_diversity, research_scheduler, store
from mcrs.autopilot import FailClosedProvider
from mcrs.ai_research import AIOpinion, ResearchReview
from mcrs.genres import (EXCLUDE, FIT, REVIEW, GENRES, LEGACY_GENRE_IDS, assess,
                         custom_genre_id, search_terms)
from support import WorkspaceTemp


def dream_pop_definition():
    return {
        "id": custom_genre_id("Dream Pop"),
        "name": "Dream Pop",
        "aliases": ["dream-pop", "dream pop"],
        "include_terms": ["dream pop", "ethereal", "synth textures"],
        "exclude_terms": ["spoken word", "white noise"],
        "jp_queries": ["ドリームポップ", "日本 ドリームポップ"],
        "kr_queries": ["드림 팝", "한국 드림팝"],
        "en_queries": ["dream pop playlist"],
        "localized_queries": {"FR": ["dream pop français", "pop onirique"]},
    }


class FlexibleGenreRuleTests(unittest.TestCase):
    def test_existing_six_ids_remain_stable_and_new_presets_are_localized(self):
        self.assertEqual(6, len(LEGACY_GENRE_IDS))
        self.assertTrue(set(LEGACY_GENRE_IDS).issubset(GENRES))
        expected = {"JAZZ_SMOOTH", "INDIE_POP", "LOFI", "MELODIC_HIPHOP", "CAFE_POP",
                    "CAFE_JAZZ", "SENIOR_OLDIES", "CHANSON", "LATIN", "BOSSA_NOVA", "ACOUSTIC_BALLAD"}
        self.assertTrue(expected.issubset(GENRES))
        for genre_id in expected:
            self.assertTrue(search_terms(genre_id, "JP"))
            self.assertTrue(search_terms(genre_id, "KR"))
            self.assertTrue(search_terms(genre_id, "EN"))

    def test_custom_genre_fails_closed_then_requires_title_and_two_description_signals(self):
        definition = dream_pop_definition()
        custom = [definition]
        self.assertEqual(REVIEW, assess(definition["id"], "Dream Pop", "", custom).decision)
        self.assertEqual(REVIEW, assess(definition["id"], "New album", "ethereal synth textures", custom).decision)
        self.assertEqual(FIT, assess(definition["id"], "Dream Pop playlist",
                                     "Ethereal vocals with synth textures.", custom).decision)
        self.assertEqual(REVIEW, assess("CUSTOM_UNREGISTERED000", "Dream Pop", "", custom).decision)
        self.assertEqual(EXCLUDE, assess(definition["id"], "Dream Pop playlist",
                                         "Ethereal vocals with synth textures and white noise.", custom).decision)

    def test_profile_custom_rules_facets_and_locales_persist_without_cross_profile_leak(self):
        definition = dream_pop_definition()
        config = {"custom_genres": [definition],
                  "subgenres": ["ethereal dream pop"],
                  "facets": {"moods": ["dreamy"], "eras": ["2020s"],
                             "audiences": ["late-night listeners"], "languages": ["English"]},
                  "search_languages": ["JP", "EN", "FR"]}
        with WorkspaceTemp() as root:
            db = root / "custom genre 日本語.db"
            first = store.create_profile("Dream Pop JP", "JP", definition["id"], db,
                                         related_genres=["JAZZ_SMOOTH"], genre_config=config)
            second = store.create_profile("Jazz KR", "KR", "JAZZ_SMOOTH", db,
                                          genre_config={"facets": {"moods": ["calm"]}, "search_languages": ["KR"]})
            isolated = []
            for index, name in enumerate(("Chanson FR", "Bossa JP", "Indie EN"), start=3):
                local = {"id": custom_genre_id(name), "name": name,
                         "include_terms": [name.casefold()], "jp_queries": [f"{name} 日本"],
                         "kr_queries": [f"{name} 한국"], "en_queries": [f"{name} playlist"]}
                local_id = store.create_profile(name, "JP" if index == 4 else "KR",
                    local["id"], db, genre_config={"custom_genres": [local], "subgenres": [f"local-{index}"]})
                isolated.append((local_id, local, f"local-{index}"))
            store.initialize(db)
            profile = store.get_profile(first, db)
            normalized_config = store.profile_genre_config(profile)
            self.assertEqual(store._normalize_genre_config(config)["custom_genres"], normalized_config["custom_genres"])
            self.assertEqual(config["facets"]["moods"], normalized_config["facets"]["moods"])
            self.assertEqual(["ethereal dream pop"], normalized_config["subgenres"])
            self.assertEqual(["JP", "EN", "FR"], normalized_config["search_languages"])
            self.assertEqual([], store.profile_genre_config(store.get_profile(second, db))["custom_genres"])
            for local_id, local, subgenre in isolated:
                local_profile = store.get_profile(local_id, db)
                local_config = store.profile_genre_config(local_profile)
                self.assertEqual(local["name"], local_config["custom_genres"][0]["name"])
                self.assertEqual([subgenre], local_config["subgenres"])
                self.assertNotEqual(definition["id"], local_profile["genre"])
            self.assertEqual(("dream pop français", "pop onirique"),
                             search_terms(definition["id"], "JP", config["custom_genres"], "FR"))
            query_plan = ai_research._profile_query_plan(profile, 12, db)[0]
            self.assertIn("dream pop français", query_plan)
            self.assertTrue(any("dreamy" in term for term in query_plan))
            self.assertTrue(any("ethereal dream pop" in term for term in query_plan))
            self.assertEqual("core", candidate_diversity.classify_search_category("dream pop français", profile, db))
            self.assertFalse(store.profile_genre_config(store.get_profile(second, db))["custom_genres"])

    def test_candidate_and_audit_keep_genre_rule_snapshot_and_human_label(self):
        definition = dream_pop_definition()
        config = {"custom_genres": [definition], "search_languages": ["EN"]}
        with WorkspaceTemp() as root:
            db = root / "snapshots.db"
            profile_id = store.create_profile("Dream Pop", "JP", definition["id"], db, genre_config=config)
            store.save_candidates(profile_id, [{"video_id": "DreaM000001", "video_title": "Dream Pop playlist",
                "description": "Ethereal vocals with synth textures."}], db)
            candidate = store.get_candidate(profile_id, "DreaM000001", db)
            audit = store.list_genre_audit(profile_id, path=db)[0]
            self.assertEqual(FIT, candidate["genre_decision"])
            canonical = store._normalize_genre_config(config)["custom_genres"]
            self.assertEqual(canonical, json.loads(candidate["genre_config_snapshot_json"])["custom_genres"])
            self.assertEqual(canonical, json.loads(audit["genre_config_snapshot_json"])["custom_genres"])
            store.label_genre_audit(profile_id, "DreaM000001", "적합", "실제 감상 후 사람 라벨을 별도 기록", db)
            updated = dream_pop_definition(); updated["exclude_terms"].append("concert")
            store.update_profile(profile_id, name="Dream Pop", market="JP", genre=definition["id"], path=db,
                                 related_genres=[], genre_config={**config, "custom_genres": [updated]})
            self.assertEqual(canonical, json.loads(store.get_candidate(profile_id, "DreaM000001", db)["genre_config_snapshot_json"])["custom_genres"])
            self.assertEqual("적합", store.list_genre_audit(profile_id, path=db)[0]["human_label"])

    def test_ai_fit_cannot_promote_uncertain_custom_genre_to_automatic_fit(self):
        definition = dream_pop_definition()
        config = store._normalize_genre_config({"custom_genres": [definition]})
        profile = {"id": "p", "genre": definition["id"], "related_genres_json": "[]",
                   "genre_config_json": json.dumps(config, ensure_ascii=False)}
        candidate = {"video_title": "New song", "description": "Ethereal sound and synth textures."}
        review = ResearchReview("p", "v", REVIEW, 0, "insufficient metadata", REVIEW,
            AIOpinion(FIT, ("genre",), (), ("Ethereal sound",), "uncertain", "listen first"),
            "mock", "mock-model", "hash", "rules + AI")
        self.assertEqual("cross_genre_review", ai_research.proposed_verdict(profile, review, candidate))

    def test_custom_genre_flows_through_bounded_search_rules_and_saved_candidate(self):
        definition = dream_pop_definition()
        with WorkspaceTemp() as root:
            db = root / "custom-autopilot.db"
            profile_id = store.create_profile("Dream Pop", "JP", definition["id"], db,
                genre_config={"custom_genres": [definition], "subgenres": ["dream pop vocals"],
                              "search_languages": ["JP"]})
            seen = []
            def collector(term, market, limit, page_token=None, order="relevance"):
                seen.append((term, market, limit, page_token, order))
                return [{"video_id": f"Dpop{index:07d}", "video_title": "Dream Pop playlist",
                         "description": f"Ethereal vocals with synth textures, track {index}."}
                        for index in range(5)]
            result = ai_research.run_auto_recommendations(profile_id, collector, target=5,
                max_search_calls=1, path=db)
            self.assertEqual(1, len(seen))
            self.assertEqual("JP", seen[0][1])
            self.assertEqual(5, sum(ai_research.proposed_verdict(store.get_profile(profile_id, db), review,
                next(row for row in result.candidates if row["video_id"] == review.video_id)) == "auto_fit"
                for review in result.reviews))
            saved = store.list_candidates(profile_id, path=db)
            self.assertEqual(5, len(saved))
            self.assertTrue(all(json.loads(row["genre_config_snapshot_json"])["subgenres"] == ["dream pop vocals"]
                                for row in saved))

    def test_autopilot_provider_wrapper_preserves_selected_genre_context(self):
        captured = {}
        class ContextProvider:
            name = "ollama"
            model = "mock"
            def review(self, candidate, genre_id, related_genres=(), genre_context=None):
                captured.update(genre_id=genre_id, related=related_genres, context=genre_context)
                return AIOpinion(REVIEW, (), (), (), "uncertain", "human review")
        provider = FailClosedProvider(ContextProvider())
        context = {"core": {"id": "CUSTOM_A", "name": "Dream Pop"}, "subgenres": ["ethereal"]}
        provider.review(ai_research.Candidate("DreaM000001", "Dream Pop", ""), "CUSTOM_A",
                        related_genres=("JAZZ_SMOOTH",), genre_context=context)
        self.assertEqual(context, captured["context"])
        self.assertEqual(("JAZZ_SMOOTH",), captured["related"])

    def test_new_or_edited_profile_rules_cannot_change_under_active_schedule(self):
        definition = dream_pop_definition()
        config = {"custom_genres": [definition], "search_languages": ["JP", "FR"]}
        now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        with WorkspaceTemp() as root:
            db = root / "schedule.db"
            profile_id = store.create_profile("Dream Pop", "JP", definition["id"], db, genre_config=config)
            research_scheduler.create(research_scheduler.ScheduleConfig(profile_id, 2, 20, 15,
                approved_search_calls=3), db, now=now)
            changed = {**config, "facets": {"moods": ["bright"]}}
            with self.assertRaises(ValueError):
                store.update_profile(profile_id, name="Dream Pop", market="JP", genre=definition["id"],
                                     genre_config=changed, path=db)


if __name__ == "__main__":
    unittest.main()
