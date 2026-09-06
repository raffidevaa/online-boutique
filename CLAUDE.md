# CLAUDE.md

Follow all guidance in `AGENTS.md` — it is the single source of truth for this project's
conventions (repo structure, environment, code conventions, agent restrictions, and
validation steps).

Claude Code specific notes:

- Before running `docker compose` commands or fault injection scripts, summarize what will
  be executed first — especially for any command that injects a fault into more than one
  service at once.
- When reading or writing files under `research/fault-injection/logs/` or
  `research/experiments/`, treat them as append-only per `AGENTS.md` — do not overwrite
  without confirmation.
- For questions about architecture or design rationale (e.g. why Docker Compose instead of
  Kubernetes/k3s/k3d despite this repo already shipping k8s manifests, why Online Boutique,
  why API-based LLM), check `research/docs/ARCHITECTURE.md` first rather than assuming or
  guessing.
- Remember the target hardware is CPU-constrained (2 core/4 thread). Flag any change that
  could meaningfully increase steady-state or startup CPU load, particularly around
  `adservice` (JVM) and `shoppingassistantservice` (LLM calls).
- The top-level `docs/` and root `README.md` belong to the upstream Online Boutique project
  — don't edit them for thesis-related changes; use `research/docs/` and `research/README.md`
  instead.
