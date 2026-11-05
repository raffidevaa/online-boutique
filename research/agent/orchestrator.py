"""AIOpsLab-compatible agent/environment loop for Compose problems."""

from __future__ import annotations

from datetime import UTC, datetime
import asyncio
from pathlib import Path
from typing import Any, Callable

from research.utils import ResearchConfig, write_json

from .models import AgentSession, ProblemContext
from .actions import ComposeMitigationActions
from .environment import EnvironmentError
from .parser import ActionParseError, ResponseParser
from .providers import LlmProvider, ProviderOutput, build_provider
from .strategy import (IncidentBudget, QueryCache, READ_ACTIONS,
                       action_fingerprint, estimate_tokens, validate_action)
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
        self.last_output: ProviderOutput | None = None

    def init_context(self, problem_description: str, instructions: str, actions: dict[str, str]) -> None:
        self.actions = actions
        action_text = "\n".join(f"- {name}: {description}" for name, description in actions.items())
        action_policy = (
            "Use only the listed actions. The restore_service action is permitted only "
            "for the active target service in this mitigation problem. "
            if "restore_service" in actions else
            "Use only the listed read-only actions and submit when ready. "
        )
        system = (
            "You are an autonomous AIOps agent. Return exactly one raw JSON action object "
            "per turn. Do not emit tool-call XML or DSML markup. Use only the listed "
            f"actions and submit when ready. {action_policy}"
            "Follow the step budget included in each user message; reserve the final two "
            "steps for submission and avoid broad service-by-service enumeration. "
            "Use JSON syntax such as {\"action\":\"get_logs\",\"window\":\"incident\"}.\n\n"
            f"Problem:\n{problem_description}\n\nAvailable actions:\n{action_text}\n\n"
            f"Instructions:\n{instructions}"
        )
        self.messages = [{"role": "system", "content": system}]

    async def get_action(self, observation: str) -> str:
        self.messages.append({"role": "user", "content": observation})
        result = await self.provider.complete(self.messages, self.actions)
        self.last_output = result if isinstance(result, ProviderOutput) else ProviderOutput(
            result, model=getattr(self.provider, "model", self.provider.name)
        )
        self.messages.append({"role": "assistant", "content": self.last_output.text})
        return self.last_output.text


