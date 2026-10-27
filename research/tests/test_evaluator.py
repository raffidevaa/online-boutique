from datetime import UTC, datetime
import csv
import json
from pathlib import Path
import tempfile
import unittest

from research.agent.evaluator import BatchEvaluator, read_eligible_runs
from research.utils import ResearchConfig


class RecordingProvider:
    name = "recording"

    def __init__(self, actions: list[str]) -> None:
        self.actions = list(actions)
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages, actions):  # type: ignore[no-untyped-def]
        self.messages.append([dict(message) for message in messages])
        action = self.actions.pop(0) if self.actions else "not_allowed()"
        return f"```\n{action}\n```"


class EvaluatorTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _make_run(
        self,
        root: Path,
        experiment_id: str,
        scenario_id: str,
        service: str,
        category: str,
        fault_type: str,
    ) -> None:
        run = root / "experiments" / "runs" / experiment_id
        truth = {
            "experiment_id": experiment_id,
            "scenario_id": scenario_id,
            "root_cause_service": service,
            "fault_category": category,
            "fault_type": fault_type,
            "target_service": service,
            "injection_start": "2026-01-01T00:00:00+00:00",
            "injection_end": "2026-01-01T00:01:00+00:00",
            "severity": "medium",
        }
        self._write_json(run / "ground_truth.json", truth)
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

    def test_batch_uses_only_eligible_rows_and_withholds_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_run(
                root,
                "EXP-ELIGIBLE",
                "FI-RES-MEM-01",
                "cartservice",
                "resource",
                "memory_pressure",
            )
            self._make_run(
                root,
                "EXP-INELIGIBLE",
                "FI-RES-MEM-01",
                "cartservice",
                "resource",
                "memory_pressure",
            )
            self._make_run(
                root,
                "EXP-ELIGIBLE-2",
                "FI-RES-MEM-01",
                "cartservice",
                "resource",
                "memory_pressure",
            )
            index = root / "experiments" / "index.csv"
            with index.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "experiment_id",
                        "scenario_id",
                        "run_path",
                        "confounder_affected",
                        "eligible_for_evaluation",
                    ),
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "experiment_id": "EXP-ELIGIBLE",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "experiments/runs/EXP-ELIGIBLE",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "true",
                    }
                )
                writer.writerow(
                    {
                        "experiment_id": "EXP-ELIGIBLE-2",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "experiments/runs/EXP-ELIGIBLE-2",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "true",
                    }
                )
                writer.writerow(
                    {
                        "experiment_id": "EXP-INELIGIBLE",
                        "scenario_id": "FI-RES-MEM-01",
                        "run_path": "experiments/runs/EXP-INELIGIBLE",
                        "confounder_affected": "false",
                        "eligible_for_evaluation": "false",
                    }
                )

            config = self._config(root)
            providers: list[RecordingProvider] = []

            def provider_factory(_config: ResearchConfig) -> RecordingProvider:
                provider = RecordingProvider(
                    [
                        'get_incident_summary(window="incident")',
                        'submit({"system_level": "resource", "fault_type": "memory_pressure", '
                        '"faulty_components": ["cartservice"]})',
                    ]
                )
                providers.append(provider)
                return provider

            runs = read_eligible_runs(index, config)
            self.assertEqual(
                [item.experiment_id for item in runs], ["EXP-ELIGIBLE", "EXP-ELIGIBLE-2"]
            )
            output = root / "evaluation"
            summary = BatchEvaluator(
                config,
                provider_factory=provider_factory,
                output_dir=output,
                max_steps=3,
            ).evaluate(runs)

            self.assertEqual(summary["eligible_run_count"], 2)
            self.assertEqual(summary["processed_run_count"], 2)
            self.assertEqual(summary["failed_run_count"], 0)
            self.assertTrue(summary["all_eligible_runs_processed"])
            self.assertEqual(summary["metrics"]["combined_rca_accuracy"], 1.0)
            trace = (output / "session-traces" / "EXP-ELIGIBLE.json").read_text()
            self.assertNotIn('"root_cause_service"', trace)
            prompt = json.dumps(providers[0].messages)
            self.assertNotIn("cartservice", prompt)
            # The normalized taxonomy is intentionally part of the action contract, so
            # a fault type may appear as an allowed choice.  The actual evaluator label
            # (service/category/type fields) is not copied into the prompt.
            self.assertNotIn('"root_cause_service"', prompt)

    def test_batch_keeps_failure_row_and_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_run(
                root,
                "EXP-BROKEN",
                "FI-NET-LOSS-01",
                "paymentservice",
                "network",
                "packet_loss",
            )
            index = root / "experiments" / "index.csv"
            index.parent.mkdir(parents=True, exist_ok=True)
            index.write_text(
                "experiment_id,scenario_id,run_path,confounder_affected,eligible_for_evaluation\n"
                "EXP-BROKEN,FI-NET-LOSS-01,experiments/runs/EXP-BROKEN,false,true\n",
                encoding="utf-8",
            )
            config = self._config(root)
            runs = read_eligible_runs(index, config)
            summary = BatchEvaluator(
                config,
                provider_factory=lambda _config: RecordingProvider(["not_allowed()"]),
                output_dir=root / "evaluation",
                max_steps=1,
            ).evaluate(runs)
            self.assertEqual(summary["processed_run_count"], 1)
            self.assertEqual(summary["failed_run_count"], 0)
            with (root / "evaluation" / "evaluation-results.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["termination_reason"], "step_budget_exhausted")
            self.assertEqual(rows[0]["valid_submission"], "False")


if __name__ == "__main__":
    unittest.main()
