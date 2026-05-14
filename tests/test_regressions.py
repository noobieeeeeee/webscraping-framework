from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib import error

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor import cli
from price_extractor.agents import ExecutionAgent, ValidationAgent
from price_extractor.browser_bootstrap import BootstrapResult
from price_extractor.knowledge_store import KnowledgeStore
from price_extractor.models import ExtractionResult, ObservationBundle, RunState, Strategy, StrategyPlan, TargetInput
from price_extractor.site_adapters import build_site_replay_candidates
from price_extractor.orchestrator import ExtractionOrchestrator
from price_extractor.models import Complexity, FeasibilityResult, ValidationResult


FIXTURES = ROOT / "tests" / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_args(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "url": "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
        "manifest_file": None,
        "manifest_csv": None,
        "site_name": None,
        "product_type": "unknown",
        "option": [],
        "options_json": None,
        "options_file": None,
        "expected_price": None,
        "expected_price_tolerance": 0.01,
        "expected_currency": "EUR",
        "headed": False,
        "headed_debug_hold_seconds": 0.0,
        "disable_auto_accept_cookies": False,
        "verbose": False,
        "allow_heuristic_fallback": False,
        "require_matched_options": False,
        "request_only": False,
        "recon_only": False,
        "recon_ttl_days": 7,
        "force_recon_refresh": False,
        "bootstrap_timeout_ms": 1000,
        "max_observed_requests": 32,
        "http_request_timeout_seconds": 15.0,
        "http_min_delay_ms": 0,
        "http_jitter_ms": 0,
        "http_max_retries": 1,
        "http_backoff_base_ms": 400,
        "http_backoff_max_ms": 5000,
        "proxy_url": None,
        "proxy_file": None,
        "proxy_rotation": "none",
        "proxy_failure_threshold": 3,
        "proxy_cooldown_seconds": 300,
        "max_dependency_probe_steps": 0,
        "knowledge_db": str(ROOT / ".data" / "test-knowledge.db"),
        "max_attempts": 1,
        "checkpoint_file": str(ROOT / ".data" / "checkpoints" / "test-job-state.json"),
        "retry_failed": False,
        "output_file": None,
        "results_jsonl": None,
        "results_jsonl_mode": "compact",
        "artifacts_dir": None,
        "fail_on_invalid": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeHttpResponse:
    def __init__(self, payload: str, content_type: str = "application/json", status: int = 200) -> None:
        self._payload = payload.encode("utf-8")
        self.headers = {"content-type": content_type}
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class RegressionTests(unittest.TestCase):
    def test_best_price_candidate_ignores_zero_values(self) -> None:
        candidates = [
            {"value": 0.0, "score": 999, "source": "named:total_gross_value"},
            {"value": 12.34, "score": 5, "source": "json:data.response.price"},
        ]

        best = ExecutionAgent._select_best_price_candidate(candidates, expected_price=None)
        self.assertIsNotNone(best)
        self.assertEqual(best.get("value"), 12.34)

    def test_saxoprint_adapter_overrides_quantity_and_print_runs(self) -> None:
        traces = load_fixture("saxoprint_template_traces.json")

        state = RunState(
            target=TargetInput(
                site_name="saxoprint.de",
                product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
                product_type="brochure",
                options={"quantity": 600},
                network_traces=traces,
                bootstrap_signals={},
            )
        )

        candidates = build_site_replay_candidates(
            state,
            effective_options={"quantity": 600},
            raw_requested_options={"quantity": 600},
        )
        self.assertTrue(candidates)
        sax = candidates[0]
        self.assertIn("saxoprint", str(sax.get("source")))

        body = json.loads(str(sax.get("body_text")))
        self.assertEqual(body.get("productGroupId"), 305)
        prop = {int(row["propertyId"]): int(row["value"]) for row in body.get("propertyConfiguration")}
        self.assertEqual(prop.get(44), 600)
        self.assertEqual(body.get("printRuns"), [600, 750, 800, 900])

    def test_saxoprint_adapter_disambiguates_brochure_cover_vs_content_options(self) -> None:
        traces = load_fixture("saxoprint_template_traces.json")

        dropdown_maps = [
            {
                "source": "fixture",
                "propertyId": 8,
                "triggerText": "Material",
                "contextHint": "content",
                "options": [
                    "250 g/m² Naturpapier FSC®",
                    "170 g/m² Bilderdruckpapier matt",
                ],
                "valueIds": [1616, 269],
            },
            {
                "source": "fixture",
                "propertyId": 36,
                "triggerText": "Material",
                "contextHint": "cover",
                "options": [
                    "130 g/m² Bilderdruckpapier",
                    "170 g/m² Bilderdruckpapier matt",
                ],
                "valueIds": [480, 481],
            },
            {
                "source": "fixture",
                "propertyId": 38,
                "triggerText": "Seitenanzahl",
                "contextHint": "cover",
                "options": ["4 Seiten"],
                "valueIds": [265],
            },
        ]

        state = RunState(
            target=TargetInput(
                site_name="saxoprint.de",
                product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
                product_type="brochure",
                options={"quantity": 600},
                network_traces=traces,
                bootstrap_signals={"request_templates": {"dropdownMaps": dropdown_maps}},
            )
        )

        candidates = build_site_replay_candidates(
            state,
            effective_options={"quantity": 600},
            raw_requested_options={
                "quantity": 600,
                "material": "170gsm",
                "umschlag_material": "130gsm",
                "umschlag_seitenanzahl": 4,
            },
        )
        self.assertTrue(candidates)
        body = json.loads(str(candidates[0].get("body_text")))
        prop = {int(row["propertyId"]): int(row["value"]) for row in body.get("propertyConfiguration")}
        self.assertEqual(prop.get(8), 269)
        self.assertEqual(prop.get(36), 480)
        self.assertEqual(prop.get(38), 265)

        applied = dict(candidates[0].get("request_template_applied") or {})
        self.assertEqual(applied.get("kind"), "saxoprint_payload")
        self.assertEqual(list(applied.get("synthesisIssues") or []), [])

    def test_saxoprint_adapter_reports_ambiguous_shorthand_in_synthesis_issues(self) -> None:
        traces = load_fixture("saxoprint_template_traces.json")

        dropdown_maps = [
            {
                "source": "fixture",
                "propertyId": 8,
                "triggerText": "Material",
                "contextHint": "content",
                "options": [
                    "170 g/m² Bilderdruckpapier matt",
                    "170 g/m² Bilderdruckpapier glänzend",
                ],
                "valueIds": [269, 270],
            }
        ]

        state = RunState(
            target=TargetInput(
                site_name="saxoprint.de",
                product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
                product_type="brochure",
                options={"quantity": 600},
                network_traces=traces,
                bootstrap_signals={"request_templates": {"dropdownMaps": dropdown_maps}},
            )
        )

        candidates = build_site_replay_candidates(
            state,
            effective_options={"quantity": 600},
            raw_requested_options={
                "quantity": 600,
                "material": "170gsm",
            },
        )
        self.assertTrue(candidates)
        applied = dict(candidates[0].get("request_template_applied") or {})
        issues = list(applied.get("synthesisIssues") or [])
        self.assertTrue(issues)
        self.assertEqual(str(issues[0].get("key")), "material")
        self.assertIn(str(issues[0].get("reason")), {"ambiguous_contains", "ambiguous_exact", "ambiguous_numeric"})

        body = json.loads(str(candidates[0].get("body_text")))
        prop = {int(row["propertyId"]): int(row["value"]) for row in body.get("propertyConfiguration")}
        self.assertIsNone(prop.get(8))

    def test_request_only_fails_when_saxoprint_synthesis_issues_present(self) -> None:
        target = TargetInput(
            site_name="saxoprint.de",
            product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
            product_type="brochure",
            expected_currency="EUR",
            options={"quantity": 600, "material": "170gsm"},
            bootstrap_signals={"request_only": True, "learned_normalization_rules": {}},
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://api.saxoprint.de/product-configuration/get-product-prices",
            payload_template={},
            confidence=0.8,
            notes="",
        )
        state.extraction = ExtractionResult(
            success=True,
            accepted_configuration=dict(target.options),
            price_value=12.34,
            currency="EUR",
            raw_response_summary={
                "replay": "http",
                "fallback": "none",
                "requestTemplateApplied": {
                    "kind": "saxoprint_payload",
                    "synthesisIssues": [{"key": "material", "reason": "ambiguous_contains"}],
                },
            },
        )

        result = ValidationAgent().run(state)
        self.assertFalse(result.is_valid)
        self.assertTrue(
            any(str(row).startswith("request_only_option_synthesis_failed") for row in result.mismatches)
        )
        self.assertEqual(result.inferred_failure_reason, "request_only_option_synthesis_failed")

    def test_saxoprint_adapter_honors_explicit_property_overrides(self) -> None:
        traces = load_fixture("saxoprint_template_traces.json")

        state = RunState(
            target=TargetInput(
                site_name="saxoprint.de",
                product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
                product_type="brochure",
                options={"quantity": 600},
                network_traces=traces,
                bootstrap_signals={},
            )
        )

        candidates = build_site_replay_candidates(
            state,
            effective_options={"quantity": 600},
            raw_requested_options={"quantity": 600, "property_9": 51},
        )
        self.assertTrue(candidates)
        body = json.loads(str(candidates[0].get("body_text")))
        prop = {int(row["propertyId"]): int(row["value"]) for row in body.get("propertyConfiguration")}
        self.assertEqual(prop.get(9), 51)

    def test_saxoprint_label_matching_accepts_fscr_shorthand(self) -> None:
        from price_extractor.site_adapters import SaxoprintSiteAdapter

        value_ids = [1616, 269]
        labels = [
            "250 g/m² Naturpapier FSC®",
            "170 g/m² Bilderdruckpapier matt",
        ]
        mapping = SaxoprintSiteAdapter._build_label_to_value_id(value_ids, labels)
        resolved = SaxoprintSiteAdapter._lookup_value_id(mapping, "250 g/m² Naturpapier FSCr")
        self.assertEqual(resolved, 1616)

    def test_build_results_jsonl_row_compact(self) -> None:
        row = cli._build_results_jsonl_row(
            unit_id="unit_123",
            index=4,
            spec={"url": "https://example.test/p/1"},
            summary={
                "site": "example.test",
                "validated": True,
                "price": 12.34,
                "currency": "EUR",
                "replay_mode": "http",
                "fallback_mode": "none",
                "extraction_reason": None,
                "mismatches": [],
                "artifact_dir": ".data/runs/example/unit_123",
            },
            status="completed",
            mode="compact",
        )
        self.assertEqual(row["unit_id"], "unit_123")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["replay_mode"], "http")
        self.assertNotIn("summary", row)

    def test_build_results_jsonl_row_full(self) -> None:
        summary = {"site": "example.test", "validated": False, "price": None}
        row = cli._build_results_jsonl_row(
            unit_id="unit_123",
            index=4,
            spec={"url": "https://example.test/p/1"},
            summary=summary,
            status="failed",
            mode="full",
        )
        self.assertEqual(row["status"], "failed")
        self.assertIn("summary", row)
        self.assertEqual(row["summary"], summary)

    def test_parse_manifest_csv_supports_option_columns_and_options_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "targets.csv"
            csv_path.write_text(
                "url,site_name,product_type,options_json,option.quantity,option.format\n"
                'https://example.test/p/1,example.test,brochure,"{""material"": ""130gsm"", ""sided"": 2}",250,A5\n',
                encoding="utf-8",
            )

            rows = cli.parse_manifest_csv(str(csv_path))

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://example.test/p/1")
        self.assertEqual(row["site_name"], "example.test")
        self.assertEqual(row["product_type"], "brochure")
        self.assertEqual(row["options"].get("quantity"), 250)
        self.assertEqual(row["options"].get("format"), "A5")
        self.assertEqual(row["options"].get("material"), "130gsm")
        self.assertEqual(row["options"].get("sided"), 2)

    def test_parse_manifest_csv_semicolon_row_config_with_default_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "matrix.csv"
            csv_path.write_text(
                "UID;Innenteil;Papier;Zusätzlicher_Umschlag;Type;Format;Auflage;Net price;Ausrichtung\n"
                "1;4-seitig;90 g/m² Bilderdruckpapier;Umschlag 130 g/m² Bilderdruck;broschueren-klammerheftung;DIN A3;250;;Hochformat\n",
                encoding="utf-8",
            )

            rows = cli.parse_manifest_csv(
                str(csv_path),
                default_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
                default_site_name="onlineprinters",
            )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["url"], "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4")
        self.assertEqual(row["site_name"], "onlineprinters")
        self.assertEqual(row["product_type"], "broschueren-klammerheftung")
        self.assertEqual(row["options"].get("Innenteil"), "4-seitig")
        self.assertEqual(row["options"].get("Papier"), "90 g/m² Bilderdruckpapier")
        self.assertEqual(row["options"].get("Auflage"), 250)

    def test_load_proxy_pool_from_file_and_direct_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            proxy_file = Path(tmp_dir) / "proxies.txt"
            proxy_file.write_text(
                "# comment\n"
                "http://user:pass@proxy-1.example:8080\n"
                "\n"
                "http://user:pass@proxy-1.example:8080\n"
                "http://proxy-2.example:8080\n",
                encoding="utf-8",
            )

            pool = cli.load_proxy_pool(str(proxy_file), "http://proxy-3.example:8080")

        self.assertEqual(
            pool,
            [
                "http://user:pass@proxy-1.example:8080",
                "http://proxy-2.example:8080",
                "http://proxy-3.example:8080",
            ],
        )

    def test_build_target_input_reuses_cached_recon_for_pricing_runs(self) -> None:
        bootstrap_artifacts = load_fixture("onlineprinters_enriched_bootstrap.json")
        bootstrap_artifacts["network_traces"] = load_fixture("onlineprinters_template_trace.json")

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = KnowledgeStore(str(Path(tmp_dir) / "knowledge.db"))
            try:
                store.save_recon_snapshot(
                    site_name="onlineprinters.de",
                    product_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
                    bootstrap_artifacts=bootstrap_artifacts,
                    ttl_days=7,
                )
                args = make_args(option=["quantity=250", "seitig=16"])

                with mock.patch("price_extractor.cli.bootstrap_product_url") as bootstrap_mock:
                    target, artifacts = cli.build_target_input(
                        {"url": args.url, "site_name": None, "product_type": "unknown", "expected_currency": "EUR", "options": {}},
                        args,
                        store,
                    )

                self.assertTrue(artifacts["recon_cache"]["hit"])
                self.assertTrue(target.bootstrap_signals["option_prevalidation"]["available"])
                bootstrap_mock.assert_not_called()
            finally:
                store.close()

    def test_build_target_input_force_refresh_bypasses_cache(self) -> None:
        bootstrap_artifacts = load_fixture("onlineprinters_enriched_bootstrap.json")
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = KnowledgeStore(str(Path(tmp_dir) / "knowledge.db"))
            try:
                store.save_recon_snapshot(
                    site_name="onlineprinters.de",
                    product_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
                    bootstrap_artifacts=bootstrap_artifacts,
                    ttl_days=7,
                )
                args = make_args(option=["quantity=250"])
                fake_bootstrap = BootstrapResult(
                    site_name="onlineprinters.de",
                    observed_requests=["https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4"],
                    network_traces=load_fixture("onlineprinters_template_trace.json"),
                    option_catalog=bootstrap_artifacts["option_catalog"],
                    request_templates=bootstrap_artifacts["request_templates"],
                    quantity_signal=bootstrap_artifacts["quantity_signal"],
                )

                with mock.patch("price_extractor.cli.bootstrap_product_url", return_value=fake_bootstrap) as bootstrap_mock:
                    _target, artifacts = cli.build_target_input(
                        {"url": args.url, "site_name": None, "product_type": "unknown", "expected_currency": "EUR", "options": {}},
                        args,
                        store,
                        force_recon_refresh=True,
                    )

                self.assertTrue(artifacts["recon_cache"]["refreshed"])
                bootstrap_mock.assert_called_once()
            finally:
                store.close()

    def test_run_single_target_refreshes_cached_recon_once_on_drift(self) -> None:
        args = make_args()
        spec = {"url": args.url}
        target_cached = TargetInput(
            site_name="onlineprinters.de",
            product_url=args.url,
            product_type="unknown",
            bootstrap_signals={
                "recon_cache": {"hit": True},
                "option_prevalidation": {"available": True, "valid": True, "matched": [], "unmatched": []},
                "drift_report": {},
            },
        )
        target_refreshed = TargetInput(
            site_name="onlineprinters.de",
            product_url=args.url,
            product_type="unknown",
            bootstrap_signals={
                "recon_cache": {"hit": False, "refreshed": True},
                "option_prevalidation": {"available": True, "valid": True, "matched": [], "unmatched": []},
                "drift_report": {},
            },
        )
        bootstrap_cached = {
            "recon_cache": {"hit": True},
            "option_catalog": [{"groupLabel": "Seitigkeit", "options": [{"visibleLabel": "8-seitig"}]}],
            "request_templates": {"currentSetLink": {"value": "https://example.com"}},
            "quantity_signal": {},
            "option_groups": [],
            "option_dependencies": [],
            "dependency_probe": {},
        }
        bootstrap_refreshed = {
            "recon_cache": {"hit": False, "refreshed": True},
            "option_catalog": [{"groupLabel": "Seitigkeit", "options": [{"visibleLabel": "16-seitig"}]}],
            "request_templates": {"currentSetLink": {"value": "https://example.com/new"}},
            "quantity_signal": {},
            "option_groups": [],
            "option_dependencies": [],
            "dependency_probe": {},
        }

        fake_orchestrator = SimpleNamespace(store=object(), run=lambda state: state)
        failing_summary = {
            "site": "onlineprinters.de",
            "url": args.url,
            "validated": False,
            "feasible": True,
            "strategy": "hybrid",
            "price": None,
            "currency": "EUR",
            "mismatches": ["price_not_extracted"],
            "failures": [],
            "http_replay": {"attempts": [{"reason": "no_matching_trace"}]},
            "replay_mode": "http",
            "fallback_mode": "none",
            "extraction_reason": "price_not_extracted",
        }
        success_summary = {
            "site": "onlineprinters.de",
            "url": args.url,
            "validated": True,
            "feasible": True,
            "strategy": "hybrid",
            "price": 123.45,
            "currency": "EUR",
            "mismatches": [],
            "failures": [],
            "http_replay": {"attempts": [{"result": "success"}]},
            "replay_mode": "http",
            "fallback_mode": "none",
            "extraction_reason": None,
        }

        with mock.patch("price_extractor.cli.build_target_input", side_effect=[(target_cached, bootstrap_cached), (target_refreshed, bootstrap_refreshed)]) as build_mock:
            with mock.patch("price_extractor.cli.summarize_run", side_effect=[failing_summary, success_summary]):
                summary, _final_state, _bootstrap = cli.run_single_target(fake_orchestrator, args, spec)

        self.assertEqual(build_mock.call_count, 2)
        self.assertEqual(summary["recon_refresh_reason"], "no_matching_trace")
        self.assertTrue(summary["drift_report"]["compared"])
        self.assertEqual(summary["drift_verdict"], "material_change")

    def _recon_cache_hit_target(self) -> TargetInput:
        return TargetInput(
            site_name="saxoprint.de",
            product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
            product_type="unknown",
            bootstrap_signals={
                "recon_cache": {"hit": True},
                "option_prevalidation": {"available": True, "valid": True, "matched": [], "unmatched": []},
                "drift_report": {},
            },
        )

    def _ok_summary(self) -> dict[str, Any]:
        return {
            "site": "saxoprint.de",
            "url": "https://www.saxoprint.de/broschueren/broschueren-drucken",
            "validated": True,
            "feasible": True,
            "strategy": "hybrid",
            "price": 123.45,
            "currency": "EUR",
            "mismatches": [],
            "failures": [],
            "http_replay": {"attempts": [{"result": "success"}]},
            "replay_mode": "http",
            "fallback_mode": "none",
            "extraction_reason": None,
        }

    def test_empty_request_templates_does_not_trigger_refresh_in_request_only(self) -> None:
        """A cached bootstrap with `request_templates: {}` (key present, empty)
        means the bootstrap ran and the site genuinely has no SetLink-style
        templates (Saxoprint, Print24). Refreshing won't change anything —
        it just causes per-run re-bootstrap churn and risks producing
        thinner traces. Only an *absent* key should trigger refresh."""
        args = make_args(url="https://www.saxoprint.de/broschueren/broschueren-drucken", request_only=True)
        spec = {"url": args.url}

        bootstrap_cached = {
            "recon_cache": {"hit": True},
            "option_catalog": [],          # present, empty — Saxoprint's DOM walker can't see Next.js widgets
            "request_templates": {},       # present, empty — Saxoprint has no SetLink-style templates
            "quantity_signal": {},
            "option_groups": [],
            "option_dependencies": [],
            "dependency_probe": {},
            "network_traces": [],
        }

        fake_orchestrator = SimpleNamespace(store=object(), run=lambda state: state)

        with mock.patch(
            "price_extractor.cli.build_target_input",
            side_effect=[(self._recon_cache_hit_target(), bootstrap_cached)],
        ) as build_mock:
            with mock.patch("price_extractor.cli.summarize_run", return_value=self._ok_summary()):
                summary, _final_state, _bootstrap = cli.run_single_target(fake_orchestrator, args, spec)

        # No refresh — build_target_input called exactly once (the initial cache hit).
        self.assertEqual(build_mock.call_count, 1)
        self.assertNotIn("recon_refresh_reason", summary)

    def test_absent_request_templates_key_triggers_missing_enriched_recon(self) -> None:
        """A cached bootstrap that pre-dates the request_templates feature
        (the key is absent entirely, not just empty) should still trigger
        a one-time refresh so the new structured signals get populated."""
        args = make_args(url="https://www.saxoprint.de/broschueren/broschueren-drucken", request_only=True)
        spec = {"url": args.url}

        bootstrap_cached_legacy = {
            "recon_cache": {"hit": True},
            "option_catalog": [],
            # request_templates intentionally absent — legacy snapshot.
            "quantity_signal": {},
            "option_groups": [],
            "option_dependencies": [],
            "dependency_probe": {},
            "network_traces": [],
        }
        bootstrap_refreshed = {
            "recon_cache": {"hit": False, "refreshed": True},
            "option_catalog": [],
            "request_templates": {},  # bootstrap ran, found nothing — fine
            "quantity_signal": {},
            "option_groups": [],
            "option_dependencies": [],
            "dependency_probe": {},
            "network_traces": [],
        }

        fake_orchestrator = SimpleNamespace(store=object(), run=lambda state: state)

        with mock.patch(
            "price_extractor.cli.build_target_input",
            side_effect=[
                (self._recon_cache_hit_target(), bootstrap_cached_legacy),
                (self._recon_cache_hit_target(), bootstrap_refreshed),
            ],
        ) as build_mock:
            with mock.patch("price_extractor.cli.summarize_run", return_value=self._ok_summary()):
                summary, _final_state, _bootstrap = cli.run_single_target(fake_orchestrator, args, spec)

        self.assertEqual(build_mock.call_count, 2)
        self.assertEqual(summary.get("recon_refresh_reason"), "missing_enriched_recon")

    def test_empty_option_catalog_in_request_only_does_not_trigger_refresh(self) -> None:
        """Pre-existing special case (preserved): in request-only mode, an
        empty option_catalog is expected (no UI probing) and must not
        trigger a refresh. This locks in the asymmetry that was already
        in place before the request_templates fix."""
        args = make_args(url="https://www.saxoprint.de/broschueren/broschueren-drucken", request_only=True)
        spec = {"url": args.url}

        bootstrap_cached = {
            "recon_cache": {"hit": True},
            "option_catalog": [],          # empty but key present
            "request_templates": {"currentSetLink": {"value": "x"}},  # non-empty
            "quantity_signal": {},
            "option_groups": [],
            "option_dependencies": [],
            "dependency_probe": {},
            "network_traces": [],
        }

        fake_orchestrator = SimpleNamespace(store=object(), run=lambda state: state)

        with mock.patch(
            "price_extractor.cli.build_target_input",
            side_effect=[(self._recon_cache_hit_target(), bootstrap_cached)],
        ) as build_mock:
            with mock.patch("price_extractor.cli.summarize_run", return_value=self._ok_summary()):
                _summary, _final_state, _bootstrap = cli.run_single_target(fake_orchestrator, args, spec)

        self.assertEqual(build_mock.call_count, 1)

    def test_print24_replay_injects_portal_header(self) -> None:
        trace = {
            "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
            "method": "POST",
            "resource_type": "xhr",
            "status": 200,
            "request_headers": {
                "accept": "application/json",
                "content-type": "application/json",
            },
            "response_content_type": "application/json",
            "post_data": "{}",
        }
        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            network_traces=[trace],
            bootstrap_signals={
                "cookies": {"portalName": "print24"},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=trace["url"], payload_template={}, confidence=0.8, notes="")
        agent = ExecutionAgent()
        seen_headers: dict[str, str] = {}

        def fake_urlopen(req, timeout=15):
            nonlocal seen_headers
            seen_headers = {key.lower(): value for key, value in req.header_items()}
            return FakeHttpResponse(
                json.dumps({"prices": {"final_prices": {"total_gross_value": 70.47, "total_net_value": 59.22}}}),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        self.assertEqual(seen_headers.get("portal"), "print24")
        self.assertEqual(replay_summary["selectedVia"], "plan_endpoint")

    def test_replay_substitutes_trace_url_for_path_prefix_match(self) -> None:
        # Planned endpoint is a template (no product id); trace has the instantiated URL.
        # Fuzzy path_prefix match should find the trace, and the HTTP request should be
        # sent to the trace URL (current-session, valid path) rather than the candidate URL.
        trace_url = "https://api.example.com/api/productDetails/12345"
        trace = {
            "url": trace_url,
            "method": "POST",
            "resource_type": "xhr",
            "status": 200,
            "request_headers": {"accept": "application/json", "content-type": "application/json"},
            "response_content_type": "application/json",
            "post_data": "{}",
        }
        target = TargetInput(
            site_name="example.com",
            product_url="https://example.com/some-product",
            product_type="brochure",
            network_traces=[trace],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://api.example.com/api/productDetails/",
            payload_template={},
            confidence=0.8,
            notes="",
        )
        agent = ExecutionAgent()
        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=15):
            seen_urls.append(req.full_url)
            return FakeHttpResponse(
                json.dumps({"total_gross_value": 42.42}),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        self.assertTrue(seen_urls, "urlopen should have been called at least once")
        self.assertEqual(seen_urls[0], trace_url)

        attempts = list(replay_summary.get("attempts") or [])
        success_attempts = [a for a in attempts if a.get("result") == "success"]
        self.assertTrue(success_attempts, "expected at least one successful attempt")
        success = success_attempts[0]
        self.assertEqual(success.get("endpoint"), trace_url)
        self.assertTrue(success.get("endpointSubstituted"))
        self.assertEqual(success.get("substitutionReason"), "path_prefix")
        self.assertEqual(
            success.get("originalCandidateEndpoint"),
            "https://api.example.com/api/productDetails/",
        )

    def test_build_http_replay_candidates_rescues_zero_score_pricing_family(self) -> None:
        # A true pricing endpoint can score <=0 when ranking is noisy. The family
        # classifier is independent, so pricing/quantity/schema families should be
        # rescued; infrastructure/unknown should not be.
        target = TargetInput(
            site_name="example.com",
            product_url="https://example.com/product",
            product_type="brochure",
            network_traces=[],
            bootstrap_signals={"cookies": {}, "request_only": True},
        )
        observation = ObservationBundle(
            has_script_heavy_ui=False,
            has_api_calls=True,
            requires_session=False,
            endpoint_rankings=[
                {
                    "url": "https://api.example.com/api/productDetails/abc",
                    "score": 0,
                    "request_family": "pricing_pipeline",
                },
                {
                    "url": "https://api.example.com/api/repo/options",
                    "score": -2,
                    "request_family": "schema_pipeline",
                },
                {
                    "url": "https://api.example.com/api/translations",
                    "score": 0,
                    "request_family": "infrastructure",
                },
                {
                    "url": "https://cdn.example.com/static/junk.js",
                    "score": -5,
                    "request_family": "unknown",
                },
            ],
        )
        state = RunState(target=target, observation=observation)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint=None,
            payload_template={},
            confidence=0.6,
            notes="",
        )

        candidates = ExecutionAgent._build_http_replay_candidates(state)
        candidate_urls = [c["url"] for c in candidates]
        candidate_sources = {c["url"]: c["source"] for c in candidates}

        self.assertIn("https://api.example.com/api/productDetails/abc", candidate_urls)
        self.assertIn("https://api.example.com/api/repo/options", candidate_urls)
        self.assertNotIn("https://api.example.com/api/translations", candidate_urls)
        self.assertNotIn("https://cdn.example.com/static/junk.js", candidate_urls)

        self.assertTrue(
            candidate_sources["https://api.example.com/api/productDetails/abc"].startswith("family_rescued_pricing_pipeline_"),
            f"expected family_rescued source, got {candidate_sources['https://api.example.com/api/productDetails/abc']}",
        )
        self.assertTrue(
            candidate_sources["https://api.example.com/api/repo/options"].startswith("family_rescued_schema_pipeline_"),
        )

    def test_build_http_replay_candidates_preserves_positive_score_priority(self) -> None:
        # Positive-score endpoints should still be added first (preserving existing behavior).
        target = TargetInput(
            site_name="example.com",
            product_url="https://example.com/product",
            product_type="brochure",
            network_traces=[],
            bootstrap_signals={"cookies": {}, "request_only": True},
        )
        observation = ObservationBundle(
            has_script_heavy_ui=False,
            has_api_calls=True,
            requires_session=False,
            endpoint_rankings=[
                {
                    "url": "https://api.example.com/api/price",
                    "score": 10,
                    "request_family": "pricing_pipeline",
                },
                {
                    "url": "https://api.example.com/api/productDetails/xyz",
                    "score": 0,
                    "request_family": "pricing_pipeline",
                },
            ],
        )
        state = RunState(target=target, observation=observation)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint=None,
            payload_template={},
            confidence=0.6,
            notes="",
        )

        candidates = ExecutionAgent._build_http_replay_candidates(state)
        ranked_idx = next(i for i, c in enumerate(candidates) if c["url"] == "https://api.example.com/api/price")
        rescued_idx = next(i for i, c in enumerate(candidates) if c["url"] == "https://api.example.com/api/productDetails/xyz")
        self.assertLess(ranked_idx, rescued_idx)
        self.assertEqual(candidates[ranked_idx]["source"], "ranked_endpoint_0")
        self.assertTrue(candidates[rescued_idx]["source"].startswith("family_rescued_"))

    def test_replay_does_not_substitute_on_exact_match(self) -> None:
        # When the candidate URL matches the trace URL exactly (normalized_exact),
        # no substitution should happen and the request should go to the candidate URL.
        trace_url = "https://api.example.com/api/productDetails/12345"
        trace = {
            "url": trace_url,
            "method": "POST",
            "resource_type": "xhr",
            "status": 200,
            "request_headers": {"accept": "application/json", "content-type": "application/json"},
            "response_content_type": "application/json",
            "post_data": "{}",
        }
        target = TargetInput(
            site_name="example.com",
            product_url="https://example.com/some-product",
            product_type="brochure",
            network_traces=[trace],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint=trace_url,
            payload_template={},
            confidence=0.8,
            notes="",
        )
        agent = ExecutionAgent()
        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=15):
            seen_urls.append(req.full_url)
            return FakeHttpResponse(
                json.dumps({"total_gross_value": 42.42}),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        self.assertEqual(seen_urls[0], trace_url)
        attempts = list(replay_summary.get("attempts") or [])
        success_attempts = [a for a in attempts if a.get("result") == "success"]
        self.assertTrue(success_attempts)
        success = success_attempts[0]
        self.assertFalse(success.get("endpointSubstituted", False))
        self.assertNotIn("originalCandidateEndpoint", success)

    def test_json_adapter_executes_saxoprint_end_to_end(self) -> None:
        target = TargetInput(
            site_name="saxoprint.de",
            product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
            product_type="brochure",
            expected_currency="EUR",
            options={"quantity": 600},
            network_traces=[],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://api.saxoprint.de/product-configuration/get-product-prices",
            payload_template={},
            confidence=0.8,
            notes="",
        )

        agent = ExecutionAgent()
        seen_body: dict[str, Any] = {}

        def fake_urlopen(req, timeout=15):
            nonlocal seen_body
            raw_body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
            seen_body = json.loads(raw_body) if raw_body else {}
            return FakeHttpResponse(
                json.dumps({"priceGross": 99.99}),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.price_value, 99.99)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(replay_summary.get("selectedVia"), "json_adapter:saxoprint_brochure_v1")

        summary = dict(result.raw_response_summary or {})
        self.assertEqual(summary.get("replay"), "adapter")

        adapter_result = dict(summary.get("adapterResult") or {})
        self.assertEqual(adapter_result.get("price"), 99.99)
        self.assertEqual(adapter_result.get("currency"), "EUR")
        self.assertEqual(adapter_result.get("source"), "adapter")

        adapter_diag = dict(summary.get("adapterDiagnostics") or {})
        self.assertEqual(adapter_diag.get("reason"), "matched")
        self.assertEqual(adapter_diag.get("matchedAdapterId"), "saxoprint_brochure_v1")
        self.assertGreaterEqual(int(adapter_diag.get("loadedCount") or 0), 1)

        property_rows = list(seen_body.get("propertyConfiguration") or [])
        self.assertTrue(property_rows)
        self.assertEqual(int(property_rows[0].get("value") or 0), 600)

    def test_request_only_validation_accepts_adapter_replay(self) -> None:
        target = TargetInput(
            site_name="saxoprint.de",
            product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
            product_type="brochure",
            expected_currency="EUR",
            options={"quantity": 600},
            network_traces=[],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://api.saxoprint.de/product-configuration/get-product-prices",
            payload_template={},
            confidence=0.8,
            notes="",
        )

        execution = ExecutionAgent()

        def fake_urlopen(req, timeout=15):
            return FakeHttpResponse(
                json.dumps({"priceGross": 99.99}),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            state.extraction = execution.run(state)

        validation = ValidationAgent().run(state)
        self.assertTrue(validation.is_valid)
        self.assertFalse(any(str(row).startswith("request_only_replay_mismatch") for row in validation.mismatches))
        self.assertEqual(str(state.extraction.raw_response_summary.get("replay") or ""), "adapter")

    def test_json_adapter_loader_reports_schema_diagnostics(self) -> None:
        from price_extractor.json_adapters import load_json_adapters_with_diagnostics

        with tempfile.TemporaryDirectory() as tmp_dir:
            adapter_dir = Path(tmp_dir)
            (adapter_dir / "valid.json").write_text(
                json.dumps(
                    {
                        "id": "valid_adapter",
                        "match": {"domains": ["example.test"]},
                        "endpoint": {"url": "https://api.example.test/price", "method": "POST"},
                        "request_template": {"quantity": 100},
                        "inject": [{"input_key": "quantity", "path": "quantity", "cast": "int"}],
                        "response_extract": {"price_path": "priceGross", "currency_const": "EUR"},
                    }
                ),
                encoding="utf-8",
            )
            (adapter_dir / "invalid_schema.json").write_text(
                json.dumps(
                    {
                        "id": "bad_adapter",
                        "match": {},
                        "endpoint": {"url": "", "method": "PATCH"},
                        "request_template": {},
                        "response_extract": {},
                    }
                ),
                encoding="utf-8",
            )
            (adapter_dir / "invalid_json.json").write_text("{ this-is-not-json", encoding="utf-8")

            adapters, diagnostics = load_json_adapters_with_diagnostics(adapter_dir)

        self.assertEqual(len(adapters), 1)
        self.assertEqual(str(adapters[0].get("id")), "valid_adapter")
        self.assertEqual(int(diagnostics.get("loadedCount") or 0), 1)
        self.assertEqual(int(diagnostics.get("invalidCount") or 0), 2)
        warnings = list(diagnostics.get("warnings") or [])
        warning_kinds = {str(row.get("kind")) for row in warnings if isinstance(row, dict)}
        self.assertIn("invalid_json", warning_kinds)
        self.assertIn("invalid_schema", warning_kinds)

    def test_json_adapter_match_diagnostics_reports_miss_reason(self) -> None:
        from price_extractor.json_adapters import match_json_adapter_with_diagnostics

        adapters = [
            {
                "id": "other_host",
                "match": {"domains": ["other.example"]},
                "endpoint": {"url": "https://api.other.example/price", "method": "POST"},
                "request_template": {},
                "response_extract": {"price_path": "price"},
            }
        ]

        matched, diagnostics = match_json_adapter_with_diagnostics(adapters, "https://example.test/product")
        self.assertIsNone(matched)
        self.assertEqual(str(diagnostics.get("reason")), "host_mismatch")
        self.assertEqual(int(diagnostics.get("loadedCount") or 0), 1)

    def test_json_adapter_loader_skips_disabled_adapter(self) -> None:
        from price_extractor.json_adapters import load_json_adapters_with_diagnostics

        with tempfile.TemporaryDirectory() as tmp_dir:
            adapter_dir = Path(tmp_dir)
            (adapter_dir / "disabled.json").write_text(
                json.dumps(
                    {
                        "id": "disabled_adapter",
                        "enabled": False,
                        "match": {"domains": ["example.test"]},
                        "endpoint": {"url": "https://api.example.test/price", "method": "POST"},
                        "request_template": {"quantity": 100},
                        "response_extract": {"price_path": "priceGross"},
                    }
                ),
                encoding="utf-8",
            )

            adapters, diagnostics = load_json_adapters_with_diagnostics(adapter_dir)

        self.assertEqual(adapters, [])
        self.assertEqual(int(diagnostics.get("loadedCount") or 0), 0)
        self.assertEqual(int(diagnostics.get("skippedCount") or 0), 1)
        warnings = list(diagnostics.get("warnings") or [])
        self.assertTrue(any(str(row.get("kind")) == "disabled_adapter" for row in warnings if isinstance(row, dict)))

    def test_json_adapter_loader_skips_env_gated_adapter_without_opt_in(self) -> None:
        from price_extractor.json_adapters import load_json_adapters_with_diagnostics

        with tempfile.TemporaryDirectory() as tmp_dir:
            adapter_dir = Path(tmp_dir)
            (adapter_dir / "env_gated.json").write_text(
                json.dumps(
                    {
                        "id": "env_gated_adapter",
                        "enabled": True,
                        "enabled_when_env": "TEST_ENABLE_JSON_ADAPTER",
                        "match": {"domains": ["example.test"]},
                        "endpoint": {"url": "https://api.example.test/price", "method": "POST"},
                        "request_template": {"quantity": 100},
                        "response_extract": {"price_path": "priceGross"},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict("os.environ", {}, clear=False):
                adapters, diagnostics = load_json_adapters_with_diagnostics(adapter_dir)

            self.assertEqual(adapters, [])
            self.assertEqual(int(diagnostics.get("loadedCount") or 0), 0)
            self.assertEqual(int(diagnostics.get("skippedCount") or 0), 1)
            warnings = list(diagnostics.get("warnings") or [])
            self.assertTrue(any(str(row.get("kind")) == "disabled_by_env" for row in warnings if isinstance(row, dict)))

            with mock.patch.dict("os.environ", {"TEST_ENABLE_JSON_ADAPTER": "1"}, clear=False):
                adapters_enabled, diagnostics_enabled = load_json_adapters_with_diagnostics(adapter_dir)

            self.assertEqual(len(adapters_enabled), 1)
            self.assertEqual(str(adapters_enabled[0].get("id")), "env_gated_adapter")
            self.assertEqual(int(diagnostics_enabled.get("loadedCount") or 0), 1)

    def test_json_adapter_executes_print24_when_opted_in(self) -> None:
        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            expected_currency="EUR",
            options={"format": "A5", "quantity": 250},
            network_traces=[],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://print24.com/api/de/itemmaster/calculation/productDetails/",
            payload_template={},
            confidence=0.8,
            notes="",
        )

        agent = ExecutionAgent()
        seen_body: dict[str, Any] = {}

        def fake_urlopen(req, timeout=15):
            nonlocal seen_body
            raw_body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
            seen_body = json.loads(raw_body) if raw_body else {}
            return FakeHttpResponse(
                json.dumps({"prices": {"final_prices": {"total_gross_value": 70.47}}}),
                content_type="application/json",
            )

        with mock.patch.dict("os.environ", {"PRICE_ADAPTER_PRINT24_ENABLE": "1"}, clear=False):
            with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
                result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.price_value, 70.47)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(replay_summary.get("selectedVia"), "json_adapter:print24_brochure_v1")

        summary = dict(result.raw_response_summary or {})
        self.assertEqual(summary.get("replay"), "adapter")

        adapter_diag = dict(summary.get("adapterDiagnostics") or {})
        self.assertEqual(adapter_diag.get("reason"), "matched")
        self.assertEqual(adapter_diag.get("matchedAdapterId"), "print24_brochure_v1")

        self.assertEqual(int(seen_body.get("item_group_id") or 0), 2)
        self.assertEqual(int(seen_body.get("product_alias_id") or 0), 388)
        props = {
            str(row.get("name")): str(row.get("id"))
            for row in list(seen_body.get("properties") or [])
            if isinstance(row, dict)
        }
        self.assertEqual(props.get("format"), "268")
        self.assertEqual(props.get("quantity"), "438")

    def test_json_adapter_print24_unsupported_quantity_keeps_template_id(self) -> None:
        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            expected_currency="EUR",
            options={"format": "A5", "quantity": 999},
            network_traces=[],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://print24.com/api/de/itemmaster/calculation/productDetails/",
            payload_template={},
            confidence=0.8,
            notes="",
        )

        agent = ExecutionAgent()
        seen_body: dict[str, Any] = {}

        def fake_urlopen(req, timeout=15):
            nonlocal seen_body
            raw_body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
            seen_body = json.loads(raw_body) if raw_body else {}
            return FakeHttpResponse(
                json.dumps({"prices": {"final_prices": {"total_gross_value": 29.68}}}),
                content_type="application/json",
            )

        with mock.patch.dict("os.environ", {"PRICE_ADAPTER_PRINT24_ENABLE": "1"}, clear=False):
            with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
                result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.price_value, 29.68)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(replay_summary.get("selectedVia"), "json_adapter:print24_brochure_v1")

        props = {
            str(row.get("name")): str(row.get("id"))
            for row in list(seen_body.get("properties") or [])
            if isinstance(row, dict)
        }
        self.assertEqual(props.get("format"), "268")
        self.assertEqual(props.get("quantity"), "339")

        summary = dict(result.raw_response_summary or {})
        request_applied = dict(summary.get("requestTemplateApplied") or {})
        injected_rows = list(request_applied.get("injected") or [])
        injected_keys = {str(row.get("inputKey")) for row in injected_rows if isinstance(row, dict)}
        self.assertIn("format", injected_keys)
        self.assertNotIn("quantity", injected_keys)

    def test_http_replay_retries_on_429_then_succeeds(self) -> None:
        trace = {
            "url": "https://api.example.test/pricing",
            "method": "POST",
            "resource_type": "xhr",
            "status": 200,
            "request_headers": {"content-type": "application/json"},
            "response_content_type": "application/json",
            "post_data": "{}",
        }
        target = TargetInput(
            site_name="example.test",
            product_url="https://example.test/product",
            product_type="brochure",
            network_traces=[trace],
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
                "http_runtime": {
                    "timeout_seconds": 10.0,
                    "min_delay_ms": 0,
                    "jitter_ms": 0,
                    "max_retries": 1,
                    "backoff_base_ms": 0,
                    "backoff_max_ms": 0,
                    "proxy_rotation": "none",
                    "proxy_pool": [],
                },
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=trace["url"], payload_template={}, confidence=0.8, notes="")
        agent = ExecutionAgent()

        http_429 = error.HTTPError(
            trace["url"],
            429,
            "Too Many Requests",
            {"content-type": "application/json"},
            io.BytesIO(b'{"error":"rate_limited"}'),
        )

        with mock.patch(
            "price_extractor.agents.request.urlopen",
            side_effect=[http_429, FakeHttpResponse(json.dumps({"total_gross_value": 12.34}))],
        ) as urlopen_mock:
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        self.assertEqual(urlopen_mock.call_count, 2)
        self.assertTrue(any(row.get("result") == "retry" for row in replay_summary.get("attempts", [])))

    def test_http_replay_applies_per_host_pacing(self) -> None:
        primary_url = "https://api.example.test/pricing"
        secondary_url = "https://api.example.test/pricing-alt"
        traces = [
            {
                "url": primary_url,
                "method": "POST",
                "resource_type": "xhr",
                "status": 200,
                "request_headers": {"content-type": "application/json"},
                "response_content_type": "application/json",
                "post_data": "{}",
            },
            {
                "url": secondary_url,
                "method": "POST",
                "resource_type": "xhr",
                "status": 200,
                "request_headers": {"content-type": "application/json"},
                "response_content_type": "application/json",
                "post_data": "{}",
            },
        ]
        target = TargetInput(
            site_name="example.test",
            product_url="https://example.test/product",
            product_type="brochure",
            network_traces=traces,
            bootstrap_signals={
                "cookies": {},
                "request_only": True,
                "learned_normalization_rules": {},
                "http_runtime": {
                    "timeout_seconds": 10.0,
                    "min_delay_ms": 250,
                    "jitter_ms": 0,
                    "max_retries": 0,
                    "backoff_base_ms": 0,
                    "backoff_max_ms": 0,
                    "proxy_rotation": "none",
                    "proxy_pool": [],
                },
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=primary_url, payload_template={}, confidence=0.8, notes="")
        state.observation = ObservationBundle(has_script_heavy_ui=True, has_api_calls=True, requires_session=True, endpoint_rankings=[{"url": secondary_url, "score": 12}])
        agent = ExecutionAgent()

        with mock.patch("price_extractor.agents.time.monotonic", side_effect=[100.0, 100.0, 100.0, 100.0]):
            with mock.patch("price_extractor.agents.time.sleep") as sleep_mock:
                with mock.patch(
                    "price_extractor.agents.request.urlopen",
                    side_effect=[error.URLError("boom"), FakeHttpResponse(json.dumps({"total_gross_value": 18.9}))],
                ):
                    result, _summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        self.assertTrue(any(call.args and float(call.args[0]) >= 0.25 for call in sleep_mock.mock_calls))

    def test_proxy_is_quarantined_after_consecutive_failures(self) -> None:
        agent = ExecutionAgent()
        runtime = {
            "proxy_pool": ["http://proxy-1.example:8080"],
            "proxy_rotation": "round_robin",
            "proxy_failure_threshold": 2,
            "proxy_cooldown_seconds": 60,
        }

        with mock.patch("price_extractor.agents.time.monotonic", return_value=100.0):
            proxy_a, _idx_a = agent._choose_proxy(runtime)
            self.assertEqual(proxy_a, "http://proxy-1.example:8080")

            agent._record_proxy_attempt_result(proxy_a, runtime, success=False)
            proxy_b, _idx_b = agent._choose_proxy(runtime)
            self.assertEqual(proxy_b, "http://proxy-1.example:8080")

            agent._record_proxy_attempt_result(proxy_b, runtime, success=False)
            proxy_c, idx_c = agent._choose_proxy(runtime)

        self.assertIsNone(proxy_c)
        self.assertIsNone(idx_c)

    def test_print24_template_synthesis_rewrites_property_ids(self) -> None:
        trace = load_fixture("print24_productdetails_trace.json")

        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            options={"format": "A5", "quantity": 250},
            network_traces=[trace],
            bootstrap_signals={
                "cookies": {"portalName": "print24"},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=trace["url"], payload_template={}, confidence=0.8, notes="")

        candidates = build_site_replay_candidates(
            state,
            effective_options=dict(target.options),
            raw_requested_options=dict(target.options),
        )
        candidate = next((row for row in candidates if row.get("source") == "print24_synthesized_template"), None)
        self.assertIsNotNone(candidate)
        body = json.loads(candidate["body_text"])
        props = {row["name"]: str(row["id"]) for row in body["properties"]}
        self.assertEqual(props.get("format"), "268")
        self.assertEqual(props.get("quantity"), "438")
        applied = dict(candidate.get("request_template_applied") or {})
        self.assertEqual(applied.get("kind"), "print24_property_ids")

    def test_print24_site_adapter_replay_selects_synthesized_candidate(self) -> None:
        trace = load_fixture("print24_productdetails_trace.json")

        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            expected_currency="EUR",
            options={"format": "A5", "quantity": 250},
            network_traces=[trace],
            bootstrap_signals={
                "cookies": {"portalName": "print24"},
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=trace["url"], payload_template={}, confidence=0.8, notes="")

        agent = ExecutionAgent()
        seen_headers: dict[str, str] = {}
        seen_body: dict[str, Any] = {}

        def fake_urlopen(req, timeout=15):
            nonlocal seen_headers, seen_body
            seen_headers = {key.lower(): value for key, value in req.header_items()}
            raw_body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
            seen_body = json.loads(raw_body) if raw_body else {}
            return FakeHttpResponse(
                json.dumps({"prices": {"final_prices": {"total_gross_value": 70.47}}}),
                content_type="application/json",
            )

        with mock.patch.dict("os.environ", {"PRICE_ADAPTER_PRINT24_ENABLE": "0"}, clear=False):
            with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
                result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.price_value, 70.47)
        self.assertEqual(result.currency, "EUR")
        self.assertEqual(seen_headers.get("portal"), "print24")
        self.assertEqual(replay_summary.get("selectedVia"), "print24_synthesized_template")

        props = {str(row.get("name")): str(row.get("id")) for row in list(seen_body.get("properties") or []) if isinstance(row, dict)}
        self.assertEqual(props.get("format"), "268")
        self.assertEqual(props.get("quantity"), "438")

        summary = dict(result.raw_response_summary or {})
        applied = dict(summary.get("requestTemplateApplied") or {})
        self.assertEqual(applied.get("kind"), "print24_property_ids")
        updates = {
            str(row.get("name")): (str(row.get("from")), str(row.get("to")))
            for row in list(applied.get("propertiesUpdated") or [])
            if isinstance(row, dict)
        }
        self.assertEqual(updates.get("format"), ("222", "268"))
        self.assertEqual(updates.get("quantity"), ("339", "438"))

    def test_print24_site_adapter_replay_is_stable_across_multiple_option_sets(self) -> None:
        trace = load_fixture("print24_productdetails_trace.json")
        cases = [
            {
                "options": {"format": "A5", "quantity": 250},
                "expected_props": {"format": "268", "quantity": "438"},
                "expected_updates": {"format": ("222", "268"), "quantity": ("339", "438")},
            },
            {
                "options": {"format": "A6", "quantity": 250},
                "expected_props": {"format": "222", "quantity": "438"},
                "expected_updates": {"quantity": ("339", "438")},
            },
            {
                "options": {"format": "A5", "quantity": 10},
                "expected_props": {"format": "268", "quantity": "339"},
                "expected_updates": {"format": ("222", "268")},
            },
        ]

        agent = ExecutionAgent()

        for case in cases:
            options = dict(case["options"])
            target = TargetInput(
                site_name="print24.com",
                product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
                product_type="brochure",
                expected_currency="EUR",
                options=options,
                network_traces=[trace],
                bootstrap_signals={
                    "cookies": {"portalName": "print24"},
                    "request_only": True,
                    "learned_normalization_rules": {},
                },
            )
            state = RunState(target=target)
            state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=trace["url"], payload_template={}, confidence=0.8, notes="")

            seen_headers: dict[str, str] = {}
            seen_body: dict[str, Any] = {}

            def fake_urlopen(req, timeout=15):
                nonlocal seen_headers, seen_body
                seen_headers = {key.lower(): value for key, value in req.header_items()}
                raw_body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
                seen_body = json.loads(raw_body) if raw_body else {}
                props = {
                    str(row.get("name")): str(row.get("id"))
                    for row in list(seen_body.get("properties") or [])
                    if isinstance(row, dict)
                }
                quantity_id = props.get("quantity")
                total_gross = 70.47 if quantity_id == "438" else 29.68
                return FakeHttpResponse(
                    json.dumps({"prices": {"final_prices": {"total_gross_value": total_gross}}}),
                    content_type="application/json",
                )

            with self.subTest(options=options):
                with mock.patch.dict("os.environ", {"PRICE_ADAPTER_PRINT24_ENABLE": "0"}, clear=False):
                    with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
                        result, replay_summary = agent._try_http_replay(state)

                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.currency, "EUR")
                self.assertEqual(seen_headers.get("portal"), "print24")
                self.assertEqual(replay_summary.get("selectedVia"), "print24_synthesized_template")

                props = {
                    str(row.get("name")): str(row.get("id"))
                    for row in list(seen_body.get("properties") or [])
                    if isinstance(row, dict)
                }
                self.assertEqual(props, dict(case["expected_props"]))

                expected_price = 70.47 if props.get("quantity") == "438" else 29.68
                self.assertEqual(result.price_value, expected_price)

                summary = dict(result.raw_response_summary or {})
                applied = dict(summary.get("requestTemplateApplied") or {})
                self.assertEqual(applied.get("kind"), "print24_property_ids")
                updates = {
                    str(row.get("name")): (str(row.get("from")), str(row.get("to")))
                    for row in list(applied.get("propertiesUpdated") or [])
                    if isinstance(row, dict)
                }
                self.assertEqual(updates, dict(case["expected_updates"]))

    def test_wir_machen_druck_template_synthesis_adds_price_scale_id(self) -> None:
        traces = load_fixture("wir_machen_druck_template_traces.json")
        target = TargetInput(
            site_name="wir-machen-druck.de",
            product_url="https://www.wir-machen-druck.de/broschuere-mit-drahtheftung-endformat-din-a4-8seitig.html",
            product_type="brochure",
            options={"quantity": 250, "format": "A5", "material": "130gsm"},
            network_traces=traces,
            bootstrap_signals={
                "request_only": True,
                "learned_normalization_rules": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://www.wir-machen-druck.de/wmdrest/article/get-price",
            payload_template={},
            confidence=0.8,
            notes="",
        )

        candidates = build_site_replay_candidates(
            state,
            effective_options=dict(target.options),
            raw_requested_options=dict(target.options),
        )
        scaled = next((row for row in candidates if row.get("source") == "wir_machen_druck_price_scale"), None)
        manual = next((row for row in candidates if row.get("source") == "wir_machen_druck_quantity_manual"), None)
        self.assertIsNotNone(scaled)
        self.assertIsNotNone(manual)

        scaled_body = json.loads(str(scaled.get("body_text") or "{}"))
        self.assertEqual(scaled_body.get("quantity"), "250")
        self.assertEqual(str(scaled_body.get("priceScaleId")), "65315686")
        self.assertFalse(bool(scaled_body.get("isIndividualQuantity")))

    def test_wir_machen_druck_http_replay_accepts_requested_quantity(self) -> None:
        traces = load_fixture("wir_machen_druck_template_traces.json")
        target = TargetInput(
            site_name="wir-machen-druck.de",
            product_url="https://www.wir-machen-druck.de/broschuere-mit-drahtheftung-endformat-din-a4-8seitig.html",
            product_type="brochure",
            expected_currency="EUR",
            options={"quantity": 250, "format": "A5", "material": "130gsm"},
            network_traces=traces,
            bootstrap_signals={
                "request_only": True,
                "learned_normalization_rules": {},
                "cookies": {},
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://www.wir-machen-druck.de/wmdrest/article/get-price",
            payload_template={},
            confidence=0.8,
            notes="",
        )

        agent = ExecutionAgent()
        seen_payload: dict[str, object] | None = None

        def fake_urlopen(req, timeout=15):
            nonlocal seen_payload
            body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
            seen_payload = json.loads(body) if body else None
            return FakeHttpResponse(
                json.dumps(
                    {
                        "code": 200,
                        "message": "OK",
                        "data": {
                            "response": {
                                "quantity": 250,
                                "price": "142.57",
                                "priceWithTax": "169.66",
                                "currency": "EUR",
                            }
                        },
                    }
                ),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result)
        self.assertEqual(replay_summary.get("selectedVia"), "wir_machen_druck_price_scale")
        self.assertIsNotNone(seen_payload)
        self.assertEqual(str(seen_payload.get("priceScaleId")), "65315686")
        self.assertEqual(seen_payload.get("quantity"), "250")
        self.assertFalse(bool(seen_payload.get("isIndividualQuantity")))
        self.assertEqual(result.accepted_configuration.get("quantity"), 250)
        self.assertIsNotNone(result.price_value)
        self.assertGreater(float(result.price_value or 0.0), 0.0)
        self.assertEqual(dict(result.raw_response_summary.get("requestTemplateApplied") or {}).get("kind"), "wir_machen_druck_price_scale")

        state.extraction = result
        validation = ValidationAgent().run(state)
        self.assertTrue(validation.is_valid)
        self.assertEqual(validation.mismatches, [])

    def test_option_key_aliases_cover_multilingual_printing_terms(self) -> None:
        aliases = cli._option_key_aliases("Zusätzlicher_Umschlag")
        self.assertIn("cover", aliases)

        aliases = cli._option_key_aliases("Ausführung Innenteil")
        self.assertIn("print", aliases)

        aliases = cli._option_key_aliases("Auflage")
        self.assertIn("quantity", aliases)

    def test_validation_allows_quantity_and_format_normalization(self) -> None:
        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            options={"quantity": 250, "format": "A5"},
            bootstrap_signals={"request_only": True, "learned_normalization_rules": {}},
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint="https://print24.com/api/de/itemmaster/calculation/productDetails/", payload_template={}, confidence=0.9, notes="")
        state.extraction = ExtractionResult(
            success=True,
            accepted_configuration={"quantity": "250 Stück", "format": "148 x 210 mm DIN A5 Hochformat"},
            price_value=123.45,
            currency="EUR",
            raw_response_summary={"effectiveRequestedOptions": {"quantity": 250, "format": "A5"}, "replay": "http", "fallback": "none"},
        )

        result = ValidationAgent().run(state)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.mismatches, [])

    def test_replay_candidate_selection_for_saxoprint_and_onlineprinters(self) -> None:
        saxo_target = TargetInput(
            site_name="saxoprint.de",
            product_url="https://www.saxoprint.de/broschueren/broschueren-drucken",
            product_type="brochure",
        )
        saxo_state = RunState(target=saxo_target)
        saxo_state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint="https://api.saxoprint.de/product-configuration/get-product-prices",
            payload_template={},
            confidence=0.9,
            notes="",
        )
        saxo_state.observation = ObservationBundle(
            has_script_heavy_ui=True,
            has_api_calls=True,
            requires_session=True,
            endpoint_rankings=[
                {"url": "https://api.saxoprint.de/product-configuration/get-product-prices", "score": 30},
                {"url": "https://api.saxoprint.de/product-configuration/get-delivery-prices", "score": 20},
            ],
        )

        online_target = TargetInput(
            site_name="onlineprinters.de",
            product_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
            product_type="brochure",
            network_traces=[{"url": "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4", "resource_type": "document", "status": 200}],
        )
        online_state = RunState(target=online_target)
        online_state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=None, payload_template={}, confidence=0.7, notes="")
        online_state.observation = ObservationBundle(
            has_script_heavy_ui=True,
            has_api_calls=True,
            requires_session=True,
            endpoint_rankings=[
                {"url": "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4", "score": 25},
            ],
        )

        saxo_candidates = ExecutionAgent._build_http_replay_candidates(saxo_state)
        online_candidates = ExecutionAgent._build_http_replay_candidates(online_state)

        self.assertEqual(saxo_candidates[0]["url"], "https://api.saxoprint.de/product-configuration/get-product-prices")
        self.assertEqual(online_candidates[0]["url"], "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4")
        self.assertEqual(sum(1 for row in online_candidates if row["url"] == online_target.product_url), 1)

    def test_onlineprinters_prevalidation_and_template_synthesis(self) -> None:
        bootstrap = load_fixture("onlineprinters_enriched_bootstrap.json")
        traces = load_fixture("onlineprinters_template_trace.json")

        valid = cli._prevalidate_requested_options({"quantity": 250, "seitig": 16}, bootstrap)
        invalid = cli._prevalidate_requested_options({"seitig": 24}, bootstrap)

        self.assertTrue(valid["valid"])
        self.assertFalse(invalid["valid"])
        self.assertIn("16-seitig", invalid["unmatched"][0]["suggestions"])

        target = TargetInput(
            site_name="onlineprinters.de",
            product_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
            product_type="brochure",
            options={"quantity": 250, "seitig": 16},
            network_traces=traces,
            bootstrap_signals={
                "option_prevalidation": valid,
                "request_templates": bootstrap["request_templates"],
                "option_catalog": bootstrap["option_catalog"],
                "quantity_signal": bootstrap["quantity_signal"],
                "request_only": True,
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(strategy=Strategy.HYBRID, endpoint=target.product_url, payload_template={}, confidence=0.7, notes="")

        candidates = build_site_replay_candidates(
            state,
            effective_options={"quantity": 250, "seitig": 16},
            raw_requested_options={"quantity": 250, "seitig": 16},
        )
        candidate = next((row for row in candidates if row.get("source") == "onlineprinters_synthesized_template"), None)
        self.assertIsNotNone(candidate)
        body_text = candidate["body_text"]
        self.assertIn("input_var_PBRA444_2_1=16-seitig", body_text)
        self.assertIn("input_var_PBRA444_3_1=250", body_text)
        self.assertIn("PBRA444.135.161000", ExecutionAgent._decode_template_text(body_text))

    def test_viaprinto_query_template_synthesis(self) -> None:
        traces = load_fixture("viaprinto_template_trace.json")
        target = TargetInput(
            site_name="viaprinto.de",
            product_url="https://www.viaprinto.de/-/content_size?PAGES=4&CONTENT_SIZE=210x297&AMOUNT=200",
            product_type="broschueren",
            options={"amount": 500, "content_size": "148x210", "pages": 8},
            network_traces=traces,
            bootstrap_signals={"request_only": True},
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint=target.product_url,
            payload_template={},
            confidence=0.8,
            notes="",
        )

        candidates = build_site_replay_candidates(
            state,
            effective_options={"amount": 500, "content_size": "148x210", "pages": 8},
            raw_requested_options={"amount": 500, "content_size": "148x210", "pages": 8},
        )

        candidate = next((row for row in candidates if row.get("source") == "viaprinto_synthesized_query"), None)
        self.assertIsNotNone(candidate)
        self.assertIn("AMOUNT=500", str(candidate.get("url") or ""))
        self.assertIn("CONTENT_SIZE=148x210", str(candidate.get("url") or ""))
        self.assertIn("PAGES=8", str(candidate.get("url") or ""))
        applied = dict(candidate.get("request_template_applied") or {})
        self.assertEqual(applied.get("kind"), "viaprinto_query_params")

    def test_print24_end_to_end_replay_from_captured_fixture(self) -> None:
        # Regression smoke test for the full bootstrap -> replay -> adapter chain.
        # Drives ExecutionAgent._try_http_replay with real captured artifacts (network
        # traces, cookies, JSON adapter) from a successful print24 run so that any
        # future regression in adapter loading, candidate prioritization, cookie/header
        # injection, or adapter response extraction will surface here.
        fixture_dir = FIXTURES / "print24_end_to_end"
        traces = json.loads((fixture_dir / "network_traces.json").read_text(encoding="utf-8"))
        cookies = json.loads((fixture_dir / "cookies.json").read_text(encoding="utf-8"))
        expected = json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8"))
        json_adapters_dir = fixture_dir / "json_adapters"

        # Sanity-check the fixture is intact so failures here point at the test data,
        # not the system under test.
        self.assertGreater(len(traces), 0, "fixture network_traces.json is empty")
        self.assertIn("portalName", cookies, "fixture cookies missing portalName")
        self.assertEqual(expected["price"], 70.47)
        self.assertEqual(expected["replay_mode"], "adapter")

        target = TargetInput(
            site_name="print24.com",
            product_url=str(expected["productUrl"]),
            product_type="brochure",
            expected_currency="EUR",
            network_traces=traces,
            bootstrap_signals={
                "cookies": cookies,
                "request_only": False,
                "learned_normalization_rules": {},
                "json_adapters_dir": str(json_adapters_dir),
            },
        )
        state = RunState(target=target)
        state.plan = StrategyPlan(
            strategy=Strategy.HYBRID,
            endpoint=str(expected["selectedEndpoint"]),
            payload_template={},
            confidence=0.8,
            notes="",
        )

        agent = ExecutionAgent()
        seen_requests: list[dict[str, Any]] = []

        # Replay the print24-shaped response that the JSON adapter parses. The exact
        # numeric value below is what the live print24 run returned for the captured
        # configuration; the adapter's response_extract path is
        # `prices.final_prices.total_gross_value`.
        def fake_urlopen(req, timeout=15):
            body = (getattr(req, "data", None) or b"").decode("utf-8", errors="replace")
            try:
                parsed_body = json.loads(body) if body else {}
            except Exception:
                parsed_body = {"_raw": body[:200]}
            seen_requests.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "headers": {k.lower(): v for k, v in req.header_items()},
                    "body": parsed_body,
                }
            )
            return FakeHttpResponse(
                json.dumps({"prices": {"final_prices": {"total_gross_value": expected["price"]}}}),
                content_type="application/json",
            )

        with mock.patch("price_extractor.agents.request.urlopen", side_effect=fake_urlopen):
            result, replay_summary = agent._try_http_replay(state)

        self.assertIsNotNone(result, "replay should have produced an ExtractionResult")
        assert result is not None
        self.assertEqual(result.price_value, expected["price"])
        self.assertEqual(result.currency, expected["currency"])

        # The JSON adapter must have been the one that fired (not the bare HTTP path).
        self.assertEqual(replay_summary["decision"], "used")
        self.assertEqual(replay_summary["selectedEndpoint"], expected["selectedEndpoint"])
        self.assertEqual(replay_summary["selectedVia"], expected["selectedVia"])

        adapter_result = dict((result.raw_response_summary or {}).get("adapterResult") or {})
        self.assertEqual(adapter_result.get("price"), expected["price"])
        self.assertEqual(adapter_result.get("source"), "adapter")
        self.assertEqual((result.raw_response_summary or {}).get("replay"), "adapter")

        # Confirm the outgoing request actually hit the adapter's endpoint with the
        # portal header (from cookies) and a JSON body shaped like the adapter template.
        self.assertEqual(len(seen_requests), 1)
        sent = seen_requests[0]
        self.assertEqual(sent["url"], expected["selectedEndpoint"])
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["headers"].get("portal"), "print24")
        self.assertIsInstance(sent["body"], dict)
        self.assertIn("properties", sent["body"])
        # The adapter template ships product_alias_id; this guards against accidental
        # body re-templating that would strip required keys.
        self.assertEqual(sent["body"].get("product_alias_id"), 388)

    def test_persists_replay_template_on_deterministic_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "knowledge.db")
            store = KnowledgeStore(db_path=db_path)
            try:
                orchestrator = ExtractionOrchestrator(store)

                target = TargetInput(
                    site_name="onlineprinters.de",
                    product_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
                    product_type="brochure",
                    options={"quantity": 250},
                    bootstrap_signals={"request_only": True, "learned_normalization_rules": {}},
                )
                state = RunState(target=target, max_attempts=1)

                orchestrator.discovery.run = mock.Mock(
                    return_value=ObservationBundle(has_script_heavy_ui=True, has_api_calls=True, requires_session=True)
                )
                orchestrator.feasibility.run = mock.Mock(
                    return_value=FeasibilityResult(
                        feasible=True,
                        complexity=Complexity.HIGH,
                        recommended_strategy=Strategy.HYBRID,
                        rationale="",
                    )
                )
                orchestrator.planner.run = mock.Mock(
                    return_value=StrategyPlan(
                        strategy=Strategy.HYBRID,
                        endpoint="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
                        payload_template={},
                        confidence=0.8,
                        notes="",
                    )
                )
                orchestrator.executor.run = mock.Mock(
                    return_value=ExtractionResult(
                        success=True,
                        accepted_configuration={"quantity": 250},
                        price_value=123.45,
                        currency="EUR",
                        raw_response_summary={
                            "endpoint": "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
                            "fallback": "none",
                            "requestTemplateApplied": {"kind": "onlineprinters_setlink_form", "fieldsUpdated": []},
                        },
                    )
                )
                orchestrator.validator.run = mock.Mock(return_value=ValidationResult(is_valid=True, mismatches=[]))

                orchestrator.run(state)

                templates = store.get_site_replay_templates("onlineprinters.de")
                self.assertEqual(len(templates), 1)
                self.assertEqual(templates[0]["templateKind"], "onlineprinters_setlink_form")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
