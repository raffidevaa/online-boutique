"""Read-only Prometheus and Loki observation for experiment windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from research.utils.config import ResearchConfig


class ObserverError(RuntimeError):
    """A required observability backend is unavailable or returned invalid data."""


def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    try:
        with urlopen(f"{url}{query}", timeout=10) as response:  # nosec B310: local configuration
            return json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ObserverError(f"Unable to query {url}: {error}") from error


class Observer:
    """Collect bounded, read-only metrics, logs, alerts, and runtime evidence."""

    def __init__(self, config: ResearchConfig) -> None:
        self.prometheus_url = config.prometheus_url.rstrip("/")
        self.loki_url = config.loki_url.rstrip("/")
        self.limit = config.telemetry_limit

    def _check_endpoint(self, url: str, name: str) -> None:
        try:
            with urlopen(url, timeout=10) as response:  # nosec B310: local configuration
                if response.status != 200:
                    raise ObserverError(f"{name} is not ready")
        except (URLError, TimeoutError) as error:
            raise ObserverError(f"{name} is unavailable: {error}") from error

    def check_ready(self) -> None:
        self._check_endpoint(f"{self.prometheus_url}/-/ready", "Prometheus")
        self._check_endpoint(f"{self.loki_url}/ready", "Loki")

    def capture(self, start: datetime, end: datetime) -> dict[str, Any]:
        metric_queries = {
            "services_up": "up",
            "container_cpu": "sum by (service) (rate(container_cpu_usage_seconds_total[1m]))",
            "container_memory": "sum by (service) (container_memory_working_set_bytes)",
            "host_memory_available": "node_memory_MemAvailable_bytes",
        }
        metrics = {
            "schema_version": 1,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "queries": {
                name: _get_json(
                    f"{self.prometheus_url}/api/v1/query_range",
                    {
                        "query": expression,
                        "start": start.timestamp(),
                        "end": end.timestamp(),
                        "step": "10s",
                    },
                )
                for name, expression in metric_queries.items()
            },
        }
        logs = {
            "schema_version": 1,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "query": '{compose_service=~".+"}',
            "result": _get_json(
                f"{self.loki_url}/loki/api/v1/query_range",
                {
                    "query": '{compose_service=~".+"}',
                    "start": int(start.timestamp() * 1_000_000_000),
                    "end": int(end.timestamp() * 1_000_000_000),
                    "limit": self.limit,
                    "direction": "forward",
                },
            ),
        }
        alerts = {
            "schema_version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "alerts": _get_json(f"{self.prometheus_url}/api/v1/alerts"),
        }
        return {"metrics": metrics, "logs": logs, "alerts": alerts}

    def capture_recent(
        self, destination: Path, duration: timedelta = timedelta(minutes=1)
    ) -> None:
        end = datetime.now(UTC)
        data = self.capture(end - duration, end)
        destination.mkdir(parents=True, exist_ok=False)
        for name, value in data.items():
            (destination / f"{name}.json").write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
