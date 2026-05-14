from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.site_adapters import SaxoprintSiteAdapter


# A minimal captured POST modeled on the real
# .data/runs/saxoprint.de/unit_9f97d18.../network_traces.json[get-product-prices]
# fixture. quantity=44, format=9, material=8 (per value_maps/saxoprint.de.json).
_CAPTURED_POST = json.dumps(
    {
        "productGroupId": 305,
        "customerNumber": 0,
        "propertyConfiguration": [
            {"propertyId": 44, "value": 1000},   # captured quantity = 1000
            {"propertyId": 9, "value": 48},      # captured format   = DIN A4
            {"propertyId": 8, "value": 98},      # captured material = (brochure-only id)
            {"propertyId": 7, "value": 78},      # captured color    = 4/4-farbig
        ],
        "printRuns": [1000, 1100, 1200, 1250],
        "deliverySplitPrintRuns": [],
    }
)


def _matched(key: str, requested_value, prop_id: str, backend_id: str, label: str, *, confidence: str = "confirmed") -> dict:
    """Build a prevalidation matched row in the shape produced by
    cli._augment_with_sideload (matchType=sideload_only branch)."""
    return {
        "key": key,
        "requestedValue": requested_value,
        "groupLabel": label,
        "matchedLabel": label,
        "matchType": "sideload_only",
        "catalogOption": {
            "name": prop_id,
            "visibleLabel": label,
            "visibleValue": label,
            "backendHints": {
                "dataPropertyId": prop_id,
                "backendId": backend_id,
                "source": "sideload",
            },
        },
        "sideloadResolution": {
            "propertyId": prop_id,
            "backendId": backend_id,
            "matchedLabel": label,
            "confidence": confidence,
            "sourceLabel": label,
            "productScope": ["*"],
            "source": "sideload",
            "propertyNote": None,
        },
    }


class _FakeState:
    """Minimal stand-in for RunState carrying only what
    SaxoprintSiteAdapter.build_http_replay_candidates reads."""

    def __init__(
        self,
        *,
        prevalidation_matched: list[dict],
        post_data: str = _CAPTURED_POST,
        product_url: str = "https://www.saxoprint.de/broschueren/broschueren-drucken",
        extra_bootstrap: dict | None = None,
    ) -> None:
        self.target = type("T", (), {})()
        self.target.site_name = "saxoprint.de"
        self.target.product_url = product_url
        self.target.network_traces = [
            {
                "url": "https://api.saxoprint.de/product-configuration/get-product-prices",
                "method": "POST",
                "resource_type": "xhr",
                "request_content_type": "application/json",
                "response_content_type": "application/json; charset=utf-8",
                "post_data": post_data,
                "response_body_preview": "",
            }
        ]
        signals = {
            "option_prevalidation": {
                "matched": list(prevalidation_matched),
                "unmatched": [],
            },
            "request_templates": {"dropdownMaps": []},
        }
        if extra_bootstrap:
            signals.update(extra_bootstrap)
        self.target.bootstrap_signals = signals


def _candidate_property_value(candidate: dict, prop_id: int) -> int | None:
    body = json.loads(candidate["body_text"])
    for row in body.get("propertyConfiguration") or []:
        if int(row.get("propertyId") or 0) == prop_id:
            return int(row.get("value"))
    return None


