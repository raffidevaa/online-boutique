"""Bounded, auditable action control for one RCA investigation."""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any

from .tasks import TaskError


READ_ACTIONS = frozenset({
    "get_incident_summary", "get_error_logs", "get_metrics", "get_logs",
    "get_traces", "get_alerts", "get_evidence_detail",
})


def estimate_tokens(text: str) -> int:
    """Conservative approximation; never recorded as provider-reported usage."""
    return (len(text) + 3) // 4


def validate_action(task: Any, name: str, args: list[Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    if name not in task.actions:
        raise TaskError(f"Invalid action {name!r}; valid actions: {', '.join(task.actions)}")
    signature = inspect.signature(task.actions[name])
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError as error:
        raise TaskError(f"Invalid arguments for {name}{signature}: {error}") from error
    bound.apply_defaults()
    normalized = dict(bound.arguments)
    if name == "submit" and task.task_name in {"localization", "analysis"}:
        answer = normalized.get("answer")
        components = normalized.get("faulty_components")
        if components is None:
            components = answer.get("faulty_components") if isinstance(answer, dict) else answer
        if not isinstance(components, list) or not components or not all(isinstance(item, str) for item in components):
            raise TaskError("submit requires one or more faulty_components service names")
        valid = task.evidence.get_targetable_services()
        if any(item not in valid for item in components):
            raise TaskError("faulty_components must use services from: " + ", ".join(sorted(valid)))
    if name in READ_ACTIONS:
        service = normalized.get("service")
        if service is not None:
            if not isinstance(service, str) or service not in task.evidence.get_targetable_services():
                raise TaskError("service must be one of: " + ", ".join(sorted(task.evidence.get_targetable_services())))
        window = normalized.get("window", "incident")
        if not isinstance(window, str) or window not in {"baseline", "incident", "recovery"}:
            raise TaskError("window must be baseline or incident (recovery only after recovery)")
        if window == "recovery" and task.evidence.run_dir is None:
            raise TaskError("recovery window is unavailable during a live incident")
        if name == "get_evidence_detail":
            evidence_id = normalized.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id not in task.evidence.evidence_store:
                raise TaskError("evidence_id must identify an observation in this session")
            limit = normalized.get("limit", 5)
            if type(limit) is not int or not 1 <= limit <= 20:
                raise TaskError("limit must be an integer from 1 to 20")
    return normalized


def action_fingerprint(name: str, arguments: dict[str, Any], run_identity: str) -> str:
    canonical = json.dumps(
        {"action": name, "arguments": arguments, "run": run_identity},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class IncidentBudget:
    steps_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True


@dataclass
class QueryCache:
    entries: dict[str, tuple[float, Any]] = field(default_factory=dict)

    def get(self, fingerprint: str, *, replay: bool, ttl_seconds: float) -> Any | None:
        entry = self.entries.get(fingerprint)
        if entry is None:
            return None
        timestamp, result = entry
        if replay or time.monotonic() - timestamp <= ttl_seconds:
            return result
        return None

    def put(self, fingerprint: str, result: Any) -> None:
        self.entries[fingerprint] = (time.monotonic(), result)
