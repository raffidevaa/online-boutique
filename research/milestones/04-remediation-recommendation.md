# Phase 05 — Remediation recommendation

## Goal

Extend the RCA agent to also recommend a remediation action for the root cause it
identifies, appropriate for a Docker Compose (non-Kubernetes) environment. The implemented
scope is recommendation-only; runtime execution remains out of scope.

## Prerequisites

- Phase 03 done — RCA agent produces validated root-cause hypotheses.

## Steps

1. **Define a remediation action taxonomy** scoped to what's actually actionable under
   Compose (no k8s autoscaling/rollouts available): e.g. restart a container, revert a
   config/env change, flag for manual resource-limit increase, flag for manual scale-up.
2. **Extend agent output (implemented).** A second `remediation` task receives the agent's own
   RCA result and emits a structured recommendation with evidence, risk, rollback, approval,
   and execution-safety fields.
3. **Gate any execution path explicitly.** If/when actual remediation execution is added
   (not just recommendation), it must be behind an explicit flag and an allowlist of safe
   actions — per `AGENTS.md`, the agent must not execute arbitrary commands against
   containers.
4. **Define a review rubric (implemented).** Since there's no automatic ground truth for "the correct
   remediation," define a simple manual rubric (e.g. 1–5: is it safe / is it relevant / would
   it actually resolve the fault) to judge recommendation quality consistently.
5. **Validate (done).** Replay all 18 eligible Phase 04 scenarios and score each recommendation
   with the rubric. Runtime and code-level scopes remain separately reported.

## Deliverables

- `research/agent/remediation_evaluator.py` emitting an RCA + remediation pair
- [`research/docs/REMEDIATION_RECOMMENDATION.md`](../docs/REMEDIATION_RECOMMATION.md)
  documenting the action taxonomy and safety boundary
- A review rubric and scored results in
  `research/experiments/evaluations/phase-05-remediation-v1/`

## Definition of done

- The agent emits a valid remediation recommendation for all 18 eligible Phase 04 scenarios.
- Each recommendation has a rubric score and a one-line justification recorded.
- Every recommendation has `requires_approval=true` and `execution_allowed=false`.

## References

- `/AGENTS.md` — "Agent (LLM) layer" section: remediation must stay read-only/recommend-only
  unless explicitly enabled, allowlist requirement.
- `research/docs/ARCHITECTURE.md` — Compose environment constraints (no k8s-native
  remediation actions available).
