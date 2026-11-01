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

Choose `detection`, `localization`, `analysis`, or `mitigation` for `--task`.
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
