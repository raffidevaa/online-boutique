import csv
import json
from pathlib import Path
import tempfile
import unittest

from research.agent.final_evaluator import CanonicalPaths, aggregate, _metrics


class FinalEvaluatorTests(unittest.TestCase):
    def _write_csv(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def _write_run(self, root: Path, experiment_id: str) -> None:
        run = root / "experiments" / "runs" / experiment_id
        run.mkdir(parents=True, exist_ok=True)
        (run / "ground_truth.json").write_text(
            json.dumps({"injection_start": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )

    def test_metrics_include_accuracy_and_exact_time_counts(self) -> None:
        rows = [
            {
                "rca_valid_submission": True,
                "service_top1_correct": True,
                "service_top3_correct": True,
                "fault_category_correct": True,
                "fault_type_correct": False,
                "combined_rca_correct": False,
                "recommendation_valid": True,
                "recommendation_only": True,
                "recommendation_acceptable": True,
                "end_to_end_valid": False,
                "end_to_end_acceptable": False,
                "correctness_score": "4",
                "relevance_score": "5",
                "safety_score": "4",
                "actionability_score": "3",
                "mttd_exact_seconds": 10.0,
                "mttr_exact_seconds": None,
                "mttd_proxy_tta_seconds": 11.0,
                "mttr_proxy_tta_seconds": 12.0,
            }
        ]
        metrics = _metrics(rows)
        self.assertEqual(metrics["run_count"], 1)
        self.assertEqual(metrics["service_top3_accuracy"], 1.0)
        self.assertEqual(metrics["fault_type_accuracy"], 0.0)
        self.assertEqual(metrics["mean_mttd_exact_seconds"], 10.0)
        self.assertIsNone(metrics["mean_mttr_exact_seconds"])
        self.assertEqual(metrics["exact_mttd_count"], 1)
        self.assertEqual(metrics["exact_mttr_count"], 0)

    def test_aggregate_filters_ineligible_rows_and_uses_exact_submission_times(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_run(root, "EXP-ELIGIBLE")
            index = root / "experiments" / "index.csv"
            self._write_csv(
                index,
                [
                    {
                        "experiment_id": "EXP-ELIGIBLE",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "experiments/runs/EXP-ELIGIBLE",
                        "fault_category": "resource",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "true",
                    },
                    {
                        "experiment_id": "EXP-SKIPPED",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "experiments/runs/EXP-SKIPPED",
                        "fault_category": "resource",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "false",
                    },
                ],
            )
            phase04 = root / "phase04"
            phase05 = root / "phase05"
            self._write_csv(
                phase04 / "evaluation-results.csv",
                [
                    {
                        "experiment_id": "EXP-ELIGIBLE",
                        "valid_submission": "True",
                        "service_top1_correct": "True",
                        "service_top3_correct": "True",
                        "fault_category_correct": "True",
                        "fault_type_correct": "True",
                        "combined_rca_correct": "True",
                        "tta_seconds": "11.0",
                    }
                ],
            )
            self._write_csv(
                phase05 / "remediation-results.csv",
                [
                    {
                        "experiment_id": "EXP-ELIGIBLE",
                        "recommendation_valid": "True",
                        "requires_approval": "True",
                        "execution_allowed": "False",
                        "tta_seconds": "12.0",
                    }
                ],
            )
            review = root / "review.csv"
            self._write_csv(
                review,
                [
                    {
                        "experiment_id": "EXP-ELIGIBLE",
                        "correctness_score": "4",
                        "relevance_score": "4",
                        "safety_score": "4",
                        "actionability_score": "4",
                        "review_justification": "safe",
                        "review_status": "reviewed",
                    }
                ],
            )
            traces04 = phase04 / "session-traces"
            traces05 = phase05 / "session-traces"
            traces04.mkdir()
            traces05.mkdir()
            (traces04 / "EXP-ELIGIBLE.json").write_text(
                json.dumps({"submitted_at": "2026-01-01T00:00:10Z"}),
                encoding="utf-8",
            )
            (traces05 / "EXP-ELIGIBLE-remediation.json").write_text(
                json.dumps({"submitted_at": "2026-01-01T00:00:20Z"}),
                encoding="utf-8",
            )

            result = aggregate(
                index_path=index,
                canonical=CanonicalPaths(phase04=phase04, phase05=phase05),
                review_path=review,
                run_root=root,
            )
            summary = result["summary"]
            self.assertEqual(summary["dataset"]["total_index_rows"], 2)
            self.assertEqual(summary["dataset"]["eligible_run_count"], 1)
            self.assertEqual(summary["dataset"]["processed_run_count"], 1)
            self.assertTrue(summary["dataset"]["all_eligible_runs_processed"])
            self.assertEqual(summary["headline_runtime_metrics"]["exact_mttd_count"], 1)
            self.assertEqual(summary["headline_runtime_metrics"]["exact_mttr_count"], 1)
            self.assertEqual(result["rows"][0]["mttd_exact_seconds"], 10.0)
            self.assertEqual(result["rows"][0]["mttr_exact_seconds"], 20.0)

    def test_missing_canonical_join_is_an_explicit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = root / "index.csv"
            self._write_csv(
                index,
                [
                    {
                        "experiment_id": "EXP-ONE",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "runs/EXP-ONE",
                        "fault_category": "resource",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "true",
                    },
                    {
                        "experiment_id": "EXP-TWO",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "runs/EXP-TWO",
                        "fault_category": "resource",
                        "confounder_affected": "true",
                        "eligible_for_evaluation": "true",
                    },
                    {
                        "experiment_id": "EXP-SKIPPED",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "runs/EXP-SKIPPED",
                        "fault_category": "resource",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "false",
                    },
                ],
            )
            phase04 = root / "phase04"
            phase05 = root / "phase05"
            row = {"experiment_id": "EXP-ONE", "valid_submission": "False"}
            self._write_csv(phase04 / "evaluation-results.csv", [row])
            self._write_csv(
                phase05 / "remediation-results.csv",
                [{"experiment_id": "EXP-ONE", "recommendation_valid": "False"}],
            )
            self._write_csv(
                root / "review.csv",
                [
                    {
                        "experiment_id": experiment_id,
                        "correctness_score": "3",
                        "relevance_score": "3",
                        "safety_score": "3",
                        "actionability_score": "3",
                        "review_justification": "reviewed",
                        "review_status": "reviewed",
                    }
                    for experiment_id in ("EXP-ONE", "EXP-TWO")
                ],
            )
            result = aggregate(
                index_path=index,
                canonical=CanonicalPaths(phase04=phase04, phase05=phase05),
                review_path=root / "review.csv",
                run_root=root,
            )
            self.assertEqual(result["summary"]["dataset"]["processed_run_count"], 2)
            self.assertEqual(result["summary"]["dataset"]["failed_join_count"], 1)
            failure = next(row for row in result["rows"] if row["experiment_id"] == "EXP-TWO")
            self.assertEqual(failure["evaluation_status"], "failure")
            self.assertIn("missing canonical result", failure["error"])


if __name__ == "__main__":
    unittest.main()
