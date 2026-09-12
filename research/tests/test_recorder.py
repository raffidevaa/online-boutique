from pathlib import Path
import tempfile
import unittest

from research.utils.artifacts import append_event, create_run, write_json


class RecorderTests(unittest.TestCase):
    def test_run_directory_is_unique_and_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first_path, first = create_run(Path(temporary))
            second_path, second = create_run(Path(temporary))
            self.assertNotEqual(first["experiment_id"], second["experiment_id"])
            self.assertTrue((first_path / "metadata.json").exists())
            with self.assertRaises(FileExistsError):
                write_json(first_path / "metadata.json", {})
            append_event(first_path, "prepared")
            self.assertIn("prepared", (first_path / "events.jsonl").read_text(encoding="utf-8"))
            self.assertTrue(second_path.exists())


if __name__ == "__main__":
    unittest.main()
