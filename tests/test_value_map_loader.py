from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.site_adapters import Print24SiteAdapter
from price_extractor.value_map_loader import (
    SCHEMA_VERSION_SUPPORTED,
    describe_value_map,
    load_value_map,
    resolve_value,
    value_matches_label,
)


def _write_map(dir_path: Path, site: str, payload: dict) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{site}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _saxoprint_min_payload() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION_SUPPORTED,
        "site": "saxoprint.de",
        "generatedAt": "2026-05-12T00:00:00Z",
        "captureMethod": "manual_extraction",
        "properties": [
            {
                "propertyId": "44",
                "canonicalKey": "quantity",
                "sourceLabel": "Auflage",
                "options": [
                    {"backendId": "25", "label": "25", "confidence": "confirmed"},
                    {"backendId": "839", "label": "1.000", "confidence": "confirmed"},
                ],
            },
            {
                "propertyId": "8",
                "canonicalKey": "material",
                "sourceLabel": "Material",
                "productScope": ["flyers"],
                "options": [
                    {"backendId": "89", "label": "80 g/m² Offsetpapier", "confidence": "confirmed"},
                    {"backendId": "93", "label": "90 g/m² Bilderdruckpapier matt", "confidence": "confirmed"},
                ],
            },
            {
                "propertyId": "6",
                "canonicalKey": "product_variant",
                "sourceLabel": "Ausführung",
                "options": [
                    {"backendId": "1", "label": "Flyer", "confidence": "partial"},
                    {"backendId": "2", "label": "Speziell", "confidence": "ui_only"},
                ],
            },
        ],
    }


class TestLoaderBasics(unittest.TestCase):
    def test_absent_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_value_map("nope.example", base_dir=Path(tmp)))

    def test_loads_and_attaches_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_map(tmp_path, "saxoprint.de", _saxoprint_min_payload())
            data = load_value_map("saxoprint.de", base_dir=tmp_path)
            self.assertIsNotNone(data)
            self.assertEqual(data["site"], "saxoprint.de")
            self.assertEqual(data["schemaVersion"], SCHEMA_VERSION_SUPPORTED)
            self.assertTrue(str(data["_path"]).endswith("saxoprint.de.json"))
            self.assertNotIn("_loadError", data)

    def test_unsupported_schema_version_flagged_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload = _saxoprint_min_payload()
            payload["schemaVersion"] = 999
            _write_map(tmp_path, "future.example", payload)
            data = load_value_map("future.example", base_dir=tmp_path)
            self.assertIsNotNone(data)
            self.assertIn("_loadError", data)
            self.assertIn("unsupported_schema_version", data["_loadError"])

    def test_malformed_json_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tmp_path.mkdir(parents=True, exist_ok=True)
            (tmp_path / "broken.example.json").write_text("{ not valid json", encoding="utf-8")
            data = load_value_map("broken.example", base_dir=tmp_path)
            self.assertIsNotNone(data)
            self.assertIn("_loadError", data)
            self.assertIn("json_parse_failed", data["_loadError"])


class TestMatcherParity(unittest.TestCase):
    """value_matches_label is duplicated from Print24SiteAdapter._value_matches_captured_label
    so the loader has no dependency on site_adapters. Pin equivalence."""

    cases = [
        ("130 g/m² Recycling-Bilderdruckpapier", "130 g/m² Recycling-Bilderdruckpapier", True),
        ("DIN A6", "105 x 148 mm DIN A6", True),
        ("10", "10 Stück", True),
        ("115 g/m² Bilderdruckpapier", "130 g/m² Recycling-Bilderdruckpapier", False),
        ("148 x 210 mm DIN A5", "105 x 148 mm DIN A6", False),
        ("250", "10 Stück", False),
        ("130 g/m2 Recycling-Bilderdruckpapier", "130 g/m² Recycling-Bilderdruckpapier", True),
        ("", "anything", False),
        ("anything", "", False),
        (None, "anything", False),
        ("din a6", "105 x 148 mm DIN A6", True),
    ]

    def test_parity_with_print24_matcher(self) -> None:
        for req, cap, _expected in self.cases:
            with self.subTest(req=req, cap=cap):
                self.assertEqual(
                    value_matches_label(req, cap),
                    Print24SiteAdapter._value_matches_captured_label(req, cap),
                )

    def test_expected_outcomes(self) -> None:
        for req, cap, expected in self.cases:
            with self.subTest(req=req, cap=cap, expected=expected):
                self.assertEqual(value_matches_label(req, cap), expected)


