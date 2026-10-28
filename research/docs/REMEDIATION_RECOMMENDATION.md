# Phase 05 — Remediation recommendation

Phase 05 adds a second, recommendation-only stage after RCA. The agent may inspect the
same read-only telemetry, but it has no Docker, shell, Compose, Pumba, or arbitrary command
action.

## Action taxonomy

| Action type | Intended use | Default risk |
|---|---|---:|
| `restart_service` | Contain a transient service failure after evidence is collected. | medium |
| `rollback_configuration` | Revert a known configuration or environment change. | medium |
| `adjust_resource_limit` | Propose a manual Compose resource-limit review. | medium |
| `network_configuration_change` | Propose timeout, retry, connectivity, or network-path review. | medium |
| `deploy_corrected_image` | Replace a known faulty code-level image variant. | high |
| `manual_investigation` | Request more evidence when the causal mechanism is uncertain. | low |
| `escalate` | Hand off when the proposed change is unsafe or outside the allowlist. | low |

The taxonomy describes a recommendation only. It is not an execution API.

## Submission contract

```json
{
  "action_type": "restart_service",
  "target_service": "cartservice",
  "rationale": "Contain the suspected memory pressure while investigating.",
  "evidence": ["The cart working set increased during the incident."],
  "expected_effect": "Restore service responsiveness temporarily.",
  "risk": "medium",
  "rollback_plan": "Require human approval and verify recovery before any change.",
  "requires_approval": true,
  "execution_allowed": false
}
```

The validator requires a targetable application service, except that `unknown` is allowed
for `manual_investigation` and `escalate`. It rejects arbitrary action names, empty evidence,
missing rollback plans, approval bypasses, and any `execution_allowed=true` value.

## Review rubric

Each recommendation is manually scored from 1–5 for:

1. correctness for the known fault;
2. relevance to the causal mechanism;
3. safety under Docker Compose constraints;
4. actionability and reversibility.

The reviewer must provide one-line justification. The evaluator writes a pending
`remediation-review.csv` template beside the recommendation results; the review utility
validates completed rows and aggregates the four mean scores.

## Evaluation

Run a fake-provider smoke test:

```bash
python3 -m research.agent.remediation_evaluate \
  --smoke --experiment-id EXP-... \
  --output-dir /tmp/phase-05-smoke
```

Run the configured provider across eligible runs:

```bash
python3 -m research.agent.remediation_evaluate \
  --output-dir research/experiments/evaluations/phase-05-remediation-v1
```

The evaluator runs `analysis` and `remediation` sequentially, stores separate session
traces, withholds ground truth until both action loops finish, and reports runtime and
code-level scopes separately.
