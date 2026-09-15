import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from research.generators import (
    FaultyImageInjector,
    SemanticValidationError,
    load_scenario,
    validate_semantic,
)
from research.orchestrator.faults import run_faulty_image
from research.service import CommandResult, ComposeApplication, ServiceCatalog, ServiceState
from research.utils import ResearchConfig


class FaultyImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ResearchConfig()
        self.injector = FaultyImageInjector(self.config)

    def test_manifest_covers_all_code_level_scenarios(self) -> None:
        scenarios = {
            path.stem
            for path in self.config.scenario_directory.glob("code-level-*.yaml")
        }
        self.assertEqual(len(self.injector.definitions), 5)
        for scenario_path in self.config.scenario_directory.glob("code-level-*.yaml"):
            scenario = load_scenario(scenario_path)
            definition = self.injector.definition(scenario)
            self.assertEqual(definition.service, scenario.target_service)
            self.assertEqual(definition.fault_type, scenario.fault_type)
        self.assertEqual(len(scenarios), 5)

    def test_plan_uses_manifest_image_and_patch_build_arg(self) -> None:
        scenario = load_scenario(
            self.config.scenario_directory / "code-level-04-missing-parameter-checkout.yaml"
        )
        plan = self.injector.plan(scenario)
        self.assertIn("--platform", plan.build_command)
        self.assertIn("linux/amd64", plan.build_command)
        self.assertIn("--build-arg", plan.build_command)
        self.assertIn("FAULT_PATCH=FI-CODE-PARAM-02.patch", plan.build_command)
        self.assertTrue(plan.definition.image.startswith("research/faulty/"))

    def test_exception_fault_plan_preserves_allowlisted_trigger(self) -> None:
        scenario = load_scenario(
            self.config.scenario_directory / "code-level-02-missing-exception-checkout.yaml"
        )
        self.assertEqual(self.injector.plan(scenario).definition.trigger, "missing_exception_handler")

    @patch("research.generators.faulty_image.subprocess.run")
    def test_build_is_explicit_and_inspects_result(self, run: object) -> None:
        scenario = load_scenario(
            self.config.scenario_directory / "code-level-01-incorrect-return-currency.yaml"
        )
        assert hasattr(run, "side_effect")
        run.side_effect = [
            subprocess.CompletedProcess(("docker",), 0, "built", ""),
            subprocess.CompletedProcess(
                ("docker",),
                0,
                '{"Id":"sha256:test","RepoDigests":[],"Os":"linux","Architecture":"amd64"}',
                "",
            ),
        ]
        result = self.injector.build(scenario)
        self.assertEqual(result["status"], "built")
        self.assertEqual(result["image_id"], "sha256:test")
        self.assertEqual(result["platform"], "linux/amd64")
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[0:2], ["docker", "build"])
        self.assertNotIn("--pull", command)

    def test_semantic_validators_accept_expected_observations(self) -> None:
        observations = {
            "currency_return": {"baseline_value": 10, "incident_value": 11},
            "missing_exception": {
                "baseline_error_count": 0,
                "incident_error_count": 2,
                "unhandled_exception": True,
            },
            "incorrect_parameter": {
                "downstream_status": "UNKNOWN",
                "observed_parameter": "INVALID",
                "expected_parameter": "USD",
            },
            "missing_parameter": {
                "downstream_status": "UNKNOWN",
                "missing_parameter": "to_code",
            },
            "missing_function_call": {
                "checkout_succeeded": True,
                "cart_items_after_checkout": 1,
            },
        }
        for validator, observation in observations.items():
            self.assertEqual(validate_semantic(validator, observation).status, "passed")

    def test_semantic_validator_rejects_unproven_fault(self) -> None:
        with self.assertRaises(SemanticValidationError):
            validate_semantic(
                "missing_function_call",
                {"checkout_succeeded": True, "cart_items_after_checkout": 0},
            )


class FaultOverrideTests(unittest.TestCase):
    def test_override_accepts_only_target_image(self) -> None:
        config = ResearchConfig()
        scenario = load_scenario(
            config.scenario_directory / "code-level-01-incorrect-return-currency.yaml"
        )
        definition = FaultyImageInjector(config).definition(scenario)
        application = ComposeApplication(config)
        resolved = json.dumps(
            {"services": {"currencyservice": {"image": definition.image}}}
        )
        with patch.object(
            application,
            "_run",
            side_effect=[
                CommandResult("", "", 0),
                CommandResult(resolved, "", 0),
            ],
        ):
            application.validate_fault_override(
                definition.override, "currencyservice", definition.image
            )

    def test_code_run_restores_baseline_and_marks_missing_semantic_observation(self) -> None:
        config = ResearchConfig()
        scenario = load_scenario(
            config.scenario_directory / "code-level-05-missing-function-call-checkout.yaml"
        )
        definition = FaultyImageInjector(config).definition(scenario)

        class FakeApplication:
            def __init__(self, _: ResearchConfig) -> None:
                self.catalog = ServiceCatalog.load(config.service_metadata)

            def validate(self) -> None:
                return None

            def validate_readiness(self) -> dict[str, ServiceState]:
                return {
                    "checkoutservice": ServiceState(
                        service="checkoutservice",
                        container_id="checkout-container",
                        image=definition.baseline_image,
                        status="running",
                        health="healthy",
                        restart_count=0,
                        memory_limit=1,
                        nano_cpus=1,
                    )
                }

            def validate_fault_override(self, *_: object) -> None:
                return None

            def apply_fault_override(self, *_: object) -> CommandResult:
                return CommandResult("applied", "", 0)

            def restore_service(self, _: str) -> CommandResult:
                return CommandResult("restored", "", 0)

            def wait_for_service_healthy(self, _: str) -> ServiceState:
                return ServiceState(
                    service="checkoutservice",
                    container_id="checkout-container",
                    image=definition.baseline_image,
                    status="running",
                    health="healthy",
                    restart_count=0,
                    memory_limit=1,
                    nano_cpus=1,
                )

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
                self.real = FaultyImageInjector(config)

            def plan(self, item: object):
                return self.real.plan(item)

            def check_image_available(self, _: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            config = ResearchConfig(experiment_runs=Path(temporary))
            with (
                patch("research.orchestrator.faults.ComposeApplication", FakeApplication),
                patch("research.orchestrator.faults.Observer", FakeObserver),
                patch("research.orchestrator.faults.FaultyImageInjector", FakeInjector),
                patch("research.orchestrator.faults.time.sleep"),
            ):
                result = run_faulty_image(scenario, config)
            run_path = Path(result["run_path"])
            self.assertEqual(result["status"], "completed_unvalidated")
            self.assertTrue((run_path / "semantic-validation.json").exists())
            self.assertIn(
                "baseline_restore_finished",
                (run_path / "events.jsonl").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
