"""Single-fault experiment lifecycle with append-only evidence artifacts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import fcntl
import json
from pathlib import Path
import time
from typing import Any, Iterator

from research.generators import (
    FaultScenario,
    FaultyImageInjector,
    InjectorError,
    PumbaInjector,
    SemanticValidationError,
    validate_semantic,
)
from research.observer import Observer, ObserverError
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
    if scenario.category == "code_level":
        faulty_plan = FaultyImageInjector(config).plan(scenario)
        description["faulty_image"] = faulty_plan.to_dict(config.research_root.parent)
        description["execution_message"] = (
            "Requires an explicitly built and prepared faulty image; Compose override "
            "execution remains gated by --execute."
        )
        return description
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


def run_fault(
    scenario: FaultScenario,
    config: ResearchConfig,
    semantic_observation_path: Path | None = None,
) -> dict[str, Any]:
    """Execute one ready Pumba scenario and preserve evidence under an ignored run directory."""
    if scenario.category == "code_level":
        return run_faulty_image(scenario, config, semantic_observation_path)
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
        try:
            baseline = observer.capture(
                baseline_end - timedelta(minutes=1), baseline_end
            )
            _write_capture(run_path / "baseline", baseline)
        except ObserverError as error:
            append_event(run_path, "baseline_capture_failed", {"message": str(error)})
            write_json(
                run_path / "result.json",
                {
                    "schema_version": 1,
                    "status": "baseline_failed",
                    "fault_injected": False,
                    "telemetry_error": str(error),
                },
            )
            raise FaultExecutionError(
                f"Baseline telemetry capture failed; fault was not injected: {error}"
            ) from error
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


def run_faulty_image(
    scenario: FaultScenario,
    config: ResearchConfig,
    semantic_observation_path: Path | None = None,
) -> dict[str, Any]:
    """Run one code-level scenario and always attempt baseline restoration."""
    if scenario.category != "code_level":
        raise FaultExecutionError("Faulty-image execution requires a code-level scenario")
    if not scenario.target_service:
        raise FaultExecutionError("Code-level scenario is missing a target")

    application = ComposeApplication(config)
    observer = Observer(config)
    injector = FaultyImageInjector(config)
    plan = injector.plan(scenario)
    with fault_lock():
        application.validate()
        catalog_target = application.catalog.get(scenario.target_service)
        if not catalog_target.targetable or not catalog_target.deployed:
            raise FaultExecutionError(f"Target is not available for faults: {scenario.target_service}")
        application.validate_readiness()
        observer.check_ready()
        injector.check_image_available(scenario)
        application.validate_fault_override(
            plan.definition.override,
            scenario.target_service,
            plan.definition.image,
            plan.definition.trigger,
        )

        run_path, metadata = create_run(config.experiment_runs, scenario.scenario_id)
        write_json(run_path / "scenario.json", scenario.to_dict())
        write_json(run_path / "faulty-image-plan.json", plan.to_dict(config.research_root.parent))
        append_event(
            run_path,
            "preflight_complete",
            {"target_service": scenario.target_service, "delivery": "faulty_image"},
        )

        baseline_end = datetime.now(UTC)
        try:
            _write_capture(
                run_path / "baseline",
                observer.capture(baseline_end - timedelta(minutes=1), baseline_end),
            )
        except ObserverError as error:
            append_event(run_path, "baseline_capture_failed", {"message": str(error)})
            write_json(
                run_path / "result.json",
                {
                    "schema_version": 1,
                    "status": "baseline_failed",
                    "fault_injected": False,
                    "telemetry_error": str(error),
                },
            )
            raise FaultExecutionError(
                f"Baseline telemetry capture failed; faulty image was not applied: {error}"
            ) from error

        append_event(run_path, "baseline_captured")
        applied = False
        start: datetime | None = None
        end: datetime | None = None
        apply_result: dict[str, object] = {}
        restore_error: Exception | None = None
        semantic_result: dict[str, object]
        try:
            applied_at = datetime.now(UTC)
            result = application.apply_fault_override(
                plan.definition.override, scenario.target_service
            )
            applied = True
            faulty_state = application.wait_for_service_healthy(scenario.target_service)
            start = datetime.now(UTC)
            append_event(
                run_path,
                "faulty_image_applied",
                {
                    "applied_at": applied_at.isoformat(),
                    "returncode": result.returncode,
                    "container_id": faulty_state.container_id,
                    "health": faulty_state.health,
                },
            )
            time.sleep(scenario.duration_seconds)
            end = datetime.now(UTC)
            write_json(run_path / "ground_truth.json", _ground_truth(
                scenario, metadata["experiment_id"], start, end
            ))
            _write_capture(run_path / "incident", observer.capture(start, end))

            if semantic_observation_path is None:
                semantic_result = {
                    "schema_version": 1,
                    "status": "not_collected",
                    "validator": plan.definition.validator,
                    "message": "Provide --semantic-observation to score business behavior.",
                }
            else:
                try:
                    observation = json.loads(
                        semantic_observation_path.read_text(encoding="utf-8")
                    )
                    semantic_result = validate_semantic(
                        plan.definition.validator, observation
                    ).to_dict()
                except (OSError, json.JSONDecodeError, SemanticValidationError) as error:
                    semantic_result = {
                        "schema_version": 1,
                        "status": "failed",
                        "validator": plan.definition.validator,
                        "message": str(error),
                    }
            write_json(run_path / "semantic-validation.json", semantic_result)
            append_event(run_path, "semantic_validation", {"status": semantic_result["status"]})
            apply_result = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        except (OSError, json.JSONDecodeError, ObserverError, SemanticValidationError) as error:
            append_event(run_path, "fault_execution_error", {"message": str(error)})
            if start is not None and end is None:
                end = datetime.now(UTC)
                write_json(run_path / "ground_truth.json", _ground_truth(
                    scenario, metadata["experiment_id"], start, end
                ))
            raise FaultExecutionError(str(error)) from error
        finally:
            if applied:
                try:
                    restored = application.restore_service(scenario.target_service)
                    restored_state = application.wait_for_service_healthy(
                        scenario.target_service
                    )
                    append_event(
                        run_path,
                        "baseline_restore_finished",
                        {
                            "returncode": restored.returncode,
                            "container_id": restored_state.container_id,
                            "health": restored_state.health,
                            "image": restored_state.image,
                        },
                    )
                except ServiceError as error:
                    restore_error = error
                    append_event(run_path, "baseline_restore_failed", {"message": str(error)})

        if restore_error is not None:
            write_json(
                run_path / "result.json",
                {"schema_version": 1, "status": "recovery_failed", "fault_injected": applied},
            )
            raise FaultExecutionError(str(restore_error)) from restore_error

        recovery_end = datetime.now(UTC)
        _write_capture(
            run_path / "recovery",
            observer.capture(recovery_end - timedelta(minutes=1), recovery_end),
        )
        write_json(run_path / "cleanup-state.json", application.snapshot())
        if semantic_result["status"] == "passed":
            final_status = "completed"
        elif semantic_result["status"] == "not_collected":
            final_status = "completed_unvalidated"
        else:
            final_status = "semantic_validation_failed"
        write_json(
            run_path / "injector-result.json",
            {"schema_version": 1, "delivery": "faulty_image", "apply": apply_result},
        )
        write_json(
            run_path / "result.json",
            {
                "schema_version": 1,
                "status": final_status,
                "fault_injected": applied,
                "recovery_recorded": True,
                "semantic_validation": semantic_result["status"],
            },
        )
        append_event(run_path, "run_complete", {"status": final_status})
        return {
            "status": final_status,
            "experiment_id": metadata["experiment_id"],
            "run_path": str(run_path),
        }
