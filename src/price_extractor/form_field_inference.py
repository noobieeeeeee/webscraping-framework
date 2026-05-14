"""Form-field metadata inference from captured baseline POST bodies.

The Onlineprinters bootstrap option_catalog extractor often loses the form-field
`name` attribute (radio cards rendered without `name` on the visible element).
However, the captured baseline POST that drives the WEBSALE configurator carries
the full picture: a list of `input_var_<PROD>_<GROUP>_<POSITION>` form fields
whose VALUE is the currently-selected visible label for that group, plus a
`SetLink` query string whose `depvar_index_set_N=<groupCode><groupCode+optionCode>`
tokens carry the backend codes.

This module turns those two captured signals into a structure the catalog
post-processor can use to enrich synthesized groups with form-field metadata —
specifically the `name` attribute, the currently-selected `option_code`, and the
`depvar_token`. For variants the user requests that are NOT the captured current
selection, only the `name` is known; the synthesis adapter then emits a clear
`[adapter-injection-skipped]` with reason `option_code_unknown_for_value` rather
than silently writing only the label.

The contract is per `onlineprinters_request_modification.md`. The module is
website-agnostic in mechanism — it just parses standard form-encoded bodies and
unwraps multiply-encoded URL strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


INPUT_VAR_NAME_RE = re.compile(r"^input_var_([A-Za-z0-9]+)_(\d+)_(\d+)$")

# depvar_index_setparent value: "<PROD><PROD.a.b.c>"  (group codes packed)
# depvar_index_set_N value:     "<groupCode><groupCode+optionCode>"
DEPVAR_TOKEN_PAIR_RE = re.compile(r"<([^<>]+)><([^<>]+)>")

# Captures <input ... name="input_var_..." ... value="..." ...> blocks anywhere
# in a response body. Attribute order varies, so we match on the input start tag
# and then pull individual attributes from within it.
INPUT_TAG_RE = re.compile(
    r'<input\b([^>]*?\bname="(input_var_[A-Za-z0-9_]+)"[^>]*?)/?>',
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*"([^"]*)"')


@dataclass(frozen=True)
class FormFieldInfo:
    field_name: str          # e.g. "input_var_PBRA444_1_1"
    prod_code: str           # e.g. "PBRA444"
    group_index: int         # e.g. 1
    position_index: int      # e.g. 1
    current_visible_value: str  # e.g. "130 g/m² Bilderdruckpapier"
    depvar_group_code: str = ""   # e.g. "ZBRXXXXA" (only known for position>=2)
    depvar_option_code: str = ""  # e.g. "ZBRXXXXAA01" (only known for current)
    depvar_param_name: str = ""   # e.g. "depvar_index_set_2" or "depvar_index_setparent"


@dataclass(frozen=True)
class OptionVariantInfo:
    """An alternative-option mapping learned from `<input>` elements embedded in
    captured configurator response HTML. Each variant carries the full backend
    code (`data-varindex`) so the synthesis adapter can switch to it."""
    field_name: str          # e.g. "input_var_PBRA444_1_1"
    visible_value: str       # e.g. "115 g/m² Bilderdruckpapier"
    data_varindex: str = ""  # e.g. "PBRA444.115.081000"
    data_prnumber: str = ""
    data_pimvarnodekey: str = ""
    data_productvariant: str = ""


@dataclass
class FormFieldMap:
    fields: list[FormFieldInfo] = field(default_factory=list)
    variants: list[OptionVariantInfo] = field(default_factory=list)
    raw_setlink: str = ""

    def by_current_value(self) -> dict[str, FormFieldInfo]:
        """Index fields by their currently-selected visible value (lowercased,
        normalized whitespace) for fast catalog-option alignment."""
        out: dict[str, FormFieldInfo] = {}
        for info in self.fields:
            key = _normalize_label_key(info.current_visible_value)
            if key:
                out.setdefault(key, info)
        return out

    def variants_by_field_and_value(self) -> dict[tuple[str, str], OptionVariantInfo]:
        """Index variants by (field_name, normalized_value) for lookup during
        catalog enrichment of non-current options."""
        out: dict[tuple[str, str], OptionVariantInfo] = {}
        for variant in self.variants:
            key = (variant.field_name, _normalize_label_key(variant.visible_value))
            if key[0] and key[1]:
                out.setdefault(key, variant)
        return out


def _normalize_label_key(value: str) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def _unwrap_url_encoded(text: str, max_passes: int = 3) -> str:
    decoded = str(text or "")
    for _ in range(max_passes):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    return decoded


def _unescape_json_embedded_html(text: str) -> str:
    """Captured AJAX responses are JSON whose string values contain HTML with
    backslash-escaped quotes and unicode-escaped angle brackets. The body
    preview is often truncated mid-string so `json.loads` cannot be used; this
    function just does the textual unescape that's needed for regex scanning."""
    if not text:
        return ""
    return (
        text
        .replace("\\\"", "\"")
        .replace("\\/", "/")
        .replace("\\u003c", "<")
        .replace("\\u003C", "<")
        .replace("\\u003e", ">")
        .replace("\\u003E", ">")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )


