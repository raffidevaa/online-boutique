import csv
import json
from pathlib import Path
import tempfile
import unittest

from research.orchestrator.dataset import build_dataset_index, write_dataset_index


class DatasetIndexTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _telemetry(self, run_path: Path) -> None:
        for phase in ("baseline", "incident", "recovery"):
            for name in ("metrics", "logs", "traces", "alerts"):
                payload: dict[str, object] = {}
                if name == "metrics":
                    payload = {
                        "queries": {
                            "container_cpu": {
                                "data": {
                                    "result": [
                                        {"metric": {"service": "loadgenerator"}}
                                    ]
                                }
                            }
                        }
                    }
                self._write_json(run_path / phase / f"{name}.json", payload)

    def _scenario(self, run_path: Path) -> None:
        self._write_json(
            run_path / "scenario.json",
            {"workload": {"generator": "loadgenerator"}},
        )

    def _ground_truth(
        self, experiment_id: str, category: str = "resource", service: str = "cartservice"
    ) -> dict[str, object]:
        return {
            "experiment_id": experiment_id,
            "scenario_id": "FI-TEST-01",
            "root_cause_service": service,
            "fault_category": category,
            "fault_type": "memory_pressure" if category == "resource" else "missing_parameter",
            "target_service": service,
            "injection_start": "2026-01-01T00:00:00+00:00",
            "injection_end": "2026-01-01T00:01:00+00:00",
            "severity": "medium",
        }

    def test_completed_runtime_run_is_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "research"
            run_path = root / "experiments" / "runs" / "EXP-002"
            self._write_json(run_path / "metadata.json", {"experiment_id": "EXP-002"})
            self._write_json(run_path / "result.json", {"status": "completed"})
            self._write_json(run_path / "ground_truth.json", self._ground_truth("EXP-002"))
            self._scenario(run_path)
            self._telemetry(run_path)

            row = build_dataset_index(run_path.parent, root)[0]

            self.assertEqual(row["semantic_validation_status"], "not_required")
            self.assertEqual(row["eligible_for_evaluation"], "true")
            self.assertEqual(row["exclusion_reason"], "")

    def test_code_fault_requires_passed_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "research"
            run_path = root / "experiments" / "runs" / "EXP-001"
            self._write_json(run_path / "metadata.json", {"experiment_id": "EXP-001"})
            self._write_json(run_path / "result.json", {"status": "completed"})
            self._write_json(
                run_path / "ground_truth.json",
                self._ground_truth("EXP-001", "code_level", "adservice"),
            )
            self._write_json(run_path / "semantic-validation.json", {"status": "failed"})
            self._scenario(run_path)
            self._telemetry(run_path)

            row = build_dataset_index(run_path.parent, root)[0]

            self.assertEqual(row["confounder_affected"], "true")
            self.assertEqual(row["eligible_for_evaluation"], "false")
            self.assertEqual(row["exclusion_reason"], "semantic_validation:failed")

    def test_cpu_fault_requires_host_impact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "research"
            run_path = root / "experiments" / "runs" / "EXP-CPU"
            truth = self._ground_truth("EXP-CPU")
            truth["fault_type"] = "cpu_hog"
            self._write_json(run_path / "metadata.json", {"experiment_id": "EXP-CPU"})
            self._write_json(run_path / "result.json", {"status": "completed"})
            self._write_json(run_path / "ground_truth.json", truth)
            self._scenario(run_path)
            self._telemetry(run_path)

            missing = build_dataset_index(run_path.parent, root)[0]
            self.assertEqual(missing["eligible_for_evaluation"], "false")
            self.assertIn("host_impact:missing", missing["exclusion_reason"])

            self._write_json(run_path / "host-impact.json", {"status": "not_flagged"})
            complete = build_dataset_index(run_path.parent, root)[0]
            self.assertEqual(complete["eligible_for_evaluation"], "true")

    def test_incomplete_run_is_indexed_with_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "research"
            run_path = root / "experiments" / "runs" / "EXP-003"
            self._write_json(
                run_path / "metadata.json",
                {"experiment_id": "EXP-003", "scenario_id": "FI-TEST-03"},
            )
            self._scenario(run_path)
            self._telemetry(run_path)
            (run_path / "incident" / "metrics.json").unlink()

            row = build_dataset_index(run_path.parent, root)[0]

            self.assertEqual(row["eligible_for_evaluation"], "false")
            self.assertIn("missing_result", row["exclusion_reason"])
            self.assertIn("missing_ground_truth", row["exclusion_reason"])
            self.assertIn("missing_incident_telemetry", row["exclusion_reason"])

    def test_writer_orders_rows_and_replaces_only_derived_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "research"
            runs = root / "experiments" / "runs"
            for experiment_id in ("EXP-002", "EXP-001"):
                run_path = runs / experiment_id
                self._write_json(run_path / "metadata.json", {"experiment_id": experiment_id})
                self._write_json(run_path / "result.json", {"status": "completed"})
                self._write_json(
                    run_path / "ground_truth.json", self._ground_truth(experiment_id)
                )
                self._scenario(run_path)
                self._telemetry(run_path)
            output = root / "experiments" / "index.csv"

            result = write_dataset_index(runs, root, output)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual([row["experiment_id"] for row in rows], ["EXP-001", "EXP-002"])
            self.assertEqual(result.total_runs, 2)
            self.assertEqual(result.eligible_runs, 2)


if __name__ == "__main__":
    unittest.main()
