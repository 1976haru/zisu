"""Run a reproducible, clearly synthetic genre fixture and build a human review CSV.

Fixture metrics are regression signals only. Precision/recall are reported only
for explicit human labels entered in the output CSV; metadata never proves the
actual audio genre and this tool never makes a real-search quality claim.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcrs import genres

FIXTURE = ROOT / "tests" / "fixtures" / "v46_genre_eval.json"
LABELS = ("fit", "review", "exclude")
FIELDS = (
    "case_id", "dataset_kind", "genre", "market", "title", "description",
    "expected_fixture_verdict", "predicted_verdict", "fixture_match", "rule_score",
    "positive_signals", "conflict_signals", "uncertainty", "human_label", "human_note",
)


def _verdict(value: str) -> str:
    return {genres.FIT: "fit", genres.REVIEW: "review", genres.EXCLUDE: "exclude"}[value]


def _atomic_write(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _metrics(pairs: list[tuple[str, str]]) -> dict:
    confusion = {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}
    for actual, predicted in pairs:
        if actual not in LABELS or predicted not in LABELS:
            raise ValueError("Human labels and predictions must be fit, review, or exclude.")
        confusion[actual][predicted] += 1
    per_class = {}
    for label in LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[actual][label] for actual in LABELS if actual != label)
        fn = sum(confusion[label][predicted] for predicted in LABELS if predicted != label)
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision is not None and recall is not None and precision + recall else None)
        per_class[label] = {"precision": precision, "recall": recall, "f1": f1,
                            "support": sum(confusion[label].values())}
    total = len(pairs)
    macro_f1_values = [row["f1"] for row in per_class.values() if row["f1"] is not None]
    return {
        "sample_count": total,
        "accuracy": sum(confusion[label][label] for label in LABELS) / total if total else None,
        "strict_fit_precision": per_class["fit"]["precision"],
        "strict_fit_recall": per_class["fit"]["recall"],
        "macro_f1": sum(macro_f1_values) / len(macro_f1_values) if macro_f1_values else None,
        "per_class": per_class,
        "confusion_matrix_actual_by_predicted": confusion,
    }


def evaluate(fixture_path: Path, output_path: Path) -> dict:
    records = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not records:
        raise ValueError("Fixture must be a non-empty JSON list.")
    ids = [str(item.get("case_id", "")) for item in records]
    if any(not case_id for case_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("Fixture case_id values must be present and unique.")

    previous = {}
    if output_path.exists():
        with output_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                case_id = row.get("case_id", "")
                if not case_id or case_id in previous:
                    raise ValueError("Existing review CSV has missing or duplicate case_id values.")
                label = (row.get("human_label") or "").strip().casefold()
                if label and label not in LABELS:
                    raise ValueError(f"Invalid human label for {case_id}; expected fit/review/exclude or blank.")
                previous[case_id] = {"human_label": label, "human_note": row.get("human_note", "") or ""}
        unknown = set(previous) - set(ids)
        if unknown:
            raise ValueError("Existing review CSV contains rows absent from the fixture; refusing to discard them.")

    rows = []
    for item in records:
        required = ("genre", "market", "title", "description", "expected_fixture_verdict")
        if any(key not in item for key in required):
            raise ValueError(f"Fixture row {item['case_id']} is missing required fields.")
        expected = str(item["expected_fixture_verdict"])
        if expected not in LABELS:
            raise ValueError(f"Invalid synthetic fixture verdict for {item['case_id']}.")
        decision = genres.assess(item["genre"], item["title"], item["description"])
        predicted = _verdict(decision.decision)
        saved = previous.get(item["case_id"], {})
        rows.append({
            "case_id": item["case_id"], "dataset_kind": "synthetic_fixture_not_real_search",
            "genre": item["genre"], "market": item["market"], "title": item["title"],
            "description": item["description"], "expected_fixture_verdict": expected,
            "predicted_verdict": predicted, "fixture_match": str(predicted == expected).lower(),
            "rule_score": decision.score, "positive_signals": " | ".join(decision.positive_signals),
            "conflict_signals": " | ".join(decision.conflict_signals),
            "uncertainty": decision.uncertainty,
            "human_label": saved.get("human_label", ""), "human_note": saved.get("human_note", ""),
        })

    def write_csv(handle):
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    _atomic_write(output_path, write_csv)

    fixture_pairs = [(row["expected_fixture_verdict"], row["predicted_verdict"]) for row in rows]
    human_pairs = [(row["human_label"], row["predicted_verdict"]) for row in rows if row["human_label"]]
    per_market_genre = {}
    for row in rows:
        key = f"{row['market']}:{row['genre']}"
        group = per_market_genre.setdefault(key, {"count": 0, "fixture_matches": 0})
        group["count"] += 1
        group["fixture_matches"] += row["fixture_match"] == "true"
    return {
        "dataset_kind": "synthetic_fixture_not_real_search",
        "sample_count": len(rows), "fixture_match_rate": _metrics(fixture_pairs)["accuracy"],
        "fixture_confusion_matrix": _metrics(fixture_pairs)["confusion_matrix_actual_by_predicted"],
        "human_label_metrics": _metrics(human_pairs) if human_pairs else None,
        "human_labeled_count": len(human_pairs), "human_label_metrics_state": "available" if human_pairs else "not_available_no_human_labels",
        "per_market_genre_fixture_summary": per_market_genre,
        "review_csv": str(output_path),
        "real_search_precision_recall": "not_measured",
        "audio_content_verified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "genre-human-review.csv")
    parser.add_argument("--report", type=Path, help="Optional machine-readable JSON summary path.")
    args = parser.parse_args()
    result = evaluate(args.fixture, args.output)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        _atomic_write(args.report, lambda handle: handle.write(rendered + "\n"))
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
