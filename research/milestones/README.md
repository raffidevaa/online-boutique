# Milestones

Detailed, phase-by-phase development roadmap for the thesis project. This expands the
high-level checklist in [`../README.md`](../README.md) into concrete, actionable steps.

Each phase file uses the same structure: **Goal**, **Prerequisites**, **Steps**,
**Deliverables**, **Definition of done**, **References**. Work through phases in order —
each one depends on the previous phase's deliverables.

| # | Phase | Status |
|---|---|---|
| 00 | [Environment setup](00-environment-setup.md) | Done |
| 01 | [Fault injection pipeline](01-fault-injection-pipeline.md) | Not started |
| 02 | [Ground truth dataset collection](02-ground-truth-dataset.md) | Not started |
| 03 | [RCA agent](03-rca-agent.md) | Not started |
| 04 | [Remediation recommendation](04-remediation-recommendation.md) | Not started |
| 05 | [Evaluation](05-evaluation.md) | Not started |

Update the **Status** column as work progresses (`Not started` / `In progress` / `Done`) —
this is the single place tracking overall thesis progress; don't duplicate it elsewhere.

## How to use this folder

- Before starting a phase, re-read its **Prerequisites** — most reference a deliverable from
  the previous phase.
- Steps are checklists, not a rigid script — adapt as needed, but don't skip the
  **Definition of done** criteria; they exist to keep the research methodologically sound
  (e.g. not accepting an RCA result without checking it against known confounders).
- Cross-cutting rules (hardware limits, JVM/LLM confounder handling, one-fault-at-a-time,
  append-only experiment data) live in [`/AGENTS.md`](../../AGENTS.md) and
  [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) — every phase assumes those rules
  apply.
