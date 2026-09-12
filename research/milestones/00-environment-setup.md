# Phase 00 — Environment setup

## Goal

Stand up the simulated environment: Online Boutique running under Docker Compose, plus the
observability stack, both healthy and reachable.

## Prerequisites

- None — this is the first phase.

## Steps

1. **Inventory services.** List all `src/` services (12: `adservice`, `cartservice`,
   `checkoutservice`, `currencyservice`, `emailservice`, `frontend`, `loadgenerator`,
   `paymentservice`, `productcatalogservice`, `recommendationservice`, `shippingservice`,
   `shoppingassistantservice`) plus `redis-cart` (data store, not under `src/`).
2. **Assign resource limits.** For each service, set `mem_limit` and `cpus` per the target
   table in `research/docs/ARCHITECTURE.md` ("Resource allocation"). Every service added to
   the compose file must have both set — no exceptions.
3. **Assemble the compose file.** Write `research/compose/docker-compose.yml` using
   **prebuilt GCR images** (`gcr.io/google-samples/microservices-demo/<service>`) — do not
   build from `src/` Dockerfiles locally (CPU cost). Split environment-specific overrides
   into `research/compose/docker-compose.override.yml` if needed.
4. **Stagger startup.** Use `depends_on` with `condition: service_healthy` so all ~13
   containers don't cold-start simultaneously on a 2-core host.
5. **Handle `adservice` (JVM).** Cap its CPU allocation explicitly and give it a longer
   health-check start period than other services — JVM startup/GC pauses are slower than the
   Go/Python/Node services.
6. **Handle `shoppingassistantservice` (LLM-backed).** Confirm what env vars/API keys it
   needs to run (check `src/shoppingassistantservice`), document them in
   `research/compose/.env.example` (never commit real keys), and decide whether it's in
   scope for the initial environment or deferred until phase 04.
7. **Add observability stack** under `research/observability/`:
   - Prometheus (scrape config for app + cAdvisor metrics)
   - cAdvisor (container-level resource metrics)
   - Promtail → Loki (log shipping/aggregation)
   - Grafana (dashboard provisioning, import/build at least one overview dashboard)
8. **Validate.** Run `docker compose -f research/compose/docker-compose.yml config` to check
   syntax, then `up -d` and confirm every container reaches `healthy`/`running` — watch
   `adservice` specifically during cold start.

## Deliverables

- `research/compose/docker-compose.yml` (+ override file if used)
- `research/observability/` — Prometheus, Promtail, Loki, Grafana configs
- A documented `.env.example` for any required secrets (e.g. `shoppingassistantservice`'s LLM
  API key)

## Definition of done

- `docker compose config` validates with no errors.
- `docker compose ps` shows all services `healthy`/`running` after a cold start.
- Grafana (`:3000`) and Prometheus (`:9090`) are reachable and showing live container metrics.
- Frontend is reachable and a basic user flow (browse → add to cart → checkout) works.

## References

- `research/docs/ARCHITECTURE.md` — resource allocation table, `adservice`/
  `shoppingassistantservice` confounder rationale, why Compose not Kubernetes.
- `/AGENTS.md` — hardware constraints, staggered-startup rule, resource-limit rule.
