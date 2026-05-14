import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from price_extractor.agentic.adapter_generation import _build_inject_rules, _group_label_matches_option
from price_extractor.agentic.onboarding import (
    OPTION_KEY_ALIASES,
    _canonical_option_key,
    _infer_option_key_from_path,
    build_onboarding_proposal,
)


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
            self.assertGreaterEqual(len(proposal.trace_template_hints), 1)
            first_hint = dict(proposal.trace_template_hints[0] or {})
            mutations = list(first_hint.get("candidate_mutations") or [])
            self.assertTrue(any(str(row.get("option_key")) == "quantity" for row in mutations if isinstance(row, dict)))
            self.assertIn("class ExampleSiteAdapter", proposal.adapter_stub)
            self.assertIn("def build_http_replay_candidates", proposal.adapter_stub)


class TestCanonicalOptionKey(unittest.TestCase):
    def test_german_color_aliases_resolve_to_color(self) -> None:
        for token in ["farbenU", "farbenI", "farbigkeit", "farbe"]:
            self.assertEqual(_canonical_option_key(token), "color", token)

    def test_german_paper_aliases_resolve_to_material(self) -> None:
        for token in ["papierU", "papierI", "paper", "material"]:
            self.assertEqual(_canonical_option_key(token), "material", token)

    def test_german_binding_aliases_resolve_to_binding(self) -> None:
        for token in ["verarbeitung", "bindung", "binding"]:
            self.assertEqual(_canonical_option_key(token), "binding", token)

    def test_german_finishing_aliases_resolve_to_finishing(self) -> None:
        for token in ["finishingO", "finishingI", "veredelung"]:
            self.assertEqual(_canonical_option_key(token), "finishing", token)

    def test_orientation_alias_resolves(self) -> None:
        self.assertEqual(_canonical_option_key("aspect_ratio"), "orientation")
        self.assertEqual(_canonical_option_key("ausrichtung"), "orientation")

    def test_quantity_german_aliases(self) -> None:
        self.assertEqual(_canonical_option_key("auflage"), "quantity")
        self.assertEqual(_canonical_option_key("menge"), "quantity")

    def test_unknown_token_returns_none(self) -> None:
        self.assertIsNone(_canonical_option_key("totally-unrelated"))
        self.assertIsNone(_canonical_option_key(""))

    def test_infer_option_key_uses_canonical_table(self) -> None:
        self.assertEqual(_infer_option_key_from_path("properties[name=papierU].id"), "material")
        self.assertEqual(_infer_option_key_from_path("properties[name=quantity].id"), "quantity")

    def test_alias_table_covers_expected_canonical_keys(self) -> None:
        expected = {"quantity", "format", "material", "pages", "color", "binding", "finishing", "orientation"}
        self.assertEqual(set(OPTION_KEY_ALIASES.keys()), expected)


class TestPropertiesWalkerExpandedVocabulary(unittest.TestCase):
    def test_german_property_names_emit_canonical_mutations(self) -> None:
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "spec.json").write_text(
                json.dumps(
                    {
                        "site_name": "example",
                        "url": "https://example.test/p",
                        "product_type": "brochure",
                        "options": {"quantity": 250},
                    },
                ),
                encoding="utf-8",
            )
            (run_dir / "summary.json").write_text(
                json.dumps({"site": "example", "url": "https://example.test/p"}),
                encoding="utf-8",
            )
            (run_dir / "bootstrap.json").write_text(
                json.dumps({"request_templates": {}, "network_traces": [], "option_catalog": []}),
                encoding="utf-8",
            )
            post_body = json.dumps(
                {
                    "properties": [
                        {"id": "1", "name": "quantity"},
                        {"id": "2", "name": "format"},
                        {"id": "3", "name": "papierU"},
                        {"id": "4", "name": "farbenU"},
                        {"id": "5", "name": "verarbeitung"},
                        {"id": "6", "name": "finishingO"},
                        {"id": "7", "name": "aspect_ratio"},
                        {"id": "8", "name": "seitenU"},
                    ]
                }
            )
            (run_dir / "network_traces.json").write_text(
                json.dumps(
                    [
                        {
                            "url": "https://api.example.test/calc",
                            "method": "POST",
                            "status": 200,
                            "resource_type": "xhr",
                            "response_content_type": "application/json",
                            "response_body_preview": "{\"total_gross_value\": 99.99}",
                            "post_data": post_body,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (run_dir / "option_catalog.json").write_text("[]", encoding="utf-8")

            proposal = build_onboarding_proposal(
                run_dir=str(run_dir),
                knowledge_db=str(run_dir / "knowledge.db"),
                top_n=5,
            )

            first_hint = dict(proposal.trace_template_hints[0] or {})
            mutations = [row for row in first_hint.get("candidate_mutations") or [] if isinstance(row, dict)]
            keys_emitted = {str(row.get("option_key")) for row in mutations}
            for expected in {"quantity", "format", "material", "color", "binding", "finishing", "orientation", "pages"}:
                self.assertIn(expected, keys_emitted, f"missing {expected} in mutations: {keys_emitted}")


class TestGroupLabelMatching(unittest.TestCase):
    def test_german_labels_match_canonical_keys(self) -> None:
        self.assertTrue(_group_label_matches_option("Farbigkeit", "color"))
        self.assertTrue(_group_label_matches_option("Veredelung", "finishing"))
        self.assertTrue(_group_label_matches_option("Bindung", "binding"))
        self.assertTrue(_group_label_matches_option("Auflage", "quantity"))
        self.assertTrue(_group_label_matches_option("Papier (Umschlag)", "material"))

    def test_unrelated_label_does_not_match(self) -> None:
        self.assertFalse(_group_label_matches_option("Shipping", "color"))
        self.assertFalse(_group_label_matches_option("", "color"))
        self.assertFalse(_group_label_matches_option("Auflage", ""))


class TestBuildInjectRulesExpandedVocabulary(unittest.TestCase):
    def test_emits_rules_for_color_binding_finishing_when_paths_present(self) -> None:
        request_template = {
            "properties": [
                {"id": "1", "name": "farbenU"},
                {"id": "2", "name": "verarbeitung"},
                {"id": "3", "name": "finishingO"},
            ]
        }
        proposal = {
            "trace_template_hints": [
                {
                    "candidate_mutations": [
                        {"option_key": "color", "path": "properties[name=farbenU].id"},
                        {"option_key": "binding", "path": "properties[name=verarbeitung].id"},
                        {"option_key": "finishing", "path": "properties[name=finishingO].id"},
                    ]
                }
            ]
        }
        spec = {"options": {}}
        option_catalog = [
            {
                "groupLabel": "Farbigkeit",
                "options": [
                    {"visibleLabel": "4/4 farbig", "backendHints": {"dataOptionId": "172"}},
                ],
            },
            {
                "groupLabel": "Verarbeitung",
                "options": [
                    {"visibleLabel": "Klammerheftung", "backendHints": {"dataOptionId": "1173"}},
                ],
            },
        ]

        rules = _build_inject_rules(request_template, proposal, spec, option_catalog)

        keys = {rule["input_key"] for rule in rules}
        self.assertIn("color", keys)
        self.assertIn("binding", keys)
        self.assertIn("finishing", keys)

        color_rule = next(rule for rule in rules if rule["input_key"] == "color")
        self.assertEqual(color_rule["path"], "properties[0].id")
        self.assertEqual(color_rule["value_map"].get("4/4 farbig"), "172")

    def test_does_not_emit_rule_without_resolvable_path(self) -> None:
        rules = _build_inject_rules({}, {"trace_template_hints": []}, {}, [])
        self.assertEqual(rules, [])
