# Phase 06 — Evaluation

## Goal

Quantitatively evaluate the RCA agent (and its remediation recommendations) across the full
ground truth dataset.

## Prerequisites

- Phase 03 done — full ground truth dataset collected.
- Phase 04 done — agent produces RCA + remediation pairs.

## Steps

1. **Define metrics:**
   - RCA accuracy — service, fault-type, combined, and Top-3 match rates against withheld ground truth.
   - MTTD (mean time to detection) — time from fault start to agent's hypothesis.
   - MTTR (mean time to remediation) — time from fault start to a remediation recommendation
     (or to actual recovery, if phase 04's execution path is enabled).
2. **Build an evaluation script** that reads the phase-03 dataset index +
   `research/experiments/runs/*`, runs the agent against each recorded incident window, and
   computes the metrics above.
3. **Run the full evaluation** across the dataset.
4. **Separate confounder-affected runs.** Report `adservice`/`shoppingassistantservice`
   -flagged runs separately from the clean set, per the flags set in phase 03 — don't let
   known confounders silently skew the headline accuracy number.
5. **Write up results** — a summary of accuracy/MTTD/MTTR (clean vs. confounder-affected),
   to feed into the thesis writeup (outside this repo's scope beyond this summary).

## Deliverables

- Evaluation script
- A results summary (metrics, clean vs. confounder-affected breakdown)

## Definition of done

- Metrics computed across the entire phase-03 dataset.
- Results summary clearly separates confounder-affected runs from clean runs.

## References

- `research/docs/EXPERIMENT_LOG.md` — per-run agent-results table this evaluation aggregates.
- `research/docs/ARCHITECTURE.md` — "Research limitations" section (confounders to separate
  out).
- `../README.md` — "Research status" (MTTD/MTTR/accuracy are the metrics named there).
