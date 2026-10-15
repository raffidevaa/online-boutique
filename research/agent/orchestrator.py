"""AIOpsLab-compatible agent/environment loop for Compose problems."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from research.utils import ResearchConfig, write_json

from .models import AgentSession, ProblemContext
from .parser import ActionParseError, ResponseParser
from .providers import LlmProvider, build_provider
from .registry import ComposeProblemRegistry, ProblemError
from .tasks import EnvironmentResponse, TaskError


class AgentRuntimeError(RuntimeError):
    """Raised when an agent session cannot proceed safely."""


class LlmAgent:
    """Provider-backed agent implementing the AIOpsLab agent contract."""

    def __init__(self, provider: LlmProvider) -> None:
        self.provider = provider
        self.messages: list[dict[str, str]] = []
        self.actions: dict[str, str] = {}

    def init_context(self, problem_description: str, instructions: str, actions: dict[str, str]) -> None:
        self.actions = actions
        action_text = "\n".join(f"- {name}: {description}" for name, description in actions.items())
        system = (
            "You are an autonomous AIOps agent. Return exactly one action in a markdown "
            "code block per turn. Use only the listed read-only actions and submit when ready.\n\n"
            f"Problem:\n{problem_description}\n\nAvailable actions:\n{action_text}\n\n"
            f"Instructions:\n{instructions}"
        )
        self.messages = [{"role": "system", "content": system}]

    async def get_action(self, observation: str) -> str:
        self.messages.append({"role": "user", "content": observation})
        response = await self.provider.complete(self.messages, self.actions)
        self.messages.append({"role": "assistant", "content": response})
        return response


class AgentOrchestrator:
    """Manage problem setup, interaction, evaluation, persistence, and cleanup."""

    def __init__(self, config: ResearchConfig, results_dir: Path | None = None) -> None:
        self.config = config
        self.results_dir = results_dir or config.agent_sessions
        self.registry = ComposeProblemRegistry(config)
        self.parser = ResponseParser()
        self.agent: Any = None
        self.agent_name = "agent"

    def register_agent(self, agent: Any, name: str = "agent") -> None:
        if not hasattr(agent, "init_context") or not hasattr(agent, "get_action"):
            raise AgentRuntimeError("Agent must implement init_context and get_action")
        self.agent = agent
        self.agent_name = name

    def init_problem(
        self,
        problem_id: str,
        *,
        start: datetime,
        end: datetime | None,
        run_dir: Path | None = None,
    ) -> tuple[ProblemContext, Any, AgentSession]:
        if self.agent is None:
            raise AgentRuntimeError("Register an agent before initializing a problem")
        try:
            task, scenario, _ = self.registry.create(
                problem_id, start=start, end=end, run_dir=run_dir
            )
        except (ProblemError, OSError) as error:
            raise AgentRuntimeError(str(error)) from error
        context = ProblemContext(
            problem_id=problem_id,
            description=task.get_task_description(),
            instructions=task.get_instructions(),
            actions=task.get_available_actions(),
        )
        self.agent.init_context(context.description, context.instructions, context.actions)
        session = AgentSession(problem_id=problem_id, agent_name=self.agent_name)
        session.add("system", context.description)
        session.add("user", context.instructions)
        return context, task, session

    async def start_problem(
        self,
        task: Any,
        session: AgentSession,
        *,
        max_steps: int = 30,
        evaluate: bool = True,
        persist: bool = True,
    ) -> dict[str, Any]:
        if self.agent is None:
            raise AgentRuntimeError("Register an agent before starting a problem")
        session.start()
        observation = "Please take the next action."
        try:
            for _ in range(max_steps):
                response = await self.agent.get_action(observation)
                session.add("assistant", response)
                try:
                    action_name, args, kwargs = self.parser.parse(response)
                    env_response: EnvironmentResponse = task.perform_action(action_name, *args, **kwargs)
                    observation = env_response.content
                except (ActionParseError, TaskError, TypeError, ValueError) as error:
                    observation = f"Action error: {error}"
                    env_response = EnvironmentResponse(observation)
                session.add("env", observation)
                if env_response.submitted:
                    session.solution = env_response.solution
                    break
            session.end()
            if evaluate:
                return self.finalize_session(task, session, persist=persist)
            return session.to_dict()
        except Exception:
            session.end()
            raise

    def finalize_session(
        self,
        task: Any,
        session: AgentSession,
        *,
        expected: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Evaluate and persist a completed session.

        Live incidents call this only after the injector has finished and the
        controller has recorded ground truth.  ``expected`` is therefore never
        available to the agent during its action loop, preventing evaluator
        labels from leaking into the prompt or telemetry observations.
        """
        if expected is not None:
            task.expected = expected
        session.results = task.eval(session.solution, session.history, session.duration_seconds)
        output = session.to_dict()
        if persist:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            write_json(self.results_dir / f"{session.session_id}.json", output)
        return output


def create_orchestrator(config: ResearchConfig, provider: LlmProvider | None = None) -> AgentOrchestrator:
    orchestrator = AgentOrchestrator(config)
    orchestrator.register_agent(LlmAgent(provider or build_provider(config)), name="llm-agent")
    return orchestrator
