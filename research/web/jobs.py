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

from research.agent.live import LiveIncidentController
from research.agent.orchestrator import create_orchestrator
from research.agent.sanitize import sanitize_text
from research.generators import FaultScenario, ScenarioError, load_scenario
from research.observer import Observer
from research.orchestrator.dataset import write_dataset_index
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
            record["runnable_for_gui"] = scenario.runnable and scenario.category != "code_level"
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
        if not scenario.runnable or scenario.category == "code_level":
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
            plan = injector.plan(scenario, target_state.container_id)
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
            "plan": plan.to_dict(),
            "warnings": [
                "Fault injection is bounded to one scenario and one target.",
                "The required loadgenerator is started during preparation when it is not already running.",
                "RCA runs with ground truth withheld until the agent submits.",
            ],
            "workload_started": workload_started,
        }

    def prepare(self, scenario_id: str, task: str, max_steps: int) -> dict[str, Any]:
        if task != "analysis":
            raise GuiJobError("GUI live runs currently support task=analysis only")
        if max_steps < 1 or max_steps > 50:
            raise GuiJobError("max_steps must be between 1 and 50")
        with self.lock:
            active = self._active_job()
            if active is not None:
                raise GuiJobError(f"Another GUI job is active: {active.job_id}")
        scenario = self._scenario(scenario_id)
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
            orchestrator = create_orchestrator(self.config)
            controller = LiveIncidentController(
                self.config,
                orchestrator,
                event_sink=lambda event: self.emit(job_id, event.pop("kind", "agent_event"), **event),
            )
            result = asyncio.run(
                controller.run(
                    scenario,
                    job.task,
                    max_steps=job.max_steps,
                    acknowledge_execute=True,
                )
            )
            job.experiment_id = result.get("experiment_id")
            job.result = result
            job.status = "completed"
            job.updated_at = _now()
            self.emit(job_id, "completed", experiment_id=job.experiment_id, result=result)
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
        final_path = self.config.evaluations_dir / "phase-06-final" / "final-results.csv"
        phase04_path = self.config.evaluations_dir / "phase-04-analysis-final-v5" / "evaluation-results.csv"
        phase04_rows = {row.get("experiment_id", ""): row for row in _read_csv(phase04_path)}
        for row in _read_csv(final_path):
            phase04 = phase04_rows.get(row.get("experiment_id", ""), {})
            rows[row.get("experiment_id", "")] = {
                "source": "phase-06",
                "experiment_id": row.get("experiment_id", ""),
                "scenario_id": row.get("scenario_id", ""),
                "scope": row.get("evaluation_scope", ""),
                "evaluation_status": row.get("evaluation_status", ""),
                "predicted_service": phase04.get("predicted_service", row.get("predicted_service", "")),
                "predicted_category": phase04.get("predicted_category", row.get("predicted_category", "")),
                "predicted_fault_type": phase04.get("predicted_fault_type", row.get("predicted_fault_type", "")),
                "valid_submission": phase04.get("valid_submission", row.get("rca_valid_submission", "")),
                "termination_reason": phase04.get("termination_reason", ""),
                "invalid_action_count": phase04.get("invalid_action_count", ""),
                "combined_rca_correct": row.get("combined_rca_correct", ""),
                "recommendation_acceptable": row.get("recommendation_acceptable", ""),
                "mttd_proxy_tta_seconds": row.get("mttd_proxy_tta_seconds", ""),
                "mttr_proxy_tta_seconds": row.get("mttr_proxy_tta_seconds", ""),
            }
        if self.config.experiment_runs.is_dir():
            for run_path in self.config.experiment_runs.iterdir():
                if not run_path.is_dir():
                    continue
                metadata = _read_json(run_path / "metadata.json") or {}
                result = _read_json(run_path / "result.json") or {}
                session = _read_json(run_path / "agent-session.json") or {}
                experiment_id = str(metadata.get("experiment_id", run_path.name))
                solution = session.get("solution") if isinstance(session.get("solution"), dict) else {}
                rows.setdefault(experiment_id, {
                    "source": "live-run",
                    "experiment_id": experiment_id,
                    "scenario_id": metadata.get("scenario_id", ""),
                    "scope": (_read_json(run_path / "scenario.json") or {}).get("category", ""),
                    "evaluation_status": result.get("status", "incomplete"),
                    "predicted_service": (solution.get("faulty_components") or [""])[0],
                    "predicted_category": solution.get("system_level", ""),
                    "predicted_fault_type": solution.get("fault_type", ""),
                    "combined_rca_correct": session.get("results", {}).get("combined_rca_correct", ""),
                    "recommendation_acceptable": "",
                    "mttd_proxy_tta_seconds": session.get("duration_seconds", ""),
                    "mttr_proxy_tta_seconds": "",
                })
        return sorted(rows.values(), key=lambda row: row.get("experiment_id", ""), reverse=True)

    def run_detail(self, experiment_id: str) -> dict[str, Any]:
        if not experiment_id or Path(experiment_id).name != experiment_id:
            raise GuiJobError("Invalid experiment_id")
        run_path = self.config.experiment_runs / experiment_id
        evaluation: dict[str, Any] = {}
        for name, path in (
            ("final", self.config.evaluations_dir / "phase-06-final" / "final-results.csv"),
            ("rca", self.config.evaluations_dir / "phase-04-analysis-final-v5" / "evaluation-results.csv"),
            ("remediation", self.config.evaluations_dir / "phase-05-remediation-v1" / "remediation-results.csv"),
        ):
            row = next((row for row in _read_csv(path) if row.get("experiment_id") == experiment_id), None)
            if row is not None:
                evaluation[name] = row
        if not run_path.is_dir() and not evaluation:
            raise GuiJobError("Experiment not found")
        if not run_path.is_dir():
            return {
                "experiment_id": experiment_id,
                "metadata": {},
                "scenario": {},
                "result": {},
                "events": [],
                "agent_session": None,
                "evaluation": _json_safe(evaluation),
            }
        result = _read_json(run_path / "result.json") or {}
        detail: dict[str, Any] = {
            "experiment_id": experiment_id,
            "metadata": _json_safe(_read_json(run_path / "metadata.json") or {}),
            "scenario": _json_safe(_read_json(run_path / "scenario.json") or {}),
            "result": _json_safe(result),
            "evaluation": _json_safe(evaluation),
            "events": [],
            "agent_session": None,
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
        if result.get("status") in {"completed", "injector_failed", "failed"}:
            detail["ground_truth"] = _json_safe(
                _read_json(run_path / "ground_truth.json") or {}, hide_labels=False
            )
        return detail
