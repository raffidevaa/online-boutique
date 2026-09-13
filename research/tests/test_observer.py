from datetime import UTC, datetime, timedelta
import json
import unittest
from unittest.mock import patch

from research.observer import Observer
from research.utils import ResearchConfig


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"status": "success", "data": {}}).encode("utf-8")


class ObserverTests(unittest.TestCase):
    @patch("research.observer.telemetry.urlopen", return_value=FakeResponse())
    def test_capture_collects_metrics_logs_and_alerts(self, _: object) -> None:
        observer = Observer(ResearchConfig())
        end = datetime.now(UTC)
        captured = observer.capture(end - timedelta(minutes=1), end)
        self.assertEqual(set(captured), {"metrics", "logs", "alerts", "traces"})
        self.assertIn("services_up", captured["metrics"]["queries"])
        self.assertIn("frontend", captured["traces"]["services"])


if __name__ == "__main__":
    unittest.main()
