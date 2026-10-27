"""Batch replay evaluator for the Phase 04 RCA baseline.

The evaluator deliberately has two separate phases for every run:

1. replay the agent against baseline/incident telemetry with an empty evaluator label;
2. hand the ground truth to the task after the agent has stopped and calculate scores.

This keeps the dataset useful for measuring RCA rather than allowing the known label to
become another input feature.  Failed rows are retained in the output so a provider or
bad run cannot silently reduce the evaluation denominator.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

from research.utils import ResearchConfig, write_json

from .models import AgentSession
from .orchestrator import AgentOrchestrator, LlmAgent
from .providers import FakeProvider, LlmProvider, ProviderError, build_provider


RESULT_COLUMNS = (
    "experiment_id",
    "scenario_id",
    "run_path",
    "evaluation_scope",
    "confounder_affected",
    "evaluation_status",
    "predicted_service",
    "predicted_services",
    "predicted_category",
    "predicted_fault_type",
    "valid_submission",
    "submitted",
    "termination_reason",
    "invalid_action_count",
    "service_correct",
    "service_top1_correct",
    "service_top3_correct",
    "fault_category_correct",
    "fault_type_correct",
    "combined_rca_correct",
    "duration_seconds",
    "tta_seconds",
    "error",
)


@dataclass(frozen=True)
class EvaluationRun:
    """One eligible index row with its resolved replay directory."""

    row: dict[str, str]
    run_dir: Path

    @property
    def experiment_id(self) -> str:
        return self.row["experiment_id"]


def _truth(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to read ground truth: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"ground truth must be an object: {path}")
    return value


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid ISO-8601 timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed


def _bool(value: str) -> bool:
    return value.strip().lower() == "true"


def _resolve_run_path(index_path: Path, run_path: str, config: ResearchConfig) -> Path:
    supplied = Path(run_path)
    if supplied.is_absolute():
        return supplied.resolve()

    # Dataset indexes contain paths relative to research_root (for example,
    # ``experiments/runs/EXP-...``).  The fallbacks make synthetic indexes convenient
    # in unit tests without changing the production path contract.
    candidates = (
        config.research_root / supplied,
        index_path.parent.parent / supplied,
        index_path.parent / supplied,
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


def read_eligible_runs(
    index_path: Path,
    config: ResearchConfig,
    *,
    experiment_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[EvaluationRun]:
    """Read every eligible row from the derived index, in CSV order.

    The function does not inspect eligibility from the run directory.  The index is the
    single selection authority, so a stale or malformed eligible row becomes an explicit
    evaluator failure instead of being silently filtered out.
    """
    try:
        with index_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError as error:
        raise ValueError(f"unable to read evaluation index: {index_path}: {error}") from error
    required = {"experiment_id", "scenario_id", "run_path", "eligible_for_evaluation"}
    missing = required - set(rows[0]) if rows else required
    if missing:
        raise ValueError(f"evaluation index is missing columns: {', '.join(sorted(missing))}")

    selected: list[EvaluationRun] = []
    for row in rows:
        if not _bool(row.get("eligible_for_evaluation", "")):
            continue
        experiment_id = row.get("experiment_id", "")
        if experiment_ids and experiment_id not in experiment_ids:
            continue
        selected.append(
            EvaluationRun(row=dict(row), run_dir=_resolve_run_path(index_path, row["run_path"], config))
        )
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        selected = selected[:limit]
    return selected


def _blank_result(item: EvaluationRun, *, error: str) -> dict[str, Any]:
    result: dict[str, Any] = {column: "" for column in RESULT_COLUMNS}
    result.update(
        {
            "experiment_id": item.experiment_id,
            "scenario_id": item.row.get("scenario_id", ""),
            "run_path": item.row.get("run_path", ""),
            "evaluation_scope": (
                "code_level"
                if item.row.get("fault_category") == "code_level"
                else "runtime"
            ),
            "confounder_affected": _bool(item.row.get("confounder_affected", "")),
            "evaluation_status": "failure",
            "valid_submission": False,
            "submitted": False,
            "termination_reason": "evaluation_failed",
            "invalid_action_count": 0,
            "service_correct": False,
            "service_top1_correct": False,
            "service_top3_correct": False,
            "fault_category_correct": False,
            "fault_type_correct": False,
            "combined_rca_correct": False,
            "error": error,
        }
    )
    return result


def _prediction(solution: Any) -> tuple[str, list[str], str, str]:
    if not isinstance(solution, dict):
        return "", [], "", ""
    raw_services = solution.get("faulty_components", [])
    services = [item for item in raw_services if isinstance(item, str)] if isinstance(raw_services, list) else []
    return (
        services[0] if services else "",
        services,
        str(solution.get("system_level", "")),
        str(solution.get("fault_type", "")),
    )


def _result_from_session(
    item: EvaluationRun,
    session: AgentSession,
    truth: dict[str, Any],
    evaluated: dict[str, Any],
) -> dict[str, Any]:
    predicted_service, predicted_services, category, fault_type = _prediction(session.solution)
    target = truth.get("root_cause_service", "")
    service_top1 = bool(predicted_services and predicted_services[0] == target)
    service_top3 = target in predicted_services[:3]
    # The task contract validates these before marking a submission successful.  Keep a
    # separate field because a provider can terminate without ever making a submission.
    valid = bool(session.submitted and isinstance(session.solution, dict))
    result: dict[str, Any] = {
        "experiment_id": item.experiment_id,
        "scenario_id": item.row.get("scenario_id", truth.get("scenario_id", "")),
        "run_path": item.row.get("run_path", ""),
        "evaluation_scope": (
            "code_level" if truth.get("fault_category") == "code_level" else "runtime"
        ),
        "confounder_affected": _bool(item.row.get("confounder_affected", "")),
        "evaluation_status": "completed",
        "predicted_service": predicted_service,
        "predicted_services": json.dumps(predicted_services, separators=(",", ":")),
        "predicted_category": category,
        "predicted_fault_type": fault_type,
        "valid_submission": valid,
        "submitted": session.submitted,
        "termination_reason": session.termination_reason or "",
        "invalid_action_count": session.invalid_action_count,
        "service_correct": service_top1,
        "service_top1_correct": service_top1,
        "service_top3_correct": service_top3,
        "fault_category_correct": bool(valid and category == truth.get("fault_category")),
        "fault_type_correct": bool(valid and fault_type == truth.get("fault_type")),
        "combined_rca_correct": bool(
            valid
            and service_top1
            and category == truth.get("fault_category")
            and fault_type == truth.get("fault_type")
        ),
        "duration_seconds": session.duration_seconds,
        "tta_seconds": session.duration_seconds,
        "error": "",
    }
    # Preserve task-level fields for debugging consistency, but never let them override
    # the evaluator's explicit valid-submission and Top-1/Top-3 semantics.
    result.update({key: value for key, value in evaluated.items() if key not in result})
    return result


def _metric_bundle(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    valid = sum(bool(row["valid_submission"]) for row in rows)
    def rate(count: int, denominator: int = total) -> float:
        return count / denominator if denominator else 0.0

    tta_values = [float(row["tta_seconds"]) for row in rows if row["tta_seconds"] not in ("", None)]
    service_top1 = sum(bool(row["service_top1_correct"]) for row in rows)
    service_top3 = sum(bool(row["service_top3_correct"]) for row in rows)
    category = sum(bool(row["fault_category_correct"]) for row in rows)
    fault_type = sum(bool(row["fault_type_correct"]) for row in rows)
    combined = sum(bool(row["combined_rca_correct"]) for row in rows)
    return {
        "run_count": total,
        "valid_submission_count": valid,
        "valid_submission_rate": rate(valid),
        "service_top1_accuracy": rate(service_top1),
        "service_top3_accuracy": rate(service_top3),
        "fault_category_accuracy": rate(category),
        "fault_type_accuracy": rate(fault_type),
        "combined_rca_accuracy": rate(combined),
        "mean_tta_seconds": sum(tta_values) / len(tta_values) if tta_values else None,
    }


class BatchEvaluator:
    """Replay all selected index rows and write an auditable evaluation bundle."""

    def __init__(
        self,
        config: ResearchConfig,
        *,
        provider_factory: Callable[[ResearchConfig], LlmProvider] | None = None,
        output_dir: Path | None = None,
        max_steps: int = 15,
        provider_name: str | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.config = config
        self.provider_factory = provider_factory or build_provider
        self.output_dir = output_dir or config.evaluations_dir / datetime.now(UTC).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        self.max_steps = max_steps
        self.provider_name = provider_name or config.aiops_llm_provider

    def _config_for_run(self, run_dir: Path) -> ResearchConfig:
        try:
            run_dir.relative_to(self.config.experiment_runs.resolve())
            return self.config
        except ValueError:
            # Synthetic indexes commonly use a temporary runs directory.  Keep the
            # registry's path safety check while adapting only this evaluator instance.
            return replace(self.config, experiment_runs=run_dir.parent)

    async def _evaluate_one(self, item: EvaluationRun) -> dict[str, Any]:
        session: AgentSession | None = None
        try:
            truth = _truth(item.run_dir / "ground_truth.json")
            start = _timestamp(str(truth["injection_start"]))
            end = _timestamp(str(truth["injection_end"]))
            scenario_id = item.row.get("scenario_id") or str(truth["scenario_id"])
            if not scenario_id:
                raise ValueError("missing scenario_id")
            run_config = self._config_for_run(item.run_dir)
            provider = self.provider_factory(run_config)
            orchestrator = AgentOrchestrator(run_config)
            orchestrator.register_agent(LlmAgent(provider), name="rca-batch-agent")
            _, task, session = orchestrator.init_problem(
                f"{scenario_id}-analysis",
                start=start,
                end=end,
                run_dir=item.run_dir,
                withhold_ground_truth=True,
            )
            await orchestrator.start_problem(
                task,
                session,
                max_steps=self.max_steps,
                evaluate=False,
                persist=False,
            )
            evaluated = orchestrator.finalize_session(
                task, session, expected=truth, persist=False
            )
            result = _result_from_session(item, session, truth, evaluated["results"])
            self._write_trace(item, evaluated)
            return result
        except Exception as error:  # one bad run must not hide the remaining dataset
            if session is not None:
                session.termination_reason = session.termination_reason or "evaluation_failed"
                self._write_trace(
                    item,
                    {
                        **session.to_dict(),
                        "evaluation_status": "failure",
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
            return _blank_result(item, error=f"{type(error).__name__}: {error}")

    def _write_trace(self, item: EvaluationRun, payload: dict[str, Any]) -> None:
        trace_dir = self.output_dir / "session-traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        destination = trace_dir / f"{item.experiment_id}.json"
        if destination.exists():
            session_id = str(payload.get("session_id", "retry"))
            destination = trace_dir / f"{item.experiment_id}-{session_id}.json"
        write_json(destination, payload)

    def evaluate(self, runs: Iterable[EvaluationRun]) -> dict[str, Any]:
        selected = list(runs)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        rows = [asyncio.run(self._evaluate_one(item)) for item in selected]
        rows.sort(key=lambda row: row["experiment_id"])
        self._write_csv(rows)

        runtime_rows = [row for row in rows if row["evaluation_scope"] == "runtime"]
        code_level_rows = [row for row in rows if row["evaluation_scope"] == "code_level"]
        clean = [row for row in runtime_rows if not row["confounder_affected"]]
        confounded = [row for row in runtime_rows if row["confounder_affected"]]
        clean_code_level = [
            row for row in code_level_rows if not row["confounder_affected"]
        ]
        failures = sum(row["evaluation_status"] == "failure" for row in rows)
        summary: dict[str, Any] = {
            "schema_version": 1,
            "task": "analysis",
            "provider": self.provider_name,
            "max_steps": self.max_steps,
            "eligible_run_count": len(selected),
            "processed_run_count": len(rows),
            "failed_run_count": failures,
            "all_eligible_runs_processed": len(rows) == len(selected),
            "metrics": _metric_bundle(rows),
            "headline_metrics": _metric_bundle(runtime_rows),
            "code_level_metrics": _metric_bundle(code_level_rows),
            "clean_run_metrics": _metric_bundle(clean),
            "confounder_affected_run_metrics": _metric_bundle(confounded),
            "clean_code_level_metrics": _metric_bundle(clean_code_level),
            "results_csv": str(self.output_dir / "evaluation-results.csv"),
            "session_trace_dir": str(self.output_dir / "session-traces"),
        }
        write_json(self.output_dir / "evaluation-summary.json", summary)
        write_json(self.output_dir / "evaluation-results.json", {"results": rows})
        return summary

    def _write_csv(self, rows: list[dict[str, Any]]) -> None:
        with (self.output_dir / "evaluation-results.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
            writer.writeheader()
            writer.writerows({column: row.get(column, "") for column in RESULT_COLUMNS} for row in rows)


def _cli_provider(config: ResearchConfig, provider_name: str | None, smoke: bool) -> LlmProvider:
    if smoke:
        if provider_name and provider_name.lower() != "fake":
            raise ValueError("--smoke requires the fake provider")
        return FakeProvider()
    if provider_name:
        config = replace(config, aiops_llm_provider=provider_name)
    if config.aiops_llm_provider.lower() == "fake":
        raise ValueError(
            "headline RCA evaluation requires the configured LLM provider; use --smoke for fake"
        )
    return build_provider(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the RCA agent over eligible replay runs")
    parser.add_argument("--index", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--experiment-id", action="append", dest="experiment_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--provider", help="override AIOPS_LLM_PROVIDER")
    parser.add_argument("--smoke", action="store_true", help="run one fake-provider smoke replay")
    arguments = parser.parse_args(argv)
    try:
        config = ResearchConfig.from_environment()
        index_path = (arguments.index or config.experiment_index).resolve()
        run_limit = 1 if arguments.smoke and arguments.limit is None else arguments.limit
        evaluator = BatchEvaluator(
            config,
            provider_factory=lambda run_config: _cli_provider(
                run_config, arguments.provider, arguments.smoke
            ),
            output_dir=arguments.output_dir,
            max_steps=arguments.max_steps,
            provider_name=(
                "fake"
                if arguments.smoke
                else arguments.provider or config.aiops_llm_provider
            ),
        )
        runs = read_eligible_runs(
            index_path,
            config,
            experiment_ids=set(arguments.experiment_ids or []),
            limit=run_limit,
        )
        if not runs:
            raise ValueError("no eligible runs selected")
        summary = evaluator.evaluate(runs)
    except (OSError, ProviderError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
