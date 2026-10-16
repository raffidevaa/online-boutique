# Phase 04 — RCA agent

## Goal

Design and implement the LLM-based agent that performs root cause analysis over an incident
time window, using the observability stack as its data source. The core replay and integrated
live loop is implemented; full multi-scenario evaluation remains pending.

## Prerequisites

- Phase 00 done — Prometheus/Loki reachable.
- Phase 02 in progress or done — at least a few validated ground truth runs to test against.

## Steps

1. **Read-only evidence environment (implemented).** `research/agent/environment.py` reads
   replay artifacts or invokes the central `Observer` configuration for live telemetry. It
   provides compact incident summaries and focused error-log, metrics, logs, traces, and alert
   actions.
2. **Orchestration and provider boundary (implemented).** `python -m research.agent` supports
   replay, live observation, and `--live-incident`; only the provider receives sanitized
   messages, while the agent cannot call Docker or shell actions.
3. **Evidence triage and submission contract (implemented).** Analysis begins with ranked
   baseline-versus-incident anomalies, uses a 15-step budget, and persists submission/termination
   metadata with task-specific RCA scores.
4. **Live lifecycle safety (implemented).** One runtime fault is injected at a time, a telemetry
   warm-up precedes the agent session, and interrupted runs attempt fault stop plus evidence and
   recovery capture.
5. **Validate (in progress).** Continue replay and live tests across the runnable scenario
   matrix; compare Top-1/Top-3 service and fault hypotheses against withheld ground truth.
6. **Confounder awareness.** When testing against `adservice`- or
   `shoppingassistantservice`-related incidents, check whether the agent's hypothesis
   correctly accounts for the known confounder (JVM noise / LLM-generated error text) rather
   than misattributing it as a genuine fault signature.

## Deliverables

- `research/agent/run.py`, `orchestrator.py`, `environment.py`, and task contracts
- Read-only, sanitized provider boundary and persisted agent-session records
- A documented first validation run (match / no-match against ground truth)

## Definition of done

- The agent runs end-to-end against a real incident window and produces a root-cause
  hypothesis. **Met** by the live `FI-RES-MEM-01` pilot: `cartservice` /
  `memory_pressure` matched withheld ground truth.
- At least one validation run's ranked hypothesis is compared against withheld ground truth and
  the result (service/fault match or no-match, and why) is documented. **Met for the pilot**;
  repeated multi-scenario evaluation remains required before this milestone is complete.

## References

- `research/docs/ARCHITECTURE.md` §3 — LLM via API rationale.
- `/AGENTS.md` — "Agent (LLM) layer" section: read-only tool-calling rule, prompts-as-files
  rule, config centralization rule.
