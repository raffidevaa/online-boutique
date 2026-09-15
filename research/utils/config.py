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
    scenario_directory: Path = RESEARCH_ROOT / "generators" / "scenarios"
    faulty_image_manifest: Path = RESEARCH_ROOT / "generators" / "faulty_images" / "manifest.yaml"
    experiment_runs: Path = RESEARCH_ROOT / "experiments" / "runs"
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    jaeger_url: str = "http://127.0.0.1:16686"
    telemetry_limit: int = 5_000
    trace_limit: int = 500
    trace_services: tuple[str, ...] = (
        "frontend",
        "productcatalogservice",
        "checkoutservice",
        "currencyservice",
        "paymentservice",
        "recommendationservice",
        "emailservice",
    )
    pumba_image: str = "ghcr.io/alexei-led/pumba:1.2.1"
    pumba_stress_image: str = "ghcr.io/alexei-led/stress-ng:0.20.01"

    @classmethod
    def from_environment(cls) -> "ResearchConfig":
        """Create configuration with safe endpoint overrides for remote hosts."""
        return cls(
            prometheus_url=os.getenv("RESEARCH_PROMETHEUS_URL", cls.prometheus_url),
            loki_url=os.getenv("RESEARCH_LOKI_URL", cls.loki_url),
            jaeger_url=os.getenv("RESEARCH_JAEGER_URL", cls.jaeger_url),
            telemetry_limit=int(os.getenv("RESEARCH_TELEMETRY_LIMIT", "5000")),
            trace_limit=int(os.getenv("RESEARCH_TRACE_LIMIT", "500")),
            pumba_image=os.getenv("RESEARCH_PUMBA_IMAGE", cls.pumba_image),
            pumba_stress_image=os.getenv(
                "RESEARCH_PUMBA_STRESS_IMAGE", cls.pumba_stress_image
            ),
        )
