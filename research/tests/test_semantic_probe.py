from pathlib import Path
import unittest
from unittest.mock import patch
from unittest.mock import Mock
import tempfile

from research.generators import (
    ProbePhaseResult,
    build_semantic_observation,
    FaultyImageInjector,
    SemanticProbeError,
    load_scenario,
    run_semantic_probe,
    validate_semantic,
)
from research.orchestrator.faults import run_faulty_image
from research.service import ServiceCatalog, ServiceState
from research.utils import ResearchConfig


class _FakeSession:
    def request(self, method: str, path: str, form: object = None) -> dict[str, object]:
        if method == "GET" and path.startswith("/product/"):
            return {"method": method, "path": path, "status": 200, "latency_ms": 1.0,
                    "body_length": 42, "body": '<p class="product-price">€10.00</p>'}
        if method == "GET" and path == "/cart":
            return {"method": method, "path": path, "status": 200, "latency_ms": 1.0,
                    "body_length": 15, "body": "<h3>Cart (0)</h3>"}
        if method == "POST" and path == "/cart/checkout":
            return {"method": method, "path": path, "status": 200, "latency_ms": 1.0,
                    "body_length": 24, "body": "Your order is complete!"}
        return {"method": method, "path": path, "status": 200, "latency_ms": 1.0,
                "body_length": 0, "body": ""}


class _TransientCurrencySession:
    def __init__(self, state: dict[str, int]) -> None:
        self.state = state

    def request(self, method: str, path: str, form: object = None) -> dict[str, object]:
        if method == "POST" and path == "/setCurrency":
            self.state["set_currency_calls"] += 1
            if self.state["set_currency_calls"] == 1:
                return {
                    "method": method,
                    "path": path,
                    "status": 500,
                    "latency_ms": 1.0,
                    "body_length": 64,
                    "body": "could not retrieve currencies: code = Unavailable",
                }
        if method == "GET" and path.startswith("/product/"):
            return {
                "method": method,
                "path": path,
                "status": 200,
                "latency_ms": 1.0,
                "body_length": 42,
                "body": '<p class="product-price">€10.00</p>',
            }
        return {
            "method": method,
            "path": path,
            "status": 200,
            "latency_ms": 1.0,
            "body_length": 0,
            "body": "",
        }


class _StableCurrencyFailureSession:
    def request(self, method: str, path: str, form: object = None) -> dict[str, object]:
        return {
            "method": method,
            "path": path,
            "status": 500,
            "latency_ms": 1.0,
            "body_length": 20,
            "body": "stable business failure",
        }


class _CheckoutFailureSession:
    def __init__(self, state: dict[str, int]) -> None:
        self.state = state

    def request(self, method: str, path: str, form: object = None) -> dict[str, object]:
        if method == "POST" and path == "/cart/checkout":
            self.state["checkout_calls"] += 1
            return {
                "method": method,
                "path": path,
                "status": 500,
                "latency_ms": 1.0,
                "body_length": 32,
                "body": "code = Unavailable",
            }
        if method == "GET" and path == "/cart":
            return {
                "method": method,
                "path": path,
                "status": 200,
                "latency_ms": 1.0,
                "body_length": 15,
                "body": "<h3>Cart (0)</h3>",
            }
        return {
            "method": method,
            "path": path,
            "status": 200,
            "latency_ms": 1.0,
            "body_length": 0,
            "body": "",
        }


