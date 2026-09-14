from datetime import UTC, datetime, timedelta
from http.client import RemoteDisconnected
import json
import unittest
from unittest.mock import patch

from research.observer import Observer, ObserverError
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

    @patch("research.observer.telemetry.urlopen")
    def test_capture_converts_disconnected_trace_backend_to_observer_error(
        self, mocked_urlopen: object
    ) -> None:
        def open_endpoint(url: str, timeout: int) -> FakeResponse:
            if "/api/traces" in url:
                raise RemoteDisconnected("Jaeger closed the connection")
            return FakeResponse()

        assert hasattr(mocked_urlopen, "side_effect")
        mocked_urlopen.side_effect = open_endpoint
        observer = Observer(ResearchConfig())
        end = datetime.now(UTC)
        with self.assertRaisesRegex(ObserverError, "Unable to query.*api/traces"):
            observer.capture(end - timedelta(minutes=1), end)


if __name__ == "__main__":
    unittest.main()
