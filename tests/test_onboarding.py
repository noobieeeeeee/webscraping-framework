import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from price_extractor.agentic.onboarding import build_onboarding_proposal


class TestOnboardingProposal(unittest.TestCase):
    def test_builds_proposal_from_minimal_run_dir(self) -> None:
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
                json.dumps(
                    {
                        "request_templates": {},
                        "network_traces": [],
                        "option_catalog": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (run_dir / "network_traces.json").write_text(
                json.dumps(
                    [
                        {
                            "url": "https://api.example.test/pricing",
                            "method": "POST",
                            "status": 200,
                            "resource_type": "xhr",
                            "response_content_type": "application/json",
                            "response_body_preview": "{\"total_gross_value\": 12.34}",
                            "post_data": "{\"quantity\":250}",
                        }
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            (run_dir / "option_catalog.json").write_text("[]", encoding="utf-8")

            proposal = build_onboarding_proposal(
                run_dir=str(run_dir),
                knowledge_db=str(run_dir / "knowledge.db"),
                top_n=5,
            )

            self.assertEqual(proposal.site_name, "example")
            self.assertIn("example.test/product", proposal.product_url)
            self.assertGreaterEqual(len(proposal.top_endpoints), 1)
            self.assertIn("class ExampleSiteAdapter", proposal.adapter_stub)
            self.assertIn("def build_http_replay_candidates", proposal.adapter_stub)
