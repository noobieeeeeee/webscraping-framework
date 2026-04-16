import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from price_extractor.agentic.gemini_onboarding import build_gemini_onboarding_payload


class TestGeminiOnboarding(unittest.TestCase):
    def test_build_payload_uses_gemini_defaults(self) -> None:
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "spec.json").write_text(
                json.dumps(
                    {
                        "site_name": "example",
                        "url": "https://example.test/product",
                        "product_type": "unknown",
                        "options": {"quantity": 250},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (run_dir / "summary.json").write_text(
                json.dumps({"site": "example", "url": "https://example.test/product"}, indent=2),
                encoding="utf-8",
            )
            (run_dir / "bootstrap.json").write_text(
                json.dumps({"request_templates": {}}, indent=2),
                encoding="utf-8",
            )
            (run_dir / "network_traces.json").write_text("[]", encoding="utf-8")
            (run_dir / "option_catalog.json").write_text("[]", encoding="utf-8")

            payload = build_gemini_onboarding_payload(
                run_dir=str(run_dir),
                knowledge_db=str(run_dir / "knowledge.db"),
                llm_enable=False,
            )

            self.assertEqual(payload.get("provider"), "gemini")
            defaults = dict(payload.get("providerDefaults") or {})
            self.assertEqual(defaults.get("api_key_env"), "GEMINI_API_KEY")
            self.assertIn("framework", payload)
            self.assertIn("proposal", payload)


if __name__ == "__main__":
    unittest.main()
