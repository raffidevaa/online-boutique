# Fault taxonomy

This is the compact index for the detailed methodology and scenario catalog in
[`FAULT_INJECTION.md`](FAULT_INJECTION.md). The primary taxonomy follows RCAEval’s three
categories; this project selects a smaller Docker Compose-compatible core set.

## Taxonomy and status

| Category | Normalized fault type | Status | Primary evidence |
|---|---|---|---|
| Resource | `cpu_hog` | Core | CPU, latency, traces |
| Resource | `memory_pressure` | Core | Working set, OOM/swap, latency |
| Resource | `disk_stress` | Candidate | I/O wait and storage latency |
| Resource | `socket_stress` | Candidate | Connections, descriptors, resets |
| Network | `network_delay` | Core | RPC latency, traces, deadlines |
| Network | `packet_loss` | Core | RPC errors, retries, traces |
| Code-level | `incorrect_return_value` | Core/deferred | Application semantics and traces |
| Code-level | `missing_exception_handler` | Core/deferred | Stack traces, failed parent spans |
| Code-level | `incorrect_parameter` | Candidate/deferred | Validation/downstream errors |
| Code-level | `missing_parameter` | Candidate/deferred | Invalid argument errors |
| Code-level | `missing_function_call` | Candidate/deferred | Missing interaction or state change |

Core/deferred code-level faults require reproducible faulty-image variants and explicit
approval before modifying or instrumenting upstream service source.

## Delivery mechanisms, not categories

Pumba, Linux `tc`, `stress-ng`, Docker lifecycle operations, Toxiproxy, and faulty image
variants describe how a fault may be introduced. They must not be used as the RCA taxonomy
or revealed to the RCA agent. Container stop/kill/pause and dependency unavailability are
implementation techniques or propagated conditions, not additional headline categories.

## Research constraints

- One primary fault and one primary target per experiment; cascading cases require separate
  documentation and explicit approval.
- Every case records explicit intensity, duration, workload, timestamps, expected local and
  propagated symptoms, telemetry evidence, ground truth, and recovery behavior.
- `adservice` JVM startup/GC noise and `shoppingassistantservice` LLM-generated fallback text
  are known confounders and must be flagged in experiment metadata.
- CPU and memory cases require host-level monitoring on the constrained 2-core host.
- Network cases must account for gRPC symptoms such as `DEADLINE_EXCEEDED` and `UNAVAILABLE`.
- Kubernetes-only node, scheduling, admission, storage, and control-plane faults are out of
  scope. Docker Compose remains the only runtime.

## Ground truth minimum

The future structured ground truth must remain separate from agent input and contain:

```yaml
root_cause_service: paymentservice
fault_category: network
fault_type: network_delay
injection_start: 2026-09-12T10:00:00Z
injection_end: 2026-09-12T10:02:00Z
severity: medium
```

Full run metadata, parameters, telemetry windows, cleanup state, and remediation families
belong in the append-only experiment directory under `research/experiments/runs/`.

## Authority and implementation status

RCAEval supplies the taxonomy and evaluation perspective. The scenario catalog supplies
thesis-specific targets, workloads, parameter bands, and propagation hypotheses. The catalog
now has 15 versioned scenario files and an allowlisted Pumba planner: eight CPU, memory, and
network cases are `ready_for_pilot`; `disk_stress` and `socket_stress` are
`experimental_unavailable`; the five code-level cases are `deferred_faulty_image`. None of
these labels means a live experiment has been validated.
