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
