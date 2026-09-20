# Fault Injection Flow and Extension Guide

This guide explains how a controlled fault moves through the research framework: from a
scenario definition to runtime injection, telemetry evidence, ground truth, recovery, and
future RCA evaluation. It is a developer-facing companion to
[ARCHITECTURE.md](ARCHITECTURE.md), [FAULT_INJECTION.md](FAULT_INJECTION.md),
[FAULT_TAXONOMY.md](FAULT_TAXONOMY.md), and [SEMANTIC_PROBES.md](SEMANTIC_PROBES.md).

The upstream Online Boutique application directories are reference material. Research
deployment, experiments, and fault-injection code live exclusively under `research/`.

## Components and ownership

| Area | Ownership |
|---|---|
| `generators/scenarios/` | Versioned experiment intent: target, fault type, parameters, expected evidence, and ground truth identity. |
| `generators/fault.py` | Strict scenario schema, taxonomy-to-category mapping, and allowed parameter contract. |
| `generators/pumba.py` | Allowlisted Pumba command planning and execution for supported runtime faults. |
| `generators/faulty_image.py` | Allowlisted faulty-image build and preparation path for code-level faults. |
| `generators/semantic_probe.py`, `semantic.py` | Deterministic business-behavior probes and validation for code-level experiments. |
| `orchestrator/` | CLI entry point and the single-fault experiment lifecycle. |
| `service/` | The only layer that queries or changes Docker Compose/Docker state. |
| `observer/` | Read-only Prometheus, Loki, Jaeger, and alert collection. |
| `utils/` | Central configuration and append-only experiment artifact helpers. |
| `experiments/runs/` | Immutable per-run evidence; do not edit or delete it after creation. |
| `agent/` | Future RCA/remediation layer. It receives telemetry evidence, not scenario IDs, injector plans, or ground truth. |

## Runtime resource and network fault flow

The currently runnable Pumba types are `cpu_hog`, `memory_pressure`, `network_delay`,
and `packet_loss`.

```text
scenario YAML
research/generators/scenarios/FI-*.yaml
        |
        v
python -m research.orchestrator fault run --scenario ... --execute
        |
        v
research/orchestrator/__main__.py: main()
        |
        +--> ResearchConfig.from_environment()
        |
        +--> load_scenario(path)
        |      research/generators/fault.py
        |      - schema and ID validation
        |      - category/type validation
        |      - exact parameter contract validation
        |      - ground-truth identity validation
        |
        v
research/orchestrator/faults.py: run_fault()
        |
        +--> fault_lock()
        |      one active local runtime fault only
        |
        +--> ComposeApplication.validate()
        |    +--> ComposeApplication.validate_readiness()
        |      research/service/compose.py
        |      - validate Compose model and service metadata
        |      - require a deployed, targetable service
        |      - inspect the exact target container ID
        |      - require running/healthy containers
        |
        +--> Observer.check_ready()
        |      research/observer/telemetry.py
        |      - Prometheus, Loki, Jaeger readiness
        |      - container-to-Compose-service metric mapping
        |
        +--> PumbaInjector.plan(scenario, container_id)
        |    +--> PumbaInjector.check_images_available(scenario)
        |      research/generators/pumba.py
        |      - convert validated intent to an allowlisted tokenized command
        |      - never accept an arbitrary shell fragment from YAML
        |
        +--> create_run() + write scenario and injector plan
        |      research/utils/artifacts.py
        |
        +--> Observer.capture(baseline window)
        |      metrics + logs + alerts + traces
        |      If this fails, do not inject the fault.
        |
        +--> PumbaInjector.execute(plan)
        |      docker run ... pumba ... against the exact container ID
        |
        +--> Observer.capture(incident window)
        +--> Observer.capture(recovery window)
        +--> ComposeApplication.snapshot()
        |
        v
research/experiments/runs/EXP-.../
```

`workload` in a scenario is currently an experiment contract and documentation field. The
runner does **not** start `loadgenerator`; establish the intended workload and a stable
baseline before starting `fault run --execute`.

## Code-level faulty-image flow

Code-level faults deliberately do not use Pumba: the injected problem must be a reproducible
application behavior change.

```text
code-level scenario YAML
        |
        v
load_scenario()
        |
        v
FaultyImageInjector.plan()
research/generators/faulty_image.py
        |
        +--> generators/faulty_images/manifest.yaml
        |      scenario ID -> service, image, Dockerfile, patch, override, validator
        |
        +--> research-owned Dockerfile + research-owned patch
        |
        v
fault image build / fault image prepare
        |
        v
run_faulty_image()
research/orchestrator/faults.py
        |
        +--> validate Compose, target readiness, telemetry, image, and override
        +--> capture telemetry baseline
        +--> run baseline semantic probe (automatic mode)
        |      baseline failure prevents injection
        |
        +--> apply_fault_override()
        |      docker compose -f base -f override up -d --no-deps --force-recreate target
        +--> wait_for_service_healthy(target)
        +--> stabilize, then run incident semantic probe
        +--> capture incident telemetry
        +--> build_semantic_observation()
        +--> validate_semantic()
        |
        +--> finally: restore_service(target)
        |      recreate target from baseline Compose configuration
        +--> require restored target health
        +--> capture recovery telemetry and write artifacts
        |
        v
completed | completed_unvalidated | semantic_validation_failed | recovery_failed
```

The `finally` restoration path is essential: even an exception while capturing evidence must
not leave the application on a faulty image.

## Decision tree: what must change for a new fault?

