import os
import sqlite3
import tempfile
import uuid
import unittest

from mcrs import network_mode, research_scheduler, store


class NetworkModeTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.gettempdir(), f"zisu-network-mode-{uuid.uuid4().hex}.db")
        store.initialize(self.db)
        self.jp = store.create_profile("JP profile", "JP", "MORNING_CHILL_RAP", path=self.db)
        self.kr = store.create_profile("KR profile", "KR", "CHILL_RAP", path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except (FileNotFoundError, PermissionError):
                pass

    def test_network_mode_is_profile_scoped_and_jp_only(self):
        self.assertEqual("default", store.get_profile(self.jp, self.db)["network_mode"])
        store.set_profile_network_mode(self.jp, "user_vpn", self.db)
        self.assertEqual("user_vpn", store.get_profile(self.jp, self.db)["network_mode"])
        self.assertEqual("default", store.get_profile(self.kr, self.db)["network_mode"])
        with self.assertRaises(ValueError):
            store.set_profile_network_mode(self.kr, "user_vpn", self.db)
        with self.assertRaises(ValueError):
            store.set_profile_network_mode(self.jp, "rotate_ip", self.db)

    def test_mode_cannot_change_while_profile_has_active_checkpoint(self):
        schedule = research_scheduler.create(
            research_scheduler.ScheduleConfig(self.jp, total_hours=2), self.db)
        with self.assertRaises(ValueError):
            store.set_profile_network_mode(self.jp, "user_vpn", self.db)
        research_scheduler.cancel(schedule["id"], self.jp, self.db)
        self.assertEqual("user_vpn", store.set_profile_network_mode(self.jp, "user_vpn", self.db))

    def test_actual_vpn_and_country_remain_unknown(self):
        view = network_mode.status({"network_mode": "user_vpn"}, signature_provider=lambda: "ephemeral")
        self.assertEqual("user_vpn", view["configured_mode"])
        self.assertEqual("available", view["interface_observation"])
        self.assertEqual("unknown", view["vpn_connection"])
        self.assertEqual("unknown", view["country"])
        self.assertTrue(network_mode.changed("before", "after"))
        self.assertFalse(network_mode.changed("", "after"))

    def test_existing_profile_rows_survive_additive_network_migration(self):
        path = os.path.join(tempfile.gettempdir(), f"zisu-network-migration-{uuid.uuid4().hex}.db")
        try:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,"
                           "market TEXT NOT NULL,genre TEXT NOT NULL,schedule TEXT NOT NULL DEFAULT '07:00,20:00',"
                           "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
                db.execute("INSERT INTO profiles(id,name,market,genre) VALUES('existing','old JP','JP','MORNING_CHILL_RAP')")
            store.initialize(path)
            profile = store.get_profile("existing", path)
            self.assertEqual(("old JP", "JP", "MORNING_CHILL_RAP", "default"),
                             (profile["name"], profile["market"], profile["genre"], profile["network_mode"]))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except (FileNotFoundError, PermissionError):
                    pass


if __name__ == "__main__":
    unittest.main()
