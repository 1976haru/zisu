import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from mcrs import oauth, store
from mcrs.credential_store import CredentialStore
from mcrs.oauth_clients import OAuthClientRegistry, OAuthClientRegistryError
from tests.support import WorkspaceTemp


class _Protector:
    def protect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0xA7 for byte in value)

    def unprotect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0xA7 for byte in value)


class OAuthManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=WorkspaceTemp(); self.root=self.temp.__enter__()
        self.previous=os.environ.get("ZISU_DATA_DIR")
        os.environ["ZISU_DATA_DIR"]=str(self.root/"isolated-data")
        self.first=store.create_profile("조사 JP","JP","MORNING_CHILL_RAP")
        self.second=store.create_profile("조사 KR","KR","CHILL_RAP")
        self.credential_factory=lambda directory: CredentialStore(directory,_Protector())
        self.registry=OAuthClientRegistry(self.root/"secure-clients",self.credential_factory)

    def tearDown(self):
        if self.previous is None: os.environ.pop("ZISU_DATA_DIR",None)
        else: os.environ["ZISU_DATA_DIR"]=self.previous
        self.temp.__exit__(None,None,None)

    def test_email_scope_alias_and_order_are_the_only_normalized_difference(self):
        alias=["openid", "email", oauth.READONLY_SCOPE]
        canonical=[oauth.READONLY_SCOPE, oauth.EMAIL_SCOPE, "openid"]
        self.assertEqual(oauth.validate_granted_scopes(alias),oauth.validate_granted_scopes(canonical))

    def test_missing_readonly_or_unrequested_scope_is_blocked(self):
        for scopes in (["openid",oauth.EMAIL_SCOPE], oauth.SCOPES+["drive.readonly"]):
            with self.subTest(scopes=scopes), self.assertRaises(oauth.OAuthError) as caught:
                oauth.validate_granted_scopes(scopes)
            self.assertEqual("scope_mismatch",caught.exception.code)

    def test_oauthlib_warning_accepts_email_alias_only(self):
        warning=Warning("private oauthlib detail")
        warning.old_scope=set(oauth.SCOPES)
        warning.new_scope={oauth.READONLY_SCOPE,"openid","email"}
        warning.token={"access_token":"mock-access-token","refresh_token":"mock-refresh-token"}
        self.assertIsNone(oauth._oauth_failure(warning,"token_exchange"))
        class Credentials:
            @classmethod
            def from_authorized_user_info(cls,info,scopes=None):
                return {"info":info,"scopes":scopes}
        config={"installed":{"client_id":"mock-client","client_secret":"mock-secret","token_uri":"https://example.test/token"}}
        recovered=oauth._credentials_from_scope_warning(warning,config,Credentials)
        self.assertEqual("mock-access-token",recovered["info"]["token"])
        self.assertEqual(set(oauth.SCOPES),set(recovered["scopes"]))

    def test_oauthlib_warning_with_missing_youtube_scope_is_rejected(self):
        warning=Warning("secret must not be displayed")
        warning.old_scope=set(oauth.SCOPES)
        warning.new_scope={"openid",oauth.EMAIL_SCOPE}
        warning.token={"access_token":"must-not-leak"}
        error=oauth._oauth_failure(warning,"token_exchange")
        self.assertEqual("scope_mismatch",error.code)
        self.assertNotIn("must-not-leak",str(error))

    def test_begin_connect_recovers_alias_warning_without_network_or_raw_error(self):
        warning=Warning("private code and header must never escape")
        warning.old_scope=set(oauth.SCOPES)
        warning.new_scope={oauth.READONLY_SCOPE,"openid","email"}
        warning.token={"access_token":"mock-access","refresh_token":"mock-refresh"}
        class Credentials:
            @classmethod
            def from_authorized_user_info(cls,info,scopes=None):
                value=type("Value",(),{})()
                value.token=info["token"]
                value.scopes=scopes
                value.to_json=lambda: json.dumps({"token":value.token,"client_id":"mock","scopes":scopes})
                return value
        class Flow:
            @classmethod
            def from_client_config(cls,*_args,**_kwargs): return cls()
            def run_local_server(self,**_kwargs): raise warning
        config={"installed":{"client_id":"mock-client","client_secret":"mock-secret","token_uri":"https://example.test/token"}}
        with patch.object(oauth,"_packages",return_value=(Flow,Credentials,None)), \
             patch.object(oauth,"_get_email",return_value="fake@example.test"), \
             patch.object(oauth,"_get_my_channels",return_value=[]):
            channels,token_json=oauth.begin_connect(client_config=config)
        self.assertEqual("fake@example.test",channels[0]["email"])
        self.assertEqual("",channels[0]["channel_id"])
        self.assertEqual(set(oauth.SCOPES),set(json.loads(token_json)["scopes"]))

    def test_malformed_oauthlib_warning_token_fails_closed(self):
        warning=Warning("response secret")
        warning.old_scope=set(oauth.SCOPES)
        warning.new_scope=set(oauth.SCOPES)
        warning.token={"refresh_token":"mock-only"}
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth._credentials_from_scope_warning(warning,{"installed":{}},object)
        self.assertEqual("scope_mismatch",caught.exception.code)
        self.assertNotIn("mock-only",str(caught.exception))

    def test_oauth_errors_are_allowlisted_and_safe(self):
        class OAuthFailure(Exception):
            error="access_denied"
            description="test user not on allowlist; secret token must stay private"
        error=oauth._oauth_failure(OAuthFailure(),"callback")
        self.assertEqual("test_user_required",error.code)
        self.assertNotIn("secret",str(error))
        class DeniedFailure(Exception): error="access_denied"; description="user canceled consent"
        self.assertEqual("access_denied",oauth._oauth_failure(DeniedFailure(),"callback").code)
        class RedirectFailure(Exception): error="redirect_uri_mismatch"; description="full callback URL secret"
        self.assertEqual("redirect_uri_mismatch",oauth._oauth_failure(RedirectFailure(),"callback").code)
        class ClientFailure(Exception): error="invalid_client"; description="client secret must stay private"
        self.assertEqual("invalid_client",oauth._oauth_failure(ClientFailure(),"callback").code)
        api_error=HTTPError("https://private.invalid",403,"private body",{},None)
        self.assertEqual("api_permission",oauth._oauth_failure(api_error,"youtube_api").code)
        api_error.close()
        self.assertEqual("token_storage",oauth._oauth_failure(PermissionError("private path"),"token_storage").code)
        self.assertEqual("local_callback",oauth._oauth_failure(ConnectionRefusedError(),"oauth_callback").code)
        self.assertEqual("network_timeout",oauth._oauth_failure(TimeoutError(),"oauth_callback").code)

    def test_registered_oauth_client_is_dpapi_encrypted_and_reusable(self):
        path=self.root/"fake-desktop-client.json"
        config={"installed":{"client_id":"fake-client.apps.googleusercontent.com","client_secret":"mock-secret-value",
                             "auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token"},
                "project_id":"fake-zisu-project"}
        path.write_text(json.dumps(config),encoding="utf-8")
        with self.assertRaises(OAuthClientRegistryError): self.registry.register_file(path)
        registered=self.registry.register_file(path,approved=True)
        self.assertEqual(registered["id"],self.registry.list_clients()[0]["id"])
        self.assertEqual(config,self.registry.load_config(registered["id"]))
        for stored in (self.root/"secure-clients").rglob("*.dpapi"):
            self.assertNotIn(b"mock-secret-value",stored.read_bytes())
        self.assertNotIn("mock-secret-value",(self.root/"secure-clients"/"index.json").read_text(encoding="utf-8"))

    def test_same_client_can_connect_two_accounts_without_token_copy(self):
        token_a=json.dumps({"token":"fake-a","client_id":"shared","scopes":oauth.SCOPES})
        token_b=json.dumps({"token":"fake-b","client_id":"shared","scopes":oauth.SCOPES})
        oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"JP","email":"a@example.test"},token_a)
        oauth.save_connection(self.second,{"channel_id":"UCbbb2","channel_title":"KR","email":"b@example.test"},token_b)
        self.assertNotEqual(oauth.token_path(self.first),oauth.token_path(self.second))
        self.assertEqual("fake-a",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        self.assertEqual("fake-b",json.loads(oauth.token_path(self.second).read_text(encoding="utf-8"))["token"])
        third=store.create_profile("JP 동일 계정","JP","MORNING_CHILL_RAP")
        oauth.save_connection(third,{"channel_id":"UCaaa1","channel_title":"JP duplicate profile","email":"a@example.test"},
                              json.dumps({"token":"fake-a-second-profile","client_id":"shared","scopes":oauth.SCOPES}))
        self.assertNotEqual(oauth.token_path(self.first),oauth.token_path(third))
        self.assertEqual("fake-a-second-profile",json.loads(oauth.token_path(third).read_text(encoding="utf-8"))["token"])

    def test_google_account_and_channel_registry_supports_many_channels_and_accounts(self):
        same_account_channel=json.dumps({"token":"fake-a-channel-2","client_id":"shared","scopes":oauth.SCOPES})
        oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"JP primary","email":"a@example.test"},
                              json.dumps({"token":"fake-a","client_id":"shared","scopes":oauth.SCOPES}))
        third=store.create_profile("JP second channel","JP","MORNING_CHILL_RAP")
        oauth.save_connection(third,{"channel_id":"UCaaa2","channel_title":"JP brand","email":"A@example.test"},same_account_channel)
        oauth.save_connection(self.second,{"channel_id":"UCbbb2","channel_title":"KR channel","email":"b@example.test"},
                              json.dumps({"token":"fake-b","client_id":"shared","scopes":oauth.SCOPES}))
        candidate={"video_id":"abcdefghijk","video_title":"Morning chill rap melody"}
        store.save_candidates(self.first,[candidate]); store.save_candidates(third,[candidate])
        store.update_candidate(self.first,"abcdefghijk","검토 중","JP-only memo","JP-only draft")
        store.update_candidate(third,"abcdefghijk","검토 중","second-channel memo","second-channel draft")
        store.record_ai_decision(self.first,"abcdefghijk",rule_verdict="검토",ai_verdict="review",
                                 proposed_verdict="cross_genre_review",evidence="profile A")
        store.record_ai_decision(third,"abcdefghijk",rule_verdict="검토",ai_verdict="review",
                                 proposed_verdict="cross_genre_review",evidence="profile B")

        jp=store.get_profile(self.first); jp2=store.get_profile(third); kr=store.get_profile(self.second)
        self.assertEqual(jp["google_account_id"],jp2["google_account_id"])
        self.assertNotEqual(jp["google_account_id"],kr["google_account_id"])
        accounts=store.list_google_accounts()
        self.assertEqual(2,len(accounts))
        shared=next(row for row in accounts if row["email"].casefold()=="a@example.test")
        self.assertEqual({"UCaaa1","UCaaa2"},{row["channel_id"] for row in shared["channels"]})
        self.assertEqual({self.first,third},{row["id"] for row in shared["profiles"]})
        self.assertNotEqual(oauth.token_path(self.first),oauth.token_path(third))
        self.assertEqual("fake-a",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        self.assertEqual("fake-a-channel-2",json.loads(oauth.token_path(third).read_text(encoding="utf-8"))["token"])
        self.assertEqual("JP-only memo",store.get_candidate(self.first,"abcdefghijk")["notes"])
        self.assertEqual("second-channel memo",store.get_candidate(third,"abcdefghijk")["notes"])
        self.assertEqual("profile A",store.list_genre_audit(self.first)[0]["evidence"])
        self.assertEqual("profile B",store.list_genre_audit(third)[0]["evidence"])

    def test_account_switch_without_explicit_confirmation_preserves_token_and_research(self):
        original=json.dumps({"token":"keep-old-token","client_id":"shared","scopes":oauth.SCOPES})
        oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"JP","email":"a@example.test"},original)
        store.save_candidates(self.first,[{"video_id":"abcdefghijk","video_title":"Morning chill rap melody"}])
        store.update_candidate(self.first,"abcdefghijk","검토 중","기존 메모","기존 초안")
        replacement=json.dumps({"token":"new-account-token","client_id":"shared","scopes":oauth.SCOPES})
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.save_connection(self.first,{"channel_id":"UCccc3","channel_title":"Other","email":"b@example.test"},replacement)
        self.assertEqual("account_mismatch",caught.exception.code)
        self.assertEqual("keep-old-token",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        self.assertEqual("기존 메모",store.get_candidate(self.first,"abcdefghijk")["notes"])
        self.assertEqual("a@example.test",store.get_profile(self.first)["connected_email"])
        with self.assertRaises(oauth.OAuthError) as blocked:
            oauth.save_connection(self.first,{"channel_id":"UCccc3","channel_title":"Other","email":"b@example.test"},
                                  replacement,allow_account_change=True)
        self.assertEqual("account_mismatch",blocked.exception.code)
        self.assertEqual("keep-old-token",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        self.assertEqual("a@example.test",store.get_profile(self.first)["connected_email"])
        self.assertEqual("기존 메모",store.get_candidate(self.first,"abcdefghijk")["notes"])
        self.assertEqual(1,len(store.list_google_accounts()))

    def test_missing_account_identity_is_rejected_before_token_write(self):
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"JP","email":""},
                                  json.dumps({"token":"unbound-token","client_id":"shared","scopes":oauth.SCOPES}))
        self.assertEqual("account_identity",caught.exception.code)
        self.assertFalse(oauth.is_connected(self.first))

    def test_active_profiled_session_blocks_account_channel_rebind(self):
        old=json.dumps({"token":"keep-active-token","client_id":"shared","scopes":oauth.SCOPES})
        oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"JP","email":"a@example.test"},old)
        with store.connect() as db:
            db.execute("INSERT INTO research_sessions(id,profile_id,market,genre,state,target,max_search_calls) "
                       "VALUES('active-mock',?,'JP','MORNING_CHILL_RAP','paused',10,20)",(self.first,))
        with self.assertRaises(oauth.OAuthError):
            oauth.save_connection(self.first,{"channel_id":"UCccc3","channel_title":"KR","email":"b@example.test"},
                                  json.dumps({"token":"new-token","client_id":"shared","scopes":oauth.SCOPES}),
                                  allow_account_change=True)
        self.assertEqual("keep-active-token",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        self.assertEqual("a@example.test",store.get_profile(self.first)["connected_email"])

    def test_account_channel_registry_migration_is_additive_and_repeatable(self):
        oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"JP","email":"a@example.test"},
                              json.dumps({"token":"migration-token","client_id":"shared","scopes":oauth.SCOPES}))
        store.save_candidates(self.first,[{"video_id":"abcdefghijk","video_title":"Morning chill rap melody"}])
        before=store.get_candidate(self.first,"abcdefghijk")
        with store.connect() as db:
            db.execute("DELETE FROM google_account_channels")
            db.execute("DELETE FROM google_accounts")
            db.execute("ALTER TABLE profiles DROP COLUMN google_account_id")
        store.initialize(); store.initialize()
        after=store.get_candidate(self.first,"abcdefghijk")
        self.assertEqual(before["video_title"],after["video_title"])
        self.assertEqual("a@example.test",store.get_profile(self.first)["connected_email"])
        self.assertTrue(store.get_profile(self.first)["google_account_id"])
        self.assertEqual("UCaaa1",store.list_google_accounts()[0]["channels"][0]["channel_id"])
        self.assertTrue(oauth.is_connected(self.first))

    def test_mismatched_connected_identity_requires_explicit_confirmation(self):
        original=json.dumps({"token":"old-test-token","client_id":"shared","scopes":oauth.SCOPES})
        oauth.save_connection(self.first,{"channel_id":"UCaaa1","channel_title":"Old","email":"a@example.test"},original)
        replacement=json.dumps({"token":"new-test-token","client_id":"shared","scopes":oauth.SCOPES})
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.save_connection(self.first,{"channel_id":"UCccc3","channel_title":"New","email":"b@example.test"},replacement)
        self.assertEqual("account_mismatch",caught.exception.code)
        self.assertEqual("old-test-token",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        oauth.save_connection(self.first,{"channel_id":"UCccc3","channel_title":"New","email":"b@example.test"},replacement,allow_account_change=True)
        self.assertEqual("new-test-token",json.loads(oauth.token_path(self.first).read_text(encoding="utf-8"))["token"])
        self.assertEqual("조사 JP",store.get_profile(self.first)["name"])

    def test_public_youtube_api_key_uses_existing_dpapi_store(self):
        vault=CredentialStore(self.root/"youtube-vault",_Protector())
        vault.save("youtube-data","mock-public-api-key")
        self.assertEqual("mock-public-api-key",vault.load("youtube-data"))
        vault.delete("youtube-data")
        self.assertIsNone(vault.load("youtube-data"))


if __name__=="__main__": unittest.main()
