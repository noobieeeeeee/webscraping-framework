from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.site_adapters import Print24SiteAdapter


class TestValueMatchesCapturedLabel(unittest.TestCase):
    """Strict matcher used by Print24SiteAdapter to avoid silently writing a
    captured prop_id when the user requested a different option in the same
    group (e.g., 115 g/m² → captured 130 g/m²)."""

    def _match(self, req, cap):
        return Print24SiteAdapter._value_matches_captured_label(req, cap)

    def test_exact_match(self) -> None:
        self.assertTrue(self._match("130 g/m² Recycling-Bilderdruckpapier", "130 g/m² Recycling-Bilderdruckpapier"))

    def test_substring_match(self) -> None:
        self.assertTrue(self._match("DIN A6", "105 x 148 mm DIN A6"))
        self.assertTrue(self._match("A6", "105 x 148 mm DIN A6"))
        self.assertTrue(self._match("10", "10 Stück"))

    def test_different_paper_weight_rejects(self) -> None:
        self.assertFalse(self._match("115 g/m² Bilderdruckpapier", "130 g/m² Recycling-Bilderdruckpapier"))

    def test_different_format_rejects(self) -> None:
        # Both have "148" as a dimension but the formats are clearly different
        self.assertFalse(self._match("148 x 210 mm DIN A5", "105 x 148 mm DIN A6"))
        # And the short forms differ too
        self.assertFalse(self._match("A5", "105 x 148 mm DIN A6"))
        self.assertFalse(self._match("DIN A5", "105 x 148 mm DIN A6"))

    def test_different_quantity_rejects(self) -> None:
        self.assertFalse(self._match("250", "10 Stück"))
        self.assertFalse(self._match("750", "10 Stück"))

    def test_nfkd_collapses_superscript(self) -> None:
        # User typed 'm2' (ASCII digit) against captured 'm²' (superscript-2).
        # NFKD decomposes ² → 2 so the substring check succeeds.
        self.assertTrue(self._match("130 g/m2 Recycling-Bilderdruckpapier", "130 g/m² Recycling-Bilderdruckpapier"))

    def test_empty_values_reject(self) -> None:
        self.assertFalse(self._match("", "anything"))
        self.assertFalse(self._match("anything", ""))
        self.assertFalse(self._match(None, "anything"))

    def test_case_insensitive(self) -> None:
        self.assertTrue(self._match("din a6", "105 x 148 mm DIN A6"))
        self.assertTrue(self._match("RECYCLING-BILDERDRUCKPAPIER", "130 g/m² Recycling-Bilderdruckpapier"))


def _matched_row(key: str, box_name: str, prop_id: str, visible_label: str) -> dict:
    return {
        "key": key,
        "requestedValue": visible_label,
        "matchType": "catalog_option",
        "groupLabel": box_name,
        "matchedLabel": visible_label,
        "catalogOption": {
            "visibleLabel": visible_label,
            "visibleValue": visible_label,
            "selected": True,
            "controlTag": "print24_property",
            "controlType": "print24_property",
            "name": box_name,
            "backendHints": {"dataPropertyId": prop_id, "boxName": box_name},
        },
    }


class _FakeState:
    """Minimal stand-in for RunState exposing only what the synthesis adapter
    reads. Keeps tests isolated from RunState construction."""

    def __init__(self, *, post_data, prevalidation_matched, product_url="https://print24.com/p"):
        self.target = type("T", (), {})()
        self.target.site_name = "print24.com"
        self.target.product_url = product_url
        self.target.network_traces = [{
            "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
            "method": "POST",
            "resource_type": "xhr",
            "request_content_type": "application/json",
            "response_content_type": "application/json",
            "response_body_preview": "{}",
            "post_data": post_data,
        }]
        self.target.bootstrap_signals = {
            "option_prevalidation": {"matched": prevalidation_matched, "unmatched": []},
        }


