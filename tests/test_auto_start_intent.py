import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcrs import store
from mcrs.gui import Studio
from tests.support import WorkspaceTemp


class AutoStartIntentTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        self.db = self.root / "autostart.sqlite"
        store.initialize(self.db)
        self._old_data_dir = os.environ.get("ZISU_DATA_DIR")
        os.environ["ZISU_DATA_DIR"] = str(self.root)
        store.initialize()
        self.profile = store.create_profile("Auto start KR", "KR", "MORNING_CHILL_RAP", path=self.db)
        self.live_profile = store.create_profile("Auto start live KR", "KR", "MORNING_CHILL_RAP")
        self.settings = {
            "total_hours": 8, "research_minutes": 20, "break_minutes": 20,
            "observation_slots": 10, "search_calls": 3,
            "provider_id": "disabled", "paid_ai": False, "ai_calls": 0,
            "market": "KR", "genre": "MORNING_CHILL_RAP", "channel_id": "",
        }

    def tearDown(self):
        if self._old_data_dir is None:
            os.environ.pop("ZISU_DATA_DIR", None)
        else:
            os.environ["ZISU_DATA_DIR"] = self._old_data_dir
        self.temp.__exit__(None, None, None)

    def test_explicit_request_is_durable_and_repeated_request_does_not_reset_state(self):
        first = store.create_vph_auto_start_intent("request_001", self.settings, self.db)
        store.update_vph_auto_start_intent("request_001", "blocked", self.db, reason="auth")
        repeated = store.create_vph_auto_start_intent("request_001", self.settings, self.db)
        self.assertEqual("pending", first["status"])
        self.assertEqual("blocked", repeated["status"])
        self.assertEqual("auth", repeated["reason"])

    def test_claim_is_profile_scoped_and_settings_snapshot_is_fixed(self):
        store.create_vph_auto_start_intent("request_002", self.settings, self.db)
        claimed = store.claim_vph_auto_start_intent("request_002", self.profile, self.settings, self.db)
        self.assertEqual((self.profile, "claimed", "KR", "MORNING_CHILL_RAP"),
                         (claimed["profile_id"], claimed["status"],
                          claimed["settings"]["market"], claimed["settings"]["genre"]))
        changed = dict(self.settings, search_calls=2)
        with self.assertRaises(ValueError):
            store.claim_vph_auto_start_intent("request_002", self.profile, changed, self.db)

    def test_paid_ai_over_budget_and_more_than_ten_slots_fail_closed(self):
        cases=(("request_101", {"paid_ai": True}), ("request_102", {"ai_calls": 1}),
               ("request_103", {"search_calls": 4}), ("request_104", {"observation_slots": 11}))
        for request_id, change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                store.create_vph_auto_start_intent(request_id, dict(self.settings, **change), self.db)

    def test_state_transitions_remain_a_single_request(self):
        store.create_vph_auto_start_intent("request_003", self.settings, self.db)
        store.claim_vph_auto_start_intent("request_003", self.profile, self.settings, self.db)
        started = store.update_vph_auto_start_intent("request_003", "started", self.db,
                                                       schedule_id="schedule_a")
        completed = store.update_vph_auto_start_intent("request_003", "completed", self.db)
        self.assertEqual(("started", "schedule_a"), (started["status"], started["schedule_id"]))
        self.assertEqual("completed", completed["status"])
        self.assertEqual([], store.list_open_vph_auto_start_intents(self.db))

    def test_gui_click_persists_once_and_duplicate_click_is_blocked(self):
        app=Studio.__new__(Studio)
        app.active_id=self.live_profile
        app.frames=[None]*8
        app.tabs=Mock()
        app._vph_research_error_profile=None
        app._vph_research_error_text=""
        app.current_profile=Mock(return_value=store.get_profile(self.live_profile))
        app._auto_start_settings=Mock(return_value=dict(self.settings))
        app._autopilot_start=Mock()
        app._auto_start_block=Mock(side_effect=lambda request_id,reason:
            store.update_vph_auto_start_intent(request_id,"blocked",reason=reason))
        with patch("mcrs.gui.uuid.uuid4",return_value=SimpleNamespace(hex="one_click")):
            app.start_vph_scheduled_research()
        intent=store.get_vph_auto_start_intent("gui_one_click")
        self.assertEqual(("claimed",self.live_profile),(intent["status"],intent["profile_id"]))
        app._autopilot_start.assert_called_once_with(vph_one_click=True,
            auto_request_id="gui_one_click",auto_settings=self.settings)
        with patch("mcrs.gui.uuid.uuid4",return_value=SimpleNamespace(hex="duplicate")):
            app.start_vph_scheduled_research()
        app._autopilot_start.assert_called_once()
        self.assertIsNone(store.get_vph_auto_start_intent("gui_duplicate"))

    def test_saved_profile_with_invalid_oauth_becomes_blocked_without_dispatch(self):
        request_id="request_auth_blocked"
        store.create_vph_auto_start_intent(request_id,self.settings)
        store.claim_vph_auto_start_intent(request_id,self.live_profile,self.settings)
        app=Studio.__new__(Studio)
        app._auto_start_request_id=""
        app._auto_start_bootstrap_done=False
        app.profiles=[store.get_profile(self.live_profile)]
        app.active_id=self.live_profile
        app._auto_start_block=Mock(side_effect=lambda rid,reason:
            store.update_vph_auto_start_intent(rid,"blocked",reason=reason))
        app._autopilot_start=Mock()
        with patch("mcrs.gui.oauth.connection_state",return_value={"status":"expired"}):
            self.assertTrue(app._automatic_research_bootstrap())
        self.assertEqual("blocked",store.get_vph_auto_start_intent(request_id)["status"])
        app._autopilot_start.assert_not_called()

    def test_environment_request_does_not_create_or_start_unapproved_work(self):
        request_id = "request_missing_1"
        app = Studio.__new__(Studio)
        app._auto_start_request_id = request_id
        app._auto_start_bootstrap_done = False
        app.profiles = [store.get_profile(self.live_profile)]
        app.active_id = self.live_profile
        with patch("mcrs.gui.oauth.connection_state", return_value={"status": "connected"}):
            self.assertFalse(app._automatic_research_bootstrap())
        self.assertIsNone(store.get_vph_auto_start_intent(request_id))
        self.assertEqual([], store.list_open_vph_auto_start_intents())

    def test_bootstrap_claims_exact_profile_and_dispatches_only_one_shot_engine(self):
        request_id = "request_dispatch_1"
        exact=dict(self.settings, market="KR", genre="MORNING_CHILL_RAP")
        store.create_vph_auto_start_intent(request_id,exact)
        store.claim_vph_auto_start_intent(request_id,self.live_profile,exact)
        app = Studio.__new__(Studio)
        app._auto_start_request_id = request_id
        app._auto_start_bootstrap_done = False
        app.profiles = [store.get_profile(self.live_profile)]
        app.active_id = self.live_profile
        app.resource_guard = Mock()
        app.resource_guard.check.return_value = SimpleNamespace(allow_new_work=True)
        app.tabs = Mock()
        app.frames = [None] * 8
        app._auto_start_settings = Mock(return_value=exact)
        app._auto_start_set_settings = Mock()
        app._autopilot_start = Mock()
        with (patch("mcrs.gui.oauth.connection_state", return_value={"status": "connected"}),
              patch("mcrs.gui.research_scheduler.list_active", return_value=[]),
              patch("mcrs.gui.vph_research.latest_active", return_value=None)):
            self.assertTrue(app._automatic_research_bootstrap())
        app._auto_start_set_settings.assert_called_once()
        app._autopilot_start.assert_called_once_with(vph_one_click=True,
            auto_request_id=request_id, auto_settings=exact)
        self.assertEqual("claimed", store.get_vph_auto_start_intent(request_id)["status"])
        self.assertFalse(app._automatic_research_bootstrap())
        app._autopilot_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
