"""Compose-backed read-only environment for replay and live sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import hashlib
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
        prior_evidence: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.start = start
        self.end = end
        self.run_dir = run_dir
        self._live: dict[str, Any] | None = None
        self._targetable_services_cache: set[str] | None = None
        self._targetable_service_ports_cache: dict[str, set[str]] | None = None
        self.evidence_store: dict[str, dict[str, Any]] = {}
        self.prior_evidence = dict(prior_evidence or {})
        self.notes: list[dict[str, Any]] = []

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

    def _payload(self, kind: str, window: str, service: str | None = None) -> dict[str, Any]:
        if self.run_dir is not None:
            phase = window if window in {"baseline", "incident", "recovery"} else "incident"
            path = self.run_dir / phase / f"{kind}.json"
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise EnvironmentError(f"Unable to read replay evidence: {path}") from error
        start, end = self._window_bounds(window)
        try:
            return Observer(self.config).capture_kind(kind, start, end, service)
        except ObserverError as error:
            raise EnvironmentError(str(error)) from error

    def _window_bounds(self, window: str) -> tuple[datetime, datetime]:
        if window == "baseline":
            duration = (self.end - self.start) if self.end else timedelta(minutes=1)
            return self.start - duration, self.start
        if window == "recovery":
            raise EnvironmentError("Recovery telemetry is unavailable before fault recovery")
        return self.start, self.end or datetime.now(UTC)

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
        query_start, query_end = self._window_bounds(window)
        try:
            return Observer(self.config).capture(query_start, query_end)
        except ObserverError as error:
            raise EnvironmentError(str(error)) from error

    def _record_evidence(
        self, source: str, window: str, service: str | None, payload: Any,
        summary: Any, count: int, displayed: int,
    ) -> str:
        start, end = self._window_bounds(window) if window != "recovery" else (self.start, self.end or self.start)
        material = json.dumps({"source": source, "window": window, "service": service,
                               "payload": payload}, sort_keys=True, default=str)
        evidence_id = "ev-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        self.evidence_store[evidence_id] = {
            "source": source, "window": window, "service": service,
            "start": start.isoformat(), "end": end.isoformat(),
            "payload": payload, "summary": summary,
        }
        envelope = {
            "evidence_id": evidence_id, "source": source, "service": service,
            "start": start.isoformat(), "end": end.isoformat(),
            "total_item_count": count, "displayed_item_count": displayed,
            "limited": displayed < count, "data_quality": "available" if count else "empty",
            "detail_options": {"action": "get_evidence_detail", "limit": "1..20"},
            "summary": summary,
        }
        return json.dumps(envelope, sort_keys=True)

    def _signal_summary(self, kind: str, payload: dict[str, Any], service: str | None, window: str) -> str:
        if kind == "metrics":
            items = []
            for name, response in payload.get("queries", {}).items():
                for series in response.get("data", {}).get("result", []):
                    if service and series.get("metric", {}).get("service") != service:
                        continue
                    values = series.get("values", [])
                    numbers = []
                    for pair in values:
                        try:
                            numbers.append(float(pair[1]))
                        except (IndexError, TypeError, ValueError):
                            pass
                    items.append({"query": name, "labels": series.get("metric", {}),
                                  "samples": len(values), "mean": sum(numbers) / len(numbers) if numbers else None,
                                  "max": max(numbers) if numbers else None})
        elif kind == "logs":
            items = []
            for stream in self._log_streams(payload):
                if service and stream.get("stream", {}).get("compose_service") != service:
                    continue
                values = stream.get("values", [])
                items.append({"service": stream.get("stream", {}).get("compose_service"),
                              "count": len(values), "sample": str(values[0][1])[:240] if values else None})
        elif kind == "traces":
            items = []
            for name, result in payload.get("services", {}).items():
                if service and name != service:
                    continue
                traces = result.get("data", []) if isinstance(result, dict) else []
                ranked = sorted(traces, key=self._trace_relevance, reverse=True)
                items.append({"service": name, "trace_count": len(traces),
                              "sample_trace_ids": [item.get("traceID") for item in ranked[:3]],
                              "ranking": "error spans then longest span"})
        else:
            result = payload.get("alerts", {})
            items = result.get("data", {}).get("alerts", []) if isinstance(result, dict) else []
        displayed = min(len(items), 5)
        return self._record_evidence(kind, window, service, payload, items[:displayed], len(items), displayed)

    @staticmethod
    def _trace_relevance(trace: dict[str, Any]) -> tuple[int, int]:
        spans = trace.get("spans", [])
        errors = sum(
            any(tag.get("key") == "error" and str(tag.get("value")).lower() == "true"
                for tag in span.get("tags", []))
            for span in spans
        )
        longest = max((int(span.get("duration") or 0) for span in spans), default=0)
        return errors, longest

    def get_evidence_detail(self, evidence_id: str, limit: int = 5) -> str:
        if not self.has_evidence(evidence_id):
            raise EnvironmentError("evidence_id is not available in this session")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise EnvironmentError("limit must be an integer from 1 to 20")
        entry = self.evidence_store.get(evidence_id) or self.prior_evidence[evidence_id]
        payload = entry["payload"]
        source = entry["source"]
        service = entry["service"]
        if source == "logs":
            items = [
                {"timestamp": value[0], "message": str(value[1])[:500],
                 "message_limited": len(str(value[1])) > 500}
                for stream in self._log_streams(payload)
                if service is None or stream.get("stream", {}).get("compose_service") == service
                for value in stream.get("values", []) if len(value) >= 2
            ]
        elif source == "traces":
            traces = [trace for name, result in payload.get("services", {}).items()
                      if service is None or name == service
                      for trace in result.get("data", [])]
            items = [
                {"trace_id": trace.get("traceID"),
                 "span_count": len(trace.get("spans", [])),
                 "spans_displayed": min(len(trace.get("spans", [])), 5),
                 "spans_limited": len(trace.get("spans", [])) > 5,
                 "spans": [{"operation": span.get("operationName"),
                            "duration": span.get("duration"),
                            "process_id": span.get("processID")}
                           for span in trace.get("spans", [])[:5]]}
                for trace in sorted(traces, key=self._trace_relevance, reverse=True)
            ]
        elif source == "metrics":
            items = [
                {"labels": series.get("metric", {}),
                 "total_samples": len(series.get("values", [])),
                 "displayed_samples": min(len(series.get("values", [])), 20),
                 "samples_limited": len(series.get("values", [])) > 20,
                 "values": series.get("values", [])[:20]}
                for response in payload.get("queries", {}).values()
                for series in response.get("data", {}).get("result", [])
                if service is None or series.get("metric", {}).get("service") == service
            ]
        else:
            items = [entry["summary"]]
        shown = items[:limit]
        return "evidence_detail: " + json.dumps({
            "evidence_id": evidence_id, "source": source,
            "observed_at": entry["end"],
            "total_item_count": len(items), "displayed_item_count": len(shown),
            "limited": len(shown) < len(items), "items": shown,
        }, sort_keys=True)

    def has_evidence(self, evidence_id: str) -> bool:
        return evidence_id in self.evidence_store or evidence_id in self.prior_evidence

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
                    "sample_quality": (
                        "sparse" if incident_edge["call_count"] < 3 else "sufficient"
                    ),
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
            if evidence["call_count"] < 3 or evidence["inferred_signature"] == "ambiguous":
                continue
            peer = evidence["peer_service"]
            score = (
                evidence["delay_score"]
                if evidence["inferred_signature"] == "network_delay"
                else evidence["loss_score"]
            )
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
        full_output = {"network_evidence": network_evidence[:12], "top_anomalies": anomalies[:8]}
        output = {
            "schema_version": 1,
            "window": window,
            "baseline_window": "baseline",
            "data_quality": {
                "incident_cpu_samples": sum(len(values) for values in cpu_incident.values()),
                "baseline_cpu_samples": sum(len(values) for values in cpu_baseline.values()),
                "incident_services": len(services),
            },
            "network_evidence": [{key: edge[key] for key in (
                "caller_service", "peer_service", "operation", "call_count",
                "sample_quality", "latency_shift_ms", "error_delta",
                "inferred_signature", "delay_score", "loss_score",
            )} for edge in network_evidence[:12]],
            "top_anomalies": [{"service": item["service"], "score": item["score"],
                "signals": {key: item["signals"][key] for key in (
                    "dominant_signal", "resource_signature", "cpu_delta",
                    "cpu_incident_max", "memory_growth_mb", "memory_relative",
                    "network_root_score", "trace_p95_latency_ms",
                    "error_incident_count", "trace_error_incident_count",
                )}} for item in anomalies[:8]],
        }
        summary = self._record_evidence(
            "incident_summary", window, None, full_output, output, len(anomalies),
            len(output["top_anomalies"]),
        )
        envelope = json.loads(summary)
        envelope.pop("summary", None)
        envelope.update(output)
        return f"incident_summary: {json.dumps(envelope, sort_keys=True)}"

    def get_error_logs(self, service: str | None = None, window: str = "incident") -> str:
        output = {
            "schema_version": 1,
            "window": window,
            "services": self._error_log_summary(self._payload("logs", window, service), service),
        }
        envelope = json.loads(self._record_evidence(
            "error_logs", window, service, output, output, len(output["services"]),
            len(output["services"]),
        ))
        envelope.pop("summary", None)
        envelope.update(output)
        return "error_logs: " + json.dumps(envelope, sort_keys=True)

    def get_metrics(self, service: str | None = None, window: str = "incident") -> str:
        payload = self._payload("metrics", window, service)
        return f"metrics window={window}: {self._signal_summary('metrics', payload, service, window)}"

    def get_logs(self, service: str | None = None, window: str = "incident") -> str:
        payload = self._payload("logs", window, service)
        return f"logs window={window}: {self._signal_summary('logs', payload, service, window)}"

    def get_traces(self, service: str | None = None, window: str = "incident") -> str:
        payload = self._payload("traces", window, service)
        return f"traces window={window}: {self._signal_summary('traces', payload, service, window)}"

    def get_alerts(self, window: str = "incident") -> str:
        payload = self._payload("alerts", window)
        return f"alerts window={window}: {self._signal_summary('alerts', payload, None, window)}"
