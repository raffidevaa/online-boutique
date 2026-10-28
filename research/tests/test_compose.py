import unittest
from unittest.mock import patch

from research.service import CommandResult, ComposeApplication
from research.utils import ResearchConfig


class ComposeTests(unittest.TestCase):
    def test_redacts_nested_secret_values(self) -> None:
        source = {"API_KEY": "value", "nested": {"password": "value", "safe": "ok"}}
        self.assertEqual(
            ComposeApplication._redact(source),
            {"API_KEY": "<redacted>", "nested": {"password": "<redacted>", "safe": "ok"}},
        )

    def test_secret_values_inside_lists_are_redacted_by_parent_key(self) -> None:
        source = {"tokens": ["first", "second"]}
        self.assertEqual(ComposeApplication._redact(source), {"tokens": "<redacted>"})

    def test_start_workload_uses_only_the_allowlisted_profile_service(self) -> None:
        application = ComposeApplication(ResearchConfig())
        with patch.object(
            ComposeApplication,
            "_run",
            return_value=CommandResult("", "", 0),
        ) as run:
            application.start_workload()
        command = run.call_args.args[0]
        self.assertIn("--profile", command)
        self.assertIn("workload", command)
        self.assertEqual(command[-1], "loadgenerator")
        self.assertNotIn("shell", command)


if __name__ == "__main__":
    unittest.main()
