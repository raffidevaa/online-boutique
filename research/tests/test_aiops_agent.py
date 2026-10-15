from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from research.agent.orchestrator import AgentOrchestrator, LlmAgent
from research.agent.parser import ActionParseError, ResponseParser
from research.agent.providers import FakeProvider
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
        with self.assertRaises(ActionParseError):
            parser.parse("```\nget_logs()\n```\n```\nsubmit(\"Yes\")\n```")
        with self.assertRaises(ActionParseError):
            parser.parse("```\nexec_shell(__import__('os').system('id'))\n```")

    def test_replay_session_runs_and_evaluates_without_exposing_ground_truth(self) -> None:
        source = Path("research/experiments/runs/EXP-20260913T133721753961Z-e41c67")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            shutil.copytree(source, runs / source.name)
            config = ResearchConfig(experiment_runs=runs, agent_sessions=root / "sessions")
            orchestrator = AgentOrchestrator(config)
            orchestrator.register_agent(
                LlmAgent(FakeProvider('submit({"faulty_components": ["checkoutservice"]})')),
                name="test-agent",
            )
            _, task, session = orchestrator.init_problem(
                "FI-RES-CPU-02-localization",
                start=datetime(2026, 9, 13, 13, 35, tzinfo=UTC),
                end=datetime(2026, 9, 13, 13, 37, tzinfo=UTC),
                run_dir=runs / source.name,
            )
            import asyncio

            result = asyncio.run(orchestrator.start_problem(task, session, max_steps=2))
            self.assertTrue(result["results"]["localization_exact"])
            self.assertNotIn('"root_cause_service"', json.dumps(result["trace"]))
            self.assertEqual(len(list((root / "sessions").glob("*.json"))), 1)

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
