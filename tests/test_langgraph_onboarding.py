import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from price_extractor.agentic.langgraph_onboarding import (
    _build_review_patch_plan,
    _build_llm_run_evidence,
    _load_dotenv_file,
    _resolve_llm_settings,
    build_langgraph_onboarding_payload,
)


class TestLangGraphOnboarding(unittest.TestCase):
    def test_build_llm_run_evidence_sanitizes_tokens_and_extracts_price_signals(self) -> None:
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "spec.json").write_text(
                json.dumps(
                    {
                        "site_name": "example",
                        "url": "https://example.test/product",
                        "product_type": "unknown",
                        "expected_currency": "EUR",
                        "options": {"quantity": 1000},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (run_dir / "summary.json").write_text(
                json.dumps({"site": "example", "url": "https://example.test/product"}, indent=2),
                encoding="utf-8",
            )

            token_value = "SUPER-SECRET-CSRF-TOKEN"
            (run_dir / "network_traces.json").write_text(
                json.dumps(
                    [
                        {
                            "url": "https://example.test/product",
                            "method": "POST",
                            "resource_type": "xhr",
                            "status": 200,
                            "request_content_type": "application/x-www-form-urlencoded",
                            "response_content_type": "application/json; charset=utf-8",
                            "post_data": f"csrf_antiforge={token_value}&qty=1000&color=4%2F4",
                            "post_data_preview": f"csrf_antiforge={token_value}&qty=1000",
                            "response_body_preview": json.dumps(
                                {
                                    "Result": 0,
                                    "WS-Ajax-start": "<script>productPageObj.prPrice='141.50';</script>",
                                }
                            ),
                        }
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )

            evidence = _build_llm_run_evidence(
                run_dir=str(run_dir),
                top_endpoints=[{"method": "POST", "url": "https://example.test/product", "score": 10}],
                max_endpoint_evidence=3,
                max_pricing_candidates=3,
            )

            blob = json.dumps(evidence, ensure_ascii=True)
            self.assertNotIn(token_value, blob)

            endpoint_evidence = list(evidence.get("endpoint_evidence") or [])
            self.assertGreaterEqual(len(endpoint_evidence), 1)

            signals = dict((endpoint_evidence[0] or {}).get("response_signals") or {})
            self.assertIn("141.50", list(signals.get("prPrice_values") or []))
            self.assertIn("Result", list(signals.get("json_keys_sample") or []))

            keys = list((endpoint_evidence[0] or {}).get("post_data_keys_sample") or [])
            self.assertIn("csrf_antiforge", keys)

    def test_load_dotenv_file_sets_missing_env(self) -> None:
        with TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("TEST_API_KEY=from-dotenv\n", encoding="utf-8")

            original = os.environ.get("TEST_API_KEY")
            os.environ.pop("TEST_API_KEY", None)
            try:
                loaded = _load_dotenv_file(str(dotenv))
            finally:
                if original is None:
                    os.environ.pop("TEST_API_KEY", None)
                else:
                    os.environ["TEST_API_KEY"] = original

        self.assertIn("TEST_API_KEY", loaded)

    def test_build_review_patch_plan_has_review_guardrails(self) -> None:
        plan = _build_review_patch_plan(
            proposal={
                "site_name": "example",
                "top_endpoints": [{"url": "https://api.example/pricing"}],
            },
            llm_suggestions={"recommendations": ["Add adapter rewrite for quantity"]},
        )
        self.assertEqual(plan.get("kind"), "review_patch_plan_v1")
        self.assertTrue(plan.get("reviewRequired"))
        self.assertFalse(plan.get("autoApply"))

    def test_resolve_llm_settings_reads_env(self) -> None:
        previous = os.environ.get("UNITTEST_LLM_KEY")
        os.environ["UNITTEST_LLM_KEY"] = "secret-test-key"
        try:
            settings = _resolve_llm_settings(
                llm_enable=True,
                llm_model="gpt-4.1-mini",
                llm_base_url="https://example.invalid/v1",
                llm_api_key_env="UNITTEST_LLM_KEY",
                llm_timeout_seconds=15.0,
            )
        finally:
            if previous is None:
                os.environ.pop("UNITTEST_LLM_KEY", None)
            else:
                os.environ["UNITTEST_LLM_KEY"] = previous

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["model"], "gpt-4.1-mini")
        self.assertEqual(settings["base_url"], "https://example.invalid/v1")
        self.assertEqual(settings["api_key_env"], "UNITTEST_LLM_KEY")
        self.assertTrue(settings["api_key_present"])

    def test_falls_back_when_langgraph_not_installed(self) -> None:
        # This test is written to pass even when LangGraph extras are not installed.
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

            payload = build_langgraph_onboarding_payload(
                run_dir=str(run_dir),
                knowledge_db=str(run_dir / "knowledge.db"),
                top_n=3,
                llm_enable=True,
                llm_api_key_env="MISSING_API_KEY_FOR_TEST",
            )

            self.assertIn("framework", payload)
            self.assertIn("llm", payload)
            self.assertIn("proposal", payload)
            self.assertIn("patchPlan", payload)
            self.assertEqual(payload["proposal"].get("site_name"), "example")
