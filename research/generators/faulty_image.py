"""Allowlisted faulty-image definitions and Docker build planning."""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import Any

import yaml

from research.generators.fault import FaultScenario
from research.utils.config import ResearchConfig


class FaultyImageError(RuntimeError):
    """Raised when a faulty-image definition cannot be used safely."""


@dataclass(frozen=True)
class FaultyImageDefinition:
    """A single reproducible, research-owned faulty image variant."""

    scenario_id: str
    service: str
    fault_type: str
    image: str
    baseline_image: str
    platform: str
    dockerfile: Path
    patch: Path
    override: Path
    validator: str
    trigger: str | None = None

    def to_dict(self, root: Path) -> dict[str, object]:
        return {
            "schema_version": 1,
            "scenario_id": self.scenario_id,
            "service": self.service,
            "fault_type": self.fault_type,
            "image": self.image,
            "baseline_image": self.baseline_image,
            "platform": self.platform,
            "dockerfile": str(self.dockerfile.relative_to(root)),
            "patch": str(self.patch.relative_to(root)),
            "override": str(self.override.relative_to(root)),
            "validator": self.validator,
            "trigger": self.trigger,
        }


@dataclass(frozen=True)
class FaultyImagePlan:
    """Non-shell build and Compose plan for one faulty image."""

    definition: FaultyImageDefinition
    build_command: tuple[str, ...]

    def to_dict(self, root: Path) -> dict[str, object]:
        value = self.definition.to_dict(root)
        value["delivery"] = "faulty_image"
        value["build_command"] = list(self.build_command)
        return value


