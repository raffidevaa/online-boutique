"""Read-only Prometheus and Loki observation for experiment windows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any
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
    except (OSError, TimeoutError, json.JSONDecodeError) as error:
        raise ObserverError(f"Unable to query {url}: {error}") from error


class Observer:
    """Collect bounded, read-only metrics, logs, alerts, and runtime evidence."""

    def __init__(self, config: ResearchConfig) -> None:
        self.prometheus_url = config.prometheus_url.rstrip("/")
        self.loki_url = config.loki_url.rstrip("/")
        self.jaeger_url = config.jaeger_url.rstrip("/")
        self.limit = config.telemetry_limit
        self.trace_limit = config.trace_limit
        self.trace_services = config.trace_services

    def _check_endpoint(self, url: str, name: str) -> None:
        try:
            with urlopen(url, timeout=10) as response:  # nosec B310: local configuration
                if response.status != 200:
                    raise ObserverError(f"{name} is not ready")
        except (OSError, TimeoutError) as error:
            raise ObserverError(f"{name} is unavailable: {error}") from error

    CONTAINER_CPU_QUERY = (
        'sum by (service) (rate(container_cpu_usage_seconds_total{job="cadvisor",'
        'name!=""}[1m]) * on(name) group_left(service) '
        'compose_container_info{service!=""})'
    )
    CONTAINER_MEMORY_QUERY = (
        'sum by (service) (container_memory_working_set_bytes{job="cadvisor",'
        'name!=""} * on(name) group_left(service) '
        'compose_container_info{service!=""})'
    )

    def check_ready(self) -> dict[str, int]:
        self._check_endpoint(f"{self.prometheus_url}/-/ready", "Prometheus")
        self._check_endpoint(f"{self.loki_url}/ready", "Loki")
        self._check_endpoint(f"{self.jaeger_url}/api/services", "Jaeger")
        mapping = self._query_instant("compose_container_info{service!=\"\"}")
        cpu = self._query_instant(self.CONTAINER_CPU_QUERY)
        memory = self._query_instant(self.CONTAINER_MEMORY_QUERY)
        counts = {
            "container_metadata_series": len(mapping),
            "container_cpu_series": len(cpu),
            "container_memory_series": len(memory),
        }
        if not all(counts.values()):
            missing = ", ".join(name for name, count in counts.items() if not count)
            raise ObserverError(f"Container telemetry mapping is not ready: {missing}")
        return counts

    def _query_instant(self, expression: str) -> list[dict[str, Any]]:
        result = _get_json(
            f"{self.prometheus_url}/api/v1/query", {"query": expression}
        )
        if result.get("status") != "success":
            raise ObserverError(f"Prometheus query failed: {expression}")
        data = result.get("data", {})
        values = data.get("result", [])
        if not isinstance(values, list):
            raise ObserverError(f"Prometheus returned invalid result: {expression}")
        return values

    def capture(self, start: datetime, end: datetime) -> dict[str, Any]:
        metric_queries = {
            "services_up": "up",
            "container_cpu": self.CONTAINER_CPU_QUERY,
            "container_memory": self.CONTAINER_MEMORY_QUERY,
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
        traces = {
            "schema_version": 1,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "services": {
                service: _get_json(
                    f"{self.jaeger_url}/api/traces",
                    {
                        "service": service,
                        "start": int(start.timestamp() * 1_000_000),
                        "end": int(end.timestamp() * 1_000_000),
                        "limit": self.trace_limit,
                    },
                )
                for service in self.trace_services
            },
        }
        return {"metrics": metrics, "logs": logs, "alerts": alerts, "traces": traces}

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
