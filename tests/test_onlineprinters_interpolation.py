from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.form_field_inference import quantity_tile_signals
from price_extractor.site_adapters import OnlineprintersSiteAdapter


def _qty_option(value: str, varindex: str = "") -> dict:
    return {
        "visibleLabel": value,
        "visibleValue": value,
        "name": "input_var_PBRA444_3_1",
        "backendHints": {"dataVarindex": varindex} if varindex else {},
    }


class TestQuantityTileSignals(unittest.TestCase):
    def test_extracts_tile_presets_and_interpolation_code(self) -> None:
        catalog = [
            {"groupLabel": "10", "options": [_qty_option("10", "PBRA444.135.0810")]},
            {"groupLabel": "100", "options": [_qty_option("100", "PBRA444.135.08100")]},
            {"groupLabel": "1000", "options": [_qty_option("1000", "PBRA444.135.081000")]},
            {"groupLabel": "19000", "options": [_qty_option("19000", "PBRA444.135.0819000")]},
            {"groupLabel": "Höhere Auflage", "options": [_qty_option("Interpolation", "PBRA444.135.0820000")]},
        ]
        signals = quantity_tile_signals(catalog, "input_var_PBRA444_3_1")
        self.assertEqual(signals["tilePresets"], ["10", "100", "1000", "19000"])
        self.assertEqual(signals["maxTilePreset"], 19000)
        self.assertTrue(signals["supportsInterpolation"])
        self.assertEqual(signals["interpolationVarindex"], "PBRA444.135.0820000")
        self.assertEqual(signals["tileVarindexByValue"]["1000"], "PBRA444.135.081000")

    def test_ignores_options_for_other_fields(self) -> None:
        catalog = [
            {"groupLabel": "x", "options": [
                {"visibleValue": "100", "name": "input_var_PBRA444_3_1", "backendHints": {"dataVarindex": "Q.100"}},
                {"visibleValue": "200", "name": "different_field", "backendHints": {"dataVarindex": "X.200"}},
            ]},
        ]
        signals = quantity_tile_signals(catalog, "input_var_PBRA444_3_1")
        self.assertEqual(signals["tilePresets"], ["100"])

    def test_empty_catalog_returns_empty_signals(self) -> None:
        signals = quantity_tile_signals([], "x")
        self.assertEqual(signals["tilePresets"], [])
        self.assertFalse(signals["supportsInterpolation"])
        self.assertEqual(signals["maxTilePreset"], 0)


class TestApplyInterpolationQuantity(unittest.TestCase):
    """Verify the form-pair manipulation matches onlineprinters_request_modification.md §5B:
    modify input_qty_1 in-place at the position immediately after the qty field,
    preserve all other pairs and their order."""

    def test_sets_qty_field_to_Interpolation_and_updates_following_input_qty_1(self) -> None:
        pairs = [
            ("input_var_PBRA444_1_1", "130 g/m² Bilderdruckpapier"),
            ("input_qty_1", "1"),
            ("input_var_PBRA444_2_1", "8-seitig"),
            ("input_qty_1", "1"),
            ("input_var_PBRA444_3_1", "1000"),
            ("input_qty_1", "1"),
            ("input_qty_1", "1"),
            ("input_var_ZBRA401U_1_2", "kein Umschlag"),
        ]
        new_pairs, did, idx = OnlineprintersSiteAdapter._apply_interpolation_quantity(
            pairs,
            qty_field_name="input_var_PBRA444_3_1",
            typed_value="25000",
        )
        self.assertTrue(did)
        self.assertEqual(idx, 4)
        # Length preserved.
        self.assertEqual(len(new_pairs), len(pairs))
        # qty field switched to literal "Interpolation".
        self.assertEqual(new_pairs[4], ("input_var_PBRA444_3_1", "Interpolation"))
        # input_qty_1 immediately after qty field carries the typed value.
        self.assertEqual(new_pairs[5], ("input_qty_1", "25000"))
        # The trailing input_qty_1 (one slot later) is NOT modified — it stays "1".
        self.assertEqual(new_pairs[6], ("input_qty_1", "1"))
        # All preceding fields untouched.
        self.assertEqual(new_pairs[0], pairs[0])
        self.assertEqual(new_pairs[1], pairs[1])
        self.assertEqual(new_pairs[2], pairs[2])
        self.assertEqual(new_pairs[3], pairs[3])
        # Following field after the qty1 pair is also untouched.
        self.assertEqual(new_pairs[7], pairs[7])

    def test_inserts_input_qty_1_when_none_exists_after_qty_field(self) -> None:
        pairs = [
            ("input_var_PBRA444_3_1", "1000"),
            ("input_var_ZBRA401U_1_2", "x"),
        ]
        new_pairs, did, idx = OnlineprintersSiteAdapter._apply_interpolation_quantity(
            pairs,
            qty_field_name="input_var_PBRA444_3_1",
            typed_value="25000",
        )
        self.assertTrue(did)
        self.assertEqual(new_pairs[0], ("input_var_PBRA444_3_1", "Interpolation"))
        self.assertEqual(new_pairs[1], ("input_qty_1", "25000"))
        self.assertEqual(new_pairs[2], pairs[1])

    def test_returns_unchanged_when_qty_field_not_in_pairs(self) -> None:
        pairs = [("a", "1"), ("b", "2")]
        new_pairs, did, idx = OnlineprintersSiteAdapter._apply_interpolation_quantity(
            pairs, qty_field_name="missing", typed_value="100"
        )
        self.assertFalse(did)
        self.assertEqual(idx, -1)
        self.assertEqual(new_pairs, pairs)

    def test_returns_unchanged_when_typed_value_empty(self) -> None:
        pairs = [("input_var_PBRA444_3_1", "1000"), ("input_qty_1", "1")]
        new_pairs, did, _ = OnlineprintersSiteAdapter._apply_interpolation_quantity(
            pairs, qty_field_name="input_var_PBRA444_3_1", typed_value=""
        )
        self.assertFalse(did)


