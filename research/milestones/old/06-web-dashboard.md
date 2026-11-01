# Phase 07 — Web RCA Dashboard and Experiment Control

## Goal

Provide a private browser dashboard for observing RCA agent actions, running one allowlisted
runtime fault at a time, and reviewing RCA performance from the Phase 06 evaluation artifacts.

## Prerequisites

- Phase 06 done — canonical evaluation summary exists.
- Docker Compose and telemetry are available on the experiment host.
- FastAPI and Uvicorn dependencies are installed from `research/requirements.txt`.

## Deliverables

- `research/web/` FastAPI application and static dashboard.
- Single-flight job manager with explicit confirmation and sanitized SSE trace events.
- Live run agent session artifact and job history.
- API and security tests in `research/tests/test_web.py`.

## Definition of done

- Browser can show Phase 06 runtime/code-level performance breakdowns.
- Browser can list the allowlisted fault catalog and disable non-runnable scenarios.
- A runtime scenario requires successful preflight, confirmation nonce, and one active job.
- Agent action/observation events are streamed without exposing ground truth before submission.
- Failed jobs remain visible and existing regression tests remain green.

## Run

```bash
source .venv/bin/activate
python -m research.web --host 0.0.0.0 --port 8000
```

The GUI prepares the required `loadgenerator` profile before the explicit fault confirmation.
It does not execute remediation. For a shared or public network, configure
`RESEARCH_GUI_AUTH_TOKEN` before exposing the host port.
