from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.print24_catalog import harvest_print24_catalog_from_traces


def _product_details_trace(prop_details_blob: list[dict]) -> dict:
    return {
        "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
        "method": "POST",
        "status": 200,
        "response_content_type": "application/json",
        "response_body_preview": json.dumps({"prop_details": prop_details_blob}),
    }


class TestHarvestPrint24Catalog(unittest.TestCase):
    """Per print24_feasibility_notes.md §3.3, productDetails returns the
    currently-selected option per group with box_name (canonical key),
    prop_id (the integer ID used in POST body `properties[].id`), and
    prop_translated (human label)."""

    def test_extracts_one_row_per_prop_details_entry(self) -> None:
        traces = [
            _product_details_trace([
                {"box_name": "format", "box_name_title": "Format", "prop_id": "222", "prop_translated": "105 x 148 mm DIN A6", "prop_name": "105 x 148 mm DIN-A-6"},
                {"box_name": "quantity", "box_name_title": "Menge", "prop_id": "339", "prop_translated": "10 Stück", "prop_name": "quantity 10"},
                {"box_name": "papierI", "box_name_title": "Inhalt", "prop_id": "9413", "prop_translated": "130 g/m² Recycling-Bilderdruckpapier", "prop_name": "130 g/qm circlesilk_white"},
            ])
        ]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(len(catalog), 3)
        by_box = {(g.get("sourceHints") or {}).get("boxName"): g for g in catalog}

        format_group = by_box["format"]
        self.assertEqual(format_group["groupLabel"], "Format")
        opt = format_group["options"][0]
        self.assertEqual(opt["name"], "format")
        self.assertEqual(opt["visibleLabel"], "105 x 148 mm DIN A6")
        self.assertEqual(opt["backendHints"]["dataPropertyId"], "222")
        self.assertTrue(opt["selected"])

        qty_group = by_box["quantity"]
        self.assertEqual(qty_group["groupLabel"], "Menge")
        self.assertEqual(qty_group["options"][0]["backendHints"]["dataPropertyId"], "339")

    def test_falls_back_to_prop_name_when_translated_missing(self) -> None:
        traces = [_product_details_trace([
            {"box_name": "x", "prop_id": "1", "prop_name": "fallback_name"},
        ])]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(catalog[0]["options"][0]["visibleLabel"], "fallback_name")

    def test_skips_entries_missing_box_name_or_prop_id(self) -> None:
        traces = [_product_details_trace([
            {"prop_id": "1", "prop_translated": "no box_name"},
            {"box_name": "x", "prop_translated": "no prop_id"},
            {"box_name": "good", "prop_id": "42", "prop_translated": "ok"},
        ])]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["options"][0]["backendHints"]["dataPropertyId"], "42")

    def test_skips_traces_without_prop_details(self) -> None:
        traces = [{
            "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
            "method": "POST",
            "status": 200,
            "response_content_type": "application/json",
            "response_body_preview": json.dumps({"something_else": []}),
        }]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(catalog, [])

    def test_skips_non_post_traces(self) -> None:
        traces = [{
            "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
            "method": "GET",
            "status": 200,
            "response_content_type": "application/json",
            "response_body_preview": json.dumps({"prop_details": [{"box_name": "x", "prop_id": "1"}]}),
        }]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(catalog, [])

    def test_skips_non_json_responses(self) -> None:
        traces = [{
            "url": "https://print24.com/api/de/itemmaster/calculation/productDetails/",
            "method": "POST",
            "status": 200,
            "response_content_type": "text/html",
            "response_body_preview": "<html>not json</html>",
        }]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(catalog, [])

    def test_prefers_later_trace_when_multiple_captured(self) -> None:
        traces = [
            _product_details_trace([{"box_name": "format", "prop_id": "111", "prop_translated": "early"}]),
            _product_details_trace([{"box_name": "format", "prop_id": "222", "prop_translated": "late"}]),
        ]
        catalog = harvest_print24_catalog_from_traces(traces)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["options"][0]["backendHints"]["dataPropertyId"], "222")


class TestPrint24CatalogEndToEnd(unittest.TestCase):
    def test_prevalidation_matches_print24_options_against_captured_bootstrap(self) -> None:
        from price_extractor.cli import _prevalidate_requested_options

        fixture = ROOT / ".data" / "runs" / "print24.com" / "unit_4f793d86afafd1eb23c3ae14f85781fec53a8198" / "bootstrap.json"
        if not fixture.exists():
            self.skipTest(f"fixture not present: {fixture}")
        bootstrap = json.loads(fixture.read_text(encoding="utf-8"))

        result = _prevalidate_requested_options(
            {
                "quantity": 10,
                "format": "105 x 148 mm DIN A6",
                "material": "130 g/m² Recycling-Bilderdruckpapier",
            },
            bootstrap,
        )
        self.assertTrue(result["valid"], msg=str(result))
        self.assertEqual(len(result["matched"]), 3)

        by_key = {m["key"]: m for m in result["matched"]}
        # Each matched row carries the catalog's prop_id under backendHints.dataPropertyId.
        for key, expected_prop_id in [("quantity", "339"), ("format", "222"), ("material", "9413")]:
            row = by_key[key]
            backend_hints = (row.get("catalogOption") or {}).get("backendHints") or {}
            self.assertEqual(
                backend_hints.get("dataPropertyId"),
                expected_prop_id,
                msg=f"key={key}: row={row}",
            )

        # quantity uses the catalog_only matchType (no quantity_signal manual input).
        self.assertEqual(by_key["quantity"]["matchType"], "catalog_only")


if __name__ == "__main__":
    unittest.main()
