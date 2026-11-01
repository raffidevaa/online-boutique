"""Runtime contracts used by a live AIOps problem."""

from __future__ import annotations

from typing import Any, Protocol


class ApplicationRuntime(Protocol):
    def validate(self) -> None: ...
    def validate_readiness(self) -> dict[str, Any]: ...
    def get_service_state(self, service: str) -> Any: ...
    def snapshot(self) -> dict[str, Any]: ...


class FaultRuntime(Protocol):
    def plan(self, scenario: Any, container_id: str) -> Any: ...
    def check_images_available(self, scenario: Any) -> None: ...
    def start(self, plan: Any) -> Any: ...
    def wait(self, running: Any, timeout: float) -> Any: ...
    def stop(self, running: Any) -> Any: ...


class TelemetryRuntime(Protocol):
    def check_ready(self) -> dict[str, int]: ...
    def capture(self, start: Any, end: Any) -> dict[str, Any]: ...


class WorkloadRuntime(Protocol):
    def ensure_running(self, generator: str | None) -> None: ...


class ComposeWorkload:
    """Enforce the declared, low-load Compose workload before fault injection."""

    def __init__(self, application: ApplicationRuntime) -> None:
        self.application = application

    def ensure_running(self, generator: str | None) -> None:
        if generator is None:
            return
        if generator != "loadgenerator":
            raise ValueError(f"Unsupported workload generator: {generator}")
        state = self.application.get_service_state("loadgenerator")
        if state is None or state.status != "running":
            raise ValueError("loadgenerator preparation did not leave the workload running")
