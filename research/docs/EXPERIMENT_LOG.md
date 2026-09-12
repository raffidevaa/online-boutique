# Experiment log template

Use this Markdown entry as supplementary commentary for a structured run under
`research/experiments/runs/<experiment_id>/`. The JSON artifacts are authoritative for
ground truth, timestamps, telemetry, and evaluation.

## Experiment identity

- Experiment ID:
- Scenario ID / variant:
- Date (UTC):
- Objective:
- Repository commit/configuration hash:

## Environment and workload

- Compose file/version:
- Active services:
- Workload profile, users, and request rate:
- Baseline window:
- Host CPU/RAM headroom before injection:
- Confounder flags (`adservice`, `shoppingassistantservice`, host contention):

## Fault definition

| Field | Value |
|---|---|
| Category | |
| Fault type | |
| Target service | |
| Severity | |
| Parameters/intensity | |
| Injection start (UTC) | |
| Injection end (UTC) | |

## Expected versus observed evidence

### Local symptoms

- Expected:
- Observed:

### Propagated symptoms

- Expected:
- Observed:

### Telemetry

- Metrics:
- Logs:
- Alerts:
- Traces (available/unavailable and why):
- Docker/runtime state:

## Recovery and data quality

- Fault cleanup result:
- Recovery verification and time:
- Artifact completeness:
- Any host-wide impact or other confounder:
- Notes/limitations:

## Agent result

| Rank | Candidate service | Candidate fault | Confidence | Evidence summary |
|---:|---|---|---:|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |

- Root-cause service correct (Top-1 / Top-3):
- Fault type correct:
- Combined RCA correct:
- Detection/localization time:
- Was ground truth withheld from the agent?:

## Remediation recommendation

- Recommendation:
- Correctness score (1–5):
- Relevance score (1–5):
- Safety score (1–5):
- Actionability score (1–5):
- Justification:

## Follow-up actions

-
