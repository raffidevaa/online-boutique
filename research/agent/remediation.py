"""Compose-safe remediation recommendation contracts and validation."""

from __future__ import annotations

from typing import Any


REMEDIATION_ACTION_TYPES = (
    "restart_service",
    "rollback_configuration",
    "adjust_resource_limit",
    "network_configuration_change",
    "deploy_corrected_image",
    "manual_investigation",
    "escalate",
)
REMEDIATION_RISKS = ("low", "medium", "high")
REMEDIATION_REQUIRED_FIELDS = (
    "action_type",
    "target_service",
    "rationale",
    "evidence",
    "expected_effect",
    "risk",
    "rollback_plan",
    "requires_approval",
    "execution_allowed",
)


def validate_remediation(
    answer: Any,
    targetable_services: set[str],
) -> dict[str, Any]:
    """Validate and return a recommendation without enabling execution.

    This contract deliberately has no command, container, or arbitrary-parameter field.
    It describes an action for human review; it cannot be converted into an executable
    operation by the agent runtime.
    """
    if not isinstance(answer, dict):
        raise ValueError("Remediation recommendation must be an object")
    missing = [field for field in REMEDIATION_REQUIRED_FIELDS if field not in answer]
    if missing:
        raise ValueError(f"Remediation recommendation requires: {', '.join(missing)}")

    action_type = answer["action_type"]
    if action_type not in REMEDIATION_ACTION_TYPES:
        raise ValueError("action_type must use the remediation taxonomy")
    target_service = answer["target_service"]
    if not isinstance(target_service, str) or not target_service.strip():
        raise ValueError("target_service must be a non-empty service name")
    if target_service not in targetable_services and not (
        target_service == "unknown" and action_type in {"manual_investigation", "escalate"}
    ):
        raise ValueError("target_service must identify a targetable service or unknown")

    for field in ("rationale", "expected_effect", "rollback_plan"):
        value = answer[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        if len(value) > 2_000:
            raise ValueError(f"{field} is too long")
    evidence = answer["evidence"]
    if (
        not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 5
        or not all(isinstance(item, str) and item.strip() for item in evidence)
    ):
        raise ValueError("evidence must contain one to five non-empty strings")
    if answer["risk"] not in REMEDIATION_RISKS:
        raise ValueError("risk must be low, medium, or high")
    if answer["requires_approval"] is not True:
        raise ValueError("requires_approval must be true")
    if answer["execution_allowed"] is not False:
        raise ValueError("execution_allowed must be false for recommendation-only mode")

    return {
        "action_type": action_type,
        "target_service": target_service,
        "rationale": answer["rationale"].strip(),
        "evidence": [item.strip() for item in evidence],
        "expected_effect": answer["expected_effect"].strip(),
        "risk": answer["risk"],
        "rollback_plan": answer["rollback_plan"].strip(),
        "requires_approval": True,
        "execution_allowed": False,
    }
