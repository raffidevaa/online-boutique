# Phase 04 — Remediation recommendation

## Goal

Extend the RCA agent to also recommend a remediation action for the root cause it
identifies, appropriate for a Docker Compose (non-Kubernetes) environment.

## Prerequisites

- Phase 03 done — RCA agent produces validated root-cause hypotheses.

## Steps

1. **Define a remediation action taxonomy** scoped to what's actually actionable under
   Compose (no k8s autoscaling/rollouts available): e.g. restart a container, revert a
   config/env change, flag for manual resource-limit increase, flag for manual scale-up.
2. **Extend agent output** to include a remediation recommendation alongside the root-cause
   hypothesis — recommendation-only by default.
3. **Gate any execution path explicitly.** If/when actual remediation execution is added
   (not just recommendation), it must be behind an explicit flag and an allowlist of safe
   actions — per `AGENTS.md`, the agent must not execute arbitrary commands against
   containers.
4. **Define a review rubric.** Since there's no automatic ground truth for "the correct
   remediation," define a simple manual rubric (e.g. 1–5: is it safe / is it relevant / would
   it actually resolve the fault) to judge recommendation quality consistently.
5. **Validate** against the same scenarios used in phase 03, scoring each recommendation with
   the rubric.

## Deliverables

- Extended `research/agent/run_rca.py` (or a new module) emitting an RCA + remediation pair
- A documented remediation action taxonomy
- A review rubric + scored results for the validated scenarios

## Definition of done

- The agent emits a remediation recommendation for every scenario it was validated against
  in phase 03.
- Each recommendation has a rubric score and a one-line justification recorded.

## References

- `/AGENTS.md` — "Agent (LLM) layer" section: remediation must stay read-only/recommend-only
  unless explicitly enabled, allowlist requirement.
- `research/docs/ARCHITECTURE.md` — Compose environment constraints (no k8s-native
  remediation actions available).