class TestEndToEndInterpolationAgainstRealBootstrap(unittest.TestCase):
    """Drive the synthesis adapter against the captured Onlineprinters bootstrap
    to confirm tile-vs-interpolation switching produces the right body."""

    def _drive(self, options: dict) -> dict:
        from price_extractor.cli import _prevalidate_requested_options
        from price_extractor.models import RunState, TargetInput

        path = ROOT / ".data" / "runs" / "onlineprinters.de" / "unit_c030aff4978830148365444fb9f4d15f4c3ca197" / "bootstrap.json"
        if not path.exists():
            self.skipTest(f"fixture not present: {path}")
        bootstrap = json.loads(path.read_text(encoding="utf-8"))
        prev = _prevalidate_requested_options(options, bootstrap)
        target = TargetInput(
            site_name="onlineprinters.de",
            product_url="https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
            product_type="brochure",
            expected_currency="EUR",
            options=options,
            network_traces=list(bootstrap.get("network_traces") or []),
            bootstrap_signals={**bootstrap, "option_prevalidation": prev},
        )
        state = RunState(target=target)
        candidates = OnlineprintersSiteAdapter().build_http_replay_candidates(
            state,
            effective_options=options,
            raw_requested_options=options,
        )
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def test_quantity_in_tile_presets_uses_tile_mode(self) -> None:
        # 250 is in the captured tile preset list
        cand = self._drive({"quantity": 250})
        rta = cand["request_template_applied"]
        qty_row = next((f for f in rta["fieldsUpdated"] if f["key"] == "quantity"), {})
        self.assertEqual(qty_row.get("quantityMode"), "tile")
        # Body should carry input_var_..._3_1 = "250" (NOT "Interpolation")
        from urllib.parse import parse_qsl
        body_pairs = parse_qsl(cand["body_text"], keep_blank_values=True)
        qty_val = dict(body_pairs).get("input_var_PBRA444_3_1")
        self.assertEqual(qty_val, "250")

    def test_quantity_above_max_tile_uses_interpolation_mode(self) -> None:
        cand = self._drive({"quantity": 25000})
        rta = cand["request_template_applied"]
        qty_row = next((f for f in rta["fieldsUpdated"] if f["key"] == "quantity"), {})
        self.assertEqual(qty_row.get("quantityMode"), "interpolation")
        # Note should warn about exceeding the max preset
        self.assertIn("19000", qty_row.get("interpolationNote", ""))
        # Body: qty field = "Interpolation"; next input_qty_1 carries 25000
        from urllib.parse import parse_qsl
        body_pairs = parse_qsl(cand["body_text"], keep_blank_values=True)
        # Find positions
        qty_field_idx = next(i for i, (k, _) in enumerate(body_pairs) if k == "input_var_PBRA444_3_1")
        self.assertEqual(body_pairs[qty_field_idx][1], "Interpolation")
        next_qty1 = next(((k, v) for k, v in body_pairs[qty_field_idx + 1 :] if k == "input_qty_1"), None)
        self.assertIsNotNone(next_qty1)
        self.assertEqual(next_qty1[1], "25000")


if __name__ == "__main__":
    unittest.main()
