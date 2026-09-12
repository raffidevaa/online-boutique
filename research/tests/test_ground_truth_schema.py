import json
from pathlib import Path
import unittest


class GroundTruthSchemaTests(unittest.TestCase):
    def test_schema_matches_the_required_fault_record_fields(self) -> None:
        path = Path(__file__).resolve().parents[1] / "experiments" / "ground-truth.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"], {"const": 1})
        self.assertTrue(
            {
                "experiment_id",
                "scenario_id",
                "root_cause_service",
                "fault_category",
                "fault_type",
                "target_service",
                "injection_start",
                "injection_end",
                "severity",
            }.issubset(schema["required"])
        )


if __name__ == "__main__":
    unittest.main()
