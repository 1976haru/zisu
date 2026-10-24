import csv
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evaluate_genre_fixture import FIXTURE, evaluate

from tests.support import WorkspaceTemp


class GenreEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = WorkspaceTemp()
        self.root = self.temp.__enter__()
        self.output = self.root / "review.csv"

    def tearDown(self):
        self.temp.__exit__(None, None, None)

    def test_synthetic_fixture_is_reproducible_and_never_claims_real_precision(self):
        result = evaluate(FIXTURE, self.output)
        self.assertEqual(24, result["sample_count"])
        self.assertEqual("synthetic_fixture_not_real_search", result["dataset_kind"])
        self.assertEqual("not_measured", result["real_search_precision_recall"])
        self.assertIsNone(result["human_label_metrics"])
        self.assertEqual("not_available_no_human_labels", result["human_label_metrics_state"])
        with self.output.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(24, len(rows))
        self.assertEqual({"JP", "KR"}, {row["market"] for row in rows})
        self.assertEqual({"MORNING_CHILL_RAP", "CHILL_RAP"}, {row["genre"] for row in rows})
        self.assertTrue(all(not row["human_label"] and not row["human_note"] for row in rows))
        self.assertTrue(all(row["dataset_kind"] == "synthetic_fixture_not_real_search" for row in rows))

    def test_human_labels_and_notes_survive_regeneration_and_metrics_are_separate(self):
        evaluate(FIXTURE, self.output)
        with self.output.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["human_label"] = "fit"
        rows[0]["human_note"] = "사람이 실제 청취 후 확인한 테스트 메모"
        with self.output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader(); writer.writerows(rows)
        report = evaluate(FIXTURE, self.output)
        with self.output.open("r", encoding="utf-8", newline="") as handle:
            regenerated = list(csv.DictReader(handle))
        self.assertEqual("fit", regenerated[0]["human_label"])
        self.assertEqual("사람이 실제 청취 후 확인한 테스트 메모", regenerated[0]["human_note"])
        self.assertEqual(1, report["human_labeled_count"])
        self.assertEqual(1, report["human_label_metrics"]["sample_count"])
        self.assertEqual("not_measured", report["real_search_precision_recall"])

    def test_existing_unknown_rows_are_not_silently_dropped(self):
        self.output.write_text("case_id,human_label,human_note\nold-case,fit,keep me\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "absent from the fixture"):
            evaluate(FIXTURE, self.output)


if __name__ == "__main__":
    unittest.main()
