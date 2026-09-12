# System architecture

## End-to-end flow

```
Future controlled fault injection
        │
        ▼
Microservices app (Online Boutique) ──► Observability (Prometheus/Loki/Grafana)
        │                                        │
        ▼                                        ▼
Ground truth logger                     Agentic AI (RCA + remediation)
        │                                        │
        └────────────────► Evaluation ◄──────────┘
```

## Current experiment architecture foundation

The research runtime is now organized around research concepts rather than direct tool calls:

```text
Experiment command -> orchestrator
                      -> Compose application service + metadata + Docker state
                      -> observer (Prometheus, Loki, alerts)
                      -> append-only experiment artifacts
                      -> future generators / RCA agent / evaluator
```

`research/orchestrator/` has no LLM dependency. `research/service/` is the sole owner of
low-level Compose and Docker inspection, while `research/observer/` provides read-only
telemetry access. `research/generators/` currently contains only a tool-neutral fault input:
no Pumba backend or fault scenario is active during the architecture phase.

Fault methodology is documented separately in `FAULT_INJECTION.md`. It uses RCAEval as the
taxonomy reference and keeps resource, network, and code-level faults distinct from the
tools used to deliver them. Code-level cases require reproducible faulty-image variants and
remain deferred until explicitly approved.

## Design decisions & rationale

### 1. Docker Compose, not Kubernetes (including k3s/k3d)

k3s + Chaos Mesh was considered first, then k3d as a Docker-hosted alternative. Both were
rejected in favor of Docker Compose + Pumba because:

- Development hardware is CPU-constrained (2 core/4 thread). Any Kubernetes control plane —
  whether k3s running natively or k3d running k3s inside Docker-in-Docker — adds background
  CPU overhead (API server, scheduler, controller-manager, CRD reconciliation loops for
  Chaos Mesh) that runs continuously, even when the system is idle. k3d adds an extra
  virtualization layer on top of that, making it strictly heavier than native k3s.
- Docker Compose removes this orchestration layer entirely, leaving more CPU/RAM headroom
  for the app, observability, and the agent.
