"""Expose Docker Compose container metadata for Prometheus joins.

The cAdvisor containerd integration provides reliable resource metrics on Docker
Desktop, but does not expose Compose labels. This exporter reads the Docker Engine
API over a read-only Unix socket and publishes the mapping separately. It also
exports lightweight runtime state so Grafana can show Docker health alongside
resource telemetry without granting Grafana Docker access.
"""

from __future__ import annotations

import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
from typing import Any
from urllib.parse import urlencode


API_VERSION = os.getenv("DOCKER_API_VERSION", "v1.41")
PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "compose")
SOCKET_PATH = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
DOCKER_HOST = os.getenv("DOCKER_HOST", "")
REFRESH_SECONDS = int(os.getenv("METADATA_REFRESH_SECONDS", "5"))


def _label(value: str) -> str:
    """Encode a Prometheus label value."""
    return json.dumps(value, ensure_ascii=True)


class UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal HTTP connection using a Unix domain socket."""

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._unix_socket_path)

    def __init__(self, path: str, timeout: float = 5) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_socket_path = path


def _connection() -> http.client.HTTPConnection:
    if DOCKER_HOST:
        return http.client.HTTPConnection(DOCKER_HOST, timeout=5)
    return UnixHTTPConnection(SOCKET_PATH)


def _get_json(path: str) -> Any:
    connection = _connection()
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    if response.status != 200:
        raise RuntimeError(f"Docker API returned HTTP {response.status}")
    return json.loads(body.decode("utf-8"))


def list_containers() -> list[dict[str, Any]]:
    """Return current and stopped Compose containers for runtime status."""
    query = urlencode({"all": "true", "label": "com.docker.compose.service"})
    decoded = _get_json(f"/{API_VERSION}/containers/json?{query}")
    if not isinstance(decoded, list):
        raise RuntimeError("Docker API returned an unexpected container list")
    return decoded


def render_metrics(containers: list[dict[str, Any]]) -> bytes:
    """Render Compose mapping and runtime status gauges."""
    lines = [
        "# HELP compose_container_info Docker Compose container to service mapping.",
        "# TYPE compose_container_info gauge",
        "# HELP compose_container_state Docker container state for a Compose service.",
        "# TYPE compose_container_state gauge",
        "# HELP compose_container_health Docker healthcheck status for a Compose service.",
        "# TYPE compose_container_health gauge",
    ]
    for container in containers:
        labels = container.get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        project = labels.get("com.docker.compose.project")
        container_id = container.get("Id")
        if not service or not container_id or (PROJECT and project != PROJECT):
            continue
        image = container.get("Image", "")
        state = str(container.get("State") or "unknown").lower()
        status = str(container.get("Status") or "").lower()
        health = "none"
        for candidate in ("healthy", "unhealthy", "starting"):
            if f"({candidate})" in status:
                health = candidate
                break
        labels_prefix = f'name={_label(str(container_id))},service={_label(str(service))}'
        lines.append(f"compose_container_state{{{labels_prefix},state={_label(state)}}} 1")
        lines.append(f"compose_container_health{{{labels_prefix},health={_label(health)}}} 1")
        # Preserve the existing running-only mapping: observer resource joins rely on it.
        if state == "running" or "State" not in container:
            lines.append(
                "compose_container_info{"
                f'{labels_prefix},image={_label(str(image))}'
                "} 1"
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    _metrics = b""
    _refreshed_at = 0.0
    _last_error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path == "/healthz":
            self._send(200, b"ok\n", "text/plain; version=0.0.4")
            return
        if self.path != "/metrics":
            self._send(404, b"not found\n", "text/plain")
            return
        import time

        if time.monotonic() - self._refreshed_at >= REFRESH_SECONDS:
            try:
                self._metrics = render_metrics(list_containers())
                self._last_error = None
                self._refreshed_at = time.monotonic()
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                self._last_error = str(error)
        if self._last_error and not self._metrics:
            self._send(503, self._last_error.encode("utf-8"), "text/plain")
            return
        self._send(200, self._metrics, "text/plain; version=0.0.4")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8081), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
