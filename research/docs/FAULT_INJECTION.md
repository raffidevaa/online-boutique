# Fault-injection methodology and scenario catalog

This document defines the proposed Root Cause Analysis (RCA) experiments for Online
Boutique running under Docker Compose. It adapts the taxonomy of [RCAEval](https://doi.org/10.1145/3701716.3715290);
the scenarios, parameters, targets, and expected propagation paths are designed specifically
for this thesis and are not official RCAEval cases.

The taxonomy is deliberately separated from implementation tools. Pumba, Linux `tc`,
`stress-ng`, a Docker lifecycle operation, or a future faulty image are delivery mechanisms;
they are not fault categories.

## Experimental principle

Every future case follows:

```text
normal workload -> baseline telemetry -> one controlled fault
                 -> abnormal telemetry/alerts -> agent RCA
                 -> remediation recommendation -> ground-truth evaluation -> recovery
```

Before a pilot, prepare the pinned injector images explicitly:

```bash
python -m research.orchestrator fault prepare \
  --scenario research/generators/scenarios/resource-03-cpu-hog-checkout.yaml \
  --pull
```

`fault plan` only renders a non-mutating Pumba command. `fault prepare` verifies the required
images and pulls missing images only when `--pull` is supplied. `fault run --execute` never
pulls images implicitly and launches Pumba on demand; Pumba is therefore not a permanent
Docker Compose service. The runner also requires the observability backends to remain ready;
if baseline telemetry cannot be captured, the run is marked `baseline_failed` and no fault is
injected.

The RCA agent may receive alerts, metrics, logs, traces when available, service topology,
service metadata, and approved runbooks. It must not receive the scenario ID, ground truth,
injection command, or injector output.

## Scenario contract

Each scenario definition must specify:

```yaml
scenario_id: FI-...
category: resource | network | code_level
fault_type: normalized taxonomy identifier
target_service: canonical Compose service
workload: reproducible profile and trigger
parameters: explicit intensity and duration
execution_status: ready_for_pilot | experimental_unavailable | deferred_faulty_image
expected_local_symptoms: []
expected_propagated_symptoms: []
expected_telemetry_evidence: {}
ground_truth: {root_cause_service, fault_category, fault_type}
expected_remediation: []
```

Cases must be reproducible, observable, non-trivial for RCA, isolated to one primary fault,
and recoverable. A visible symptom in `frontend` must not automatically be treated as the
root cause when a downstream dependency is responsible.

The versioned scenario files in `research/generators/scenarios/` are the executable catalog
contract. Their status is deliberately more precise than the research priority: a catalogued
case is not automatically safe or available to inject.

## Service-target guidance

Prioritize services by dependency role rather than injecting every fault into every service:

| Service | Experimental rationale |
|---|---|
| `frontend` | User-visible edge symptoms and fan-out effects |
| `checkoutservice` | Central orchestration and multiple downstream calls |
| `cartservice` | Stateful, frequently accessed shopping path |
| `paymentservice` | Critical checkout dependency |
| `shippingservice` | Critical checkout dependency |
| `currencyservice` | Frequently called browsing dependency |
| `productcatalogservice` | Core browsing dependency and safe smoke-test target |
| `recommendationservice` | Controlled resource degradation target |
| `emailservice` | Non-critical downstream propagation target |

`loadgenerator` is a workload backend, not a fault target. `adservice` and
`shoppingassistantservice` require confounder flags: JVM startup/GC noise and LLM-generated
fallback text respectively.

## Core experimental set

The initial six-case set is chosen for telemetry diversity. Core does not mean immediately
executable: code-level cases are deferred until reproducible faulty-image variants and
explicit source-change approval exist.

### FI-RES-CPU-01 — CPU saturation on recommendation service

- **Category/type:** `resource` / `cpu_hog`
- **Target/workload:** `recommendationservice`; browse home page, product detail, and recommendations.
- **Pilot parameters:** 80–95% target CPU, 120 seconds; finalize intensity after pilot runs.
- **Local evidence:** container CPU and recommendation latency increase, with p95/p99 degradation.
- **Propagation:** recommendation delay causes longer product-page/frontend requests.
- **Ground truth:** `recommendationservice`, `resource`, `cpu_hog`.
- **Remediation family:** identify CPU-intensive work, restart only for a runaway process, investigate hot code paths, or adjust CPU allocation when demand is legitimate.
- **Status:** core resource case; host CPU must be monitored separately.

### FI-RES-MEM-01 — Memory pressure on cart service

- **Category/type:** `resource` / `memory_pressure`
- **Target/workload:** `cartservice`; repeated add/get-cart operations through browse and checkout flows.
- **Pilot parameters:** gradual 50% → 70% → 85%+ working-set growth over 180–300 seconds; avoid immediate OOM.
- **Local evidence:** working set, allocation pressure, swap/OOM indicators, and cart latency increase.
- **Propagation:** slower `GetCart`/`AddItem` affects frontend and checkout.
- **Ground truth:** `cartservice`, `resource`, `memory_pressure`.
- **Remediation family:** inspect growth, bound caches/state, restart if unbounded, and correct object lifecycle or limits.
- **Status:** core resource case; memory safety gate required on the 2-core host.

### FI-NET-DELAY-01 — Payment service network latency

- **Category/type:** `network` / `network_delay`
- **Target/workload:** `paymentservice`; active checkout traffic.
- **Pilot parameters:** 300 ms delay, 50 ms jitter, 120 seconds; pilot low/medium/high bands of 100/300/800 ms.
- **Local evidence:** payment RPC/span latency and deadline symptoms increase.
- **Propagation:** checkout waits on payment, increasing checkout and frontend latency.
- **Ground truth:** `paymentservice`, `network`, `network_delay`.
- **Remediation family:** inspect path/connectivity, timeout/retry configuration, retry amplification, and recent network/proxy changes.
- **Status:** core network case.

### FI-NET-LOSS-01 — Packet loss on payment service

- **Category/type:** `network` / `packet_loss`
- **Target/workload:** `paymentservice`; active checkout traffic.
- **Pilot parameters:** 15% loss for 120 seconds, with conceptual low/medium/high bands of 5/15/30%.
- **Local evidence:** failed RPC spans, resets, deadline errors, retries, and increased payment error rate.
- **Propagation:** checkout failures or timeouts surface at the frontend.
- **Ground truth:** `paymentservice`, `network`, `packet_loss`.
- **Remediation family:** verify connectivity/path, reduce retry amplification, and roll back faulty network configuration.
- **Status:** core network case.

### FI-CODE-RETURN-01 — Incorrect currency return value

- **Category/type:** `code_level` / `incorrect_return_value`
- **Target/workload:** `currencyservice`; browse flows that display converted prices.
- **Fault concept:** return zero, invalid, or incorrect conversion while transport remains healthy.
- **Evidence:** semantically wrong prices with normal infrastructure metrics and successful traces/logs.
- **RCA challenge:** requires application-level reasoning rather than infrastructure anomaly matching.
- **Ground truth:** `currencyservice`, `code_level`, `incorrect_return_value`.
- **Remediation family:** inspect recent release/business logic, validate conversion tests, roll back, and deploy a corrected image.
- **Status:** core but deferred; requires faulty image variant and explicit approval for source/instrumentation work.

### FI-CODE-EXC-01 — Missing exception handler

- **Category/type:** `code_level` / `missing_exception_handler`
- **Target/workload:** `checkoutservice`; checkout traffic with a downstream failure condition.
- **Fault concept:** a downstream exception is not handled or translated correctly.
- **Evidence:** unhandled stack traces, failed parent spans, and increased checkout errors.
- **RCA challenge:** distinguish the service reporting the error from the service introducing the handling defect.
- **Ground truth:** `checkoutservice`, `code_level`, `missing_exception_handler`.
- **Remediation family:** restore error handling/fallback behavior, validate propagation, roll back, and deploy a corrected image.
- **Status:** core but deferred; requires faulty image variant and explicit approval for source/instrumentation work.

## Candidate and backlog cases

These cases are represented in the scenario catalog now, but do not expand the initial core
headline metrics until pilots show distinct RCA value and safe reproducibility. Disk and socket
cases are registered as `experimental_unavailable`: neither has an attributable target under
the current telemetry model. Code-level cases are registered as `deferred_faulty_image` and
remain unavailable until an approved, reproducible faulty image exists.

| ID | Type | Target | Purpose/status |
|---|---|---|---|
| `FI-RES-CPU-02` | `cpu_hog` | `checkoutservice` | `ready_for_pilot`; distinguish orchestration saturation from downstream latency. |
| `FI-RES-DISK-01` | `disk_stress` | target selected after I/O pilot | `experimental_unavailable`; require attributable storage telemetry. |
| `FI-RES-SOCK-01` | `socket_stress` | target selected after connection pilot | `experimental_unavailable`; require attributable connection telemetry. |
| `FI-NET-DELAY-02` | `network_delay` | `shippingservice` | `ready_for_pilot`; paired checkout case. |
| `FI-NET-DELAY-03` | `network_delay` | `currencyservice` | `ready_for_pilot`; frontend-visible browsing symptoms. |
| `FI-NET-LOSS-02` | `packet_loss` | `productcatalogservice` | `ready_for_pilot`; non-critical first live-pilot target. |
| `FI-CODE-PARAM-01` | `incorrect_parameter` | `checkoutservice` | `deferred_faulty_image`; malformed downstream request. |
| `FI-CODE-PARAM-02` | `missing_parameter` | `checkoutservice` | `deferred_faulty_image`; missing required field. |
| `FI-CODE-CALL-01` | `missing_function_call` | `checkoutservice` | `deferred_faulty_image`; missing business-flow call. |

## Severity and variation

Severity is finalized through pilot measurements, not assumed from the command:

```yaml
low: observable degradation; service remains healthy
medium: significant degradation; service remains available
high: timeout/error likely; partial service failure possible
```

Build coverage as `fault type × target × severity × repetition`. Repeating payment and
shipping delay with the same apparent checkout symptom tests whether RCA follows telemetry
instead of memorized service names. Use Top-1 and Top-3 ranked hypotheses.

## Evaluation targets

Report these separately before combining them:

- root-cause service accuracy;
- fault-type accuracy;
- combined service-and-fault accuracy;
- Top-1 and Top-3 ranked root-cause accuracy;
- detection/localization time where the agent exposes timestamps.

## Priority roadmap

1. **Runtime pilots:** CPU hog, memory pressure, network delay, and packet loss. Eight
   catalogued cases are `ready_for_pilot`; verify injection, telemetry diversity, ground
   truth, and recovery one at a time.
2. **Deferred application faults:** incorrect return value and missing exception handler. Use
   faulty image variants and test application-level reasoning.
3. **Optional extensions:** disk stress, socket stress, incorrect/missing parameters, and
   missing function calls only when pilots show non-redundant RCA value.

## Ground truth and experiment records

Ground truth is stored separately from agent input and must include at least:

```yaml
root_cause_service: paymentservice
fault_category: network
fault_type: network_delay
injection_start: ISO-8601 timestamp
injection_end: ISO-8601 timestamp
severity: medium
```

The structured run record additionally stores scenario metadata, parameters, workload,
baseline/incident/recovery windows, telemetry artifacts, and cleanup status. Use
`research/experiments/ground-truth.schema.json` as the canonical ground-truth contract;
this catalog defines the intended future fields.

## Remediation evaluation

Do not score recommendations by exact string matching. Score each on:

1. correctness for the known fault;
2. relevance to the causal mechanism;
3. safety under Docker Compose constraints;
4. actionability and reversibility.

Recommendations may include investigation, restart when justified, configuration rollback,
resource adjustment with evidence, or deployment of a corrected faulty-image variant. Actual
execution remains outside this documentation phase.

## Research boundary

RCAEval contributes the taxonomy, multi-source telemetry perspective, and ground-truth
evaluation idea. Online Boutique targets, intensities, workloads, propagation paths, and
Compose-specific recovery procedures are this thesis’s design. Kubernetes-only scheduling,
node, admission, storage, and control-plane faults remain out of scope.
