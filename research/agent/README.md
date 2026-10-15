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