- This decision compounds with the CPU cost profile of Online Boutique itself (see decision
  #2) — stacking Kubernetes overhead on top of Online Boutique's own JVM/gRPC overhead was
  judged too risky for a 2-core host.
- **Note:** this repo happens to be a clone of the upstream Online Boutique project, which
  already ships ready-made Kubernetes manifests, a Helm chart, and Istio manifests
  (`kubernetes-manifests/`, `helm-chart/`, `istio-manifests/`). Their existence doesn't
  change the calculus above — the constraint being optimized for is steady-state host CPU
  headroom during fault injection, not the effort of writing manifests — so Compose remains
  the choice for this research, and those directories are left untouched as upstream
  reference material.
- Trade-off: loses the ability to inject node-level and kernel-level faults (only relevant
  under Kubernetes). This research's fault taxonomy is scoped to resource, network, and
  code-level faults; delivery mechanisms remain separate — see `FAULT_TAXONOMY.md`.

### 2. Online Boutique as the microservices app

Chosen over Sock Shop and TeaStore for one primary reason: **source code transparency**.

- Online Boutique (`GoogleCloudPlatform/microservices-demo`) is a **monorepo** — full source
  code for every service lives under `src/`, alongside each service's Dockerfile.
- Sock Shop, by contrast, is a **polyrepo**: the main deployment repo
  (`microservices-demo/microservices-demo`) contains only compose/Kubernetes manifests, not
  application source. Getting source code requires cloning up to ~9 separate repos.
- Trade-offs accepted in exchange for this transparency:
  - No official docker-compose file is provided by Google (unlike Sock Shop) — one must be
    assembled manually from the `src/` Dockerfiles or, preferably, from the prebuilt GCR
    images. (Confirmed: this repo ships Kubernetes manifests/Helm/Skaffold, but no
    docker-compose file anywhere.)
  - Heavier resource footprint (~3-4 GB RAM for the app alone) due to a more heterogeneous
    runtime mix (Go, Java/JVM, Node.js, Python, C#/.NET) compared to Sock Shop. The current
    `src/` tree has 12 services plus the `loadgenerator` (Go/Python `locust`-based) — one
    more than earlier revisions of this plan accounted for
    (`shoppingassistantservice`, an LLM-backed service, was added upstream since).
  - `adservice` (JVM) introduces CPU spikes at startup and during garbage collection that
    are independent of any injected fault — treated explicitly as a known confounder (see
    "Research limitations" below).
  - `shoppingassistantservice` calls an LLM itself, which is a second, distinct kind of
    confounder: its failure modes can look like AI-generated error text rather than a
    typical stack trace or timeout, which the RCA agent needs to be able to distinguish from
    genuine injected faults.
  - gRPC (used for all inter-service communication) has slightly higher serialization
    overhead than the plain HTTP/REST used by Sock Shop.
- Mitigation: always use prebuilt images from Google's GCR registry
  (`gcr.io/google-samples/microservices-demo/<service>`) instead of building from source
  locally, to avoid the additional CPU cost of compiling Java/restoring .NET packages during
  setup. Source code remains available under `src/` for reading, documentation, and any
  targeted instrumentation work, without requiring local builds for day-to-day operation.

### 3. LLM via API, not self-hosted

- A self-hosted LLM (even a small 7B quantized model) requires significant RAM and, without
  a GPU, inference on a 2-core/4-thread CPU would be far too slow for an agent that needs to
  perform iterative tool-calling per incident.
- Accessing the LLM via API moves the compute burden to the provider; the homelab only runs
  the orchestration layer (lightweight, ~100–300 MB RAM).
- This mirrors the pattern used by the majority of surveyed LLM-agent research for
  RCA/incident response (MicroRCA-Agent, IRCopilot, Intelligent SRE Framework).

## Resource allocation (target, homelab 2 core/4 thread, 20 GB RAM)

| Component | RAM | Notes |
|---|---|---|
| Online Boutique (12 `src/` services + `loadgenerator`, prebuilt GCR images) | ~3-4 GB | See `research/compose/docker-compose.yml` for per-service limits; `adservice` capped explicitly on CPU |
| Observability (Prometheus, Grafana, Loki, cAdvisor) | ~1.5-2 GB | |
| Pumba + ground truth logger | ~0.1-0.2 GB | Run on-demand, not as a permanent daemon |
| Agent orchestration + vector store (optional) | ~0.5-1 GB | |
| OS + buffer | ~2 GB | |

CPU remains the primary constraint even after the RAM upgrade to 20 GB — see "Research
limitations" below.

## Research limitations (scope limitation)

1. The fault taxonomy is scoped to resource, network, and code-level faults — it does not
   cover node-level or kernel-level faults, since Kubernetes is not used for this research
   (despite the upstream Kubernetes manifests being present in this repo for other purposes).
2. Host CPU is limited (2 core/4 thread). CPU-stress fault scenarios can affect the entire
   host rather than only the target container. Mitigation: faults are executed one at a
   time (never in parallel unless explicitly testing a cascading scenario), and host-level
   resource usage is monitored separately from container-level usage during experiments to
   detect potential confounding.
3. `adservice` (JVM) can exhibit CPU spikes from startup and garbage collection independent
   of injected faults. Any anomaly detected there by the RCA agent is cross-checked against
   this known confounder before being accepted as a genuine finding.
4. `shoppingassistantservice` (LLM-backed) can produce AI-generated error content when
   faulted, which may not match the typical error signatures (stack traces, gRPC status
   codes) the RCA agent is tuned to recognize elsewhere. Treated as a second known
   confounder, cross-checked the same way as `adservice`.
5. Online Boutique has no official docker-compose file; the one used in this project was
   assembled manually and is documented in `research/compose/docker-compose.yml`.
6. Distributed tracing relies on Online Boutique's native OpenTelemetry instrumentation;
   deeper custom instrumentation, if needed, requires building selected services from source
   rather than using prebuilt images.
7. The actual homelab host has 7.6 GB RAM (2-core/4-thread CPU confirmed: i3-6100T), not the
   20 GB this doc's "Resource allocation" table targets — the RAM upgrade referenced there
   hadn't happened as of phase 00. The full stack (app + observability, `research/compose/`)
   still runs, but with the desktop session active alongside it, available memory sits in the
   3-3.5 GB range and some (zram-backed, not disk) swap gets used. Re-check headroom before
   adding the agent layer (phase 04+) or running memory-heavier fault scenarios.
8. This host's Docker Engine (29.x) defaults to the containerd-snapshotter storage driver.
   cAdvisor's classic Docker-container factory claims every container's cgroup by name before
   the containerd factory can, then fails to read per-container stats from the (nonexistent,
   under this driver) graphdriver layerdb path — silently dropping all container metrics.
   Fix (see `research/compose/docker-compose.yml`'s `cadvisor` service): don't give cAdvisor a
   docker.sock at all, so its Docker factory fails registration at startup like the
   crio/podman ones already do; point it at `/run/containerd/containerd.sock` with
   `-containerd-namespace=moby` instead. Trade-off: per-container metrics carry `image` and a
   numeric container-ID `name`, not a `container_label_com_docker_compose_service` label
   (that enrichment came from docker.sock); Prometheus derives a readable `service` label from
   `image` via `metric_relabel_configs` instead (see `research/observability/prometheus/prometheus.yml`).
