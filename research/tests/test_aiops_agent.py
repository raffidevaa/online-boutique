from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest

from research.agent.orchestrator import AgentOrchestrator, LlmAgent
from research.agent.parser import ActionParseError, ResponseParser
from research.agent.providers import FakeProvider
from research.agent.sanitize import sanitize_text
from research.agent.live import LiveIncidentController, LiveIncidentError
from research.generators import PumbaInjector, PumbaPlan, load_scenario
from research.utils import ResearchConfig
from research.utils.config import _load_dotenv


class AioAgentTests(unittest.TestCase):
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

            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=2))
            self.assertTrue(result["results"]["localization_exact"])
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
            self.assertIn("submit", orchestrator.agent.messages[0]["content"])

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


if __name__ == "__main__":
    unittest.main()
