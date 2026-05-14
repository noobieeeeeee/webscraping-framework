from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from price_extractor.form_field_inference import (
    build_form_field_map_from_traces,
    collect_variants_from_traces,
    enrich_catalog_with_form_fields,
    parse_form_field_map,
    parse_html_option_inputs,
    pick_best_baseline_post,
)


def _double_encode(value: str) -> str:
    """The captured WEBSALE SetLink is triple-encoded; parse_qsl decodes once,
    so test bodies should encode twice to mimic the captured form-pair value."""
    from urllib.parse import quote
    return quote(quote(value, safe=""), safe="")


class TestParseFormFieldMap(unittest.TestCase):
    def test_extracts_input_var_fields_and_setlink_depvar_tokens(self) -> None:
        setlink_decoded = (
            "https://www.onlineprinters.de/p/x?"
            "depvar_index_setparent=<PBRA444><PBRA444.135.081000>"
            "&depvar_index_set_1=<ZBRA401U><ZBRA401UU00.00>"
            "&depvar_index_set_2=<ZBRXXXXA><ZBRXXXXAA01>"
        )
        body = (
            f"SetLink={_double_encode(setlink_decoded)}"
            "&input_var_PBRA444_1_1=130%20g%2Fm%C2%B2%20Bilderdruckpapier"
            "&input_var_PBRA444_3_1=1000"
            "&input_var_ZBRA401U_1_2=kein%20Umschlag"
            "&input_var_ZBRXXXXA_1_3=gl%C3%A4nzend%20gestrichen"
        )
        result = parse_form_field_map(body)
        self.assertEqual(len(result.fields), 4)

        paper = next(f for f in result.fields if f.group_index == 1 and f.position_index == 1)
        self.assertEqual(paper.field_name, "input_var_PBRA444_1_1")
        self.assertEqual(paper.prod_code, "PBRA444")
        self.assertEqual(paper.current_visible_value, "130 g/m² Bilderdruckpapier")
        # position 1 ↔ setparent
        self.assertEqual(paper.depvar_param_name, "depvar_index_setparent")
        self.assertEqual(paper.depvar_group_code, "PBRA444")
        self.assertEqual(paper.depvar_option_code, "PBRA444.135.081000")

        coating = next(f for f in result.fields if f.position_index == 3)
        # position 3 ↔ depvar_index_set_2
        self.assertEqual(coating.depvar_param_name, "depvar_index_set_2")
        self.assertEqual(coating.depvar_group_code, "ZBRXXXXA")
        self.assertEqual(coating.depvar_option_code, "ZBRXXXXAA01")

    def test_empty_body_returns_empty_map(self) -> None:
        result = parse_form_field_map("")
        self.assertEqual(result.fields, [])

    def test_body_without_input_var_returns_empty_fields(self) -> None:
        result = parse_form_field_map("foo=bar&baz=qux")
        self.assertEqual(result.fields, [])

    def test_by_current_value_index_is_whitespace_and_case_insensitive(self) -> None:
        body = "input_var_X_1_1=  130 g/m² Bilderdruckpapier  "
        result = parse_form_field_map(body)
        idx = result.by_current_value()
        self.assertIn("130 g/m² bilderdruckpapier", idx)


class TestPickBestBaselinePost(unittest.TestCase):
    def test_prefers_post_to_product_url_with_setlink_and_input_var(self) -> None:
        traces = [
            {
                "url": "https://tracking.example/pixel",
                "method": "GET",
                "post_data": "",
            },
            {
                "url": "https://www.onlineprinters.de/p/something-else",
                "method": "POST",
                "post_data": "SetLink=x&input_var_FOO_1_1=bar",
                "response_content_type": "text/html",
            },
            {
                "url": "https://www.onlineprinters.de/p/target",
                "method": "POST",
                "post_data": "SetLink=x&input_var_FOO_1_1=baz",
                "response_content_type": "application/json",
                "response_body_preview": "{}",
                "resource_type": "xhr",
            },
        ]
        body = pick_best_baseline_post(traces, "https://www.onlineprinters.de/p/target")
        self.assertIn("baz", body)

    def test_returns_empty_when_no_post_has_setlink_and_input_var(self) -> None:
        traces = [{"url": "x", "method": "POST", "post_data": "foo=bar"}]
        self.assertEqual(pick_best_baseline_post(traces, "x"), "")


