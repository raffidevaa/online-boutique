"""Central configuration for the research framework."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


RESEARCH_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ResearchConfig:
    """Paths and read-only observability endpoints used by runtime modules."""

    research_root: Path = RESEARCH_ROOT
    compose_file: Path = RESEARCH_ROOT / "compose" / "docker-compose.yml"
    service_metadata: Path = RESEARCH_ROOT / "service" / "metadata.yaml"
    experiment_runs: Path = RESEARCH_ROOT / "experiments" / "runs"
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    telemetry_limit: int = 5_000

    @classmethod
    def from_environment(cls) -> "ResearchConfig":
        """Create configuration with safe endpoint overrides for remote hosts."""
        return cls(
            prometheus_url=os.getenv("RESEARCH_PROMETHEUS_URL", cls.prometheus_url),
            loki_url=os.getenv("RESEARCH_LOKI_URL", cls.loki_url),
            telemetry_limit=int(os.getenv("RESEARCH_TELEMETRY_LIMIT", "5000")),
        )
