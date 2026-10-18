from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
import socket
from unittest.mock import patch
import unittest

from mcrs import analytics, store
from mcrs.credential_store import CredentialStore
from tests.support import WorkspaceTemp


class FakeResponse:
    def __init__(self, payload): self.payload=json.dumps(payload).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self,*_args): return False
    def read(self,limit=-1): return self.payload[:limit]


def report_payload(report, rows=None):
    names=[analytics.REPORTS[report]["dimensions"],*analytics.REPORTS[report]["metrics"].split(",")]
    return {"columnHeaders":[{"name":name} for name in names],"rows":rows or []}


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp();self.root=self.temp.__enter__()
        self.previous=os.environ.get("ZISU_DATA_DIR");os.environ["ZISU_DATA_DIR"]=str(self.root/"data")
        store.initialize()
        self.p1=store.create_profile("JP channel one","JP","MORNING_CHILL_RAP")
        self.p2=store.create_profile("JP channel two","JP","CHILL_RAP")
        store.bind_google_account_channel(self.p1,"UCabcdefgh123456789","One","one@example.test")
        store.bind_google_account_channel(self.p2,"UCijklmnop123456789","Two","two@example.test")
        self.profile=store.get_profile(self.p1)
        self.today=date.today()
        self.start,self.end=analytics.date_window(7,today=self.today)
        self.repo=analytics.AnalyticsRepository()
        self.token={"access_token":"mock-token-never-logged","scopes":[analytics.READONLY_SCOPE],
                    "email":"one@example.test","channel_id":self.profile["youtube_channel_id"]}

    def tearDown(self):
        if self.previous is None:os.environ.pop("ZISU_DATA_DIR",None)
        else:os.environ["ZISU_DATA_DIR"]=self.previous
        self.temp.__exit__(None,None,None)

    def test_query_contract_is_channel_scoped_read_only_and_reach_metrics_are_separate(self):
        query=analytics.build_query(self.profile["youtube_channel_id"],"daily",self.start,self.end)
        self.assertEqual("channel=="+self.profile["youtube_channel_id"],query["ids"])
        self.assertEqual("day",query["dimensions"])
        self.assertIn("averageViewPercentage",query["metrics"])
        self.assertNotIn("impression",query["metrics"].casefold())
        self.assertNotIn("ctr",query["metrics"].casefold())
        self.assertEqual("https://www.googleapis.com/auth/yt-analytics.readonly",analytics.READONLY_SCOPE)
        with self.assertRaises(ValueError):analytics.build_query("UCbad","daily",self.start,self.end)
        seen=[]
        with patch("mcrs.analytics.urlopen",side_effect=lambda request,timeout:(seen.append((request.get_method(),timeout,request.full_url)) or FakeResponse(report_payload("daily")))):
            with analytics.AnalyticsClient._http_get("https://example.test/reports",{"Authorization":"Bearer dummy"}):pass
        self.assertEqual("GET",seen[0][0]);self.assertEqual(20,seen[0][1])

    def test_empty_data_is_not_fabricated_as_zero_and_dashboard_stays_uncollected(self):
        parsed=analytics.parse_report("daily",report_payload("daily"))
        self.assertEqual("no_data",parsed["state"])
        self.assertEqual([],parsed["rows"])
        self.repo.put(self.p1,self.profile["youtube_channel_id"],"daily",7,parsed)
        view=analytics.dashboard_view(self.p1,self.profile["youtube_channel_id"],7,self.repo)
        self.assertEqual("no_data",view["state"])
        self.assertEqual({},view["metrics"])
        self.assertEqual("not_collected",view["impressions_ctr"]["state"])
        self.assertEqual("not_collected",analytics.dashboard_view(self.p2,store.get_profile(self.p2)["youtube_channel_id"],7,self.repo)["state"])

    def test_missing_metric_values_remain_unavailable_instead_of_becoming_zero(self):
        self.repo.put(self.p1,self.profile["youtube_channel_id"],"daily",7,
            {"state":"data","rows":[{"day":self.end,"views":0,"estimatedMinutesWatched":None,
             "averageViewDuration":None,"averageViewPercentage":None,"subscribersGained":0,"subscribersLost":0}]})
        view=analytics.dashboard_view(self.p1,self.profile["youtube_channel_id"],7,self.repo)
        self.assertEqual(0,view["metrics"]["views"])
        self.assertIsNone(view["metrics"]["watch_minutes"])
        self.assertIsNone(view["metrics"]["avg_view_duration_seconds"])

    def test_fetches_current_and_prior_reports_with_get_and_keeps_profile_channel_scope(self):
        calls=[]
        fixtures={
            "daily":report_payload("daily",[[self.end,12,36,90,45.0,2,1]]),
            "videos":report_payload("videos",[["video-one",12,36,90,45.0]]),
            "countries":report_payload("countries",[["JP",10,28]]),
            "traffic":report_payload("traffic",[["RELATED_VIDEO",8,22]]),
        }
        prior_start,previous_end=analytics.previous_date_window(7,today=self.today)
        fixtures["daily_previous"]=report_payload("daily",[[previous_end,10,30,80,42.0,1,1]])
        def transport(url,headers):
            calls.append((url,headers))
            params=parse_qs(urlparse(url).query)
            report="daily_previous" if params.get("startDate")==[prior_start] else next(k for k,v in analytics.REPORTS.items() if params.get("dimensions")==[v["dimensions"]])
            return FakeResponse(fixtures[report])
        client=analytics.AnalyticsClient(lambda _pid:self.token,transport=transport,repository=self.repo,
                                         project_hash=analytics.project_scope_hash("mock-project"))
        result=client.fetch(self.profile,7,today=self.today,use_cache=False)
        self.assertEqual("data",result["state"])
        self.assertEqual(5,len(calls))
        for url,headers in calls:
            self.assertNotIn("mock-token",url)
            self.assertEqual("Bearer mock-token-never-logged",headers["Authorization"])
            self.assertEqual(["channel=="+self.profile["youtube_channel_id"]],parse_qs(urlparse(url).query)["ids"])
        view=analytics.dashboard_view(self.p1,self.profile["youtube_channel_id"],7,self.repo)
        self.assertEqual(12,view["metrics"]["views"])
        self.assertEqual(2,view["metrics"]["views_change"])
        self.assertEqual(1,view["metrics"]["subscriber_change"])
        self.assertEqual("video-one",view["top_videos"][0]["video"])

    def test_missing_scope_and_wrong_account_never_send_request(self):
        calls=[]
        client=analytics.AnalyticsClient(lambda _pid:{**self.token,"scopes":[]},transport=lambda *_:calls.append(1),
            repository=self.repo,project_hash="p")
        result=client.fetch(self.profile,7,today=self.today,use_cache=False)
        self.assertEqual("consent_required",result["state"])
        self.assertEqual([],calls)
        client=analytics.AnalyticsClient(lambda _pid:{**self.token,"email":"wrong@example.test"},transport=lambda *_:calls.append(1),
            repository=self.repo,project_hash="p")
        result=client.fetch(self.profile,7,today=self.today,use_cache=False)
        self.assertEqual("account_mismatch",result["state"])
        self.assertEqual([],calls)

    def test_write_scope_rejected_and_http_failures_are_allowlisted_without_retry(self):
        calls=[]
        client=analytics.AnalyticsClient(lambda _pid:{**self.token,"scopes":[analytics.READONLY_SCOPE,"https://www.googleapis.com/auth/youtube"]},
            transport=lambda *_:calls.append(1),repository=self.repo,project_hash="p")
        result=client.fetch(self.profile,7,today=self.today,use_cache=False)
        self.assertEqual("consent_required",result["state"]);self.assertEqual([],calls)
        attempts=[]
        def forbidden(url,headers):
            attempts.append(1);raise HTTPError(url,429,"raw private response text",{},None)
        client=analytics.AnalyticsClient(lambda _pid:self.token,transport=forbidden,repository=self.repo,project_hash="p")
        result=client.fetch(self.profile,7,today=self.today,use_cache=False)
        self.assertEqual("rate_limited",result["state"]);self.assertEqual(1,len(attempts))
        self.assertNotIn("raw private",result["message"])

    def test_auth_permission_and_server_errors_are_distinct_and_not_retried(self):
        for status,expected in ((401,"auth_required"),(403,"permission_denied"),(503,"server_error")):
            attempts=[]
            def fail(url,headers):
                attempts.append(1);raise HTTPError(url,status,"private provider body",{},None)
            client=analytics.AnalyticsClient(lambda _pid:self.token,transport=fail,
                repository=analytics.AnalyticsRepository(),project_hash=f"p{status}")
            result=client.fetch(self.profile,7,today=self.today,use_cache=False)
            self.assertEqual(expected,result["state"]);self.assertEqual(1,len(attempts))
            self.assertNotIn("private provider body",result.get("message",""))

    def test_network_failures_are_classified_and_uncertain_requests_are_not_retried(self):
        cases = ((URLError(socket.gaierror("private host")), "dns_failure", False),
                 (TimeoutError("private timeout"), "network_timeout", True),
                 (OSError(10050, "private offline"), "network_offline", False))
        for index, (failure, expected, uncertain) in enumerate(cases):
            repo = analytics.AnalyticsRepository()
            client = analytics.AnalyticsClient(lambda _pid:self.token,
                transport=lambda *_args, failure=failure:(_ for _ in ()).throw(failure),
                repository=repo, project_hash=f"network-{index}")
            result = client.fetch(self.profile, 7, today=self.today, use_cache=False)
            self.assertEqual(expected, result["state"])
            self.assertNotIn("private", result.get("message", ""))
            with store.connect() as db:
                status = db.execute("SELECT status FROM youtube_analytics_calls WHERE project_hash=? LIMIT 1",
                                    (f"network-{index}",)).fetchone()[0]
            self.assertEqual("uncertain" if uncertain else "failed", status)

        changed = iter(("before", "after"))
        with patch("mcrs.analytics._network_signature", side_effect=lambda:next(changed)):
            client = analytics.AnalyticsClient(lambda _pid:self.token,
                transport=lambda *_args:(_ for _ in ()).throw(URLError("private")),
                repository=analytics.AnalyticsRepository(), project_hash="network-changed")
            result = client.fetch(self.profile, 7, today=self.today, use_cache=False)
        self.assertEqual("network_changed", result["state"])

    def test_http_403_quota_reason_is_distinct_without_exposing_provider_body(self):
        payload = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'
        self.assertEqual("quota_exhausted", analytics.classify_http(403, payload))
        self.assertEqual("permission_denied", analytics.classify_http(403, b"invalid private response"))
        self.assertEqual("rate_limited", analytics.classify_http(403,
                         b'{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}'))

    def test_cache_is_isolated_by_profile_and_local_request_caps_are_conservative(self):
        value=analytics.parse_report("videos",report_payload("videos",[["v",1,2,3,4.0]]))
        self.repo.put(self.p1,self.profile["youtube_channel_id"],"videos",7,value)
        self.assertIsNotNone(self.repo.get(self.p1,self.profile["youtube_channel_id"],"videos",7))
        self.assertIsNone(self.repo.get(self.p2,store.get_profile(self.p2)["youtube_channel_id"],"videos",7))
        first=self.repo.reserve(self.p1,self.profile["youtube_channel_id"],"daily","project",profile_limit=1,project_limit=3)
        with self.assertRaises(analytics.AnalyticsError) as caught:
            self.repo.reserve(self.p1,self.profile["youtube_channel_id"],"videos","project",profile_limit=1,project_limit=3)
        self.assertEqual("request_limit",caught.exception.code)
        self.repo.finish(first,"uncertain")
        # Uncertain external requests remain counted after restart.
        with self.assertRaises(analytics.AnalyticsError):
            analytics.AnalyticsRepository().reserve(self.p1,self.profile["youtube_channel_id"],"daily","project",profile_limit=1,project_limit=3)

    def test_shared_project_call_limit_applies_across_profiles(self):
        self.repo.reserve(self.p1,self.profile["youtube_channel_id"],"daily","shared",profile_limit=5,project_limit=1)
        second=store.get_profile(self.p2)
        with self.assertRaises(analytics.AnalyticsError) as caught:
            self.repo.reserve(self.p2,second["youtube_channel_id"],"daily","shared",profile_limit=5,project_limit=1)
        self.assertEqual("request_limit",caught.exception.code)

    def test_incremental_consent_requires_user_action_and_exact_profile_identity(self):
        class Credentials:
            scopes=[analytics.READONLY_SCOPE,analytics.YOUTUBE_READONLY_SCOPE,"openid",analytics.EMAIL_SCOPE]
            token="not-output"
            def to_json(self):return json.dumps({"token":"secret","refresh_token":"secret2","scopes":self.scopes})
        class Flow:
            def run_local_server(self,**kwargs):
                self.kwargs=kwargs;return Credentials()
        flow=Flow();saved=[]
        class Manager:
            def save(self,*args,**kwargs):saved.append((args,kwargs))
        def factory(_config,**kwargs):return flow
        result=analytics.begin_analytics_consent(self.profile,{"installed":{"client_id":"mock"}},approved=False,
            credential_manager=Manager(),flow_factory=factory,identity_loader=lambda _token:("one@example.test",[{"channel_id":self.profile["youtube_channel_id"]}]))
        self.assertEqual("cancelled",result["state"]);self.assertEqual([],saved)
        with self.assertRaises(analytics.AnalyticsError) as caught:
            analytics.begin_analytics_consent(self.profile,{"installed":{"client_id":"mock"}},approved=True,
                credential_manager=Manager(),flow_factory=factory,identity_loader=lambda _token:("wrong@example.test",[{"channel_id":self.profile["youtube_channel_id"]}]))
        self.assertEqual("account_mismatch",caught.exception.code);self.assertEqual([],saved)
        result=analytics.begin_analytics_consent(self.profile,{"installed":{"client_id":"mock"}},approved=True,
            credential_manager=Manager(),flow_factory=factory,identity_loader=lambda _token:("one@example.test",[{"channel_id":self.profile["youtube_channel_id"]}]))
        self.assertEqual("connected",result["state"]);self.assertEqual(1,len(saved))
        self.assertEqual("true",flow.kwargs["include_granted_scopes"])

    def test_scope_warning_accepts_only_email_alias_and_requires_analytics_read_scope(self):
        requested=[analytics.READONLY_SCOPE,analytics.YOUTUBE_READONLY_SCOPE,"openid",analytics.EMAIL_SCOPE]
        class Credentials:
            def __init__(self,scopes):self.scopes=scopes;self.token="mock-token"
            def to_json(self):return json.dumps({"token":"mock-token","refresh_token":"mock-refresh"})
            @classmethod
            def from_authorized_user_info(cls,info,scopes):return cls(scopes)
        class ScopeWarning(Warning):pass
        warning=ScopeWarning()
        warning.old_scope=" ".join(requested)
        warning.new_scope=" ".join([analytics.READONLY_SCOPE,analytics.YOUTUBE_READONLY_SCOPE,"openid","email"])
        warning.token={"access_token":"mock-token","refresh_token":"mock-refresh"}
        recovered,scopes=analytics._recover_scope_warning(warning,
            {"installed":{"client_id":"mock","client_secret":"not-real","token_uri":"https://example.test/token"}},Credentials)
        self.assertEqual(set(requested),set(scopes));self.assertEqual("mock-token",recovered.token)
        warning.new_scope=" ".join([analytics.YOUTUBE_READONLY_SCOPE,"openid","email"])
        with self.assertRaises(analytics.AnalyticsError) as caught:
            analytics._recover_scope_warning(warning,
                {"installed":{"client_id":"mock","client_secret":"not-real","token_uri":"https://example.test/token"}},Credentials)
        self.assertEqual("consent_required",caught.exception.code)

    def test_analytics_oauth_material_uses_separate_profile_scoped_credential_vault(self):
        class Protector:
            def protect(self,value):return b"protected:"+value
            def unprotect(self,value):return value.removeprefix(b"protected:")
        protector=Protector()
        factory=lambda directory:CredentialStore(directory=directory,protector=protector)
        manager=analytics.AnalyticsCredentialManager(directory=self.root/"analytics-vault",credential_store_factory=factory)
        credential_json=json.dumps({"token":"mock-access","refresh_token":"mock-refresh","client_id":"mock-client"})
        manager.save(self.p1,credential_json,email="one@example.test",channel_id=self.profile["youtube_channel_id"],scopes=[analytics.READONLY_SCOPE])
        manager.save(self.p2,credential_json,email="two@example.test",channel_id=store.get_profile(self.p2)["youtube_channel_id"],scopes=[analytics.READONLY_SCOPE])
        first_path=self.root/"analytics-vault"/self.p1/"youtube-analytics.dpapi"
        self.assertTrue(first_path.is_file())
        self.assertNotIn(b"mock-refresh",first_path.read_bytes())
        self.assertEqual("one@example.test",manager.read(self.p1)["email"])
        self.assertEqual("two@example.test",manager.read(self.p2)["email"])
        self.assertNotEqual(manager.read(self.p1)["channel_id"],manager.read(self.p2)["channel_id"])

    def test_suggestions_are_explicit_hypotheses_not_causal_claims(self):
        result=analytics.build_suggestions({"videos":{"rows":[{"views":2},{"views":10}]},"daily":{"rows":[{"views":2}]}})
        self.assertEqual("measured_fact",result[0]["kind"])
        self.assertTrue(any("가설" in row["text"] or "단정할 수 없습니다" in row["text"] for row in result if row["kind"]=="hypothesis"))


if __name__=="__main__":
    unittest.main()
