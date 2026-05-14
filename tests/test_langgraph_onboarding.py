import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from price_extractor.agentic.langgraph_onboarding import (
    _compact_critic_context,
    _build_critic_prompt_registry,
    _build_deterministic_gate,
    _build_extraction_recipe,
    _coerce_llm_critic_result,
    _build_patch_critique,
    _build_promotion_decision,
    _build_recipe_critique,
    _run_llm_critic_pass,
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

    def test_build_review_patch_plan_includes_sanitized_proposed_patches(self) -> None:
        plan = _build_review_patch_plan(
            proposal={
                "site_name": "example",
                "top_endpoints": [{"url": "https://api.example/pricing"}],
                "trace_template_hints": [
                    {
                        "endpoint_url": "https://api.example/pricing",
                        "method": "POST",
                        "candidate_mutations": [{"option_key": "quantity", "path": "quantity"}],
                    }
                ],
            },
            llm_suggestions={
                "site_adapter_code": "class ExampleSiteAdapter:\n    pass",
                "proposed_file_patches": [
                    {
                        "path": "src/price_extractor/site_adapters.py",
                        "operation": "update",
                        "content": "# safe content",
                    },
                    {
                        "path": "src/price_extractor/agents.py",
                        "operation": "update",
                        "content": "# disallowed path",
                    },
                    {
                        "path": "tests/test_regressions.py",
                        "operation": "update",
                        "content": "Authorization: Bearer secret-token",
                    },
                ],
            },
        )

        self.assertEqual(plan.get("patchSchema"), "v2")
        patches = list(plan.get("proposedFilePatches") or [])
        self.assertEqual(len(patches), 1)
        self.assertEqual(str(patches[0].get("path") or ""), "src/price_extractor/site_adapters.py")
        selected_hint = dict(plan.get("selectedTraceTemplateHint") or {})
        self.assertEqual(str(selected_hint.get("endpoint_url") or ""), "https://api.example/pricing")

    def test_build_review_patch_plan_generates_structured_site_adapter_patches(self) -> None:
        plan = _build_review_patch_plan(
            proposal={
                "site_name": "example",
                "top_endpoints": [{"url": "https://api.example/pricing"}],
                "trace_template_hints": [
                    {
                        "endpoint_url": "https://api.example/pricing",
                        "method": "POST",
                        "candidate_mutations": [{"option_key": "quantity", "path": "quantity"}],
                    }
                ],
            },
            llm_suggestions={
                "site_adapter_code": "@dataclass(frozen=True)\nclass ExampleSiteAdapter:\n    pass",
            },
        )

        evidence = dict(plan.get("rewriteEvidenceGate") or {})
        self.assertTrue(bool(evidence.get("sufficient")))

        patches = list(plan.get("proposedFilePatches") or [])
        site_patches = [
            row for row in patches if isinstance(row, dict) and str(row.get("path") or "") == "src/price_extractor/site_adapters.py"
        ]
        self.assertEqual(len(site_patches), 2)
        self.assertTrue(any(str(row.get("anchor") or "") == "_DEFAULT_ADAPTERS: list[SiteAdapter] | None = None" for row in site_patches))
        self.assertTrue(any(str(row.get("anchor") or "") == "_DEFAULT_ADAPTERS = [" for row in site_patches))

    def test_build_review_patch_plan_refuses_site_adapter_patch_without_trace_hints(self) -> None:
        plan = _build_review_patch_plan(
            proposal={
                "site_name": "example",
                "top_endpoints": [{"url": "https://api.example/pricing"}],
                "trace_template_hints": [],
            },
            llm_suggestions={
                "site_adapter_code": "class ExampleSiteAdapter:\n    pass",
                "proposed_file_patches": [
                    {
                        "path": "src/price_extractor/site_adapters.py",
                        "operation": "update",
                        "content": "class ExampleSiteAdapter:\n    pass",
                    }
                ],
            },
        )

        evidence = dict(plan.get("rewriteEvidenceGate") or {})
        self.assertFalse(bool(evidence.get("sufficient")))

        patches = list(plan.get("proposedFilePatches") or [])
        self.assertFalse(any(str(row.get("path") or "") == "src/price_extractor/site_adapters.py" for row in patches if isinstance(row, dict)))
        safety_checks = [str(row) for row in list(plan.get("safetyChecks") or [])]
        self.assertTrue(any("Refuse generated SiteAdapter patch due to insufficient rewrite evidence" in row for row in safety_checks))

    def test_compact_critic_context_truncates_and_preserves_core_fields(self) -> None:
        context = {
            "proposal": {
                "site_name": "example",
                "product_url": "https://example.test/product",
                "requires_session": True,
                "blockers": ["session_required"],
                "top_endpoints": [
                    {
                        "method": "POST",
                        "url": "https://api.example.test/pricing",
                        "score": 10,
                        "confidence": 0.9,
                    }
                ],
                "trace_template_hints": [
                    {
                        "endpoint_url": "https://api.example.test/pricing",
                        "method": "POST",
                        "body_kind": "json",
                        "candidate_mutations": [
                            {"option_key": "quantity", "path": "payload.quantity" * 50}
                        ],
                    }
                ],
            },
            "extractionRecipe": {
                "replayStrategy": {
                    "mode": "request_only_http_or_adapter",
                    "selectedEndpoint": {"method": "POST", "url": "https://api.example.test/pricing"},
                },
                "requestSynthesisStrategy": {
                    "templateDriven": True,
                    "candidateMutations": [
                        {"option_key": "quantity", "path": "payload.quantity" * 50}
                    ],
                },
                "quantityStrategy": {"required": True},
                "validationStrategy": {"requiresAcceptedConfigProofForValidated": True},
                "runtimeSafetyPolicy": {"requiresApprovedEgressProxy": True},
            },
            "patchPlan": {
                "selectedEndpoint": "https://api.example.test/pricing",
                "rewriteEvidenceGate": {"sufficient": True},
                "proposedFilePatches": [
                    {
                        "path": "src/price_extractor/site_adapters.py",
                        "operation": "insert",
                        "changeScope": "adapter_only",
                        "intent": "insert generated adapter",
                        "anchor": "_DEFAULT_ADAPTERS = [",
                        "content": "x" * 5000,
                    }
                ],
            },
            "recipeCritique": {"overallVerdict": "pass", "critics": []},
        }

        compact = _compact_critic_context(
            context_payload=context,
            stage_name="recipe",
            max_chars=1800,
        )

        self.assertEqual(str(dict(compact.get("proposal") or {}).get("site_name") or ""), "example")
        rendered = json.dumps(compact, ensure_ascii=True)
        self.assertLessEqual(len(rendered), 1800)

    def test_llm_critics_execution_exposes_context_controls(self) -> None:
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
                llm_critics_enable=True,
                llm_critics_context_mode="compact",
                llm_critics_context_max_chars=1500,
                llm_api_key_env="MISSING_API_KEY_FOR_TEST",
            )

            llm = dict(payload.get("llm") or {})
            self.assertEqual(str(llm.get("llm_critics_context_mode") or ""), "compact")
            self.assertEqual(int(llm.get("llm_critics_context_max_chars") or 0), 1500)
            execution = dict(dict(payload.get("llmCritics") or {}).get("execution") or {})
            self.assertEqual(str(execution.get("contextMode") or ""), "compact")
            self.assertEqual(int(execution.get("contextMaxChars") or 0), 1500)

    def test_build_review_patch_plan_drops_unresolvable_adapter_import_patch(self) -> None:
        plan = _build_review_patch_plan(
            proposal={
                "site_name": "example",
                "top_endpoints": [{"url": "https://api.example/pricing"}],
                "trace_template_hints": [
                    {
                        "endpoint_url": "https://api.example/pricing",
                        "method": "POST",
                        "candidate_mutations": [{"option_key": "quantity", "path": "quantity"}],
                    }
                ],
            },
            llm_suggestions={
                "proposed_file_patches": [
                    {
                        "path": "src/price_extractor/site_adapters.py",
                        "operation": "insert",
                        "content": "from .print24 import Print24SiteAdapter\n",
                    }
                ],
            },
        )

        patches = [row for row in list(plan.get("proposedFilePatches") or []) if isinstance(row, dict)]
        self.assertEqual(patches, [])

    def test_extraction_recipe_contains_required_strategies(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "requires_session": True,
            "token_indicators": ["csrf_antiforge"],
            "option_catalog_groups": 2,
            "top_endpoints": [
                {
                    "method": "POST",
                    "url": "https://api.example.test/pricing",
                    "score": 12,
                    "confidence": 0.9,
                }
            ],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
            "suggested_next_steps": ["Use template-driven synthesis."],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)

        self.assertEqual(str(recipe.get("kind") or ""), "extraction_recipe_v1")
        self.assertIn("sessionStrategy", recipe)
        self.assertIn("replayStrategy", recipe)
        self.assertIn("requestSynthesisStrategy", recipe)
        self.assertIn("quantityStrategy", recipe)
        self.assertIn("validationStrategy", recipe)
        self.assertIn("runtimeSafetyPolicy", recipe)

        validation = dict(recipe.get("validationStrategy") or {})
        self.assertTrue(bool(validation.get("requiresIntendedVsAcceptedComparison")))
        self.assertTrue(bool(validation.get("requiresAcceptedConfigProofForValidated")))
        self.assertTrue(bool(validation.get("quantityValidationIsMandatory")))

    def test_recipe_critique_blocks_when_validation_integrity_disabled(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [
                {"method": "POST", "url": "https://api.example.test/pricing", "score": 10, "confidence": 0.8}
            ],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe["validationStrategy"] = {
            "requiresIntendedVsAcceptedComparison": False,
            "requiresAcceptedConfigProofForValidated": False,
            "quantityValidationIsMandatory": False,
            "forbidInterpolationAssumptions": False,
        }

        critique = _build_recipe_critique(recipe)
        self.assertEqual(str(critique.get("overallVerdict") or ""), "block")
        critics = [
            row
            for row in list(critique.get("critics") or [])
            if isinstance(row, dict)
            and str(row.get("criticId") or "") == "recipe_validation_integrity"
        ]
        self.assertEqual(len(critics), 1)
        self.assertEqual(str(critics[0].get("verdict") or ""), "block")

    def test_promotion_decision_needs_more_evidence_when_rewrite_gate_warns(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [],
        }
        patch_plan = _build_review_patch_plan(
            proposal=proposal,
            llm_suggestions={"site_adapter_code": "class ExampleSiteAdapter:\n    pass"},
        )
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)
        deterministic_gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
        )

        decision = _build_promotion_decision(
            patch_plan=patch_plan,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            extraction_recipe=recipe,
            deterministic_gate=deterministic_gate,
        )

        self.assertEqual(str(decision.get("kind") or ""), "promotion_decision_v1")
        self.assertEqual(str(decision.get("decision") or ""), "needs_more_evidence")

    def test_promotion_decision_warns_on_llm_critic_errors_by_default(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)
        deterministic_gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=True,
            runtime_check_timeout_seconds=30.0,
            runtime_commands_override=[
                {
                    "id": "smoke_ok",
                    "command": "python -c \"print('ok')\"",
                    "required": True,
                    "status": "not_run",
                }
            ],
        )

        decision = _build_promotion_decision(
            patch_plan=patch_plan,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            extraction_recipe=recipe,
            deterministic_gate=deterministic_gate,
            llm_critics={
                "recipe": {"errorCount": 1},
                "patch": {"errorCount": 0},
            },
        )

        self.assertEqual(str(decision.get("decision") or ""), "needs_more_evidence")
        checks = [row for row in list(decision.get("checks") or []) if isinstance(row, dict)]
        llm_checks = [row for row in checks if str(row.get("id") or "") == "llm_critic_execution_quality"]
        self.assertEqual(len(llm_checks), 1)
        self.assertEqual(str(llm_checks[0].get("status") or ""), "warn")

    def test_promotion_decision_rejects_on_llm_critic_errors_when_configured_fail(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)
        deterministic_gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=True,
            runtime_check_timeout_seconds=30.0,
            runtime_commands_override=[
                {
                    "id": "smoke_ok",
                    "command": "python -c \"print('ok')\"",
                    "required": True,
                    "status": "not_run",
                }
            ],
        )

        decision = _build_promotion_decision(
            patch_plan=patch_plan,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            extraction_recipe=recipe,
            deterministic_gate=deterministic_gate,
            llm_critics={
                "recipe": {"errorCount": 1},
                "patch": {"errorCount": 1},
            },
            llm_critic_error_severity="fail",
        )

        self.assertEqual(str(decision.get("decision") or ""), "reject")
        checks = [row for row in list(decision.get("checks") or []) if isinstance(row, dict)]
        llm_checks = [row for row in checks if str(row.get("id") or "") == "llm_critic_execution_quality"]
        self.assertEqual(len(llm_checks), 1)
        self.assertEqual(str(llm_checks[0].get("status") or ""), "fail")

    def test_promotion_decision_ignores_llm_critic_errors_when_configured(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)
        deterministic_gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=True,
            runtime_check_timeout_seconds=30.0,
            runtime_commands_override=[
                {
                    "id": "smoke_ok",
                    "command": "python -c \"print('ok')\"",
                    "required": True,
                    "status": "not_run",
                }
            ],
        )

        decision = _build_promotion_decision(
            patch_plan=patch_plan,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            extraction_recipe=recipe,
            deterministic_gate=deterministic_gate,
            llm_critics={
                "recipe": {"errorCount": 2},
                "patch": {"errorCount": 0},
            },
            llm_critic_error_severity="ignore",
        )

        self.assertEqual(str(decision.get("decision") or ""), "promote")
        checks = [row for row in list(decision.get("checks") or []) if isinstance(row, dict)]
        llm_checks = [row for row in checks if str(row.get("id") or "") == "llm_critic_execution_quality"]
        self.assertEqual(len(llm_checks), 1)
        self.assertEqual(str(llm_checks[0].get("status") or ""), "pass")

    def test_deterministic_gate_fails_on_schema_contract_break(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)

        # Break the recipe contract intentionally.
        recipe["validationStrategy"] = {}

        gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
        )

        self.assertEqual(str(gate.get("kind") or ""), "deterministic_gate_v1")
        self.assertEqual(str(gate.get("overallStatus") or ""), "fail")
        checks = [row for row in list(gate.get("checks") or []) if isinstance(row, dict)]
        schema_checks = [row for row in checks if str(row.get("id") or "") == "schema_extraction_recipe"]
        self.assertEqual(len(schema_checks), 1)
        self.assertEqual(str(schema_checks[0].get("status") or ""), "fail")

    def test_deterministic_gate_runtime_execution_passes_when_commands_succeed(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)

        gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=True,
            runtime_check_timeout_seconds=30.0,
            runtime_commands_override=[
                {
                    "id": "smoke_ok",
                    "command": "python -c \"print('ok')\"",
                    "required": True,
                    "status": "not_run",
                }
            ],
        )

        self.assertEqual(str(gate.get("overallStatus") or ""), "pass")
        checks = [row for row in list(gate.get("checks") or []) if isinstance(row, dict)]
        runtime_checks = [row for row in checks if str(row.get("id") or "") == "runtime_checks_execution"]
        self.assertEqual(len(runtime_checks), 1)
        self.assertEqual(str(runtime_checks[0].get("status") or ""), "pass")

    def test_deterministic_gate_runtime_execution_fails_when_commands_fail(self) -> None:
        proposal = {
            "site_name": "example",
            "product_url": "https://example.test/product",
            "top_endpoints": [{"method": "POST", "url": "https://api.example.test/pricing"}],
            "trace_template_hints": [
                {
                    "endpoint_url": "https://api.example.test/pricing",
                    "method": "POST",
                    "candidate_mutations": [{"option_key": "quantity", "path": "payload.qty"}],
                }
            ],
        }
        patch_plan = _build_review_patch_plan(proposal=proposal, llm_suggestions={})
        recipe = _build_extraction_recipe(proposal, {}, patch_plan)
        recipe_critique = _build_recipe_critique(recipe)
        patch_critique = _build_patch_critique(patch_plan, recipe)

        gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=True,
            runtime_check_timeout_seconds=30.0,
            runtime_commands_override=[
                {
                    "id": "smoke_fail",
                    "command": "python -c \"import sys; sys.exit(3)\"",
                    "required": True,
                    "status": "not_run",
                }
            ],
        )

        self.assertEqual(str(gate.get("overallStatus") or ""), "fail")
        checks = [row for row in list(gate.get("checks") or []) if isinstance(row, dict)]
        runtime_checks = [row for row in checks if str(row.get("id") or "") == "runtime_checks_execution"]
        self.assertEqual(len(runtime_checks), 1)
        self.assertEqual(str(runtime_checks[0].get("status") or ""), "fail")

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

    def test_critic_prompt_registry_has_stage_scoped_prompts(self) -> None:
        registry = _build_critic_prompt_registry()

        self.assertEqual(str(registry.get("schemaVersion") or ""), "critic_prompt_registry_v1")
        recipe = [row for row in list(registry.get("recipe") or []) if isinstance(row, dict)]
        patch = [row for row in list(registry.get("patch") or []) if isinstance(row, dict)]

        self.assertGreaterEqual(len(recipe), 4)
        self.assertGreaterEqual(len(patch), 3)

        for row in recipe + patch:
            self.assertTrue(str(row.get("criticId") or "").strip())
            self.assertTrue(str(row.get("domain") or "").strip())
            self.assertTrue(str(row.get("systemPrompt") or "").strip())
            self.assertIn("{{CONTEXT_JSON}}", str(row.get("userPromptTemplate") or ""))

    def test_coerce_llm_critic_result_marks_nonpass_without_findings_as_error(self) -> None:
        result = _coerce_llm_critic_result(
            critic_spec={"criticId": "recipe_extraction_correctness", "domain": "extraction"},
            response_obj={"verdict": "warn", "score": 84, "summary": "", "findings": []},
        )

        self.assertEqual(str(result.get("status") or ""), "error")
        self.assertEqual(str(result.get("errorCategory") or ""), "output_schema_quality")
        self.assertEqual(str(result.get("summary") or ""), "llm_critic_noncompliant_output")

    def test_run_llm_critic_pass_retries_once_on_schema_quality_failure(self) -> None:
        critic_specs = [
            {
                "criticId": "recipe_extraction_correctness",
                "domain": "extraction",
                "systemPrompt": "critic system",
                "userPromptTemplate": "Context:\n{{CONTEXT_JSON}}",
            }
        ]
        llm_settings = {
            "base_url": "https://example.invalid/v1",
            "api_key": "secret",
            "model": "dummy",
            "timeout_seconds": 10.0,
        }

        with mock.patch(
            "price_extractor.agentic.langgraph_onboarding._call_openai_compatible_json",
            side_effect=[
                {"verdict": "warn", "score": 70, "summary": "", "findings": []},
                {
                    "verdict": "pass",
                    "score": 96,
                    "summary": "Evidence and extraction alignment are sufficient.",
                    "findings": [],
                },
            ],
        ) as mocked_call:
            results = _run_llm_critic_pass(
                stage_name="recipe",
                critic_specs=critic_specs,
                context_payload={"foo": "bar"},
                llm_settings=llm_settings,
                max_critics=1,
                inter_request_delay_seconds=0.0,
                retry_attempts=0,
                retry_wait_seconds=0.0,
                retry_backoff_multiplier=1.0,
                context_mode="compact",
                context_max_chars=1500,
                verbose_logs=False,
            )

        self.assertEqual(mocked_call.call_count, 2)
        self.assertEqual(len(results), 1)
        self.assertEqual(str(results[0].get("status") or ""), "ok")
        self.assertEqual(str(results[0].get("verdict") or ""), "pass")

    def test_run_llm_critic_pass_stops_remaining_calls_on_quota_exhaustion(self) -> None:
        critic_specs = [
            {
                "criticId": "recipe_evidence_sufficiency",
                "domain": "evidence",
                "systemPrompt": "critic system",
                "userPromptTemplate": "Context:\n{{CONTEXT_JSON}}",
            },
            {
                "criticId": "recipe_validation_integrity",
                "domain": "validation",
                "systemPrompt": "critic system",
                "userPromptTemplate": "Context:\n{{CONTEXT_JSON}}",
            },
        ]
        llm_settings = {
            "base_url": "https://example.invalid/v1",
            "api_key": "secret",
            "model": "dummy",
            "timeout_seconds": 10.0,
        }

        quota_error = RuntimeError(
            "HTTP 429 Too Many Requests: RESOURCE_EXHAUSTED Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests"
        )

        with mock.patch(
            "price_extractor.agentic.langgraph_onboarding._call_openai_compatible_json",
            side_effect=[quota_error],
        ) as mocked_call:
            results = _run_llm_critic_pass(
                stage_name="recipe",
                critic_specs=critic_specs,
                context_payload={"foo": "bar"},
                llm_settings=llm_settings,
                max_critics=2,
                inter_request_delay_seconds=0.0,
                retry_attempts=2,
                retry_wait_seconds=0.0,
                retry_backoff_multiplier=1.0,
                context_mode="compact",
                context_max_chars=1500,
                verbose_logs=False,
            )

        self.assertEqual(mocked_call.call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(str(results[0].get("status") or ""), "error")
        self.assertEqual(str(results[1].get("status") or ""), "error")
        self.assertIn("quota_exhausted", str(results[1].get("findings") or "").lower())

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
            self.assertIn("extractionRecipe", payload)
            self.assertIn("recipeCritique", payload)
            self.assertIn("patchCritique", payload)
            self.assertIn("llmCritics", payload)
            self.assertIn("criticPromptRegistry", payload)
            self.assertIn("deterministicGate", payload)
            self.assertIn("promotionDecision", payload)
            self.assertEqual(payload["proposal"].get("site_name"), "example")
            self.assertFalse(bool((payload.get("llmCritics") or {}).get("enabled")))
