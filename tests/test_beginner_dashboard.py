import json
import os
import unittest

from mcrs import store
from tests.support import WorkspaceTemp


class BeginnerDashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp();self.root=self.temp.__enter__()
        self.previous=os.environ.get("ZISU_DATA_DIR")
        os.environ["ZISU_DATA_DIR"]=str(self.root/"isolated-data")
        self.jp=store.create_profile("JP morning", "JP", "MORNING_CHILL_RAP")
        self.jp2=store.create_profile("JP second", "JP", "CHILL_RAP")
        self.kr=store.create_profile("KR lounge", "KR", "JAZZ_LOUNGE")
        store.bind_google_account_channel(self.jp,"UCdashboard1","Morning JP","same@example.test")
        store.bind_google_account_channel(self.jp2,"UCdashboard2","Second JP","SAME@example.test")
        store.bind_google_account_channel(self.kr,"UCdashboard3","KR Lounge","other@example.test")

    def tearDown(self):
        if self.previous is None:os.environ.pop("ZISU_DATA_DIR",None)
        else:os.environ["ZISU_DATA_DIR"]=self.previous
        self.temp.__exit__(None,None,None)

    def test_dashboard_groups_multiple_channels_by_account_without_credentials(self):
        accounts=store.list_google_accounts()
        self.assertEqual(2,len(accounts))
        same=next(row for row in accounts if row["email"].casefold()=="same@example.test")
        self.assertEqual({"UCdashboard1","UCdashboard2"},{row["channel_id"] for row in same["channels"]})
        self.assertEqual({self.jp,self.jp2},{row["id"] for row in same["profiles"]})
        self.assertNotIn("token",json.dumps(accounts).casefold())

    def test_dashboard_summary_is_scoped_to_profile_and_counts_only(self):
        row={"video_id":"dashboard01","video_title":"Morning chill rap with mellow vocals"}
        other={"video_id":"dashboard01","video_title":"Jazz lounge smooth trumpet jazz"}
        store.save_candidates(self.jp,[row]);store.save_candidates(self.kr,[other])
        store.update_candidate(self.jp,"dashboard01","검토 중","JP memo","JP draft")
        first=store.dashboard_summary(self.jp)
        second=store.dashboard_summary(self.kr)
        self.assertEqual(1,first["candidates"])
        self.assertEqual(1,second["candidates"])
        self.assertEqual("same@example.test",first["account_email"].casefold())
        self.assertEqual("other@example.test",second["account_email"])
        self.assertEqual("JP memo",store.get_candidate(self.jp,"dashboard01")["notes"])
        self.assertEqual("",store.get_candidate(self.kr,"dashboard01")["notes"])

    def test_dashboard_summary_missing_profile_is_empty(self):
        self.assertIsNone(store.dashboard_summary("not-a-profile"))


if __name__=="__main__":
    unittest.main()
