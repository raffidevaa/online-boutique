# Diagram catalog

Use the smallest diagram that materially improves understanding. Keep diagrams aligned
with the current Compose-based research architecture and label proposed elements as
future or deferred.

## System topology

Use for questions about component ownership and runtime boundaries. Show:

- Compose application services and key dependencies;
- Prometheus, Loki, Jaeger/OTel, Grafana, and Docker-state observation;
- orchestrator, generators, artifacts, agent, and evaluator;
- arrows for telemetry, control, and evidence with a legend.

## Experiment lifecycle

Use for fault execution or ground-truth questions. Show:

`baseline -> preflight -> one fault injection -> incident observation -> cleanup/recovery -> RCA -> remediation recommendation -> evaluation`.

Mark the explicit execution gate, baseline failure stop, append-only artifacts, and the
separation between agent-visible telemetry and withheld ground truth.

## Observability-to-RCA flow

Use for agent and tool-calling questions. Show read-only queries from the agent to metrics,
logs, traces, alerts, service metadata, and approved runbooks. Do not show scenario ID,
ground truth, injector command, or injector output entering the agent context.

## Fault propagation graph

Use for a specific scenario. Show one target fault, local symptoms, dependent-service or
frontend symptoms, telemetry evidence, and recovery. Do not imply that a propagated
symptom is the root cause. Mark `adservice` and `shoppingassistantservice` confounders
when relevant.

## Remediation boundary

Use when discussing automated remediation. Separate recommendation-only behavior from a
future execution path. The execution path must show an explicit approval gate, an action
allowlist, reversible Compose-scoped actions, audit evidence, and a failure/rollback path.

## Diagram conventions

- Use `flowchart TD` unless another orientation is clearer.
- Use stable lowercase service names and taxonomy identifiers in node IDs.
- Use dashed edges for future/deferred behavior and solid edges for implemented behavior.
- Label read-only edges `read-only` and mutation edges `explicit gate + allowlist`.
- Include a short legend below every diagram.
- Never include credentials, raw logs, PII, or experiment secrets in diagram labels.
