"""Deterministic semantic validators for code-level fault experiments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class SemanticValidationError(ValueError):
    """Raised when a validator observation is malformed or does not prove the fault."""


@dataclass(frozen=True)
class SemanticValidationResult:
    validator: str
    status: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "validator": self.validator,
            "status": self.status,
            "evidence": self.evidence,
        }


def _mapping(observation: object) -> dict[str, Any]:
    if not isinstance(observation, dict):
        raise SemanticValidationError("semantic observation must be a JSON object")
    return observation


def _require(observation: dict[str, Any], key: str) -> Any:
    if key not in observation:
        raise SemanticValidationError(f"semantic observation is missing {key}")
    return observation[key]


def _currency_return(observation: object) -> dict[str, Any]:
    value = _mapping(observation)
    baseline = _require(value, "baseline_value")
    incident = _require(value, "incident_value")
    if baseline == incident:
        raise SemanticValidationError("currency result did not change during the fault")
    return {"baseline_value": baseline, "incident_value": incident}


def _missing_exception(observation: object) -> dict[str, Any]:
    value = _mapping(observation)
    baseline_errors = _require(value, "baseline_error_count")
    incident_errors = _require(value, "incident_error_count")
    unhandled = _require(value, "unhandled_exception")
    if not isinstance(baseline_errors, int) or not isinstance(incident_errors, int):
        raise SemanticValidationError("checkout error counts must be integers")
    if incident_errors <= baseline_errors or unhandled is not True:
        raise SemanticValidationError("unhandled checkout failure was not demonstrated")
    return {
        "baseline_error_count": baseline_errors,
        "incident_error_count": incident_errors,
        "unhandled_exception": unhandled,
    }


def _incorrect_parameter(observation: object) -> dict[str, Any]:
    value = _mapping(observation)
    status = _require(value, "downstream_status")
    parameter = _require(value, "observed_parameter")
    if status == "OK" or parameter == value.get("expected_parameter"):
        raise SemanticValidationError("incorrect parameter behavior was not demonstrated")
    return {"downstream_status": status, "observed_parameter": parameter}


def _missing_parameter(observation: object) -> dict[str, Any]:
    value = _mapping(observation)
    status = _require(value, "downstream_status")
    missing = _require(value, "missing_parameter")
    if status == "OK" or not isinstance(missing, str) or not missing:
        raise SemanticValidationError("missing parameter behavior was not demonstrated")
    return {"downstream_status": status, "missing_parameter": missing}


def _missing_function_call(observation: object) -> dict[str, Any]:
    value = _mapping(observation)
    succeeded = _require(value, "checkout_succeeded")
    remaining = _require(value, "cart_items_after_checkout")
    if succeeded is not True or not isinstance(remaining, int) or remaining <= 0:
        raise SemanticValidationError("missing function call behavior was not demonstrated")
    return {"checkout_succeeded": succeeded, "cart_items_after_checkout": remaining}


VALIDATORS: dict[str, Callable[[object], dict[str, Any]]] = {
    "currency_return": _currency_return,
    "missing_exception": _missing_exception,
    "incorrect_parameter": _incorrect_parameter,
    "missing_parameter": _missing_parameter,
    "missing_function_call": _missing_function_call,
}


def validate_semantic(validator: str, observation: object) -> SemanticValidationResult:
    """Validate a normalized observation emitted by a workload harness."""
    try:
        function = VALIDATORS[validator]
    except KeyError as error:
        raise SemanticValidationError(f"Unknown semantic validator: {validator}") from error
    evidence = function(observation)
    return SemanticValidationResult(validator, "passed", evidence)
