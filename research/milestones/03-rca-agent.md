# Phase 03 — RCA agent

## Goal

Design and implement the LLM-based agent that performs root cause analysis over an incident
time window, using the observability stack as its data source.

## Prerequisites

- Phase 00 done — Prometheus/Loki reachable.
- Phase 02 in progress or done — at least a few validated ground truth runs to test against.

## Steps

1. **Tool wrappers.** Implement read-only tool-calling wrappers for Prometheus (range
   queries) and Loki (log queries) under `research/agent/tools/`.
2. **Centralize config.** Put all observability endpoints/ports in
   `research/agent/tools/config.py` (or a `.env` file) — never hardcode them in multiple
   places.
3. **System prompt(s).** Write the agent's system prompt(s) as files under
   `research/agent/prompts/` — not as long inline strings in Python code.
4. **Orchestration entrypoint.** Implement `research/agent/run_rca.py`: takes `--start`/
   `--end`, pulls metrics + logs for that window via the tool wrappers, calls the LLM via API
   (see `research/docs/ARCHITECTURE.md` §3 for why API-based, not self-hosted), and outputs a
   root-cause hypothesis.
5. **Enforce read-only by default.** Tool-calling must not be able to execute arbitrary
   commands against containers; any mutating action requires an explicit allowlist (this
   becomes relevant in phase 04).
6. **Validate.** Run the agent against ≥1 previously-validated fault scenario from phases
   01/02 and compare its output against that run's `expected_root_cause`.
7. **Confounder awareness.** When testing against `adservice`- or
   `shoppingassistantservice`-related incidents, check whether the agent's hypothesis
   correctly accounts for the known confounder (JVM noise / LLM-generated error text) rather
   than misattributing it as a genuine fault signature.

## Deliverables

- `research/agent/run_rca.py`
- `research/agent/tools/` (Prometheus/Loki wrappers + `config.py`)
- `research/agent/prompts/` (system prompt files)
- A documented first validation run (match / no-match against ground truth)

## Definition of done

- The agent runs end-to-end against a real incident window and produces a root-cause
  hypothesis.
- At least one validation run's hypothesis is compared against `expected_root_cause` and the
  result (match/no-match, and why) is documented.

## References

- `research/docs/ARCHITECTURE.md` §3 — LLM via API rationale.
- `/AGENTS.md` — "Agent (LLM) layer" section: read-only tool-calling rule, prompts-as-files
  rule, config centralization rule.
