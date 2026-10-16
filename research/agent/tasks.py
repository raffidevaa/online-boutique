"""AIOpsLab-style task contracts and Compose-safe task implementations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from research.generators.fault import VALID_FAULT_TYPES

from .environment import ComposeEvidence


@dataclass(frozen=True)
class EnvironmentResponse:
    content: str
    submitted: bool = False
    solution: Any = None


class TaskError(ValueError):
    """Raised for invalid task actions or submissions."""


class BaseTask:
    task_name = "base"

    def __init__(self, evidence: ComposeEvidence, expected: dict[str, Any]) -> None:
        self.evidence = evidence
        self.expected = expected
        self.actions: dict[str, Callable[..., EnvironmentResponse]] = {
            "get_incident_summary": self.get_incident_summary,
            "get_error_logs": self.get_error_logs,
            "get_metrics": self.get_metrics,
            "get_logs": self.get_logs,
            "get_traces": self.get_traces,
            "get_alerts": self.get_alerts,
        }

    def get_metrics(self, service: str | None = None, window: str = "incident") -> EnvironmentResponse:
        return EnvironmentResponse(self.evidence.get_metrics(service, window))

    def get_incident_summary(self, window: str = "incident") -> EnvironmentResponse:
        return EnvironmentResponse(self.evidence.get_incident_summary(window))

    def get_error_logs(
        self, service: str | None = None, window: str = "incident"
    ) -> EnvironmentResponse:
        return EnvironmentResponse(self.evidence.get_error_logs(service, window))

    def get_logs(self, service: str | None = None, window: str = "incident") -> EnvironmentResponse:
        return EnvironmentResponse(self.evidence.get_logs(service, window))

    def get_traces(self, service: str | None = None, window: str = "incident") -> EnvironmentResponse:
        return EnvironmentResponse(self.evidence.get_traces(service, window))

    def get_alerts(self, window: str = "incident") -> EnvironmentResponse:
        return EnvironmentResponse(self.evidence.get_alerts(window))

    def get_task_description(self) -> str:
        return "You are an SRE analyzing an Online Boutique incident."

    def get_instructions(self) -> str:
        return "Use one read-only action per turn and submit your answer when ready."

    def get_available_actions(self) -> dict[str, str]:
        return {
            "get_incident_summary": 'get_incident_summary(window: str = "incident")',
            "get_error_logs": 'get_error_logs(service: str | None = None, window: str = "incident")',
            "get_metrics": 'get_metrics(service: str | None = None, window: str = "incident")',
            "get_logs": 'get_logs(service: str | None = None, window: str = "incident")',
            "get_traces": 'get_traces(service: str | None = None, window: str = "incident")',
            "get_alerts": 'get_alerts(window: str = "incident")',
        }

    def perform_action(self, action_name: str, *args: Any, **kwargs: Any) -> EnvironmentResponse:
        action = self.actions.get(action_name)
        if action is None:
            raise TaskError(f"Invalid action: {action_name}")
        return action(*args, **kwargs)

    def eval(self, solution: Any, trace: list[Any], duration: float) -> dict[str, Any]:
        return {"steps": len([item for item in trace if item.role == "assistant"]), "duration_seconds": duration}


class DetectionTask(BaseTask):
    task_name = "detection"

    def __init__(self, evidence: ComposeEvidence, expected: dict[str, Any]) -> None:
        super().__init__(evidence, expected)
        self.actions["submit"] = self.submit

    def get_task_description(self) -> str:
        return "Detect whether the supplied telemetry contains an anomaly."

    def get_instructions(self) -> str:
        return (
            'Start with get_incident_summary(window="incident"), then use at most one focused '
            'evidence action if needed. Submit "Yes" or "No" before the final step.'
        )

    def get_available_actions(self) -> dict[str, str]:
        actions = super().get_available_actions()
        actions["submit"] = 'submit(answer: "Yes" | "No")'
        return actions

    def submit(self, answer: str) -> EnvironmentResponse:
        if answer not in {"Yes", "No"}:
            raise TaskError('Detection submission must be "Yes" or "No"')
        return EnvironmentResponse("VALID_SUBMISSION", True, answer)

    def eval(self, solution: Any, trace: list[Any], duration: float) -> dict[str, Any]:
        results = super().eval(solution, trace, duration)
        expected = "Yes" if self.expected else "No"
        results.update({"task": self.task_name, "detection_accuracy": solution == expected, "TTD": duration})
        return results


class LocalizationTask(BaseTask):
    task_name = "localization"

    def __init__(self, evidence: ComposeEvidence, expected: dict[str, Any]) -> None:
        super().__init__(evidence, expected)
        self.actions["submit"] = self.submit

    def get_task_description(self) -> str:
        return "Identify the faulty service from the supplied telemetry."

    def get_instructions(self) -> str:
        return (
            'Start with get_incident_summary(window="incident"). Focus follow-up evidence on '
            'the top one or two anomalous services, then submit a JSON object such as '
            'submit({"faulty_components": ["service"]}) before the final step.'
        )

    def get_available_actions(self) -> dict[str, str]:
        actions = super().get_available_actions()
        actions["submit"] = 'submit(answer: {"faulty_components": ["service"]})'
        return actions

    def submit(self, answer: dict[str, Any]) -> EnvironmentResponse:
        components = answer.get("faulty_components") if isinstance(answer, dict) else None
        if not isinstance(components, list) or not all(isinstance(item, str) for item in components):
            raise TaskError("faulty_components must be a list of service names")
        return EnvironmentResponse("VALID_SUBMISSION", True, answer)

    def eval(self, solution: Any, trace: list[Any], duration: float) -> dict[str, Any]:
        results = super().eval(solution, trace, duration)
        predicted = (solution or {}).get("faulty_components", []) if isinstance(solution, dict) else []
        target = self.expected.get("root_cause_service")
        results.update({
            "task": self.task_name,
            "localization_exact": predicted == [target],
            "localization_top3": target in predicted[:3],
            "TTL": duration,
        })
        return results


class AnalysisTask(BaseTask):
    task_name = "analysis"

    def __init__(self, evidence: ComposeEvidence, expected: dict[str, Any]) -> None:
        super().__init__(evidence, expected)
        self.actions["submit"] = self.submit

    def get_task_description(self) -> str:
        return "Perform root-cause analysis and identify category, fault type, and service."

    def get_instructions(self) -> str:
        fault_types = "|".join(VALID_FAULT_TYPES)
        return (
            'First call get_incident_summary(window="incident"). Then inspect error logs or '
            'traces for at most two top-ranked anomalous services; do not enumerate healthy '
            'services. Submit JSON before the final step: '
            'submit({"system_level": "resource|network|code_level", '
            f'"fault_type": "{fault_types}", '
            '"faulty_components": ["service"]}). Use only the normalized fault_type '
            'identifiers listed above and reserve the final step for submission.'
        )

    def get_available_actions(self) -> dict[str, str]:
        actions = super().get_available_actions()
        fault_types = "|".join(VALID_FAULT_TYPES)
        actions["submit"] = (
            'submit(answer: {"system_level": "resource|network|code_level", '
            f'"fault_type": "{fault_types}", '
            '"faulty_components": ["service"]})'
        )
        return actions

    def submit(self, answer: dict[str, Any]) -> EnvironmentResponse:
        if not isinstance(answer, dict):
            raise TaskError("Analysis submission must be an object")
        required = {"system_level", "fault_type", "faulty_components"}
        if not required.issubset(answer):
            raise TaskError(f"Analysis submission requires: {', '.join(sorted(required))}")
        if not isinstance(answer["faulty_components"], list):
            raise TaskError("faulty_components must be a list")
        return EnvironmentResponse("VALID_SUBMISSION", True, answer)

    def eval(self, solution: Any, trace: list[Any], duration: float) -> dict[str, Any]:
        results = super().eval(solution, trace, duration)
        solution = solution if isinstance(solution, dict) else {}
        expected = self.expected
        service_match = expected.get("root_cause_service") in solution.get("faulty_components", [])
        category_match = solution.get("system_level") == expected.get("fault_category")
        type_match = solution.get("fault_type") == expected.get("fault_type")
        results.update({
            "task": self.task_name,
            "root_cause_service_correct": service_match,
            "fault_category_correct": category_match,
            "fault_type_correct": type_match,
            "combined_rca_correct": service_match and category_match and type_match,
            "TTA": duration,
        })
        return results