class TestResolveValueOutcomes(unittest.TestCase):
    def setUp(self) -> None:
        self.value_map = _saxoprint_min_payload()

    def test_no_map_short_circuits(self) -> None:
        self.assertEqual(resolve_value(None, "quantity", "250")["status"], "no_map")

    def test_no_property_for_missing_canonical_key(self) -> None:
        outcome = resolve_value(self.value_map, "binding", "Klebebindung")
        self.assertEqual(outcome["status"], "no_property")

    def test_resolved_exact_quantity(self) -> None:
        outcome = resolve_value(self.value_map, "quantity", "25")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "25")
        self.assertEqual(outcome["propertyId"], "44")
        self.assertEqual(outcome["confidence"], "confirmed")

    def test_resolved_substring_material(self) -> None:
        outcome = resolve_value(self.value_map, "material", "80 g/m² Offsetpapier")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "89")

    def test_resolved_with_ascii_m2(self) -> None:
        outcome = resolve_value(self.value_map, "material", "80 g/m2 Offsetpapier")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "89")

    def test_no_match_when_label_not_present(self) -> None:
        outcome = resolve_value(self.value_map, "quantity", "12345")
        self.assertEqual(outcome["status"], "no_match")
        self.assertIn("25", outcome["candidateLabels"])

    def test_ui_only_blocked_by_default(self) -> None:
        outcome = resolve_value(self.value_map, "product_variant", "Speziell")
        self.assertEqual(outcome["status"], "no_match")

    def test_ui_only_resolves_when_opt_in(self) -> None:
        outcome = resolve_value(
            self.value_map, "product_variant", "Speziell", allow_ui_only=True
        )
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "2")
        self.assertEqual(outcome["confidence"], "ui_only")

    def test_partial_confidence_accepted_by_default(self) -> None:
        outcome = resolve_value(self.value_map, "product_variant", "Flyer")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["confidence"], "partial")

    def test_ambiguous_when_multiple_options_match(self) -> None:
        payload = _saxoprint_min_payload()
        payload["properties"].append(
            {
                "propertyId": "99",
                "canonicalKey": "size",
                "sourceLabel": "Size",
                "options": [
                    {"backendId": "X", "label": "small", "confidence": "confirmed"},
                    {"backendId": "Y", "label": "small (variant)", "confidence": "confirmed"},
                ],
            }
        )
        outcome = resolve_value(payload, "size", "small")
        self.assertEqual(outcome["status"], "ambiguous")
        self.assertEqual(len(outcome["candidates"]), 2)

    def test_key_collision_when_multiple_properties_match(self) -> None:
        payload = _saxoprint_min_payload()
        payload["properties"].append(
            {
                "propertyId": "8b",
                "canonicalKey": "material",
                "sourceLabel": "Material (cover)",
                "options": [
                    {"backendId": "8901", "label": "80 g/m² Offsetpapier", "confidence": "confirmed"},
                ],
            }
        )
        outcome = resolve_value(payload, "material", "80 g/m² Offsetpapier")
        self.assertEqual(outcome["status"], "key_collision")
        self.assertEqual(len(outcome["candidates"]), 2)
        property_ids = {c["propertyId"] for c in outcome["candidates"]}
        self.assertSetEqual(property_ids, {"8", "8b"})

    def test_product_scope_passthrough_advisory(self) -> None:
        # productScope is currently advisory (logged, not gating).
        outcome = resolve_value(self.value_map, "material", "80 g/m² Offsetpapier")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["productScope"], ["flyers"])

    def test_property_note_surfaced(self) -> None:
        payload = _saxoprint_min_payload()
        payload["properties"][0]["note"] = "verified against trace foo"
        outcome = resolve_value(payload, "quantity", "25")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["propertyNote"], "verified against trace foo")


