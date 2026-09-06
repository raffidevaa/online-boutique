# Phase 01 — Fault injection pipeline

## Goal

Build and validate the fault injection pipeline: a scenario runner that drives Pumba and a
ground truth logger that records every execution in the required schema.

## Prerequisites

- Phase 00 done — environment is up and healthy, Grafana/Prometheus reachable (needed to
  confirm steady-state before/after each injected fault).

## Steps

1. **Ground truth logger.** Implement a small module that writes the required JSON schema
   (see `research/docs/FAULT_TAXONOMY.md` → "Ground truth logging schema") to
   `research/fault-injection/logs/` for every fault execution — structured output
   (JSON/CSV), never just stdout, per `AGENTS.md`.
2. **Scenario runner.** Implement `research/fault-injection/run_scenario.py`: reads a
   scenario file (`research/fault-injection/scenarios/<category>-<number>.yaml`), invokes the
   corresponding Pumba command, records `timestamp_start`/`timestamp_end`, and calls the
   ground truth logger.
3. **Author first scenarios**, one per fault category in `FAULT_TAXONOMY.md`:
   - Resource-level: `cpu_stress` or `memory_stress`
   - Network-level: `network_delay` or `packet_loss`
   - Container-level: `container_kill` or `container_pause`
   - Dependency-level: `timeout_injection` (via `checkoutservice`'s downstream calls)
4. **Validate against a non-critical service first** (`productcatalogservice`), per
   `AGENTS.md`'s validation rule — confirm the ground truth log is generated correctly before
   testing against anything else.
5. **Confounder check.** For `cpu_stress` scenarios, run with host-level monitoring
   (`htop`/`free -h`) active and note in the log/notes whether effects extended beyond the
   target container.
6. **Enforce one-fault-at-a-time.** Do not build a code path that fires faults at multiple
   services concurrently unless explicitly implementing a documented cascading scenario.

## Deliverables

- `research/fault-injection/run_scenario.py` + ground truth logger module
- One scenario YAML per fault category under `research/fault-injection/scenarios/`
- At least one validated log entry per category under `research/fault-injection/logs/`
  (gitignored — don't commit)

## Definition of done

- Running the scenario runner against each of the 4 authored scenarios produces a correctly
  shaped ground truth log entry (matches the schema exactly).
- The Grafana dashboard visibly reflects each fault's effect during the run.
- `cpu_stress` runs have host-monitoring notes confirming (or flagging) host-wide impact.

## References

- `research/docs/FAULT_TAXONOMY.md` — fault types, tools, ground truth schema, execution
  rules.
- `/AGENTS.md` — scenario file naming convention, logging requirements, validation rule.
