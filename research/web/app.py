"""FastAPI application for the private RCA experiment dashboard."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from research.utils import ResearchConfig
from research.observer import Observer
from research.service import ComposeApplication
from research.agent.sanitize import sanitize_text

from .jobs import GuiJobError, JobManager


STATIC_DIR = Path(__file__).with_name("static")


def _require_auth(config: ResearchConfig, authorization: str | None) -> None:
    if not config.gui_auth_token:
        return
    expected = f"Bearer {config.gui_auth_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="GUI authentication required")


def create_app(config: ResearchConfig | None = None) -> FastAPI:
    config = config or ResearchConfig.from_environment()
    manager = JobManager(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.close()

    app = FastAPI(
        title="Online Boutique RCA Dashboard",
        version="1.0",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.jobs = manager

    @app.exception_handler(GuiJobError)
    async def gui_error_handler(_: Request, error: GuiJobError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": str(error)})

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/static/{asset:path}")
    async def static_asset(asset: str) -> Response:
        path = (STATIC_DIR / asset).resolve()
        if STATIC_DIR.resolve() not in path.parents:
            raise HTTPException(status_code=404, detail="asset not found")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        media_type = "text/plain"
        if path.suffix == ".css":
            media_type = "text/css"
        elif path.suffix == ".js":
            media_type = "application/javascript"
        return Response(path.read_bytes(), media_type=media_type)

    @app.get("/api/health")
    async def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_auth(config, authorization)
        runtime: dict[str, Any] = {"status": "unknown"}
        telemetry: dict[str, Any] = {"status": "unknown"}
        try:
            states = ComposeApplication(config).validate_readiness()
            runtime = {
                "status": "ready",
                "healthy_services": sum(
                    state.health in (None, "healthy") for state in states.values()
                ),
                "service_count": len(states),
            }
        except Exception as error:
            runtime = {"status": "unavailable", "error": sanitize_text(str(error))}
        try:
            Observer(config).check_ready()
            telemetry = {"status": "ready"}
        except Exception as error:
            telemetry = {"status": "unavailable", "error": sanitize_text(str(error))}
        return {
            "status": "ok",
            "provider": config.aiops_llm_provider,
            "prometheus_url": config.prometheus_url,
            "loki_url": config.loki_url,
            "jaeger_url": config.jaeger_url,
            "runtime": runtime,
            "telemetry": telemetry,
            "active_jobs": len([job for job in manager.list_jobs() if job["status"] in {"prepared", "queued", "running"}]),
        }

    @app.get("/api/scenarios")
    async def scenarios(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_auth(config, authorization)
        return {"scenarios": manager.scenarios()}

    @app.post("/api/jobs/prepare")
    async def prepare(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_auth(config, authorization)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        try:
            return manager.prepare(
                str(body.get("scenario_id", "")),
                str(body.get("task", "analysis")),
                int(body.get("max_steps", 15)),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/jobs/{job_id}/start")
    async def start(
        job_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_auth(config, authorization)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("confirmation_nonce"), str):
            raise HTTPException(status_code=400, detail="confirmation_nonce is required")
        return {"job": manager.start(job_id, body["confirmation_nonce"])}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(
        job_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_auth(config, authorization)
        return {"job": manager.cancel(job_id)}

    @app.get("/api/jobs")
    async def jobs(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_auth(config, authorization)
        return {"jobs": manager.list_jobs()}

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_auth(config, authorization)
        return {"job": manager.get(job_id).public()}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        _require_auth(config, authorization)
        manager.get(job_id)
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError as error:
            raise HTTPException(status_code=400, detail="after must be an integer") from error

        async def stream():
            sequence = max(0, after)
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(manager.wait_for_events, job_id, sequence, 10.0)
                if events:
                    for event in events:
                        yield f"data: {__import__('json').dumps(event, sort_keys=True)}\n\n"
                    sequence = events[-1]["sequence"] + 1
                else:
                    yield ": heartbeat\n\n"
                if manager.get(job_id).status in {"completed", "failed"} and not manager.events_after(job_id, sequence):
                    return

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/runs")
    async def runs(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_auth(config, authorization)
        return {"runs": manager.list_runs()}

    @app.get("/api/runs/{experiment_id}")
    async def run_detail(
        experiment_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_auth(config, authorization)
        return manager.run_detail(experiment_id)

    @app.get("/api/metrics/summary")
    async def metrics(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _require_auth(config, authorization)
        path = config.evaluations_dir / "phase-06-final" / "final-summary.json"
        if not path.is_file():
            return {"available": False, "error": "Phase 06 summary not found"}
        import json

        return {"available": True, "summary": json.loads(path.read_text(encoding="utf-8"))}

    return app