class TestDescribeValueMap(unittest.TestCase):
    def test_describes_loaded_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _write_map(tmp_path, "saxoprint.de", _saxoprint_min_payload())
            data = load_value_map("saxoprint.de", base_dir=tmp_path)
            summary = describe_value_map(data)
            self.assertTrue(summary["loaded"])
            self.assertEqual(summary["site"], "saxoprint.de")
            self.assertEqual(summary["propertyCount"], 3)
            self.assertIn("quantity", summary["canonicalKeys"])
            self.assertIn("material", summary["canonicalKeys"])
            self.assertIn("product_variant", summary["canonicalKeys"])

    def test_describes_absent_map(self) -> None:
        self.assertEqual(describe_value_map(None), {"loaded": False})


class TestRepoSaxoprintJson(unittest.TestCase):
    """End-to-end: load the checked-in value_maps/saxoprint.de.json and resolve
    a representative quantity / material / format trio."""

    def setUp(self) -> None:
        self.value_map = load_value_map("saxoprint.de", base_dir=ROOT / "value_maps")
        self.assertIsNotNone(self.value_map)
        self.assertNotIn("_loadError", self.value_map)

    def test_quantity_resolves(self) -> None:
        outcome = resolve_value(self.value_map, "quantity", "1.000")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "1000")
        self.assertEqual(outcome["propertyId"], "44")

    def test_quantity_resolves_via_substring(self) -> None:
        outcome = resolve_value(self.value_map, "quantity", "250")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "250")

    def test_material_resolves_with_ascii_m2(self) -> None:
        outcome = resolve_value(self.value_map, "material", "130 g/m2 Bilderdruckpapier glanz FSC")
        self.assertEqual(outcome["status"], "resolved")
        # Substring match: '130 g/m2 Bilderdruckpapier glanz FSC' is contained in the
        # NFKD-normalized 'g/m² ... FSC®' label.
        self.assertEqual(outcome["backendId"], "1630")

    def test_format_resolves(self) -> None:
        outcome = resolve_value(self.value_map, "format", "DIN A5 (148 x 210 mm) hoch")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "50")

    def test_color_no_match_when_label_unknown(self) -> None:
        outcome = resolve_value(self.value_map, "color", "no-such-color")
        self.assertEqual(outcome["status"], "no_match")


class TestRepoPrint24Json(unittest.TestCase):
    def setUp(self) -> None:
        self.value_map = load_value_map("print24.com", base_dir=ROOT / "value_maps")
        self.assertIsNotNone(self.value_map)

    def test_format_resolves(self) -> None:
        outcome = resolve_value(self.value_map, "format", "DIN A5")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "268")
        self.assertEqual(outcome["propertyId"], "format")

    def test_quantity_resolves(self) -> None:
        outcome = resolve_value(self.value_map, "quantity", "250")
        self.assertEqual(outcome["status"], "resolved")
        self.assertEqual(outcome["backendId"], "438")


