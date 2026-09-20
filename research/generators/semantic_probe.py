"""Deterministic, user-facing semantic probes for code-level experiments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
import http.cookiejar
import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from research.generators.fault import FaultScenario
from research.utils.config import ResearchConfig


class SemanticProbeError(RuntimeError):
    """Raised when a deterministic semantic flow cannot be completed."""

    def __init__(self, message: str, requests: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.requests = requests or []


@dataclass(frozen=True)
class ProbePhaseResult:
    """Raw request evidence and normalized values for one experiment phase."""

    phase: str
    started_at: str
    ended_at: str
    requests: list[dict[str, Any]]
    normalized: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "requests": self.requests,
            "normalized": self.normalized,
        }


class _ProductPriceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes: set[str] = set()
        for name, class_value in attrs:
            if name == "class" and class_value:
                classes.update(class_value.split())
        if "product-price" in classes:
            self._depth = 1
        elif self._depth:
            self._depth += 1

    def handle_endtag(self, _: str) -> None:
        if self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._parts.append(data)

    @property
    def value(self) -> str:
        return "".join(self._parts).strip()


def _price_value(body: str) -> str:
    parser = _ProductPriceParser()
    parser.feed(body)
    displayed = parser.value
    matches = re.findall(r"-?\d[\d,]*(?:\.\d+)?", displayed)
    if not matches:
        raise SemanticProbeError("product response did not contain a numeric product price")
    return matches[-1].replace(",", "")


def _cart_size(body: str) -> int:
    if "Your shopping cart is empty!" in body:
        return 0
    match = re.search(r"Cart\s*\(\s*(\d+)\s*\)", body)
    if not match:
        raise SemanticProbeError("cart response did not expose a cart size")
    return int(match.group(1))


def _is_order_success(body: str, status: int) -> bool:
    return status < 400 and "Your order is complete!" in body


_TRANSIENT_MARKERS = (
    "connection refused",
    "code = unavailable",
    "could not retrieve",
    "transport error",
    "temporarily unavailable",
)


def _transient_response(response: dict[str, Any]) -> bool:
    status = response.get("status")
    body = str(response.get("body", "")).lower()
    return status in {502, 503, 504} or (
        status == 500 and any(marker in body for marker in _TRANSIENT_MARKERS)
    ) or any(marker in body for marker in _TRANSIENT_MARKERS)


def _transient_error(error: SemanticProbeError) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _request_evidence(
    response: dict[str, Any], attempt: int, retry_reason: str | None = None
) -> dict[str, Any]:
    evidence = {key: value for key, value in response.items() if key != "body"}
    evidence["attempt"] = attempt
    if retry_reason is not None:
        evidence["retryable"] = True
        evidence["retry_reason"] = retry_reason
    return evidence


def _retry_count(requests: list[dict[str, Any]]) -> int:
    return sum(1 for request in requests if request.get("retryable"))


class _FrontendSession:
    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = build_opener(
            HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self,
        method: str,
        path: str,
        form: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "text/html,application/xhtml+xml"}
        if form is not None:
            data = urlencode({key: str(value) for key, value in form.items()}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        started = time.perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                status = int(response.status)
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            status = int(error.code)
            body = error.read().decode("utf-8", errors="replace")
        except (OSError, URLError, TimeoutError) as error:
            raise SemanticProbeError(f"frontend request {method} {path} failed: {error}") from error
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": elapsed_ms,
            "body_length": len(body),
            "body": body[:16000],
        }


def _checkout_form() -> dict[str, str]:
    return {
        "email": "semantic-probe@example.com",
        "street_address": "1600 Amphitheatre Parkway",
        "zip_code": "94043",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
        "credit_card_number": "4432801561520454",
        "credit_card_expiration_month": "12",
        "credit_card_expiration_year": "2099",
        "credit_card_cvv": "672",
    }


def _run_currency_phase(
    scenario: FaultScenario, config: ResearchConfig, phase: str
) -> ProbePhaseResult:
    probe = scenario.semantic_probe or {}
    product_id = str(probe["product_id"])
    currency = str(probe["currency"])
    repetitions = int(probe.get("repetitions", 1))
    timeout_seconds = int(probe.get("timeout_seconds", 10))
    started = datetime.now(UTC)
    requests: list[dict[str, Any]] = []
    values: list[str] = []
    max_attempts = max(1, config.semantic_probe_retry_attempts)
    for _ in range(repetitions):
        for attempt in range(1, max_attempts + 1):
            session = _FrontendSession(config.frontend_url, timeout_seconds)
            try:
                currency_response = session.request(
                    "POST", "/setCurrency", {"currency_code": currency}
                )
                if currency_response["status"] >= 400:
                    reason = (
                        f"{phase} setCurrency returned HTTP {currency_response['status']}"
                    )
                    requests.append(_request_evidence(currency_response, attempt, reason))
                    if _transient_response(currency_response) and attempt < max_attempts:
                        time.sleep(config.semantic_probe_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise SemanticProbeError(reason, requests)
                requests.append(_request_evidence(currency_response, attempt))

                product_response = session.request("GET", f"/product/{product_id}")
                if product_response["status"] >= 400:
                    reason = (
                        f"{phase} product request returned HTTP {product_response['status']}"
                    )
                    requests.append(_request_evidence(product_response, attempt, reason))
                    if _transient_response(product_response) and attempt < max_attempts:
                        time.sleep(config.semantic_probe_retry_backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise SemanticProbeError(reason, requests)
                requests.append(_request_evidence(product_response, attempt))
                values.append(_price_value(str(product_response["body"])))
                break
            except SemanticProbeError as error:
                if error.requests:
                    requests = error.requests
                if _transient_error(error) and attempt < max_attempts:
                    time.sleep(config.semantic_probe_retry_backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise SemanticProbeError(str(error), requests) from error
    ended = datetime.now(UTC)
    return ProbePhaseResult(
        phase=phase,
        started_at=started.isoformat(),
        ended_at=ended.isoformat(),
        requests=requests,
        normalized={
            "product_id": product_id,
            "currency": currency,
            "values": values,
            "value": values[0],
            "retry_count": _retry_count(requests),
        },
    )


def _run_checkout_phase(
    scenario: FaultScenario, config: ResearchConfig, phase: str
) -> ProbePhaseResult:
    probe = scenario.semantic_probe or {}
    product_id = str(probe["product_id"])
    quantity = int(probe.get("quantity", 1))
    repetitions = int(probe.get("repetitions", 1))
    timeout_seconds = int(probe.get("timeout_seconds", 15))
    started = datetime.now(UTC)
    requests: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for _ in range(repetitions):
        session = _FrontendSession(config.frontend_url, timeout_seconds)
        add_response = session.request(
            "POST", "/cart", {"product_id": product_id, "quantity": quantity}
        )
        requests.append({key: value for key, value in add_response.items() if key != "body"})
        if add_response["status"] >= 400:
            raise SemanticProbeError(
                f"{phase} add-to-cart returned HTTP {add_response['status']}"
            )
        checkout_response = session.request("POST", "/cart/checkout", _checkout_form())
        requests.append({key: value for key, value in checkout_response.items() if key != "body"})
        succeeded = _is_order_success(
            str(checkout_response["body"]), int(checkout_response["status"])
        )
        cart_response = session.request("GET", "/cart")
        requests.append({key: value for key, value in cart_response.items() if key != "body"})
        if cart_response["status"] >= 400:
            raise SemanticProbeError(
                f"{phase} cart inspection returned HTTP {cart_response['status']}"
            )
        cart_size = _cart_size(str(cart_response["body"]))
        outcomes.append(
            {
                "checkout_succeeded": succeeded,
                "checkout_http_status": checkout_response["status"],
                "cart_items_after_checkout": cart_size,
            }
        )
        cleanup = session.request("POST", "/cart/empty")
        requests.append({key: value for key, value in cleanup.items() if key != "body"})
        if cleanup["status"] >= 400:
            raise SemanticProbeError(
                f"{phase} cart cleanup returned HTTP {cleanup['status']}"
            )
    ended = datetime.now(UTC)
    errors = sum(1 for outcome in outcomes if not outcome["checkout_succeeded"])
    return ProbePhaseResult(
        phase=phase,
        started_at=started.isoformat(),
        ended_at=ended.isoformat(),
        requests=requests,
        normalized={
            "product_id": product_id,
            "repetitions": repetitions,
            "outcomes": outcomes,
            "error_count": errors,
            "checkout_succeeded": all(
                outcome["checkout_succeeded"] for outcome in outcomes
            ),
            "cart_items_after_checkout": max(
                outcome["cart_items_after_checkout"] for outcome in outcomes
            ),
            "downstream_status": "ERROR" if errors else "OK",
        },
    )


def run_semantic_probe(
    scenario: FaultScenario, config: ResearchConfig, phase: str
) -> ProbePhaseResult:
    """Run the configured probe against the public frontend."""
    if scenario.category != "code_level" or not scenario.semantic_probe:
        raise SemanticProbeError("scenario has no code-level semantic probe")
    probe_type = scenario.semantic_probe["type"]
    if probe_type == "currency_display":
        return _run_currency_phase(scenario, config, phase)
    if probe_type == "checkout_flow":
        return _run_checkout_phase(scenario, config, phase)
    raise SemanticProbeError(f"unsupported semantic probe type: {probe_type}")


def _telemetry_text(capture: dict[str, Any], service: str) -> list[str]:
    texts: list[str] = []
    logs = capture.get("logs", {}).get("result", {}).get("data", {}).get("result", [])
    if isinstance(logs, list):
        for stream in logs:
            if not isinstance(stream, dict):
                continue
            labels = stream.get("stream", {})
            if labels.get("compose_service") != service:
                continue
            for value in stream.get("values", []):
                if isinstance(value, list) and len(value) >= 2:
                    texts.append(str(value[1]))
    traces = capture.get("traces", {}).get("services", {}).get(service, {})
    if traces:
        texts.append(json.dumps(traces, sort_keys=True))
    return texts


def _matches_telemetry(capture: dict[str, Any], service: str, terms: tuple[str, ...]) -> list[str]:
    matches: list[str] = []
    for text in _telemetry_text(capture, service):
        lowered = text.lower()
        if any(term in lowered for term in terms):
            matches.append(text[:500])
    return matches


def build_semantic_observation(
    scenario: FaultScenario,
    baseline: ProbePhaseResult,
    incident: ProbePhaseResult,
    incident_capture: dict[str, Any],
) -> dict[str, Any]:
    """Combine probe phases with bounded telemetry evidence."""
    fault_type = scenario.fault_type
    baseline_values = baseline.normalized
    incident_values = incident.normalized
    evidence_source = ["frontend_http"]
    telemetry_matches: dict[str, list[str]] = {}
    normalized: dict[str, Any]
    if fault_type == "incorrect_return_value":
        normalized = {
            "baseline_value": baseline_values["value"],
            "incident_value": incident_values["value"],
        }
    elif fault_type == "missing_exception_handler":
        checkout_matches = _matches_telemetry(
            incident_capture,
            "checkoutservice",
            ("panic", "runtime error", "unhandled", "faulty image"),
        )
        telemetry_matches["checkoutservice"] = checkout_matches
        normalized = {
            "baseline_error_count": baseline_values["error_count"],
            "incident_error_count": incident_values["error_count"],
            "unhandled_exception": bool(checkout_matches),
            "telemetry_evidence": bool(checkout_matches),
        }
        evidence_source.extend(["loki", "jaeger"])
    elif fault_type in {"incorrect_parameter", "missing_parameter"}:
        checkout_matches = _matches_telemetry(
            incident_capture,
            "checkoutservice",
            ("failed to convert", "failed to prepare", "invalid", "error", "unavailable"),
        )
        currency_matches = _matches_telemetry(
            incident_capture,
            "currencyservice",
            ("conversion request failed", "error", "invalid", "nan"),
        )
        telemetry_matches["checkoutservice"] = checkout_matches
        telemetry_matches["currencyservice"] = currency_matches
        normalized = {
            "checkout_succeeded": incident_values["checkout_succeeded"],
            "incident_error_count": incident_values["error_count"],
            "downstream_status": incident_values["downstream_status"],
            "downstream_service": "currencyservice",
            "downstream_error_evidence": bool(checkout_matches or currency_matches),
        }
        evidence_source.extend(["loki", "jaeger"])
    elif fault_type == "missing_function_call":
        normalized = {
            "checkout_succeeded": incident_values["checkout_succeeded"],
            "cart_items_after_checkout": incident_values["cart_items_after_checkout"],
            "baseline_cart_items_after_checkout": baseline_values[
                "cart_items_after_checkout"
            ],
        }
    else:
        raise SemanticProbeError(f"no semantic observation mapping for {fault_type}")
    return {
        "schema_version": 2,
        "scenario_id": scenario.scenario_id,
        "probe_type": scenario.semantic_probe["type"] if scenario.semantic_probe else None,
        "started_at": baseline.started_at,
        "baseline": baseline.to_dict(),
        "incident": incident.to_dict(),
        "normalized_observation": normalized,
        "evidence_source": evidence_source,
        "telemetry_matches": telemetry_matches,
    }
