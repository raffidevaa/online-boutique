"""Integrated live incident lifecycle for AIOpsLab-compatible sessions."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from research.generators import (FaultScenario, PumbaInjector, FaultyImageInjector,
    run_semantic_probe, build_semantic_observation, validate_semantic)
from research.observer import Observer, ObserverError
from research.orchestrator.faults import (
    build_host_impact,
    fault_lock,
)
from research.service import ComposeApplication
from research.utils import ResearchConfig, append_event, create_run, write_json

from .orchestrator import AgentOrchestrator
from .models import AgentSession
from .strategy import IncidentBudget
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


class ImageFaultRuntime:
    """Adapt an approved faulty image to the live fault lifecycle."""

    restores_on_stop = True

    def __init__(self, config: ResearchConfig, application: Any, observer: Any) -> None:
        self.config = config
        self.application = application
        self.observer = observer
        self.images = FaultyImageInjector(config)
        self.scenario: FaultScenario | None = None
        self.run_path: Path | None = None
        self.baseline_probe: Any = None
        self.restored = False

    def plan(self, scenario: FaultScenario, container_id: str) -> Any:
        self.scenario = scenario
        return self.images.plan(scenario)

    def check_images_available(self, scenario: FaultScenario) -> None:
        self.images.check_image_available(scenario)
        plan = self.images.plan(scenario)
        self.application.validate_fault_override(
            plan.definition.override, scenario.target_service,
            plan.definition.image, plan.definition.trigger,
        )

    def start(self, plan: Any) -> Any:
        from types import SimpleNamespace

        assert self.scenario is not None and self.run_path is not None
        self.baseline_probe = run_semantic_probe(self.scenario, self.config, "baseline")
        applied = False
        try:
            self.application.apply_fault_override(
                plan.definition.override, self.scenario.target_service
            )
            applied = True
            if self.scenario.fault_type not in {
                "missing_exception_handler", "incorrect_parameter", "missing_parameter"
            }:
                self.application.wait_for_service_healthy(self.scenario.target_service)
            started = datetime.now(UTC)
            time.sleep(max(0.0, self.config.semantic_probe_stabilization_seconds))
            incident_probe = run_semantic_probe(self.scenario, self.config, "incident")
            if self.scenario.fault_type in {"incorrect_parameter", "missing_parameter"}:
                time.sleep(10)
            captured = self.observer.capture(started, datetime.now(UTC))
            observation = build_semantic_observation(
                self.scenario, self.baseline_probe, incident_probe, captured
            )
            write_json(self.run_path / "semantic-observation.json", observation)
            write_json(self.run_path / "semantic-probe.json", {
                "schema_version": 2, "status": "completed", **observation,
            })
            validation = validate_semantic(plan.definition.validator, observation).to_dict()
            write_json(self.run_path / "semantic-validation.json", validation)
            return SimpleNamespace(started_at=started, ended_at=None, plan=plan)
        except Exception:
            if applied:
                self.application.restore_service(self.scenario.target_service)
                self.restored = True
            raise

    def stop(self, running: Any) -> None:
        if not self.restored:
            assert self.scenario is not None
            self.application.restore_service(self.scenario.target_service)
            self.application.wait_for_service_healthy(self.scenario.target_service)
            self.restored = True
        running.ended_at = datetime.now(UTC)

    def wait(self, running: Any, timeout: float) -> subprocess.CompletedProcess[str]:
        self.stop(running)
        return subprocess.CompletedProcess(["faulty_image"], 0, "", "")


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


def _prior_stage_context(
    records: dict[str, tuple[Any, AgentSession]], max_age_seconds: float = 15.0
) -> str | None:
    """Carry agent-owned findings and evidence references, never evaluator labels."""
    if not records:
        return None
    stages = []
    for name, (_, session) in records.items():
        evidence_times = []
        for note in session.notes:
            if note.get("status") == "evidence_collected" and note.get("updated_at"):
                try:
                    evidence_times.append(datetime.fromisoformat(note["updated_at"]))
                except (TypeError, ValueError):
                    pass
        reference_time = min(evidence_times) if evidence_times else session.end_time
        age = (
            (datetime.now(UTC) - reference_time).total_seconds()
            if reference_time is not None else None
        )
        stages.append({
            "stage": name,
            "submitted": session.submitted,
            "solution": session.solution if session.submitted else None,
            "notes": session.notes[-5:],
            "evidence_ids": session.metrics.get("evidence_ids", []),
            "evidence_age_seconds": round(age, 3) if age is not None else None,
            "evidence_stale": age is None or age > max_age_seconds,
        })
    return json.dumps(stages, ensure_ascii=False, separators=(",", ":"))


def _rca_handoff_trace(session: AgentSession) -> list[dict[str, str]]:
    """Give recommendation a concise agent-owned evidence ledger."""
    return [{"role": "env", "content": json.dumps({
        "notes": session.notes[-5:],
        "evidence_ids": session.metrics.get("evidence_ids", []),
    }, separators=(",", ":"))}]


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
                if not (run_path / "ground_truth.json").exists():
                    write_json(
                        run_path / "ground_truth.json",
                        _ground_truth(
                            scenario, metadata["experiment_id"], injection_start, injection_end
                        ),
                    )
                if not (run_path / "incident").exists():
                    _capture(
                        run_path / "incident",
                        observer.capture(injection_start, injection_end),
                    )
                recovery_end = datetime.now(UTC)
                if not (run_path / "recovery").exists():
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
        if not scenario.runnable and scenario.category != "code_level":
            raise LiveIncidentError(
                "Integrated live currently supports ready_for_pilot runtime scenarios only"
            )
        if not scenario.target_service:
            raise LiveIncidentError("Live scenario has no target service")

        application = self.application
        observer = self.observer
        injector = self.injector
        if scenario.category == "code_level":
            injector = ImageFaultRuntime(self.config, application, observer)
        running = None
        run_path: Path | None = None
        metadata: dict[str, Any] | None = None
        injection_start: datetime | None = None
        injection_end: datetime | None = None
        session: AgentSession | None = None
        self.orchestrator.incident_budget = IncidentBudget()
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
                write_json(run_path / "injector-plan.json", (
                    plan.to_dict(self.config.research_root.parent)
                    if scenario.category == "code_level" else plan.to_dict()
                ))
                if scenario.category == "code_level":
                    injector.run_path = run_path
                    write_json(run_path / "faulty-image-plan.json", plan.to_dict(
                        self.config.research_root.parent
                    ))
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
                stage_records: dict[str, tuple[Any, AgentSession]] = {}
                mitigation_actions = None
                stages = (
                    ("detection", "localization", "analysis", "remediation", "mitigation")
                    if task_name == "full" else (task_name,)
                )
                rca_solution = None
                rca_trace = None
                for stage in stages:
                    if stage == "remediation" and not isinstance(rca_solution, dict):
                        self._emit("stage_skipped", stage=stage, reason="rca_not_submitted")
                        continue
                    if stage == "mitigation":
                        mitigation_actions = ComposeMitigationActions(
                            application, scenario.target_service, injector, running
                        )
                    self._emit("stage_started", stage=stage)
                    prior_context = (
                        _prior_stage_context(stage_records, self.config.aiops_live_cache_ttl_seconds)
                        if task_name == "full" else None
                    )
                    _, current_task, current_session = self.orchestrator.init_problem(
                        f"{scenario.scenario_id}-{stage}",
                        start=injection_start,
                        end=None,
                        run_dir=None,
                        mitigation_actions=mitigation_actions if stage == "mitigation" else None,
                        rca_solution=rca_solution if stage == "remediation" else None,
                        rca_trace=rca_trace if stage == "remediation" else None,
                        prior_context=prior_context,
                    )
                    session = current_session
                    await self.orchestrator.start_problem(
                        current_task,
                        current_session,
                        max_steps=max_steps,
                        evaluate=False,
                        persist=False,
                        initial_observation=(
                            f"Step 1 of {max_steps}. Use the previous stage findings. "
                            + (
                                "If restoration is justified, call restore_service now; verify afterward."
                                if stage == "mitigation" else
                                "Submit if they already support this task; request only missing evidence."
                            )
                            if prior_context else None
                        ),
                        event_sink=(
                            lambda event, active_stage=stage: self.event_sink(
                                {**event, "stage": active_stage}
                            )
                            if self.event_sink is not None else None
                        ),
                    )
                    stage_records[stage] = (current_task, current_session)
                    self._emit("stage_finished", stage=stage, submitted=current_session.submitted)
                    if stage == "analysis" and current_session.submitted:
                        rca_solution = current_session.solution
                        rca_trace = _rca_handoff_trace(current_session)
                task, session = stage_records["analysis" if task_name == "full" else task_name]
                remediation_task, remediation_session = stage_records.get("remediation", (None, None))
                if task_name == "analysis" and session.submitted and isinstance(session.solution, dict):
                    self._emit("remediation_started")
                    _, remediation_task, remediation_session = self.orchestrator.init_problem(
                        f"{scenario.scenario_id}-remediation",
                        start=injection_start, end=None, run_dir=None,
                        rca_solution=session.solution,
                        rca_trace=_rca_handoff_trace(session),
                    )
                    await self.orchestrator.start_problem(
                        remediation_task, remediation_session, max_steps=max_steps,
                        evaluate=False, persist=False, event_sink=self.event_sink,
                    )
                    stage_records["remediation"] = (remediation_task, remediation_session)
                    self._emit("remediation_finished", submitted=remediation_session.submitted)
                injector_result = await asyncio.to_thread(
                    injector.wait, running, scenario.duration_seconds + 60
                )
                injection_end = running.ended_at or datetime.now(UTC)
                ground_truth = _ground_truth(
                    scenario, metadata["experiment_id"], injection_start, injection_end
                )
                write_json(run_path / "ground_truth.json", ground_truth)
                stage_results: dict[str, dict[str, Any]] = {}
                for stage, (stage_task, stage_session) in stage_records.items():
                    evaluated = self.orchestrator.finalize_session(
                        stage_task, stage_session, expected=ground_truth, persist=False
                    )
                    stage_results[stage] = evaluated
                    filename = "agent-session.json" if stage == "analysis" else f"{stage}-session.json"
                    write_json(run_path / filename, evaluated)
                session_result = stage_results["analysis" if task_name == "full" else task_name]
                remediation_result = stage_results.get("remediation")
                write_json(
                    run_path / "agent-evaluation.json",
                    {
                        "schema_version": 2,
                        "task": task_name,
                        "task_results": session_result["results"],
                        "stages": {stage: result["results"] for stage, result in stage_results.items()},
                        "strategy_metrics": {
                            stage: result.get("metrics", {}) for stage, result in stage_results.items()
                        },
                        "rca": stage_results.get("analysis", {}).get("results"),
                        "remediation": remediation_result["results"] if remediation_result else None,
                        "remediation_status": "completed" if remediation_result else "skipped",
                    },
                )
                self._emit("agent_evaluation_complete", stages=list(stage_results))
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
                    "stages": stage_results,
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