class TestPrint24AdapterSideloadPrecedence(unittest.TestCase):
    """Integration: Print24SiteAdapter precedence is catalog_applied > sideload >
    hardcoded. catalog_skipped does NOT block sideload."""

    def _state(self, *, post_data, matched):
        # mirrors test_print24_site_adapter_catalog._FakeState shape
        state = type("State", (), {})()
        state.target = type("T", (), {})()
        state.target.site_name = "print24.com"
        state.target.product_url = "https://print24.com/p"
        state.target.network_traces = [{
            "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
            "method": "POST",
            "resource_type": "xhr",
            "request_content_type": "application/json",
            "response_content_type": "application/json",
            "response_body_preview": "{}",
            "post_data": post_data,
        }]
        state.target.bootstrap_signals = {
            "option_prevalidation": {"matched": matched, "unmatched": []},
        }
        return state

    def _template_body(self, properties):
        return json.dumps({
            "properties": properties,
            "product_alias_id": 388,
        })

    def test_sideload_fills_when_catalog_does_not_cover(self) -> None:
        state = self._state(
            post_data=self._template_body([
                {"id": "0000", "name": "format"},
                {"id": "0000", "name": "quantity"},
            ]),
            matched=[
                {
                    "key": "format",
                    "requestedValue": "DIN A5",
                    "matchType": "sideload_only",
                    "groupLabel": "Format",
                    "matchedLabel": "DIN A5",
                    "catalogOption": {"name": "format", "visibleLabel": "DIN A5", "backendHints": {}},
                    "sideloadResolution": {
                        "propertyId": "format",
                        "backendId": "268",
                        "matchedLabel": "DIN A5",
                        "confidence": "confirmed",
                        "productScope": ["brochures"],
                        "source": "sideload",
                    },
                },
            ],
        )
        adapter = Print24SiteAdapter()
        opts = {"format": "DIN A5"}
        cands = adapter.build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        sideload_keys = {entry["key"] for entry in rta.get("sideloadApplied", [])}
        self.assertEqual(sideload_keys, {"format"})
        body = json.loads(cands[0]["body_text"])
        props_by_name = {p["name"]: p["id"] for p in body["properties"]}
        self.assertEqual(props_by_name["format"], "268")

    def test_catalog_applied_takes_precedence_over_sideload(self) -> None:
        # Same row carries BOTH a clean catalog match (prop_id=222) AND a
        # sideload resolution (prop_id=999, deliberately different). Adapter
        # must use 222, not 999 — catalog wins.
        state = self._state(
            post_data=self._template_body([{"id": "0000", "name": "format"}]),
            matched=[
                {
                    "key": "format",
                    "requestedValue": "DIN A6",
                    "matchType": "catalog_option",
                    "groupLabel": "format",
                    "matchedLabel": "105 x 148 mm DIN A6",
                    "catalogOption": {
                        "name": "format",
                        "visibleLabel": "105 x 148 mm DIN A6",
                        "backendHints": {"dataPropertyId": "222", "boxName": "format"},
                    },
                    "sideloadResolution": {
                        "propertyId": "format",
                        "backendId": "999",
                        "matchedLabel": "DIN A6",
                        "confidence": "confirmed",
                        "productScope": ["brochures"],
                        "source": "sideload",
                    },
                },
            ],
        )
        adapter = Print24SiteAdapter()
        opts = {"format": "DIN A6"}
        cands = adapter.build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        # catalog applied, sideload did NOT
        catalog_keys = {entry["key"] for entry in rta["catalogApplied"]}
        self.assertEqual(catalog_keys, {"format"})
        self.assertEqual(rta.get("sideloadApplied", []), [])
        body = json.loads(cands[0]["body_text"])
        self.assertEqual(body["properties"][0]["id"], "222")

    def test_sideload_overrides_catalog_skipped(self) -> None:
        # Catalog had the group (papierI) but couldn't resolve the requested
        # value (different paper weight). Sideload knows the prop_id for the
        # requested label. Sideload should fill — otherwise the user's request
        # silently no-ops even though we have the data.
        state = self._state(
            post_data=self._template_body([{"id": "9413", "name": "papierI"}]),
            matched=[
                {
                    "key": "material",
                    "requestedValue": "115 g/m² Bilderdruckpapier",
                    "matchType": "catalog_option",
                    "groupLabel": "Material",
                    "matchedLabel": "130 g/m² Recycling-Bilderdruckpapier",
                    "catalogOption": {
                        "name": "papierI",
                        "visibleLabel": "130 g/m² Recycling-Bilderdruckpapier",
                        "backendHints": {"dataPropertyId": "9413", "boxName": "papierI"},
                    },
                    "sideloadResolution": {
                        "propertyId": "papierI",
                        "backendId": "7777",
                        "matchedLabel": "115 g/m² Bilderdruckpapier",
                        "confidence": "confirmed",
                        "productScope": ["brochures"],
                        "source": "sideload",
                    },
                },
            ],
        )
        adapter = Print24SiteAdapter()
        opts = {"material": "115 g/m² Bilderdruckpapier"}
        cands = adapter.build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        sideload_keys = {entry["key"] for entry in rta.get("sideloadApplied", [])}
        self.assertEqual(sideload_keys, {"material"})
        # catalog_skipped still surfaces as a diagnostic
        self.assertEqual(len(rta["skipped"]), 1)
        self.assertEqual(rta["skipped"][0]["reason"], "prop_id_unknown_for_value")
        # Payload was mutated to the sideload's prop_id
        body = json.loads(cands[0]["body_text"])
        self.assertEqual(body["properties"][0]["id"], "7777")


if __name__ == "__main__":
    unittest.main()
