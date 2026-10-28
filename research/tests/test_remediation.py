from datetime import UTC, datetime
import csv
import json
from pathlib import Path
import tempfile
import unittest

from research.agent.evaluator import read_eligible_runs
from research.agent.remediation import validate_remediation
from research.agent.remediation_evaluator import RemediationBatchEvaluator
from research.agent.remediation_review import validate_review_rows, write_review_template
from research.utils import ResearchConfig


class RemediationTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _make_run(self, root: Path, experiment_id: str) -> None:
        run = root / "experiments" / "runs" / experiment_id
        self._write_json(
            run / "ground_truth.json",
            {
                "experiment_id": experiment_id,
                "scenario_id": "FI-RES-MEM-01",
                "root_cause_service": "cartservice",
                "fault_category": "resource",
                "fault_type": "memory_pressure",
                "injection_start": "2026-01-01T00:00:00+00:00",
                "injection_end": "2026-01-01T00:01:00+00:00",
            },
        )
        for phase in ("baseline", "incident"):
            for kind in ("metrics", "logs", "traces", "alerts"):
                self._write_json(run / phase / f"{kind}.json", {})

    def _config(self, root: Path) -> ResearchConfig:
        return ResearchConfig(
            research_root=root,
            scenario_directory=Path("research/generators/scenarios").resolve(),
            experiment_runs=root / "experiments" / "runs",
            experiment_index=root / "experiments" / "index.csv",
            evaluations_dir=root / "experiments" / "evaluations",
        )

    def test_recommendation_contract_is_read_only_and_allowlisted(self) -> None:
        answer = {
            "action_type": "restart_service",
            "target_service": "cartservice",
            "rationale": "Contain a suspected memory leak while investigating.",
            "evidence": ["Working set increased during the incident"],
            "expected_effect": "Restore service responsiveness temporarily.",
            "risk": "medium",
            "rollback_plan": "Do not apply automatically; verify recovery first.",
            "requires_approval": True,
            "execution_allowed": False,
        }
        validated = validate_remediation(answer, {"cartservice"})
        self.assertFalse(validated["execution_allowed"])
        with self.assertRaises(ValueError):
            validate_remediation({**answer, "execution_allowed": True}, {"cartservice"})
        with self.assertRaises(ValueError):
            validate_remediation({**answer, "action_type": "run_shell"}, {"cartservice"})

    def test_two_stage_evaluator_withholds_truth_and_writes_review_template(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_run(root, "EXP-REMEDIATION")
            index = root / "experiments" / "index.csv"
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text(
                "experiment_id,scenario_id,run_path,confounder_affected,eligible_for_evaluation\n"
                "EXP-REMEDIATION,FI-RES-MEM-01,experiments/runs/EXP-REMEDIATION,false,true\n",
                encoding="utf-8",
            )
            config = self._config(root)
            runs = read_eligible_runs(index, config)
            output = root / "evaluation"
            summary = RemediationBatchEvaluator(
                config,
                output_dir=output,
                max_steps=3,
            ).evaluate(runs)
            self.assertEqual(summary["processed_run_count"], 1)
            self.assertEqual(summary["failed_run_count"], 0)
            self.assertEqual(summary["metrics"]["valid_recommendation_rate"], 1.0)
            self.assertEqual(summary["metrics"]["recommendation_only_rate"], 1.0)
            remediation_trace = (
                output / "session-traces" / "EXP-REMEDIATION-remediation.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn('"root_cause_service"', remediation_trace)
            with (output / "remediation-review.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                review_rows = list(csv.DictReader(handle))
            self.assertEqual(review_rows[0]["review_status"], "pending")

    def test_review_rubric_requires_all_scores_and_justification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results.csv"
            results.write_text("experiment_id,recommendation_valid\nEXP-1,True\n", encoding="utf-8")
            review = root / "review.csv"
            write_review_template(results, review)
            with review.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            row.update(
                {
                    "correctness_score": "4",
                    "relevance_score": "4",
                    "safety_score": "5",
                    "actionability_score": "4",
                    "review_justification": "Safe and relevant containment recommendation.",
                    "review_status": "reviewed",
                }
            )
            metrics = validate_review_rows([row], {"EXP-1"})
            self.assertEqual(metrics["reviewed_count"], 1)
            self.assertEqual(metrics["mean_safety"], 5.0)


if __name__ == "__main__":
    unittest.main()
