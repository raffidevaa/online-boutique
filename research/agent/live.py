"""Integrated live incident lifecycle for AIOpsLab-compatible sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from research.generators import FaultScenario, PumbaInjector
from research.observer import Observer, ObserverError
from research.orchestrator.faults import (
    build_host_impact,
    fault_lock,
)
from research.service import ComposeApplication
from research.utils import ResearchConfig, append_event, create_run, write_json

from .orchestrator import AgentOrchestrator
from .models import AgentSession
from .actions import ComposeMitigationActions
from .runtime import (
    ApplicationRuntime,
    ComposeWorkload,
    FaultRuntime,
    TelemetryRuntime,
    WorkloadRuntime,
)


class LiveIncidentError(RuntimeError):
    """Raised when an integrated incident cannot complete safely."""


def _capture(destination: Path, capture: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for name, payload in capture.items():
        write_json(destination / f"{name}.json", payload)


def _ground_truth(scenario: FaultScenario, experiment_id: str, start: datetime, end: datetime) -> dict[str, Any]:
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


class LiveIncidentController:
    """Run one allowlisted fault while an agent interacts with live telemetry."""

    def __init__(
        self,
        config: ResearchConfig,
        orchestrator: AgentOrchestrator,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        application: ApplicationRuntime | None = None,
        observer: TelemetryRuntime | None = None,
        injector: FaultRuntime | None = None,
        workload: WorkloadRuntime | None = None,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.event_sink = event_sink
        self.application = application or ComposeApplication(config)
        self.observer = observer or Observer(config)
        self.injector = injector or PumbaInjector(config)
        self.workload = workload or ComposeWorkload(self.application)

    def _emit(self, kind: str, **details: Any) -> None:
        if self.event_sink is not None:
            self.event_sink({"kind": kind, **details})

    def _record_failed_run(
        self,
        scenario: FaultScenario,
        application: ApplicationRuntime,
        observer: TelemetryRuntime,
        injector: FaultRuntime,
        running: Any,
        run_path: Path | None,
        metadata: dict[str, Any] | None,
        injection_start: datetime | None,
        error: BaseException,
        session: AgentSession | None = None,
    ) -> None:
        """Stop an active fault and preserve failure/recovery artifacts."""
        if running is not None:
            try:
                injector.stop(running)
            except Exception as cleanup_error:
                if run_path is not None:
                    append_event(
                        run_path,
                        "live_fault_cleanup_failed",
                        {"error": str(cleanup_error)},
                    )
        if run_path is None or metadata is None:
            return

        append_event(run_path, "live_run_failed", {"error": str(error)})
        if session is not None and not (run_path / "agent-session.json").exists():
            session.termination_reason = session.termination_reason or "runtime_error"
            if session.end_time is None:
                session.end()
            write_json(run_path / "agent-session.json", session.to_dict())
        recovery_recorded = False
        if injection_start is not None:
            injection_end = running.ended_at or datetime.now(UTC)
            try:
                write_json(
                    run_path / "ground_truth.json",
                    _ground_truth(
                        scenario,
                        metadata["experiment_id"],
                        injection_start,
                        injection_end,
                    ),
                )
                _capture(
                    run_path / "incident",
                    observer.capture(injection_start, injection_end),
                )
                recovery_end = datetime.now(UTC)
                _capture(
                    run_path / "recovery",
                    observer.capture(recovery_end - timedelta(minutes=1), recovery_end),
                )
                recovery_recorded = True
            except (ObserverError, OSError, ValueError) as capture_error:
                append_event(
                    run_path,
                    "live_cleanup_capture_failed",
                    {"error": str(capture_error)},
                )
        try:
            write_json(run_path / "cleanup-state.json", application.snapshot())
        except (OSError, TypeError, ValueError):
            pass
        try:
            write_json(
                run_path / "result.json",
                {
                    "schema_version": 1,
                    "status": "failed",
                    "recovery_recorded": recovery_recorded,
                    "error": str(error),
                },
            )
        except FileExistsError:
            pass

    async def run(
        self,
        scenario: FaultScenario,
        task_name: str,
        *,
        max_steps: int = 15,
        acknowledge_execute: bool = False,
    ) -> dict[str, Any]:
        if not acknowledge_execute:
            raise LiveIncidentError("Integrated live mode requires explicit execute acknowledgement")
        if not scenario.runnable or scenario.category == "code_level":
            raise LiveIncidentError(
                "Integrated live currently supports ready_for_pilot runtime scenarios only"
            )
        if not scenario.target_service:
            raise LiveIncidentError("Live scenario has no target service")

        application = self.application
        observer = self.observer
        injector = self.injector
        running = None
        run_path: Path | None = None
        metadata: dict[str, Any] | None = None
        injection_start: datetime | None = None
        injection_end: datetime | None = None
        session: AgentSession | None = None
        with fault_lock():
            try:
                self._emit("preflight_started", scenario_id=scenario.scenario_id)
                application.validate()
                states = application.validate_readiness()
                target_state = states.get(scenario.target_service)
                if target_state is None:
                    raise LiveIncidentError(f"Target is not running: {scenario.target_service}")
                observer.check_ready()
                plan = injector.plan(scenario, target_state.container_id)
                injector.check_images_available(scenario)
                self.workload.ensure_running(scenario.workload.get("generator"))
                run_path, metadata = create_run(self.config.experiment_runs, scenario.scenario_id)
                write_json(run_path / "scenario.json", scenario.to_dict())
                write_json(run_path / "injector-plan.json", plan.to_dict())
                append_event(run_path, "live_preflight_complete", {"task": task_name})
                self._emit(
                    "preflight_complete",
                    scenario_id=scenario.scenario_id,
                    target_service=scenario.target_service,
                )
                baseline_end = datetime.now(UTC)
                baseline = observer.capture(
                    baseline_end - timedelta(minutes=1), baseline_end
                )
                _capture(run_path / "baseline", baseline)
                append_event(run_path, "baseline_captured")
                self._emit("baseline_captured")

                running = injector.start(plan)
                injection_start = running.started_at
                append_event(run_path, "live_fault_started", {"injection_start": injection_start.isoformat()})
                self._emit("fault_started", injection_start=injection_start.isoformat())
                telemetry_warmup = min(10.0, max(1.0, scenario.duration_seconds / 4))
                await asyncio.sleep(telemetry_warmup)
                append_event(
                    run_path,
                    "telemetry_warmup_complete",
                    {"seconds": telemetry_warmup},
                )
                self._emit("telemetry_warmup_complete", seconds=telemetry_warmup)
                problem_id = f"{scenario.scenario_id}-{task_name}"
                mitigation_actions = (
                    ComposeMitigationActions(
                        application, scenario.target_service, injector, running
                    )
                    if task_name == "mitigation" else None
                )
                _, task, session = self.orchestrator.init_problem(
                    problem_id,
                    start=injection_start,
                    end=None,
                    run_dir=None,
                    mitigation_actions=mitigation_actions,
                )
                await self.orchestrator.start_problem(
                    task,
                    session,
                    max_steps=max_steps,
                    evaluate=False,
                    persist=False,
                    event_sink=self.event_sink,
                )
                remediation_task = None
                remediation_session = None
                if task_name == "analysis" and session.submitted and isinstance(session.solution, dict):
                    self._emit("remediation_started")
                    _, remediation_task, remediation_session = self.orchestrator.init_problem(
                        f"{scenario.scenario_id}-remediation",
                        start=injection_start,
                        end=None,
                        run_dir=None,
                        rca_solution=session.solution,
                        rca_trace=[
                            item.to_dict()
                            for item in session.history
                            if item.role in {"assistant", "env"}
                        ],
                    )
                    await self.orchestrator.start_problem(
                        remediation_task,
                        remediation_session,
                        max_steps=max_steps,
                        evaluate=False,
                        persist=False,
                        event_sink=self.event_sink,
                    )
                    self._emit("remediation_finished", submitted=remediation_session.submitted)
                else:
                    self._emit("remediation_skipped", reason="rca_not_submitted")
                injector_result = await asyncio.to_thread(
                    injector.wait, running, scenario.duration_seconds + 60
                )
                injection_end = running.ended_at or datetime.now(UTC)
                ground_truth = _ground_truth(
                    scenario, metadata["experiment_id"], injection_start, injection_end
                )
                write_json(run_path / "ground_truth.json", ground_truth)
                session_result = self.orchestrator.finalize_session(
                    task, session, expected=ground_truth, persist=False
                )
                write_json(run_path / "agent-session.json", session_result)
                remediation_result = None
                if remediation_session is not None and remediation_task is not None:
                    remediation_result = self.orchestrator.finalize_session(
                        remediation_task,
                        remediation_session,
                        expected=ground_truth,
                        persist=False,
                    )
                    write_json(run_path / "remediation-session.json", remediation_result)
                write_json(
                    run_path / "agent-evaluation.json",
                    {
                        "schema_version": 1,
                        "task": task_name,
                        "task_results": session_result["results"],
                        "rca": session_result["results"] if task_name == "analysis" else None,
                        "remediation": remediation_result["results"] if remediation_result else None,
                        "remediation_status": (
                            "completed" if remediation_result else "skipped"
                        ),
                    },
                )
                self._emit(
                    "agent_evaluation_complete",
                    submitted=session_result.get("submitted", False),
                    termination_reason=session_result.get("termination_reason"),
                )
                write_json(
                    run_path / "injector-result.json",
                    {
                        "schema_version": 1,
                        "returncode": injector_result.returncode,
                        "stdout": injector_result.stdout,
                        "stderr": injector_result.stderr,
                        "started_at": injection_start.isoformat(),
                        "ended_at": injection_end.isoformat(),
                        "agent_stopped_fault": bool(
                            mitigation_actions and mitigation_actions.fault_stopped
                        ),
                    },
                )
                incident = observer.capture(injection_start, injection_end)
                _capture(run_path / "incident", incident)
                self._emit("incident_captured", injection_end=injection_end.isoformat())
                if scenario.fault_type == "cpu_hog":
                    write_json(
                        run_path / "host-impact.json",
                        build_host_impact(baseline, incident),
                    )
                recovery_start = datetime.now(UTC)
                append_event(
                    run_path,
                    "recovery_observation_started",
                    {"seconds": self.config.recovery_observation_seconds},
                )
                self._emit(
                    "recovery_observation_started",
                    seconds=self.config.recovery_observation_seconds,
                )
                if self.config.recovery_observation_seconds > 0:
                    await asyncio.sleep(self.config.recovery_observation_seconds)
                recovery_end = datetime.now(UTC)
                _capture(
                    run_path / "recovery",
                    observer.capture(recovery_start, recovery_end),
                )
                append_event(run_path, "recovery_observation_finished")
                self._emit("recovery_observation_finished")
                write_json(run_path / "cleanup-state.json", application.snapshot())
                status = "completed" if (
                    injector_result.returncode == 0 or
                    mitigation_actions is not None and mitigation_actions.fault_stopped
                ) else "injector_failed"
                write_json(
                    run_path / "result.json",
                    {
                        "schema_version": 1,
                        "status": status,
                        "returncode": injector_result.returncode,
                        "recovery_recorded": True,
                    },
                )
                append_event(run_path, "live_run_complete", {"status": status})
                self._emit("run_complete", status=status, experiment_id=metadata["experiment_id"])
                return {
                    "status": status,
                    "experiment_id": metadata["experiment_id"],
                    "run_path": str(run_path),
                    "session": session_result,
                    "remediation_session": remediation_result,
                }
            except Exception as error:
                self._record_failed_run(
                    scenario,
                    application,
                    observer,
                    injector,
                    running,
                    run_path,
                    metadata,
                    injection_start,
                    error,
                    session,
                )
                raise LiveIncidentError(str(error)) from error
            except (KeyboardInterrupt, asyncio.CancelledError) as error:
                self._record_failed_run(
                    scenario,
                    application,
                    observer,
                    injector,
                    running,
                    run_path,
                    metadata,
                    injection_start,
                    error,
                    session,
                )
                raise
