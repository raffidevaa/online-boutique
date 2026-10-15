"""Compose-backed read-only environment for replay and live sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from research.observer import Observer, ObserverError
from research.utils import ResearchConfig


class EnvironmentError(RuntimeError):
    """Raised when telemetry cannot be read safely."""


class ComposeEvidence:
    """Expose bounded telemetry observations without Docker or shell access."""

    def __init__(
        self,
        config: ResearchConfig,
        start: datetime,
        end: datetime | None,
        run_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.start = start
        self.end = end
        self.run_dir = run_dir
        self._live: dict[str, Any] | None = None

    def _payload(self, kind: str, window: str) -> dict[str, Any]:
        if self.run_dir is not None:
            phase = window if window in {"baseline", "incident", "recovery"} else "incident"
            path = self.run_dir / phase / f"{kind}.json"
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise EnvironmentError(f"Unable to read replay evidence: {path}") from error
        if window == "baseline":
            duration = (self.end - self.start) if self.end else timedelta(minutes=1)
            query_start, query_end = self.start - duration, self.start
        elif window == "recovery":
            raise EnvironmentError("Recovery telemetry is unavailable before fault recovery")
        else:
            query_start, query_end = self.start, self.end or datetime.now(UTC)
        try:
            return Observer(self.config).capture(query_start, query_end)[kind]
        except ObserverError as error:
            raise EnvironmentError(str(error)) from error

    @staticmethod
    def _compact(payload: Any, service: str | None) -> str:
        if service and isinstance(payload, dict):
            if "queries" in payload:
                filtered_queries: dict[str, Any] = {}
                for name, response in payload["queries"].items():
                    result = response.get("data", {}).get("result", [])
                    selected = [
                        item for item in result
                        if item.get("metric", {}).get("service") == service
                    ]
                    filtered_queries[name] = {"data": {"result": selected}}
                payload = {"queries": filtered_queries}
            elif "result" in payload:
                result = payload.get("result", {}).get("data", {}).get("result", [])
                payload = {
                    "result": {
                        "data": {
                            "result": [
                                item for item in result
                                if item.get("stream", {}).get("compose_service") == service
                            ]
                        }
                    }
                }
            elif "services" in payload:
                payload = {"services": {service: payload["services"].get(service, {})}}
        encoded = json.dumps(payload, sort_keys=True)
        return encoded if len(encoded) <= 8_000 else encoded[:8_000] + "...[truncated]"

    def get_metrics(self, service: str | None = None, window: str = "incident") -> str:
        return f"metrics window={window}: {self._compact(self._payload('metrics', window), service)}"

    def get_logs(self, service: str | None = None, window: str = "incident") -> str:
        return f"logs window={window}: {self._compact(self._payload('logs', window), service)}"

    def get_traces(self, service: str | None = None, window: str = "incident") -> str:
        return f"traces window={window}: {self._compact(self._payload('traces', window), service)}"

    def get_alerts(self, window: str = "incident") -> str:
        return f"alerts window={window}: {self._compact(self._payload('alerts', window), None)}"
