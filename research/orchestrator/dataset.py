"""Deterministic dataset index generation from append-only experiment runs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
from typing import Any


INDEX_COLUMNS = (
    "experiment_id",
    "scenario_id",
    "run_path",
    "target_service",
    "fault_category",
    "fault_type",
    "severity",
    "run_status",
    "semantic_validation_status",
    "host_impact_status",
    "workload_status",
    "baseline_available",
    "incident_available",
    "recovery_available",
    "confounder_affected",
    "eligible_for_evaluation",
    "exclusion_reason",
)

TELEMETRY_FILES = ("metrics.json", "logs.json", "traces.json", "alerts.json")
CONFOUNDER_SERVICES = {"adservice", "shoppingassistantservice"}
GROUND_TRUTH_FIELDS = (
    "experiment_id",
    "scenario_id",
    "root_cause_service",
    "fault_category",
    "fault_type",
    "target_service",
    "injection_start",
    "injection_end",
    "severity",
)


@dataclass(frozen=True)
class DatasetIndexResult:
    """Summary returned after writing a dataset index."""

    output: Path
    total_runs: int
    eligible_runs: int
    excluded_runs: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "generated",
            "output": str(self.output),
            "total_runs": self.total_runs,
            "eligible_runs": self.eligible_runs,
            "excluded_runs": self.excluded_runs,
        }


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"invalid_{path.stem}: {error}"
    if not isinstance(value, dict):
        return None, f"invalid_{path.stem}: expected a JSON object"
    return value, None


def _phase_available(run_path: Path, phase: str) -> bool:
    phase_path = run_path / phase
    return all((phase_path / filename).is_file() for filename in TELEMETRY_FILES)


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _loadgenerator_observed(metrics: dict[str, Any] | None) -> bool:
    result = (
        (metrics or {})
        .get("queries", {})
        .get("container_cpu", {})
        .get("data", {})
        .get("result", [])
    )
    if not isinstance(result, list):
        return False
    return any(
        isinstance(series, dict)
        and series.get("metric", {}).get("service") == "loadgenerator"
        for series in result
    )


def index_run(run_path: Path, research_root: Path) -> dict[str, str]:
    """Build one CSV row without changing the experiment directory."""
    metadata, metadata_error = _read_json(run_path / "metadata.json")
    result, result_error = _read_json(run_path / "result.json")
    ground_truth, ground_truth_error = _read_json(run_path / "ground_truth.json")
    semantic, semantic_error = _read_json(run_path / "semantic-validation.json")
    scenario, scenario_error = _read_json(run_path / "scenario.json")
    baseline_metrics, baseline_metrics_error = _read_json(
        run_path / "baseline" / "metrics.json"
    )
    metadata = metadata or {}
    result = result or {}
    ground_truth = ground_truth or {}

    experiment_id = (
        _string(ground_truth.get("experiment_id"))
        or _string(metadata.get("experiment_id"))
        or run_path.name
    )
    scenario_id = (
        _string(ground_truth.get("scenario_id"))
        or _string(metadata.get("scenario_id"))
    )
    target_service = (
        _string(ground_truth.get("target_service"))
        or _string(ground_truth.get("root_cause_service"))
    )
    fault_category = _string(ground_truth.get("fault_category"))
    fault_type = _string(ground_truth.get("fault_type"))
    run_status = _string(result.get("status")) or "incomplete"
    baseline_available = _phase_available(run_path, "baseline")
    incident_available = _phase_available(run_path, "incident")
    recovery_available = _phase_available(run_path, "recovery")

    if fault_category == "code_level":
        semantic_status = _string((semantic or {}).get("status")) or "missing"
    else:
        semantic_status = "not_required"
    host_impact, host_impact_error = _read_json(run_path / "host-impact.json")
    if fault_type == "cpu_hog":
        host_impact_status = _string((host_impact or {}).get("status")) or "missing"
    else:
        host_impact_status = "not_required"
    workload = (scenario or {}).get("workload", {})
    workload_generator = workload.get("generator") if isinstance(workload, dict) else None
    if workload_generator == "loadgenerator":
        workload_status = (
            "observed" if _loadgenerator_observed(baseline_metrics) else "missing"
        )
    else:
        workload_status = "not_required"

    reasons: list[str] = []
    for error in (
        metadata_error,
        result_error,
        ground_truth_error,
        semantic_error,
        host_impact_error,
        scenario_error,
        baseline_metrics_error,
    ):
        if error:
            reasons.append(error)
    if not metadata:
        reasons.append("missing_metadata")
    if not result:
        reasons.append("missing_result")
    if not ground_truth:
        reasons.append("missing_ground_truth")
    else:
        missing_fields = [
            field
            for field in GROUND_TRUTH_FIELDS
            if ground_truth.get(field) in (None, "")
        ]
        if missing_fields:
            reasons.append("incomplete_ground_truth:" + "|".join(missing_fields))
    if run_status != "completed":
        reasons.append(f"run_status:{run_status}")
    if not baseline_available:
        reasons.append("missing_baseline_telemetry")
    if not incident_available:
        reasons.append("missing_incident_telemetry")
    if not recovery_available:
        reasons.append("missing_recovery_telemetry")
    if fault_category == "code_level" and semantic_status != "passed":
        reasons.append(f"semantic_validation:{semantic_status}")
    if fault_type == "cpu_hog" and host_impact_status not in {"flagged", "not_flagged"}:
        reasons.append(f"host_impact:{host_impact_status}")
    if workload_generator == "loadgenerator" and workload_status != "observed":
        reasons.append(f"workload:{workload_status}")

    try:
        relative_path = run_path.relative_to(research_root)
    except ValueError:
        relative_path = run_path

    eligible = not reasons
    return {
        "experiment_id": experiment_id,
        "scenario_id": scenario_id,
        "run_path": relative_path.as_posix(),
        "target_service": target_service,
        "fault_category": fault_category,
        "fault_type": fault_type,
        "severity": _string(ground_truth.get("severity")),
        "run_status": run_status,
        "semantic_validation_status": semantic_status,
        "host_impact_status": host_impact_status,
        "workload_status": workload_status,
        "baseline_available": str(baseline_available).lower(),
        "incident_available": str(incident_available).lower(),
        "recovery_available": str(recovery_available).lower(),
        "confounder_affected": str(target_service in CONFOUNDER_SERVICES).lower(),
        "eligible_for_evaluation": str(eligible).lower(),
        "exclusion_reason": ";".join(reasons),
    }


def build_dataset_index(runs_root: Path, research_root: Path) -> list[dict[str, str]]:
    """Index every run directory in stable experiment-ID order."""
    if not runs_root.is_dir():
        return []
    rows = [
        index_run(path, research_root)
        for path in runs_root.iterdir()
        if path.is_dir()
    ]
    return sorted(rows, key=lambda row: (row["experiment_id"], row["run_path"]))


def write_dataset_index(
    runs_root: Path, research_root: Path, output: Path
) -> DatasetIndexResult:
    """Atomically replace the derived CSV index; source runs remain untouched."""
    rows = build_dataset_index(runs_root, research_root)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=INDEX_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = buffer.getvalue()

    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.exists() or output.read_text(encoding="utf-8") != content:
        temporary = output.with_suffix(f"{output.suffix}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(output)

    eligible = sum(row["eligible_for_evaluation"] == "true" for row in rows)
    return DatasetIndexResult(
        output=output,
        total_runs=len(rows),
        eligible_runs=eligible,
        excluded_runs=len(rows) - eligible,
    )
