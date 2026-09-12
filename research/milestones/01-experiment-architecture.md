# Phase 01 — Experiment architecture foundation

## Goal

Establish the Docker Compose experiment framework before implementing any fault injection.

## Deliverables

- `ComposeApplication`, static service metadata, and read-only Docker-state snapshots.
- Read-only `Observer` access to Prometheus, Loki, and alerts.
- A minimal tool-neutral fault input plus append-only artifact helpers, with no concrete injector.
- Architecture adaptation documentation and canonical `doctor`/`snapshot` commands.

## Definition of done

- `docker compose config` validates and the stack remains reachable.
- `python -m research.orchestrator doctor` validates the live runtime without changing it.
- A read-only snapshot captures Compose configuration, Docker state, metrics, logs, and alerts.
- No Pumba backend, scenario YAML, or fault command is added or run.
