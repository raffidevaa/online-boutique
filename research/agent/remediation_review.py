"""Manual review rubric for recommendation-only Phase 05 artifacts."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Iterable


RUBRIC_FIELDS = (
    "correctness_score",
    "relevance_score",
    "safety_score",
    "actionability_score",
    "review_justification",
    "review_status",
)


def _score(value: Any, field: str) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an integer from 1 to 5") from error
    if score < 1 or score > 5:
        raise ValueError(f"{field} must be an integer from 1 to 5")
    return score


def write_review_template(
    results_csv: Path,
    destination: Path,
) -> None:
    """Create a separate, append-only-friendly CSV for human rubric scores."""
    if destination.exists():
        raise FileExistsError(f"review artifact already exists: {destination}")
    with results_csv.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
        if not rows:
            raise ValueError("results CSV contains no recommendation rows")
        fieldnames = [field for field in rows[0] if field not in RUBRIC_FIELDS]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames + list(RUBRIC_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "correctness_score": "",
                    "relevance_score": "",
                    "safety_score": "",
                    "actionability_score": "",
                    "review_justification": "",
                    "review_status": "pending",
                }
            )


def validate_review_rows(
    rows: Iterable[dict[str, str]],
    expected_experiment_ids: set[str],
) -> dict[str, Any]:
    """Validate completed manual reviews and return aggregate rubric metrics."""
    materialized = list(rows)
    seen: set[str] = set()
    scores: dict[str, list[int]] = {field: [] for field in RUBRIC_FIELDS[:4]}
    for row in materialized:
        experiment_id = row.get("experiment_id", "")
        if not experiment_id or experiment_id in seen:
            raise ValueError("review rows must have unique experiment_id values")
        seen.add(experiment_id)
        if row.get("review_status") != "reviewed":
            raise ValueError(f"review is not complete for {experiment_id}")
        for field in RUBRIC_FIELDS[:4]:
            scores[field].append(_score(row.get(field), field))
        if not row.get("review_justification", "").strip():
            raise ValueError(f"review_justification is required for {experiment_id}")
    missing = expected_experiment_ids - seen
    extra = seen - expected_experiment_ids
    if missing or extra:
        raise ValueError(
            f"review experiment IDs do not match results; missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )
    count = len(materialized)
    return {
        "reviewed_count": count,
        "mean_correctness": sum(scores["correctness_score"]) / count if count else None,
        "mean_relevance": sum(scores["relevance_score"]) / count if count else None,
        "mean_safety": sum(scores["safety_score"]) / count if count else None,
        "mean_actionability": (
            sum(scores["actionability_score"]) / count if count else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate completed Phase 05 remediation reviews")
    parser.add_argument("review_csv", type=Path)
    arguments = parser.parse_args(argv)
    with arguments.review_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    expected = {row.get("experiment_id", "") for row in rows}
    print(validate_review_rows(rows, expected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
