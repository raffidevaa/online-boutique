import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).parents[1]
    / "observability"
    / "container-metadata-exporter"
    / "server.py"
)
SPEC = importlib.util.spec_from_file_location("container_metadata_exporter", MODULE_PATH)
assert SPEC and SPEC.loader
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


class ContainerMetadataExporterTests(unittest.TestCase):
    def test_render_metrics_maps_compose_container(self) -> None:
        output = exporter.render_metrics(
            [
                {
                    "Id": "abc123",
                    "Image": "example/frontend:v1",
                    "Labels": {
                        "com.docker.compose.project": "compose",
                        "com.docker.compose.service": "frontend",
                    },
                }
            ]
        ).decode()
        self.assertIn('name="abc123"', output)
        self.assertIn('service="frontend"', output)
        self.assertIn('compose_container_state{name="abc123",service="frontend",state="unknown"} 1', output)
        self.assertIn('compose_container_health{name="abc123",service="frontend",health="none"} 1', output)

    def test_render_metrics_ignores_other_project_and_unlabelled(self) -> None:
        output = exporter.render_metrics(
            [
                {"Id": "other", "Labels": {"com.docker.compose.service": "api"}},
                {
                    "Id": "unlabelled",
                    "Labels": {"com.docker.compose.project": "compose"},
                },
            ]
        ).decode()
        self.assertNotIn("other", output)
        self.assertNotIn("unlabelled", output)

    def test_render_metrics_escapes_label_values(self) -> None:
        output = exporter.render_metrics(
            [
                {
                    "Id": "abc",
                    "Image": "image",
                    "Labels": {
                        "com.docker.compose.project": "compose",
                        "com.docker.compose.service": 'service"name',
                    },
                }
            ]
        ).decode()
        self.assertIn('service="service\\"name"', output)

    def test_render_metrics_exposes_runtime_state_and_health(self) -> None:
        output = exporter.render_metrics(
            [
                {
                    "Id": "stopped",
                    "Image": "example/api:v2",
                    "State": "exited",
                    "Status": "Exited (1) 5 seconds ago",
                    "Labels": {
                        "com.docker.compose.project": "compose",
                        "com.docker.compose.service": "api",
                    },
                }
            ]
        ).decode()
        self.assertIn('compose_container_state{name="stopped",service="api",state="exited"} 1', output)
        self.assertIn('compose_container_health{name="stopped",service="api",health="none"} 1', output)
        self.assertNotIn('compose_container_info{name="stopped"', output)

    def test_render_metrics_reads_health_from_docker_list_status(self) -> None:
        output = exporter.render_metrics(
            [{
                "Id": "healthy", "State": "running", "Status": "Up 2 minutes (healthy)",
                "Labels": {"com.docker.compose.project": "compose", "com.docker.compose.service": "api"},
            }]
        ).decode()
        self.assertIn('compose_container_health{name="healthy",service="api",health="healthy"} 1', output)


if __name__ == "__main__":
    unittest.main()
