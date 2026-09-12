import unittest

from research.service import ComposeApplication


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


if __name__ == "__main__":
    unittest.main()
