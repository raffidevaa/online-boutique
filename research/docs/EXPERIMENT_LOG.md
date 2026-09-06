# Experiment log

Template for recording each fault injection + agent evaluation session. Duplicate the
section below for each new experiment, or save as a separate entry under
`research/experiments/<experiment_id>.md`.

---

## Experiment: `<experiment_id>`

**Date:** yyyy-mm-dd
**Goal:** (e.g. validate end-to-end pipeline / collect ground truth dataset / evaluate RCA
agent accuracy)

### Environment conditions

- Compose stack version: (commit hash / tag)
- Active services: (list, or "all per `research/compose/docker-compose.yml`")
- Load generator: on/off, what intensity
- Host CPU/RAM headroom before starting (from `htop`/`free -h`)

### Fault scenarios executed

| experiment_id | fault_type | target_service | start time | end time | notes |
|---|---|---|---|---|---|
| | | | | | |

### Observability results

- Were the fault's effects captured as expected in metrics/logs/(traces)?
- Any anomalies/confounders observed (e.g. host CPU spiking beyond the intended target,
  `adservice` JVM noise, `shoppingassistantservice` LLM-generated error text)?

### Agent results (if this experiment evaluates the agent)

| experiment_id | agent's root cause | ground truth root cause | match? | detection time | remediation recommendation |
|---|---|---|---|---|---|
| | | | | | |

### Notes / limitations for this run

-

### Follow-up actions

-
