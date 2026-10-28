"""AIOpsLab-style task contracts and Compose-safe task implementations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from research.generators.fault import VALID_FAULT_TYPES

from .environment import ComposeEvidence
from .remediation import (
    REMEDIATION_ACTION_TYPES,
    validate_remediation,
)


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
        taxonomy = (
            "resource=[cpu_hog,memory_pressure,disk_stress,socket_stress]; "
            "network=[network_delay,packet_loss]; "
            "code_level=[incorrect_return_value,missing_exception_handler,"
            "incorrect_parameter,missing_parameter,missing_function_call]"
        )
        return (
            'First call get_incident_summary(window="incident"). Then inspect error logs or '
            'traces for at most two top-ranked anomalous services; do not enumerate healthy '
            'services or infrastructure/workload services. Distinguish the root-cause service '
            'from propagated frontend errors. Submit JSON before the final step with up to three '
            'services ordered by confidence: '
            'submit({"system_level": "resource|network|code_level", '
            f'"fault_type": "{fault_types}", '
            '"faulty_components": ["service1", "service2"]}). Taxonomy mapping: '
            f'{taxonomy}. Use only the normalized fault_type '
            'identifiers. Diagnostic rules: memory_growth dominant with CPU not dominant '
            'supports memory_pressure; cpu_growth dominant supports cpu_hog; latency dominant '
            'network_symptoms or latency dominant supports network_delay; error_rate or trace_errors dominant with connection or '
            'RPC failures supports packet_loss. In network_evidence, a large incident median '
            'latency shift with most calls slow supports network_delay; a near-baseline median '
            'with sparse latency outliers or new RPC errors supports packet_loss. Treat the '
            'peer_service in network_evidence as the likely affected service, and the caller '
            'as propagated context. For resource faults, absolute memory growth of roughly '
            '32 MB or more supports memory_pressure; CPU growth of 0.1 or more with less than '
            '32 MB memory growth supports cpu_hog, even when a small baseline makes the '
            'relative memory percentage look large. These are evidence rules, not labels. '
            'Reserve the final step for submission.'
        )

    def get_available_actions(self) -> dict[str, str]:
        actions = super().get_available_actions()
        fault_types = "|".join(VALID_FAULT_TYPES)
        actions["submit"] = (
            'submit(answer: {"system_level": "resource|network|code_level", '
            f'"fault_type": "{fault_types}", '
            '"faulty_components": ["service1", "service2", "service3"]})'
        )
        return actions

    def submit(self, answer: dict[str, Any]) -> EnvironmentResponse:
        if not isinstance(answer, dict):
            raise TaskError("Analysis submission must be an object")
        required = {"system_level", "fault_type", "faulty_components"}
        if not required.issubset(answer):
            raise TaskError(f"Analysis submission requires: {', '.join(sorted(required))}")
        if answer["system_level"] not in {"resource", "network", "code_level"}:
            raise TaskError("system_level must be resource, network, or code_level")
        fault_type = answer["fault_type"]
        if fault_type not in VALID_FAULT_TYPES:
            raise TaskError("fault_type must use a normalized taxonomy identifier")
        if VALID_FAULT_TYPES[fault_type] != answer["system_level"]:
            raise TaskError("fault_type does not belong to system_level")
        components = answer["faulty_components"]
        if not isinstance(components, list) or not components or len(components) > 3:
            raise TaskError("faulty_components must contain one to three services")
        if not all(isinstance(item, str) and item for item in components):
            raise TaskError("faulty_components must contain service names")
        if len(set(components)) != len(components):
            raise TaskError("faulty_components must not contain duplicates")
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


class RemediationTask(BaseTask):
    """Produce a reviewable remediation plan without an execution capability."""

    task_name = "remediation"

    def __init__(
        self,
        evidence: ComposeEvidence,
        expected: dict[str, Any],
        rca_solution: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(evidence, expected)
        self.rca_solution = rca_solution if isinstance(rca_solution, dict) else {}
        self.actions["submit"] = self.submit

    def get_task_description(self) -> str:
        return "Recommend a safe, reviewable remediation action for the RCA hypothesis."

    def get_instructions(self) -> str:
        action_types = "|".join(REMEDIATION_ACTION_TYPES)
        rca = json.dumps(self.rca_solution, sort_keys=True, separators=(",", ":"))
        return (
            "You are in the remediation recommendation stage after an RCA analysis. "
            "Use only read-only evidence actions and do not execute or propose an arbitrary "
            "shell, Docker, Compose, or injector command. The previous RCA submission was: "
            f"{rca}. Verify the mechanism with incident evidence before submitting one "
            "reviewable action plan. Choose an action_type from "
            f"{action_types}. Include one to five evidence statements, expected effect, "
            "risk, and a reversible rollback_plan. Always set requires_approval=true and "
            "execution_allowed=false. If evidence is insufficient, use manual_investigation "
            "or escalate rather than inventing a configuration change. Submit JSON before "
            "the final step."
        )

    def get_available_actions(self) -> dict[str, str]:
        actions = super().get_available_actions()
        action_types = "|".join(REMEDIATION_ACTION_TYPES)
        actions["submit"] = (
            'submit(answer: {"action_type": "' + action_types + '", '
            '"target_service": "service", "rationale": "...", '
            '"evidence": ["..."], "expected_effect": "...", '
            '"risk": "low|medium|high", "rollback_plan": "...", '
            '"requires_approval": true, "execution_allowed": false})'
        )
        return actions

    def submit(self, answer: dict[str, Any]) -> EnvironmentResponse:
        try:
            validated = validate_remediation(
                answer,
                self.evidence.get_targetable_services(),
            )
        except ValueError as error:
            raise TaskError(str(error)) from error
        return EnvironmentResponse("VALID_REMEDIATION_SUBMISSION", True, validated)

    def eval(self, solution: Any, trace: list[Any], duration: float) -> dict[str, Any]:
        results = super().eval(solution, trace, duration)
        valid = False
        if isinstance(solution, dict):
            try:
                validate_remediation(solution, self.evidence.get_targetable_services())
                valid = True
            except ValueError:
                pass
        results.update(
            {
                "task": self.task_name,
                "recommendation_valid": valid,
                "recommendation_complete": valid,
                "TTA": duration,
            }
        )
        return results
