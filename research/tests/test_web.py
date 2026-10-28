import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from research.utils import ResearchConfig
from research.web.app import create_app
from research.web.jobs import GuiJobError, JobManager


class WebDashboardTests(unittest.IsolatedAsyncioTestCase):
    def test_gui_defaults_to_all_host_interfaces(self) -> None:
        self.assertEqual(ResearchConfig().gui_host, "0.0.0.0")

    def _config(self, root: Path, token: str = "") -> ResearchConfig:
        return ResearchConfig(
            research_root=Path.cwd() / "research",
            scenario_directory=Path.cwd() / "research" / "generators" / "scenarios",
            experiment_runs=root / "runs",
            experiment_index=root / "index.csv",
            evaluations_dir=root / "evaluations",
            agent_sessions=root / "sessions",
            gui_auth_token=token,
        )

    async def test_scenario_catalog_hides_ground_truth_and_marks_only_runtime_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(self._config(Path(temporary)))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/api/scenarios")
            self.assertEqual(response.status_code, 200)
            scenarios = response.json()["scenarios"]
            self.assertGreaterEqual(len(scenarios), 15)
            self.assertTrue(all("ground_truth" not in scenario for scenario in scenarios))
            ready = [scenario for scenario in scenarios if scenario["runnable_for_gui"]]
            self.assertTrue(ready)
            self.assertTrue(all(scenario["category"] != "code_level" for scenario in ready))

    async def test_static_dashboard_assets_are_served(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(self._config(Path(temporary)))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/")
                script = await client.get("/static/app.js")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Online Boutique RCA Lab", page.text)
            self.assertEqual(script.status_code, 200)
            self.assertIn("loadMetrics", script.text)

    async def test_authentication_is_required_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(self._config(Path(temporary), token="test-token"))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                self.assertEqual((await client.get("/api/health")).status_code, 401)
                response = await client.get(
                    "/api/health",
                    headers={"Authorization": "Bearer test-token"},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ok")

    async def test_prepare_rejects_code_level_without_touching_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(self._config(Path(temporary)))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/jobs/prepare",
                    json={"scenario_id": "FI-CODE-RETURN-01", "task": "analysis"},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("runtime", response.json()["error"])

    def test_event_sanitization_removes_secrets_and_evaluator_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            manager = JobManager(config)
            try:
                with patch.object(manager, "_preflight", return_value={"plan": {}, "warnings": []}):
                    prepared = manager.prepare("FI-NET-DELAY-01", "analysis", 15)
                job_id = prepared["job"]["job_id"]
                manager.emit(
                    job_id,
                    "observation",
                    content="Bearer abc123 password=secret http://10.0.0.1",
                    expected={"root_cause_service": "paymentservice"},
                )
                event = manager.events_after(job_id)[-1]
                serialized = json.dumps(event)
                self.assertNotIn("abc123", serialized)
                self.assertNotIn("secret", serialized)
                self.assertNotIn("10.0.0.1", serialized)
                self.assertNotIn("root_cause_service", serialized)
            finally:
                manager.close()

    def test_prepared_job_accepts_only_one_valid_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(self._config(Path(temporary)))
            try:
                with patch.object(manager, "_preflight", return_value={"plan": {}, "warnings": []}):
                    prepared = manager.prepare("FI-NET-DELAY-01", "analysis", 15)
                job_id = prepared["job"]["job_id"]
                with self.assertRaises(GuiJobError):
                    manager.start(job_id, "wrong-token")
                manager.jobs[job_id].confirmation_expires_at = manager.jobs[job_id].confirmation_expires_at.replace(year=2020)
                with self.assertRaises(GuiJobError):
                    manager.start(job_id, prepared["confirmation_nonce"])
            finally:
                manager.close()

    def test_preflight_starts_stopped_loadgenerator_before_fault_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(self._config(Path(temporary)))
            application = SimpleNamespace(
                validate=Mock(),
                validate_readiness=Mock(
                    return_value={
                        "paymentservice": SimpleNamespace(container_id="container-1")
                    }
                ),
                get_service_state=Mock(
                    return_value=SimpleNamespace(status="exited")
                ),
                start_workload=Mock(),
                wait_for_service_healthy=Mock(),
            )
            observer = SimpleNamespace(check_ready=Mock())
            injector = SimpleNamespace(
                plan=Mock(
                    return_value=SimpleNamespace(to_dict=lambda: {"command": ["allowlisted"]})
                ),
                check_images_available=Mock(),
            )
            try:
                with patch.multiple(
                    "research.web.jobs",
                    ComposeApplication=Mock(return_value=application),
                    Observer=Mock(return_value=observer),
                    PumbaInjector=Mock(return_value=injector),
                ):
                    result = manager._preflight(manager._scenario("FI-NET-DELAY-01"))
                application.start_workload.assert_called_once_with()
                application.wait_for_service_healthy.assert_called_once_with("loadgenerator")
                self.assertTrue(result["workload_started"])
            finally:
                manager.close()

    def test_cancel_stops_only_workload_started_by_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(self._config(Path(temporary)))
            application = SimpleNamespace(stop_workload=Mock())
            try:
                with patch.object(
                    manager,
                    "_preflight",
                    return_value={"plan": {}, "warnings": [], "workload_started": True},
                ), patch(
                    "research.web.jobs.ComposeApplication",
                    return_value=application,
                ):
                    prepared = manager.prepare("FI-NET-DELAY-01", "analysis", 15)
                    result = manager.cancel(prepared["job"]["job_id"])
                application.stop_workload.assert_called_once_with()
                self.assertEqual(result["status"], "cancelled")
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
