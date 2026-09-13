"""Read-only architecture diagnostics CLI."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

from research.generators import ScenarioError, load_scenario
from research.observer import Observer, ObserverError
from research.orchestrator.faults import FaultExecutionError, describe_scenario, run_fault
from research.service import ComposeApplication, ServiceError
from research.utils import ResearchConfig, write_json


def doctor(config: ResearchConfig) -> dict[str, object]:
    application = ComposeApplication(config)
    observer = Observer(config)
    application.validate()
    states = application.validate_readiness()
    observer.check_ready()
    return {
        "status": "healthy",
        "services": {name: state.status for name, state in states.items()},
        "prometheus_url": config.prometheus_url,
        "loki_url": config.loki_url,
    }


def snapshot(config: ResearchConfig, output: Path) -> dict[str, object]:
    application = ComposeApplication(config)
    observer = Observer(config)
    application.validate_readiness()
    observer.check_ready()
    output.mkdir(parents=True, exist_ok=False)
    docker_state = application.snapshot()
    write_json(output / "docker_state.json", docker_state)
    (output / "compose-resolved.yaml").write_text(
        application.resolved_compose_yaml(), encoding="utf-8"
    )
    observer.capture_recent(output / "telemetry")
    metadata = {
        "schema_version": 1,
        "kind": "architecture_snapshot",
        "timestamp": datetime.now(UTC).isoformat(),
        "read_only": True,
    }
    write_json(output / "metadata.json", metadata)
    return {"status": "captured", "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Research architecture diagnostics")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="Validate Compose, runtime state, and telemetry")
    snapshot_parser = subcommands.add_parser(
        "snapshot", help="Capture a read-only Docker and telemetry snapshot"
    )
    snapshot_parser.add_argument("--output", required=True, type=Path)
    fault_parser = subcommands.add_parser("fault", help="Plan or execute one controlled fault")
    fault_commands = fault_parser.add_subparsers(dest="fault_command", required=True)
    fault_plan_parser = fault_commands.add_parser(
        "plan", help="Validate and print a non-mutating fault plan"
    )
    fault_plan_parser.add_argument("--scenario", required=True, type=Path)
    fault_run_parser = fault_commands.add_parser(
        "run", help="Execute one ready scenario after runtime preflight"
    )
    fault_run_parser.add_argument("--scenario", required=True, type=Path)
    fault_run_parser.add_argument(
        "--execute", action="store_true", help="Required acknowledgement for fault injection"
    )
    arguments = parser.parse_args()
    config = ResearchConfig.from_environment()
    try:
        if arguments.command == "doctor":
            result = doctor(config)
        elif arguments.command == "snapshot":
            result = snapshot(config, arguments.output)
        else:
            scenario = load_scenario(arguments.scenario)
            if arguments.fault_command == "plan":
                result = describe_scenario(scenario, config)
            elif not arguments.execute:
                raise FaultExecutionError("Refusing fault injection without --execute")
            else:
                result = run_fault(scenario, config)
    except (FaultExecutionError, ScenarioError, ServiceError, ObserverError, FileExistsError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
