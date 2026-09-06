# Fault taxonomy

List of valid `fault_type` values for use in scenarios (`research/fault-injection/scenarios/*.yaml`)
and the ground truth log. Scoped to what can be simulated with Docker Compose + Pumba (see
`ARCHITECTURE.md` for why node-level/kernel-level faults are out of scope).

Target services (Online Boutique, current `src/` tree): `frontend`, `cartservice`,
`productcatalogservice`, `currencyservice`, `paymentservice`, `shippingservice`,
`emailservice`, `checkoutservice`, `recommendationservice`, `adservice`,
`shoppingassistantservice`, `loadgenerator`, plus `redis-cart` (the cache backing
`cartservice`; not under `src/`, but a running container and a valid fault target).

## 1. Resource-level

| fault_type | Description | Tool |
|---|---|---|
| `cpu_stress` | Load the target container's CPU | `pumba stress --stress-cpu` |
| `memory_stress` | Load/exhaust the target container's memory | `pumba stress --stress-memory` |
| `disk_io_stress` | Load disk read/write | `pumba stress --stress-io` (via stress-ng) |

**Note:** use `cpu_stress` sparingly on this host. Given the 2-core/4-thread CPU, always run
with host-level monitoring (`htop`/`free -h`) active to detect whether effects extend beyond
the target container.

## 2. Network-level

| fault_type | Description | Tool |
|---|---|---|
| `network_delay` | Add latency to inter-service communication | `pumba netem delay` |
| `packet_loss` | Drop a portion of packets | `pumba netem loss` |
| `packet_corrupt` | Corrupt packet contents | `pumba netem corrupt` |
| `bandwidth_limit` | Restrict bandwidth | `pumba netem rate` |

Note: all inter-service calls in Online Boutique use gRPC. Network faults may surface as
gRPC-specific errors (e.g. `DEADLINE_EXCEEDED`, `UNAVAILABLE`) rather than generic HTTP
errors — capture this in `expected_root_cause`.

## 3. Application/container-level

| fault_type | Description | Tool |
|---|---|---|
| `container_kill` | Forcefully kill the container | `pumba kill` |
| `container_pause` | Freeze the container's process | `pumba pause` |
| `container_stop` | Graceful stop (simulates slow shutdown) | `pumba stop` |

**`adservice` caveat:** JVM restart/cold-start after `container_kill` or `container_stop`
takes noticeably longer than the other (Go/Python/Node) services. Account for this in
expected recovery time, and don't mistake slow JVM warm-up for a persistent fault effect.

**`shoppingassistantservice` caveat:** this service calls an LLM to generate responses.
Faulting it (or its dependencies) can surface as AI-generated error/fallback text instead of
a typical stack trace or gRPC status code. Treat this as a known confounder — cross-check any
RCA finding here against this caveat before accepting it as a genuine detection, the same way
`adservice` JVM noise is handled.

## 4. Dependency-level

| fault_type | Description | Tool |
|---|---|---|
| `grpc_error_injection` | Force an error response from a service's gRPC endpoint | Toxiproxy / manual interceptor |
| `timeout_injection` | Delay a response past the caller's timeout | Toxiproxy |
| `service_unavailable` | Downstream genuinely unreachable | Combination of `container_stop` + network partition |

`checkoutservice` is the most useful orchestration point for dependency-level scenarios: it
calls `paymentservice`, `shippingservice`, `emailservice`, `cartservice`, and
`currencyservice` in sequence, making it a good target for observing fault propagation.

## Ground truth logging schema

Every fault execution MUST be logged with the following schema (see also `AGENTS.md`):

```json
{
  "experiment_id": "exp-2026-09-06-001",
  "timestamp_start": "2026-09-06T10:00:00+07:00",
  "timestamp_end": "2026-09-06T10:02:00+07:00",
  "fault_type": "container_kill",
  "target_service": "productcatalogservice",
  "parameters": {},
  "expected_root_cause": "productcatalogservice unavailable, propagates to frontend and recommendationservice"
}
```

## Execution rules

- One fault per execution, except for explicitly designed and separately documented
  "cascading fault" scenarios.
- Minimum gap between fault executions: enough for the system to return to a steady-state
  baseline — verify via the Grafana dashboard before proceeding.
- `resource-level` faults (especially `cpu_stress`) and any fault involving `adservice` or
  `shoppingassistantservice` require extra attention because of known potential confounders
  — run these with host monitoring active and note any host-wide effects.