def _template_body(properties: list[dict]) -> str:
    return json.dumps({
        "item_group_id": 2,
        "properties": properties,
        "product_alias_id": 388,
        "delivery_country_code": "DE",
        "premium_file_check": 0,
    })


class TestPrint24SiteAdapterConsumesCatalog(unittest.TestCase):
    def _adapter(self) -> Print24SiteAdapter:
        return Print24SiteAdapter()

    def test_catalog_match_applies_prop_id(self) -> None:
        state = _FakeState(
            post_data=_template_body([
                {"id": "9999", "name": "format"},
                {"id": "0000", "name": "quantity"},
            ]),
            prevalidation_matched=[
                _matched_row("format", "format", "222", "105 x 148 mm DIN A6"),
                _matched_row("quantity", "quantity", "339", "10 Stück"),
            ],
        )
        opts = {"format": "DIN A6", "quantity": "10"}
        cands = self._adapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        applied_keys = {c["key"] for c in rta["catalogApplied"]}
        self.assertEqual(applied_keys, {"format", "quantity"})
        # Payload's format.id should now be 222 (from 9999), quantity.id = 339.
        body = json.loads(cands[0]["body_text"])
        props_by_name = {p["name"]: p["id"] for p in body["properties"]}
        self.assertEqual(props_by_name["format"], "222")
        self.assertEqual(props_by_name["quantity"], "339")

    def test_value_mismatch_skips_with_prop_id_unknown(self) -> None:
        state = _FakeState(
            post_data=_template_body([{"id": "9413", "name": "papierI"}]),
            prevalidation_matched=[
                _matched_row("material", "papierI", "9413", "130 g/m² Recycling-Bilderdruckpapier"),
            ],
        )
        opts = {"material": "115 g/m² Bilderdruckpapier"}
        cands = self._adapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        # No properties were updated.
        self.assertEqual(rta["propertiesUpdated"], [])
        # Catalog skipped with precise reason and captured context.
        self.assertEqual(len(rta["skipped"]), 1)
        skip = rta["skipped"][0]
        self.assertEqual(skip["reason"], "prop_id_unknown_for_value")
        self.assertEqual(skip["boxName"], "papierI")
        self.assertEqual(skip["capturedPropId"], "9413")
        self.assertIn("130", skip["capturedValue"])
        # Payload should NOT have been mutated to use the captured 9413 for a
        # value the user did NOT request — that's the silent-wrong-price bug
        # this whole path exists to prevent.
        body = json.loads(cands[0]["body_text"])
        self.assertEqual(body["properties"][0]["id"], "9413")  # template default unchanged

    def test_hardcoded_fallback_when_catalog_missing_for_key(self) -> None:
        state = _FakeState(
            post_data=_template_body([
                {"id": "0000", "name": "format"},
                {"id": "0000", "name": "quantity"},
            ]),
            prevalidation_matched=[],  # empty catalog
        )
        opts = {"format": "A5", "quantity": "250"}
        cands = self._adapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        hardcoded_keys = {h["key"] for h in rta["hardcodedApplied"]}
        self.assertEqual(hardcoded_keys, {"format", "quantity"})
        body = json.loads(cands[0]["body_text"])
        props = {p["name"]: p["id"] for p in body["properties"]}
        self.assertEqual(props["format"], "268")
        self.assertEqual(props["quantity"], "438")

    def test_catalog_skipped_does_not_fall_through_to_hardcoded(self) -> None:
        """If the catalog has the key but skipped it (value mismatch),
        do NOT silently substitute a hardcoded mapping — the user must see
        the precise prop_id_unknown_for_value diagnostic."""
        state = _FakeState(
            post_data=_template_body([{"id": "9999", "name": "quantity"}]),
            prevalidation_matched=[
                _matched_row("quantity", "quantity", "339", "10 Stück"),
            ],
        )
        opts = {"quantity": "250"}  # 250 has a hardcoded mapping (438), but catalog disagrees
        cands = self._adapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        rta = cands[0]["request_template_applied"]
        self.assertEqual(rta["hardcodedApplied"], [])
        self.assertEqual(len(rta["skipped"]), 1)
        self.assertEqual(rta["skipped"][0]["reason"], "prop_id_unknown_for_value")

    def test_unsupported_inputs_surfaces_keys_with_no_catalog_or_hardcoded(self) -> None:
        state = _FakeState(
            post_data=_template_body([{"id": "0000", "name": "format"}]),
            prevalidation_matched=[],
        )
        opts = {"format": "A5", "binding": "softcover"}
        cands = self._adapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        rta = cands[0]["request_template_applied"]
        unsupported_keys = {u["inputKey"] for u in rta["unsupportedInputs"]}
        self.assertEqual(unsupported_keys, {"binding"})

    def test_returns_no_candidate_when_user_matches_captured_exactly(self) -> None:
        """If the user's request equals the captured config, propertiesUpdated
        is empty and nothing was skipped/unsupported — return [] so the
        captured trace replays as-is."""
        state = _FakeState(
            post_data=_template_body([{"id": "222", "name": "format"}]),
            prevalidation_matched=[
                _matched_row("format", "format", "222", "105 x 148 mm DIN A6"),
            ],
        )
        opts = {"format": "DIN A6"}
        cands = self._adapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts
        )
        self.assertEqual(cands, [])


