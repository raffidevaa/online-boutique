"""Validated, tool-neutral fault scenario definitions.

Scenario files describe research intent. They never contain shell fragments or arbitrary
commands; delivery adapters translate the small, validated parameter vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import yaml


FaultStatus = Literal[
    "ready_for_pilot", "experimental_unavailable", "deferred_faulty_image"
]
FaultCategory = Literal["resource", "network", "code_level"]

VALID_FAULT_TYPES: dict[str, FaultCategory] = {
    "cpu_hog": "resource",
    "memory_pressure": "resource",
    "disk_stress": "resource",
    "socket_stress": "resource",
    "network_delay": "network",
    "packet_loss": "network",
    "incorrect_return_value": "code_level",
    "missing_exception_handler": "code_level",
    "incorrect_parameter": "code_level",
    "missing_parameter": "code_level",
    "missing_function_call": "code_level",
}

RUNNABLE_TYPES = {"cpu_hog", "memory_pressure", "network_delay", "packet_loss"}

PARAMETER_KEYS: dict[str, set[str]] = {
    "cpu_hog": {"duration_seconds", "cpu_workers", "intended_cpu_percent"},
    "memory_pressure": {
        "duration_seconds",
        "memory_workers",
        "memory_mb_per_worker",
        "intended_memory_percent",
    },
    "network_delay": {"duration_seconds", "delay_ms", "jitter_ms"},
    "packet_loss": {"duration_seconds", "loss_percent"},
    "disk_stress": {"duration_seconds", "delivery_requirement"},
    "socket_stress": {"duration_seconds", "delivery_requirement"},
    "incorrect_return_value": {"duration_seconds", "delivery_requirement"},
    "missing_exception_handler": {"duration_seconds", "delivery_requirement"},
    "incorrect_parameter": {"duration_seconds", "delivery_requirement"},
    "missing_parameter": {"duration_seconds", "delivery_requirement"},
    "missing_function_call": {"duration_seconds", "delivery_requirement"},
}


class ScenarioError(ValueError):
    """Raised when a scenario is not a safe, complete catalog entry."""


@dataclass(frozen=True)
class FaultSpec:
    """A fault description, not an executable shell command."""

    fault_type: str
    duration: timedelta
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FaultScenario:
    """The normalized catalog contract shared by planning and execution."""

    scenario_id: str
    category: FaultCategory
    fault_type: str
    execution_status: FaultStatus
    target_service: str | None
    workload: dict[str, Any]
    parameters: dict[str, Any]
    severity: Literal["low", "medium", "high"]
    expected_local_symptoms: tuple[str, ...]
    expected_propagated_symptoms: tuple[str, ...]
    expected_telemetry_evidence: dict[str, Any]
    ground_truth: dict[str, str | None]
    expected_remediation: tuple[str, ...]
    recovery: str
    semantic_probe: dict[str, Any] | None = None
    source_path: Path | None = None

    @property
    def duration_seconds(self) -> int:
        return int(self.parameters["duration_seconds"])

    @property
    def runnable(self) -> bool:
        return self.execution_status == "ready_for_pilot"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scenario_id": self.scenario_id,
            "category": self.category,
            "fault_type": self.fault_type,
            "execution_status": self.execution_status,
            "target_service": self.target_service,
            "workload": self.workload,
            "parameters": self.parameters,
            "severity": self.severity,
            "expected_local_symptoms": list(self.expected_local_symptoms),
            "expected_propagated_symptoms": list(self.expected_propagated_symptoms),
            "expected_telemetry_evidence": self.expected_telemetry_evidence,
            "ground_truth": self.ground_truth,
            "expected_remediation": list(self.expected_remediation),
            "recovery": self.recovery,
            "semantic_probe": self.semantic_probe,
        }


REQUIRED_KEYS = {
    "schema_version",
    "scenario_id",
    "category",
    "fault_type",
    "execution_status",
    "target_service",
    "workload",
    "parameters",
    "severity",
    "expected_local_symptoms",
    "expected_propagated_symptoms",
    "expected_telemetry_evidence",
    "ground_truth",
    "expected_remediation",
    "recovery",
}

OPTIONAL_KEYS = {"semantic_probe"}


def _require_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError(f"{field_name} must be a mapping")
    return value


def _require_strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ScenarioError(f"{field_name} must be a list of non-empty strings")
    return tuple(value)


def load_scenario(path: Path) -> FaultScenario:
    """Load one strict, versioned scenario YAML definition."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ScenarioError(f"Unable to read scenario {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ScenarioError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ScenarioError(f"Scenario must be a mapping: {path}")
    missing = REQUIRED_KEYS - raw.keys()
    extra = raw.keys() - REQUIRED_KEYS - OPTIONAL_KEYS
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected " + ", ".join(sorted(extra)))
        raise ScenarioError(f"Invalid scenario keys in {path}: {'; '.join(details)}")
    if raw["schema_version"] != 1:
        raise ScenarioError(f"Unsupported scenario schema in {path}")
    fault_type = raw["fault_type"]
    category = raw["category"]
    if fault_type not in VALID_FAULT_TYPES or VALID_FAULT_TYPES[fault_type] != category:
        raise ScenarioError(f"Invalid category/fault_type pair in {path}")
    status = raw["execution_status"]
    if status not in {"ready_for_pilot", "experimental_unavailable", "deferred_faulty_image"}:
        raise ScenarioError(f"Invalid execution_status in {path}")
    if status == "ready_for_pilot" and fault_type not in RUNNABLE_TYPES:
        raise ScenarioError(f"Unsupported runnable fault type in {path}")
    if status == "deferred_faulty_image" and category != "code_level":
        raise ScenarioError(f"Only code-level faults may require faulty images: {path}")
    semantic_probe = raw.get("semantic_probe")
    if category == "code_level":
        if not isinstance(semantic_probe, dict):
            raise ScenarioError(f"Code-level scenarios require semantic_probe: {path}")
        probe_type = semantic_probe.get("type")
        if probe_type not in {"currency_display", "checkout_flow"}:
            raise ScenarioError(f"Invalid semantic_probe.type in {path}")
        product_id = semantic_probe.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            raise ScenarioError(f"semantic_probe.product_id must be non-empty: {path}")
        repetitions = semantic_probe.get("repetitions", 1)
        timeout_seconds = semantic_probe.get("timeout_seconds", 10)
        for name, value in (("repetitions", repetitions), ("timeout_seconds", timeout_seconds)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ScenarioError(f"semantic_probe.{name} must be positive: {path}")
        if repetitions > 10:
            raise ScenarioError(f"semantic_probe.repetitions cannot exceed 10: {path}")
        if timeout_seconds > 60:
            raise ScenarioError(f"semantic_probe.timeout_seconds cannot exceed 60: {path}")
        if probe_type == "currency_display":
            currency = semantic_probe.get("currency")
            if not isinstance(currency, str) or not currency:
                raise ScenarioError(f"currency probes require semantic_probe.currency: {path}")
        elif "currency" in semantic_probe:
            raise ScenarioError(f"checkout probes cannot define semantic_probe.currency: {path}")
    target = raw["target_service"]
    if target is not None and (not isinstance(target, str) or not target):
        raise ScenarioError(f"target_service must be a non-empty string or null: {path}")
    if status == "ready_for_pilot" and target is None:
        raise ScenarioError(f"Runnable scenarios need a target service: {path}")
    parameters = _require_mapping(raw["parameters"], "parameters")
    if set(parameters) != PARAMETER_KEYS[fault_type]:
        raise ScenarioError(
            f"Invalid parameters for {fault_type} in {path}: expected "
            f"{', '.join(sorted(PARAMETER_KEYS[fault_type]))}"
        )
    duration = parameters.get("duration_seconds")
    if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
        raise ScenarioError(f"duration_seconds must be a positive integer: {path}")
    for name, value in parameters.items():
        if name == "delivery_requirement":
            if not isinstance(value, str) or not value:
                raise ScenarioError(f"delivery_requirement must be a non-empty string: {path}")
        elif name != "duration_seconds" and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            raise ScenarioError(f"{name} must be a positive integer: {path}")
    if parameters.get("loss_percent", 0) > 100:
        raise ScenarioError(f"loss_percent cannot exceed 100: {path}")
    for name in ("intended_cpu_percent", "intended_memory_percent"):
        if name in parameters and parameters[name] > 100:
            raise ScenarioError(f"{name} cannot exceed 100: {path}")
    if raw["severity"] not in {"low", "medium", "high"}:
        raise ScenarioError(f"Invalid severity in {path}")
    ground_truth = _require_mapping(raw["ground_truth"], "ground_truth")
    expected_truth = {
        "root_cause_service": target,
        "fault_category": category,
        "fault_type": fault_type,
    }
    if ground_truth != expected_truth:
        raise ScenarioError(f"ground_truth must exactly match scenario identity: {path}")
    if not isinstance(raw["scenario_id"], str) or not raw["scenario_id"].startswith("FI-"):
        raise ScenarioError(f"scenario_id must use the FI-* catalog identifier: {path}")
    if not isinstance(raw["workload"], dict) or not raw["workload"]:
        raise ScenarioError(f"workload must be a non-empty mapping: {path}")
    if not isinstance(raw["expected_telemetry_evidence"], dict):
        raise ScenarioError(f"expected_telemetry_evidence must be a mapping: {path}")
    if not isinstance(raw["recovery"], str) or not raw["recovery"]:
        raise ScenarioError(f"recovery must be a non-empty string: {path}")
    return FaultScenario(
        scenario_id=raw["scenario_id"],
        category=category,
        fault_type=fault_type,
        execution_status=status,
        target_service=target,
        workload=raw["workload"],
        parameters=parameters,
        severity=raw["severity"],
        expected_local_symptoms=_require_strings(
            raw["expected_local_symptoms"], "expected_local_symptoms"
        ),
        expected_propagated_symptoms=_require_strings(
            raw["expected_propagated_symptoms"], "expected_propagated_symptoms"
        ),
        expected_telemetry_evidence=raw["expected_telemetry_evidence"],
        ground_truth=ground_truth,
        expected_remediation=_require_strings(raw["expected_remediation"], "expected_remediation"),
        recovery=raw["recovery"],
        semantic_probe=semantic_probe,
        source_path=path,
    )


def load_scenarios(directory: Path) -> list[FaultScenario]:
    """Load the complete catalog and reject duplicate identifiers."""
    scenarios = [load_scenario(path) for path in sorted(directory.glob("*.yaml"))]
    if not scenarios:
        raise ScenarioError(f"No scenario YAML files found in {directory}")
    identifiers = [scenario.scenario_id for scenario in scenarios]
    if len(identifiers) != len(set(identifiers)):
        raise ScenarioError("Duplicate scenario_id in catalog")
    return scenarios
