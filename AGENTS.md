# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Copilot, etc.) working in this repo.

## About this project

This repo is a clone of `GoogleCloudPlatform/microservices-demo` (Online Boutique), used as
the target application for a thesis project: Design and Implementation of an Agentic AI
System for Root Cause Analysis and Automated Remediation Recommendation in a Simulated SRE
Environment. The thesis work has three main parts:

1. **Simulated environment** — Online Boutique plus an observability stack, run via Docker
   Compose on a homelab server.
2. **Experiment framework and fault-injection layer** — Compose environment, telemetry, and
   later controlled-fault adapters that record ground truth.
3. **Agentic AI layer** — an LLM-based agent (accessed via API) that performs RCA and
   recommends remediation based on observability data.

Read `research/docs/ARCHITECTURE.md` before changing any part of the system structure.

**Important — two things live side by side in this repo:**

- `src/`, `kubernetes-manifests/`, `helm-chart/`, `istio-manifests/`, `kustomize/`,
  `skaffold.yaml`, `terraform/`, `protos/`, `release/` are the **upstream Online Boutique
  project**. They exist for source-code transparency (see `research/docs/ARCHITECTURE.md`
  §2) and are kept untouched. Do not use them as this research's deployment path — this
  project does not use Kubernetes (see below).
- `research/` is the **thesis project scaffold** — everything described in this file and in
  `research/docs/` lives there.

## Repo structure (thesis scaffold, under `research/`)

```
research/
├── compose/                # docker-compose.yml + override files
├── generators/             # fault/workload generator inputs and later adapters
├── orchestrator/           # experiment commands and later lifecycle runner
├── service/                # Compose application adapter, Docker state, and service metadata
├── observer/               # read-only Prometheus, Loki, and alert access
├── utils/                  # shared configuration and append-only artifact helpers
├── observability/          # Prometheus, Loki, Promtail config, Grafana dashboard JSON
├── agent/                  # agentic AI code (orchestration, tools, prompts)
│   ├── tools/               # tool-calling wrappers for Prometheus/Loki API
│   └── prompts/
├── docs/                   # technical & methodology documentation for this thesis
├── milestones/             # phase-by-phase development roadmap (start here)
└── experiments/            # experiment results, analysis notebooks, evaluation
```

Note: the top-level `docs/` in this repo belongs to the upstream Online Boutique project
(development guide, adding a microservice, releasing, etc.) — thesis documentation goes in
`research/docs/` instead, to avoid mixing the two.

## Environment & running the project

- The target app is **Online Boutique** (`GoogleCloudPlatform/microservices-demo`), a
  monorepo with source code for every service under `src/`. Always use the **prebuilt
  images from Google's GCR registry** (`gcr.io/google-samples/microservices-demo/<service>`)
  rather than building images locally — building (compiling Java, restoring .NET packages,
  etc.) is significantly more CPU-intensive than pulling a prebuilt image, and this project
  runs on constrained hardware.
- Everything for this research runs via Docker Compose, **not Kubernetes**. Do not introduce
  Kubernetes dependencies (kubectl, helm, k8s manifests, k3s/k3d) into the research workflow
  unless explicitly requested — even though this repo already ships Kubernetes
  manifests/Helm chart upstream, see `research/docs/ARCHITECTURE.md` for why Kubernetes
  (including lightweight distros like k3s/k3d) was still rejected for this hardware profile.
- Target hardware: homelab server with a 2-core/4-thread CPU and 20 GB RAM. **Always set
  resource limits (`mem_limit`, `cpus`) on every service added to the compose file.**
- **`adservice` (JVM) needs special care.** JVM startup and garbage-collection pauses can
  cause CPU spikes independent of any injected fault. Always cap its CPU allocation
  explicitly and treat unexplained latency spikes there as a potential confounder, not
  automatically a genuine cascading failure.
- **`shoppingassistantservice` (LLM-backed) needs special care too.** It calls an LLM itself,
  so faults targeting it can surface as AI-generated error text rather than a typical
  stack trace/timeout — treat this as a second known confounder, same handling as
  `adservice`. See `research/docs/FAULT_TAXONOMY.md`.
