"""Two-stage replay evaluator for Phase 05 remediation recommendations."""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

from research.utils import ResearchConfig, write_json

from .evaluator import EvaluationRun, _bool, _timestamp, _truth, read_eligible_runs
from .models import AgentSession
from .orchestrator import AgentOrchestrator, LlmAgent
from .providers import FakeProvider, LlmProvider, ProviderError, build_provider
from .remediation_review import write_review_template


RESULT_COLUMNS = (
    "experiment_id",
    "scenario_id",
    "run_path",
    "evaluation_scope",
    "confounder_affected",
    "evaluation_status",
    "rca_valid_submission",
    "rca_predicted_services",
    "rca_predicted_category",
    "rca_predicted_fault_type",
    "action_type",
    "target_service",
    "recommendation_json",
    "recommendation_valid",
    "recommendation_complete",
    "risk",
    "requires_approval",
    "execution_allowed",
    "submitted",
    "termination_reason",
    "invalid_action_count",
    "duration_seconds",
    "tta_seconds",
    "correctness_score",
    "relevance_score",
    "safety_score",
    "actionability_score",
    "review_justification",
    "review_status",
    "error",
)


def _rca_prediction(solution: Any) -> tuple[list[str], str, str]:
    if not isinstance(solution, dict):
        return [], "", ""
    services = solution.get("faulty_components", [])
    if not isinstance(services, list):
        services = []
    return (
        [item for item in services if isinstance(item, str)],
        str(solution.get("system_level", "")),
        str(solution.get("fault_type", "")),
    )


def _blank_result(item: EvaluationRun, error: str) -> dict[str, Any]:
    result = {column: "" for column in RESULT_COLUMNS}
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
            "recommendation_valid": False,
            "recommendation_complete": False,
            "submitted": False,
            "termination_reason": "evaluation_failed",
            "invalid_action_count": 0,
            "review_status": "pending",
            "error": error,
        }
    )
    return result


def _result_from_sessions(
    item: EvaluationRun,
    truth: dict[str, Any],
    analysis_session: AgentSession,
    remediation_session: AgentSession,
    evaluated: dict[str, Any],
) -> dict[str, Any]:
    services, category, fault_type = _rca_prediction(analysis_session.solution)
    recommendation = (
        remediation_session.solution
        if isinstance(remediation_session.solution, dict)
        else {}
    )
    recommendation_valid = bool(
        evaluated.get("results", {}).get("recommendation_valid")
    )
    result: dict[str, Any] = {column: "" for column in RESULT_COLUMNS}
    result.update(
        {
            "experiment_id": item.experiment_id,
            "scenario_id": item.row.get("scenario_id", truth.get("scenario_id", "")),
            "run_path": item.row.get("run_path", ""),
            "evaluation_scope": (
                "code_level" if truth.get("fault_category") == "code_level" else "runtime"
            ),
            "confounder_affected": _bool(item.row.get("confounder_affected", "")),
            "evaluation_status": "completed",
            "rca_valid_submission": bool(
                analysis_session.submitted and isinstance(analysis_session.solution, dict)
            ),
            "rca_predicted_services": json.dumps(services, separators=(",", ":")),
            "rca_predicted_category": category,
            "rca_predicted_fault_type": fault_type,
            "action_type": recommendation.get("action_type", ""),
            "target_service": recommendation.get("target_service", ""),
            "recommendation_json": json.dumps(recommendation, sort_keys=True),
            "recommendation_valid": recommendation_valid,
            "recommendation_complete": recommendation_valid,
            "risk": recommendation.get("risk", ""),
            "requires_approval": recommendation.get("requires_approval", ""),
            "execution_allowed": recommendation.get("execution_allowed", ""),
            "submitted": remediation_session.submitted,
            "termination_reason": remediation_session.termination_reason or "",
            "invalid_action_count": remediation_session.invalid_action_count,
            "duration_seconds": analysis_session.duration_seconds
            + remediation_session.duration_seconds,
            "tta_seconds": remediation_session.duration_seconds,
            "review_status": "pending",
            "error": "",
        }
    )
    return result


