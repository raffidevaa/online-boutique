# Reference architecture adaptation

This framework is inspired by, and is not compatible with, the reference projects below.
It deliberately adapts only concepts useful for a constrained Docker Compose research runtime.

| Reference | Original concept | Adopted concept | Docker Compose adaptation | Excluded parts | Reason |
|---|---|---|---|---|---|
| [AIOpsLab](https://github.com/microsoft/AIOpsLab) | Service/application abstraction | `research/service/` | Docker Compose commands, service metadata, and Compose-label-based inspection | Helm, kubectl, namespaces, Pods | The thesis runtime is Compose-only. |
| AIOpsLab | Orchestrator and session | `research/orchestrator/`, `research/agent/`, and append-only artifacts | Compose lifecycle plus replay/integrated-live agent sessions with a constrained action parser | Helm, kubectl, namespaces, Pods | The thesis runtime is Compose-only and the agent must not execute arbitrary commands. |
| AIOpsLab | Generators | `research/generators/` | Versioned scenario catalog plus allowlisted Pumba and faulty-image adapters | Kubernetes Jobs and wrk deployment | Existing low-load Compose service is sufficient. |
| AIOpsLab | Observer | `research/observer/` | Read-only Prometheus, Loki, Jaeger traces, and alerts adapters; `service/` supplies Docker state | Cluster-specific observability | Docker state is the relevant runtime evidence. |
| AIOpsLab | Utils | `research/utils/` | Central configuration and append-only artifact helpers | Upstream session/cache helpers | Only active local utilities are retained. |
| [Cloud-OpsBench](https://github.com/LLM4Ops/Cloud-OpsBench) | Benchmark case metadata and ground truth | Versioned experiment/artifact schemas | Future runs retain scenario, ground truth, and environment separately | Live Kubernetes dependency | Artifacts must remain replayable without Kubernetes. |
| Cloud-OpsBench | `k8s_states.json` | `docker_state.json` | Compose service/container state, health, limits, networks, and config hash | Kubernetes objects | Docker provides the relevant state model. |
| Cloud-OpsBench | Raw metrics/logs/alerts | Telemetry envelopes | Time-window captures from Prometheus and Loki | Released benchmark corpus | This project collects its own controlled runs. |
| Cloud-OpsBench | Process labels and golden trajectories | Agent-session traces and deferred evaluation inputs | Sessions record bounded tool use, submission status, and task scores; larger benchmark evaluation remains future work | Process scoring and reference trajectories | The RCA loop exists, but full-dataset evaluation is not yet complete. |
| RCAEval | Resource/network/code-level taxonomy and ground-truth RCA evaluation | Six-case core taxonomy plus candidate backlog | Compose-specific targets, workload, intensity, and propagation hypotheses | Official RCAEval cases and infrastructure assumptions | The thesis adapts the research idea, not the benchmark corpus. |

## Deliberate boundaries

- Docker Compose remains the only runtime; no Kubernetes, Helm, or cluster fault categories are
  introduced.
- Scenario and injector adapters remain allowlisted: one target and one active runtime fault at
  a time; no arbitrary container command is accepted from an agent or scenario.
- Telemetry adapters and agent tools are read-only. The future remediation layer must remain
  separately gated.