class TestEnrichCatalogWithFormFields(unittest.TestCase):
    def _catalog(self) -> list[dict]:
        return [
            {
                "groupLabel": "material (innen)",
                "options": [
                    {"visibleLabel": "90 g/m² Bilderdruckpapier", "visibleValue": "90 g/m² Bilderdruckpapier"},
                    {"visibleLabel": "130 g/m² Bilderdruckpapier", "visibleValue": "130 g/m² Bilderdruckpapier"},
                    {"visibleLabel": "150 g/m² Bilderdruckpapier", "visibleValue": "150 g/m² Bilderdruckpapier"},
                ],
                "sourceHints": {"synthesized": True},
            }
        ]

    def _form_map_with_current_paper(self) -> "object":
        setlink = (
            "https://www.onlineprinters.de/p/x?"
            "depvar_index_setparent=<PBRA444><PBRA444.135.081000>"
        )
        body = (
            f"SetLink={_double_encode(setlink)}"
            "&input_var_PBRA444_1_1=130%20g%2Fm%C2%B2%20Bilderdruckpapier"
        )
        return parse_form_field_map(body)

    def test_currently_selected_option_gets_full_metadata(self) -> None:
        form_map = self._form_map_with_current_paper()
        enriched = enrich_catalog_with_form_fields(self._catalog(), form_map)
        group = enriched[0]
        opts_by_label = {opt["visibleLabel"]: opt for opt in group["options"]}
        current = opts_by_label["130 g/m² Bilderdruckpapier"]
        self.assertEqual(current["name"], "input_var_PBRA444_1_1")
        self.assertTrue(current["selected"])
        self.assertEqual(current["backendHints"]["dataVarindex"], "PBRA444.135.081000")
        self.assertEqual(current["backendHints"]["depvarGroupCode"], "PBRA444")
        self.assertNotIn("optionCodeUnknown", current)

    def test_other_options_get_field_name_but_unknown_code_flag(self) -> None:
        form_map = self._form_map_with_current_paper()
        enriched = enrich_catalog_with_form_fields(self._catalog(), form_map)
        opts_by_label = {opt["visibleLabel"]: opt for opt in enriched[0]["options"]}
        other = opts_by_label["90 g/m² Bilderdruckpapier"]
        self.assertEqual(other["name"], "input_var_PBRA444_1_1")
        self.assertTrue(other["optionCodeUnknown"])
        # No dataVarindex assigned — we don't know the code for non-current variants
        self.assertEqual(other["backendHints"].get("dataVarindex"), None)

    def test_group_with_no_matching_option_is_left_unchanged(self) -> None:
        form_map = self._form_map_with_current_paper()
        catalog = [{"groupLabel": "Auflage", "options": [{"visibleLabel": "1000", "visibleValue": "1000"}]}]
        enriched = enrich_catalog_with_form_fields(catalog, form_map)
        self.assertEqual(enriched[0]["options"][0].get("name", ""), "")

    def test_source_hints_record_form_field_provenance(self) -> None:
        form_map = self._form_map_with_current_paper()
        enriched = enrich_catalog_with_form_fields(self._catalog(), form_map)
        hints = enriched[0]["sourceHints"]
        self.assertEqual(hints["formFieldName"], "input_var_PBRA444_1_1")
        self.assertEqual(hints["formFieldProd"], "PBRA444")
        self.assertEqual(hints["formFieldGroupIndex"], 1)
        self.assertTrue(hints["formFieldEnrichedFromCapturedPost"])