def parse_html_option_inputs(html_or_json: str) -> list[OptionVariantInfo]:
    """Scan a (possibly JSON-encoded) HTML blob for option-tile input elements
    of the form `<input ... name="input_var_..." value="..." data-varindex="..." ...>`.

    Each match yields one OptionVariantInfo. Onlineprinters' configurator AJAX
    responses ship the full option HTML inline (every paper variant is rendered
    as its own input element with the data-varindex carrying the backend code),
    so this is the website-agnostic way to recover the full (label, code) map
    without probing through the configurator one option at a time.
    """
    if not html_or_json:
        return []
    text = _unescape_json_embedded_html(html_or_json)
    out: list[OptionVariantInfo] = []
    seen: set[tuple[str, str]] = set()
    for inner_match in INPUT_TAG_RE.finditer(text):
        attrs = dict(ATTR_RE.findall(inner_match.group(1)))
        name = attrs.get("name", "")
        if not name or not name.startswith("input_var_"):
            continue
        value = attrs.get("value", "")
        if not value:
            continue
        key = (name, _normalize_label_key(value))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            OptionVariantInfo(
                field_name=name,
                visible_value=value,
                data_varindex=attrs.get("data-varindex", ""),
                data_prnumber=attrs.get("data-prnumber", ""),
                data_pimvarnodekey=attrs.get("data-pimvarnodekey", ""),
                data_productvariant=attrs.get("data-productvariant", ""),
            )
        )
    return out


_NUMERIC_VALUE_RE = re.compile(r"^\d+(?:[.,]\d+)?$")


def quantity_tile_signals(
    catalog_groups: list[dict[str, Any]],
    quantity_field_name: str,
) -> dict[str, Any]:
    """Extract Onlineprinters/WEBSALE-style Auflage tile signals for a quantity
    form field.

    Looks at every catalog option whose `name` equals `quantity_field_name` and
    inspects its visibleValue. Numeric values are tile presets; the literal
    visibleValue `"Interpolation"` (from the `Höhere Auflage angeben...` group)
    is the interpolation sentinel and carries the depvar option code that
    `setlink.depvar_index_setparent` needs when the user requests a value above
    the highest tile preset.

    Returns:
      tilePresets: list of preset numeric values as strings, sorted ascending
      tileVarindexByValue: map from preset value (str) to its `data-varindex`
      interpolationVarindex: data-varindex of the Interpolation tile (or "")
      supportsInterpolation: bool — True when the Interpolation tile is captured
      maxTilePreset: largest preset as int (0 if none)
    """
    presets: dict[str, str] = {}
    interpolation_code = ""
    for group in catalog_groups or []:
        if not isinstance(group, dict):
            continue
        for opt in list(group.get("options") or []):
            if not isinstance(opt, dict):
                continue
            if str(opt.get("name") or "") != quantity_field_name:
                continue
            vv = str(opt.get("visibleValue") or "").strip()
            hints = dict(opt.get("backendHints") or {})
            varindex = str(hints.get("dataVarindex") or "")
            if vv.lower() == "interpolation":
                if varindex and not interpolation_code:
                    interpolation_code = varindex
                continue
            if _NUMERIC_VALUE_RE.match(vv):
                presets.setdefault(vv, varindex)

    sorted_presets = sorted(presets.keys(), key=lambda v: int(v))
    max_preset = max((int(v) for v in presets), default=0)
    return {
        "tilePresets": sorted_presets,
        "tileVarindexByValue": dict(presets),
        "interpolationVarindex": interpolation_code,
        "supportsInterpolation": bool(interpolation_code),
        "maxTilePreset": max_preset,
    }


def collect_variants_from_traces(network_traces: list[dict[str, Any]]) -> list[OptionVariantInfo]:
    """Aggregate option-tile variants across all captured response previews.

    Captures are often truncated, so concatenating widens the surface area for
    pattern matches without changing semantics — each variant is a fully
    self-contained `<input>` element, so the truncation only loses variants
    that didn't fit in a given preview, not ones that did."""
    seen: set[tuple[str, str]] = set()
    out: list[OptionVariantInfo] = []
    for trace in network_traces or []:
        if not isinstance(trace, dict):
            continue
        preview = str(trace.get("response_body_preview") or "")
        if not preview:
            continue
        for variant in parse_html_option_inputs(preview):
            key = (variant.field_name, _normalize_label_key(variant.visible_value))
            if key in seen:
                continue
            seen.add(key)
            out.append(variant)
    return out


