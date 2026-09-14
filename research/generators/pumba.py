"""Allowlisted Pumba planning and execution for runtime fault scenarios."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from research.generators.fault import FaultScenario, ScenarioError
from research.utils.config import ResearchConfig


class InjectorError(RuntimeError):
    """Raised when a scenario cannot safely be delivered by Pumba."""


@dataclass(frozen=True)
class PumbaPlan:
    """A fully tokenized command; no shell parsing is used."""

    scenario_id: str
    target_service: str
    target_container_id: str
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "delivery": "pumba",
            "scenario_id": self.scenario_id,
            "target_service": self.target_service,
            "target_container_id": self.target_container_id,
            "command": list(self.command),
        }


class PumbaInjector:
    """Translate only the validated runnable taxonomy into Pumba invocations."""

    def __init__(self, config: ResearchConfig) -> None:
        self.config = config

    @staticmethod
    def _positive_int(parameters: dict[str, object], name: str) -> int:
        value = parameters.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InjectorError(f"{name} must be a positive integer")
        return value

    def plan(self, scenario: FaultScenario, container_id: str) -> PumbaPlan:
        """Build a Pumba command for one ready scenario and one exact container."""
        if not scenario.runnable:
            if scenario.execution_status == "experimental_unavailable":
                raise InjectorError(
                    f"{scenario.scenario_id} is catalogued but unavailable until pilot "
                    "telemetry proves a target and attributable signal"
                )
            raise InjectorError(
                f"{scenario.scenario_id} requires an approved faulty-image adapter; "
                "it cannot be injected with Pumba"
            )
        if not scenario.target_service:
            raise InjectorError("Runnable scenario has no target service")
        duration = self._positive_int(scenario.parameters, "duration_seconds")
        command: list[str] = [
            "docker",
            "run",
            "--rm",
            "--pull",
            "never",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            self.config.pumba_image,
        ]
        if scenario.fault_type == "cpu_hog":
            workers = self._positive_int(scenario.parameters, "cpu_workers")
            command.extend(
                [
                    "stress",
                    "--duration",
                    f"{duration}s",
                    "--stress-image",
                    self.config.pumba_stress_image,
                    "--pull-image=false",
                    "--inject-cgroup",
                    f"--stressors=--cpu {workers} --timeout {duration}s",
                    container_id,
                ]
            )
        elif scenario.fault_type == "memory_pressure":
            workers = self._positive_int(scenario.parameters, "memory_workers")
            memory_mb = self._positive_int(scenario.parameters, "memory_mb_per_worker")
            command.extend(
                [
                    "stress",
                    "--duration",
                    f"{duration}s",
                    "--stress-image",
                    self.config.pumba_stress_image,
                    "--pull-image=false",
                    "--inject-cgroup",
                    (
                        f"--stressors=--vm {workers} --vm-bytes {memory_mb}M "
                        f"--vm-keep --timeout {duration}s"
                    ),
                    container_id,
                ]
            )
        elif scenario.fault_type == "network_delay":
            delay_ms = self._positive_int(scenario.parameters, "delay_ms")
            jitter_ms = self._positive_int(scenario.parameters, "jitter_ms")
            command.extend(
                [
                    "netem",
                    "--duration",
                    f"{duration}s",
                    "delay",
                    "--time",
                    str(delay_ms),
                    "--jitter",
                    str(jitter_ms),
                    container_id,
                ]
            )
        elif scenario.fault_type == "packet_loss":
            loss_percent = self._positive_int(scenario.parameters, "loss_percent")
            if loss_percent > 100:
                raise InjectorError("loss_percent cannot exceed 100")
            command.extend(
                [
                    "netem",
                    "--duration",
                    f"{duration}s",
                    "loss",
                    "--percent",
                    str(loss_percent),
                    container_id,
                ]
            )
        else:
            raise InjectorError(f"Pumba delivery is not implemented for {scenario.fault_type}")
        return PumbaPlan(
            scenario_id=scenario.scenario_id,
            target_service=scenario.target_service,
            target_container_id=container_id,
            command=tuple(command),
        )

    def required_images(self, scenario: FaultScenario) -> tuple[str, ...]:
        """Return the pinned images required by a validated runnable scenario."""
        self.plan(scenario, "<image-preflight>")
        images = [self.config.pumba_image]
        if scenario.fault_type in {"cpu_hog", "memory_pressure"}:
            images.append(self.config.pumba_stress_image)
        return tuple(images)

    @staticmethod
    def _inspect_image(image: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["docker", "image", "inspect", image],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise InjectorError(f"Unable to inspect required image {image}: {error}") from error

    @staticmethod
    def _missing_image_error(image: str) -> InjectorError:
        return InjectorError(
            f"Required image is unavailable locally: {image}. Pull and verify it before "
            "running a fault; the runner will not pull images implicitly."
        )

    def check_images_available(self, scenario: FaultScenario) -> None:
        """Fail before creating experiment data when a pinned required image is absent."""
        for image in self.required_images(scenario):
            result = self._inspect_image(image)
            if result.returncode != 0:
                raise self._missing_image_error(image)

    def prepare_images(
        self, scenario: FaultScenario, *, pull: bool = False
    ) -> dict[str, object]:
        """Verify required images, optionally pulling only missing pinned images."""
        images = self.required_images(scenario)
        pulled: list[str] = []
        statuses: list[dict[str, object]] = []
        for image in images:
            result = self._inspect_image(image)
            if result.returncode != 0:
                if not pull:
                    raise self._missing_image_error(image)
                try:
                    pull_result = subprocess.run(
                        ["docker", "pull", image],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                except OSError as error:
                    raise InjectorError(f"Unable to pull required image {image}: {error}") from error
                if pull_result.returncode != 0:
                    detail = pull_result.stderr.strip() or "docker pull returned a non-zero status"
                    raise InjectorError(f"Unable to pull required image {image}: {detail}")
                pulled.append(image)
                result = self._inspect_image(image)
            if result.returncode != 0:
                raise self._missing_image_error(image)
            statuses.append({"image": image, "available": True, "pulled": image in pulled})
        return {"status": "ready", "pulled": pulled, "images": statuses}

    @staticmethod
    def execute(plan: PumbaPlan, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        """Run the preplanned command without a shell and with a bounded timeout."""
        try:
            return subprocess.run(
                plan.command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InjectorError(f"Pumba execution failed: {error}") from error


def plan_pumba(
    scenario: FaultScenario, container_id: str, config: ResearchConfig
) -> PumbaPlan:
    """Convenience entry point used by tests and the experiment CLI."""
    try:
        return PumbaInjector(config).plan(scenario, container_id)
    except ScenarioError as error:
        raise InjectorError(str(error)) from error
