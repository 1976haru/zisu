import io
import json
import socket
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from mcrs import quota_ledger, research_scheduler as scheduler, store, youtube_api
from mcrs import autopilot, oauth
from support import WorkspaceTemp


class NetworkQuotaSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        self.db = self.root / "isolated.sqlite"
        store.initialize(self.db)
        self.profile = store.create_profile("quota test", "JP", "MORNING_CHILL_RAP", path=self.db)
        self.other = store.create_profile("other profile", "KR", "OLD_POP", path=self.db)
        self.identity = "api-key:" + "a" * 64

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    @staticmethod
    def _http_error(status, reason="", payload=None):
        body = io.BytesIO(json.dumps(payload or {"error": {"errors": [{"reason": reason}]}}).encode())
        return HTTPError("https://www.googleapis.com/youtube/v3/search", status, "safe", {}, body)

    def test_http_statuses_are_normalized_without_provider_body(self):
        cases = [(401, "", "auth_required"), (403, "quotaExceeded", "quota_exhausted"),
                 (403, "forbidden", "permission_denied"), (429, "", "rate_limited"),
                 (500, "", "server_error"), (503, "", "server_error")]
        for status, reason, expected in cases:
            with self.subTest(status=status, reason=reason), patch.object(
                    youtube_api, "urlopen", side_effect=self._http_error(status, reason)):
                with self.assertRaises(youtube_api.APIError) as caught:
                    youtube_api._get("search", "NOT-A-REAL-KEY", retries=2, quota_context={
                        "profile_id": self.profile, "path": self.db,
                        "project_identity": self.identity, "search_limit": 30})
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn("NOT-A-REAL-KEY", str(caught.exception))

    def test_dns_timeout_offline_and_vpn_change_are_distinguished(self):
        self.assertEqual("dns_failure", youtube_api.classify_network_exception(
            URLError(socket.gaierror("secret-host")))[0])
        self.assertEqual("network_timeout", youtube_api.classify_network_exception(TimeoutError())[0])
        self.assertEqual("network_offline", youtube_api.classify_network_exception(
            URLError(OSError(10051, "offline")))[0])
        self.assertEqual("network_changed", youtube_api.classify_network_exception(
            ConnectionError(), before="a", after="b")[0])

    def test_uncertain_timeout_is_not_retried_and_reservation_survives(self):
        context = {"profile_id": self.profile, "path": self.db,
                   "project_identity": self.identity, "search_limit": 20}
        with patch.object(youtube_api, "urlopen", side_effect=TimeoutError("secret detail")) as request:
            with self.assertRaises(youtube_api.APIError) as caught:
                youtube_api._get("search", "private-key", retries=2, retry_delay=0,
                                 quota_context=context)
        self.assertEqual(1, request.call_count)
        self.assertTrue(caught.exception.request_uncertain)
        summary = quota_ledger.summary(self.identity, self.db, profile_id=self.profile)
        self.assertEqual(1, summary["search_calls_used_estimate"])
        with store.connect(self.db) as db:
            row = db.execute("SELECT status,error_code FROM youtube_quota_ledger").fetchone()
            self.assertEqual(("uncertain", "network_timeout"), tuple(row))
        self.assertNotIn(b"private-key", self.db.read_bytes())

    def test_shared_project_and_profile_budgets_are_separate_and_restart_safe(self):
        first = quota_ledger.reserve(self.identity, self.profile, "search", self.db,
                                     session_id="s1", search_limit=2)
        quota_ledger.finish(first, "succeeded", self.db)
        second = quota_ledger.reserve(self.identity, self.other, "search", self.db,
                                      session_id="s2", search_limit=2)
        self.assertEqual(2, quota_ledger.summary(self.identity, self.db)["search_calls_used_estimate"])
        self.assertEqual(1, quota_ledger.summary(self.identity, self.db,
            profile_id=self.profile, session_id="s1")["search_calls_used_estimate"])
        self.assertEqual(1, quota_ledger.summary(self.identity, self.db,
            profile_id=self.profile, session_id="s1")["session_search_calls_used_estimate"])
        with self.assertRaises(quota_ledger.QuotaLimitExceeded):
            quota_ledger.reserve(self.identity, self.profile, "search", self.db,
                                 search_limit=2)
        quota_ledger.finish(second, "failed", self.db)
        self.assertEqual(2, quota_ledger.summary(self.identity, self.db)["search_calls_used_estimate"])
        with store.connect(self.db) as db:
            rows = db.execute("SELECT project_scope_hash,profile_id,status FROM youtube_quota_ledger").fetchall()
            self.assertTrue(all(self.identity not in row["project_scope_hash"] for row in rows))
            self.assertEqual({self.profile, self.other}, {row["profile_id"] for row in rows})

    def test_non_search_units_and_daily_pacific_reset(self):
        now = datetime(2026, 10, 18, 6, 59, tzinfo=timezone.utc)
        request_id = quota_ledger.reserve(self.identity, self.profile, "videos", self.db,
                                          unit_limit=2, now=now)
        self.assertEqual(1, quota_ledger.summary(self.identity, self.db, now=now)["other_units_used_estimate"])
        quota_ledger.finish(request_id, "succeeded", self.db)
        self.assertNotEqual(quota_ledger.quota_day(now), quota_ledger.quota_day(
            datetime(2026, 10, 18, 8, 1, tzinfo=timezone.utc)))

    def test_scheduler_pauses_only_own_profile_and_requires_auth_recovery_confirmation(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.profile, total_hours=2),
                                   self.db, now=datetime(2026, 10, 18, 9, tzinfo=timezone.utc))
        error = youtube_api.APIError(code="auth_required", request_uncertain=False)
        paused = scheduler.pause_for_api_error(session["id"], self.profile, error, self.db)
        self.assertEqual(("paused", "auth_required"), (paused["state"], paused["pause_reason"]))
        with self.assertRaises(ValueError):
            scheduler.resume(session["id"], self.profile, self.db)
        resumed = scheduler.resume(session["id"], self.profile, self.db,
                                   external_recovery_confirmed=True)
        self.assertEqual("running", resumed["state"])
        self.assertIsNone(scheduler.get(session["id"], self.other, self.db))

    def test_uncertain_scheduler_request_cannot_resume_automatically(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.profile, total_hours=2),
                                   self.db, now=datetime(2026, 10, 18, 9, tzinfo=timezone.utc))
        with store.connect(self.db) as db:
            db.execute("UPDATE research_schedules SET search_in_flight=1 WHERE id=?", (session["id"],))
        error = youtube_api.APIError(code="network_timeout", request_uncertain=True)
        paused = scheduler.pause_for_api_error(session["id"], self.profile, error, self.db)
        self.assertEqual("network_wait", paused["pause_reason"])
        self.assertTrue(paused["search_in_flight"])
        with self.assertRaises(scheduler.UnresolvedSearchCall):
            scheduler.resume(session["id"], self.profile, self.db)

    def test_oauth_failure_is_normalized_without_exception_detail(self):
        raw = "private-marker-not-real"
        result = autopilot._safe_error(oauth.OAuthError(raw, code="scope_mismatch"))
        self.assertEqual("google_permission", result["type"])
        self.assertNotIn(raw, result["impact"] + result["recommended_action"])

    def test_oauth_scope_failure_pauses_only_profile_as_permission_required(self):
        session = scheduler.create(scheduler.ScheduleConfig(self.profile, total_hours=2),
                                   self.db, now=datetime(2026, 10, 18, 9, tzinfo=timezone.utc))
        paused = scheduler.pause_for_api_error(session["id"], self.profile,
            oauth.OAuthError(code="scope_mismatch"), self.db)
        self.assertEqual(("paused", "permission_denied", False),
                         (paused["state"], paused["pause_reason"], paused["search_in_flight"]))


if __name__ == "__main__":
    unittest.main()
