"""Factory-backed problem registry for scenario YAML and AIOps task variants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from research.generators import FaultScenario, load_scenario
from research.utils import ResearchConfig

from .environment import ComposeEvidence
from .actions import ComposeMitigationActions
from .tasks import (
    AnalysisTask,
    BaseTask,
    DetectionTask,
    LocalizationTask,
    MitigationTask,
    RemediationTask,
)


class ProblemError(ValueError):
    """Raised when a problem ID or evidence source is invalid."""


TASKS = {
    "detection": DetectionTask,
    "localization": LocalizationTask,
    "analysis": AnalysisTask,
    "mitigation": MitigationTask,
    "remediation": RemediationTask,
}


@dataclass(frozen=True)
class ProblemDefinition:
    """The five components of an AIOpsLab problem, independent of runtime."""

    problem_id: str
    application: str
    task: str
    fault: FaultScenario
    workload: dict[str, Any]
    evaluator: str


class ComposeProblemRegistry:
    def __init__(self, config: ResearchConfig) -> None:
        self.config = config
        self._scenarios = self._load_scenarios()

    def _load_scenarios(self) -> dict[str, FaultScenario]:
        scenarios: dict[str, FaultScenario] = {}
        for path in sorted(self.config.scenario_directory.glob("*.yaml")):
            scenario = load_scenario(path)
            scenarios[scenario.scenario_id] = scenario
        return scenarios

    def get_problem_ids(self) -> list[str]:
        return [f"{scenario_id}-{task}" for scenario_id in sorted(self._scenarios) for task in TASKS]

    def get_problem_definition(self, problem_id: str) -> ProblemDefinition:
        scenario, task_name = self._split_problem_id(problem_id)
        return ProblemDefinition(
            problem_id=problem_id,
            application="online-boutique",
            task=task_name,
            fault=scenario,
            workload=dict(scenario.workload),
            evaluator=task_name,
        )

    def _split_problem_id(self, problem_id: str) -> tuple[FaultScenario, str]:
        for task_name in TASKS:
            suffix = f"-{task_name}"
            if problem_id.endswith(suffix):
                scenario_id = problem_id[: -len(suffix)]
                try:
                    return self._scenarios[scenario_id], task_name
                except KeyError as error:
                    raise ProblemError(f"Unknown scenario in problem ID: {problem_id}") from error
        raise ProblemError(f"Problem ID must end with one of: {', '.join(TASKS)}")

    def _expected(self, scenario: FaultScenario, run_dir: Path | None) -> dict[str, Any]:
        if run_dir is None:
            return {}
        path = run_dir / "ground_truth.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProblemError(f"Unable to load evaluator ground truth: {path}") from error

    def create(
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
    ) -> tuple[BaseTask, FaultScenario, dict[str, Any]]:
        definition = self.get_problem_definition(problem_id)
        scenario, task_name = definition.fault, definition.task
        if run_dir is not None:
            resolved = run_dir.resolve()
            try:
                resolved.relative_to(self.config.experiment_runs.resolve())
            except ValueError as error:
                raise ProblemError("Replay run must be under research/experiments/runs") from error
            run_dir = resolved
        evidence = ComposeEvidence(self.config, start, end, run_dir)
        # Replay needs the run directory for telemetry, but the agent must not receive
        # evaluator labels during its action loop.  The evaluator hands the labels to
        # ``finalize_session`` only after the loop has ended.
        expected = {} if withhold_ground_truth else self._expected(scenario, run_dir)
        if task_name == "mitigation":
            if run_dir is not None or mitigation_actions is None:
                raise ProblemError("Mitigation requires an active live action adapter")
            task = MitigationTask(evidence, expected, mitigation_actions)
        elif task_name == "remediation":
            task = RemediationTask(
                evidence,
                expected,
                rca_solution=rca_solution,
                rca_trace=rca_trace,
            )
        else:
            task = TASKS[task_name](evidence, expected)
        return task, scenario, expected
