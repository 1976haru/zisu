import json, os, sqlite3, unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
from mcrs import oauth, store
from mcrs.youtube_api import lookup_videos
from support import WorkspaceTemp

class OAuthAndMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__()
        self.previous=os.environ.get("ZISU_DATA_DIR"); os.environ["ZISU_DATA_DIR"]=str(self.root)
        self.db=self.root/"zisu_v2.db"; self.profile=store.create_profile("일본 조사 계정","JP","MORNING_CHILL_RAP")
    def tearDown(self):
        if self.previous is None: os.environ.pop("ZISU_DATA_DIR",None)
        else: os.environ["ZISU_DATA_DIR"]=self.previous
        self.temp.__exit__(None,None,None)
    def payload(self,scopes=None): return json.dumps({"token":"fake-test-token","client_id":"test","scopes":scopes or oauth.SCOPES})
    def test_v2_migration_preserves_rows(self):
        with sqlite3.connect(self.db) as db:
            for name in ("connected_email","connected_at","youtube_channel_id","youtube_channel_title"): db.execute(f"ALTER TABLE profiles DROP COLUMN {name}")
        store.initialize(); self.assertEqual("일본 조사 계정",store.get_profile(self.profile)["name"])
    def test_tokens_are_separate_by_profile(self):
        second=store.create_profile("한국 조사 계정","KR","CHILL_RAP")
        self.assertNotEqual(oauth.token_path(self.profile),oauth.token_path(second))
        oauth.save_connection(self.profile,{"channel_id":"UCtest1","channel_title":"Mio Morning","email":"research@example.test"},self.payload())
        self.assertTrue(oauth.is_connected(self.profile)); self.assertFalse(oauth.is_connected(second))
    def test_disconnect_keeps_profile_data(self):
        oauth.save_connection(self.profile,{"channel_id":"UCtest1","channel_title":"Mio Morning","email":"research@example.test"},self.payload())
        oauth.disconnect(self.profile); self.assertFalse(oauth.is_connected(self.profile)); self.assertEqual("일본 조사 계정",store.get_profile(self.profile)["name"])
    def test_account_without_channel(self):
        oauth.save_connection(self.profile,{"channel_id":"","channel_title":"조사용 계정 (YouTube 채널 없음)","email":"new@example.test"},self.payload())
        self.assertEqual("",store.get_profile(self.profile)["youtube_channel_id"]); self.assertTrue(oauth.is_connected(self.profile))
    def test_readonly_scope_required(self):
        with self.assertRaises(oauth.OAuthError): oauth.save_connection(self.profile,{"channel_id":"UCtest1","channel_title":"A"},self.payload(["email"]))
        self.assertFalse(oauth.is_connected(self.profile))
    def test_oauth_api_never_adds_key(self):
        calls=[]
        def fake(req,timeout=0):
            calls.append(req.full_url)
            return type("Response",(),{"__enter__":lambda s:s,"__exit__":lambda *a:None,"read":lambda s:b'{"items":[]}'})()
        with patch("mcrs.youtube_api.urlopen",fake): lookup_videos("should-not-leak","칠랩","KR",access_token="access-test")
        self.assertTrue(all("key=" not in parse_qs(urlparse(url).query) for url in calls))
    def test_token_path_rejects_traversal(self):
        with self.assertRaises(oauth.OAuthError): oauth.token_path("../secret")

if __name__ == "__main__": unittest.main()
