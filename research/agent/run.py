"""CLI for replay-first AIOpsLab-compatible sessions."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

from research.utils import ResearchConfig

from research.generators import ScenarioError, load_scenario

from .live import LiveIncidentController, LiveIncidentError
from .orchestrator import AgentRuntimeError, create_orchestrator


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Expected ISO-8601 timestamp") from error
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an AIOpsLab-compatible Compose agent")
    parser.add_argument("--problem-id")
    parser.add_argument("--start", type=_timestamp)
    parser.add_argument("--end", type=_timestamp)
    parser.add_argument("--run-dir", type=Path, help="Replay a recorded experiment")
    parser.add_argument(
        "--live-observe",
        action="store_true",
        help="Use live read-only telemetry for the supplied time window",
    )
    parser.add_argument("--live-incident", action="store_true")
    parser.add_argument("--scenario", type=Path, help="Scenario YAML for integrated live")
    parser.add_argument("--task", choices=("detection", "localization", "analysis"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-steps", type=int, default=30)
    arguments = parser.parse_args()
    modes = int(arguments.run_dir is not None) + int(arguments.live_observe) + int(arguments.live_incident)
    if modes != 1:
        parser.error("Specify exactly one of --run-dir, --live-observe, or --live-incident")
    try:
        config = ResearchConfig.from_environment()
        orchestrator = create_orchestrator(config)
        if arguments.live_incident:
            if arguments.scenario is None or arguments.task is None:
                parser.error("--live-incident requires --scenario and --task")
            scenario = load_scenario(arguments.scenario)
            result = asyncio.run(
                LiveIncidentController(config, orchestrator).run(
                    scenario,
                    arguments.task,
                    max_steps=arguments.max_steps,
                    acknowledge_execute=arguments.execute,
                )
            )
        else:
            if not arguments.problem_id or arguments.start is None or arguments.end is None:
                parser.error("Replay and live-observe require --problem-id, --start, and --end")
            _, task, session = orchestrator.init_problem(
                arguments.problem_id,
                start=arguments.start,
                end=arguments.end,
                run_dir=arguments.run_dir,
            )
            result = asyncio.run(
                orchestrator.start_problem(task, session, max_steps=arguments.max_steps)
            )
    except (AgentRuntimeError, LiveIncidentError, ScenarioError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
