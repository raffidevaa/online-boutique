"""Allowlisted Compose actions for an active mitigation problem."""

from __future__ import annotations

from typing import Any, Protocol


class MitigationActionError(ValueError):
    """The requested operation is outside the active problem's allowlist."""


class ServiceActionRuntime(Protocol):
    def get_service_state(self, service: str) -> Any: ...
    def restore_service(self, service: str) -> Any: ...


class ActiveFaultRuntime(Protocol):
    def stop(self, running: Any) -> Any: ...


class ComposeMitigationActions:
    """Execute only service restoration for the faulted service."""

    def __init__(
        self,
        runtime: ServiceActionRuntime,
        target_service: str,
        fault_runtime: ActiveFaultRuntime | None = None,
        running_fault: Any = None,
    ) -> None:
        if not target_service:
            raise MitigationActionError("A target service is required")
        self.runtime = runtime
        self.target_service = target_service
        self.fault_runtime = fault_runtime
        self.running_fault = running_fault
        self.fault_stopped = False
        self.history: list[dict[str, str]] = []

    def restore_service(self, service: str) -> str:
        if service != self.target_service:
            raise MitigationActionError("Only the active problem target may be restored")
        before = self.runtime.get_service_state(service)
        if before is None:
            raise MitigationActionError("Target service is unavailable")
        if self.fault_runtime is not None and self.running_fault is not None:
            self.fault_runtime.stop(self.running_fault)
            self.fault_stopped = True
        self.runtime.restore_service(service)
        after = self.runtime.get_service_state(service)
        status = getattr(after, "status", "unknown")
        self.history.append({"action": "restore_service", "service": service, "status": status})
        return f"Restored {service}; status={status}"
