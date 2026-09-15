"""Read-only Docker Compose access for the Online Boutique application."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Sequence

import yaml

from research.utils.config import ResearchConfig


class ServiceError(RuntimeError):
    """Raised when Docker Compose cannot provide a required application fact."""


SECRET_KEY = re.compile(r"(api[_-]?key|password|secret|token|credential)", re.IGNORECASE)


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    role: str
    port: int | None = None
    dependencies: tuple[str, ...] = ()
    targetable: bool = False
    deployed: bool = True
    confounders: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceCatalog:
    application: str
    services: dict[str, ServiceDefinition]

    @classmethod
    def load(cls, path: Path) -> "ServiceCatalog":
        with path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if raw.get("schema_version") != 1 or not isinstance(raw.get("services"), dict):
            raise ValueError(f"Invalid service metadata: {path}")

        services: dict[str, ServiceDefinition] = {}
        for name, value in raw["services"].items():
            if not isinstance(value, dict) or not value.get("role"):
                raise ValueError(f"Invalid service definition for {name}")
            services[name] = ServiceDefinition(
                name=name,
                role=value["role"],
                port=value.get("port"),
                dependencies=tuple(value.get("dependencies", [])),
                targetable=bool(value.get("targetable", False)),
                deployed=bool(value.get("deployed", True)),
                confounders=tuple(value.get("confounders", [])),
            )
        return cls(raw.get("application", ""), services)

    def get(self, name: str) -> ServiceDefinition:
        try:
            return self.services[name]
        except KeyError as error:
            raise KeyError(f"Unknown service: {name}") from error


@dataclass(frozen=True)
class ServiceState:
    service: str
    container_id: str
    image: str
    status: str
    health: str | None
    restart_count: int
    memory_limit: int | None
    nano_cpus: int | None
    networks: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "container_id": self.container_id,
            "image": self.image,
            "status": self.status,
            "health": self.health,
            "restart_count": self.restart_count,
            "memory_limit": self.memory_limit,
            "nano_cpus": self.nano_cpus,
            "networks": list(self.networks),
            "labels": self.labels,
        }


class ComposeApplication:
    """Narrow, read-only adapter for the Docker Compose application runtime."""

    def __init__(self, config: ResearchConfig, catalog: ServiceCatalog | None = None) -> None:
        self.config = config
        self.catalog = catalog or ServiceCatalog.load(config.service_metadata)

    def _compose(self, *args: str) -> list[str]:
        return ["docker", "compose", "-f", str(self.config.compose_file), *args]

    def _compose_with_override(self, override: Path, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.config.compose_file),
            "-f",
            str(override),
            *args,
        ]

    @staticmethod
    def _run(command: Sequence[str], check: bool = True) -> CommandResult:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise ServiceError(f"{' '.join(command[:4])} …: {message}")
        return CommandResult(result.stdout, result.stderr, result.returncode)

    def validate(self) -> None:
        """Validate the Compose model and metadata without changing runtime state."""
        self._run(self._compose("config", "--quiet"))
        configured = set(self.list_services())
        missing = [
            name
            for name, service in self.catalog.services.items()
            if service.deployed and name not in configured
        ]
        if missing:
            raise ServiceError(
                "Metadata services missing from Compose configuration: " + ", ".join(missing)
            )

    def list_services(self) -> list[str]:
        result = self._run(self._compose("config", "--services"))
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _container_id(self, service: str) -> str | None:
        result = self._run(self._compose("ps", "-q", service), check=False)
        container_id = result.stdout.strip()
        return container_id or None

    def get_service_state(self, service: str) -> ServiceState | None:
        container_id = self._container_id(service)
        if container_id is None:
            return None
        result = self._run(["docker", "inspect", container_id])
        inspected = json.loads(result.stdout)[0]
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        compose_service = labels.get("com.docker.compose.service")
        if compose_service != service:
            raise ServiceError(
                f"Container {container_id} does not belong to Compose service {service}"
            )
        state = inspected.get("State", {})
        host_config = inspected.get("HostConfig", {})
        networks = tuple((inspected.get("NetworkSettings", {}).get("Networks") or {}).keys())
        health = (state.get("Health") or {}).get("Status")
        return ServiceState(
            service=service,
            container_id=inspected.get("Id", container_id),
            image=inspected.get("Config", {}).get("Image", ""),
            status=state.get("Status", "unknown"),
            health=health,
            restart_count=int(state.get("RestartCount", 0)),
            memory_limit=host_config.get("Memory") or None,
            nano_cpus=host_config.get("NanoCpus") or None,
            networks=networks,
            labels={key: str(value) for key, value in labels.items()},
        )

    def get_all_service_states(self) -> dict[str, ServiceState | None]:
        return {service: self.get_service_state(service) for service in self.list_services()}

    def validate_readiness(self) -> dict[str, ServiceState]:
        """Ensure configured services are running and healthy where checks exist."""
        self.validate()
        unavailable: list[str] = []
        ready: dict[str, ServiceState] = {}
        for service, state in self.get_all_service_states().items():
            if state is None or state.status != "running":
                unavailable.append(service)
                continue
            if state.health is not None and state.health != "healthy":
                unavailable.append(f"{service} ({state.health})")
                continue
            ready[service] = state
        if unavailable:
            raise ServiceError("Services not ready: " + ", ".join(unavailable))
        return ready

    def wait_for_service_healthy(
        self,
        service: str,
        *,
        timeout_seconds: float = 90.0,
        poll_seconds: float = 2.0,
    ) -> ServiceState:
        """Wait until one service is running and its configured healthcheck passes."""
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("timeout_seconds and poll_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        last_state: ServiceState | None = None
        while True:
            last_state = self.get_service_state(service)
            if (
                last_state is not None
                and last_state.status == "running"
                and last_state.health in (None, "healthy")
            ):
                return last_state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_state is None:
                    detail = "container is absent"
                else:
                    detail = f"status={last_state.status}, health={last_state.health}"
                raise ServiceError(
                    f"Service {service} did not become healthy within "
                    f"{timeout_seconds:.0f}s ({detail})"
                )
            time.sleep(min(poll_seconds, remaining))

    def validate_fault_override(
        self,
        override: Path,
        service: str,
        expected_image: str,
        expected_trigger: str | None = None,
    ) -> None:
        """Validate that an override changes only the approved target image."""
        if not override.is_file():
            raise ServiceError(f"Compose override does not exist: {override}")
        try:
            raw = yaml.safe_load(override.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as error:
            raise ServiceError(f"Unable to read Compose override {override}: {error}") from error
        if not isinstance(raw, dict) or set(raw) - {"services"}:
            raise ServiceError(f"Invalid Compose override keys: {override}")
        services = raw.get("services")
        if not isinstance(services, dict) or set(services) != {service}:
            raise ServiceError(
                f"Fault override must contain only target service {service}: {override}"
            )
        target = services.get(service)
        if not isinstance(target, dict):
            raise ServiceError(f"Invalid target service override: {override}")
        allowed_keys = {"image"}
        if expected_trigger is not None:
            allowed_keys.add("environment")
        if set(target) != allowed_keys:
            raise ServiceError(
                f"Fault override contains unapproved target fields: {override}"
            )
        if target.get("image") != expected_image:
            raise ServiceError(
                f"Fault override image mismatch for {service}: {target.get('image')}"
            )
        if expected_trigger is not None:
            environment = target.get("environment")
            if environment != {"RESEARCH_FAULT_TRIGGER": expected_trigger}:
                raise ServiceError(f"Fault override trigger mismatch for {service}")
        elif "environment" in target:
            raise ServiceError(f"Fault override cannot add an environment trigger: {override}")
        self._run(self._compose_with_override(override, "config", "--quiet"))
        resolved = self._run(
            self._compose_with_override(
                override, "config", "--format", "json", "--no-path-resolution"
            )
        )
        configured = json.loads(resolved.stdout)
        resolved_service = configured.get("services", {}).get(service, {})
        if resolved_service.get("image") != expected_image:
            raise ServiceError(f"Resolved Compose image mismatch for {service}")

    def apply_fault_override(self, override: Path, service: str) -> CommandResult:
        """Recreate only the target service using an approved faulty image."""
        return self._run(
            self._compose_with_override(
                override, "up", "-d", "--no-deps", "--force-recreate", service
            )
        )

    def restore_service(self, service: str) -> CommandResult:
        """Recreate one service from the baseline Compose file."""
        return self._run(self._compose("up", "-d", "--no-deps", "--force-recreate", service))

    def get_resolved_compose_config(self) -> dict[str, Any]:
        result = self._run(
            self._compose("config", "--format", "json", "--no-path-resolution")
        )
        return self._redact(json.loads(result.stdout))

    @classmethod
    def _redact(cls, value: Any, key: str | None = None) -> Any:
        if key and SECRET_KEY.search(key):
            return "<redacted>"
        if isinstance(value, dict):
            return {item_key: cls._redact(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    def snapshot(self) -> dict[str, Any]:
        """Return a Docker/Compose equivalent of a cluster-state snapshot."""
        resolved = self.get_resolved_compose_config()
        encoded = json.dumps(resolved, sort_keys=True, separators=(",", ":")).encode()
        states = self.get_all_service_states()
        return {
            "schema_version": 1,
            "timestamp": datetime.now(UTC).isoformat(),
            "compose_file": str(self.config.compose_file.relative_to(self.config.research_root)),
            "resolved_compose_sha256": hashlib.sha256(encoded).hexdigest(),
            "services": {
                name: state.to_dict() if state is not None else None
                for name, state in states.items()
            },
        }

    def resolved_compose_yaml(self) -> str:
        return yaml.safe_dump(self.get_resolved_compose_config(), sort_keys=False)
