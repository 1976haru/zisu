import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from mcrs.genres import EXCLUDE, FIT, GENRES, REVIEW, assess
from mcrs import store, youtube_api
from support import WorkspaceTemp


FIT_TITLES = {
    "MORNING_CHILL_RAP": {"KR": "아침 morning chill rap", "JP": "朝 morning chill rap"},
    "CHILL_RAP": {"KR": "감성 힙합 chill rap", "JP": "チルラップ chill rap"},
    "RNB_HIPHOP": {"KR": "감성 알앤비 힙합 r&b hip-hop", "JP": "R&B ヒップホップ r&b hip-hop"},
    "CAFE_CHILL": {"KR": "카페 칠 cafe chill", "JP": "カフェチル cafe chill"},
    "OLD_POP": {"KR": "추억의 팝송 old pop oldies", "JP": "Japan old pop oldies classics"},
    "JAZZ_LOUNGE": {"KR": "재즈 라운지 jazz lounge", "JP": "ジャズラウンジ jazz lounge"},
}
EXCLUDE_TITLES = {
    "MORNING_CHILL_RAP": "수면용 Lo-Fi ASMR 무보컬 ambient",
    "CHILL_RAP": "수면용 Lo-Fi ASMR",
    "RNB_HIPHOP": "ASMR 빗소리",
    "CAFE_CHILL": "수면 음악 ASMR",
    "OLD_POP": "heavy metal EDM",
    "JAZZ_LOUNGE": "hardcore metal EDM",
}
SUFFIXES = ("", " 2026", " | mix", " 🎧", " 【HQ】", " vol.01 – édition")


class GenreBoundaryMatrixTests(unittest.TestCase):
    def test_216_balanced_multilingual_boundary_cases(self):
        counts = {FIT: 0, REVIEW: 0, EXCLUDE: 0}
        for genre_id in GENRES:
            for market in ("KR", "JP"):
                cases = (
                    (FIT, FIT_TITLES[genre_id][market]),
                    (REVIEW, "새로운 음악 컬렉션 new release"),
                    (EXCLUDE, EXCLUDE_TITLES[genre_id]),
                )
                for expected, title in cases:
                    for suffix in SUFFIXES:
                        with self.subTest(genre=genre_id, market=market, expected=expected, suffix=suffix):
                            actual = assess(genre_id, title + suffix).decision
                            self.assertEqual(expected, actual)
                            counts[actual] += 1
        self.assertEqual({FIT: 72, REVIEW: 72, EXCLUDE: 72}, counts)

    def test_unicode_emoji_special_and_long_metadata_stays_conservative(self):
        title = "朝 아침 Café 🎧 <>&\"' " + ("경계 " * 500)
        verdict = assess("MORNING_CHILL_RAP", title)
        self.assertEqual(REVIEW, verdict.decision)
        self.assertTrue(verdict.uncertainty)

    def test_storage_bounds_very_long_multilingual_api_metadata(self):
        with WorkspaceTemp() as root:
            database = root / "긴 제목 日本語.db"
            profile = store.create_profile("긴 제목 검사", "JP", "MORNING_CHILL_RAP", database)
            store.save_candidates(profile, [{
                "video_id": "LongTitle01",
                "video_title": "朝 morning chill rap 🎧" + ("가" * 5000),
                "description": "説明" * 10000,
                "channel_title": "채널" * 1000,
            }], database)
            row = store.get_candidate(profile, "LongTitle01", database)
        self.assertEqual(1000, len(row["video_title"]))
        self.assertEqual(10000, len(row["description"]))
        self.assertEqual(500, len(row["channel_title"]))


class APIResilienceTests(unittest.TestCase):
    @staticmethod
    def response(payload):
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        return response

    def test_uncertain_network_error_is_not_retried(self):
        with patch.object(youtube_api, "urlopen", side_effect=URLError("offline")) as request:
            with self.assertRaises(youtube_api.APIError) as caught:
                youtube_api._get("search", "key", retries=2, retry_delay=0, part="snippet")
        self.assertEqual("connection_failed", caught.exception.code)
        self.assertTrue(caught.exception.request_uncertain)
        self.assertEqual(1, request.call_count)

    def test_quota_error_is_not_retried(self):
        body = io.BytesIO(json.dumps({
            "error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}
        }).encode("utf-8"))
        error = HTTPError("https://example.invalid", 403, "Forbidden", {}, body)
        with patch.object(youtube_api, "urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(youtube_api.APIError, "할당량"):
                youtube_api._get("search", "key", retries=2, retry_delay=0, part="snippet")
        self.assertEqual(1, request.call_count)


if __name__ == "__main__":
    unittest.main()
