"""Compose-backed read-only environment for replay and live sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
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
        return self._capture_window(window)[kind]

    def _capture_window(self, window: str) -> dict[str, Any]:
        if self.run_dir is not None:
            phase = window if window in {"baseline", "incident", "recovery"} else "incident"
            payloads: dict[str, Any] = {}
            for kind in ("metrics", "logs", "alerts", "traces"):
                path = self.run_dir / phase / f"{kind}.json"
                try:
                    payloads[kind] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise EnvironmentError(f"Unable to read replay evidence: {path}") from error
            return payloads
        if window == "baseline":
            duration = (self.end - self.start) if self.end else timedelta(minutes=1)
            query_start, query_end = self.start - duration, self.start
        elif window == "recovery":
            raise EnvironmentError("Recovery telemetry is unavailable before fault recovery")
        else:
            query_start, query_end = self.start, self.end or datetime.now(UTC)
        try:
            return Observer(self.config).capture(query_start, query_end)
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

    @staticmethod
    def _metric_values(payload: dict[str, Any], query_name: str) -> dict[str, list[float]]:
        """Return numeric Prometheus values grouped by Compose service."""
        result = payload.get("queries", {}).get(query_name, {}).get("data", {}).get("result", [])
        grouped: dict[str, list[float]] = {}
        for series in result if isinstance(result, list) else []:
            service = series.get("metric", {}).get("service")
            values = series.get("values", [])
            if not isinstance(service, str) or not isinstance(values, list):
                continue
            numeric = []
            for value in values:
                try:
                    numeric.append(float(value[1]))
                except (IndexError, TypeError, ValueError):
                    continue
            if numeric:
                grouped[service] = numeric
        return grouped

    @staticmethod
    def _log_streams(payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = payload.get("result", {}).get("data", {}).get("result", [])
        return result if isinstance(result, list) else []

    @staticmethod
    def _log_text(value: Any) -> str:
        if not isinstance(value, list) or len(value) < 2:
            return ""
        raw = str(value[1])
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(decoded, dict):
            message = decoded.get("message")
            return str(message) if message is not None else raw
        return raw

    @classmethod
    def _error_log_summary(
        cls, payload: dict[str, Any], service: str | None = None
    ) -> dict[str, Any]:
        pattern = re.compile(
            r"\b(error|exception|panic|timeout|timed out|deadline exceeded|"
            r"failed|failure|fatal|5\d\d)\b",
            re.IGNORECASE,
        )
        by_service: dict[str, dict[str, Any]] = {}
        for stream in cls._log_streams(payload):
            labels = stream.get("stream", {})
            stream_service = labels.get("compose_service")
            if not isinstance(stream_service, str) or (service and stream_service != service):
                continue
            detected_level = str(labels.get("detected_level", "")).lower()
            records = by_service.setdefault(stream_service, {"count": 0, "samples": []})
            for value in stream.get("values", []):
                text = cls._log_text(value)
                if detected_level not in {"error", "fatal", "critical"} and not pattern.search(text):
                    continue
                records["count"] += 1
                if len(records["samples"]) < 3:
                    records["samples"].append(text[:500])
        return by_service

    @staticmethod
    def _trace_summary(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Summarize Jaeger errors and latency by queried service."""
        summary: dict[str, dict[str, Any]] = {}
        services = payload.get("services", {})
        if not isinstance(services, dict):
            return summary
        for service, response in services.items():
            if not isinstance(response, dict):
                continue
            data = response.get("data", [])
            spans: list[dict[str, Any]] = []
            for trace in data if isinstance(data, list) else []:
                if isinstance(trace, dict) and isinstance(trace.get("spans"), list):
                    spans.extend(span for span in trace["spans"] if isinstance(span, dict))
            errors = 0
            max_latency_ms = 0.0
            for span in spans:
                try:
                    max_latency_ms = max(max_latency_ms, float(span.get("duration", 0)) / 1000)
                except (TypeError, ValueError):
                    pass
                tags = {tag.get("key"): tag.get("value") for tag in span.get("tags", [])}
                status = str(tags.get("otel.status_code", "")).upper()
                rpc_status = str(tags.get("rpc.response.status_code", "")).upper()
                if status in {"ERROR", "ERROR_STATUS"} or rpc_status not in {"", "OK", "0"}:
                    errors += 1
            if spans:
                summary[str(service)] = {
                    "span_count": len(spans),
                    "error_count": errors,
                    "max_latency_ms": round(max_latency_ms, 3),
                }
        return summary

    def get_incident_summary(self, window: str = "incident") -> str:
        """Return compact, ranked anomalies without exposing evaluator labels."""
        incident_capture = self._capture_window(window)
        baseline_capture = self._capture_window("baseline")
        incident = incident_capture["metrics"]
        baseline = baseline_capture["metrics"]
        incident_logs = self._error_log_summary(incident_capture["logs"])
        baseline_logs = self._error_log_summary(baseline_capture["logs"])
        incident_traces = self._trace_summary(incident_capture["traces"])
        baseline_traces = self._trace_summary(baseline_capture["traces"])

        cpu_incident = self._metric_values(incident, "container_cpu")
        cpu_baseline = self._metric_values(baseline, "container_cpu")
        memory_incident = self._metric_values(incident, "container_memory")
        memory_baseline = self._metric_values(baseline, "container_memory")
        services = (
            set(cpu_incident)
            | set(cpu_baseline)
            | set(memory_incident)
            | set(memory_baseline)
            | set(incident_logs)
            | set(baseline_logs)
            | set(incident_traces)
            | set(baseline_traces)
        )
        anomalies: list[dict[str, Any]] = []
        for service in services:
            cpu_i = cpu_incident.get(service, [])
            cpu_b = cpu_baseline.get(service, [])
            memory_i = memory_incident.get(service, [])
            memory_b = memory_baseline.get(service, [])
            cpu_max_i = max(cpu_i, default=0.0)
            cpu_max_b = max(cpu_b, default=0.0)
            memory_max_i = max(memory_i, default=0.0)
            memory_max_b = max(memory_b, default=0.0)
            errors_i = incident_logs.get(service, {}).get("count", 0)
            errors_b = baseline_logs.get(service, {}).get("count", 0)
            traces_i = incident_traces.get(service, {})
            traces_b = baseline_traces.get(service, {})
            cpu_delta = max(0.0, cpu_max_i - cpu_max_b) if cpu_b else cpu_max_i
            memory_delta = max(0.0, memory_max_i - memory_max_b) if memory_b else memory_max_i
            error_delta = max(0, errors_i - errors_b)
            trace_error_delta = max(
                0, traces_i.get("error_count", 0) - traces_b.get("error_count", 0)
            )
            memory_score = memory_delta / memory_max_b if memory_b and memory_max_b else 0.0
            score = cpu_delta + memory_score
            score += min(error_delta, 10) * 0.5 + min(trace_error_delta, 10) * 0.5
            anomalies.append(
                {
                    "service": service,
                    "score": round(score, 6),
                    "signals": {
                        "cpu_baseline_max": round(cpu_max_b, 6),
                        "cpu_incident_max": round(cpu_max_i, 6),
                        "cpu_delta": round(cpu_delta, 6),
                        "memory_baseline_max": round(memory_max_b, 3),
                        "memory_incident_max": round(memory_max_i, 3),
                        "error_baseline_count": errors_b,
                        "error_incident_count": errors_i,
                        "trace_error_baseline_count": traces_b.get("error_count", 0),
                        "trace_error_incident_count": traces_i.get("error_count", 0),
                        "trace_max_latency_ms": traces_i.get("max_latency_ms", 0.0),
                    },
                }
            )
        anomalies.sort(key=lambda item: (-item["score"], item["service"]))
        output = {
            "schema_version": 1,
            "window": window,
            "baseline_window": "baseline",
            "data_quality": {
                "incident_cpu_samples": sum(len(values) for values in cpu_incident.values()),
                "baseline_cpu_samples": sum(len(values) for values in cpu_baseline.values()),
                "incident_services": len(services),
            },
            "top_anomalies": anomalies[:8],
        }
        return f"incident_summary: {json.dumps(output, sort_keys=True)}"

    def get_error_logs(self, service: str | None = None, window: str = "incident") -> str:
        output = {
            "schema_version": 1,
            "window": window,
            "services": self._error_log_summary(self._payload("logs", window), service),
        }
        return f"error_logs: {json.dumps(output, sort_keys=True)}"

    def get_metrics(self, service: str | None = None, window: str = "incident") -> str:
        return f"metrics window={window}: {self._compact(self._payload('metrics', window), service)}"

    def get_logs(self, service: str | None = None, window: str = "incident") -> str:
        return f"logs window={window}: {self._compact(self._payload('logs', window), service)}"

    def get_traces(self, service: str | None = None, window: str = "incident") -> str:
        return f"traces window={window}: {self._compact(self._payload('traces', window), service)}"

    def get_alerts(self, window: str = "incident") -> str:
        return f"alerts window={window}: {self._compact(self._payload('alerts', window), None)}"