def _recommendation_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    valid = sum(bool(row["recommendation_valid"]) for row in rows)
    submitted = sum(bool(row["submitted"]) for row in rows)
    execution_safe = sum(row["execution_allowed"] in {False, "False"} for row in rows)
    tta = [float(row["tta_seconds"]) for row in rows if row["tta_seconds"] not in ("", None)]
    return {
        "run_count": total,
        "valid_recommendation_count": valid,
        "valid_recommendation_rate": valid / total if total else 0.0,
        "submitted_count": submitted,
        "submission_rate": submitted / total if total else 0.0,
        "recommendation_only_rate": execution_safe / total if total else 0.0,
        "mean_tta_seconds": sum(tta) / len(tta) if tta else None,
    }


class RemediationBatchEvaluator:
    """Run analysis followed by recommendation and preserve both stage traces."""

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
            "%Y%m%dT%H%M%SZ-remediation"
        )
        self.max_steps = max_steps
        self.provider_name = provider_name or config.aiops_llm_provider

    def _config_for_run(self, run_dir: Path) -> ResearchConfig:
        try:
            run_dir.relative_to(self.config.experiment_runs.resolve())
            return self.config
        except ValueError:
            return replace(self.config, experiment_runs=run_dir.parent)

    async def _stage(
        self,
        run_config: ResearchConfig,
        problem_id: str,
        start: datetime,
        end: datetime,
        run_dir: Path,
        *,
        rca_solution: dict[str, Any] | None = None,
    ) -> tuple[AgentSession, Any]:
        provider = self.provider_factory(run_config)
        orchestrator = AgentOrchestrator(run_config)
        orchestrator.register_agent(LlmAgent(provider), name="phase-05-agent")
        _, task, session = orchestrator.init_problem(
            problem_id,
            start=start,
            end=end,
            run_dir=run_dir,
            withhold_ground_truth=True,
            rca_solution=rca_solution,
        )
        await orchestrator.start_problem(
            task,
            session,
            max_steps=self.max_steps,
            evaluate=False,
            persist=False,
        )
        return session, task

    async def _evaluate_one(self, item: EvaluationRun) -> dict[str, Any]:
        truth = _truth(item.run_dir / "ground_truth.json")
        start = _timestamp(str(truth["injection_start"]))
        end = _timestamp(str(truth["injection_end"]))
        scenario_id = item.row.get("scenario_id") or str(truth["scenario_id"])
        run_config = self._config_for_run(item.run_dir)
        analysis_session: AgentSession | None = None
        remediation_session: AgentSession | None = None
        analysis_task: Any = None
        remediation_task: Any = None
        try:
            analysis_session, analysis_task = await self._stage(
                run_config,
                f"{scenario_id}-analysis",
                start,
                end,
                item.run_dir,
            )
            analysis_orchestrator = AgentOrchestrator(run_config)
            analysis_evaluated = analysis_orchestrator.finalize_session(
                analysis_task,
                analysis_session,
                expected=truth,
                persist=False,
            )
            rca_solution = (
                analysis_session.solution
                if isinstance(analysis_session.solution, dict)
                else {}
            )
            remediation_session, remediation_task = await self._stage(
                run_config,
                f"{scenario_id}-remediation",
                start,
                end,
                item.run_dir,
                rca_solution=rca_solution,
            )
            remediation_orchestrator = AgentOrchestrator(run_config)
            remediation_evaluated = remediation_orchestrator.finalize_session(
                remediation_task,
                remediation_session,
                expected=truth,
                persist=False,
            )
            self._write_trace(item, "analysis", analysis_evaluated)
            self._write_trace(item, "remediation", remediation_evaluated)
            return _result_from_sessions(
                item,
                truth,
                analysis_session,
                remediation_session,
                remediation_evaluated,
            )
        except Exception as error:
            if analysis_session is not None:
                self._write_trace(
                    item,
                    "analysis",
                    {**analysis_session.to_dict(), "error": f"{type(error).__name__}: {error}"},
                )
            if remediation_session is not None:
                self._write_trace(
                    item,
                    "remediation",
                    {**remediation_session.to_dict(), "error": f"{type(error).__name__}: {error}"},
                )
            return _blank_result(item, f"{type(error).__name__}: {error}")

    def _write_trace(self, item: EvaluationRun, stage: str, payload: dict[str, Any]) -> None:
        trace_dir = self.output_dir / "session-traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        write_json(trace_dir / f"{item.experiment_id}-{stage}.json", payload)

    def evaluate(self, runs: Iterable[EvaluationRun]) -> dict[str, Any]:
        selected = list(runs)
        if self.output_dir.exists() and any(self.output_dir.iterdir()):
            raise FileExistsError(
                f"Phase 05 output directory is not empty: {self.output_dir}"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        rows = [asyncio.run(self._evaluate_one(item)) for item in selected]
        rows.sort(key=lambda row: row["experiment_id"])
        results_csv = self.output_dir / "remediation-results.csv"
        with results_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULT_COLUMNS)
            writer.writeheader()
            writer.writerows(
                {column: row.get(column, "") for column in RESULT_COLUMNS} for row in rows
            )
        review_csv = self.output_dir / "remediation-review.csv"
        write_review_template(results_csv, review_csv)
        runtime = [row for row in rows if row["evaluation_scope"] == "runtime"]
        code_level = [row for row in rows if row["evaluation_scope"] == "code_level"]
        failures = sum(row["evaluation_status"] == "failure" for row in rows)
        summary = {
            "schema_version": 1,
            "task": "analysis_then_remediation",
            "provider": self.provider_name,
            "max_steps_per_stage": self.max_steps,
            "eligible_run_count": len(selected),
            "processed_run_count": len(rows),
            "failed_run_count": failures,
            "all_eligible_runs_processed": len(rows) == len(selected),
            "metrics": _recommendation_metrics(rows),
            "headline_metrics": _recommendation_metrics(runtime),
            "code_level_metrics": _recommendation_metrics(code_level),
            "results_csv": str(results_csv),
            "review_csv": str(review_csv),
            "session_trace_dir": str(self.output_dir / "session-traces"),
        }
        write_json(self.output_dir / "remediation-summary.json", summary)
        write_json(self.output_dir / "remediation-results.json", {"results": rows})
        return summary


