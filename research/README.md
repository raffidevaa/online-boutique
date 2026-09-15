# Agentic AI for Root Cause Analysis & Automated Remediation in a Simulated SRE Environment

Thesis project: design and implementation of an agentic AI system that performs root cause
analysis (RCA) and recommends automated remediation, evaluated in a simulated SRE
environment with controlled fault injection.

This directory lives inside a clone of `GoogleCloudPlatform/microservices-demo` (Online
Boutique) and reuses its `src/` for source-code transparency, but runs its own Docker
Compose stack under `research/`, independent of the upstream Kubernetes
manifests/Helm chart/Skaffold config at the repo root — see
[`research/docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for why.

## Main components

| Layer | Tools | Location |
|---|---|---|
| Microservices app | Online Boutique (`GoogleCloudPlatform/microservices-demo`) | `research/compose/` (source under repo-root `src/`) |
| Observability | Prometheus, Grafana, Loki, Promtail, cAdvisor, OpenTelemetry, Jaeger | `research/observability/` |
| Experiment runtime | Compose application access, read-only observer, CLI, and shared utilities | `research/service/`, `observer/`, `orchestrator/`, `utils/` |
| Fault injection | Versioned scenario catalog, gated Pumba planner, and future workload controls | `research/generators/` |
| Agentic AI | LLM via API + tool-calling into observability | `research/agent/` |

## Why Online Boutique

Chosen over Sock Shop primarily for **source code transparency**: Online Boutique is a
monorepo with full source under `src/` for every service, whereas Sock Shop is a polyrepo
where the deployment repo only contains manifests, not application code. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full comparison and trade-offs
(Online Boutique is heavier on CPU, requires a hand-assembled Docker Compose file, and needs
extra care around the JVM-based `adservice` and the LLM-backed `shoppingassistantservice`).

## Quickstart

```bash
# 1. Start the simulated environment + observability stack
docker compose -f research/compose/docker-compose.yml up -d

# 2. Verify all services are healthy
docker compose -f research/compose/docker-compose.yml ps

# 3. Access
# Online Boutique frontend : http://localhost:8080
# Grafana                  : http://localhost:3000
# Prometheus               : http://localhost:9090
# Jaeger UI                : http://localhost:16686

# 4. Validate the architecture without changing runtime state
python -m research.orchestrator doctor

# 5. Capture a read-only snapshot outside the repository
python -m research.orchestrator snapshot --output /tmp/research-architecture-snapshot

# 6. Inspect a fault without contacting Docker or creating experiment data
python -m research.orchestrator fault plan \
  --scenario research/generators/scenarios/network-05-loss-productcatalog.yaml

# 7. Prepare fault-injection images explicitly (only needed before a fault pilot)
python -m research.orchestrator fault prepare \
  --scenario research/generators/scenarios/resource-03-cpu-hog-checkout.yaml \
  --pull

# 8. Execute one controlled fault after preparation
python -m research.orchestrator fault run \
  --scenario research/generators/scenarios/resource-03-cpu-hog-checkout.yaml \
  --execute

# 9. Build one research-owned code-level faulty image
python -m research.orchestrator fault image build \
  --scenario research/generators/scenarios/code-level-01-incorrect-return-currency.yaml

# 10. Verify a built code-level image and its explicit Compose override
python -m research.orchestrator fault image prepare \
  --scenario research/generators/scenarios/code-level-01-incorrect-return-currency.yaml
```

Pumba is an on-demand fault-injection tool, not a permanent Compose service. The prepare
command explicitly verifies or pulls its pinned image and any scenario-specific helper image;
the fault runner still uses `--pull never` and will not pull images implicitly during an
experiment.

Code-level scenarios use research-owned faulty images built from temporary patched copies of
the upstream service source. The baseline source tree is not edited; each faulty image is
selected only through its allowlisted scenario-specific Compose override. Code-level runs
remain gated until the image build, behavior validator, telemetry capture, and baseline
recovery have all been validated.

## Development hardware

- Homelab server: 2-core/4-thread CPU, 20 GB RAM (after upgrade), 512 GB SSD + 320 GB HDD.
- Orchestration: Docker Compose (not Kubernetes, not k3s/k3d) — justified in
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) due to CPU constraints, even though this
  repo already ships Kubernetes manifests/Helm chart for the upstream project.
- Always use prebuilt GCR images for Online Boutique services rather than building from
  source, to avoid unnecessary CPU load during setup.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system architecture & technical decision rationale
- [`docs/FAULT_TAXONOMY.md`](docs/FAULT_TAXONOMY.md) — fault taxonomy covered by this research
- [`docs/FAULT_INJECTION.md`](docs/FAULT_INJECTION.md) — RCAEval-aligned methodology and scenario catalog
- [`docs/EXPERIMENT_LOG.md`](docs/EXPERIMENT_LOG.md) — experiment log template
- [`../AGENTS.md`](../AGENTS.md) — conventions for AI coding agents working in this repo

## Research status

See [`research/milestones/`](milestones/README.md) for the detailed development roadmap and
current phase status.
