import json
import tempfile
import unittest
from pathlib import Path

from price_extractor.results_export import export_results_jsonl_to_csv


class TestResultsExport(unittest.TestCase):
    def test_exports_compact_and_full_rows_to_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "results.jsonl"
            csv_path = Path(tmp) / "results.csv"

            rows = [
                {
                    "unit_id": "unit_a",
                    "index": 1,
                    "url": "https://example.test/a",
                    "status": "completed",
                    "site": "example.test",
                    "validated": True,
                    "price": 11.1,
                    "currency": "EUR",
                    "replay_mode": "http",
                },
                {
                    "unit_id": "unit_b",
                    "index": 2,
                    "url": "https://example.test/b",
                    "status": "failed",
                    "summary": {
                        "site": "example.test",
                        "validated": False,
                        "price": None,
                        "currency": "EUR",
                        "replay_mode": "http",
                        "fallback_mode": "none",
                        "extraction_reason": "price_not_extracted",
                    },
                },
            ]
            jsonl_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            result = export_results_jsonl_to_csv(jsonl_path=str(jsonl_path), csv_path=str(csv_path))

            self.assertEqual(result["rows"], 2)
            content = csv_path.read_text(encoding="utf-8")
            self.assertIn("unit_id,index,url,status", content)
            self.assertIn("unit_a", content)
            self.assertIn("price_not_extracted", content)


if __name__ == "__main__":
    unittest.main()
