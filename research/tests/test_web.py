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
    async def test_full_run_history_exposes_five_stage_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            run = config.experiment_runs / "EXP-FULL"
            run.mkdir(parents=True)
            (run / "metadata.json").write_text(json.dumps({"experiment_id": run.name, "scenario_id": "FI-NET-LOSS-02"}))
            (run / "result.json").write_text('{"status":"completed"}')
            scores = {"detection": {"detection_accuracy": True}, "localization": {"localization_exact": True}, "analysis": {"combined_rca_correct": True}, "mitigation": {"mitigation_success": True}}
            (run / "agent-evaluation.json").write_text(json.dumps({"task": "full", "stages": scores}))
            for name in ("detection", "localization", "mitigation", "remediation"):
                (run / f"{name}-session.json").write_text('{"trace":[],"submitted":true}')
            (run / "agent-session.json").write_text('{"trace":[],"submitted":true}')
            app = create_app(config)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                listed = (await client.get("/api/runs")).json()["runs"][0]
                detail = (await client.get("/api/runs/EXP-FULL")).json()
            self.assertEqual(listed["task"], "full")
            self.assertTrue(listed["task_result"])
            self.assertEqual(set(detail["agent_sessions"]), {"live_detection", "live_localization", "live_rca", "remediation_recommendation", "live_mitigation"})

    def test_prepare_accepts_only_full_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(self._config(Path(temporary)))
            with patch.object(manager, "_preflight", return_value={"scenario_id": "FI-NET-LOSS-02"}):
                prepared = manager.prepare("FI-NET-LOSS-02", "full", 3)
                self.assertEqual(prepared["job"]["task"], "full")
                manager.cancel(prepared["job"]["job_id"])
                with self.assertRaises(GuiJobError):
                    manager.prepare("FI-NET-LOSS-02", "analysis", 3)
            manager.close()

    def test_job_marks_injector_failure_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager(self._config(Path(temporary)))
            with patch.object(manager, "_preflight", return_value={"scenario_id": "FI-NET-LOSS-02"}):
                job_id = manager.prepare("FI-NET-LOSS-02", "full", 2)["job"]["job_id"]

            async def failed_run(*args, **kwargs):
                return {"status": "injector_failed", "experiment_id": "EXP-FAIL"}

            with patch("research.web.jobs.create_orchestrator") as factory, patch("research.web.jobs.write_dataset_index"):
                factory.return_value.run_live_problem = failed_run
                manager._run(job_id)
            job = manager.get(job_id)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.experiment_id, "EXP-FAIL")
            manager.close()

    async def test_current_mitigation_runs_keep_failed_status_out_of_success_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            for name, status in (("EXP-FAIL", "injector_failed"), ("EXP-OK", "completed")):
                run = config.experiment_runs / name
                run.mkdir(parents=True)
                (run / "metadata.json").write_text(json.dumps({"experiment_id": name, "scenario_id": "FI-NET-LOSS-02"}))
                (run / "result.json").write_text(json.dumps({"status": status}))
                (run / "agent-session.json").write_text(json.dumps({"problem_id": "FI-NET-LOSS-02-mitigation", "submitted": True, "results": {"mitigation_success": True}}))
                (run / "agent-evaluation.json").write_text(json.dumps({"task": "mitigation"}))
            app = create_app(config)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                runs = (await client.get("/api/runs")).json()["runs"]
                summary = (await client.get("/api/metrics/summary")).json()["summary"]
                detail = (await client.get("/api/runs/EXP-OK")).json()
            self.assertIsNone(next(row for row in runs if row["experiment_id"] == "EXP-FAIL")["task_result"])
            self.assertEqual(summary["mitigation"], {"total": 2, "completed": 1, "failed": 1, "partial": 0, "successful": 1})
            self.assertIn("live_mitigation", detail["agent_sessions"])

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
                detail_page = await client.get("/runs/EXP-TEST")
                script = await client.get("/static/app.js")
            self.assertEqual(page.status_code, 200)
            self.assertIn("RCA Lab · Online Boutique", page.text)
            self.assertEqual(detail_page.status_code, 200)
            self.assertIn('id="run-page"', detail_page.text)
            self.assertEqual(script.status_code, 200)
            self.assertIn("recommendationMarkup", script.text)
            self.assertNotIn('data-tab="overview"', page.text)

    async def test_history_detail_shows_single_rca_and_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            run = config.experiment_runs / "EXP-TEST-01"
            run.mkdir(parents=True)
            (run / "metadata.json").write_text(
                json.dumps({"experiment_id": run.name, "scenario_id": "FI-NET-DELAY-01"}),
                encoding="utf-8",
            )
            for name, solution in (
                ("agent-session.json", {"faulty_components": ["paymentservice"]}),
                ("remediation-session.json", {"action_type": "manual_investigation"}),
            ):
                (run / name).write_text(
                    json.dumps({
                        "trace": [
                            {"role": "assistant", "content": '{"action":"get_metrics"}'},
                            {"role": "env", "content": "Bearer abc123 password=secret"},
                        ],
                        "solution": solution,
                    }),
                    encoding="utf-8",
                )
            app = create_app(config)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                listed = await client.get("/api/runs")
                detail = await client.get(f"/api/runs/{run.name}")
            self.assertEqual(listed.status_code, 200)
            self.assertTrue(listed.json()["runs"][0]["has_remediation_recommendation"])
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(
                set(detail.json()["agent_sessions"]),
                {"live_rca", "remediation_recommendation"},
            )
            self.assertNotIn("ground_truth", detail.json())
            self.assertNotIn("abc123", detail.text)
            self.assertNotIn("secret", detail.text)
            self.assertIn("get_metrics", detail.text)

    async def test_live_run_detail_retains_legacy_session_and_withholds_truth_until_done(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            run = config.experiment_runs / "EXP-LIVE"
            run.mkdir(parents=True)
            (run / "agent-session.json").write_text(
                json.dumps({"trace": [{"role": "assistant", "content": "submit"}]}),
                encoding="utf-8",
            )
            (run / "ground_truth.json").write_text(
                json.dumps({"root_cause_service": "paymentservice"}),
                encoding="utf-8",
            )
            (run / "result.json").write_text('{"status":"running"}', encoding="utf-8")
            app = create_app(config)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail = await client.get("/api/runs/EXP-LIVE")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["agent_sessions"]["live_rca"], detail.json()["agent_session"])
            self.assertNotIn("ground_truth", detail.json())
            listed = app.state.jobs.list_runs()
            self.assertFalse(listed[0]["has_remediation_recommendation"])
            (run / "result.json").write_text('{"status":"completed"}', encoding="utf-8")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                completed = await client.get("/api/runs/EXP-LIVE")
            self.assertEqual(
                completed.json()["ground_truth"]["root_cause_service"],
                "paymentservice",
            )

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
                    json={"scenario_id": "FI-CODE-RETURN-01", "task": "full"},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("runtime", response.json()["error"])

    def test_event_sanitization_removes_secrets_and_evaluator_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = self._config(Path(temporary))
            manager = JobManager(config)
            try:
                with patch.object(manager, "_preflight", return_value={"plan": {}, "warnings": []}):
                    prepared = manager.prepare("FI-NET-DELAY-01", "full", 15)
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
                    prepared = manager.prepare("FI-NET-DELAY-01", "full", 15)
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
                    prepared = manager.prepare("FI-NET-DELAY-01", "full", 15)
                    result = manager.cancel(prepared["job"]["job_id"])
                application.stop_workload.assert_called_once_with()
                self.assertEqual(result["status"], "cancelled")
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
