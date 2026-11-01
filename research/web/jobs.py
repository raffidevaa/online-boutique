"""Single-flight experiment jobs and sanitized event streams for the web UI."""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
import uuid

from research.agent.orchestrator import create_orchestrator
from research.agent.sanitize import sanitize_text
from research.generators import FaultScenario, FaultyImageInjector, ScenarioError, load_scenario
from research.observer import Observer
from research.orchestrator.dataset import write_dataset_index
from research.orchestrator.faults import run_faulty_image
from research.generators import PumbaInjector
from research.service import ComposeApplication
from research.utils import ResearchConfig


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"prepared", "queued", "running"}


class GuiJobError(RuntimeError):
    """Raised when a GUI job cannot be safely prepared or started."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_safe(value: Any, *, hide_labels: bool = True) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if hide_labels and str(key).lower() in {
                "ground_truth", "expected", "truth", "evaluator"
            }:
                continue
            output[str(key)] = _json_safe(item, hide_labels=hide_labels)
        return output
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, hide_labels=hide_labels) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(str(value))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


@dataclass
class GuiJob:
    job_id: str
    scenario_id: str
    task: str
    max_steps: int
    confirmation_nonce: str
    confirmation_expires_at: datetime
    workload_started_by_job: bool = False
    status: str = "prepared"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    experiment_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock()),
        repr=False,
    )

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "scenario_id": self.scenario_id,
            "task": self.task,
            "max_steps": self.max_steps,
            "workload_started_by_job": self.workload_started_by_job,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "experiment_id": self.experiment_id,
            "result": _json_safe(self.result) if self.result else None,
            "error": sanitize_text(self.error) if self.error else None,
            "event_count": len(self.events),
        }


class JobManager:
    """Run one controlled live experiment at a time and expose audit events."""

    def __init__(self, config: ResearchConfig) -> None:
        self.config = config
        self.jobs_root = config.evaluations_dir / "gui-jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, GuiJob] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rca-gui")
        self.futures: dict[str, Future[Any]] = {}

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _scenario(self, scenario_id: str) -> FaultScenario:
        if not scenario_id or "/" in scenario_id or "\\" in scenario_id:
            raise GuiJobError("scenario_id is invalid")
        for path in sorted(self.config.scenario_directory.glob("*.yaml")):
            scenario = load_scenario(path)
            if scenario.scenario_id == scenario_id:
                return scenario
        raise GuiJobError(f"Unknown scenario: {scenario_id}")

    def scenarios(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.config.scenario_directory.glob("*.yaml")):
            try:
                scenario = load_scenario(path)
            except ScenarioError as error:
                records.append({"scenario_id": path.stem, "error": str(error), "runnable_for_gui": False})
                continue
            record = scenario.to_dict()
            record.pop("ground_truth", None)
            record["source_path"] = str(path.relative_to(self.config.research_root))
            record["runnable_for_gui"] = scenario.runnable or scenario.category == "code_level"
            record["gui_mode"] = "semantic_fault" if scenario.category == "code_level" else "full"
            if not record["runnable_for_gui"]:
                record["gui_reason"] = (
                    "Only ready_for_pilot runtime scenarios can be executed from the GUI."
                )
            records.append(record)
        return records

    def _active_job(self) -> GuiJob | None:
        with self.lock:
            return next(
                (job for job in self.jobs.values() if job.status in ACTIVE_STATUSES),
                None,
            )

    def _preflight(self, scenario: FaultScenario) -> dict[str, Any]:
        if not scenario.runnable and scenario.category != "code_level":
            raise GuiJobError(
                "GUI execution supports ready_for_pilot runtime scenarios only"
            )
        if not scenario.target_service:
            raise GuiJobError("Scenario has no target service")
        application = ComposeApplication(self.config)
        observer = Observer(self.config)
        injector = PumbaInjector(self.config)
        workload_started = False
        try:
            application.validate()
            states = application.validate_readiness()
            target_state = states.get(scenario.target_service)
            if target_state is None:
                raise GuiJobError(f"Target service is not ready: {scenario.target_service}")
            observer.check_ready()
            if scenario.category == "code_level":
                plan = FaultyImageInjector(self.config).prepare(scenario)
            else:
                plan = injector.plan(scenario, target_state.container_id).to_dict()
                injector.check_images_available(scenario)
            workload_generator = scenario.workload.get("generator")
            if workload_generator == "loadgenerator":
                workload_state = application.get_service_state("loadgenerator")
                if workload_state is None or workload_state.status != "running":
                    application.start_workload()
                    application.wait_for_service_healthy("loadgenerator")
                    workload_started = True
        except GuiJobError:
            raise
        except Exception as error:
            raise GuiJobError(f"Preflight failed: {error}") from error
        return {
            "scenario": scenario.to_dict(),
            "scenario_id": scenario.scenario_id,
            "target_service": scenario.target_service,
            "plan": plan,
            "warnings": ([
                "Fault injection is bounded to one scenario and one target.",
                "The baseline image is restored after semantic validation.",
            ] if scenario.category == "code_level" else [
                "Fault injection is bounded to one scenario and one target.",
                "The required loadgenerator is started during preparation when it is not already running.",
                "Ground truth is withheld until the full agent pipeline has ended.",
            ]),
            "workload_started": workload_started,
        }

    def prepare(self, scenario_id: str, task: str, max_steps: int) -> dict[str, Any]:
        scenario = self._scenario(scenario_id)
        expected_task = "semantic_fault" if scenario.category == "code_level" else "full"
        if task != expected_task:
            raise GuiJobError(f"This scenario requires task={expected_task}")
        if max_steps < 1 or max_steps > 50:
            raise GuiJobError("max_steps must be between 1 and 50")
        with self.lock:
            active = self._active_job()
            if active is not None:
                raise GuiJobError(f"Another GUI job is active: {active.job_id}")
        plan = self._preflight(scenario)
        job_id = f"GUI-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        job = GuiJob(
            job_id=job_id,
            scenario_id=scenario_id,
            task=task,
            max_steps=max_steps,
            confirmation_nonce=secrets.token_urlsafe(24),
            confirmation_expires_at=datetime.now(UTC) + timedelta(seconds=90),
            workload_started_by_job=bool(plan.get("workload_started", False)),
        )
        with self.lock:
            self.jobs[job_id] = job
        self.emit(job_id, "prepared", plan=plan)
        return {
            "job": job.public(),
            "plan": _json_safe(plan),
            "confirmation_nonce": job.confirmation_nonce,
            "confirmation_expires_at": job.confirmation_expires_at.isoformat(),
        }

    def start(self, job_id: str, confirmation_nonce: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise GuiJobError("Unknown job")
            if job.status != "prepared":
                raise GuiJobError(f"Job is not prepared: {job.status}")
            if datetime.now(UTC) >= job.confirmation_expires_at:
                self._stop_prepared_workload(job)
                job.status = "failed"
                job.error = "Confirmation nonce expired"
                raise GuiJobError(job.error)
            if not secrets.compare_digest(job.confirmation_nonce, confirmation_nonce):
                raise GuiJobError("Invalid confirmation nonce")
            job.confirmation_nonce = ""
            job.status = "queued"
            job.updated_at = _now()
            self.futures[job_id] = self.executor.submit(self._run, job_id)
        self.emit(job_id, "queued")
        return job.public()

    def _stop_prepared_workload(self, job: GuiJob) -> None:
        if not job.workload_started_by_job:
            return
        ComposeApplication(self.config).stop_workload()
        job.workload_started_by_job = False

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise GuiJobError("Unknown job")
            if job.status != "prepared":
                raise GuiJobError(f"Job cannot be cancelled from status: {job.status}")
            self._stop_prepared_workload(job)
            job.confirmation_nonce = ""
            job.status = "cancelled"
            job.updated_at = _now()
        self.emit(job_id, "cancelled")
        return job.public()

    def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]
        try:
            scenario = self._scenario(job.scenario_id)
            with job.condition:
                job.status = "running"
                job.updated_at = _now()
                job.condition.notify_all()
            self.emit(job_id, "run_started")
            if scenario.category == "code_level":
                self.emit(job_id, "semantic_fault_started", target_service=scenario.target_service)
                result = run_faulty_image(scenario, self.config, semantic_probe_mode="auto")
                self.emit(job_id, "semantic_fault_finished", status=result.get("status"))
            else:
                orchestrator = create_orchestrator(self.config)
                result = asyncio.run(
                    orchestrator.run_live_problem(
                        scenario,
                        job.task,
                        max_steps=job.max_steps,
                        acknowledge_execute=True,
                        event_sink=lambda event: self.emit(
                            job_id, event.pop("kind", "agent_event"), **event
                        ),
                    )
                )
            job.experiment_id = result.get("experiment_id")
            job.result = result
            job.status = "completed" if result.get("status") == "completed" else "failed"
            if job.status == "failed":
                job.error = f"Experiment ended with status {result.get('status', 'unknown')}"
            job.updated_at = _now()
            self.emit(job_id, job.status, experiment_id=job.experiment_id, result=result)
            try:
                write_dataset_index(
                    self.config.experiment_runs,
                    self.config.research_root,
                    self.config.experiment_index,
                )
            except OSError as error:
                self.emit(job_id, "index_refresh_failed", error=str(error))
        except Exception as error:  # preserve failure visibility for the browser
            job.status = "failed"
            job.error = str(error)
            job.updated_at = _now()
            self.emit(job_id, "failed", error=str(error))
        finally:
            if job.workload_started_by_job:
                try:
                    self._stop_prepared_workload(job)
                    self.emit(job_id, "workload_stopped")
                except Exception as error:
                    self.emit(job_id, "workload_stop_failed", error=str(error))
            with job.condition:
                job.condition.notify_all()

    def emit(self, job_id: str, kind: str, **details: Any) -> dict[str, Any]:
        job = self.jobs[job_id]
        with job.condition:
            event = {
                "sequence": len(job.events),
                "timestamp": _now(),
                "kind": kind,
                **_json_safe(details),
            }
            job.events.append(event)
            job.updated_at = event["timestamp"]
            self.jobs_root.mkdir(parents=True, exist_ok=True)
            with (self.jobs_root / f"{job_id}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
            job.condition.notify_all()
            return event

    def get(self, job_id: str) -> GuiJob:
        with self.lock:
            try:
                return self.jobs[job_id]
            except KeyError as error:
                raise GuiJobError("Unknown job") from error

    def events_after(self, job_id: str, sequence: int = 0) -> list[dict[str, Any]]:
        job = self.get(job_id)
        with job.condition:
            return list(job.events[sequence:])

    def wait_for_events(self, job_id: str, sequence: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        job = self.get(job_id)
        with job.condition:
            if len(job.events) <= sequence and job.status not in TERMINAL_STATUSES:
                job.condition.wait(timeout)
            return list(job.events[sequence:])

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [job.public() for job in sorted(self.jobs.values(), key=lambda item: item.created_at, reverse=True)]

    def list_runs(self) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        if self.config.experiment_runs.is_dir():
            for run_path in self.config.experiment_runs.iterdir():
                if not run_path.is_dir():
                    continue
                metadata = _read_json(run_path / "metadata.json") or {}
                result = _read_json(run_path / "result.json") or {}
                session = _read_json(run_path / "agent-session.json") or {}
                evaluation = _read_json(run_path / "agent-evaluation.json") or {}
                scenario_record = _read_json(run_path / "scenario.json") or {}
                semantic = _read_json(run_path / "semantic-validation.json") or {}
                partial_traces = any(
                    bool((_read_json(run_path / phase / "traces.json") or {}).get("errors"))
                    for phase in ("baseline", "incident", "recovery")
                )
                task = str(evaluation.get("task") or session.get("problem_id", "").rsplit("-", 1)[-1] or ("semantic_fault" if scenario_record.get("category") == "code_level" else "unknown"))
                scores = session.get("results") if isinstance(session.get("results"), dict) else {}
                stages = evaluation.get("stages") if isinstance(evaluation.get("stages"), dict) else {}
                stage_keys = {
                    "detection": "detection_accuracy",
                    "localization": "localization_exact",
                    "analysis": "combined_rca_correct",
                    "mitigation": "mitigation_success",
                }
                if task == "full":
                    task_result = all(
                        stages.get(stage, {}).get(key) is True
                        for stage, key in stage_keys.items()
                    )
                elif task == "semantic_fault":
                    task_result = semantic.get("status") == "passed"
                else:
                    task_result = scores.get(stage_keys.get(task, ""))
                experiment_id = str(metadata.get("experiment_id", run_path.name))
                solution = session.get("solution") if isinstance(session.get("solution"), dict) else {}
                rows.setdefault(experiment_id, {
                    "source": "live-run",
                    "experiment_id": experiment_id,
                    "scenario_id": metadata.get("scenario_id", ""),
                    "scope": scenario_record.get("category", ""),
                    "evaluation_status": result.get("status", "incomplete"),
                    "partial_trace_telemetry": partial_traces,
                    "task": task,
                    "task_result": task_result if result.get("status") == "completed" else None,
                    "stages": {stage: values.get(key) for stage, key in stage_keys.items() for values in [stages.get(stage, {})]} if task == "full" else {},
                    "submitted": session.get("submitted", False),
                    "duration_seconds": session.get("duration_seconds"),
                    "predicted_service": (solution.get("faulty_components") or [""])[0],
                    "predicted_category": solution.get("system_level", ""),
                    "predicted_fault_type": solution.get("fault_type", ""),
                    "combined_rca_correct": session.get("results", {}).get("combined_rca_correct", ""),
                    "recommendation_acceptable": "",
                    "mttd_proxy_tta_seconds": session.get("duration_seconds", ""),
                    "mttr_proxy_tta_seconds": "",
                })
        for experiment_id, row in rows.items():
            remediation = _read_json(
                self.config.experiment_runs / experiment_id / "remediation-session.json"
            ) or {}
            row["has_remediation_recommendation"] = bool(remediation.get("solution"))
        return sorted(rows.values(), key=lambda row: row.get("experiment_id", ""), reverse=True)

    def run_detail(self, experiment_id: str) -> dict[str, Any]:
        if not experiment_id or experiment_id in {".", ".."} or Path(experiment_id).name != experiment_id:
            raise GuiJobError("Invalid experiment_id")
        run_path = self.config.experiment_runs / experiment_id
        if not run_path.is_dir():
            raise GuiJobError("Experiment not found")
        sessions: dict[str, Any] = {}
        result = _read_json(run_path / "result.json") or {}
        detail: dict[str, Any] = {
            "experiment_id": experiment_id,
            "metadata": _json_safe(_read_json(run_path / "metadata.json") or {}),
            "scenario": _json_safe(_read_json(run_path / "scenario.json") or {}),
            "result": _json_safe(result),
            "events": [],
            "agent_session": None,
            "agent_sessions": sessions,
            "agent_evaluation": _json_safe(_read_json(run_path / "agent-evaluation.json") or {}),
            "semantic_validation": _json_safe(_read_json(run_path / "semantic-validation.json") or {}),
            "semantic_observation": _json_safe(_read_json(run_path / "semantic-observation.json") or {}),
            "semantic_probe": _json_safe(_read_json(run_path / "semantic-probe.json") or {}),
            "faulty_image_plan": _json_safe(_read_json(run_path / "faulty-image-plan.json") or {}),
            "telemetry": {
                phase: {
                    kind: _json_safe(_read_json(run_path / phase / f"{kind}.json") or {})
                    for kind in ("metrics", "logs", "traces", "alerts")
                }
                for phase in ("baseline", "incident", "recovery")
                if (run_path / phase).is_dir()
            },
            "task": str((_read_json(run_path / "agent-evaluation.json") or {}).get("task") or (_read_json(run_path / "agent-session.json") or {}).get("problem_id", "").rsplit("-", 1)[-1] or ("semantic_fault" if (_read_json(run_path / "scenario.json") or {}).get("category") == "code_level" else "unknown")),
            "pilot_assessment": _json_safe(_read_json(run_path / "pilot-assessment.json") or {}),
            "partial_trace_telemetry": any(
                bool((_read_json(run_path / phase / "traces.json") or {}).get("errors"))
                for phase in ("baseline", "incident", "recovery")
            ),
        }
        events_path = run_path / "events.jsonl"
        if events_path.is_file():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    detail["events"].append(_json_safe(json.loads(line)))
                except json.JSONDecodeError:
                    continue
        session = _read_json(run_path / "agent-session.json")
        if session is not None:
            detail["agent_session"] = _json_safe(session)
            detail["agent_sessions"]["live_mitigation" if detail["task"] == "mitigation" else "live_rca"] = _json_safe(session)
        remediation_session = _read_json(run_path / "remediation-session.json")
        if remediation_session is not None:
            detail["agent_sessions"]["remediation_recommendation"] = _json_safe(remediation_session)
        for stage in ("detection", "localization", "mitigation"):
            stage_session = _read_json(run_path / f"{stage}-session.json")
            if stage_session is not None:
                detail["agent_sessions"][f"live_{stage}"] = _json_safe(stage_session)
        if result.get("status") in {"completed", "injector_failed", "failed"}:
            detail["ground_truth"] = _json_safe(
                _read_json(run_path / "ground_truth.json") or {}, hide_labels=False
            )
        return detail