class TestSideloadExtraction(unittest.TestCase):
    """`_extract_sideload_overrides` reads matched[*].sideloadResolution and
    emits one record per resolved row."""

    def test_extracts_resolved_rows(self) -> None:
        state = _FakeState(
            prevalidation_matched=[
                _matched("quantity", 250, "44", "250", "250"),
                _matched("format", "DIN A5 hoch", "9", "50", "DIN A5 (148 x 210 mm) hoch"),
                _matched("material", "100 g/m² Offsetpapier", "8", "95", "100 g/m² Offsetpapier"),
            ]
        )
        records = SaxoprintSiteAdapter._extract_sideload_overrides(state)
        self.assertEqual(len(records), 3)
        by_pid = {int(r["propertyId"]): r for r in records}
        self.assertEqual(by_pid[44]["backendId"], 250)
        self.assertEqual(by_pid[9]["backendId"], 50)
        self.assertEqual(by_pid[8]["backendId"], 95)
        self.assertEqual(by_pid[44]["confidence"], "confirmed")
        self.assertEqual(by_pid[44]["via"], "sideload")

    def test_skips_rows_without_resolution(self) -> None:
        state = _FakeState(
            prevalidation_matched=[
                {"key": "format", "requestedValue": "X", "matchType": "catalog_option"},
            ]
        )
        self.assertEqual(SaxoprintSiteAdapter._extract_sideload_overrides(state), [])

    def test_skips_malformed_ids(self) -> None:
        bad_row = _matched("quantity", 250, "not-a-number", "still-bad", "250")
        state = _FakeState(prevalidation_matched=[bad_row])
        self.assertEqual(SaxoprintSiteAdapter._extract_sideload_overrides(state), [])

    def test_skips_non_positive_ids(self) -> None:
        zero_row = _matched("quantity", 250, "0", "250", "250")
        neg_row = _matched("quantity", 250, "44", "-1", "250")
        state = _FakeState(prevalidation_matched=[zero_row, neg_row])
        self.assertEqual(SaxoprintSiteAdapter._extract_sideload_overrides(state), [])


