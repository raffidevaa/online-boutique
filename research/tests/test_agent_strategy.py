"""Offline checks for bounded RCA action and context behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.agent.environment import ComposeEvidence, EnvironmentError
from research.agent.orchestrator import AgentOrchestrator, LlmAgent
from research.agent.live import _prior_stage_context
from research.agent.models import AgentSession
from research.agent.providers import FakeProvider
from research.agent.strategy import QueryCache, action_fingerprint, validate_action
from research.utils import ResearchConfig


START = datetime(2026, 9, 20, 3, 28, tzinfo=UTC)
END = datetime(2026, 9, 20, 3, 30, tzinfo=UTC)


class StrategyTests(unittest.TestCase):
    def _replay(self, root: Path, actions: list[str], **config_values):
        run_dir = root / "runs" / "EXP-STRATEGY"
        for phase in ("baseline", "incident"):
            (run_dir / phase).mkdir(parents=True, exist_ok=True)
            for kind in ("metrics", "logs", "alerts", "traces"):
                (run_dir / phase / f"{kind}.json").write_text("{}", encoding="utf-8")
        (run_dir / "ground_truth.json").write_text(json.dumps({
            "root_cause_service": "checkoutservice", "fault_category": "resource",
            "fault_type": "cpu_hog",
        }), encoding="utf-8")
        config = ResearchConfig(experiment_runs=root / "runs", **config_values)
        orchestrator = AgentOrchestrator(config)
        orchestrator.register_agent(LlmAgent(FakeProvider(actions=actions)))
        _, task, session = orchestrator.init_problem(
            "FI-RES-CPU-02-analysis", start=START, end=END, run_dir=run_dir,
        )
        return orchestrator, task, session

    def test_fingerprint_normalizes_arguments_and_live_cache_expires(self):
        first = action_fingerprint("get_logs", {"window": "incident", "service": "checkoutservice"}, "run-1")
        second = action_fingerprint("get_logs", {"service": "checkoutservice", "window": "incident"}, "run-1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, action_fingerprint("get_logs", {"service": "checkoutservice", "window": "baseline"}, "run-1"))
        cache = QueryCache()
        with patch("research.agent.strategy.time.monotonic", return_value=100):
            cache.put(first, "data")
        with patch("research.agent.strategy.time.monotonic", return_value=120):
            self.assertIsNone(cache.get(first, replay=False, ttl_seconds=15))
            self.assertEqual(cache.get(first, replay=True, ttl_seconds=15), "data")

    def test_combined_budget_and_partial_result(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, task, session = self._replay(
                Path(directory), ["bad()"], aiops_incident_max_steps=1,
            )
            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=5, persist=False))
            self.assertEqual(result["termination_reason"], "incident_step_budget_exhausted")
            self.assertEqual(result["metrics"]["stage_steps_used"], 1)
            self.assertEqual(result["metrics"]["incident_steps_used"], 1)
            self.assertIsNone(result["solution"])
            self.assertEqual(result["partial_result"]["stop_reason"], "incident_step_budget_exhausted")
            self.assertFalse(result["results"]["combined_rca_correct"])

    def test_input_budget_stops_before_call_and_cost_needs_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, task, session = self._replay(
                Path(directory), ['submit({"system_level":"resource","fault_type":"cpu_hog",'
                                  '"faulty_components":["checkoutservice"]})'],
                aiops_max_input_tokens=1,
            )
            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=3, persist=False))
            self.assertEqual(result["termination_reason"], "input_token_budget_exhausted")
            self.assertEqual(result["metrics"]["stage_steps_used"], 0)
            self.assertIsNotNone(result["partial_result"])
            other, other_task, other_session = self._replay(
                Path(directory), [], aiops_max_cost_usd=0.01,
            )
            with self.assertRaisesRegex(RuntimeError, "requires both configured model rates"):
                asyncio.run(other.start_problem(other_task, other_session, persist=False))

    def test_validation_rejects_unknown_service_window_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            _, task, _ = self._replay(Path(directory), [])
            for arguments in (
                {"service": "not-a-service"}, {"window": "later"}, {"limit": 500},
            ):
                with self.assertRaises(ValueError):
                    validate_action(task, "get_logs", [], arguments)
            with self.assertRaises(ValueError):
                validate_action(task, "shell", [], {"command": "whoami"})

    def test_deduplication_and_stagnation(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, task, session = self._replay(
                Path(directory), ['get_logs(service="checkoutservice")'] * 5,
                aiops_stagnant_limit=2,
            )
            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=5, persist=False))
            self.assertEqual(result["termination_reason"], "progress_stalled")
            self.assertEqual(result["metrics"]["actual_queries"], 1)
            self.assertEqual(result["metrics"]["cache_hits"], 2)
            self.assertEqual(result["metrics"]["queries_avoided"], 2)

    def test_evidence_id_detail_and_ground_truth_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, task, session = self._replay(
                Path(directory), ['get_logs(service="checkoutservice")',
                                  'submit({"system_level":"resource","fault_type":"cpu_hog",'
                                  '"faulty_components":["checkoutservice"]})'],
            )
            self.assertEqual(task.expected, {})
            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=3, persist=False))
            evidence_id = result["metrics"]["evidence_ids"][0]
            detail = task.evidence.get_evidence_detail(evidence_id)
            self.assertIn(evidence_id, detail)
            self.assertNotIn("root_cause_service", json.dumps(result["trace"]))
            with self.assertRaises(EnvironmentError):
                task.evidence.get_evidence_detail("/etc/passwd")
            self.assertTrue(result["results"]["combined_rca_correct"])

    def test_hypothesis_update_requires_session_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            _, task, _ = self._replay(Path(directory), [])
            observation = task.get_logs(service="checkoutservice")
            self.assertIn("evidence_id", observation.content)
            evidence_id = next(iter(task.evidence.evidence_store))
            update = task.update_hypothesis(
                "h-checkout", "checkoutservice", "cpu_hog", "supported",
                [evidence_id], [], "Check CPU trend",
            )
            self.assertIn("h-checkout", update.content)
            with self.assertRaisesRegex(ValueError, "unchanged"):
                task.update_hypothesis(
                    "h-checkout", "checkoutservice", "cpu_hog", "supported",
                    [evidence_id], [], "Check CPU trend",
                )
            with self.assertRaisesRegex(ValueError, "evidence IDs"):
                task.update_hypothesis("h-other", supporting_evidence_ids=["ev-invented"])

    def test_compaction_retains_notes_and_evidence_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, task, session = self._replay(Path(directory), [])
            session.notes = [{"hypothesis_id": "h-1", "contradicting_evidence_ids": ["ev-opposite"]}]
            session.metrics["evidence_ids"] = ["ev-1"]
            orchestrator.agent.messages += [
                {"role": "user", "content": "old"}, {"role": "assistant", "content": "old"},
                {"role": "user", "content": "recent"}, {"role": "assistant", "content": "recent"},
            ]
            orchestrator._compact_agent_context(session)
            compact = str(orchestrator.agent.messages)
            self.assertIn("ev-opposite", compact)
            self.assertIn("ev-1", compact)
            self.assertEqual(session.metrics["compactions"], 1)

    def test_automatic_compaction_keeps_audit_history(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator, task, session = self._replay(
                Path(directory), [
                    'get_logs(service="checkoutservice")',
                    'get_metrics(service="checkoutservice")',
                    'submit({"system_level":"resource","fault_type":"cpu_hog",'
                    '"faulty_components":["checkoutservice"]})',
                ], aiops_context_compact_tokens=1,
            )
            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=4, persist=False))
            self.assertTrue(result["submitted"])
            self.assertGreaterEqual(result["metrics"].get("compactions", 0), 1)
            self.assertEqual(len([item for item in result["trace"] if item["role"] == "assistant"]), 3)
            self.assertIn(result["metrics"]["evidence_ids"][0], str(orchestrator.agent.messages))

    def test_handoff_marks_old_live_evidence_stale(self):
        session = AgentSession("FI-RES-CPU-02-analysis")
        session.end_time = datetime.now(UTC)
        session.notes.append({
            "status": "evidence_collected",
            "updated_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "supporting_evidence_ids": ["ev-old"],
        })
        session.metrics["evidence_ids"] = ["ev-old"]
        handoff = _prior_stage_context({"analysis": (None, session)}, max_age_seconds=15)
        self.assertIsNotNone(handoff)
        payload = json.loads(handoff)
        self.assertTrue(payload[0]["evidence_stale"])
        self.assertIn("ev-old", payload[0]["evidence_ids"])

    def test_live_detail_fetches_one_signal_without_full_capture(self):
        evidence = ComposeEvidence(ResearchConfig(), START, None)
        payload = {"result": {"data": {"result": [
            {"stream": {"compose_service": "checkoutservice"},
             "values": [["1", "error sample"]]}
        ]}}}
        with patch("research.agent.environment.Observer.capture_kind", return_value=payload) as focused:
            with patch("research.agent.environment.Observer.capture", side_effect=AssertionError("full capture")):
                result = evidence.get_logs("checkoutservice")
        focused.assert_called_once()
        self.assertIn("evidence_id", result)

    def test_replay_detail_respects_service_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            _, task, _ = self._replay(Path(directory), [])
            path = task.evidence.run_dir / "incident" / "logs.json"
            path.write_text(json.dumps({"result": {"data": {"result": [
                {"stream": {"compose_service": "checkoutservice"},
                 "values": [["1", "checkout error"]]},
                {"stream": {"compose_service": "frontend"},
                 "values": [["2", "frontend secret"]]},
            ]}}}), encoding="utf-8")
            task.get_logs("checkoutservice")
            evidence_id = next(iter(task.evidence.evidence_store))
            detail = task.get_evidence_detail(evidence_id).content
            self.assertIn("checkout error", detail)
            self.assertNotIn("frontend secret", detail)

    def test_sparse_single_span_does_not_dominate_network_ranking(self):
        def trace(operation: str, port: int, duration: int, index: int) -> dict:
            return {
                "traceID": f"t-{index}", "processes": {"p1": {"serviceName": "frontend"}},
                "spans": [{
                    "spanID": f"s-{index}", "processID": "p1",
                    "operationName": operation, "duration": duration,
                    "tags": [{"key": "span.kind", "value": "client"},
                             {"key": "server.port", "value": port}],
                }],
            }

        def capture(incident: bool) -> dict:
            catalog = [trace("hipstershop.ProductCatalogService/GetProduct", 3550,
                             200_000 if incident and index == 9 else 1_000, index)
                       for index in range(10)]
            checkout = [trace("hipstershop.CheckoutService/PlaceOrder", 5050,
                              320_000 if incident else 1_000, 20)]
            metrics = {"queries": {"container_cpu": {"data": {"result": [
                {"metric": {"service": service}, "values": [[1, "0.01"]]}
                for service in ("checkoutservice", "productcatalogservice")
            ]}}, "container_memory": {"data": {"result": []}}}}
            return {"metrics": metrics, "logs": {"result": {"data": {"result": []}}},
                    "alerts": {}, "traces": {"services": {
                        "frontend": {"data": catalog + checkout}
                    }}}

        evidence = ComposeEvidence(ResearchConfig(), START, END)
        with patch.object(evidence, "_capture_window", side_effect=lambda window: capture(window == "incident")):
            output = json.loads(evidence.get_incident_summary().split(": ", 1)[1])
        scores = {item["service"]: item["signals"]["network_root_score"]
                  for item in output["top_anomalies"]}
        self.assertEqual(scores["checkoutservice"], 0)
        self.assertGreater(scores["productcatalogservice"], 0)


if __name__ == "__main__":
    unittest.main()