class TestParseHtmlOptionInputs(unittest.TestCase):
    """Approach (b): the configurator AJAX response ships <input> elements
    inline for every option variant. Each input has data-varindex carrying the
    backend code we need to switch to that variant."""

    def test_extracts_data_varindex_from_input_tag(self) -> None:
        html = (
            '<input type="checkbox" name="input_var_PBRA444_1_1" '
            'value="115 g/m² Bilderdruckpapier" data-varindex="PBRA444.115.081000" '
            'data-prnumber="PBRA444115081000" data-pimvarnodekey="PBRA444_115_08" '
            'data-productvariant="115">'
        )
        variants = parse_html_option_inputs(html)
        self.assertEqual(len(variants), 1)
        v = variants[0]
        self.assertEqual(v.field_name, "input_var_PBRA444_1_1")
        self.assertEqual(v.visible_value, "115 g/m² Bilderdruckpapier")
        self.assertEqual(v.data_varindex, "PBRA444.115.081000")
        self.assertEqual(v.data_prnumber, "PBRA444115081000")
        self.assertEqual(v.data_pimvarnodekey, "PBRA444_115_08")
        self.assertEqual(v.data_productvariant, "115")

    def test_handles_json_escaped_html(self) -> None:
        # WEBSALE AJAX responses are JSON; HTML attributes inside string values
        # have backslash-escaped quotes and unicode-escaped angle brackets.
        body = (
            '"WS-Ajax-tile" : "\\u003cinput type=\\"checkbox\\" '
            'name=\\"input_var_PBRA444_1_1\\" '
            'value=\\"90 g/m\\u00b2 Bilderdruckpapier\\" '
            'data-varindex=\\"PBRA444.090.081000\\"\\u003e"'
        )
        variants = parse_html_option_inputs(body)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].data_varindex, "PBRA444.090.081000")

    def test_ignores_inputs_without_input_var_name(self) -> None:
        html = (
            '<input type="text" name="email" value="foo@bar">'
            '<input type="hidden" name="csrf" value="abc">'
        )
        variants = parse_html_option_inputs(html)
        self.assertEqual(variants, [])

    def test_dedupes_repeated_variant_blocks(self) -> None:
        # The same variant input often appears multiple times in different
        # AJAX response fragments (tile + summary block, etc.).
        html = (
            '<input name="input_var_X_1_1" value="A" data-varindex="X.001">'
            '<input name="input_var_X_1_1" value="A" data-varindex="X.001">'
        )
        variants = parse_html_option_inputs(html)
        self.assertEqual(len(variants), 1)

    def test_handles_arbitrary_attribute_order(self) -> None:
        html = (
            '<input data-varindex="X.222" name="input_var_X_1_1" '
            'data-extra="ignore" value="B">'
        )
        variants = parse_html_option_inputs(html)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].data_varindex, "X.222")
        self.assertEqual(variants[0].visible_value, "B")


class TestCollectVariantsFromTraces(unittest.TestCase):
    def test_aggregates_variants_across_traces_and_dedupes(self) -> None:
        traces = [
            {
                "response_body_preview": (
                    '<input name="input_var_X_1_1" value="A" data-varindex="X.001">'
                ),
            },
            {
                "response_body_preview": (
                    '<input name="input_var_X_1_1" value="B" data-varindex="X.002">'
                    '<input name="input_var_X_1_1" value="A" data-varindex="X.001">'
                ),
            },
            {"response_body_preview": ""},
        ]
        variants = collect_variants_from_traces(traces)
        self.assertEqual(len(variants), 2)
        codes = {v.data_varindex for v in variants}
        self.assertEqual(codes, {"X.001", "X.002"})


