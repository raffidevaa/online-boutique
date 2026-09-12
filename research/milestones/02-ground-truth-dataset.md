# Phase 03 — Ground truth dataset collection

## Goal

Systematically run fault scenarios across services to build the ground truth dataset the RCA
agent will later be evaluated against.

## Prerequisites

- Phase 02 done — fault injector and ground-truth artifacts validated on at least one service.

## Steps

1. **Define the experiment matrix.** Start with the six-case core catalog in
   `FAULT_INJECTION.md`, varying fault type × target × severity × repetition. Exclude
   multi-service parallel faults unless explicitly designing a separately-documented
   cascading scenario.
2. **Sequence runs with recovery gaps.** Between executions, wait for the system to return to
   a steady-state baseline — verify via the Grafana dashboard before starting the next run.
3. **Log every experiment** in the structured run directory
   `research/experiments/runs/<experiment_id>/`; use `research/docs/EXPERIMENT_LOG.md` only
   for supplementary research notes.
4. **Build a dataset index** (e.g. `research/experiments/index.csv` or similar) summarizing
   every run: experiment_id, fault_type, target_service, whether it's confounder-affected.
5. **Flag confounder-affected runs.** Any run touching `adservice` (JVM noise) or
   `shoppingassistantservice` (LLM-generated error text) gets flagged in the index so later
   evaluation can report them separately.
6. **Check coverage.** Expand beyond the core catalog only when pilot runs show distinct RCA
   value and safe reproducibility, as defined in `FAULT_INJECTION.md`.

## Deliverables

- `research/experiments/runs/<experiment_id>/` — one structured directory per run
- A dataset index summarizing all runs and their confounder status

## Definition of done

- Every target service has ≥1 logged run per applicable fault category.
- The dataset index accurately reflects every file under `research/experiments/`.
- Confounder-affected runs are clearly flagged and distinguishable from clean runs.

## References

- `research/docs/EXPERIMENT_LOG.md` — experiment log template.
- `research/docs/FAULT_TAXONOMY.md` — target services, execution rules, confounder notes.
- `/AGENTS.md` — `research/experiments/` is append-only; don't overwrite without
  confirmation; don't commit large raw datasets.