def _required_string(raw: dict[str, Any], key: str, source: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise FaultyImageError(f"{key} must be a non-empty string in {source}")
    return value


def load_faulty_images(config: ResearchConfig) -> dict[str, FaultyImageDefinition]:
    """Load and validate the research-owned faulty-image manifest."""
    path = config.faulty_image_manifest
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FaultyImageError(f"Unable to read faulty-image manifest {path}: {error}") from error
    except yaml.YAMLError as error:
        raise FaultyImageError(f"Invalid faulty-image manifest {path}: {error}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise FaultyImageError(f"Unsupported faulty-image manifest: {path}")
    entries = raw.get("images")
    if not isinstance(entries, dict) or not entries:
        raise FaultyImageError(f"Faulty-image manifest has no images: {path}")

    root = config.research_root.parent
    definitions: dict[str, FaultyImageDefinition] = {}
    for scenario_id, value in entries.items():
        if not isinstance(scenario_id, str) or not scenario_id.startswith("FI-"):
            raise FaultyImageError(f"Invalid scenario ID in {path}: {scenario_id!r}")
        if not isinstance(value, dict):
            raise FaultyImageError(f"Faulty-image entry must be a mapping: {scenario_id}")
        service = _required_string(value, "service", path)
        fault_type = _required_string(value, "fault_type", path)
        image = _required_string(value, "image", path)
        if not image.startswith("research/faulty/"):
            raise FaultyImageError(f"Faulty image is outside the allowlist: {image}")
        baseline_image = _required_string(value, "baseline_image", path)
        platform = _required_string(value, "platform", path)
        if platform != "linux/amd64":
            raise FaultyImageError(
                f"Unsupported faulty-image platform for {scenario_id}: {platform}"
            )
        validator = _required_string(value, "validator", path)
        relative_paths = {}
        for key in ("dockerfile", "patch", "override"):
            relative = _required_string(value, key, path)
            resolved = (root / relative).resolve()
            if root.resolve() not in resolved.parents:
                raise FaultyImageError(f"{key} escapes research repository: {relative}")
            if not resolved.is_file():
                raise FaultyImageError(f"{key} does not exist for {scenario_id}: {resolved}")
            relative_paths[key] = resolved
        if scenario_id in definitions:
            raise FaultyImageError(f"Duplicate faulty-image scenario: {scenario_id}")
        definitions[scenario_id] = FaultyImageDefinition(
            scenario_id=scenario_id,
            service=service,
            fault_type=fault_type,
            image=image,
            baseline_image=baseline_image,
            platform=platform,
            dockerfile=relative_paths["dockerfile"],
            patch=relative_paths["patch"],
            override=relative_paths["override"],
            validator=validator,
            trigger=value.get("trigger"),
        )
    return definitions


class FaultyImageInjector:
    """Build and select only manifest-approved faulty images."""

    def __init__(self, config: ResearchConfig) -> None:
        self.config = config
        self.root = config.research_root.parent
        self.definitions = load_faulty_images(config)

    def definition(self, scenario: FaultScenario) -> FaultyImageDefinition:
        try:
            definition = self.definitions[scenario.scenario_id]
        except KeyError as error:
            raise FaultyImageError(
                f"No faulty-image definition for {scenario.scenario_id}"
            ) from error
        if scenario.category != "code_level":
            raise FaultyImageError("Faulty images are only valid for code-level scenarios")
        if definition.service != scenario.target_service:
            raise FaultyImageError(
                f"Faulty-image target mismatch for {scenario.scenario_id}: "
                f"{definition.service} != {scenario.target_service}"
            )
        if definition.fault_type != scenario.fault_type:
            raise FaultyImageError(
                f"Faulty-image type mismatch for {scenario.scenario_id}: "
                f"{definition.fault_type} != {scenario.fault_type}"
            )
        return definition

    def plan(self, scenario: FaultScenario) -> FaultyImagePlan:
        definition = self.definition(scenario)
        command = [
            "docker",
            "build",
            "--platform",
            definition.platform,
            "--file",
            str(definition.dockerfile),
            "--tag",
            definition.image,
            "--build-arg",
            f"FAULT_PATCH={definition.patch.name}",
            "--label",
            f"research.fault.scenario={definition.scenario_id}",
            "--label",
            f"research.fault.type={definition.fault_type}",
            str(self.root),
        ]
        return FaultyImagePlan(definition, tuple(command))

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, check=False, capture_output=True, text=True)
        except OSError as error:
            raise FaultyImageError(f"Unable to execute Docker command: {error}") from error

    def build(self, scenario: FaultScenario, *, pull: bool = False) -> dict[str, object]:
        """Build one approved image and return its immutable local inspection result."""
        plan = self.plan(scenario)
        command = list(plan.build_command)
        if pull:
            command.insert(2, "--pull")
        result = self._run(command)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "docker build failed"
            raise FaultyImageError(f"Unable to build {plan.definition.image}: {detail}")
        inspection = self._run(
            ["docker", "image", "inspect", "--format", "{{json .}}", plan.definition.image]
        )
        if inspection.returncode != 0:
            raise FaultyImageError(
                f"Built image is not inspectable locally: {plan.definition.image}"
            )
        try:
            inspected = yaml.safe_load(inspection.stdout)
        except yaml.YAMLError as error:
            raise FaultyImageError("Docker returned invalid image inspection JSON") from error
        if not isinstance(inspected, dict):
            raise FaultyImageError("Docker image inspection did not return an object")
        actual_platform = f"{inspected.get('Os')}/{inspected.get('Architecture')}"
        if actual_platform != plan.definition.platform:
            raise FaultyImageError(
                f"Built image platform mismatch for {plan.definition.image}: "
                f"{actual_platform} != {plan.definition.platform}"
            )
        return {
            "status": "built",
            "scenario_id": plan.definition.scenario_id,
            "image": plan.definition.image,
            "platform": actual_platform,
            "image_id": inspected.get("Id"),
            "repo_digests": inspected.get("RepoDigests", []),
            "dockerfile": str(plan.definition.dockerfile.relative_to(self.root)),
            "patch": str(plan.definition.patch.relative_to(self.root)),
            "inspect": inspection.stdout,
        }

    def check_image_available(self, scenario: FaultScenario) -> None:
        definition = self.definition(scenario)
        result = self._run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Os}}/{{.Architecture}}",
                definition.image,
            ]
        )
        if result.returncode != 0:
            raise FaultyImageError(
                f"Faulty image is unavailable locally: {definition.image}. "
                "Build it explicitly before running the scenario."
            )
        actual_platform = result.stdout.strip()
        if actual_platform != definition.platform:
            raise FaultyImageError(
                f"Faulty image platform mismatch for {definition.image}: "
                f"{actual_platform} != {definition.platform}. Rebuild it before running."
            )

    def prepare(self, scenario: FaultScenario) -> dict[str, object]:
        """Verify the image and Compose override without changing runtime state."""
        plan = self.plan(scenario)
        self.check_image_available(scenario)
        from research.service import ComposeApplication

        ComposeApplication(self.config).validate_fault_override(
            plan.definition.override,
            scenario.target_service or "",
            plan.definition.image,
            plan.definition.trigger,
        )
        return {
            "status": "ready",
            "scenario_id": scenario.scenario_id,
            "image": plan.definition.image,
            "override": str(plan.definition.override.relative_to(self.root)),
            "validator": plan.definition.validator,
        }
