from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from research.generators import InjectorError, PumbaInjector, PumbaPlan, load_scenarios
from research.observer import ObserverError
from research.orchestrator.faults import FaultExecutionError, run_fault
from research.service import ServiceCatalog, ServiceState
from research.utils import ResearchConfig


class FaultCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ResearchConfig()
        self.scenarios = load_scenarios(self.config.scenario_directory)

    def test_complete_documented_catalog_is_valid_and_unique(self) -> None:
        self.assertEqual(len(self.scenarios), 15)
        identifiers = {scenario.scenario_id for scenario in self.scenarios}
        self.assertEqual(len(identifiers), 15)
        self.assertEqual(
            {scenario.execution_status for scenario in self.scenarios},
            {"ready_for_pilot", "experimental_unavailable", "deferred_faulty_image"},
        )

    def test_only_runtime_taxonomy_is_pumba_runnable(self) -> None:
        ready = [scenario for scenario in self.scenarios if scenario.runnable]
        self.assertEqual(len(ready), 8)
        self.assertEqual(
            {scenario.fault_type for scenario in ready},
            {"cpu_hog", "memory_pressure", "network_delay", "packet_loss"},
        )
        injector = PumbaInjector(self.config)
        for scenario in ready:
            plan = injector.plan(scenario, "container-id")
            self.assertEqual(plan.command[:3], ("docker", "run", "--rm"))
            self.assertNotIn("sh", plan.command)
            self.assertNotIn("bash", plan.command)

    def test_unavailable_cases_cannot_be_planned_for_injection(self) -> None:
        injector = PumbaInjector(self.config)
        unavailable = [scenario for scenario in self.scenarios if not scenario.runnable]
        self.assertEqual(len(unavailable), 7)
        for scenario in unavailable:
            with self.assertRaises(InjectorError):
                injector.plan(scenario, "container-id")

    def test_prepare_cpu_fault_requires_and_reports_both_images(self) -> None:
        scenario = next(item for item in self.scenarios if item.scenario_id == "FI-RES-CPU-02")
        injector = PumbaInjector(self.config)
        available = subprocess.CompletedProcess(("docker",), 0, "", "")
        with patch("research.generators.pumba.subprocess.run", return_value=available) as run:
            result = injector.prepare_images(scenario)
        self.assertEqual(
            [item["image"] for item in result["images"]],
            [self.config.pumba_image, self.config.pumba_stress_image],
        )
        self.assertEqual(result["pulled"], [])
        self.assertEqual(run.call_count, 2)

    def test_prepare_pulls_only_missing_images_when_explicitly_requested(self) -> None:
        scenario = next(item for item in self.scenarios if item.scenario_id == "FI-RES-CPU-02")
        injector = PumbaInjector(self.config)
        missing = subprocess.CompletedProcess(("docker",), 1, "", "missing")
        available = subprocess.CompletedProcess(("docker",), 0, "", "")
        with patch(
            "research.generators.pumba.subprocess.run",
            side_effect=[missing, available, available, available],
        ) as run:
            result = injector.prepare_images(scenario, pull=True)
        self.assertEqual(result["pulled"], [self.config.pumba_image])
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "pull", self.config.pumba_image])

    def test_catalog_paths_are_repository_files(self) -> None:
        for scenario in self.scenarios:
            self.assertIsNotNone(scenario.source_path)
            self.assertTrue(Path(scenario.source_path).is_file())

    def test_mocked_run_creates_withheld_ground_truth_and_evidence(self) -> None:
        scenario = next(item for item in self.scenarios if item.scenario_id == "FI-NET-LOSS-02")

        class FakeApplication:
            def __init__(self, config: ResearchConfig) -> None:
                self.catalog = ServiceCatalog.load(config.service_metadata)

            def validate(self) -> None:
                return None

            def validate_readiness(self) -> dict[str, ServiceState]:
                return {
                    "productcatalogservice": ServiceState(
                        service="productcatalogservice",
                        container_id="catalog-container",
                        image="catalog-image",
                        status="running",
                        health="healthy",
                        restart_count=0,
                        memory_limit=1,
                        nano_cpus=1,
                    )
                }

            def snapshot(self) -> dict[str, object]:
                return {"schema_version": 1, "services": {}}

        class FakeObserver:
            def __init__(self, _: ResearchConfig) -> None:
                return None

            def check_ready(self) -> None:
                return None

            def capture(self, *_: object) -> dict[str, object]:
                return {"metrics": {}, "logs": {}, "alerts": {}, "traces": {}}

        class FakeInjector:
            def __init__(self, _: ResearchConfig) -> None:
                return None

            def plan(self, item: object, container_id: str) -> PumbaPlan:
                return PumbaPlan("FI-NET-LOSS-02", "productcatalogservice", container_id, ("noop",))

            def check_images_available(self, _: object) -> None:
                return None

            def execute(self, _: PumbaPlan, __: int) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(("noop",), 0, "", "")

        with tempfile.TemporaryDirectory() as temporary:
            config = ResearchConfig(experiment_runs=Path(temporary))
            with (
                patch("research.orchestrator.faults.ComposeApplication", FakeApplication),
                patch("research.orchestrator.faults.Observer", FakeObserver),
                patch("research.orchestrator.faults.PumbaInjector", FakeInjector),
            ):
                result = run_fault(scenario, config)
            run_path = Path(result["run_path"])
            self.assertEqual(result["status"], "completed")
            self.assertTrue((run_path / "ground_truth.json").exists())
            self.assertTrue((run_path / "baseline" / "metrics.json").exists())
            self.assertTrue((run_path / "incident" / "alerts.json").exists())
            self.assertTrue((run_path / "recovery" / "logs.json").exists())
            self.assertTrue((run_path / "incident" / "traces.json").exists())

    def test_baseline_failure_does_not_inject_fault(self) -> None:
        scenario = next(item for item in self.scenarios if item.scenario_id == "FI-NET-LOSS-02")

        class FakeApplication:
            def __init__(self, config: ResearchConfig) -> None:
                self.catalog = ServiceCatalog.load(config.service_metadata)

            def validate(self) -> None:
                return None

            def validate_readiness(self) -> dict[str, ServiceState]:
                return {
                    "productcatalogservice": ServiceState(
                        service="productcatalogservice",
                        container_id="catalog-container",
                        image="catalog-image",
                        status="running",
                        health="healthy",
                        restart_count=0,
                        memory_limit=1,
                        nano_cpus=1,
                    )
                }

        class FakeObserver:
            def __init__(self, _: ResearchConfig) -> None:
                return None

            def check_ready(self) -> None:
                return None

            def capture(self, *_: object) -> dict[str, object]:
                raise ObserverError("Jaeger is unavailable")

        class FakeInjector:
            def __init__(self, _: ResearchConfig) -> None:
                return None

            def plan(self, item: object, container_id: str) -> PumbaPlan:
                return PumbaPlan("FI-NET-LOSS-02", "productcatalogservice", container_id, ("noop",))

            def check_images_available(self, _: object) -> None:
                return None

            def execute(self, _: PumbaPlan, __: int) -> subprocess.CompletedProcess[str]:
                raise AssertionError("fault injector must not run after baseline failure")

        with tempfile.TemporaryDirectory() as temporary:
            config = ResearchConfig(experiment_runs=Path(temporary))
            with (
                patch("research.orchestrator.faults.ComposeApplication", FakeApplication),
                patch("research.orchestrator.faults.Observer", FakeObserver),
                patch("research.orchestrator.faults.PumbaInjector", FakeInjector),
            ):
                with self.assertRaisesRegex(FaultExecutionError, "fault was not injected"):
                    run_fault(scenario, config)
            run_path = next(Path(temporary).iterdir())
            result = (run_path / "result.json").read_text(encoding="utf-8")
            self.assertIn('"status": "baseline_failed"', result)
            self.assertFalse((run_path / "ground_truth.json").exists())


if __name__ == "__main__":
    unittest.main()