class TestEnrichmentWithVariantCodes(unittest.TestCase):
    """The key win from approach (b): non-current options now get their depvar
    option code from response-HTML variants, so the synthesis adapter can
    actually switch to them instead of skipping with option_code_unknown."""

    def test_non_current_option_gets_code_from_variant_index(self) -> None:
        from price_extractor.form_field_inference import FormFieldMap, OptionVariantInfo

        catalog = [
            {
                "groupLabel": "material (innen)",
                "options": [
                    {"visibleLabel": "90 g/m² Bilderdruckpapier", "visibleValue": "90 g/m² Bilderdruckpapier"},
                    {"visibleLabel": "115 g/m² Bilderdruckpapier", "visibleValue": "115 g/m² Bilderdruckpapier"},
                ],
            }
        ]
        # Only variants, no baseline-POST fields — exercises the variants-only path.
        form_map = FormFieldMap(
            variants=[
                OptionVariantInfo(
                    field_name="input_var_PBRA444_1_1",
                    visible_value="90 g/m² Bilderdruckpapier",
                    data_varindex="PBRA444.090.081000",
                ),
                OptionVariantInfo(
                    field_name="input_var_PBRA444_1_1",
                    visible_value="115 g/m² Bilderdruckpapier",
                    data_varindex="PBRA444.115.081000",
                ),
            ]
        )
        enriched = enrich_catalog_with_form_fields(catalog, form_map)
        opts = {o["visibleLabel"]: o for o in enriched[0]["options"]}
        self.assertEqual(opts["115 g/m² Bilderdruckpapier"]["name"], "input_var_PBRA444_1_1")
        self.assertEqual(
            opts["115 g/m² Bilderdruckpapier"]["backendHints"]["dataVarindex"],
            "PBRA444.115.081000",
        )
        self.assertNotIn("optionCodeUnknown", opts["115 g/m² Bilderdruckpapier"])

    def test_variant_index_overrides_baseline_only_for_matching_value(self) -> None:
        # If both baseline and variants are available, variants supply
        # individual option codes per row while baseline supplies the
        # `currently-selected` marker.
        from price_extractor.form_field_inference import FormFieldMap, OptionVariantInfo, FormFieldInfo

        catalog = [
            {
                "groupLabel": "material (innen)",
                "options": [
                    {"visibleLabel": "130 g/m² Bilderdruckpapier", "visibleValue": "130 g/m² Bilderdruckpapier"},
                    {"visibleLabel": "115 g/m² Bilderdruckpapier", "visibleValue": "115 g/m² Bilderdruckpapier"},
                ],
            }
        ]
        form_map = FormFieldMap(
            fields=[
                FormFieldInfo(
                    field_name="input_var_PBRA444_1_1",
                    prod_code="PBRA444",
                    group_index=1,
                    position_index=1,
                    current_visible_value="130 g/m² Bilderdruckpapier",
                    depvar_group_code="PBRA444",
                    depvar_option_code="PBRA444.135.081000",
                    depvar_param_name="depvar_index_setparent",
                )
            ],
            variants=[
                OptionVariantInfo(
                    field_name="input_var_PBRA444_1_1",
                    visible_value="130 g/m² Bilderdruckpapier",
                    data_varindex="PBRA444.135.081000",
                ),
                OptionVariantInfo(
                    field_name="input_var_PBRA444_1_1",
                    visible_value="115 g/m² Bilderdruckpapier",
                    data_varindex="PBRA444.115.081000",
                ),
            ],
        )
        enriched = enrich_catalog_with_form_fields(catalog, form_map)
        opts = {o["visibleLabel"]: o for o in enriched[0]["options"]}
        # Current selection: marked selected, code present
        self.assertTrue(opts["130 g/m² Bilderdruckpapier"]["selected"])
        self.assertEqual(
            opts["130 g/m² Bilderdruckpapier"]["backendHints"]["dataVarindex"],
            "PBRA444.135.081000",
        )
        # Non-current: not selected, but now has its OWN code from variants
        self.assertNotIn("selected", opts["115 g/m² Bilderdruckpapier"])
        self.assertEqual(
            opts["115 g/m² Bilderdruckpapier"]["backendHints"]["dataVarindex"],
            "PBRA444.115.081000",
        )
        self.assertNotIn("optionCodeUnknown", opts["115 g/m² Bilderdruckpapier"])


class TestBuildFormFieldMapFromTracesIntegration(unittest.TestCase):
    def test_combines_baseline_post_fields_with_response_html_variants(self) -> None:
        baseline_setlink = (
            "https://x.test/p?depvar_index_setparent=<PBRA444><PBRA444.135.081000>"
        )
        baseline_post_body = (
            f"SetLink={_double_encode(baseline_setlink)}"
            "&input_var_PBRA444_1_1=130%20g%2Fm%C2%B2%20Bilderdruckpapier"
        )
        traces = [
            {
                "method": "POST",
                "url": "https://x.test/p",
                "post_data": baseline_post_body,
                "response_content_type": "application/json",
                "response_body_preview": (
                    '<input name="input_var_PBRA444_1_1" value="90 g/m² Bilderdruckpapier" '
                    'data-varindex="PBRA444.090.081000">'
                    '<input name="input_var_PBRA444_1_1" value="115 g/m² Bilderdruckpapier" '
                    'data-varindex="PBRA444.115.081000">'
                ),
            },
        ]
        form_map = build_form_field_map_from_traces(traces, "https://x.test/p")
        self.assertEqual(len(form_map.fields), 1)
        self.assertEqual(form_map.fields[0].current_visible_value, "130 g/m² Bilderdruckpapier")
        self.assertEqual(len(form_map.variants), 2)
        codes = {v.data_varindex for v in form_map.variants}
        self.assertEqual(codes, {"PBRA444.090.081000", "PBRA444.115.081000"})


if __name__ == "__main__":
    unittest.main()