def parse_form_field_map(post_body: str) -> FormFieldMap:
    """Parse a captured POST body into FormFieldMap.

    Tolerates double-URL-encoded SetLink (the WEBSALE configurator triple-encodes
    it). Returns an empty map if no input_var fields are present (caller can then
    skip enrichment).
    """
    if not post_body:
        return FormFieldMap()

    pairs = parse_qsl(post_body, keep_blank_values=True)
    if not pairs:
        return FormFieldMap()

    raw_setlink = next((value for key, value in pairs if key.lower() == "setlink" and value), "")
    decoded_setlink = _unwrap_url_encoded(raw_setlink) if raw_setlink else ""

    # Extract per-position depvar tokens keyed by the position number in
    # `depvar_index_set_N`. setparent applies to position 1.
    depvar_by_param: dict[str, tuple[str, str]] = {}
    if decoded_setlink:
        query = urlsplit(decoded_setlink).query
        for key, value in parse_qsl(query, keep_blank_values=True):
            if not key.startswith("depvar_index_set"):
                continue
            match = DEPVAR_TOKEN_PAIR_RE.search(value)
            if not match:
                continue
            depvar_by_param[key] = (match.group(1), match.group(2))

    fields: list[FormFieldInfo] = []
    for key, value in pairs:
        match = INPUT_VAR_NAME_RE.match(key)
        if not match:
            continue
        prod_code = match.group(1)
        group_index = int(match.group(2))
        position_index = int(match.group(3))
        # Figure out which depvar token applies to this position.
        depvar_param = (
            "depvar_index_setparent"
            if position_index == 1
            else f"depvar_index_set_{position_index - 1}"
        )
        depvar_pair = depvar_by_param.get(depvar_param, ("", ""))
        fields.append(
            FormFieldInfo(
                field_name=key,
                prod_code=prod_code,
                group_index=group_index,
                position_index=position_index,
                current_visible_value=str(value or ""),
                depvar_group_code=depvar_pair[0],
                depvar_option_code=depvar_pair[1],
                depvar_param_name=depvar_param if depvar_pair[0] else "",
            )
        )

    return FormFieldMap(fields=fields, raw_setlink=raw_setlink)


def build_form_field_map_from_traces(
    network_traces: list[dict[str, Any]],
    product_url: str,
) -> FormFieldMap:
    """Convenience entry point: parse the best baseline POST body for the
    current-selection metadata, then enrich with all option variants harvested
    from configurator response HTML across captured traces."""
    baseline_body = pick_best_baseline_post(network_traces, product_url)
    base_map = parse_form_field_map(baseline_body) if baseline_body else FormFieldMap()
    variants = collect_variants_from_traces(network_traces)
    if variants:
        base_map = FormFieldMap(
            fields=list(base_map.fields),
            variants=variants,
            raw_setlink=base_map.raw_setlink,
        )
    return base_map


def pick_best_baseline_post(network_traces: list[dict[str, Any]], product_url: str) -> str:
    """Select the captured POST body most likely to be the configurator baseline.

    Scores prefer: POSTs to the product page itself, containing `setlink=` AND at
    least one `input_var_` field, with a JSON response. Returns the post body
    string (raw, not URL-decoded). Returns empty string if no candidate found.
    """
    product_url_lower = str(product_url or "").strip().lower()
    candidates: list[tuple[int, str]] = []
    for trace in network_traces or []:
        if not isinstance(trace, dict):
            continue
        method = str(trace.get("method") or "").upper()
        if method != "POST":
            continue
        body = str(trace.get("post_data") or "")
        if not body:
            continue
        body_lower = body.lower()
        if "setlink=" not in body_lower or "input_var_" not in body_lower:
            continue

        url = str(trace.get("url") or "").lower()
        score = 0
        if product_url_lower and product_url_lower in url:
            score += 6
        if "application/json" in str(trace.get("response_content_type") or "").lower():
            score += 4
        if str(trace.get("response_body_preview") or "").strip():
            score += 2
        if str(trace.get("resource_type") or "").lower() in {"xhr", "fetch"}:
            score += 2
        candidates.append((score, body))

    if not candidates:
        return ""
    return max(candidates, key=lambda row: row[0])[1]


