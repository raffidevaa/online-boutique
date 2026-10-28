"""Compose-backed read-only environment for replay and live sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from typing import Any

import yaml

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
        self._targetable_services_cache: set[str] | None = None
        self._targetable_service_ports_cache: dict[str, set[str]] | None = None

    def _targetable_services(self) -> set[str]:
        """Return application services that are valid root-cause candidates."""
        if self._targetable_services_cache is not None:
            return self._targetable_services_cache
        try:
            raw = yaml.safe_load(self.config.service_metadata.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise EnvironmentError(
                f"Unable to read service metadata: {self.config.service_metadata}"
            ) from error
        services = raw.get("services", {}) if isinstance(raw, dict) else {}
        if not isinstance(services, dict):
            raise EnvironmentError("Service metadata must contain a services mapping")
        targetable = {
            str(name)
            for name, metadata in services.items()
            if isinstance(metadata, dict) and metadata.get("targetable") is True
        }
        if not targetable:
            raise EnvironmentError("Service metadata contains no targetable services")
        self._targetable_services_cache = targetable
        return targetable

    def get_targetable_services(self) -> set[str]:
        """Return application services that may be named in a recommendation."""
        return set(self._targetable_services())

    def _targetable_service_ports(self) -> dict[str, set[str]]:
        """Return targetable services and their configured endpoint ports."""
        if self._targetable_service_ports_cache is not None:
            return self._targetable_service_ports_cache
        # Populate and validate the same metadata source used for candidate filtering.
        targetable = self._targetable_services()
        try:
            raw = yaml.safe_load(self.config.service_metadata.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise EnvironmentError(
                f"Unable to read service metadata: {self.config.service_metadata}"
            ) from error
        services = raw.get("services", {}) if isinstance(raw, dict) else {}
        ports: dict[str, set[str]] = {}
        for name in targetable:
            metadata = services.get(name, {}) if isinstance(services, dict) else {}
            port = metadata.get("port") if isinstance(metadata, dict) else None
            if port is not None:
                ports[name] = {str(port)}
            else:
                ports[name] = set()
        self._targetable_service_ports_cache = ports
        return ports

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
    def _percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
        return ordered[index]

    @staticmethod
    def _span_tags(span: dict[str, Any]) -> dict[str, Any]:
        return {
            tag.get("key"): tag.get("value")
            for tag in span.get("tags", [])
            if isinstance(tag, dict) and isinstance(tag.get("key"), str)
        }

    @staticmethod
    def _rpc_service_name(operation: Any, tags: dict[str, Any]) -> str | None:
        """Extract a logical peer service from an RPC operation name."""
        value = tags.get("rpc.method") or operation
        if isinstance(value, str) and "/" not in value:
            value = operation
        if not isinstance(value, str) or "/" not in value:
            return None
        service_name = value.split("/", 1)[0].rsplit(".", 1)[-1]
        if not service_name.casefold().endswith("service"):
            return None
        return service_name.casefold()

    @classmethod
    def _peer_service(
        cls,
        span: dict[str, Any],
        tags: dict[str, Any],
        service_ports: dict[str, set[str]],
    ) -> str | None:
        rpc_name = cls._rpc_service_name(span.get("operationName"), tags)
        if rpc_name:
            for service in service_ports:
                if service.casefold() == rpc_name:
                    return service
        port = tags.get("server.port")
        if port is None:
            return None
        matches = [
            service for service, ports in service_ports.items() if str(port) in ports
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _network_signature(
        incident_p50: float,
        baseline_p50: float,
        error_delta: int,
        outlier_rate: float,
        incident_max: float,
    ) -> tuple[str, float, float]:
        """Classify network evidence from distribution shape, not the scenario label."""
        latency_shift = max(0.0, incident_p50 - baseline_p50)
        delay_score = min(latency_shift / 100.0, 5.0)
        loss_score = min(error_delta, 10) * 0.5 + min(outlier_rate * 5.0, 5.0)
        if delay_score >= 1.0 and incident_p50 >= 100.0:
            signature = "network_delay"
        elif error_delta > 0 or (
            0.05 <= outlier_rate <= 0.30
            and incident_p50 < 25.0
            and incident_max >= 100.0
        ):
            signature = "packet_loss"
        else:
            signature = "ambiguous"
        return signature, delay_score, loss_score

    @classmethod
    def _trace_summary(
        cls,
        payload: dict[str, Any],
        service_ports: dict[str, set[str]],
    ) -> dict[str, Any]:
        """Summarize actual span services and caller-to-peer network evidence.

        Jaeger queries are grouped by the service used to search for a trace. That is
        not necessarily the service that owns each span, so this method resolves the
        process name from every span and deduplicates spans returned by multiple queries.
        """
        service_summary: dict[str, dict[str, Any]] = {}
        edge_values: dict[tuple[str, str, str], dict[str, Any]] = {}
        seen_spans: set[str] = set()
        services = payload.get("services", {})
        if not isinstance(services, dict):
            return {"services": {}, "network_edges": []}
        for queried_service, response in services.items():
            if not isinstance(response, dict):
                continue
            data = response.get("data", [])
            for trace in data if isinstance(data, list) else []:
                if not isinstance(trace, dict) or not isinstance(trace.get("spans"), list):
                    continue
                processes = trace.get("processes", {})
                for span in trace["spans"]:
                    if not isinstance(span, dict):
                        continue
                    span_id = span.get("spanID")
                    if not isinstance(span_id, str):
                        span_id = json.dumps(
                            [trace.get("traceID"), span.get("operationName"), span.get("startTime")],
                            sort_keys=True,
                        )
                    if span_id in seen_spans:
                        continue
                    seen_spans.add(span_id)
                    process = processes.get(span.get("processID"), {})
                    actual_service = (
                        process.get("serviceName")
                        if isinstance(process, dict)
                        else None
                    )
                    if not isinstance(actual_service, str):
                        actual_service = queried_service
                    tags = cls._span_tags(span)
                    try:
                        duration_ms = max(0.0, float(span.get("duration", 0)) / 1000)
                    except (TypeError, ValueError):
                        duration_ms = 0.0
                    service_data = service_summary.setdefault(
                        actual_service,
                        {"durations": [], "error_count": 0},
                    )
                    service_data["durations"].append(duration_ms)
                    status = str(tags.get("otel.status_code", "")).upper()
                    rpc_status = str(
                        tags.get("rpc.response.status_code", tags.get("rpc.grpc.status_code", ""))
                    ).upper()
                    is_error = bool(tags.get("error")) or (
                        status in {"ERROR", "ERROR_STATUS"}
                        or rpc_status not in {"", "OK", "0"}
                        or "exception.message" in tags
                    )
                    if is_error:
                        service_data["error_count"] += 1
                    if str(tags.get("span.kind", "")).lower() == "client":
                        peer = cls._peer_service(span, tags, service_ports)
                        if peer and peer != actual_service:
                            operation = str(tags.get("rpc.method") or span.get("operationName", ""))
                            key = (actual_service, peer, operation)
                            edge = edge_values.setdefault(
                                key, {"durations": [], "error_count": 0}
                            )
                            edge["durations"].append(duration_ms)
                            if is_error:
                                edge["error_count"] += 1

        compact_services: dict[str, dict[str, Any]] = {}
        for service, data in service_summary.items():
            durations = data["durations"]
            compact_services[service] = {
                "span_count": len(durations),
                "error_count": data["error_count"],
                "p50_latency_ms": round(cls._percentile(durations, 0.50), 3),
                "p95_latency_ms": round(cls._percentile(durations, 0.95), 3),
                "max_latency_ms": round(max(durations, default=0.0), 3),
            }
        compact_edges: list[dict[str, Any]] = []
        for (caller, peer, operation), data in edge_values.items():
            durations = data["durations"]
            compact_edges.append(
                {
                    "caller_service": caller,
                    "peer_service": peer,
                    "operation": operation,
                    "call_count": len(durations),
                    "error_count": data["error_count"],
                    "p50_latency_ms": round(cls._percentile(durations, 0.50), 3),
                    "p95_latency_ms": round(cls._percentile(durations, 0.95), 3),
                    "max_latency_ms": round(max(durations, default=0.0), 3),
                    "durations_ms": durations,
                }
            )
        return {"services": compact_services, "network_edges": compact_edges}

    def get_incident_summary(self, window: str = "incident") -> str:
        """Return compact, ranked anomalies without exposing evaluator labels."""
        incident_capture = self._capture_window(window)
        baseline_capture = self._capture_window("baseline")
        incident = incident_capture["metrics"]
        baseline = baseline_capture["metrics"]
        incident_logs = self._error_log_summary(incident_capture["logs"])
        baseline_logs = self._error_log_summary(baseline_capture["logs"])
        service_ports = self._targetable_service_ports()
        incident_trace_data = self._trace_summary(incident_capture["traces"], service_ports)
        baseline_trace_data = self._trace_summary(baseline_capture["traces"], service_ports)
        incident_traces = incident_trace_data["services"]
        baseline_traces = baseline_trace_data["services"]

        network_evidence: list[dict[str, Any]] = []
        baseline_edges = {
            (
                edge["caller_service"],
                edge["peer_service"],
                edge["operation"],
            ): edge
            for edge in baseline_trace_data["network_edges"]
        }
        for incident_edge in incident_trace_data["network_edges"]:
            peer = incident_edge["peer_service"]
            if peer not in service_ports:
                continue
            key = (
                incident_edge["caller_service"],
                peer,
                incident_edge["operation"],
            )
            baseline_edge = baseline_edges.get(key, {})
            incident_durations = incident_edge.get("durations_ms", [])
            incident_p50 = float(incident_edge["p50_latency_ms"])
            baseline_p50 = float(baseline_edge.get("p50_latency_ms", 0.0))
            incident_p95 = float(incident_edge["p95_latency_ms"])
            baseline_p95 = float(baseline_edge.get("p95_latency_ms", 0.0))
            incident_max = float(incident_edge["max_latency_ms"])
            latency_shift = max(0.0, incident_p50 - baseline_p50)
            outlier_threshold = max(10.0, baseline_p95 * 5.0)
            outlier_count = sum(
                duration >= outlier_threshold for duration in incident_durations
            )
            outlier_rate = outlier_count / max(len(incident_durations), 1)
            error_delta = max(
                0,
                int(incident_edge.get("error_count", 0))
                - int(baseline_edge.get("error_count", 0)),
            )
            signature, delay_score, loss_score = self._network_signature(
                incident_p50,
                baseline_p50,
                error_delta,
                outlier_rate,
                incident_max,
            )
            network_evidence.append(
                {
                    "caller_service": incident_edge["caller_service"],
                    "peer_service": peer,
                    "operation": incident_edge["operation"],
                    "call_count": incident_edge["call_count"],
                    "baseline_p50_latency_ms": round(baseline_p50, 3),
                    "incident_p50_latency_ms": round(incident_p50, 3),
                    "baseline_p95_latency_ms": round(baseline_p95, 3),
                    "incident_p95_latency_ms": round(incident_p95, 3),
                    "incident_max_latency_ms": round(incident_max, 3),
                    "latency_shift_ms": round(latency_shift, 3),
                    "error_delta": error_delta,
                    "outlier_rate": round(outlier_rate, 6),
                    "delay_score": round(delay_score, 6),
                    "loss_score": round(loss_score, 6),
                    "inferred_signature": signature,
                }
            )
        network_evidence.sort(
            key=lambda item: (
                -max(item["delay_score"], item["loss_score"]),
                item["peer_service"],
                item["caller_service"],
                item["operation"],
            )
        )
        network_scores: dict[str, float] = {}
        caller_scores: dict[str, float] = {}
        for evidence in network_evidence:
            if evidence["inferred_signature"] == "ambiguous":
                continue
            peer = evidence["peer_service"]
            score = max(evidence["delay_score"], evidence["loss_score"])
            network_scores[peer] = max(network_scores.get(peer, 0.0), score)
            caller = evidence["caller_service"]
            caller_scores[caller] = max(caller_scores.get(caller, 0.0), score)
        # A propagated service can be both a peer and a caller (frontend -> checkout
        # -> payment). Prefer the deepest affected peer when the evidence is comparable.
        for service in set(network_scores) & set(caller_scores):
            network_scores[service] *= 0.25

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
        services &= self._targetable_services()
        measurements: dict[str, dict[str, Any]] = {}
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
            cpu_relative = cpu_delta / max(cpu_max_b, 0.01) if cpu_b else cpu_delta
            memory_baseline_level = self._percentile(memory_b, 0.50)
            memory_incident_level = self._percentile(memory_i, 0.50)
            memory_delta_mb = max(
                0.0,
                memory_incident_level - memory_baseline_level,
            ) / (1024.0 * 1024.0)
            memory_score = (
                max(0.0, memory_incident_level - memory_baseline_level)
                / memory_baseline_level
                if memory_baseline_level
                else 0.0
            )
            latency_score = min(traces_i.get("p95_latency_ms", 0.0) / 1000.0, 5.0)
            error_signal = min(error_delta, 10) * 0.1
            trace_error_signal = min(trace_error_delta, 10) * 0.25
            network_signal = latency_score + error_signal + trace_error_signal
            measurements[service] = {
                "cpu_max_i": cpu_max_i,
                "cpu_max_b": cpu_max_b,
                "memory_max_i": memory_max_i,
                "memory_max_b": memory_max_b,
                "errors_i": errors_i,
                "errors_b": errors_b,
                "traces_i": traces_i,
                "traces_b": traces_b,
                "cpu_delta": cpu_delta,
                "memory_score": memory_score,
                "memory_delta_mb": memory_delta_mb,
                "memory_baseline_level": memory_baseline_level,
                "memory_incident_level": memory_incident_level,
                "error_delta": error_delta,
                "trace_error_delta": trace_error_delta,
                "cpu_relative": cpu_relative,
                "latency_score": latency_score,
                "error_signal": error_signal,
                "trace_error_signal": trace_error_signal,
                "network_signal": network_signal,
            }
        anomalies: list[dict[str, Any]] = []
        for service in services:
            measurement = measurements[service]
            cpu_max_i = measurement["cpu_max_i"]
            cpu_max_b = measurement["cpu_max_b"]
            memory_max_i = measurement["memory_max_i"]
            memory_max_b = measurement["memory_max_b"]
            errors_i = measurement["errors_i"]
            errors_b = measurement["errors_b"]
            traces_i = measurement["traces_i"]
            traces_b = measurement["traces_b"]
            cpu_delta = measurement["cpu_delta"]
            memory_score = measurement["memory_score"]
            memory_delta_mb = measurement["memory_delta_mb"]
            error_delta = measurement["error_delta"]
            trace_error_delta = measurement["trace_error_delta"]
            cpu_relative = measurement["cpu_relative"]
            latency_score = measurement["latency_score"]
            network_signal = measurement["network_signal"]
            network_root_score = network_scores.get(service, 0.0)
            memory_signal = min(memory_delta_mb / 128.0, 5.0)
            resource_signature = (
                "memory_pressure"
                if memory_delta_mb >= 32.0
                else "cpu_hog"
                if cpu_delta >= 0.1
                else "mixed"
            )
            signal_strengths = {
                "cpu_growth": round(cpu_delta, 6),
                "memory_growth": round(memory_signal, 6),
                "latency": round(latency_score, 6),
                "error_rate": round(min(error_delta, 10) * 0.1, 6),
                "trace_errors": round(min(trace_error_delta, 10) * 0.25, 6),
                "network_symptoms": round(network_signal, 6),
            }
            dominant_signal = (
                "mixed"
                if max(signal_strengths.values(), default=0.0) <= 0.0
                else max(signal_strengths, key=signal_strengths.get)
            )
            # Resource deltas are stronger root-cause evidence than raw log volume.
            # Log and trace errors remain useful, but propagated/workload noise must not
            # dominate the ranking.
            # Keep CPU in its observed delta units: a small CPU baseline can otherwise
            # turn an ordinary rate change into an artificial high relative score and
            # hide a much stronger memory-growth signal.
            resource_score = cpu_delta + (2.0 * memory_score)
            # Keep network symptoms available to the agent without globally boosting
            # propagated latency across every upstream service in the dependency graph.
            score = resource_score + min(error_delta, 10) * 0.1
            score += min(trace_error_delta, 10) * 0.25
            score += latency_score * 0.1
            anomalies.append(
                {
                    "service": service,
                    "score": round(score, 6),
                    "signals": {
                        "cpu_baseline_max": round(cpu_max_b, 6),
                        "cpu_incident_max": round(cpu_max_i, 6),
                        "cpu_delta": round(cpu_delta, 6),
                        "cpu_relative": round(cpu_relative, 6),
                        "memory_baseline_max": round(memory_max_b, 3),
                        "memory_incident_max": round(memory_max_i, 3),
                        "memory_baseline_level": round(
                            measurement["memory_baseline_level"], 3
                        ),
                        "memory_incident_level": round(
                            measurement["memory_incident_level"], 3
                        ),
                        "memory_growth_mb": round(memory_delta_mb, 3),
                        "memory_relative": round(memory_score, 6),
                        "resource_signature": resource_signature,
                        "dominant_signal": dominant_signal,
                        "signal_strengths": signal_strengths,
                        "error_baseline_count": errors_b,
                        "error_incident_count": errors_i,
                        "trace_error_baseline_count": traces_b.get("error_count", 0),
                        "trace_error_incident_count": traces_i.get("error_count", 0),
                        "trace_p95_latency_ms": traces_i.get("p95_latency_ms", 0.0),
                        "trace_max_latency_ms": traces_i.get("max_latency_ms", 0.0),
                        "network_root_score": round(network_root_score, 6),
                    },
                }
            )
            anomalies[-1]["score"] = round(
                anomalies[-1]["score"] + (1.5 * network_root_score), 6
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
            "network_evidence": network_evidence[:12],
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
