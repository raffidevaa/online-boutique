# Phase 02 — Fault injection pipeline

## Goal

Build and validate the fault injection pipeline: a Pumba backend in `research/generators/`
and structured experiment artifacts.

## Prerequisites

- Phase 00 done — environment is up and healthy, Grafana/Prometheus reachable (needed to
  confirm steady-state before/after each injected fault).

## Steps

1. **Ground-truth artifacts.** Extend the artifact helpers so every valid execution writes
   `metadata.json`, `ground_truth.json`, injector details, and snapshots under
   `research/experiments/runs/<experiment_id>/`.
2. **Pumba adapter.** Implement it under `research/generators/`; scenarios belong under
   `research/generators/scenarios/` and must use the RCAEval-aligned identifiers in
   `research/docs/FAULT_TAXONOMY.md`, not raw commands.
3. **Author the initial core set** from `research/docs/FAULT_INJECTION.md`:
   - Resource: `cpu_hog`, `memory_pressure`
   - Network: `network_delay`, `packet_loss`
   - Code-level (deferred): `incorrect_return_value`, `missing_exception_handler`
4. **Validate against a non-critical service first** (`productcatalogservice`), per
   `AGENTS.md`'s validation rule — confirm the ground truth log is generated correctly before
   testing against anything else.
5. **Confounder check.** For `cpu_hog` scenarios, run with host-level monitoring
   (`htop`/`free -h`) active and note in the log/notes whether effects extended beyond the
   target container.
6. **Enforce one-fault-at-a-time.** Do not build a code path that fires faults at multiple
   services concurrently unless explicitly implementing a documented cascading scenario.

## Deliverables

- Pumba backend and scenario parser under `research/generators/`
- One scenario YAML per implemented category under `research/generators/scenarios/`
- At least one validated run directory per implemented category under `research/experiments/runs/`
  (gitignored — don't commit)

## Definition of done

- Running the scenario runner against each of the 4 authored scenarios produces a correctly
  shaped ground truth log entry (matches the schema exactly).
- The Grafana dashboard visibly reflects each fault's effect during the run.
- `cpu_hog` runs have host-monitoring notes confirming (or flagging) host-wide impact.

## References

- `research/docs/FAULT_TAXONOMY.md` — fault types, tools, ground truth schema, execution
  rules.
- `/AGENTS.md` — scenario file naming convention, logging requirements, validation rule.