- Avoid starting all services simultaneously on first boot. Prefer staggered startup
  (`depends_on` with `condition: service_healthy`) to avoid CPU contention during the
  cold-start of a dozen services at once on a 2-core host.
- Never run fault injection and a high-intensity load generator at the same time without an
  explicit reason — this can cause host-level CPU starvation that confounds results (it
  becomes unclear whether an observed failure is due to the injected fault or to host
  resource contention).

Common commands:
```bash
docker compose -f research/compose/docker-compose.yml up -d
docker compose -f research/compose/docker-compose.yml ps
docker compose -f research/compose/docker-compose.yml logs -f <service>
```

## Code conventions

- Python for all scripts (fault injection wrapper, ground truth logger, agent
  orchestration). Follow PEP8, use type hints.
- Any script that produces experiment data must write structured output (JSON/CSV), not
  just print to stdout — this becomes the ground truth dataset.
- Future fault executions must follow `research/docs/FAULT_INJECTION.md`, use the
  RCAEval-aligned identifiers in `research/docs/FAULT_TAXONOMY.md`, and write versioned
  ground-truth and metadata artifacts under `research/experiments/`; do not add direct Pumba
  scripts outside the `research/generators/` interface.

```json
{
  "experiment_id": "string",
  "scenario_id": "string",
  "root_cause_service": "string",
  "fault_category": "resource|network|code_level",
  "fault_type": "string",
  "target_service": "string",
  "injection_start": "ISO8601",
  "injection_end": "ISO8601",
  "severity": "low|medium|high"
}
```

- Don't hardcode observability endpoints/ports in multiple places — centralize them in
  `research/utils/config.py` (future agent tools should import it).

## Agent (LLM) layer

- The LLM is accessed via API (not self-hosted) — see `research/docs/ARCHITECTURE.md` for
  the reasoning (homelab hardware constraints, no GPU).
- Agent tool-calling must be read-only against the observability API unless the explicit
  remediation step is enabled. Do not create tools that can execute arbitrary commands
  against containers without an allowlist.
- Store system prompts in `research/agent/prompts/`, not as long inline strings in Python
  code.

## What coding agents must NOT do without explicit user confirmation

- Increase resource limits in the compose file beyond what is documented in
  `research/docs/ARCHITECTURE.md` without recording a reason.
- Run fault scenarios that target multiple services in parallel — default to one fault per
  execution unless explicitly asked to design a cascading-fault scenario.
- Modify or delete files in `research/experiments/` — this is research data and should be
  treated as append-only once experiment execution begins.
- Commit large files (raw datasets, database dumps) to git — use `.gitignore` and store
  them elsewhere if needed.
- Introduce Kubernetes/k3s/k3d into the research workflow without an explicit request — this
  contradicts the CPU-constraint decision documented in `research/docs/ARCHITECTURE.md`
  (the upstream `kubernetes-manifests/`/`helm-chart/` are reference material only).
- Modify the upstream Online Boutique files (`src/`, `kubernetes-manifests/`, `helm-chart/`,
  `istio-manifests/`, `kustomize/`, `skaffold.yaml`, `terraform/`) or the top-level `docs/`
  as part of thesis work, unless the task explicitly requires touching Online Boutique's own
  source (e.g. targeted instrumentation).

## Validation before considering a change done

- After a compose file change: run `docker compose config` to validate syntax, then
  `docker compose up -d` and confirm all containers are `healthy`/`running` — pay attention
  to `adservice` health during cold start specifically.
- After a fault injection script change: test against a non-critical service first (e.g.
  `productcatalogservice`), verify the ground truth log is generated correctly.
- After an agent change: run it against at least one previously validated core scenario and
  compare ranked service/fault output against the withheld ground-truth fields.

## References

- `research/milestones/README.md` — phase-by-phase development roadmap and current status;
  start here to see what to work on next.
- `research/docs/ARCHITECTURE.md` — full system design and technical decision rationale.
- `research/docs/FAULT_TAXONOMY.md` — categories and types of faults covered by this
  research.
- `research/docs/EXPERIMENT_LOG.md` — template for logging experiments and evaluation
  results.
