"""Central configuration for the research framework."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


RESEARCH_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv(path: Path) -> dict[str, str]:
    """Read a small, dependency-free dotenv file without overriding the shell."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class ResearchConfig:
    """Paths and read-only observability endpoints used by runtime modules."""

    research_root: Path = RESEARCH_ROOT
    compose_file: Path = RESEARCH_ROOT / "compose" / "docker-compose.yml"
    service_metadata: Path = RESEARCH_ROOT / "service" / "metadata.yaml"
    scenario_directory: Path = RESEARCH_ROOT / "generators" / "scenarios"
    faulty_image_manifest: Path = RESEARCH_ROOT / "generators" / "faulty_images" / "manifest.yaml"
    experiment_runs: Path = RESEARCH_ROOT / "experiments" / "runs"
    experiment_index: Path = RESEARCH_ROOT / "experiments" / "index.csv"
    agent_sessions: Path = RESEARCH_ROOT / "experiments" / "agent-sessions"
    frontend_url: str = "http://127.0.0.1:8080"
    semantic_probe_stabilization_seconds: float = 5.0
    semantic_probe_retry_attempts: int = 3
    semantic_probe_retry_backoff_seconds: float = 1.0
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
    aiops_llm_provider: str = "fake"
    aiops_llm_base_url: str = ""
    aiops_llm_api_key: str = ""
    aiops_llm_model: str = ""
    aiops_llm_timeout_seconds: float = 60.0

    @classmethod
    def from_environment(cls) -> "ResearchConfig":
        """Create configuration with safe endpoint overrides for remote hosts."""
        # Keep secrets in the repository-root .env (ignored by git). Explicit shell
        # variables take precedence, which is important in CI and production.
        dotenv = _load_dotenv(RESEARCH_ROOT.parent / ".env")

        def env(name: str, default: str) -> str:
            return os.getenv(name, dotenv.get(name, default))

        return cls(
            frontend_url=env("RESEARCH_FRONTEND_URL", cls.frontend_url),
            semantic_probe_stabilization_seconds=float(
                env("RESEARCH_SEMANTIC_PROBE_STABILIZATION_SECONDS", "5")
            ),
            semantic_probe_retry_attempts=int(
                env("RESEARCH_SEMANTIC_PROBE_RETRY_ATTEMPTS", "3")
            ),
            semantic_probe_retry_backoff_seconds=float(
                env("RESEARCH_SEMANTIC_PROBE_RETRY_BACKOFF_SECONDS", "1")
            ),
            prometheus_url=env("RESEARCH_PROMETHEUS_URL", cls.prometheus_url),
            loki_url=env("RESEARCH_LOKI_URL", cls.loki_url),
            jaeger_url=env("RESEARCH_JAEGER_URL", cls.jaeger_url),
            telemetry_limit=int(env("RESEARCH_TELEMETRY_LIMIT", "5000")),
            trace_limit=int(env("RESEARCH_TRACE_LIMIT", "500")),
            pumba_image=env("RESEARCH_PUMBA_IMAGE", cls.pumba_image),
            pumba_stress_image=env("RESEARCH_PUMBA_STRESS_IMAGE", cls.pumba_stress_image),
            aiops_llm_provider=env("AIOPS_LLM_PROVIDER", cls.aiops_llm_provider),
            aiops_llm_base_url=env("AIOPS_LLM_BASE_URL", cls.aiops_llm_base_url),
            aiops_llm_api_key=env("AIOPS_LLM_API_KEY", cls.aiops_llm_api_key),
            aiops_llm_model=env("AIOPS_LLM_MODEL", cls.aiops_llm_model),
            aiops_llm_timeout_seconds=float(
                env("AIOPS_LLM_TIMEOUT_SECONDS", str(cls.aiops_llm_timeout_seconds))
            ),
        )