```text
Need to add a fault experiment
        |
        v
Is its fault type already runtime-runnable?
        |
        +-- Yes: cpu_hog, memory_pressure, network_delay, packet_loss
        |       |
        |       v
        |   Add one scenario YAML only
        |   (provided the target is deployed and targetable)
        |
        +-- No
                |
                v
        Is it a code-level behavior defect?
                |
                +-- Yes
                |     |
                |     v
                |   Add scenario + faulty-image manifest entry + patch + override
                |   + Dockerfile if the target service has no supported one
                |   + semantic probe/validator when no existing behavior proof fits
                |
                +-- No: a new resource/network delivery type
                      |
                      v
                    Update taxonomy and documentation
                      -> add type/category in fault.py
                      -> add exact parameter contract in fault.py
                      -> implement an allowlisted delivery adapter
                      -> prove attributable telemetry evidence
                      -> permit ready_for_pilot only after the above
                      -> add unit tests and a scenario YAML
```

`disk_stress` and `socket_stress` demonstrate the distinction: they are catalogued taxonomy
types, but remain `experimental_unavailable` because the current system does not yet provide
a safe delivery path and attributable target telemetry.

## Add a scenario for an existing Pumba type

For a new variation of an existing runnable type, begin in
`research/generators/scenarios/`. Use a unique `FI-*` identifier and the exact field set
enforced by `load_scenario()`.

```yaml
schema_version: 1
scenario_id: FI-NET-DELAY-04
category: network
fault_type: network_delay
execution_status: ready_for_pilot
target_service: emailservice
workload: {generator: loadgenerator, trigger: active checkout traffic}
parameters: {duration_seconds: 120, delay_ms: 300, jitter_ms: 50}
severity: medium
expected_local_symptoms: ["email RPC latency increases"]
expected_propagated_symptoms: ["checkout latency or failure increases"]
expected_telemetry_evidence: {metrics: [service_latency], logs: [deadline_exceeded]}
ground_truth:
  root_cause_service: emailservice
  fault_category: network
  fault_type: network_delay
expected_remediation: ["inspect path, timeout, retry, and network configuration"]
recovery: "Pumba removes the netem rule at duration end; confirm recovery."
```

The exact allowed parameters are defined in `PARAMETER_KEYS` in `generators/fault.py`:

| Fault type | Required parameters |
|---|---|
| `cpu_hog` | `duration_seconds`, `cpu_workers`, `intended_cpu_percent` |
| `memory_pressure` | `duration_seconds`, `memory_workers`, `memory_mb_per_worker`, `intended_memory_percent` |
| `network_delay` | `duration_seconds`, `delay_ms`, `jitter_ms` |
| `packet_loss` | `duration_seconds`, `loss_percent` |

The target must be present in `research/service/metadata.yaml`, have `targetable: true`, be
deployed, and be healthy at run time. Do not target `loadgenerator`; it is a workload source,
not a fault target. Treat `adservice` and `shoppingassistantservice` as known confounder cases
when they become deployed.

## Safe command sequence

Use the commands in this order. The first is non-mutating; the second only pulls explicitly
requested injector images; the third is the actual controlled fault injection.

```bash
python -m research.orchestrator fault plan \
  --scenario research/generators/scenarios/network-06-delay-email.yaml

python -m research.orchestrator fault prepare \
  --scenario research/generators/scenarios/network-06-delay-email.yaml --pull

python -m research.orchestrator fault run \
  --scenario research/generators/scenarios/network-06-delay-email.yaml --execute
```

For code-level scenarios, build and verify the approved image before `fault run`:

```bash
python -m research.orchestrator fault image plan --scenario <scenario.yaml>
python -m research.orchestrator fault image build --scenario <scenario.yaml>
python -m research.orchestrator fault image prepare --scenario <scenario.yaml>
python -m research.orchestrator fault run --scenario <scenario.yaml> --execute
```

## Artifact and information flow

```text
scenario YAML -------------------------> scenario.json
validated injector plan ---------------> injector-plan.json / faulty-image-plan.json
preflight and lifecycle events --------> events.jsonl
baseline telemetry --------------------> baseline/{metrics,logs,alerts,traces}.json
fault interval telemetry --------------> incident/{metrics,logs,alerts,traces}.json
recovery telemetry --------------------> recovery/{metrics,logs,alerts,traces}.json
actual injection timing and identity --> ground_truth.json
delivery output -----------------------> injector-result.json
final lifecycle state -----------------> result.json + cleanup-state.json

telemetry artifacts -------------------> future RCA agent
scenario ID / injector plan / ground truth -X-> future RCA agent
ground truth + RCA output -------------> evaluation
```

The run directory is created by `create_run()`. `write_json()` refuses to overwrite an
existing file, preserving experiment evidence as append-only data.

## Implementation checklist

Before considering a new fault ready:

1. Define one primary target and one primary causal mechanism; do not run multiple faults in
   parallel.
2. Document taxonomy, workload, expected local/propagated symptoms, expected telemetry, and
   recovery behavior.
3. Add the scenario and pass strict scenario validation.
4. For a new delivery type, implement a constrained adapter rather than accepting arbitrary
   commands from a scenario file.
5. Confirm the target service, Compose configuration, resource limits, and observability
   mapping support safe attribution.
6. Ensure a baseline capture failure prevents injection.
7. For code-level faults, prove the intended business behavior with a semantic probe and
   always restore the baseline image.
8. Add tests for the scenario contract, delivery plan, failure gates, evidence artifacts, and
   cleanup/recovery.
9. Pilot one fault at a time, preferably against a non-critical service first, then inspect
   baseline, incident, recovery, and ground truth artifacts before using the result for RCA
   evaluation.