def _cli_provider(config: ResearchConfig, provider_name: str | None, smoke: bool) -> LlmProvider:
    if smoke:
        if provider_name and provider_name.lower() != "fake":
            raise ValueError("--smoke requires the fake provider")
        return FakeProvider()
    if provider_name:
        config = replace(config, aiops_llm_provider=provider_name)
    if config.aiops_llm_provider.lower() == "fake":
        raise ValueError("Phase 05 evaluation requires configured LLM provider; use --smoke")
    return build_provider(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Phase 05 remediation recommendations")
    parser.add_argument("--index", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--experiment-id", action="append", dest="experiment_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--provider")
    parser.add_argument("--smoke", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        config = ResearchConfig.from_environment()
        index_path = (arguments.index or config.experiment_index).resolve()
        run_limit = 1 if arguments.smoke and arguments.limit is None else arguments.limit
        evaluator = RemediationBatchEvaluator(
            config,
            provider_factory=lambda run_config: _cli_provider(
                run_config, arguments.provider, arguments.smoke
            ),
            output_dir=arguments.output_dir,
            max_steps=arguments.max_steps,
            provider_name="fake" if arguments.smoke else arguments.provider or config.aiops_llm_provider,
        )
        runs = read_eligible_runs(
            index_path,
            config,
            experiment_ids=set(arguments.experiment_ids or []),
            limit=run_limit,
        )
        if not runs:
            raise ValueError("no eligible runs selected")
        print(json.dumps(evaluator.evaluate(runs), indent=2, sort_keys=True))
        return 0
    except (OSError, ProviderError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
