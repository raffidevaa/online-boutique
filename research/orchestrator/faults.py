"""Single-fault experiment lifecycle with append-only evidence artifacts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
from pathlib import Path
from typing import Any, Iterator

from research.generators import FaultScenario, InjectorError, PumbaInjector
from research.observer import Observer
from research.service import ComposeApplication, ServiceError
from research.utils import ResearchConfig, append_event, create_run, write_json


class FaultExecutionError(RuntimeError):
    """Raised when a fault cannot meet its safety preconditions."""


@contextmanager
def fault_lock(path: Path = Path("/tmp/online-boutique-fault.lock")) -> Iterator[None]:
    """Prevent multiple local processes from injecting faults concurrently."""
    with path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FaultExecutionError("Another fault execution is already active") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def describe_scenario(scenario: FaultScenario, config: ResearchConfig) -> dict[str, Any]:
    """Return a non-mutating description, including unavailable-case reasons."""
    description = scenario.to_dict()
    description["pumba_image"] = config.pumba_image
    description["pumba_stress_image"] = config.pumba_stress_image
    if scenario.execution_status == "experimental_unavailable":
        description["execution_message"] = (
            "Not runnable: a pilot must first prove an attributable target and telemetry signal."
        )
    elif scenario.execution_status == "deferred_faulty_image":
        description["execution_message"] = (
            "Not runnable: an approved faulty-image adapter is required; Pumba is not used."
        )
    else:
        description["execution_message"] = (
            "Ready for a controlled pilot only; fault run requires --execute and full preflight."
        )
        description["planned_command"] = PumbaInjector(config).plan(
            scenario, "<resolved-at-execution>"
        ).to_dict()["command"]
    return description


def _write_capture(destination: Path, capture: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name, payload in capture.items():
        write_json(destination / f"{name}.json", payload)


def _ground_truth(
    scenario: FaultScenario, experiment_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "scenario_id": scenario.scenario_id,
        "root_cause_service": scenario.ground_truth["root_cause_service"],
        "fault_category": scenario.category,
        "fault_type": scenario.fault_type,
        "target_service": scenario.target_service,
        "parameters": scenario.parameters,
        "injection_start": start.isoformat(),
        "injection_end": end.isoformat(),
        "severity": scenario.severity,
    }


def run_fault(scenario: FaultScenario, config: ResearchConfig) -> dict[str, Any]:
    """Execute one ready Pumba scenario and preserve evidence under an ignored run directory."""
    if not scenario.runnable:
        raise FaultExecutionError(describe_scenario(scenario, config)["execution_message"])
    if not scenario.target_service:
        raise FaultExecutionError("Runnable scenario is missing a target")
    application = ComposeApplication(config)
    observer = Observer(config)
    injector = PumbaInjector(config)
    with fault_lock():
        application.validate()
        catalog_target = application.catalog.get(scenario.target_service)
        if not catalog_target.targetable or not catalog_target.deployed:
            raise FaultExecutionError(f"Target is not available for faults: {scenario.target_service}")
        states = application.validate_readiness()
        target_state = states.get(scenario.target_service)
        if target_state is None:
            raise FaultExecutionError(f"Target is not running: {scenario.target_service}")
        observer.check_ready()
        plan = injector.plan(scenario, target_state.container_id)
        injector.check_images_available(scenario)
        run_path, metadata = create_run(config.experiment_runs, scenario.scenario_id)
        append_event(run_path, "preflight_complete", {"target_service": scenario.target_service})
        write_json(run_path / "scenario.json", scenario.to_dict())
        write_json(run_path / "injector-plan.json", plan.to_dict())
        baseline_end = datetime.now(UTC)
        _write_capture(
            run_path / "baseline",
            observer.capture(baseline_end - timedelta(minutes=1), baseline_end),
        )
        append_event(run_path, "baseline_captured")
        start = datetime.now(UTC)
        try:
            result = injector.execute(plan, scenario.duration_seconds + 60)
        except InjectorError as error:
            end = datetime.now(UTC)
            write_json(run_path / "ground_truth.json", _ground_truth(scenario, metadata["experiment_id"], start, end))
            append_event(run_path, "injector_error", {"message": str(error)})
            raise FaultExecutionError(str(error)) from error
        end = datetime.now(UTC)
        write_json(run_path / "ground_truth.json", _ground_truth(scenario, metadata["experiment_id"], start, end))
        write_json(
            run_path / "injector-result.json",
            {
                "schema_version": 1,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "started_at": start.isoformat(),
                "ended_at": end.isoformat(),
            },
        )
        append_event(run_path, "injector_finished", {"returncode": result.returncode})
        _write_capture(run_path / "incident", observer.capture(start, end))
        recovery_end = datetime.now(UTC)
        _write_capture(
            run_path / "recovery",
            observer.capture(recovery_end - timedelta(minutes=1), recovery_end),
        )
        write_json(run_path / "cleanup-state.json", application.snapshot())
        write_json(
            run_path / "result.json",
            {
                "schema_version": 1,
                "status": "completed" if result.returncode == 0 else "injector_failed",
                "returncode": result.returncode,
                "recovery_recorded": True,
            },
        )
        append_event(run_path, "run_complete", {"returncode": result.returncode})
        return {
            "status": "completed" if result.returncode == 0 else "injector_failed",
            "experiment_id": metadata["experiment_id"],
            "run_path": str(run_path),
        }
