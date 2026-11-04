"""Checks for the faulty-image adapter used by live agent runs."""

from datetime import UTC, datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from research.agent.live import ImageFaultRuntime
from research.generators import load_scenario


class ImageLiveTests(unittest.TestCase):
    def test_start_validates_fault_and_cleanup_restores_baseline(self) -> None:
        scenario = load_scenario(Path(
            "research/generators/scenarios/code-level-01-incorrect-return-currency.yaml"
        ))
        config = SimpleNamespace(
            research_root=Path("research"), semantic_probe_stabilization_seconds=0,
            faulty_image_manifest=Path("research/generators/faulty_images/manifest.yaml"),
        )
        application = Mock()
        observer = Mock()
        observer.capture.return_value = {}
        adapter = ImageFaultRuntime(config, application, observer)
        plan = SimpleNamespace(definition=SimpleNamespace(
            override=Path("override.yml"), validator="currency_display",
        ))
        adapter.scenario = scenario
        with tempfile.TemporaryDirectory() as temporary:
            adapter.run_path = Path(temporary)
            probe = SimpleNamespace(normalized={}, requests=[])
            with patch("research.agent.live.run_semantic_probe", return_value=probe), \
                 patch("research.agent.live.build_semantic_observation", return_value={"status": "completed"}), \
                 patch("research.agent.live.validate_semantic", return_value=SimpleNamespace(to_dict=lambda: {"status": "passed"})):
                running = adapter.start(plan)
            self.assertTrue((adapter.run_path / "semantic-validation.json").exists())
            self.assertIsInstance(adapter.wait(running, 1), subprocess.CompletedProcess)
            self.assertIsInstance(running.ended_at, datetime)
            self.assertEqual(running.ended_at.tzinfo, UTC)
            application.restore_service.assert_called_once_with("currencyservice")

    def test_failed_probe_restores_baseline(self) -> None:
        scenario = load_scenario(Path(
            "research/generators/scenarios/code-level-01-incorrect-return-currency.yaml"
        ))
        config = SimpleNamespace(
            research_root=Path("research"), semantic_probe_stabilization_seconds=0,
            faulty_image_manifest=Path("research/generators/faulty_images/manifest.yaml"),
        )
        application = Mock()
        adapter = ImageFaultRuntime(config, application, Mock())
        adapter.scenario = scenario
        plan = SimpleNamespace(definition=SimpleNamespace(override=Path("override.yml")))
        with tempfile.TemporaryDirectory() as temporary:
            adapter.run_path = Path(temporary)
            with patch("research.agent.live.run_semantic_probe", side_effect=[Mock(), RuntimeError("probe failed")]):
                with self.assertRaisesRegex(RuntimeError, "probe failed"):
                    adapter.start(plan)
        application.restore_service.assert_called_once_with("currencyservice")
