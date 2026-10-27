# Compose AIOps Agent

This package follows the AIOpsLab interaction contract while keeping the research
environment Docker Compose-only and read-only for RCA tasks.

The lifecycle is:

```text
problem registry -> task context/actions -> agent action -> Compose observation
       ^                                                        |
       +---------------- session trace / evaluator <------------+
```

Replay a recorded run:

```bash
python3 -m research.agent \
  --problem-id FI-RES-CPU-02-analysis \
  --run-dir research/experiments/runs/EXP-20260913T133721753961Z-e41c67 \
  --start 2026-09-13T13:37:00Z \
  --end 2026-09-13T13:39:00Z
```

Use live, read-only telemetry explicitly for diagnostics:

```bash
python3 -m research.agent \
  --problem-id FI-RES-CPU-02-analysis \
  --live-observe \
  --start 2026-09-13T13:37:00Z \
  --end 2026-09-13T13:39:00Z
```

Run an integrated incident benchmark (runtime faults only):

```bash
python3 -m research.agent \
  --live-incident \
  --scenario research/generators/scenarios/resource-03-cpu-hog-checkout.yaml \
  --task analysis \
  --execute
```

The default provider is `fake`. For local use, copy the repository-root `.env.example`
to `.env` and fill in the provider settings. `.env` is git-ignored; shell environment
variables take precedence over values in that file. Session JSON is stored under
`research/experiments/agent-sessions/`.
Integrated live runs also create a normal append-only experiment under
`research/experiments/runs/`; the agent itself never receives its ground truth.

## Batch RCA evaluation

Run the Phase 04 replay evaluator against every row marked
`eligible_for_evaluation=true` in `research/experiments/index.csv`:

```bash
python3 -m research.agent.evaluate \
  --output-dir research/experiments/evaluations/phase-04-analysis
```

The headline run uses the provider configured by `AIOPS_LLM_PROVIDER` and the other
`AIOPS_LLM_*` values in the repository-root `.env`. A fake-provider smoke replay is
deliberately limited to one run:

```bash
python3 -m research.agent.evaluate \
  --smoke --experiment-id EXP-... \
  --output-dir /tmp/phase-04-smoke
```

Each selected run produces one row in `evaluation-results.csv`; failures are retained as
explicit rows. `evaluation-summary.json` reports valid submission rate, service Top-1 and
Top-3, category, normalized fault type, combined RCA accuracy, mean TTA, and separate clean
versus confounder-affected metrics. Session traces are written below `session-traces/`,
outside the experiment run directories. Ground truth is handed to the task only after the
agent action loop completes.
