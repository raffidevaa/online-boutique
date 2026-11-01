from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research.agent.orchestrator import AgentOrchestrator, LlmAgent
from research.agent.environment import ComposeEvidence
from research.agent.parser import ActionParseError, ResponseParser
from research.agent.providers import FakeProvider
from research.agent.sanitize import sanitize_text
from research.agent.live import LiveIncidentController, LiveIncidentError
from research.agent.runtime import ComposeWorkload
from research.agent.actions import ComposeMitigationActions, MitigationActionError
from research.agent.tasks import MitigationTask
from research.agent.tasks import LocalizationTask
from research.agent.models import AgentSession
from research.agent.registry import ComposeProblemRegistry, ProblemError
from research.generators import PumbaInjector, PumbaPlan, load_scenario
from research.utils import ResearchConfig
from research.utils.config import _load_dotenv


class AioAgentTests(unittest.TestCase):
    def test_orchestrator_passes_full_mode_to_live_controller(self) -> None:
        import asyncio

        scenario = load_scenario(Path("research/generators/scenarios/network-05-loss-productcatalog.yaml"))
        orchestrator = AgentOrchestrator(ResearchConfig())
        with patch("research.agent.live.LiveIncidentController") as controller:
            async def completed(*args, **kwargs):
                return {"status": "completed"}
            controller.return_value.run.side_effect = completed
            asyncio.run(orchestrator.run_live_problem(scenario, "full", acknowledge_execute=True))
            self.assertEqual(controller.return_value.run.call_args.args[1], "full")

    def test_full_live_pipeline_records_each_stage_with_one_fault(self) -> None:
        import asyncio
        from dataclasses import replace
        from contextlib import nullcontext
        from research.agent.providers import FakeProvider

        source = load_scenario(Path("research/generators/scenarios/network-05-loss-productcatalog.yaml"))
        scenario = replace(source, parameters={**source.parameters, "duration_seconds": 1})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ResearchConfig(experiment_runs=root / "runs", recovery_observation_seconds=0)
            provider = FakeProvider(actions=[
                'submit("Yes")',
                'submit(faulty_components=["productcatalogservice"])',
                'submit({"system_level":"network","fault_type":"packet_loss","faulty_components":["productcatalogservice"]})',
                'submit({"action_type":"manual_investigation","target_service":"unknown","rationale":"Collect evidence","evidence":["Incident telemetry"],"expected_effect":"Clarify cause","risk":"low","rollback_plan":"No change","requires_approval":True,"execution_allowed":False})',
                'restore_service("productcatalogservice")',
                'submit()',
            ])
            prompts = []
            original_complete = provider.complete

            async def capture_prompt(messages, actions):
                prompts.append([dict(message) for message in messages])
                return await original_complete(messages, actions)

            provider.complete = capture_prompt
            orchestrator = AgentOrchestrator(config)
            orchestrator.register_agent(LlmAgent(provider))
            application = Mock()
            application.validate_readiness.return_value = {"productcatalogservice": SimpleNamespace(container_id="test")}
            application.get_service_state.return_value = SimpleNamespace(status="running")
            application.snapshot.return_value = {}
            observer = Mock()
            observer.capture.return_value = {kind: {} for kind in ("metrics", "logs", "traces", "alerts")}
            injector = Mock()
            injector.plan.return_value.to_dict.return_value = {}
            running = SimpleNamespace(started_at=datetime.now(UTC), ended_at=None)
            injector.start.return_value = running
            injector.wait.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            run_path = config.experiment_runs / "EXP-TEST-FULL"
            run_path.mkdir(parents=True)
            controller = LiveIncidentController(config, orchestrator, application=application, observer=observer, injector=injector)
            async def direct_thread(function, *args):
                return function(*args)

            with patch("research.agent.live.fault_lock", return_value=nullcontext()), patch("research.agent.live.create_run", return_value=(run_path, {"experiment_id": run_path.name})), patch("research.agent.live.asyncio.to_thread", side_effect=direct_thread):
                result = asyncio.run(controller.run(scenario, "full", max_steps=2, acknowledge_execute=True))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(injector.start.call_count, 1)
            self.assertEqual(set(result["stages"]), {"detection", "localization", "analysis", "remediation", "mitigation"})
            stage_prompts = [messages[0]["content"] for messages in prompts]
            self.assertIn('"stage":"detection"', stage_prompts[1])
            self.assertIn('"solution":"Yes"', stage_prompts[1])
            self.assertIn('"stage":"localization"', stage_prompts[2])
            self.assertIn('"stage":"remediation"', stage_prompts[-2])
            self.assertIn("Use the previous stage findings", prompts[1][1]["content"])
            self.assertIn("call restore_service now", prompts[-2][1]["content"])
            self.assertNotIn("root_cause_service", stage_prompts[2])
            for name in ("detection-session.json", "localization-session.json", "agent-session.json", "remediation-session.json", "mitigation-session.json"):
                self.assertTrue((run_path / name).is_file(), name)

    def test_fake_provider_uses_valid_mitigation_submission(self) -> None:
        self.assertEqual(FakeProvider._default_action({"submit": "submit()"}), "submit()")

    def test_failed_live_run_preserves_agent_session(self) -> None:
        scenario = load_scenario(Path("research/generators/scenarios/resource-03-cpu-hog-checkout.yaml"))
        config = ResearchConfig()
        controller = LiveIncidentController(config, AgentOrchestrator(config))
        with tempfile.TemporaryDirectory() as temporary:
            run_path = Path(temporary)
            session = AgentSession(problem_id=f"{scenario.scenario_id}-analysis")
            session.start()
            session.add("assistant", "get_metrics()")
            application = Mock()
            application.snapshot.return_value = {}
            controller._record_failed_run(
                scenario, application, Mock(), Mock(), None, run_path,
                {"experiment_id": "EXP-TEST"}, None, RuntimeError("provider failed"), session,
            )
            saved = json.loads((run_path / "agent-session.json").read_text())
            self.assertEqual(saved["termination_reason"], "runtime_error")
            self.assertEqual(saved["history"], saved["trace"])

    def test_localization_accepts_upstream_list_submission(self) -> None:
        task = LocalizationTask(Mock(), {"root_cause_service": "checkoutservice"})
        response = task.submit(faulty_components=["checkoutservice"])
        self.assertEqual(response.solution, {"faulty_components": ["checkoutservice"]})
        self.assertTrue(task.eval(response.solution, [], 0.1)["localization_exact"])

    def test_mitigation_registry_requires_live_adapter(self) -> None:
        registry = ComposeProblemRegistry(ResearchConfig())
        problem_id = "FI-RES-CPU-02-mitigation"
        self.assertIn(problem_id, registry.get_problem_ids())
        with self.assertRaisesRegex(ProblemError, "active live"):
            registry.create(problem_id, start=datetime.now(UTC), end=None)

    def test_mitigation_agent_session_uses_live_action_adapter(self) -> None:
        import asyncio

        config = ResearchConfig()
        orchestrator = AgentOrchestrator(config)
        orchestrator.register_agent(LlmAgent(FakeProvider('submit()')))
        runtime = Mock()
        runtime.get_service_state.return_value = SimpleNamespace(status="running")
        actions = ComposeMitigationActions(runtime, "checkoutservice")
        context, task, session = orchestrator.init_problem(
            "FI-RES-CPU-02-mitigation",
            start=datetime.now(UTC),
            end=None,
            mitigation_actions=actions,
        )
        self.assertIn("restore_service", context.actions)
        result = asyncio.run(orchestrator.start_problem(
            task, session, max_steps=1, persist=False,
        ))
        self.assertTrue(result["submitted"])
        self.assertFalse(result["results"]["mitigation_success"])

    def test_mitigation_task_executes_and_scores_only_active_target(self) -> None:
        runtime = Mock()
        runtime.get_service_state.return_value = SimpleNamespace(status="running")
        operations = ComposeMitigationActions(runtime, "checkoutservice", Mock(), object())
        task = MitigationTask(Mock(), {}, operations)
        self.assertIn("restore_service", task.get_available_actions())
        with self.assertRaises(MitigationActionError):
            task.perform_action("restore_service", "paymentservice")
        task.perform_action("restore_service", "checkoutservice")
        submitted = task.perform_action("submit")
        self.assertTrue(submitted.submitted)
        self.assertTrue(task.eval(submitted.solution, [], 1.0)["mitigation_success"])

    def test_mitigation_action_is_scoped_to_active_service(self) -> None:
        runtime = Mock()
        runtime.get_service_state.return_value = SimpleNamespace(status="running")
        actions = ComposeMitigationActions(runtime, "checkoutservice")
        with self.assertRaises(MitigationActionError):
            actions.restore_service("paymentservice")
        runtime.restore_service.assert_not_called()
        self.assertIn("status=running", actions.restore_service("checkoutservice"))
        runtime.restore_service.assert_called_once_with("checkoutservice")
        self.assertEqual(len(actions.history), 1)

    def test_mitigation_stops_active_fault_before_recreating_target(self) -> None:
        calls = []
        runtime = Mock()
        runtime.get_service_state.return_value = SimpleNamespace(status="running")
        runtime.restore_service.side_effect = lambda service: calls.append("restore")
        fault = Mock()
        fault.stop.side_effect = lambda running: calls.append("stop")
        actions = ComposeMitigationActions(runtime, "checkoutservice", fault, object())
        actions.restore_service("checkoutservice")
        self.assertEqual(calls, ["stop", "restore"])
        self.assertTrue(actions.fault_stopped)

    def test_mitigation_does_not_claim_naturally_expired_fault(self) -> None:
        runtime = Mock()
        runtime.get_service_state.return_value = SimpleNamespace(status="running")
        fault = Mock()
        running = SimpleNamespace(process=Mock())
        running.process.poll.return_value = 0
        actions = ComposeMitigationActions(runtime, "checkoutservice", fault, running)
        actions.restore_service("checkoutservice")
        fault.stop.assert_not_called()
        self.assertFalse(actions.fault_stopped)

    def test_compose_workload_requires_allowlisted_running_generator(self) -> None:
        application = Mock()
        workload = ComposeWorkload(application)
        workload.ensure_running(None)
        application.get_service_state.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Unsupported workload"):
            workload.ensure_running("wrk")
        application.get_service_state.return_value = SimpleNamespace(status="exited")
        with self.assertRaisesRegex(ValueError, "did not leave"):
            workload.ensure_running("loadgenerator")
        application.get_service_state.return_value = SimpleNamespace(status="running")
        workload.ensure_running("loadgenerator")

    def test_dotenv_loader_reads_local_values_without_exposing_secrets(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as env_file:
            env_file.write(
                "# comment\nexport AIOPS_LLM_PROVIDER=deepseek\n"
                "AIOPS_LLM_API_KEY=local-secret\n"
            )
            env_file.flush()
            values = _load_dotenv(Path(env_file.name))
        self.assertEqual(values["AIOPS_LLM_PROVIDER"], "deepseek")
        self.assertEqual(values["AIOPS_LLM_API_KEY"], "local-secret")

    def test_parser_requires_one_literal_action_block(self) -> None:
        parser = ResponseParser()
        self.assertEqual(parser.parse('```\nsubmit("Yes")\n```')[0], "submit")
        self.assertEqual(
            parser.parse('{"action":"get_alerts","window":"incident"}'),
            ("get_alerts", [], {"window": "incident"}),
        )
        self.assertEqual(
            parser.parse('```json\n{"action":"get_alerts","window":"incident"}\n```'),
            ("get_alerts", [], {"window": "incident"}),
        )
        with self.assertRaises(ActionParseError):
            parser.parse("```\nget_logs()\n```\n```\nsubmit(\"Yes\")\n```")
        with self.assertRaises(ActionParseError):
            parser.parse("```\nexec_shell(__import__('os').system('id'))\n```")
        with self.assertRaises(ActionParseError):
            parser.parse('```json\n{"window":"incident"}\n```')

    def test_replay_session_runs_and_evaluates_without_exposing_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            run_dir = runs / "EXP-TEST-CODE-EXC-01"
            incident = run_dir / "incident"
            incident.mkdir(parents=True)
            (run_dir / "ground_truth.json").write_text(
                json.dumps(
                    {
                        "scenario_id": "FI-CODE-EXC-01",
                        "root_cause_service": "checkoutservice",
                        "fault_category": "code_level",
                        "fault_type": "missing_exception_handler",
                    }
                ),
                encoding="utf-8",
            )
            for kind in ("metrics", "logs", "traces", "alerts"):
                (incident / f"{kind}.json").write_text("{}", encoding="utf-8")
            config = ResearchConfig(experiment_runs=runs, agent_sessions=root / "sessions")
            orchestrator = AgentOrchestrator(config)
            orchestrator.register_agent(
                LlmAgent(FakeProvider('submit({"faulty_components": ["checkoutservice"]})')),
                name="test-agent",
            )
            _, task, session = orchestrator.init_problem(
                "FI-CODE-EXC-01-localization",
                start=datetime(2026, 9, 20, 3, 28, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
                run_dir=run_dir,
            )
            import asyncio

            events = []
            result = asyncio.run(
                orchestrator.start_problem(
                    task, session, max_steps=2, event_sink=events.append
                )
            )
            self.assertTrue(result["results"]["localization_exact"])
            self.assertIsNotNone(result["submitted_at"])
            self.assertIsNotNone(result["submission_elapsed_seconds"])
            self.assertIn("action_parsed", [event["kind"] for event in events])
            self.assertIn("submitted", [event["kind"] for event in events])
            self.assertEqual(result["history"], result["trace"])
            self.assertEqual(result["final_state"], "VALID_SUBMISSION")
            self.assertNotIn('"root_cause_service"', json.dumps(result["trace"]))
            self.assertEqual(len(list((root / "sessions").glob("*.json"))), 1)

    def test_task_actions_expose_submit_to_the_agent(self) -> None:
        orchestrator = AgentOrchestrator(ResearchConfig())
        orchestrator.register_agent(LlmAgent(FakeProvider()), name="test-agent")
        for task_name in ("detection", "localization", "analysis"):
            context, _, _ = orchestrator.init_problem(
                f"FI-CODE-EXC-01-{task_name}",
                start=datetime(2026, 9, 20, 3, 28, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
            )
            self.assertIn("submit", context.actions)
            self.assertIn("get_incident_summary", context.actions)
            self.assertIn("get_error_logs", context.actions)
            self.assertIn("submit", orchestrator.agent.messages[0]["content"])

    def test_incident_summary_ranks_cpu_spike_and_error_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            for phase in ("baseline", "incident"):
                (run_dir / phase).mkdir(parents=True)
                metrics = {
                    "queries": {
                        "container_cpu": {
                            "data": {
                                "result": [
                                    {
                                        "metric": {"service": "checkoutservice"},
                                        "values": [[1, "0.01"]] if phase == "baseline" else [[2, "0.20"]],
                                    },
                                    {
                                        "metric": {"service": "frontend"},
                                        "values": [[1, "0.02"]] if phase == "baseline" else [[2, "0.03"]],
                                    },
                                ]
                            }
                        },
                        "container_memory": {"data": {"result": []}},
                    }
                }
                logs = {
                    "result": {
                        "data": {
                            "result": [
                                {
                                    "stream": {
                                        "compose_service": "checkoutservice",
                                        "detected_level": "error",
                                    },
                                    "values": [["1", "panic: simulated checkout failure"]],
                                }
                            ]
                            if phase == "incident"
                            else []
                        }
                    }
                }
                for name, payload in {
                    "metrics": metrics,
                    "logs": logs,
                    "alerts": {"alerts": {"data": {"alerts": []}}},
                    "traces": {"services": {}},
                }.items():
                    (run_dir / phase / f"{name}.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )

            evidence = ComposeEvidence(
                ResearchConfig(experiment_runs=root / "experiments"),
                start=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 32, tzinfo=UTC),
                run_dir=run_dir,
            )
            summary = json.loads(evidence.get_incident_summary().split(": ", 1)[1])
            self.assertEqual(summary["top_anomalies"][0]["service"], "checkoutservice")
            self.assertGreater(summary["top_anomalies"][0]["signals"]["cpu_delta"], 0.1)
            self.assertEqual(
                summary["top_anomalies"][0]["signals"]["dominant_signal"], "cpu_growth"
            )
            errors = json.loads(evidence.get_error_logs("checkoutservice").split(": ", 1)[1])
            self.assertEqual(errors["services"]["checkoutservice"]["count"], 1)

    def test_incident_summary_identifies_memory_growth_over_cpu_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            for phase in ("baseline", "incident"):
                (run_dir / phase).mkdir(parents=True)
                metrics = {
                    "queries": {
                        "container_cpu": {
                            "data": {
                                "result": [
                                    {
                                        "metric": {"service": "cartservice"},
                                        "values": [[1, "0.01"]]
                                        if phase == "baseline"
                                        else [[2, "0.03"]],
                                    }
                                ]
                            }
                        },
                        "container_memory": {
                            "data": {
                                "result": [
                                    {
                                        "metric": {"service": "cartservice"},
                                        "values": [[1, "48000000"]]
                                        if phase == "baseline"
                                        else [[2, "120000000"]],
                                    }
                                ]
                            }
                        },
                    }
                }
                for name, payload in {
                    "metrics": metrics,
                    "logs": {"result": {"data": {"result": []}}},
                    "alerts": {"alerts": {"data": {"alerts": []}}},
                    "traces": {"services": {}},
                }.items():
                    (run_dir / phase / f"{name}.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
            evidence = ComposeEvidence(
                ResearchConfig(experiment_runs=root / "experiments"),
                start=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 32, tzinfo=UTC),
                run_dir=run_dir,
            )
            summary = json.loads(evidence.get_incident_summary().split(": ", 1)[1])
            signals = summary["top_anomalies"][0]["signals"]
            self.assertEqual(summary["top_anomalies"][0]["service"], "cartservice")
            self.assertEqual(signals["dominant_signal"], "memory_growth")
            self.assertGreater(signals["memory_relative"], signals["cpu_delta"])
            self.assertEqual(signals["resource_signature"], "memory_pressure")
            self.assertGreater(signals["memory_growth_mb"], 32.0)

    def test_trace_summary_resolves_actual_service_and_rpc_peer(self) -> None:
        payload = {
            "services": {
                "checkoutservice": {
                    "data": [
                        {
                            "traceID": "trace-1",
                            "processes": {
                                "p1": {"serviceName": "frontend"},
                            },
                            "spans": [
                                {
                                    "spanID": "span-1",
                                    "processID": "p1",
                                    "operationName": "hipstershop.PaymentService/Charge",
                                    "duration": 300000,
                                    "tags": [
                                        {"key": "span.kind", "value": "client"},
                                        {"key": "rpc.method", "value": "Charge"},
                                        {"key": "server.port", "value": 50051},
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        }
        summary = ComposeEvidence._trace_summary(
            payload,
            {"frontend": {"8080"}, "paymentservice": {"50051"}},
        )
        self.assertEqual(summary["services"]["frontend"]["span_count"], 1)
        self.assertEqual(summary["network_edges"][0]["peer_service"], "paymentservice")
        self.assertEqual(summary["network_edges"][0]["p50_latency_ms"], 300.0)

    def test_network_evidence_separates_delay_from_sparse_loss_outliers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            for phase, durations in (
                ("baseline", [2000, 2200, 2100]),
                ("incident", [300000, 320000, 280000]),
            ):
                (run_dir / phase).mkdir(parents=True)
                trace = {
                    "services": {
                        "frontend": {
                            "data": [
                                {
                                    "traceID": f"{phase}-trace",
                                    "processes": {"p1": {"serviceName": "frontend"}},
                                    "spans": [
                                        {
                                            "spanID": f"{phase}-{index}",
                                            "processID": "p1",
                                            "operationName": "hipstershop.PaymentService/Charge",
                                            "duration": duration,
                                            "tags": [
                                                {"key": "span.kind", "value": "client"},
                                                {"key": "rpc.method", "value": "Charge"},
                                                {"key": "server.port", "value": 50051},
                                            ],
                                        }
                                    ],
                                }
                                for index, duration in enumerate(durations)
                            ]
                        }
                    }
                }
                metrics = {
                    "queries": {
                        "container_cpu": {"data": {"result": []}},
                        "container_memory": {"data": {"result": []}},
                    }
                }
                for name, payload in {
                    "metrics": metrics,
                    "logs": {"result": {"data": {"result": []}}},
                    "alerts": {"alerts": {"data": {"alerts": []}}},
                    "traces": trace,
                }.items():
                    (run_dir / phase / f"{name}.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
            evidence = ComposeEvidence(
                ResearchConfig(experiment_runs=root / "experiments"),
                start=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 32, tzinfo=UTC),
                run_dir=run_dir,
            )
            summary = json.loads(evidence.get_incident_summary().split(": ", 1)[1])
            evidence_row = next(
                item
                for item in summary["network_evidence"]
                if item["peer_service"] == "paymentservice"
            )
            self.assertEqual(evidence_row["inferred_signature"], "network_delay")
            self.assertGreater(evidence_row["latency_shift_ms"], 100.0)
            signature, delay_score, loss_score = ComposeEvidence._network_signature(
                incident_p50=3.0,
                baseline_p50=2.0,
                error_delta=0,
                outlier_rate=0.125,
                incident_max=220.0,
            )
            self.assertEqual(signature, "packet_loss")
            self.assertLess(delay_score, 1.0)
            self.assertGreater(loss_score, 0.0)

    def test_analysis_instructions_include_fault_signature_rules(self) -> None:
        orchestrator = AgentOrchestrator(ResearchConfig())
        orchestrator.register_agent(LlmAgent(FakeProvider()), name="test-agent")
        _, task, _ = orchestrator.init_problem(
            "FI-RES-MEM-01-analysis",
            start=datetime(2026, 9, 13, 13, 35, tzinfo=UTC),
            end=datetime(2026, 9, 13, 13, 36, tzinfo=UTC),
        )
        instructions = task.get_instructions()
        self.assertIn("memory_growth dominant", instructions)
        self.assertIn("latency dominant", instructions)
        self.assertIn("packet_loss", instructions)

    def test_incident_summary_excludes_non_targetable_workload_services(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            for phase in ("baseline", "incident"):
                (run_dir / phase).mkdir(parents=True)
                metrics = {
                    "queries": {
                        "container_cpu": {
                            "data": {
                                "result": [
                                    {
                                        "metric": {"service": "checkoutservice"},
                                        "values": [[1, "0.01"]]
                                        if phase == "baseline"
                                        else [[2, "0.20"]],
                                    },
                                    {
                                        "metric": {"service": "loadgenerator"},
                                        "values": [[1, "0.01"]],
                                    },
                                ]
                            }
                        },
                        "container_memory": {"data": {"result": []}},
                    }
                }
                logs = {
                    "result": {
                        "data": {
                            "result": [
                                {
                                    "stream": {
                                        "compose_service": "loadgenerator",
                                        "detected_level": "error",
                                    },
                                    "values": [[str(index), "error"] for index in range(30)]
                                    if phase == "incident"
                                    else [],
                                }
                            ]
                        }
                    }
                }
                for name, payload in {
                    "metrics": metrics,
                    "logs": logs,
                    "alerts": {"alerts": {"data": {"alerts": []}}},
                    "traces": {"services": {}},
                }.items():
                    (run_dir / phase / f"{name}.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
            evidence = ComposeEvidence(
                ResearchConfig(experiment_runs=root / "experiments"),
                start=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 32, tzinfo=UTC),
                run_dir=run_dir,
            )
            summary = json.loads(evidence.get_incident_summary().split(": ", 1)[1])
            services = [item["service"] for item in summary["top_anomalies"]]
            self.assertIn("checkoutservice", services)
            self.assertNotIn("loadgenerator", services)

    def test_step_budget_records_submission_and_exhaustion(self) -> None:
        import asyncio

        orchestrator = AgentOrchestrator(ResearchConfig())
        orchestrator.register_agent(LlmAgent(FakeProvider("not_allowed()")))
        _, task, session = orchestrator.init_problem(
            "FI-RES-CPU-02-detection",
            start=datetime(2026, 9, 13, 13, 35, tzinfo=UTC),
            end=datetime(2026, 9, 13, 13, 36, tzinfo=UTC),
        )
        result = asyncio.run(orchestrator.start_problem(task, session, max_steps=2, persist=False))
        self.assertFalse(result["submitted"])
        self.assertEqual(result["termination_reason"], "step_budget_exhausted")
        self.assertEqual(result["results"]["termination_reason"], "step_budget_exhausted")

    def test_fake_provider_default_submission_is_valid_for_each_task(self) -> None:
        import asyncio

        provider = FakeProvider()
        detection = asyncio.run(provider.complete([], {"submit": 'submit(answer: "Yes" | "No")'}))
        localization = asyncio.run(
            provider.complete([], {"submit": 'submit(answer: {"faulty_components": ["service"]})'})
        )
        analysis = asyncio.run(
            provider.complete(
                [],
                {
                    "submit": 'submit(answer: {"system_level": "resource", '
                    '"fault_type": "taxonomy type", "faulty_components": ["service"]})'
                },
            )
        )
        self.assertIn('submit("No")', detection)
        self.assertIn('"faulty_components": []', localization)
        self.assertIn('"system_level": "resource"', analysis)

    def test_fake_provider_can_replay_triage_sequence(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "runs" / "EXP-TEST-TRIAGE"
            for phase in ("baseline", "incident"):
                (run_dir / phase).mkdir(parents=True)
                for kind in ("metrics", "logs", "alerts", "traces"):
                    (run_dir / phase / f"{kind}.json").write_text("{}", encoding="utf-8")
            (run_dir / "ground_truth.json").write_text(
                json.dumps(
                    {
                        "root_cause_service": "checkoutservice",
                        "fault_category": "resource",
                        "fault_type": "cpu_hog",
                    }
                ),
                encoding="utf-8",
            )
            config = ResearchConfig(
                experiment_runs=root / "runs", agent_sessions=root / "sessions"
            )
            orchestrator = AgentOrchestrator(config)
            provider = FakeProvider(
                actions=[
                    'get_incident_summary(window="incident")',
                    'get_error_logs(service="checkoutservice", window="incident")',
                    'submit({"system_level": "resource", "fault_type": "cpu_hog", '
                    '"faulty_components": ["checkoutservice"]})',
                ]
            )
            orchestrator.register_agent(LlmAgent(provider), name="triage-agent")
            _, task, session = orchestrator.init_problem(
                "FI-RES-CPU-02-analysis",
                start=datetime(2026, 9, 20, 3, 28, tzinfo=UTC),
                end=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
                run_dir=run_dir,
            )
            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=5, persist=False))
            self.assertTrue(result["submitted"])
            self.assertEqual(result["termination_reason"], "submitted")
            self.assertEqual(result["results"]["steps"], 3)
            self.assertTrue(result["results"]["combined_rca_correct"])

    def test_external_provider_sanitizer_redacts_secrets_and_network_identifiers(self) -> None:
        raw = (
            "checkoutservice failed at https://10.0.0.12:3100/api with "
            "api_key=secret-value and Authorization: Bearer abc.def"
        )
        sanitized = sanitize_text(raw)
        self.assertIn("checkoutservice", sanitized)
        self.assertNotIn("secret-value", sanitized)
        self.assertNotIn("abc.def", sanitized)
        self.assertNotIn("10.0.0.12", sanitized)
        self.assertNotIn("https://", sanitized)

        pii = "user=0f23e354-a2df-4b4f-85bd-741c0dfeeb99 email=test@example.com"
        pii += " trace=0123456789abcdef0123456789abcdef"
        sanitized_pii = sanitize_text(pii)
        self.assertNotIn("0f23e354-a2df-4b4f-85bd-741c0dfeeb99", sanitized_pii)
        self.assertNotIn("test@example.com", sanitized_pii)
        self.assertNotIn("0123456789abcdef0123456789abcdef", sanitized_pii)

    def test_registry_problem_ids_include_three_tasks(self) -> None:
        config = ResearchConfig()
        from research.agent.registry import ComposeProblemRegistry

        ids = ComposeProblemRegistry(config).get_problem_ids()
        self.assertIn("FI-RES-CPU-02-detection", ids)
        self.assertIn("FI-RES-CPU-02-localization", ids)
        self.assertIn("FI-RES-CPU-02-analysis", ids)

    def test_live_context_does_not_leak_scenario_or_ground_truth(self) -> None:
        config = ResearchConfig()
        orchestrator = AgentOrchestrator(config)
        agent = LlmAgent(FakeProvider('submit({"faulty_components": ["checkoutservice"]})'))
        orchestrator.register_agent(agent, name="test-agent")
        _, task, session = orchestrator.init_problem(
            "FI-RES-CPU-02-localization",
            start=datetime(2026, 9, 13, 13, 35, tzinfo=UTC),
            end=None,
            run_dir=None,
        )
        system_prompt = agent.messages[0]["content"]
        self.assertNotIn("FI-RES-CPU-02", system_prompt)
        self.assertNotIn("checkoutservice", system_prompt)
        self.assertNotIn("cpu_hog", system_prompt)
        self.assertNotIn("root_cause_service", system_prompt)
        self.assertEqual(task.expected, {})
        self.assertEqual(session.history[0].role, "system")

    def test_live_session_is_evaluated_only_after_ground_truth_handoff(self) -> None:
        config = ResearchConfig()
        with tempfile.TemporaryDirectory() as temporary:
            config = ResearchConfig(agent_sessions=Path(temporary) / "sessions")
            orchestrator = AgentOrchestrator(config)
            orchestrator.register_agent(
                LlmAgent(FakeProvider('submit({"faulty_components": ["checkoutservice"]})')),
                name="test-agent",
            )
            _, task, session = orchestrator.init_problem(
                "FI-RES-CPU-02-localization",
                start=datetime(2026, 9, 13, 13, 35, tzinfo=UTC),
                end=None,
                run_dir=None,
            )
            import asyncio

            asyncio.run(
                orchestrator.start_problem(
                    task, session, max_steps=2, evaluate=False, persist=False
                )
            )
            self.assertEqual(session.results, {})
            self.assertEqual(task.expected, {})
            result = orchestrator.finalize_session(
                task,
                session,
                expected={"root_cause_service": "checkoutservice"},
            )
            self.assertTrue(result["results"]["localization_exact"])
            self.assertNotIn("root_cause_service", json.dumps(result["trace"]))
            self.assertEqual(len(list(config.agent_sessions.glob("*.json"))), 1)

    def test_pumba_running_injection_can_be_waited_without_docker(self) -> None:
        plan = PumbaPlan("test", "checkoutservice", "container", ("true",))
        running = PumbaInjector.start(plan)
        result = PumbaInjector.wait(running, timeout_seconds=5)
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(running.ended_at)

    def test_integrated_live_requires_explicit_execute(self) -> None:
        from research.utils import ResearchConfig

        scenario = load_scenario(Path("research/generators/scenarios/resource-03-cpu-hog-checkout.yaml"))
        controller = LiveIncidentController(ResearchConfig(), AgentOrchestrator(ResearchConfig()))
        import asyncio

        with self.assertRaisesRegex(LiveIncidentError, "explicit execute"):
            asyncio.run(controller.run(scenario, "analysis"))

    def test_live_failure_cleanup_stops_injection_and_records_recovery(self) -> None:
        from datetime import timedelta

        scenario = load_scenario(Path("research/generators/scenarios/resource-03-cpu-hog-checkout.yaml"))
        config = ResearchConfig()
        controller = LiveIncidentController(config, AgentOrchestrator(config))
        application = Mock()
        application.snapshot.return_value = {"checkoutservice": {"status": "running"}}
        observer = Mock()
        observer.capture.return_value = {
            "metrics": {},
            "logs": {},
            "alerts": {},
            "traces": {},
        }
        injector = Mock()
        running = SimpleNamespace(ended_at=None)

        def stop(active: SimpleNamespace) -> None:
            active.ended_at = datetime.now(UTC)

        injector.stop.side_effect = stop
        with tempfile.TemporaryDirectory() as temporary:
            run_path = Path(temporary) / "run"
            run_path.mkdir()
            injection_start = datetime.now(UTC) - timedelta(seconds=30)
            controller._record_failed_run(
                scenario,
                application,
                observer,
                injector,
                running,
                run_path,
                {"experiment_id": "EXP-TEST-LIVE"},
                injection_start,
                RuntimeError("provider failed"),
            )
            injector.stop.assert_called_once_with(running)
            self.assertTrue((run_path / "ground_truth.json").exists())
            self.assertTrue((run_path / "incident").is_dir())
            self.assertTrue((run_path / "recovery").is_dir())
            self.assertEqual(json.loads((run_path / "result.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