def enrich_catalog_with_form_fields(
    catalog: list[dict[str, Any]],
    form_map: FormFieldMap,
) -> list[dict[str, Any]]:
    """Attach `name` + currentOptionCode to catalog options whose visibleLabel
    matches a captured input_var current value.

    Only options whose label is the currently-selected one get a full set of
    metadata. Other options in the same synthetic group inherit the field `name`
    (so the synthesis adapter knows WHERE to write) but `optionCodeUnknown=True`
    so the adapter can emit a precise skip reason instead of silently writing.

    Operates in-place on a deep-ish copy and returns the result.
    """
    if not catalog or (not form_map.fields and not form_map.variants):
        return list(catalog or [])

    index = form_map.by_current_value()
    variants_index = form_map.variants_by_field_and_value()
    if not index and not variants_index:
        return list(catalog or [])

    def _candidate_keys(option: dict[str, Any]) -> list[str]:
        seen: list[str] = []
        for raw in (option.get("visibleValue"), option.get("visibleLabel"), option.get("sourceGroupLabel")):
            key = _normalize_label_key(str(raw or ""))
            if key and key not in seen:
                seen.append(key)
        return seen

    out: list[dict[str, Any]] = []
    for group in catalog:
        if not isinstance(group, dict):
            out.append(group)
            continue
        new_group = dict(group)
        options = list(new_group.get("options") or [])

        # First pass A: find the captured baseline-POST field that this group
        # represents (gives us currently-selected option code + group/param).
        matched_field: FormFieldInfo | None = None
        for option in options:
            for label_key in _candidate_keys(option):
                match = index.get(label_key)
                if match is not None:
                    matched_field = match
                    break
            if matched_field is not None:
                break

        # First pass B: even without a baseline match, response-HTML variants
        # can identify the field for the group.
        field_name = matched_field.field_name if matched_field else ""
        if not field_name and variants_index:
            for option in options:
                for label_key in _candidate_keys(option):
                    for (vname, vlabel), _variant in variants_index.items():
                        if vlabel == label_key:
                            field_name = vname
                            break
                    if field_name:
                        break
                if field_name:
                    break

        if not field_name:
            out.append(new_group)
            continue

        current_value_key = (
            _normalize_label_key(matched_field.current_visible_value)
            if matched_field else ""
        )
        new_options: list[dict[str, Any]] = []
        variants_used = 0
        for option in options:
            new_opt = dict(option)
            new_opt["name"] = field_name
            backend_hints = dict(new_opt.get("backendHints") or {})

            option_code: str = ""
            # First check for a response-HTML variant with the full code.
            for label_key in _candidate_keys(option):
                variant = variants_index.get((field_name, label_key))
                if variant is not None:
                    backend_hints["dataVarindex"] = variant.data_varindex
                    if variant.data_prnumber:
                        backend_hints["dataPrnumber"] = variant.data_prnumber
                    if variant.data_pimvarnodekey:
                        backend_hints["dataPimvarnodekey"] = variant.data_pimvarnodekey
                    if variant.data_productvariant:
                        backend_hints["dataProductvariant"] = variant.data_productvariant
                    option_code = variant.data_varindex
                    variants_used += 1
                    break

            # Fall back to the baseline-POST code for the currently-selected
            # option (only useful when variants didn't cover it).
            if not option_code and matched_field is not None and current_value_key:
                if current_value_key in _candidate_keys(option):
                    if matched_field.depvar_option_code:
                        backend_hints.setdefault("dataVarindex", matched_field.depvar_option_code)
                        option_code = matched_field.depvar_option_code
                    if matched_field.depvar_group_code:
                        backend_hints.setdefault("depvarGroupCode", matched_field.depvar_group_code)
                    if matched_field.depvar_param_name:
                        backend_hints.setdefault("depvarParam", matched_field.depvar_param_name)

            # Mark currently-selected.
            if current_value_key and current_value_key in _candidate_keys(option):
                new_opt["selected"] = True

            if not option_code:
                new_opt["optionCodeUnknown"] = True

            new_opt["backendHints"] = backend_hints
            new_options.append(new_opt)
        new_group["options"] = new_options

        source_hints = dict(new_group.get("sourceHints") or {})
        source_hints["formFieldName"] = field_name
        if matched_field is not None:
            source_hints["formFieldProd"] = matched_field.prod_code
            source_hints["formFieldGroupIndex"] = matched_field.group_index
            source_hints["formFieldPositionIndex"] = matched_field.position_index
            source_hints["formFieldEnrichedFromCapturedPost"] = True
        if variants_used:
            source_hints["formFieldVariantsEnriched"] = variants_used
        new_group["sourceHints"] = source_hints
        out.append(new_group)

    return out
