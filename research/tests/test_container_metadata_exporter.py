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


if __name__ == "__main__":
    unittest.main()