class AgentOrchestrator:
    """Manage problem setup, interaction, evaluation, persistence, and cleanup."""

    def __init__(self, config: ResearchConfig, results_dir: Path | None = None) -> None:
        self.config = config
        self.results_dir = results_dir or config.agent_sessions
        self.registry = ComposeProblemRegistry(config)
        self.parser = ResponseParser()
        self.agent: Any = None
        self.agent_name = "agent"
        self.incident_budget = IncidentBudget()

    def register_agent(self, agent: Any, name: str = "agent") -> None:
        if not callable(getattr(agent, "get_action", None)):
            raise AgentRuntimeError("Agent must implement get_action")
        self.agent = agent
        self.agent_name = name

    async def run_live_problem(
        self,
        scenario: Any,
        task_name: str,
        *,
        max_steps: int = 15,
        acknowledge_execute: bool = False,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run a complete live problem through the Compose lifecycle adapter."""
        from .live import LiveIncidentController

        problem_id = f"{scenario.scenario_id}-{'analysis' if task_name == 'full' else task_name}"
        try:
            definition = self.registry.get_problem_definition(problem_id)
        except ProblemError as error:
            raise AgentRuntimeError(str(error)) from error
        if scenario.to_dict() != definition.fault.to_dict():
            raise AgentRuntimeError("Live scenario must match the registered problem catalog")
        return await LiveIncidentController(self.config, self, event_sink).run(
            definition.fault,
            task_name,
            max_steps=max_steps,
            acknowledge_execute=acknowledge_execute,
        )

    def init_problem(
        self,
        problem_id: str,
        *,
        start: datetime,
        end: datetime | None,
        run_dir: Path | None = None,
        withhold_ground_truth: bool = False,
        rca_solution: dict[str, Any] | None = None,
        rca_trace: list[dict[str, str]] | None = None,
        mitigation_actions: ComposeMitigationActions | None = None,
        prior_context: str | None = None,
        prior_evidence: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[ProblemContext, Any, AgentSession]:
        if self.agent is None:
            raise AgentRuntimeError("Register an agent before initializing a problem")
        try:
            definition = self.registry.get_problem_definition(problem_id)
            task, scenario, _ = self.registry.create(
                problem_id,
                start=start,
                end=end,
                run_dir=run_dir,
                withhold_ground_truth=True,
                rca_solution=rca_solution,
                rca_trace=rca_trace,
                mitigation_actions=mitigation_actions,
                prior_evidence=prior_evidence,
            )
        except (ProblemError, OSError) as error:
            raise AgentRuntimeError(str(error)) from error
        instructions = task.get_instructions()
        if prior_context:
            instructions += (
                "\n\nFindings from earlier stages of this same incident follow. "
                "They are agent observations and hypotheses, not evaluator labels. "
                "Continue from them. Do not repeat an evidence query already answered "
                "unless the evidence is stale or insufficient.\n" + prior_context
            )
            if task.task_name == "mitigation":
                instructions += (
                    "\nThe fault may expire soon. If the prior RCA and recommendation "
                    "justify restoring the identified service, make restore_service "
                    "your first action. Verify afterward, then submit."
                )
        context = ProblemContext(
            problem_id=definition.problem_id,
            description=task.get_task_description(),
            instructions=instructions,
            actions=task.get_available_actions(),
        )
        init_context = getattr(self.agent, "init_context", None)
        if callable(init_context):
            init_context(context.description, context.instructions, context.actions)
        session = AgentSession(problem_id=problem_id, agent_name=self.agent_name)
        session.notes = task.evidence.notes
        session.add("system", context.description)
        session.add("user", context.instructions)
        return context, task, session

    async def start_problem(
        self,
        task: Any,
        session: AgentSession,
        *,
        max_steps: int = 15,
        evaluate: bool = True,
        persist: bool = True,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        initial_observation: str | None = None,
    ) -> dict[str, Any]:
        if self.agent is None:
            raise AgentRuntimeError("Register an agent before starting a problem")
        if max_steps < 1:
            raise AgentRuntimeError("max_steps must be positive")
        session.start()
        enabled = self.config.aiops_strategy == "enabled"
        if self.config.aiops_strategy not in {"enabled", "baseline"}:
            raise AgentRuntimeError("AIOPS_STRATEGY must be enabled or baseline")
        if (self.config.aiops_max_cost_usd is not None and
                (self.config.aiops_input_usd_per_million is None or
                 self.config.aiops_output_usd_per_million is None)):
            raise AgentRuntimeError("AIOPS_MAX_COST_USD requires both configured model rates")
        cache = QueryCache()
        seen_fingerprints: set[str] = set()
        metrics = session.metrics
        metrics.update({
            "strategy": self.config.aiops_strategy, "stage_steps_used": 0,
            "incident_steps_used": self.incident_budget.steps_used,
            "valid_actions": 0, "invalid_actions": 0, "duplicate_attempts": 0,
            "cache_hits": 0, "queries_avoided": 0, "actual_queries": 0,
            "stagnant_steps": 0, "context_tokens_peak_estimated": 0,
            "observation_chars": 0, "input_tokens": 0, "output_tokens": 0,
            "usage_source": "unavailable", "cost_usd": None,
            "cost_source": "unavailable", "model": None,
        })
        if event_sink is not None:
            event_sink({"kind": "session_started", "problem_id": session.problem_id})
        observation = initial_observation or f"Step 1 of {max_steps}. Start with the recommended triage action."
        try:
            for step in range(1, max_steps + 1):
                if enabled:
                    reason = self._limit_reason(session)
                    if reason:
                        session.termination_reason = reason
                        break
                context_size = estimate_tokens(str(getattr(self.agent, "messages", [])) + observation)
                metrics["context_tokens_peak_estimated"] = max(
                    metrics["context_tokens_peak_estimated"], context_size
                )
                if enabled and (
                    self.incident_budget.input_tokens + context_size
                    + self.config.aiops_final_reserve_tokens > self.config.aiops_max_input_tokens
                ):
                    session.termination_reason = "input_token_budget_exhausted"
                    break
                if enabled and self.config.aiops_max_cost_usd is not None and (
                    self.config.aiops_input_usd_per_million is not None
                    and self.config.aiops_output_usd_per_million is not None
                ):
                    projected = self.incident_budget.cost_usd + (
                        context_size * self.config.aiops_input_usd_per_million
                        + 512 * self.config.aiops_output_usd_per_million
                    ) / 1_000_000
                    if projected > self.config.aiops_max_cost_usd:
                        session.termination_reason = "cost_budget_exhausted"
                        break
                if enabled and context_size > self.config.aiops_context_compact_tokens:
                    self._compact_agent_context(session)
                try:
                    response = await asyncio.wait_for(
                        self.agent.get_action(observation),
                        timeout=self.config.aiops_llm_timeout_seconds,
                    )
                except TimeoutError:
                    session.termination_reason = "model_timeout"
                    break
                output = getattr(self.agent, "last_output", None)
                if not isinstance(output, ProviderOutput) or output.text != response:
                    output = ProviderOutput(response)
                input_count = output.input_tokens if output.input_tokens is not None else context_size
                output_count = output.output_tokens if output.output_tokens is not None else estimate_tokens(response)
                metrics["model"] = output.model
                metrics["usage_source"] = (
                    "actual" if output.usage_source == "actual" and metrics["usage_source"] in {"actual", "unavailable"}
                    else "estimated"
                )
                metrics["input_tokens"] += input_count
                metrics["output_tokens"] += output_count
                self.incident_budget.input_tokens += input_count
                self.incident_budget.output_tokens += output_count
                if self.config.aiops_input_usd_per_million is not None and self.config.aiops_output_usd_per_million is not None:
                    cost = (input_count * self.config.aiops_input_usd_per_million
                            + output_count * self.config.aiops_output_usd_per_million) / 1_000_000
                    metrics["cost_usd"] = (metrics["cost_usd"] or 0) + cost
                    metrics["cost_source"] = metrics["usage_source"]
                    self.incident_budget.cost_usd += cost
                metrics["stage_steps_used"] += 1
                self.incident_budget.steps_used += 1
                metrics["incident_steps_used"] = self.incident_budget.steps_used
                session.add("assistant", response)
                if event_sink is not None:
                    event_sink({
                        "kind": "agent_response",
                        "step": step,
                        "content": response,
                    })
                try:
                    action_name, args, kwargs = self.parser.parse(response)
                    if enabled:
                        normalized = validate_action(task, action_name, args, kwargs)
                    else:
                        normalized = kwargs
                    if event_sink is not None:
                        event_sink({
                            "kind": "action_parsed",
                            "step": step,
                            "action": action_name,
                            "args": list(args),
                            "kwargs": kwargs,
                        })
                    fingerprint = None
                    cached = None
                    if action_name in READ_ACTIONS:
                        identity = str(task.evidence.run_dir or task.evidence.start.isoformat())
                        fingerprint_args = dict(normalized)
                        if action_name == "get_metrics":
                            fingerprint_args["resolution"] = "10s"
                        if task.evidence.run_dir is None and task.evidence.end is None:
                            fingerprint_args["live_window_end_bucket"] = int(
                                datetime.now(UTC).timestamp() // 10
                            )
                        fingerprint = action_fingerprint(action_name, fingerprint_args, identity)
                        if fingerprint in seen_fingerprints:
                            metrics["duplicate_attempts"] += 1
                        seen_fingerprints.add(fingerprint)
                        if enabled:
                            cached = cache.get(
                                fingerprint, replay=task.evidence.run_dir is not None,
                                ttl_seconds=self.config.aiops_live_cache_ttl_seconds,
                            )
                    if cached is not None:
                        metrics["cache_hits"] += 1
                        metrics["queries_avoided"] += 1
                        env_response = cached
                    elif enabled and action_name in READ_ACTIONS:
                        env_response = task.perform_action(action_name, *args, **kwargs)
                        metrics["actual_queries"] += 1
                        cache.put(fingerprint, env_response)
                    else:
                        env_response = task.perform_action(action_name, *args, **kwargs)
                        if action_name in READ_ACTIONS:
                            metrics["actual_queries"] += 1
                    metrics["valid_actions"] += 1
                    observation = env_response.content
                except (ActionParseError, EnvironmentError, TaskError, TypeError, ValueError, TimeoutError) as error:
                    observation = f"Action error: {error}"
                    env_response = EnvironmentResponse(observation)
                    session.invalid_action_count += 1
                    metrics["invalid_actions"] += 1
                    if event_sink is not None:
                        event_sink({
                            "kind": "invalid_action",
                            "step": step,
                            "error": str(error),
                            "invalid_action_count": session.invalid_action_count,
                        })
                session.add("env", observation)
                metrics["observation_chars"] += len(observation)
                if enabled and not env_response.submitted:
                    evidence_ids = list(getattr(task.evidence, "evidence_store", {}))
                    previous = set(metrics.get("evidence_ids", []))
                    new_ids = [item for item in evidence_ids if item not in previous]
                    metrics["evidence_ids"] = evidence_ids
                    note_count = len(session.notes)
                    notes_updated = note_count > metrics.get("notes_seen", 0)
                    if new_ids or notes_updated:
                        metrics["stagnant_steps"] = 0
                        metrics["progress_reason"] = "new evidence or hypothesis update"
                    if new_ids:
                        session.notes.append({
                            "hypothesis_id": f"h-{len(session.notes) + 1}",
                            "candidate_service": None, "candidate_fault": None,
                            "status": "evidence_collected",
                            "supporting_evidence_ids": new_ids,
                            "contradicting_evidence_ids": [],
                            "next_question": "Which service and fault does this evidence support?",
                            "updated_at": datetime.now(UTC).isoformat(),
                        })
                    metrics["notes_seen"] = len(session.notes)
                    if not new_ids and not notes_updated:
                        metrics["stagnant_steps"] += 1
                        metrics["progress_reason"] = "no new evidence or hypothesis update"
                    if metrics["stagnant_steps"] == max(1, self.config.aiops_stagnant_limit // 2):
                        observation += "\nName one specific missing piece of evidence before querying again."
                    if metrics["stagnant_steps"] >= self.config.aiops_stagnant_limit:
                        session.termination_reason = "progress_stalled"
                        break
                if event_sink is not None:
                    event_sink({
                        "kind": "observation",
                        "step": step,
                        "content": observation,
                        "submitted": env_response.submitted,
                    })
                if env_response.submitted:
                    session.solution = env_response.solution
                    session.notes.append({
                        "hypothesis_id": f"h-{len(session.notes) + 1}",
                        "candidate_service": (
                            (env_response.solution.get("faulty_components") or [None])[0]
                            if isinstance(env_response.solution, dict) else None
                        ),
                        "candidate_fault": (
                            env_response.solution.get("fault_type")
                            if isinstance(env_response.solution, dict) else None
                        ),
                        "status": "submitted", "supporting_evidence_ids": metrics.get("evidence_ids", []),
                        "contradicting_evidence_ids": [], "next_question": None,
                        "updated_at": datetime.now(UTC).isoformat(),
                    })
                    session.submitted = True
                    session.submitted_at = datetime.now(UTC)
                    if session.start_time is not None:
                        session.submission_elapsed_seconds = (
                            session.submitted_at - session.start_time
                        ).total_seconds()
                    session.termination_reason = "submitted"
                    if event_sink is not None:
                        event_sink({
                            "kind": "submitted",
                            "step": step,
                            "submitted_at": session.submitted_at.isoformat()
                            if session.submitted_at else None,
                        })
                    break
                if step < max_steps:
                    remaining = max_steps - step
                    urgency = " Submit now if you have enough evidence." if remaining <= 2 else ""
                    observation = f"Step {step + 1} of {max_steps}; {remaining} steps remain.{urgency}\n{observation}"
            else:
                session.termination_reason = "step_budget_exhausted"
            if not session.submitted:
                hypotheses = [note for note in session.notes if note.get("candidate_service")
                              or note.get("candidate_fault")]
                current = hypotheses[-1] if hypotheses else None
                session.partial_result = {
                    "hypothesis": current,
                    "evidence_ids": metrics.get("evidence_ids", []),
                    "unverified_claims": [
                        {"candidate_service": note.get("candidate_service"),
                         "candidate_fault": note.get("candidate_fault")}
                        for note in hypotheses if not note.get("supporting_evidence_ids")
                    ],
                    "stop_reason": session.termination_reason,
                }
            session.end()
            if event_sink is not None:
                event_sink({
                    "kind": "session_finished",
                    "submitted": session.submitted,
                    "termination_reason": session.termination_reason,
                    "duration_seconds": session.duration_seconds,
                })
            if evaluate:
                return self.finalize_session(task, session, persist=persist)
            return session.to_dict()
        except Exception:
            session.end()
            raise

    def _limit_reason(self, session: AgentSession) -> str | None:
        config = self.config
        budget = self.incident_budget
        if budget.steps_used >= config.aiops_incident_max_steps:
            return "incident_step_budget_exhausted"
        if budget.input_tokens >= config.aiops_max_input_tokens - config.aiops_final_reserve_tokens:
            return "input_token_budget_exhausted"
        if budget.output_tokens >= config.aiops_max_output_tokens - config.aiops_final_reserve_tokens:
            return "output_token_budget_exhausted"
        if config.aiops_max_cost_usd is not None and budget.cost_usd >= config.aiops_max_cost_usd:
            return "cost_budget_exhausted"
        if session.start_time and (datetime.now(UTC) - session.start_time).total_seconds() >= config.aiops_max_stage_seconds:
            return "time_budget_exhausted"
        return None

    def _compact_agent_context(self, session: AgentSession) -> None:
        messages = getattr(self.agent, "messages", None)
        if not isinstance(messages, list) or len(messages) < 5:
            return
        notes = session.notes[-5:]
        evidence_ids = session.metrics.get("evidence_ids", [])
        compact = {"notes": notes, "evidence_ids": evidence_ids}
        self.agent.messages = [messages[0], {
            "role": "user", "content": "Investigation state: " + str(compact)
        }, *messages[-4:]]
        session.metrics["compactions"] = session.metrics.get("compactions", 0) + 1

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
        elif getattr(task.evidence, "run_dir", None) is not None:
            import json
            task.expected = json.loads((task.evidence.run_dir / "ground_truth.json").read_text(encoding="utf-8"))
        session.results = task.eval(session.solution, session.history, session.duration_seconds)
        session.results.update(
            {
                "submitted": session.submitted,
                "termination_reason": session.termination_reason,
                "invalid_action_count": session.invalid_action_count,
                "partial_result": session.partial_result is not None,
            }
        )
        output = session.to_dict()
        if persist:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            write_json(self.results_dir / f"{session.session_id}.json", output)
        return output


def create_orchestrator(config: ResearchConfig, provider: LlmProvider | None = None) -> AgentOrchestrator:
    orchestrator = AgentOrchestrator(config)
    orchestrator.register_agent(LlmAgent(provider or build_provider(config)), name="llm-agent")
    return orchestrator