class SemanticProbeTests(unittest.TestCase):
    def config(self) -> ResearchConfig:
        return ResearchConfig(frontend_url="http://semantic-probe.test")

    def test_currency_probe_collects_displayed_value(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-01-incorrect-return-currency.yaml")
        )
        with patch("research.generators.semantic_probe._FrontendSession", return_value=_FakeSession()):
            result = run_semantic_probe(scenario, self.config(), "baseline")
        self.assertEqual(result.normalized["value"], "10.00")
        self.assertEqual(len(result.requests), 6)

    def test_currency_probe_retries_transient_dependency_failure(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-01-incorrect-return-currency.yaml")
        )
        state = {"set_currency_calls": 0}
        session = _TransientCurrencySession(state)
        with (
            patch("research.generators.semantic_probe._FrontendSession", return_value=session),
            patch("research.generators.semantic_probe.time.sleep") as sleep,
        ):
            result = run_semantic_probe(scenario, self.config(), "incident")
        self.assertEqual(result.normalized["value"], "10.00")
        self.assertEqual(result.normalized["retry_count"], 1)
        self.assertEqual(state["set_currency_calls"], 4)
        self.assertEqual(sleep.call_count, 1)
        self.assertTrue(result.requests[0]["retryable"])
        self.assertEqual(result.requests[0]["attempt"], 1)

    def test_stable_http_500_is_not_retried(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-01-incorrect-return-currency.yaml")
        )
        with (
            patch(
                "research.generators.semantic_probe._FrontendSession",
                return_value=_StableCurrencyFailureSession(),
            ),
            patch("research.generators.semantic_probe.time.sleep") as sleep,
        ):
            with self.assertRaises(SemanticProbeError) as raised:
                run_semantic_probe(scenario, self.config(), "incident")
        self.assertEqual(len(raised.exception.requests), 1)
        sleep.assert_not_called()

    def test_checkout_probe_collects_success_and_empty_cart(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-05-missing-function-call-checkout.yaml")
        )
        with patch("research.generators.semantic_probe._FrontendSession", return_value=_FakeSession()):
            result = run_semantic_probe(scenario, self.config(), "baseline")
        self.assertTrue(result.normalized["checkout_succeeded"])
        self.assertEqual(result.normalized["cart_items_after_checkout"], 0)

    def test_checkout_probe_does_not_replay_checkout_transaction(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-05-missing-function-call-checkout.yaml")
        )
        state = {"checkout_calls": 0}
        with (
            patch(
                "research.generators.semantic_probe._FrontendSession",
                return_value=_CheckoutFailureSession(state),
            ),
            patch("research.generators.semantic_probe.time.sleep") as sleep,
        ):
            result = run_semantic_probe(scenario, self.config(), "incident")
        self.assertFalse(result.normalized["checkout_succeeded"])
        self.assertEqual(result.normalized["error_count"], 3)
        self.assertEqual(state["checkout_calls"], 3)
        sleep.assert_not_called()

    def test_schema_v2_validator_uses_telemetry_evidence(self) -> None:
        observation = {
            "schema_version": 2,
            "evidence_source": ["frontend_http", "loki", "jaeger"],
            "normalized_observation": {
                "downstream_status": "ERROR",
                "downstream_service": "currencyservice",
                "incident_error_count": 2,
                "downstream_error_evidence": True,
            },
        }
        result = validate_semantic("incorrect_parameter", observation)
        self.assertEqual(result.status, "passed")

    def test_observation_builder_records_panic_evidence(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-02-missing-exception-checkout.yaml")
        )
        baseline = ProbePhaseResult(
            "baseline", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", [],
            {"error_count": 0},
        )
        incident = ProbePhaseResult(
            "incident", "2026-01-01T00:00:02+00:00", "2026-01-01T00:00:03+00:00", [],
            {"error_count": 1},
        )
        capture = {
            "logs": {
                "result": {
                    "data": {
                        "result": [
                            {
                                "stream": {"compose_service": "checkoutservice"},
                                "values": [["1", "panic: faulty image"]],
                            }
                        ]
                    }
                }
            },
            "traces": {"services": {"checkoutservice": {}}},
        }
        observation = build_semantic_observation(scenario, baseline, incident, capture)
        self.assertTrue(observation["normalized_observation"]["unhandled_exception"])
        self.assertEqual(validate_semantic("missing_exception", observation).status, "passed")

    def test_auto_probe_failure_prevents_fault_injection(self) -> None:
        scenario = load_scenario(
            Path("research/generators/scenarios/code-level-05-missing-function-call-checkout.yaml")
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = ResearchConfig(experiment_runs=Path(temporary))
            application = Mock()
            application.catalog = ServiceCatalog.load(config.service_metadata)
            application.validate_readiness.return_value = {
                "checkoutservice": ServiceState(
                    service="checkoutservice",
                    container_id="checkout-container",
                    image="baseline",
                    status="running",
                    health="healthy",
                    restart_count=0,
                    memory_limit=1,
                    nano_cpus=1,
                )
            }
            application.snapshot.return_value = {"schema_version": 1, "services": {}}
            observer = Mock()
            observer.capture.return_value = {
                "metrics": {}, "logs": {}, "alerts": {}, "traces": {}
            }
            real_injector = FaultyImageInjector(config)
            injector = Mock()
            injector.plan.side_effect = real_injector.plan
            injector.check_image_available.return_value = None
            with (
                patch("research.orchestrator.faults.ComposeApplication", return_value=application),
                patch("research.orchestrator.faults.Observer", return_value=observer),
                patch("research.orchestrator.faults.FaultyImageInjector", return_value=injector),
                patch(
                    "research.orchestrator.faults.run_semantic_probe",
                    side_effect=SemanticProbeError("frontend unavailable"),
                ),
            ):
                result = run_faulty_image(scenario, config, semantic_probe_mode="auto")
            run_path = Path(result["run_path"])
            self.assertEqual(result["status"], "baseline_semantic_failed")
            application.apply_fault_override.assert_not_called()
            self.assertTrue((run_path / "semantic-probe.json").exists())


if __name__ == "__main__":
    unittest.main()
