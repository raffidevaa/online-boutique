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
| Observability | Prometheus, Grafana, Loki, Promtail, cAdvisor | `research/observability/` |
| Fault injection | Pumba + custom ground truth logger | `research/fault-injection/` |
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

# 4. Run an example fault scenario
python research/fault-injection/run_scenario.py --scenario research/fault-injection/scenarios/resource-01.yaml

# 5. Run the agent against an incident time window
python research/agent/run_rca.py --start <timestamp> --end <timestamp>
```

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
- [`docs/EXPERIMENT_LOG.md`](docs/EXPERIMENT_LOG.md) — experiment log template
- [`../AGENTS.md`](../AGENTS.md) — conventions for AI coding agents working in this repo

## Research status

See [`research/milestones/`](milestones/README.md) for the detailed development roadmap and
current phase status.