class TestPrint24SiteAdapterEndToEndAgainstRealBootstrap(unittest.TestCase):
    """Smoke against `unit_4f793d8.../bootstrap.json`."""

    def _drive(self, opts):
        from price_extractor.cli import _prevalidate_requested_options
        from price_extractor.models import RunState, TargetInput

        path = ROOT / ".data" / "runs" / "print24.com" / "unit_4f793d86afafd1eb23c3ae14f85781fec53a8198" / "bootstrap.json"
        if not path.exists():
            self.skipTest(f"fixture not present: {path}")
        bootstrap = json.loads(path.read_text(encoding="utf-8"))
        prev = _prevalidate_requested_options(opts, bootstrap)
        target = TargetInput(
            site_name="print24.com",
            product_url="https://print24.com/de/druckprodukte/broschueren/broschueren-klammerheftung-greenline",
            product_type="brochure",
            expected_currency="EUR",
            options=opts,
            network_traces=list(bootstrap.get("network_traces") or []),
            bootstrap_signals={**bootstrap, "option_prevalidation": prev},
        )
        state = RunState(target=target)
        return Print24SiteAdapter().build_http_replay_candidates(
            state, effective_options=opts, raw_requested_options=opts,
        )

    def test_paper_change_skips_material_loudly(self) -> None:
        cands = self._drive({
            "quantity": 10,
            "format": "105 x 148 mm DIN A6",
            "material": "115 g/m² Bilderdruckpapier",
        })
        self.assertEqual(len(cands), 1)
        rta = cands[0]["request_template_applied"]
        skip_keys = {s["key"] for s in rta["skipped"]}
        self.assertIn("material", skip_keys)
        # And the diagnostic carries the captured value (130 g/m²) so the user
        # knows exactly what was actually in the request.
        material_skip = next(s for s in rta["skipped"] if s["key"] == "material")
        self.assertIn("130", material_skip["capturedValue"])

    def test_nfkd_lets_user_type_ascii_for_unicode_capture(self) -> None:
        # Captured label has "g/m²" (Unicode); user types "g/m2" (ASCII).
        # NFKD normalization in _value_matches_captured_label makes them match.
        cands = self._drive({
            "quantity": 10,
            "format": "DIN A6",
            "material": "130 g/m2 Recycling-Bilderdruckpapier",
        })
        # All three match captured → no synthesis needed → []
        self.assertEqual(cands, [])


if __name__ == "__main__":
    unittest.main()
