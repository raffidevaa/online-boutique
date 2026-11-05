# Compose AIOps agent

The current agent supports detection, localization, analysis, and live mitigation on
Docker Compose. RCA actions read telemetry. Live mitigation can restore only the active
problem's target service; it stops the running fault before recreating that service.
The separate `remediation` task produces a recommendation without execution.

Run a controlled live problem after checking the scenario and workload:

```bash
source .venv/bin/activate
python -m research.agent \
  --live-incident \
  --scenario research/generators/scenarios/network-05-loss-productcatalog.yaml \
  --task analysis \
  --execute
```

Choose `full` to run detection, localization, analysis, recommendation, and mitigation
against one fault. Individual tasks remain available for diagnostic and benchmark runs.
The default provider is deterministic `fake`; configure an API provider through the
repository-root `.env` or environment variables for an LLM run. Every live run writes
an append-only experiment under `research/experiments/runs/`.

Replay an existing analysis run with its own timestamps:

```bash
python -m research.agent \
  --problem-id FI-NET-LOSS-02-analysis \
  --run-dir research/experiments/runs/<experiment-id> \
  --start <ISO8601-injection-start> \
  --end <ISO8601-injection-end>
```

Replay cannot execute `mitigation`; it requires a live fault and an allowlisted
Compose action adapter. Use `python -m research.agent --help` for other modes.
The old phase-specific evaluation commands and examples are archived in
[`old/README.md`](old/README.md). See the [refactor record](../docs/AIOPSLAB_REFACTOR_PLAN.md)
for validation status and known limits.

## Action and context strategy

`AIOPS_STRATEGY=enabled` is the default. Set `AIOPS_STRATEGY=baseline` for a
control run with the same provider, temperature, replay artifact, task, and action
sequence. The switch controls cumulative budgets, action validation, replay/live
query caching, progress stopping, and context compaction. Evidence summaries and
IDs are shared by both modes, so this switch isolates controller behavior rather
than recreating the older raw-output implementation.

Optional environment variables (also accepted from the repository-root `.env`):

| Variable | Default | Meaning |
|---|---:|---|
| `AIOPS_INCIDENT_MAX_STEPS` | 35 | Cumulative model calls across a full incident. |
| `AIOPS_MAX_INPUT_TOKENS` | 120000 | Estimated or reported incident input-token ceiling. |
| `AIOPS_MAX_OUTPUT_TOKENS` | 12000 | Estimated or reported incident output-token ceiling. |
| `AIOPS_FINAL_RESERVE_TOKENS` | 800 | Capacity reserved before another model call. |
| `AIOPS_MAX_COST_USD` | unset | Optional cost ceiling; requires both model rates. |
| `AIOPS_INPUT_USD_PER_MILLION` | unset | Input model rate. |
| `AIOPS_OUTPUT_USD_PER_MILLION` | unset | Output model rate. |
| `AIOPS_MAX_STAGE_SECONDS` | 300 | Stage wall-time ceiling. |
| `AIOPS_TOOL_TIMEOUT_SECONDS` | 20 | Timeout per focused backend request. |
| `AIOPS_CONTEXT_COMPACT_TOKENS` | 12000 | Estimated context threshold for deterministic compaction. |
| `AIOPS_LIVE_CACHE_TTL_SECONDS` | 15 | Maximum age for a repeated live query. |
| `AIOPS_STAGNANT_LIMIT` | 4 | Consecutive turns without new evidence or a hypothesis update. |

Each session records `metrics`, structured `notes`, and, if no valid submission was
made, a `partial_result`. Provider usage is tagged `actual` only when the endpoint
reports both token counts; otherwise it is `estimated`. Cost stays null until
both rates are configured. A partial result does not satisfy the evaluator's
submission contract. `get_evidence_detail` accepts IDs from the current or
earlier stages of the same live incident; detail includes the observation time.
Incident summaries show key signals, with complete signals available in detail.
`update_hypothesis` records agent-owned hypotheses and
supporting or contradicting evidence IDs; it cannot access ground truth. Live
handoff marks prior evidence as stale using its age, so the next stage can refresh
the relevant signal.

`actual_queries` counts executed read-only **agent actions**. A summary action can
issue several backend HTTP requests, so this metric is not an HTTP request count.
