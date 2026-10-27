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
3. **Catalog the documented set** from `research/docs/FAULT_INJECTION.md`. The Pumba-ready
   subset is CPU hog, memory pressure, network delay, and packet loss; disk/socket scenarios
   remain unavailable until attribution pilots, and code-level scenarios require faulty images:
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
- One versioned scenario YAML per documented case under `research/generators/scenarios/`, with
  a status that prevents unverified cases from running
- At least one validated run directory per implemented category under `research/experiments/runs/`
  (gitignored — don't commit)

## Definition of done

- Running representative scenarios for all 4 runtime fault types produces a correctly shaped
  ground truth log entry (matches the schema exactly).
- The Grafana dashboard visibly reflects each fault's effect during the run.
- `cpu_hog` runs have host-monitoring notes confirming (or flagging) host-wide impact.

Validated on 2026-10-27 with a low-intensity workload and one fault at a time:

- `FI-NET-LOSS-02` (`EXP-20261027T144602728442Z-123171`) captured product-catalog latency
  degradation and 329 target traces without an outage.
- `FI-NET-DELAY-01` (`EXP-20261027T145012506622Z-b4246e`) increased payment span mean from
  about 2.1 ms to 36.3 ms and maximum from about 39.8 ms to 427.7 ms.
- `FI-RES-CPU-01` (`EXP-20261027T145436904344Z-4cdd01`) increased recommendation CPU while
  `host-impact.json` classified host contention as `not_flagged`.
- `FI-RES-MEM-01` (`EXP-20261027T145858386275Z-a2e7fb`) increased cart memory from about
  65 MB to 126 MB and returned to about 59 MB in a non-overlapping recovery window.

Grafana provisioned the Research Overview and Service Detail dashboards, every run wrote the
required structured artifacts, and post-run `doctor` checks returned healthy.

## References

- `research/docs/FAULT_TAXONOMY.md` — fault types, tools, ground truth schema, execution
  rules.
- `/AGENTS.md` — scenario file naming convention, logging requirements, validation rule.
