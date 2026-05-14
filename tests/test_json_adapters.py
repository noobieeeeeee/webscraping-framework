from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.json_adapters import apply_injections, build_adapter_http_candidate


class TestApplyInjectionsSkipped(unittest.TestCase):
    def test_require_mapped_with_unknown_value_is_recorded_as_skipped(self) -> None:
        template = {"properties": [{"id": "339", "name": "quantity"}]}
        rules = [
            {
                "input_key": "quantity",
                "path": "properties[0].id",
                "cast": "str",
                "require_mapped": True,
                "value_map": {"250": "339"},
            }
        ]
        skipped: list[dict] = []
        payload, applied = apply_injections(template, rules, {"quantity": "500"}, skipped=skipped)

        self.assertEqual(applied, [])
        self.assertEqual(payload["properties"][0]["id"], "339")
        self.assertEqual(len(skipped), 1)
        entry = skipped[0]
        self.assertEqual(entry["inputKey"], "quantity")
        self.assertEqual(entry["reason"], "value_not_in_map")
        self.assertEqual(entry["rawValue"], "500")
        self.assertEqual(entry["knownValues"], ["250"])

    def test_mapped_value_still_applies_and_is_not_skipped(self) -> None:
        template = {"properties": [{"id": "339", "name": "quantity"}]}
        rules = [
            {
                "input_key": "quantity",
                "path": "properties[0].id",
                "cast": "str",
                "require_mapped": True,
                "value_map": {"250": "339", "500": "501"},
            }
        ]
        skipped: list[dict] = []
        payload, applied = apply_injections(template, rules, {"quantity": "500"}, skipped=skipped)

        self.assertEqual(skipped, [])
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["value"], "501")
        self.assertEqual(payload["properties"][0]["id"], "501")

    def test_cast_failure_is_recorded(self) -> None:
        template = {"properties": [{"id": "0", "name": "quantity"}]}
        rules = [
            {
                "input_key": "quantity",
                "path": "properties[0].id",
                "cast": "int",
                "require_mapped": False,
            }
        ]
        skipped: list[dict] = []
        _payload, applied = apply_injections(template, rules, {"quantity": "abc"}, skipped=skipped)

        self.assertEqual(applied, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "cast_failed")

    def test_skipped_param_is_optional(self) -> None:
        template = {"properties": [{"id": "339", "name": "quantity"}]}
        rules = [
            {
                "input_key": "quantity",
                "path": "properties[0].id",
                "cast": "str",
                "require_mapped": True,
                "value_map": {"250": "339"},
            }
        ]
        payload, applied = apply_injections(template, rules, {"quantity": "500"})
        self.assertEqual(applied, [])
        self.assertEqual(payload["properties"][0]["id"], "339")


class TestBuildAdapterCandidateSurfacesSkipped(unittest.TestCase):
    def _adapter(self) -> dict:
        return {
            "id": "test_adapter",
            "endpoint": {
                "url": "https://example.test/api/price",
                "method": "POST",
                "headers": {},
            },
            "request_template": {"properties": [{"id": "339", "name": "quantity"}]},
            "inject": [
                {
                    "input_key": "quantity",
                    "path": "properties[0].id",
                    "cast": "str",
                    "require_mapped": True,
                    "value_map": {"250": "339"},
                }
            ],
            "response_extract": {"price_path": "price", "currency_const": "EUR"},
        }

    def test_request_template_applied_includes_skipped(self) -> None:
        candidate = build_adapter_http_candidate(
            self._adapter(),
            target_url="https://example.test/product",
            effective_options={"quantity": "999"},
            raw_requested_options={"quantity": "999"},
        )
        self.assertIsNotNone(candidate)
        rta = candidate["request_template_applied"]
        self.assertEqual(rta["injected"], [])
        self.assertEqual(len(rta["skipped"]), 1)
        self.assertEqual(rta["skipped"][0]["reason"], "value_not_in_map")
        self.assertEqual(rta["skipped"][0]["rawValue"], "999")

    def test_request_template_applied_skipped_empty_when_all_apply(self) -> None:
        candidate = build_adapter_http_candidate(
            self._adapter(),
            target_url="https://example.test/product",
            effective_options={"quantity": "250"},
            raw_requested_options={"quantity": "250"},
        )
        self.assertIsNotNone(candidate)
        rta = candidate["request_template_applied"]
        self.assertEqual(rta["skipped"], [])
        self.assertEqual(len(rta["injected"]), 1)

    def test_unsupported_inputs_surfaced_when_adapter_has_no_rule_for_key(self) -> None:
        candidate = build_adapter_http_candidate(
            self._adapter(),
            target_url="https://example.test/product",
            effective_options={"quantity": "250", "material": "115 g/m² Bilderdruckpapier"},
            raw_requested_options={"quantity": "250", "material": "115 g/m² Bilderdruckpapier"},
        )
        self.assertIsNotNone(candidate)
        rta = candidate["request_template_applied"]
        unsupported = rta["unsupportedInputs"]
        self.assertEqual(len(unsupported), 1)
        self.assertEqual(unsupported[0]["inputKey"], "material")
        self.assertEqual(unsupported[0]["rawValue"], "115 g/m² Bilderdruckpapier")
        self.assertEqual(len(rta["injected"]), 1)

    def test_unsupported_inputs_empty_when_every_key_has_rule(self) -> None:
        candidate = build_adapter_http_candidate(
            self._adapter(),
            target_url="https://example.test/product",
            effective_options={"quantity": "250"},
            raw_requested_options={"quantity": "250"},
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["request_template_applied"]["unsupportedInputs"], [])


if __name__ == "__main__":
    unittest.main()
