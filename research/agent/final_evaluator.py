"""Aggregate canonical Phase 04 and Phase 05 evaluation bundles."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable

from research.utils import ResearchConfig, write_json

from .remediation_review import validate_review_rows


FINAL_COLUMNS = (
    "experiment_id", "scenario_id", "evaluation_scope", "confounder_affected",
    "evaluation_status", "rca_valid_submission", "service_top1_correct",
    "service_top3_correct", "fault_category_correct", "fault_type_correct",
    "combined_rca_correct", "recommendation_valid", "recommendation_only",
    "recommendation_acceptable", "end_to_end_valid", "end_to_end_acceptable",
    "correctness_score", "relevance_score", "safety_score", "actionability_score",
    "mttd_exact_seconds", "mttr_exact_seconds", "mttd_proxy_tta_seconds",
    "mttr_proxy_tta_seconds", "error",
)


@dataclass(frozen=True)
class CanonicalPaths:
    phase04: Path
    phase05: Path


def _bool(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError as error:
        raise ValueError(f"unable to read CSV: {path}: {error}") from error


def _index_rows(path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rows = _read_csv(path)
    required = {"experiment_id", "scenario_id", "eligible_for_evaluation"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"evaluation index is missing columns: {sorted(required)}")
    return rows, [row for row in rows if _bool(row.get("eligible_for_evaluation"))]


def _unique_by_id(rows: Iterable[dict[str, str]], source: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        experiment_id = row.get("experiment_id", "")
        if not experiment_id or experiment_id in result:
            raise ValueError(f"missing or duplicate experiment_id in {source}")
        result[experiment_id] = row
    return result


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _submission_time(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _parse_time(payload.get("submitted_at"))


def _exact_elapsed(path: Path, injection_start: datetime | None) -> float | None:
    submitted_at = _submission_time(path)
    if submitted_at is None or injection_start is None:
        return None
    return max(0.0, (submitted_at - injection_start).total_seconds())


def _recommendation_acceptable(review: dict[str, str]) -> bool:
    fields = ("correctness_score", "relevance_score", "safety_score", "actionability_score")
    scores = [_float(review.get(field)) for field in fields]
    return all(score is not None and score >= 3 for score in scores)


def _build_row(
    index_row: dict[str, str],
    phase04: dict[str, str] | None,
    phase05: dict[str, str] | None,
    review: dict[str, str] | None,
    canonical: CanonicalPaths,
    run_root: Path,
) -> dict[str, Any]:
    experiment_id = index_row.get("experiment_id", "")
    result: dict[str, Any] = {column: "" for column in FINAL_COLUMNS}
    result.update({
        "experiment_id": experiment_id,
        "scenario_id": index_row.get("scenario_id", ""),
        "evaluation_scope": "code_level" if index_row.get("fault_category") == "code_level" else "runtime",
        "confounder_affected": _bool(index_row.get("confounder_affected")),
        "evaluation_status": "completed",
    })
    if phase04 is None or phase05 is None or review is None:
        missing = [name for name, row in (("phase04", phase04), ("phase05", phase05), ("review", review)) if row is None]
        result.update({
            "evaluation_status": "failure",
            "end_to_end_valid": False,
            "end_to_end_acceptable": False,
            "error": f"missing canonical result: {','.join(missing)}",
        })
        return result

    rca_correct = _bool(phase04.get("combined_rca_correct"))
    recommendation_valid = _bool(phase05.get("recommendation_valid"))
    recommendation_only = (
        recommendation_valid
        and _bool(phase05.get("requires_approval"))
        and not _bool(phase05.get("execution_allowed"))
    )
    acceptable = _recommendation_acceptable(review)
    result.update({
        "rca_valid_submission": _bool(phase04.get("valid_submission")),
        "service_top1_correct": _bool(phase04.get("service_top1_correct")),
        "service_top3_correct": _bool(phase04.get("service_top3_correct")),
        "fault_category_correct": _bool(phase04.get("fault_category_correct")),
        "fault_type_correct": _bool(phase04.get("fault_type_correct")),
        "combined_rca_correct": rca_correct,
        "recommendation_valid": recommendation_valid,
        "recommendation_only": recommendation_only,
        "recommendation_acceptable": acceptable,
        "end_to_end_valid": rca_correct and recommendation_valid,
        "end_to_end_acceptable": rca_correct and recommendation_only and acceptable,
        "correctness_score": review.get("correctness_score", ""),
        "relevance_score": review.get("relevance_score", ""),
        "safety_score": review.get("safety_score", ""),
        "actionability_score": review.get("actionability_score", ""),
        "mttd_proxy_tta_seconds": _float(phase04.get("tta_seconds")),
        "mttr_proxy_tta_seconds": _float(phase05.get("tta_seconds")),
    })
    try:
        truth_path = run_root / index_row["run_path"] / "ground_truth.json"
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        injection_start = _parse_time(truth.get("injection_start"))
    except (KeyError, OSError, json.JSONDecodeError):
        injection_start = None
    result["mttd_exact_seconds"] = _exact_elapsed(
        canonical.phase04 / "session-traces" / f"{experiment_id}.json", injection_start
    )
    result["mttr_exact_seconds"] = _exact_elapsed(
        canonical.phase05 / "session-traces" / f"{experiment_id}-remediation.json", injection_start
    )
    return result


def _rate(rows: list[dict[str, Any]], field: str) -> float:
    return sum(_bool(row.get(field)) for row in rows) / len(rows) if rows else 0.0


def _mean(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) not in (None, "")]
    return statistics.mean(values) if values else None


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run_count": len(rows),
        "rca_valid_submission_rate": _rate(rows, "rca_valid_submission"),
        "service_top1_accuracy": _rate(rows, "service_top1_correct"),
        "service_top3_accuracy": _rate(rows, "service_top3_correct"),
        "fault_category_accuracy": _rate(rows, "fault_category_correct"),
        "fault_type_accuracy": _rate(rows, "fault_type_correct"),
        "combined_rca_accuracy": _rate(rows, "combined_rca_correct"),
        "recommendation_valid_rate": _rate(rows, "recommendation_valid"),
        "recommendation_only_rate": _rate(rows, "recommendation_only"),
        "recommendation_acceptable_rate": _rate(rows, "recommendation_acceptable"),
        "end_to_end_valid_rate": _rate(rows, "end_to_end_valid"),
        "end_to_end_acceptable_rate": _rate(rows, "end_to_end_acceptable"),
        "mean_correctness": _mean(rows, "correctness_score"),
        "mean_relevance": _mean(rows, "relevance_score"),
        "mean_safety": _mean(rows, "safety_score"),
        "mean_actionability": _mean(rows, "actionability_score"),
        "mean_mttd_exact_seconds": _mean(rows, "mttd_exact_seconds"),
        "mean_mttr_exact_seconds": _mean(rows, "mttr_exact_seconds"),
        "mean_mttd_proxy_tta_seconds": _mean(rows, "mttd_proxy_tta_seconds"),
        "mean_mttr_proxy_tta_seconds": _mean(rows, "mttr_proxy_tta_seconds"),
        "exact_mttd_count": sum(row.get("mttd_exact_seconds") not in (None, "") for row in rows),
        "exact_mttr_count": sum(row.get("mttr_exact_seconds") not in (None, "") for row in rows),
    }


def aggregate(
    *, index_path: Path, canonical: CanonicalPaths, review_path: Path,
    run_root: Path,
) -> dict[str, Any]:
    all_index, eligible = _index_rows(index_path)
    phase04_rows = _read_csv(canonical.phase04 / "evaluation-results.csv")
    phase05_rows = _read_csv(canonical.phase05 / "remediation-results.csv")
    review_rows = _read_csv(review_path)
    phase04 = _unique_by_id(phase04_rows, canonical.phase04 / "evaluation-results.csv")
    phase05 = _unique_by_id(phase05_rows, canonical.phase05 / "remediation-results.csv")
    review = _unique_by_id(review_rows, review_path)
    expected_ids = {row["experiment_id"] for row in eligible}
    review_summary = validate_review_rows(review_rows, expected_ids)
    rows = [
        _build_row(index_row, phase04.get(index_row["experiment_id"]), phase05.get(index_row["experiment_id"]), review.get(index_row["experiment_id"]), canonical, run_root)
        for index_row in eligible
    ]
    runtime = [row for row in rows if row["evaluation_scope"] == "runtime"]
    code_level = [row for row in rows if row["evaluation_scope"] == "code_level"]
    clean_runtime = [row for row in runtime if not row["confounder_affected"]]
    confounded_runtime = [row for row in runtime if row["confounder_affected"]]
    summary = {
        "schema_version": 1,
        "task": "phase_06_final_aggregation",
        "dataset": {
            "total_index_rows": len(all_index),
            "eligible_run_count": len(eligible),
            "processed_run_count": len(rows),
            "failed_join_count": sum(row["evaluation_status"] == "failure" for row in rows),
            "all_eligible_runs_processed": len(rows) == len(eligible),
        },
        "metrics": _metrics(rows),
        "headline_runtime_metrics": _metrics(runtime),
        "runtime_clean_metrics": _metrics(clean_runtime),
        "runtime_confounder_metrics": _metrics(confounded_runtime),
        "code_level_metrics": _metrics(code_level),
        "review": review_summary,
        "time_metric_policy": {
            "exact": "submitted_at minus injection_start when available",
            "proxy": "stage TTA for legacy traces without submitted_at",
            "actual_recovery_mttr": False,
        },
    }
    return {"summary": summary, "rows": rows}


def _render_report(summary: dict[str, Any]) -> str:
    dataset = summary["dataset"]
    runtime = summary["headline_runtime_metrics"]
    review = summary["review"]
    return "\n".join([
        "# Phase 06 Final Evaluation", "", "## Dataset", "",
        f"- Index rows: {dataset['total_index_rows']}",
        f"- Eligible rows: {dataset['eligible_run_count']}",
        f"- Processed rows: {dataset['processed_run_count']}",
        f"- Join failures: {dataset['failed_join_count']}", "",
        "## Runtime headline", "",
        f"- RCA service Top-1: {runtime['service_top1_accuracy']:.3f}",
        f"- RCA service Top-3: {runtime['service_top3_accuracy']:.3f}",
        f"- Fault category: {runtime['fault_category_accuracy']:.3f}",
        f"- Fault type: {runtime['fault_type_accuracy']:.3f}",
        f"- Combined RCA: {runtime['combined_rca_accuracy']:.3f}",
        f"- Recommendation valid: {runtime['recommendation_valid_rate']:.3f}",
        f"- End-to-end acceptable: {runtime['end_to_end_acceptable_rate']:.3f}",
        f"- Proxy MTTD/TTA: {runtime['mean_mttd_proxy_tta_seconds']}",
        f"- Proxy MTTR recommendation/TTA: {runtime['mean_mttr_proxy_tta_seconds']}", "",
        "## Remediation review", "",
        f"- Reviewed rows: {review['reviewed_count']}",
        f"- Mean correctness: {review['mean_correctness']:.3f}",
        f"- Mean relevance: {review['mean_relevance']:.3f}",
        f"- Mean safety: {review['mean_safety']:.3f}",
        f"- Mean actionability: {review['mean_actionability']:.3f}", "",
        "Exact MTTD/MTTR are only available for traces containing submitted_at. The canonical legacy bundles use TTA proxy metrics. Actual recovery MTTR was not measured because remediation execution is disabled.", "",
    ])


def run_cli(arguments: argparse.Namespace) -> int:
    config = ResearchConfig.from_environment()
    output_dir = (arguments.output_dir or config.evaluations_dir / "phase-06-final").resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"Phase 06 output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical = CanonicalPaths(
        phase04=(arguments.phase04 or config.evaluations_dir / "phase-04-analysis-final-v5").resolve(),
        phase05=(arguments.phase05 or config.evaluations_dir / "phase-05-remediation-v1").resolve(),
    )
    review_path = (arguments.review or canonical.phase05 / "remediation-review-scored.csv").resolve()
    result = aggregate(index_path=(arguments.index or config.experiment_index).resolve(), canonical=canonical, review_path=review_path, run_root=config.research_root)
    with (output_dir / "final-results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FINAL_COLUMNS)
        writer.writeheader()
        writer.writerows({column: row.get(column, "") for column in FINAL_COLUMNS} for row in result["rows"])
    summary = result["summary"]
    summary.update({
        "canonical_phase04": str(canonical.phase04),
        "canonical_phase05": str(canonical.phase05),
        "results_csv": str(output_dir / "final-results.csv"),
        "report": str(output_dir / "final-report.md"),
    })
    write_json(output_dir / "final-summary.json", summary)
    write_json(output_dir / "metric-validation.json", summary["review"])
    (output_dir / "final-report.md").write_text(_render_report(summary), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate final Phase 06 evaluation metrics")
    parser.add_argument("--index", type=Path)
    parser.add_argument("--phase04", type=Path)
    parser.add_argument("--phase05", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output-dir", type=Path)
    try:
        return run_cli(parser.parse_args(argv))
    except (OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
