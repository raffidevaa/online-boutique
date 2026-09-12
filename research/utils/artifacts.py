"""Small append-only helpers for experiment and snapshot artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
import uuid


def new_experiment_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"EXP-{timestamp}-{uuid.uuid4().hex[:6]}"


def write_json(path: Path, value: Any) -> None:
    """Write a JSON artifact once; existing evidence is never overwritten."""
    if path.exists():
        raise FileExistsError(f"Artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def create_run(runs_root: Path, scenario_id: str | None = None) -> tuple[Path, dict[str, Any]]:
    """Create a unique run directory and its initial metadata artifact."""
    experiment_id = new_experiment_id()
    run_path = runs_root / experiment_id
    run_path.mkdir(parents=True, exist_ok=False)
    metadata = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "status": "created",
        "timestamp_start": datetime.now(UTC).isoformat(),
        "scenario_id": scenario_id,
    }
    write_json(run_path / "metadata.json", metadata)
    return run_path, metadata


def append_event(run_path: Path, event: str, details: dict[str, Any] | None = None) -> None:
    """Append a timestamped event without rewriting a prior experiment record."""
    payload = {
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "details": details or {},
    }
    with (run_path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