class TestSideloadAppliedToPayload(unittest.TestCase):
    """End-to-end: sideload rows mutate the synthesized POST body so the
    propertyConfiguration carries the value-map-resolved backend IDs."""

    def test_sideload_rewrites_quantity_format_material(self) -> None:
        state = _FakeState(
            prevalidation_matched=[
                _matched("quantity", 250, "44", "250", "250"),
                _matched("format", "DIN A5 hoch", "9", "50", "DIN A5 (148 x 210 mm) hoch"),
                _matched("material", "100 g/m² Offsetpapier", "8", "95", "100 g/m² Offsetpapier"),
            ]
        )
        candidates = SaxoprintSiteAdapter().build_http_replay_candidates(
            state,
            effective_options={"quantity": 250, "format": "DIN A5 hoch", "material": "100 g/m² Offsetpapier"},
            raw_requested_options={"quantity": 250, "format": "DIN A5 hoch", "material": "100 g/m² Offsetpapier"},
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(_candidate_property_value(candidates[0], 44), 250)
        self.assertEqual(_candidate_property_value(candidates[0], 9), 50)
        self.assertEqual(_candidate_property_value(candidates[0], 8), 95)
        # Untouched property survives.
        self.assertEqual(_candidate_property_value(candidates[0], 7), 78)

    def test_sideload_emits_rollup(self) -> None:
        state = _FakeState(
            prevalidation_matched=[
                _matched("format", "DIN A5 hoch", "9", "50", "DIN A5 (148 x 210 mm) hoch"),
                _matched("material", "100 g/m² Offsetpapier", "8", "95", "100 g/m² Offsetpapier"),
            ]
        )
        candidates = SaxoprintSiteAdapter().build_http_replay_candidates(
            state,
            effective_options={"format": "DIN A5 hoch", "material": "100 g/m² Offsetpapier"},
            raw_requested_options={"format": "DIN A5 hoch", "material": "100 g/m² Offsetpapier"},
        )
        rollup = candidates[0]["request_template_applied"]["sideloadApplied"]
        self.assertEqual(len(rollup), 2)
        by_pid = {int(r["propertyId"]): r for r in rollup}
        self.assertIn(9, by_pid)
        self.assertIn(8, by_pid)
        self.assertEqual(by_pid[9]["backendId"], 50)
        self.assertEqual(by_pid[8]["backendId"], 95)
        self.assertEqual(by_pid[9]["via"], "sideload")

    def test_sideload_updates_carry_source_tag(self) -> None:
        state = _FakeState(
            prevalidation_matched=[
                _matched("format", "DIN A5 hoch", "9", "50", "DIN A5 (148 x 210 mm) hoch"),
            ]
        )
        candidates = SaxoprintSiteAdapter().build_http_replay_candidates(
            state,
            effective_options={"format": "DIN A5 hoch"},
            raw_requested_options={"format": "DIN A5 hoch"},
        )
        updates = candidates[0]["request_template_applied"]["updates"]
        sideload_updates = [u for u in updates if u.get("source") == "saxoprint_sideload"]
        self.assertEqual(len(sideload_updates), 1)
        self.assertEqual(sideload_updates[0]["propertyId"], 9)
        self.assertEqual(sideload_updates[0]["to"], 50)
        self.assertEqual(sideload_updates[0]["key"], "format")


class TestSideloadPrecedence(unittest.TestCase):
    """Precedence is: explicit property_*=N > label property_*="..." > sideload."""

    def test_explicit_property_override_beats_sideload(self) -> None:
        # Sideload resolution would set property_9=50; explicit override says 999.
        state = _FakeState(
            prevalidation_matched=[
                _matched("format", "DIN A5 hoch", "9", "50", "DIN A5 (148 x 210 mm) hoch"),
            ]
        )
        candidates = SaxoprintSiteAdapter().build_http_replay_candidates(
            state,
            effective_options={"property_9": 999},
            raw_requested_options={"property_9": 999},
        )
        self.assertEqual(_candidate_property_value(candidates[0], 9), 999)
        # The explicit override did NOT show up in sideloadApplied — only
        # rows whose precedence was actually won by sideload do.
        rollup = candidates[0]["request_template_applied"]["sideloadApplied"]
        self.assertEqual([r for r in rollup if int(r["propertyId"]) == 9], [])

    def test_sideload_applies_when_no_explicit_override(self) -> None:
        state = _FakeState(
            prevalidation_matched=[
                _matched("format", "DIN A5 hoch", "9", "50", "DIN A5 (148 x 210 mm) hoch"),
            ]
        )
        candidates = SaxoprintSiteAdapter().build_http_replay_candidates(
            state,
            effective_options={"format": "DIN A5 hoch"},
            raw_requested_options={"format": "DIN A5 hoch"},
        )
        self.assertEqual(_candidate_property_value(candidates[0], 9), 50)


class TestSideloadAgainstCapturedBootstrap(unittest.TestCase):
    """Smoke against the real captured artifact: given the three sideload
    resolutions for the failing Saxoprint summary, the synthesized POST
    flips quantity/format/material to the value-map-resolved backend IDs."""

    def test_against_real_capture(self) -> None:
        traces_path = (
            ROOT
            / ".data"
            / "runs"
            / "saxoprint.de"
            / "unit_9f97d182902013b3da2c6217c5c9e469dcf11d5f"
            / "network_traces.json"
        )
        if not traces_path.exists():
            self.skipTest("captured Saxoprint network_traces.json not available")

        captured = json.loads(traces_path.read_text(encoding="utf-8"))
        price_post = next(
            (
                row
                for row in captured
                if str(row.get("url", "")).endswith("/get-product-prices")
                and str(row.get("method", "")).upper() == "POST"
                and row.get("post_data")
            ),
            None,
        )
        if price_post is None:
            self.skipTest("no get-product-prices POST in captured trace")

        state = _FakeState(
            prevalidation_matched=[
                _matched("quantity", 250, "44", "250", "250"),
                _matched(
                    "format",
                    "DIN A5 (148 x 210 mm) hoch",
                    "9",
                    "50",
                    "DIN A5 (148 x 210 mm) hoch",
                ),
                _matched(
                    "material",
                    "100 g/m² Offsetpapier",
                    "8",
                    "95",
                    "100 g/m² Offsetpapier",
                ),
            ],
            post_data=str(price_post["post_data"]),
        )
        candidates = SaxoprintSiteAdapter().build_http_replay_candidates(
            state,
            effective_options={
                "quantity": 250,
                "format": "DIN A5 (148 x 210 mm) hoch",
                "material": "100 g/m² Offsetpapier",
            },
            raw_requested_options={
                "quantity": 250,
                "format": "DIN A5 (148 x 210 mm) hoch",
                "material": "100 g/m² Offsetpapier",
            },
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(_candidate_property_value(candidates[0], 44), 250)
        self.assertEqual(_candidate_property_value(candidates[0], 9), 50)
        self.assertEqual(_candidate_property_value(candidates[0], 8), 95)
        rollup = candidates[0]["request_template_applied"]["sideloadApplied"]
        self.assertEqual({int(r["propertyId"]) for r in rollup}, {9, 8})  # qty handled by setdefault path


if __name__ == "__main__":
    unittest.main()
