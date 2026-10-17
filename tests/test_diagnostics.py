import unittest

from mcrs.diagnostics import collect, format_report
from mcrs import store
from support import WorkspaceTemp


class DiagnosticsTests(unittest.TestCase):
    def test_report_checks_database_without_identity_or_secret_values(self):
        with WorkspaceTemp() as root:
            database = root / "진단 test.db"
            profile = store.create_profile("진단 프로필", "KR", "CHILL_RAP", database)
            store.bind_youtube_channel(profile, "UCdiagnostic", "비공개 채널", "secret@example.test", database)
            text = format_report(collect(database))
        self.assertIn("database_integrity: ok", text)
        self.assertIn("profiles_rows: 1", text)
        self.assertNotIn("secret@example.test", text)
        self.assertNotIn("UCdiagnostic", text)
        self.assertNotIn(str(database), text)


if __name__ == "__main__":
    unittest.main()
