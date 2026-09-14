---
name: online-boutique-archify
description: Review and plan the Online Boutique thesis architecture, experiment workflow, observability boundaries, RCA agent, remediation safety, and Mermaid diagrams from the repository's actual research sources. Use for architecture overviews, change-impact reviews, implementation plans, and architecture diagrams; do not use for routine isolated code edits.
metadata:
  short-description: Analyze Online Boutique research architecture
---

# Online Boutique Archify

Provide evidence-backed architecture guidance for this repository's thesis scaffold. This
skill is advisory: it may inspect files and run read-only validation, but it must not edit
code, Compose configuration, experiment data, or upstream Online Boutique files unless the
user separately requests implementation outside the skill workflow.

## Grounding order

Work from the repository root. Before making an architecture claim, read the applicable
sources of truth:

1. `AGENTS.md` for repository boundaries and safety constraints.
2. `research/docs/ARCHITECTURE.md` for system decisions and hardware limitations.
3. `research/milestones/README.md` and the relevant phase file for current status.
4. `research/compose/docker-compose.yml` and `research/service/metadata.yaml` for the
   runtime topology, service roles, resource limits, dependencies, and targetability.
5. The relevant implementation modules under `research/service/`, `observer/`,
   `orchestrator/`, `generators/`, `utils/`, and `agent/`.
6. `research/docs/FAULT_TAXONOMY.md`, `research/docs/FAULT_INJECTION.md`, and
   `research/experiments/ground-truth.schema.json` for experiment and evidence contracts.

Treat runtime Python code, Compose configuration, metadata, and JSON schema as implemented
behavior. Treat roadmap prose and deferred scenarios as intent, not as proof that a feature
exists or an experiment has been validated.

## Repository invariants

Keep these explicit in reviews and plans:

- Docker Compose is the research runtime. Do not introduce Kubernetes, Helm, k3s, k3d, or
  Kubernetes-only fault categories.
- Every Compose service needs explicit `mem_limit` and `cpus`; do not silently raise limits
  beyond the documented hardware budget.
- `adservice` JVM startup/GC CPU spikes and `shoppingassistantservice` LLM-generated text
  are known confounders and must be flagged rather than misclassified automatically.
- Faults use normalized resource, network, and code-level taxonomy identifiers. Delivery
  tools such as Pumba are mechanisms, not RCA labels.
- Experiments default to one primary fault and one target. Fault execution requires an
  explicit gate and must produce append-only structured artifacts.
- `loadgenerator` is a workload service, not a fault target. A visible `frontend` symptom
  is not automatically the root cause.
- Observer and agent telemetry access is read-only. Remediation is recommendation-only by
  default; any future execution requires an explicit allowlist and gate.
- Ground truth, injection details, and scenario identifiers must remain withheld from RCA
  agent input during evaluation.
- Upstream `src/`, Kubernetes manifests, Helm, Istio, Kustomize, Skaffold, Terraform, and
  top-level `docs/` are reference material and stay untouched for thesis changes.

## Operating modes

Select the smallest mode that answers the request.

### Architecture overview

Report the active system boundaries and data flow: Online Boutique services, Compose,
Prometheus/Loki/Jaeger/Grafana, Docker state, fault generators, append-only artifacts,
RCA agent, remediation recommendation, and evaluation. Include service dependencies,
targetability, resource/confounder warnings, current milestone status, and explicit
deferred or unavailable components.

### Change-impact review

For a proposed diff or feature, identify affected boundaries across runtime, service state,
observer, generator, orchestrator, artifacts, agent, and evaluation. Report findings by
severity with the violated invariant, affected experiment scenario, concrete correction,
and missing validation. Distinguish implemented behavior from planned behavior and warn
when a change touches protected upstream or append-only experiment data.

### Implementation plan

Produce a decision-complete plan covering behavior, module boundaries, interfaces, data
flow, structured artifacts, safety gates, compatibility, failure modes, and test scenarios.
Do not invent new fault fields, remediation actions, or telemetry semantics when existing
documents leave them unresolved; mark those points as discovery gates.

### Diagram generation

Read `references/diagram-catalog.md` and choose only diagrams that clarify the request.
Use stable service names and fault taxonomy identifiers as Mermaid identifiers, readable
labels, and clear annotations for read-only paths, explicit mutation gates, parallelism,
joins, recovery, and evaluation. State whether the diagram reflects the current runtime,
the proposed design, or both.

## Required output shape

Lead with the decision or finding. Then include only the sections relevant to the request:

- affected boundaries;
- repository evidence and current-state facts;
- invariants, risks, and discovery gates;
- Mermaid diagram(s), when requested;
- implementation or validation plan;
- test scenarios and acceptance criteria.

Never expose secrets, PII, raw sensitive experiment content, storage keys, or presigned
URLs. Avoid presenting a catalogued scenario as a completed validation run.
