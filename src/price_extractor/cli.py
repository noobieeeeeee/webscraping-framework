from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import hashlib
from io import StringIO
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from .agents import state_snapshot
from .agentic.onboarding import _canonical_option_key, build_onboarding_proposal
from .agentic.langgraph_onboarding import build_langgraph_onboarding_payload
from .bootstrap_schema import (
    BootstrapArtifacts,
    bootstrap_result_to_artifacts,
    normalize_bootstrap_artifacts,
)
from .browser_bootstrap import bootstrap_product_url
from .job_state import (
    build_unit_id,
    load_checkpoint,
    mark_completed,
    mark_failed,
    mark_skipped,
    save_checkpoint,
    should_process_unit,
)
from .knowledge_store import KnowledgeStore
from .models import RunState, TargetInput
from .form_field_inference import (
    build_form_field_map_from_traces,
    enrich_catalog_with_form_fields,
    quantity_tile_signals,
)
from .option_catalog_postprocess import regroup_singleton_clusters
from .print24_catalog import harvest_print24_catalog_from_traces
from .value_map_loader import (
    describe_value_map,
    load_value_map,
    resolve_value as resolve_value_map_value,
)
from .agentic.adapter_generation import generate_runnable_adapters
from .orchestrator import ExtractionOrchestrator, summarize_run
from .staged_runner import run_staged_rollout


def parse_options_json(payload: str | None) -> dict:
    if payload is None:
        return {}
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("--options-json must be a JSON object")
    return parsed


def load_options_file(path: str | None) -> dict:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--options-file must contain a JSON object")
    return payload


def _coerce_option_value(raw_value: str) -> Any:
    value = str(raw_value).strip()
    lowered = value.lower()

    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
        try:
            return float(value)
        except ValueError:
            pass
    return value


def parse_option_pairs(pairs: list[str] | None) -> dict[str, Any]:
    if not pairs:
        return {}

    parsed: dict[str, Any] = {}
    for item in pairs:
        text = str(item or "").strip()
        if "=" not in text:
            raise ValueError("--option values must be in key=value format")
        key, value = text.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--option key must be non-empty")
        parsed[key] = _coerce_option_value(value)
    return parsed


def compose_options(spec: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    merged: dict[str, Any] = {}

    spec_options = spec.get("options")
    if isinstance(spec_options, dict):
        merged.update(spec_options)

    file_options = load_options_file(args.options_file)
    json_options = parse_options_json(args.options_json)
    pair_options = parse_option_pairs(args.option)

    # Precedence: --option > --options-json > --options-file > manifest/spec defaults.
    merged.update(file_options)
    merged.update(json_options)
    merged.update(pair_options)
    return merged


def load_proxy_pool(proxy_file: str | None, proxy_url: str | None) -> list[str]:
    proxies: list[str] = []
    if proxy_file:
        lines = Path(proxy_file).read_text(encoding="utf-8").splitlines()
        for raw in lines:
            text = str(raw or "").strip()
            if not text or text.startswith("#"):
                continue
            proxies.append(text)
    if proxy_url:
        value = str(proxy_url).strip()
        if value:
            proxies.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for proxy in proxies:
        if proxy in seen:
            continue
        seen.add(proxy)
        deduped.append(proxy)
    return deduped


def _normalize_match_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.replace("&", " and ").replace("²", "2")
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text)
    return " ".join(ascii_text.split())


def _token_set(value: Any) -> set[str]:
    normalized = _normalize_match_text(value)
    if not normalized:
        return set()
    return {token for token in normalized.split(" ") if token}


def _text_match_score(candidate: Any, desired: Any) -> int:
    candidate_norm = _normalize_match_text(candidate)
    desired_norm = _normalize_match_text(desired)
    if not candidate_norm or not desired_norm:
        return 0
    if candidate_norm == desired_norm:
        return 30
    if candidate_norm in desired_norm or desired_norm in candidate_norm:
        return 20
    candidate_compact = candidate_norm.replace(" ", "")
    desired_compact = desired_norm.replace(" ", "")
    if candidate_compact and desired_compact:
        if candidate_compact == desired_compact:
            return 24
        if candidate_compact in desired_compact or desired_compact in candidate_compact:
            return 16
    overlap = len(_token_set(candidate_norm).intersection(_token_set(desired_norm)))
    if overlap <= 0:
        return 0
    return min(14, overlap * 4)


def _option_key_aliases(raw_key: Any) -> set[str]:
    normalized = _normalize_match_text(raw_key)
    aliases = {normalized} if normalized else set()
    alias_groups = {
        "quantity": {
            "quantity",
            "qty",
            "auflage",
            "menge",
            "amount",
            "stuck",
            "stueck",
            "anzahl",
        },
        "format": {
            "format",
            "size",
            "endformat",
            "din",
            "groesse",
            "grosse",
            "content size",
            "content_size",
            "end format",
        },
        "material": {
            "material",
            "papier",
            "paper",
            "gramm",
            "grammage",
            "bilderdruck",
            "karton",
            "content paper",
            "content_paper",
            "innenteil papier",
            "innenteil_papier",
        },
        "pages": {
            "seiten",
            "seitig",
            "pages",
            "umfang",
            "page",
            "inner",
            "inner part",
            "innenteil",
            "seitenzahl",
        },
        "cover": {
            "cover",
            "umschlag",
            "zusatzlicher umschlag",
            "zusatzlicher_umschlag",
            "cover paper",
            "cover_paper",
            "cover material",
            "cover_material",
            "cover type",
            "covertype",
        },
        "delivery": {
            "delivery",
            "lieferung",
            "lieferzeit",
            "arbeitstage",
            "production time",
            "versand",
        },
        "print": {
            "farbe",
            "color",
            "colour",
            "druck",
            "print",
            "printing",
            "content color",
            "content_color",
            "cover color",
            "cover_color",
            "ausfuhrung innenteil",
            "ausrichtung",
        },
    }
    for canonical, group_aliases in alias_groups.items():
        if normalized == canonical or normalized in group_aliases or any(alias in normalized for alias in group_aliases):
            aliases.add(canonical)
            aliases.update(group_aliases)
    return {alias for alias in aliases if alias}


def _compact_prevalidation(prevalidation: dict[str, Any]) -> dict[str, Any]:
    matched = list(prevalidation.get("matched") or [])
    unmatched = list(prevalidation.get("unmatched") or [])
    return {
        "available": bool(prevalidation.get("available", False)),
        "valid": bool(prevalidation.get("valid", True)),
        "matchedCount": len(matched),
        "unmatchedCount": len(unmatched),
        "warnings": list(prevalidation.get("warnings") or []),
        "errors": list(prevalidation.get("errors") or []),
        "matched": [
            {
                "key": row.get("key"),
                "requestedValue": row.get("requestedValue"),
                "groupLabel": row.get("groupLabel"),
                "matchedLabel": row.get("matchedLabel"),
                "matchType": row.get("matchType"),
            }
            for row in matched[:12]
        ],
        "unmatched": [
            {
                "key": row.get("key"),
                "requestedValue": row.get("requestedValue"),
                "reason": row.get("reason"),
                "groupLabel": row.get("groupLabel"),
                "suggestions": list(row.get("suggestions") or []),
            }
            for row in unmatched[:12]
        ],
    }


def _catalog_groups_from_bootstrap(bootstrap_artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = list(bootstrap_artifacts.get("option_catalog") or [])
    network_traces = list(bootstrap_artifacts.get("network_traces") or [])
    if catalog:
        regrouped = catalog
    else:
        summary_groups = list(bootstrap_artifacts.get("option_groups") or [])
        regrouped = regroup_singleton_clusters(summary_groups) if summary_groups else []

    # Print24's DOM walker doesn't see the Next.js-rendered option widgets,
    # so its `option_groups` is either empty or full of irrelevant display
    # preferences (Nettopreise / Bruttopreise / etc.). The productDetails AJAX
    # response carries the currently-selected option per group with the right
    # `prop_id`, so harvest it regardless and prepend — pre-validation prefers
    # rows with structured backendHints over labelled-only fallback rows.
    print24_rows = harvest_print24_catalog_from_traces(network_traces)
    if print24_rows:
        regrouped = print24_rows + regrouped

    if not regrouped:
        return []

    # Enrich with form-field metadata learned from:
    #   (a) the captured baseline POST (currently-selected option codes), and
    #   (b) `<input ... name="input_var_..." ... data-varindex="...">` elements
    #       embedded in configurator AJAX response HTML (full option-code map
    #       for non-current variants, per the variant-URL scrape approach).
    # Sites without input_var-style fields (Print24, Saxoprint) get a no-op.
    network_traces = list(bootstrap_artifacts.get("network_traces") or [])
    product_url = ""
    for trace in network_traces:
        if isinstance(trace, dict) and str(trace.get("method") or "").upper() == "POST":
            product_url = str(trace.get("url") or "")
            if product_url:
                break
    form_map = build_form_field_map_from_traces(network_traces, product_url)
    if not form_map.fields and not form_map.variants:
        return regrouped
    return enrich_catalog_with_form_fields(regrouped, form_map)


def _catalog_option_score(option_row: dict[str, Any], requested_value: Any) -> int:
    candidate_texts = [
        option_row.get("visibleLabel"),
        option_row.get("visibleValue"),
        option_row.get("title"),
        option_row.get("ariaLabel"),
        option_row.get("name"),
        option_row.get("href"),
    ]
    backend_hints = dict(option_row.get("backendHints") or {})
    candidate_texts.extend(
        [
            backend_hints.get("dataVarindex"),
            backend_hints.get("dataPrnumber"),
            backend_hints.get("dataPimvarnodekey"),
            backend_hints.get("dataOptionId"),
            backend_hints.get("dataPropertyId"),
        ]
    )
    return max((_text_match_score(text, requested_value) for text in candidate_texts), default=0)


def _best_suggestions(option_rows: list[dict[str, Any]], requested_value: Any, limit: int = 3) -> list[str]:
    ranked: list[tuple[int, str]] = []
    for row in option_rows:
        label = str(row.get("visibleLabel") or row.get("visibleValue") or "").strip()
        if not label:
            continue
        score = _catalog_option_score(row, requested_value)
        ranked.append((score, label))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    suggestions: list[str] = []
    for _score, label in ranked:
        if label not in suggestions:
            suggestions.append(label)
        if len(suggestions) >= limit:
            break
    return suggestions


def _group_match_score(group_row: dict[str, Any], key_aliases: set[str], requested_value: Any) -> int:
    group_texts = [
        group_row.get("groupLabel"),
        group_row.get("visibleGroupLabel"),
        group_row.get("normalizedGroupLabel"),
    ]
    source_hints = dict(group_row.get("sourceHints") or {})
    group_texts.extend(source_hints.values())
    backend_hints = dict(group_row.get("backendHints") or {})
    for hint_key in ["dataVarindex", "dataPrnumber", "dataPimvarnodekey"]:
        values = backend_hints.get(hint_key)
        if isinstance(values, list):
            group_texts.extend(values)
        elif values:
            group_texts.append(values)

    score = 0
    for alias in key_aliases:
        for text in group_texts:
            alias_score = _text_match_score(text, alias)
            if alias_score > 0:
                score = max(score, alias_score + 6)

    option_rows = list(group_row.get("options") or [])
    best_value_score = max((_catalog_option_score(row, requested_value) for row in option_rows), default=0)
    if score == 0 and best_value_score >= 20:
        score = 8
    elif score > 0 and best_value_score > 0:
        score += min(10, best_value_score // 3)
    return score


def _prevalidate_requested_options(
    options: dict[str, Any],
    bootstrap_artifacts: dict[str, Any],
    *,
    allow_sideload_ui_only: bool = False,
) -> dict[str, Any]:
    catalog_groups = _catalog_groups_from_bootstrap(bootstrap_artifacts)
    quantity_signal = dict(bootstrap_artifacts.get("quantity_signal") or {})
    available = bool(catalog_groups) or bool(quantity_signal.get("hasManualInput")) or bool(quantity_signal.get("presetValues"))
    result: dict[str, Any] = {
        "available": available,
        "valid": True,
        "matched": [],
        "unmatched": [],
        "warnings": [],
        "errors": [],
    }

    if not options:
        _augment_with_sideload(
            result,
            options,
            bootstrap_artifacts,
            allow_ui_only=allow_sideload_ui_only,
        )
        return result

    if not available:
        result["warnings"].append("option_catalog_unavailable")
        _augment_with_sideload(
            result,
            options,
            bootstrap_artifacts,
            allow_ui_only=allow_sideload_ui_only,
        )
        return result

    for key, requested_value in options.items():
        key_aliases = _option_key_aliases(key)
        requested_norm = _normalize_match_text(requested_value)
        key_norm = _normalize_match_text(key)
        canonical_quantity = "quantity" in key_aliases or key_norm == "quantity"

        if canonical_quantity:
            matched_quantity_control: dict[str, Any] | None = None
            numeric_value_re = re.compile(r"^\d+(?:[.,]\d+)?$")
            for group_row in catalog_groups:
                if _group_match_score(group_row, key_aliases, requested_value) <= 0:
                    continue
                # Require the option's current value to look numeric. Non-numeric
                # options (e.g. paper labels like "115 g/m² Bilderdruckpapier" that
                # now carry an enriched `name` attribute) must not be picked as
                # the quantity control; otherwise the synthesis adapter would
                # write the quantity number into the paper input_var field.
                numeric_options = [
                    option_row
                    for option_row in list(group_row.get("options") or [])
                    if str(option_row.get("name") or "").strip()
                    and numeric_value_re.match(str(option_row.get("visibleValue") or "").strip())
                ]
                if numeric_options:
                    matched_quantity_control = numeric_options[0]
                    break

            # Backstop: if the requested quantity isn't represented in any
            # catalog group (e.g. user types an off-preset value like 12345
            # while the configurator's presets jump 11000/12000/13000), pick
            # any catalog option whose name starts with `input_var_` and whose
            # visibleValue is numeric. All options of one quantity tile field
            # share the same form-field name, so the choice is structurally
            # equivalent — we just need a handle on the qty field so the
            # synthesis adapter can switch to interpolation mode.
            #
            # Print24-style fallback: a catalog row whose option's `name` is the
            # literal canonical key `"quantity"` (the box_name from
            # productDetails harvesting) IS the quantity control regardless of
            # the visible label format (Print24 renders "10 Stück" / "quantity 10",
            # not a bare number).
            if matched_quantity_control is None:
                for group_row in catalog_groups:
                    for option_row in list(group_row.get("options") or []):
                        name = str(option_row.get("name") or "").strip()
                        value = str(option_row.get("visibleValue") or "").strip()
                        if name == "quantity":
                            matched_quantity_control = option_row
                            break
                        if name.startswith("input_var_") and numeric_value_re.match(value):
                            matched_quantity_control = option_row
                            break
                    if matched_quantity_control is not None:
                        break

            quantity_field_name = str((matched_quantity_control or {}).get("name") or "")
            tile_signals = (
                quantity_tile_signals(catalog_groups, quantity_field_name)
                if quantity_field_name
                else {
                    "tilePresets": [],
                    "tileVarindexByValue": {},
                    "interpolationVarindex": "",
                    "supportsInterpolation": False,
                    "maxTilePreset": 0,
                }
            )

            if quantity_signal.get("hasManualInput", False):
                result["matched"].append(
                    {
                        "key": key,
                        "requestedValue": requested_value,
                        "groupLabel": "quantity",
                        "matchedLabel": str(requested_value),
                        "matchType": "quantity_manual",
                        "catalogOption": matched_quantity_control,
                        "quantityTileSignals": tile_signals,
                    }
                )
                continue

            preset_values = [str(value) for value in list(quantity_signal.get("presetValues") or []) if str(value)]
            if requested_norm in {_normalize_match_text(value) for value in preset_values}:
                result["matched"].append(
                    {
                        "key": key,
                        "requestedValue": requested_value,
                        "groupLabel": "quantity",
                        "matchedLabel": str(requested_value),
                        "matchType": "quantity_preset",
                        "catalogOption": matched_quantity_control,
                        "quantityTileSignals": tile_signals,
                    }
                )
                continue

            # Catalog-only quantity match (Print24-style): the DOM walker
            # gave us no `quantity_signal` (Print24 renders inside Next.js
            # client components the walker can't see), but the productDetails
            # harvest produced a catalog row whose option carries the
            # currently-captured prop_id under `backendHints.dataPropertyId`.
            # Honor the match so the synthesis adapter can use the prop_id;
            # the catalog only knows the currently-captured value, so any
            # OTHER quantity will still need a prop_id mapping the user
            # supplies explicitly (or a future `repo` probe).
            if matched_quantity_control and (
                str(matched_quantity_control.get("name") or "") == "quantity"
                or str((matched_quantity_control.get("backendHints") or {}).get("dataPropertyId") or "")
            ):
                result["matched"].append(
                    {
                        "key": key,
                        "requestedValue": requested_value,
                        "groupLabel": "quantity",
                        "matchedLabel": str(matched_quantity_control.get("visibleLabel") or requested_value),
                        "matchType": "catalog_only",
                        "catalogOption": matched_quantity_control,
                        "quantityTileSignals": tile_signals,
                    }
                )
                continue

            suggestions = preset_values[:3]
            result["valid"] = False
            result["unmatched"].append(
                {
                    "key": key,
                    "requestedValue": requested_value,
                    "reason": "quantity_not_in_catalog",
                    "suggestions": suggestions,
                }
            )
            result["errors"].append(
                f"required_option_not_in_catalog key={key} requested={requested_value}"
            )
            continue

        best_group: dict[str, Any] | None = None
        best_group_score = 0
        for group_row in catalog_groups:
            score = _group_match_score(group_row, key_aliases, requested_value)
            if score > best_group_score:
                best_group_score = score
                best_group = group_row

        if best_group is None or best_group_score <= 0:
            result["valid"] = False
            result["unmatched"].append(
                {
                    "key": key,
                    "requestedValue": requested_value,
                    "reason": "no_matching_group",
                    "suggestions": [],
                }
            )
            result["errors"].append(
                f"required_option_group_missing key={key} requested={requested_value}"
            )
            continue

        option_rows = list(best_group.get("options") or [])
        best_option: dict[str, Any] | None = None
        best_option_score = 0
        for option_row in option_rows:
            score = _catalog_option_score(option_row, requested_value)
            if score > best_option_score:
                best_option_score = score
                best_option = option_row

        if best_option is None or best_option_score < 12:
            suggestions = _best_suggestions(option_rows, requested_value)
            result["valid"] = False
            result["unmatched"].append(
                {
                    "key": key,
                    "requestedValue": requested_value,
                    "reason": "value_not_in_catalog",
                    "groupLabel": best_group.get("groupLabel"),
                    "suggestions": suggestions,
                }
            )
            result["errors"].append(
                f"required_option_value_missing key={key} requested={requested_value} group={best_group.get('groupLabel')}"
            )
            continue

        result["matched"].append(
            {
                "key": key,
                "requestedValue": requested_value,
                "groupLabel": best_group.get("groupLabel"),
                "matchedLabel": best_option.get("visibleLabel") or best_option.get("visibleValue") or str(requested_value),
                "matchType": "catalog_option",
                "catalogOption": best_option,
                "catalogGroup": {
                    "groupLabel": best_group.get("groupLabel"),
                    "normalizedGroupLabel": best_group.get("normalizedGroupLabel"),
                    "backendHints": dict(best_group.get("backendHints") or {}),
                    "groupAttributes": dict(best_group.get("groupAttributes") or {}),
                    "sourceHints": dict(best_group.get("sourceHints") or {}),
                },
            }
        )

    _augment_with_sideload(
        result,
        options,
        bootstrap_artifacts,
        allow_ui_only=allow_sideload_ui_only,
    )
    return result


def _augment_with_sideload(
    prevalidation: dict[str, Any],
    options: dict[str, Any],
    bootstrap_artifacts: dict[str, Any],
    *,
    allow_ui_only: bool,
) -> None:
    """Resolve each requested option against the sideloaded value-map and
    attach `sideloadResolution` to existing matched rows or promote previously
    unmatched rows. Records the loaded-map summary on `prevalidation` under
    `sideload` so the CLI can emit the [sideload-loaded] diagnostic without
    re-reading the file.
    """
    site_name = str(bootstrap_artifacts.get("site_name") or "").strip().lower()
    value_map = load_value_map(site_name) if site_name else None
    prevalidation["sideload"] = {
        "site": site_name,
        "summary": describe_value_map(value_map),
        "allowUiOnly": bool(allow_ui_only),
        "outcomes": [],
    }
    if not options or not value_map or value_map.get("_loadError"):
        return

    matched_by_key: dict[str, dict[str, Any]] = {
        str(row.get("key") or ""): row
        for row in prevalidation.get("matched", [])
        if isinstance(row, dict)
    }
    unmatched_index_by_key: dict[str, int] = {}
    for idx, row in enumerate(prevalidation.get("unmatched", [])):
        if isinstance(row, dict):
            unmatched_index_by_key[str(row.get("key") or "")] = idx

    for key, requested_value in options.items():
        canonical = _canonical_option_key(key)
        if not canonical:
            prevalidation["sideload"]["outcomes"].append(
                {"key": key, "canonicalKey": None, "status": "no_canonical_key"}
            )
            continue
        outcome = resolve_value_map_value(
            value_map,
            canonical,
            requested_value,
            allow_ui_only=allow_ui_only,
        )
        outcome_record = {
            "key": key,
            "canonicalKey": canonical,
            "status": outcome.get("status"),
            "propertyId": outcome.get("propertyId"),
            "backendId": outcome.get("backendId"),
            "matchedLabel": outcome.get("matchedLabel"),
            "confidence": outcome.get("confidence"),
            "productScope": outcome.get("productScope"),
            "sourceLabel": outcome.get("sourceLabel"),
            "propertyNote": outcome.get("propertyNote"),
            "candidates": outcome.get("candidates"),
            "candidateLabels": outcome.get("candidateLabels"),
        }
        prevalidation["sideload"]["outcomes"].append(outcome_record)

        if outcome.get("status") != "resolved":
            continue

        resolution = {
            "propertyId": outcome["propertyId"],
            "backendId": outcome["backendId"],
            "matchedLabel": outcome["matchedLabel"],
            "confidence": outcome["confidence"],
            "sourceLabel": outcome.get("sourceLabel"),
            "productScope": outcome.get("productScope"),
            "source": "sideload",
            "propertyNote": outcome.get("propertyNote"),
        }

        if key in matched_by_key:
            matched_by_key[key]["sideloadResolution"] = resolution
            continue

        promoted_row: dict[str, Any] = {
            "key": key,
            "requestedValue": requested_value,
            "groupLabel": outcome.get("sourceLabel") or canonical,
            "matchedLabel": outcome["matchedLabel"],
            "matchType": "sideload_only",
            "catalogOption": {
                "name": str(outcome["propertyId"]),
                "visibleLabel": outcome["matchedLabel"],
                "visibleValue": outcome["matchedLabel"],
                "backendHints": {
                    "dataPropertyId": outcome["propertyId"],
                    "backendId": outcome["backendId"],
                    "source": "sideload",
                },
            },
            "sideloadResolution": resolution,
        }
        prevalidation["matched"].append(promoted_row)
        matched_by_key[key] = promoted_row

        if key in unmatched_index_by_key:
            idx = unmatched_index_by_key[key]
            try:
                prevalidation["unmatched"].pop(idx)
            except IndexError:
                pass
            unmatched_index_by_key = {
                str(row.get("key") or ""): i
                for i, row in enumerate(prevalidation.get("unmatched", []))
                if isinstance(row, dict)
            }
            err_marker = f"key={key} requested={requested_value}"
            prevalidation["errors"] = [
                msg for msg in prevalidation.get("errors", []) if err_marker not in str(msg)
            ]
            if not prevalidation["unmatched"] and not prevalidation["errors"]:
                prevalidation["valid"] = True


def _recon_signature(bootstrap_artifacts: dict[str, Any]) -> dict[str, Any]:
    traces = list(bootstrap_artifacts.get("network_traces") or [])
    trace_urls = sorted({str(row.get("url") or "").strip() for row in traces if str(row.get("url") or "").strip()})
    option_catalog = _catalog_groups_from_bootstrap(bootstrap_artifacts)
    catalog_payload = [
        {
            "groupLabel": row.get("groupLabel") or row.get("group"),
            "normalizedGroupLabel": row.get("normalizedGroupLabel"),
            "options": [
                {
                    "visibleLabel": option.get("visibleLabel"),
                    "visibleValue": option.get("visibleValue"),
                    "name": option.get("name"),
                    "backendHints": option.get("backendHints"),
                }
                for option in list(row.get("options") or [])[:24]
            ],
        }
        for row in option_catalog[:24]
    ]
    request_templates = dict(bootstrap_artifacts.get("request_templates") or {})
    template_payload = {
        "currentSetLink": request_templates.get("currentSetLink"),
        "hiddenInputs": list(request_templates.get("hiddenInputs") or [])[:24],
        "forms": list(request_templates.get("forms") or [])[:12],
    }
    return {
        "traceCount": len(traces),
        "traceUrlCount": len(trace_urls),
        "traceFingerprint": hashlib.sha1(json.dumps(trace_urls, ensure_ascii=True).encode("utf-8")).hexdigest()[:12],
        "optionCatalogGroupCount": len(option_catalog),
        "optionCatalogFingerprint": hashlib.sha1(
            json.dumps(catalog_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12],
        "templatePresent": bool(request_templates),
        "templateFingerprint": hashlib.sha1(
            json.dumps(template_payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12],
        "hiddenInputCount": len(list(request_templates.get("hiddenInputs") or [])),
    }


def _build_drift_report(
    previous_bootstrap: dict[str, Any],
    current_bootstrap: dict[str, Any],
    refresh_reason: str | None,
) -> dict[str, Any]:
    previous = _recon_signature(previous_bootstrap)
    current = _recon_signature(current_bootstrap)
    changed_fields = sorted(
        {
            key
            for key in previous.keys() | current.keys()
            if previous.get(key) != current.get(key)
        }
    )
    material_change = bool(changed_fields)
    return {
        "compared": True,
        "refreshReason": refresh_reason,
        "verdict": "material_change" if material_change else "no_material_change",
        "materialChange": material_change,
        "changedFields": changed_fields,
        "previous": previous,
        "current": current,
    }


def _default_drift_report(bootstrap_artifacts: dict[str, Any]) -> dict[str, Any]:
    recon_cache = dict(bootstrap_artifacts.get("recon_cache") or {})
    if recon_cache.get("hit"):
        verdict = "cache_reused"
    elif recon_cache.get("refreshed"):
        verdict = "fresh_recon"
    else:
        verdict = "not_available"
    return {
        "compared": False,
        "refreshReason": bootstrap_artifacts.get("recon_refresh_reason"),
        "verdict": verdict,
        "materialChange": False,
        "changedFields": [],
        "current": _recon_signature(bootstrap_artifacts),
    }


def _should_refresh_cached_recon(summary: dict[str, Any]) -> str | None:
    recon_cache = dict(summary.get("recon_cache") or {})
    if not recon_cache.get("hit"):
        return None

    request_only = bool(summary.get("request_only", False))

    prevalidation = dict(summary.get("option_prevalidation") or {})
    if (not request_only) and prevalidation.get("available") and not prevalidation.get("valid", True):
        return "catalog_prevalidation_failed"

    http_replay = dict(summary.get("http_replay") or {})
    replay_reason = str(http_replay.get("reason") or "").strip()
    if replay_reason in {"missing_endpoint_candidates", "all_http_candidates_failed"}:
        return replay_reason

    attempts = list(http_replay.get("attempts") or [])
    for row in attempts:
        reason = str(row.get("reason") or "").strip()
        if reason in {"no_matching_trace", "request_error"}:
            return reason
        content_type = str(row.get("contentType") or "").lower()
        if "html" in content_type and str(row.get("result") or "") != "success":
            return "html_response_without_price"

    extraction_reason = str(summary.get("extraction_reason") or "").strip()
    if extraction_reason in {"price_not_extracted", "request_only_http_replay_failed", "prevalidation_failed"}:
        return extraction_reason

    mismatches = {str(row) for row in list(summary.get("mismatches") or [])}
    if "price_not_extracted" in mismatches:
        return "price_not_extracted"

    return None


def parse_manifest_file(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and isinstance(payload.get("targets"), list):
        entries = payload["targets"]
    else:
        raise ValueError("--manifest-file must contain a JSON array or {\"targets\": [...]} object")

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each manifest entry must be a JSON object")
        if not str(entry.get("url", "")).strip():
            raise ValueError("Each manifest entry must include a non-empty 'url'")
        normalized.append(entry)
    return normalized


def parse_optional_manifest_file(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    return parse_manifest_file(path)


def _read_text_with_fallback(path: Path) -> str:
    for encoding in ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize_csv_key(raw: str) -> str:
    text = str(raw or "").strip().lower().replace("-", "_")
    return re.sub(r"\s+", "_", text)


def parse_manifest_csv(
    path: str | None,
    *,
    default_url: str | None = None,
    default_site_name: str | None = None,
    default_product_type: str | None = None,
) -> list[dict[str, Any]]:
    if path is None:
        return []

    csv_path = Path(path)
    csv_text = _read_text_with_fallback(csv_path)
    sample = csv_text[:4096]

    delimiter = ","
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = str(getattr(sniffed, "delimiter", ",") or ",")
    except Exception:
        if sample.count(";") > sample.count(","):
            delimiter = ";"

    reader = csv.DictReader(StringIO(csv_text), delimiter=delimiter)
    if not reader.fieldnames:
        return []

    default_url_text = str(default_url or "").strip()
    default_site_text = str(default_site_name or "").strip()
    default_product_text = str(default_product_type or "").strip()

    reserved_keys = {
        "url",
        "site_name",
        "product_type",
        "type",
        "expected_currency",
        "expected_price",
        "expected_price_tolerance",
        "options",
        "options_json",
        "uid",
        "id",
        "row_id",
        "net_price",
        "gross_price",
        "price",
        "currency",
    }

    normalized: list[dict[str, Any]] = []
    for row_index, row in enumerate(reader, start=2):
        row_data = {str(key or "").strip(): value for key, value in dict(row or {}).items()}
        lowered = {
            _normalize_csv_key(key): value
            for key, value in row_data.items()
        }

        url = str(row_data.get("url") or lowered.get("url") or "").strip() or default_url_text
        if not url:
            raise ValueError(
                f"CSV row {row_index} must include a non-empty 'url' or use --manifest-csv-url"
            )

        options: dict[str, Any] = {}
        options_json_raw = str(
            row_data.get("options_json")
            or row_data.get("options")
            or lowered.get("options_json")
            or lowered.get("options")
            or ""
        ).strip()
        if options_json_raw:
            try:
                parsed = json.loads(options_json_raw)
            except Exception as exc:
                raise ValueError(f"CSV row {row_index} has invalid options_json: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"CSV row {row_index} options_json must be a JSON object")
            options.update(parsed)

        for key, raw_value in row_data.items():
            option_key = ""
            if key.startswith("option."):
                option_key = key.split(".", 1)[1].strip()
            else:
                normalized_key = _normalize_csv_key(key)
                if normalized_key not in reserved_keys:
                    option_key = str(key).strip()

            if not option_key:
                continue
            value_text = str(raw_value or "").strip()
            if value_text == "":
                continue
            options[option_key] = _coerce_option_value(value_text)

        expected_price: float | None = None
        expected_price_raw = str(
            row_data.get("expected_price")
            or lowered.get("expected_price")
            or row_data.get("Net price")
            or lowered.get("net_price")
            or ""
        ).strip()
        if expected_price_raw:
            try:
                expected_price = float(expected_price_raw)
            except ValueError:
                expected_price = None

        expected_price_tolerance = 0.01
        tolerance_raw = str(
            row_data.get("expected_price_tolerance")
            or lowered.get("expected_price_tolerance")
            or ""
        ).strip()
        if tolerance_raw:
            try:
                expected_price_tolerance = float(tolerance_raw)
            except ValueError as exc:
                raise ValueError(
                    f"CSV row {row_index} has invalid expected_price_tolerance: {tolerance_raw}"
                ) from exc

        site_name = str(
            row_data.get("site_name")
            or lowered.get("site_name")
            or default_site_text
            or ""
        ).strip()
        product_type = str(
            row_data.get("product_type")
            or lowered.get("product_type")
            or row_data.get("Type")
            or lowered.get("type")
            or default_product_text
            or "unknown"
        ).strip() or "unknown"

        expected_currency = str(
            row_data.get("expected_currency")
            or lowered.get("expected_currency")
            or "EUR"
        ).strip() or "EUR"

        normalized.append(
            {
                "url": url,
                "site_name": site_name or None,
                "product_type": product_type,
                "expected_currency": expected_currency,
                "expected_price": expected_price,
                "expected_price_tolerance": expected_price_tolerance,
                "options": options,
            }
        )

    return normalized


def build_target_spec_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "url": args.url,
        "site_name": args.site_name,
        "product_type": args.product_type,
        "expected_price": args.expected_price,
        "expected_price_tolerance": args.expected_price_tolerance,
        "expected_currency": args.expected_currency,
        "options": {},
    }


def _target_from_bootstrap_artifacts(
    spec: dict[str, Any],
    args: argparse.Namespace,
    url: str,
    options: dict[str, Any],
    bootstrap_artifacts: dict[str, Any],
) -> TargetInput:
    expected_currency = str(spec.get("expected_currency") or "EUR")
    expected_price_tolerance = float(spec.get("expected_price_tolerance") or 0.01)
    proxy_pool = load_proxy_pool(args.proxy_file, args.proxy_url)

    return TargetInput(
        site_name=str(spec.get("site_name") or bootstrap_artifacts.get("site_name") or "unknown-site"),
        product_url=url,
        product_type=str(spec.get("product_type") or "unknown"),
        observed_requests=list(bootstrap_artifacts.get("observed_requests") or []),
        expected_price=spec.get("expected_price"),
        expected_price_tolerance=expected_price_tolerance,
        expected_currency=expected_currency,
        options=options,
        network_traces=list(bootstrap_artifacts.get("network_traces") or []),
        bootstrap_signals={
            "token_indicators": list(bootstrap_artifacts.get("token_indicators") or []),
            "requires_session": bool(bootstrap_artifacts.get("requires_session", False)),
            "anti_bot_suspected": bool(bootstrap_artifacts.get("anti_bot_suspected", False)),
            "cookie_names": list(bootstrap_artifacts.get("cookie_names") or []),
            "cookies": dict(bootstrap_artifacts.get("cookies") or {}),
            "option_groups": list(bootstrap_artifacts.get("option_groups") or []),
            "option_catalog": list(bootstrap_artifacts.get("option_catalog") or []),
            "option_dependencies": list(bootstrap_artifacts.get("option_dependencies") or []),
            "dependency_probe": dict(bootstrap_artifacts.get("dependency_probe") or {}),
            "option_application": dict(bootstrap_artifacts.get("option_application") or {}),
            "option_prevalidation": dict(bootstrap_artifacts.get("option_prevalidation") or {}),
            "option_url_variants": list(bootstrap_artifacts.get("option_url_variants") or []),
            "request_templates": dict(bootstrap_artifacts.get("request_templates") or {}),
            "quantity_signal": dict(bootstrap_artifacts.get("quantity_signal") or {}),
            "dom_price_candidates_before": list(bootstrap_artifacts.get("dom_price_candidates_before") or []),
            "dom_price_candidates_after": list(bootstrap_artifacts.get("dom_price_candidates_after") or []),
            "dom_price_candidates": list(bootstrap_artifacts.get("dom_price_candidates") or []),
            "selected_configuration": dict(bootstrap_artifacts.get("selected_configuration") or {}),
            "selected_configuration_rows": list(bootstrap_artifacts.get("selected_configuration_rows") or []),
            "price_summary_lines": list(bootstrap_artifacts.get("price_summary_lines") or []),
            "consent_state": dict(bootstrap_artifacts.get("consent_state") or {}),
            "recon_cache": dict(bootstrap_artifacts.get("recon_cache") or {}),
            "recon_refresh_reason": bootstrap_artifacts.get("recon_refresh_reason"),
            "drift_report": dict(bootstrap_artifacts.get("drift_report") or {}),
            "allow_heuristic_fallback": bool(args.allow_heuristic_fallback),
            "require_matched_options": bool(args.require_matched_options),
            "request_only": bool(getattr(args, "request_only", False)),
            "json_adapters_dir": str(getattr(args, "json_adapters_dir", "") or "").strip(),
            "site_adapters_dir": str(getattr(args, "site_adapters_dir", "") or "").strip(),
            "allow_generated_site_adapters": bool(getattr(args, "allow_generated_site_adapters", False)),
            "http_runtime": {
                "timeout_seconds": max(float(getattr(args, "http_request_timeout_seconds", 15.0) or 15.0), 1.0),
                "min_delay_ms": max(int(getattr(args, "http_min_delay_ms", 0) or 0), 0),
                "jitter_ms": max(int(getattr(args, "http_jitter_ms", 0) or 0), 0),
                "max_retries": max(int(getattr(args, "http_max_retries", 0) or 0), 0),
                "backoff_base_ms": max(int(getattr(args, "http_backoff_base_ms", 400) or 400), 0),
                "backoff_max_ms": max(int(getattr(args, "http_backoff_max_ms", 5000) or 5000), 0),
                "proxy_failure_threshold": max(int(getattr(args, "proxy_failure_threshold", 3) or 3), 1),
                "proxy_cooldown_seconds": max(int(getattr(args, "proxy_cooldown_seconds", 300) or 300), 1),
                "proxy_rotation": str(getattr(args, "proxy_rotation", "none") or "none"),
                "proxy_pool": proxy_pool,
            },
        },
    )


def build_target_input(
    spec: dict[str, Any],
    args: argparse.Namespace,
    store: KnowledgeStore,
    force_recon_refresh: bool = False,
    recon_refresh_reason: str | None = None,
) -> tuple[TargetInput, BootstrapArtifacts]:
    url = str(spec.get("url", "")).strip()
    if not url:
        raise ValueError("Target spec must contain a non-empty 'url'")

    options = compose_options(spec, args)

    cached_snapshot: dict[str, Any] | None = None
    bootstrap_artifacts: BootstrapArtifacts | None = None

    use_cached_recon = not (args.force_recon_refresh or force_recon_refresh)
    if use_cached_recon:
        cached_snapshot = store.get_recon_snapshot(url)
        if cached_snapshot is not None:
            bootstrap_artifacts = normalize_bootstrap_artifacts(
                dict(cached_snapshot.get("bootstrap_artifacts") or {})
            )
            bootstrap_artifacts["recon_cache"] = {
                "hit": True,
                "cache_key": cached_snapshot.get("cache_key"),
                "captured_at": cached_snapshot.get("captured_at"),
                "expires_at": cached_snapshot.get("expires_at"),
            }

    if bootstrap_artifacts is None:
        if args.verbose:
            print(f"[recon-cache] miss url={url} refreshing_recon=true")

        request_only = bool(getattr(args, "request_only", False))
        requested_options = {} if request_only else options
        dependency_probe_steps = 0 if request_only else args.max_dependency_probe_steps

        bootstrap = bootstrap_product_url(
            product_url=url,
            headless=not args.headed,
            timeout_ms=args.bootstrap_timeout_ms,
            max_observed_requests=args.max_observed_requests,
            dependency_probe_steps=dependency_probe_steps,
            requested_options=requested_options,
            auto_accept_cookies=not args.disable_auto_accept_cookies,
            debug_hold_seconds=args.headed_debug_hold_seconds,
        )

        if bootstrap.warnings:
            for warning in bootstrap.warnings:
                print(f"[bootstrap-warning] {url} :: {warning}")

        bootstrap_artifacts = bootstrap_result_to_artifacts(bootstrap)

        store.save_recon_snapshot(
            site_name=str(spec.get("site_name") or bootstrap.site_name),
            product_url=url,
            bootstrap_artifacts=bootstrap_artifacts,
            ttl_days=args.recon_ttl_days,
        )
        refreshed_snapshot = store.get_recon_snapshot(url)
        bootstrap_artifacts["recon_cache"] = {
            "hit": False,
            "refreshed": True,
            "cache_key": (refreshed_snapshot or {}).get("cache_key", ""),
            "captured_at": (refreshed_snapshot or {}).get("captured_at", ""),
            "expires_at": (refreshed_snapshot or {}).get("expires_at", ""),
            "ttl_days": int(args.recon_ttl_days),
        }
    elif args.verbose:
        recon_cache = dict(bootstrap_artifacts.get("recon_cache") or {})
        print(
            "[recon-cache] "
            f"hit=true url={url} captured_at={recon_cache.get('captured_at')} "
            f"expires_at={recon_cache.get('expires_at')}"
        )

    if dict(bootstrap_artifacts.get("recon_cache") or {}).get("hit"):
        bootstrap_artifacts["option_application"] = {
            "requestedCount": len(options),
            "matched": [],
            "unmatched": [],
            "strictPassMatched": 0,
            "relaxedPassMatched": 0,
            "fromCachedRecon": True,
        }

    bootstrap_artifacts["option_prevalidation"] = _prevalidate_requested_options(
        options,
        bootstrap_artifacts,
        allow_sideload_ui_only=bool(getattr(args, "allow_sideload_ui_only", False)),
    )
    sideload_info = dict(
        (bootstrap_artifacts.get("option_prevalidation") or {}).get("sideload") or {}
    )
    sideload_summary = dict(sideload_info.get("summary") or {})
    if args.verbose and sideload_summary.get("loaded"):
        print(
            "[sideload-loaded] "
            + json.dumps(
                {
                    "site": sideload_summary.get("site"),
                    "path": sideload_summary.get("path"),
                    "schemaVersion": sideload_summary.get("schemaVersion"),
                    "captureMethod": sideload_summary.get("captureMethod"),
                    "generatedAt": sideload_summary.get("generatedAt"),
                    "propertyCount": sideload_summary.get("propertyCount"),
                    "canonicalKeys": sideload_summary.get("canonicalKeys"),
                    "allowUiOnly": sideload_info.get("allowUiOnly", False),
                    "loadError": sideload_summary.get("loadError"),
                },
                ensure_ascii=False,
            )
        )
    bootstrap_artifacts["recon_refresh_reason"] = recon_refresh_reason
    bootstrap_artifacts.setdefault("drift_report", {})
    target = _target_from_bootstrap_artifacts(spec, args, url, options, bootstrap_artifacts)
    return target, bootstrap_artifacts


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "unknown"


def _print_verbose_bootstrap(url: str, bootstrap_artifacts: dict[str, Any]) -> None:
    option_groups = list(bootstrap_artifacts.get("option_groups") or [])
    option_catalog = list(bootstrap_artifacts.get("option_catalog") or [])
    option_dependencies = list(bootstrap_artifacts.get("option_dependencies") or [])
    dependency_probe = dict(bootstrap_artifacts.get("dependency_probe") or {})
    option_application = dict(bootstrap_artifacts.get("option_application") or {})
    option_prevalidation = dict(bootstrap_artifacts.get("option_prevalidation") or {})
    option_url_variants = list(bootstrap_artifacts.get("option_url_variants") or [])
    consent_state = dict(bootstrap_artifacts.get("consent_state") or {})
    quantity_signal = dict(bootstrap_artifacts.get("quantity_signal") or {})
    selected_configuration = dict(bootstrap_artifacts.get("selected_configuration") or {})
    selected_configuration_rows = list(bootstrap_artifacts.get("selected_configuration_rows") or [])
    price_summary_lines = list(bootstrap_artifacts.get("price_summary_lines") or [])

    print(
        "[bootstrap] "
        f"url={url} requests={len(bootstrap_artifacts.get('observed_requests', []))} "
        f"traces={len(bootstrap_artifacts.get('network_traces', []))} "
        f"requires_session={bootstrap_artifacts.get('requires_session', False)} "
        f"anti_bot={bootstrap_artifacts.get('anti_bot_suspected', False)} "
        f"tokens={bootstrap_artifacts.get('token_indicators', [])}"
    )
    if option_groups:
        preview = [
            {
                "group": g.get("group"),
                "optionCount": g.get("optionCount"),
                "controlTypes": g.get("controlTypes"),
            }
            for g in option_groups[:8]
        ]
        print(f"[bootstrap-options] groups={len(option_groups)} preview={json.dumps(preview, ensure_ascii=True)}")
    else:
        print("[bootstrap-options] groups=0")

    if option_catalog:
        catalog_preview = [
            {
                "group": row.get("groupLabel"),
                "capturedOptionCount": len(list(row.get("options") or [])),
                "selectedOptionCount": row.get("selectedOptionCount"),
                "truncated": row.get("truncated", False),
            }
            for row in option_catalog[:8]
        ]
        print(f"[bootstrap-option-catalog] groups={len(option_catalog)} preview={json.dumps(catalog_preview, ensure_ascii=True)}")

    if option_dependencies:
        print(
            f"[bootstrap-dependencies] inferred_edges={len(option_dependencies)} "
            f"preview={json.dumps(option_dependencies[:8], ensure_ascii=True)}"
        )
    active_probe_edges = list(dependency_probe.get("activeProbeDependencies") or [])
    if active_probe_edges:
        print(
            f"[bootstrap-active-probe] edges={len(active_probe_edges)} "
            f"preview={json.dumps(active_probe_edges[:8], ensure_ascii=True)}"
        )

    if quantity_signal:
        print(f"[bootstrap-quantity] {json.dumps(quantity_signal, ensure_ascii=True)}")

    if consent_state:
        print(
            "[bootstrap-consent] "
            f"attempted={consent_state.get('attempted', False)} "
            f"banner_detected={consent_state.get('bannerDetected', False)} "
            f"clicked={consent_state.get('clicked', False)} "
            f"deferred={consent_state.get('deferred', False)} "
            f"selector={consent_state.get('matchedSelector')} "
            f"text={consent_state.get('matchedText')}"
        )
        retry_attempts = list(consent_state.get("retryAttempts") or [])
        if retry_attempts:
            print(
                f"[bootstrap-consent-retry] count={len(retry_attempts)} "
                f"any_clicked={any(a.get('clicked') for a in retry_attempts)} "
                f"any_banner={any(a.get('bannerDetected') for a in retry_attempts)}"
            )
        readiness_info = consent_state.get("readiness") or {}
        if readiness_info:
            print(
                "[bootstrap-readiness] "
                f"reason={readiness_info.get('reason')} "
                f"waited_ms={readiness_info.get('waitedMs')} "
                f"max_wait_ms={readiness_info.get('maxWaitMs')} "
                f"interactive_count={readiness_info.get('interactiveCount')} "
                f"api_hits={readiness_info.get('apiHits')} "
                f"consent_retries={readiness_info.get('consentRetriesAttempted', 0)}"
            )
        label_probe = consent_state.get("labelProbe") or {}
        if label_probe:
            print(
                "[bootstrap-label-probe] "
                f"groups_detected={len(label_probe.get('groupsDetected') or [])} "
                f"clicks_performed={len(label_probe.get('clicksPerformed') or [])} "
                f"request_delta={label_probe.get('requestCountDelta', 0)} "
                f"pricing_api_delta={label_probe.get('pricingApiHitsDelta', 0)} "
                f"error={label_probe.get('error')}"
            )
            groups = list(label_probe.get("groupsDetected") or [])
            if groups:
                print(f"[bootstrap-label-probe-groups] {json.dumps(groups[:8], ensure_ascii=True)}")
            clicks = list(label_probe.get("clicksPerformed") or [])
            if clicks:
                print(f"[bootstrap-label-probe-clicks] {json.dumps(clicks[:8], ensure_ascii=True)}")

    if option_application:
        matched = list(option_application.get("matched") or [])
        unmatched = list(option_application.get("unmatched") or [])
        print(
            "[bootstrap-option-apply] "
            f"requested={option_application.get('requestedCount', 0)} "
            f"matched={len(matched)} unmatched={len(unmatched)} "
            f"strict_matched={option_application.get('strictPassMatched', 0)} "
            f"relaxed_matched={option_application.get('relaxedPassMatched', 0)} "
            f"triggered_requests={option_application.get('triggeredRequestCount', 0)}"
        )
        if matched:
            print(f"[bootstrap-option-apply-matched] {json.dumps(matched[:8], ensure_ascii=True)}")
        if unmatched:
            print(f"[bootstrap-option-apply-unmatched] {json.dumps(unmatched[:8], ensure_ascii=True)}")
    if option_prevalidation.get("available"):
        print(
            "[bootstrap-option-prevalidation] "
            f"valid={option_prevalidation.get('valid', True)} "
            f"matched={len(option_prevalidation.get('matched') or [])} "
            f"unmatched={len(option_prevalidation.get('unmatched') or [])}"
        )
        if option_prevalidation.get("warnings"):
            print(f"[bootstrap-option-prevalidation-warnings] {json.dumps(option_prevalidation.get('warnings', [])[:8], ensure_ascii=True)}")
        if option_prevalidation.get("errors"):
            print(f"[bootstrap-option-prevalidation-errors] {json.dumps(option_prevalidation.get('errors', [])[:8], ensure_ascii=True)}")
    if option_url_variants:
        print(f"[bootstrap-option-url-variants] {json.dumps(option_url_variants[:12], ensure_ascii=True)}")

    dom_prices_before = list(bootstrap_artifacts.get("dom_price_candidates_before") or [])
    dom_prices_after = list(bootstrap_artifacts.get("dom_price_candidates_after") or [])
    if dom_prices_before:
        print(f"[bootstrap-dom-price-before] candidates={json.dumps(dom_prices_before[:15], ensure_ascii=True)}")
    if dom_prices_after:
        print(f"[bootstrap-dom-price-after] candidates={json.dumps(dom_prices_after[:15], ensure_ascii=True)}")

    dom_prices = list(bootstrap_artifacts.get("dom_price_candidates") or [])
    if dom_prices:
        print(f"[bootstrap-dom-price] candidates={json.dumps(dom_prices[:15], ensure_ascii=True)}")

    recon_cache = dict(bootstrap_artifacts.get("recon_cache") or {})
    if recon_cache:
        print(
            "[recon-cache] "
            f"hit={recon_cache.get('hit', False)} "
            f"captured_at={recon_cache.get('captured_at')} "
            f"expires_at={recon_cache.get('expires_at')}"
        )

    if selected_configuration:
        print(f"[bootstrap-selected-config] groups={len(selected_configuration)} values={json.dumps(selected_configuration, ensure_ascii=True)}")
    if selected_configuration_rows:
        print(f"[bootstrap-selected-config-rows] count={len(selected_configuration_rows)} values={json.dumps(selected_configuration_rows[:60], ensure_ascii=True)}")
    if price_summary_lines:
        print(f"[bootstrap-price-summary] lines={len(price_summary_lines)} values={json.dumps(price_summary_lines[:40], ensure_ascii=True)}")


def _augment_summary_with_bootstrap(
    summary: dict[str, Any],
    target: TargetInput,
    bootstrap_artifacts: dict[str, Any],
) -> dict[str, Any]:
    summary["recon_cache"] = dict(target.bootstrap_signals.get("recon_cache") or {})
    summary["recon_refresh_reason"] = target.bootstrap_signals.get("recon_refresh_reason")
    drift_report = dict(target.bootstrap_signals.get("drift_report") or bootstrap_artifacts.get("drift_report") or {})
    if not drift_report:
        drift_report = _default_drift_report(bootstrap_artifacts)
    summary["drift_report"] = drift_report
    summary["drift_verdict"] = drift_report.get("verdict", "not_available")
    option_prevalidation = dict(target.bootstrap_signals.get("option_prevalidation") or {})
    if option_prevalidation:
        summary["option_prevalidation"] = _compact_prevalidation(option_prevalidation)
    return summary


def _build_prevalidation_failure_summary(
    target: TargetInput,
    bootstrap_artifacts: dict[str, Any],
) -> dict[str, Any]:
    prevalidation = dict(target.bootstrap_signals.get("option_prevalidation") or {})
    unmatched = list(prevalidation.get("unmatched") or [])
    mismatches = [
        f"required_options_unmatched key={row.get('key')} requested={row.get('requestedValue')} reason={row.get('reason')}"
        for row in unmatched
    ]
    if not mismatches:
        mismatches = list(prevalidation.get("errors") or ["required_options_unmatched"])

    summary = {
        "site": target.site_name,
        "url": target.product_url,
        "final_status": "failed",
        "request_only": bool(target.bootstrap_signals.get("request_only", False)),
        "attempts": 0,
        "feasible": True,
        "complexity": None,
        "strategy": "prevalidation_failed",
        "validated": False,
        "price": None,
        "currency": target.expected_currency,
        "mismatches": mismatches,
        "failures": [],
        "quantity_behavior_hint": dict(bootstrap_artifacts.get("quantity_signal") or {}).get("mode", "unknown"),
        "option_group_count": len(list(bootstrap_artifacts.get("option_groups") or [])),
        "dependency_edge_count": len(list(bootstrap_artifacts.get("option_dependencies") or [])),
        "active_dependency_edge_count": len(
            list(dict(bootstrap_artifacts.get("dependency_probe") or {}).get("activeProbeDependencies") or [])
        ),
        "has_quantity_threshold_behavior": bool(dict(bootstrap_artifacts.get("quantity_signal") or {}).get("hasThresholdBehavior", False)),
        "has_manual_quantity_input": bool(dict(bootstrap_artifacts.get("quantity_signal") or {}).get("hasManualInput", False)),
        "has_preset_quantities": bool(dict(bootstrap_artifacts.get("quantity_signal") or {}).get("presetValues")),
        "option_requested_count": len(target.options),
        "option_matched_count": len(list(prevalidation.get("matched") or [])),
        "option_unmatched_count": len(unmatched),
        "option_matched": [],
        "option_unmatched": unmatched,
        "replay_mode": "none",
        "fallback_mode": "none",
        "http_replay": {},
        "extraction_reason": "prevalidation_failed",
        "named_prices": {},
        "price_candidates": [],
        "price_candidates_raw": [],
        "price_candidate_groups": {},
        "price_candidate_group_counts": {},
        "site_reference_price": None,
        "site_reference_source": None,
        "site_reference_kind": "none",
        "price_vs_site_delta": None,
        "price_vs_site_within_tolerance": None,
    }
    return _augment_summary_with_bootstrap(summary, target, bootstrap_artifacts)


def _print_verbose_outcome(summary: dict[str, Any], final_state: RunState) -> None:
    observation = final_state.observation
    plan = final_state.plan
    validation = final_state.validation
    extraction = final_state.extraction

    if observation is not None:
        print(
            "[discovery] "
            f"api_calls={observation.has_api_calls} script_heavy={observation.has_script_heavy_ui} "
            f"payload_score={observation.payload_signal_score} response_score={observation.response_signal_score} "
            f"option_groups={observation.option_group_count} dep_edges={observation.dependency_edge_count} "
            f"active_dep_edges={observation.active_dependency_edge_count} quantity_hint={observation.quantity_behavior_hint}"
        )
        if observation.endpoint_rankings:
            print("[endpoint-candidates] top-ranked endpoints")
            for idx, row in enumerate(observation.endpoint_rankings[:8], start=1):
                print(
                    "  "
                    f"{idx:02d}. score={row.get('score')} role={row.get('role')} conf={row.get('confidence')} "
                    f"method={row.get('method')} status={row.get('status')} url={row.get('url')}"
                )

    if plan is not None:
        print(
            "[plan] "
            f"strategy={plan.strategy.value} endpoint={plan.endpoint} confidence={plan.confidence} "
            f"notes={plan.notes}"
        )

    if extraction is not None:
        response_summary = extraction.raw_response_summary if isinstance(extraction.raw_response_summary, dict) else {}
        print(
            "[execution] "
            f"success={extraction.success} price={extraction.price_value} currency={extraction.currency} "
            f"replay_mode={summary.get('replay_mode')} fallback_mode={summary.get('fallback_mode')} "
            f"reason={summary.get('extraction_reason')} "
            f"price_source={response_summary.get('priceSource')} price_score={response_summary.get('priceScore')}"
        )
        adapter_diag = dict(response_summary.get("adapterDiagnostics") or {})
        if adapter_diag:
            print(
                "[adapter] "
                f"loaded={adapter_diag.get('loadedCount')} matched={adapter_diag.get('matchedAdapterId')} "
                f"reason={adapter_diag.get('reason')} warnings={len(list(adapter_diag.get('warnings') or []))}"
            )
        named_prices = dict(response_summary.get("namedPrices") or {})
        if named_prices:
            print(f"[execution-named-prices] {json.dumps(named_prices, ensure_ascii=True)}")
        reduced_price_candidates = list(response_summary.get("priceCandidates") or [])
        if reduced_price_candidates:
            print(
                f"[execution-prices-reduced] count={len(reduced_price_candidates)} "
                f"values={json.dumps(reduced_price_candidates, ensure_ascii=True)}"
            )
        all_price_candidates = list(response_summary.get("priceCandidatesRaw") or reduced_price_candidates)
        if all_price_candidates:
            print(f"[execution-all-prices] count={len(all_price_candidates)} values={json.dumps(all_price_candidates, ensure_ascii=True)}")
        grouped_candidates = dict(response_summary.get("priceCandidateGroups") or {})
        if grouped_candidates:
            group_counts = {
                "top_level_total": len(grouped_candidates.get("top_level_total") or []),
                "component": len(grouped_candidates.get("component") or []),
                "shipping": len(grouped_candidates.get("shipping") or []),
                "other": len(grouped_candidates.get("other") or []),
            }
            print(f"[execution-price-groups] counts={json.dumps(group_counts, ensure_ascii=True)}")
        web_selected = dict(final_state.target.bootstrap_signals.get("selected_configuration") or {})
        if web_selected:
            print(f"[config-web-selected] {json.dumps(web_selected, ensure_ascii=True)}")
        response_snapshot = dict(response_summary.get("configurationSnapshot") or {})
        if response_snapshot:
            print(f"[config-response-snapshot] {json.dumps(response_snapshot, ensure_ascii=True)}")
        effective_requested = dict(response_summary.get("effectiveRequestedOptions") or {})
        if effective_requested:
            print(f"[config-effective-requested] {json.dumps(effective_requested, ensure_ascii=True)}")
        normalization_applied = list(response_summary.get("normalizationAppliedRules") or [])
        if normalization_applied:
            print(f"[config-normalization-applied] {json.dumps(normalization_applied, ensure_ascii=True)}")
        learned_mappings = list(response_summary.get("learnedNormalizationMappings") or [])
        if learned_mappings:
            print(f"[config-normalization-learned] {json.dumps(learned_mappings, ensure_ascii=True)}")
        promoted_mappings = list(response_summary.get("promotedNormalizationMappings") or [])
        if promoted_mappings:
            print(f"[config-normalization-promoted] {json.dumps(promoted_mappings, ensure_ascii=True)}")
        request_template_applied = dict(response_summary.get("requestTemplateApplied") or {})
        if request_template_applied:
            print(f"[execution-request-template] {json.dumps(request_template_applied, ensure_ascii=True)}")
            skipped_injections = list(request_template_applied.get("skipped") or [])
            if skipped_injections:
                print(
                    "[adapter-injection-skipped] "
                    + json.dumps(
                        {"count": len(skipped_injections), "entries": skipped_injections},
                        ensure_ascii=True,
                    )
                )
            unsupported_inputs = list(request_template_applied.get("unsupportedInputs") or [])
            if unsupported_inputs:
                print(
                    "[adapter-injection-unsupported] "
                    + json.dumps(
                        {
                            "count": len(unsupported_inputs),
                            "entries": unsupported_inputs,
                            "note": "These --option keys were supplied but no inject/synthesis rule covered them; the request was sent with the template default for those fields.",
                        },
                        ensure_ascii=True,
                    )
                )
            sideload_applied_entries = list(request_template_applied.get("sideloadApplied") or [])
            if sideload_applied_entries:
                print(
                    "[adapter-injection-applied-sideload] "
                    + json.dumps(
                        {
                            "count": len(sideload_applied_entries),
                            "entries": sideload_applied_entries,
                        },
                        ensure_ascii=False,
                    )
                )
        request_payload = dict(response_summary.get("requestPayload") or {})
        if request_payload:
            print(f"[execution-request-payload] {json.dumps(request_payload, ensure_ascii=True)}")
        response_dump = dict(response_summary.get("responseDump") or {})
        if response_dump:
            print(f"[execution-response-dump] {json.dumps(response_dump, ensure_ascii=True)}")
        if extraction.accepted_configuration:
            print(f"[config-server-selected] {json.dumps(extraction.accepted_configuration, ensure_ascii=True)}")
        if extraction.price_value is not None:
            if extraction.accepted_configuration:
                print("[config-price-context] using_above_selected_configuration=true")
            print(
                "[config-price] "
                f"price={extraction.price_value} currency={extraction.currency} "
                f"endpoint={response_summary.get('endpoint')} source={response_summary.get('priceSource')}"
            )

    if validation is not None:
        print(
            "[validation] "
            f"valid={validation.is_valid} mismatches={len(validation.mismatches)} "
            f"reason={validation.inferred_failure_reason}"
        )
        if validation.mismatches:
            print(f"[validation-mismatches] {json.dumps(validation.mismatches, ensure_ascii=True)}")


def _write_unit_artifacts(
    artifacts_dir: str,
    unit_id: str,
    spec: dict[str, Any],
    summary: dict[str, Any],
    final_state: RunState | None,
    bootstrap_artifacts: dict[str, Any],
) -> str:
    site = _slugify(str(summary.get("site") or spec.get("site_name") or "unknown-site"))
    run_dir = Path(artifacts_dir) / site / unit_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (run_dir / "bootstrap.json").write_text(json.dumps(bootstrap_artifacts, indent=2), encoding="utf-8")
    traces = list(bootstrap_artifacts.get("network_traces") or [])
    (run_dir / "network_traces.json").write_text(json.dumps(traces, indent=2), encoding="utf-8")
    option_catalog = list(bootstrap_artifacts.get("option_catalog") or [])
    (run_dir / "option_catalog.json").write_text(json.dumps(option_catalog, indent=2), encoding="utf-8")
    drift_report = dict(summary.get("drift_report") or _default_drift_report(bootstrap_artifacts))
    (run_dir / "drift_report.json").write_text(json.dumps(drift_report, indent=2), encoding="utf-8")

    if final_state is not None:
        (run_dir / "state_snapshot.json").write_text(
            json.dumps(state_snapshot(final_state), indent=2),
            encoding="utf-8",
        )

    return str(run_dir)


def run_single_target(
    orchestrator: ExtractionOrchestrator,
    args: argparse.Namespace,
    spec: dict[str, Any],
) -> tuple[dict[str, Any], RunState | None, dict[str, Any]]:
    def run_once(
        *,
        force_recon_refresh: bool = False,
        recon_refresh_reason: str | None = None,
    ) -> tuple[dict[str, Any], RunState | None, dict[str, Any]]:
        target, bootstrap_artifacts = build_target_input(
            spec,
            args,
            orchestrator.store,
            force_recon_refresh=force_recon_refresh,
            recon_refresh_reason=recon_refresh_reason,
        )
        if args.verbose:
            _print_verbose_bootstrap(target.product_url, bootstrap_artifacts)

        if args.recon_only:
            option_groups = list(bootstrap_artifacts.get("option_groups") or [])
            option_dependencies = list(bootstrap_artifacts.get("option_dependencies") or [])
            summary = {
                "site": target.site_name,
                "url": target.product_url,
                "final_status": "completed",
                "validated": True,
                "feasible": True,
                "strategy": "recon_only",
                "price": None,
                "currency": target.expected_currency,
                "option_group_count": len(option_groups),
                "dependency_edge_count": len(option_dependencies),
                "quantity_behavior_hint": dict(bootstrap_artifacts.get("quantity_signal") or {}).get("mode", "unknown"),
                "available_option_groups": option_groups,
                "available_option_dependencies": option_dependencies,
                "option_catalog_group_count": len(list(bootstrap_artifacts.get("option_catalog") or [])),
            }
            return _augment_summary_with_bootstrap(summary, target, bootstrap_artifacts), None, bootstrap_artifacts

        option_prevalidation = dict(target.bootstrap_signals.get("option_prevalidation") or {})
        if args.require_matched_options and option_prevalidation.get("available") and not option_prevalidation.get("valid", True):
            return _build_prevalidation_failure_summary(target, bootstrap_artifacts), None, bootstrap_artifacts

        state = RunState(target=target, max_attempts=args.max_attempts)
        final_state = orchestrator.run(state)
        summary = _augment_summary_with_bootstrap(summarize_run(final_state), target, bootstrap_artifacts)

        if args.verbose:
            _print_verbose_outcome(summary, final_state)

        return summary, final_state, bootstrap_artifacts

    initial_summary, initial_state, initial_bootstrap = run_once()
    refresh_reason = None
    if not args.recon_only:
        recon_cache = dict(initial_summary.get("recon_cache") or {})
        if recon_cache.get("hit"):
            request_only = bool(initial_summary.get("request_only", False)) or bool(getattr(args, "request_only", False))
            # Distinguish "key absent" (snapshot pre-dates the feature → refresh
            # once to populate) from "key present but empty" (current bootstrap
            # ran and the site genuinely has none — refreshing won't change
            # anything and only causes per-run re-bootstrap churn).
            templates_key_absent = "request_templates" not in initial_bootstrap
            catalog_key_absent = "option_catalog" not in initial_bootstrap

            # Cache-hit refresh is meant to enrich the recon snapshot (option catalog + request templates).
            # In request-only mode, an empty option catalog is expected and should not force a refresh.
            # Symmetrically: an empty request_templates dict is expected on sites without SetLink-style
            # templates (Saxoprint, Print24) and must not force a refresh either; only an *absent* key
            # (legacy snapshot from before the feature existed) triggers re-bootstrap.
            if templates_key_absent or ((not request_only) and catalog_key_absent):
                refresh_reason = "missing_enriched_recon"
            else:
                refresh_reason = _should_refresh_cached_recon(initial_summary)
    if refresh_reason is None:
        return initial_summary, initial_state, initial_bootstrap

    refreshed_summary, refreshed_state, refreshed_bootstrap = run_once(
        force_recon_refresh=True,
        recon_refresh_reason=refresh_reason,
    )
    drift_report = _build_drift_report(initial_bootstrap, refreshed_bootstrap, refresh_reason)
    refreshed_bootstrap["drift_report"] = drift_report
    refreshed_bootstrap["recon_refresh_reason"] = refresh_reason

    if refreshed_state is not None:
        refreshed_state.target.bootstrap_signals["drift_report"] = drift_report
        refreshed_state.target.bootstrap_signals["recon_refresh_reason"] = refresh_reason
    refreshed_summary["drift_report"] = drift_report
    refreshed_summary["drift_verdict"] = drift_report.get("verdict", "not_available")
    refreshed_summary["recon_refresh_reason"] = refresh_reason
    if "option_prevalidation" not in refreshed_summary:
        prevalidation = dict((refreshed_state.target.bootstrap_signals if refreshed_state else {}).get("option_prevalidation") or {})
        if prevalidation:
            refreshed_summary["option_prevalidation"] = _compact_prevalidation(prevalidation)
    return refreshed_summary, refreshed_state, refreshed_bootstrap


def is_successful_summary(summary: dict[str, Any]) -> bool:
    final_status = summary.get("final_status")
    if isinstance(final_status, str):
        return final_status == "completed"

    # Backward-compatible success rule for current summarize_run output.
    validated = bool(summary.get("validated"))
    feasible = bool(summary.get("feasible", True))
    return validated and feasible


def _build_results_jsonl_row(
    *,
    unit_id: str,
    index: int,
    spec: dict[str, Any],
    summary: dict[str, Any] | None,
    status: str,
    reason: str | None = None,
    mode: str = "full",
) -> dict[str, Any]:
    base = {
        "unit_id": unit_id,
        "index": int(index),
        "url": str(spec.get("url") or ""),
        "status": str(status),
    }

    if reason:
        base["reason"] = str(reason)

    if summary is None:
        return base

    if mode == "compact":
        base.update(
            {
                "site": str(summary.get("site") or ""),
                "validated": bool(summary.get("validated", False)),
                "price": summary.get("price"),
                "currency": summary.get("currency"),
                "replay_mode": summary.get("replay_mode"),
                "fallback_mode": summary.get("fallback_mode"),
                "extraction_reason": summary.get("extraction_reason"),
                "mismatches": list(summary.get("mismatches") or []),
                "artifact_dir": summary.get("artifact_dir"),
            }
        )
        return base

    base.update(
        {
            "site": str(summary.get("site") or ""),
            "validated": bool(summary.get("validated", False)),
            "price": summary.get("price"),
            "currency": summary.get("currency"),
            "artifact_dir": summary.get("artifact_dir"),
            "summary": summary,
        }
    )
    return base


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agentic price extraction MVP")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--url", help="Product URL to bootstrap and run")
    input_group.add_argument("--manifest-file", help="Path to JSON manifest with multiple targets")
    input_group.add_argument("--manifest-csv", help="Path to CSV manifest with one target row per line")
    parser.add_argument(
        "--manifest-csv-url",
        help="Default URL for CSV rows that do not include a url column (one-URL-per-file workflows)",
    )

    parser.add_argument("--site-name", help="Optional site name override when using --url")
    parser.add_argument("--product-type", default="unknown", help="Product type label when using --url")
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        help="Repeatable key=value option override. Example: --option quantity=250 --option seitig=16",
    )
    parser.add_argument(
        "--options-json",
        help="JSON object with selected options. Merge precedence: --option > --options-json > --options-file > spec defaults",
    )
    parser.add_argument(
        "--options-file",
        help="Path to JSON file with selected options. Merge precedence: --option > --options-json > --options-file > spec defaults",
    )
    parser.add_argument("--expected-price", type=float, help="Optional expected price for validation")
    parser.add_argument(
        "--expected-price-tolerance",
        type=float,
        default=0.01,
        help="Absolute tolerance for expected-price comparison",
    )
    parser.add_argument("--expected-currency", default="EUR", help="Expected currency for validation")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode for bootstrap")
    parser.add_argument(
        "--headed-debug-hold-seconds",
        type=float,
        default=0.0,
        help="When headed mode is enabled, keep the page open for inspection before closing",
    )
    parser.add_argument(
        "--disable-auto-accept-cookies",
        action="store_true",
        help="Disable generic cookie-consent auto-accept logic during bootstrap",
    )
    parser.add_argument("--verbose", action="store_true", help="Print detailed phase-by-phase logs")
    parser.add_argument(
        "--allow-heuristic-fallback",
        action="store_true",
        help="Allow synthetic heuristic pricing when replay extraction fails",
    )
    parser.add_argument(
        "--require-matched-options",
        action="store_true",
        help="Fail validation when any requested options remain unmatched after bootstrap option application",
    )
    parser.add_argument(
        "--allow-sideload-ui-only",
        action="store_true",
        help=(
            "Allow sideload value-map entries with confidence='ui_only' to participate in resolution. "
            "Default is to only accept 'confirmed' and 'partial' entries."
        ),
    )
    parser.add_argument(
        "--request-only",
        action="store_true",
        help=(
            "Strict request-only pricing mode: do not apply options in the browser, "
            "disable active dependency probing, and require HTTP replay (no captured-trace/DOM/heuristic fallbacks)"
        ),
    )
    parser.add_argument(
        "--recon-only",
        action="store_true",
        help="Run recon only (discover options/endpoints and cache them), skip pricing extraction",
    )
    parser.add_argument(
        "--recon-ttl-days",
        type=int,
        default=7,
        help="TTL in days for recon snapshots stored in local knowledge DB",
    )
    parser.add_argument(
        "--force-recon-refresh",
        action="store_true",
        help="Force fresh recon even when a non-stale recon snapshot exists",
    )
    parser.add_argument("--bootstrap-timeout-ms", type=int, default=20000, help="Browser bootstrap timeout")
    parser.add_argument(
        "--max-observed-requests",
        type=int,
        default=250,
        help="Maximum captured requests from browser bootstrap",
    )
    parser.add_argument(
        "--http-request-timeout-seconds",
        type=float,
        default=15.0,
        help="HTTP replay timeout per request in seconds",
    )
    parser.add_argument(
        "--http-min-delay-ms",
        type=int,
        default=0,
        help="Minimum per-host delay between HTTP replay requests in milliseconds",
    )
    parser.add_argument(
        "--http-jitter-ms",
        type=int,
        default=0,
        help="Additional random jitter (0..N ms) added to per-host request delay",
    )
    parser.add_argument(
        "--http-max-retries",
        type=int,
        default=1,
        help="Max retry attempts for retryable HTTP replay failures (timeout/429/5xx)",
    )
    parser.add_argument(
        "--http-backoff-base-ms",
        type=int,
        default=400,
        help="Base backoff in milliseconds for retryable replay failures",
    )
    parser.add_argument(
        "--http-backoff-max-ms",
        type=int,
        default=5000,
        help="Maximum backoff in milliseconds for retryable replay failures",
    )
    parser.add_argument(
        "--proxy-url",
        help="Optional proxy URL for HTTP replay, e.g. http://user:pass@host:port",
    )
    parser.add_argument(
        "--proxy-file",
        help="Optional text file with one proxy URL per line for replay proxy rotation",
    )
    parser.add_argument(
        "--proxy-rotation",
        choices=["none", "round_robin", "random"],
        default="none",
        help="Proxy selection strategy when replay proxy pool has multiple entries",
    )
    parser.add_argument(
        "--proxy-failure-threshold",
        type=int,
        default=3,
        help="Quarantine a proxy after N consecutive replay failures",
    )
    parser.add_argument(
        "--proxy-cooldown-seconds",
        type=int,
        default=300,
        help="Cooldown window before a quarantined proxy can be retried",
    )
    parser.add_argument(
        "--json-adapters-dir",
        help="Optional directory of JSON adapters to load instead of the default adapters/ search",
    )
    parser.add_argument(
        "--site-adapters-dir",
        help="Optional directory of generated SiteAdapter Python modules to load",
    )
    parser.add_argument(
        "--allow-generated-site-adapters",
        action="store_true",
        help="Allow loading generated SiteAdapter modules from --site-adapters-dir",
    )
    parser.add_argument(
        "--max-dependency-probe-steps",
        type=int,
        default=6,
        help="Maximum active dependency-probe interactions during bootstrap",
    )
    parser.add_argument(
        "--knowledge-db",
        default=".data/knowledge.db",
        help="Path to sqlite knowledge database",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum self-correction attempts",
    )
    parser.add_argument(
        "--checkpoint-file",
        default=".data/checkpoints/job_state.json",
        help="Path to job checkpoint JSON state",
    )
    parser.add_argument(
        "--staged-rollout",
        action="store_true",
        help="Run staged safe-scale extraction flow (single_config -> probe -> pilot -> staging -> production)",
    )
    parser.add_argument(
        "--staged-policy-file",
        help="Optional JSON policy file to override default stage budgets/gates",
    )
    parser.add_argument(
        "--staged-generated-manifest-file",
        help="Optional JSON manifest providing generated units to combine with primary manifest units",
    )
    parser.add_argument(
        "--staged-job-db",
        default=".data/checkpoints/staged_job_state.db",
        help="SQLite checkpoint DB for staged rollout mode",
    )
    parser.add_argument(
        "--staged-auto-approve",
        action="store_true",
        help="Auto-approve promotion from pilot to later stages (non-interactive)",
    )
    parser.add_argument(
        "--staged-breaker-window",
        type=int,
        default=40,
        help="Rolling window size (units) for staged circuit breaker evaluation",
    )
    parser.add_argument(
        "--staged-breaker-min-events",
        type=int,
        default=10,
        help="Minimum events before staged circuit breaker can open",
    )
    parser.add_argument(
        "--staged-breaker-block-rate",
        type=float,
        default=0.20,
        help="Trip staged circuit breaker when 403/429 rate in rolling window reaches this threshold",
    )
    parser.add_argument(
        "--staged-breaker-server-error-rate",
        type=float,
        default=0.30,
        help="Trip staged circuit breaker when 5xx/request-error rate in rolling window reaches this threshold",
    )
    parser.add_argument(
        "--staged-generate-agentic-proposals",
        action="store_true",
        help="Generate per-product onboarding proposal JSON (with adapter stub) during staged runs",
    )
    parser.add_argument(
        "--staged-proposals-dir",
        default=".data/proposals/staged",
        help="Output directory for per-product staged onboarding proposal artifacts",
    )
    parser.add_argument(
        "--staged-proposal-top-n",
        type=int,
        default=10,
        help="Number of top ranked endpoints to include in each staged onboarding proposal",
    )
    parser.add_argument(
        "--staged-generate-runnable-adapters",
        action="store_true",
        help="Emit runnable JSON adapters + optional SiteAdapter modules alongside staged proposals",
    )
    parser.add_argument(
        "--staged-generated-adapters-dir",
        help="Optional base directory to write generated adapters (defaults to per-unit proposal folder)",
    )
    parser.add_argument(
        "--staged-agentic-use-langgraph",
        action="store_true",
        help="Use LangGraph onboarding payload generation for staged per-product proposals",
    )
    parser.add_argument(
        "--staged-agentic-llm-enable",
        action="store_true",
        help="When LangGraph is enabled, also enable OpenAI-compatible LLM enrichment",
    )
    parser.add_argument(
        "--staged-agentic-llm-model",
        default="gpt-4.1-mini",
        help="OpenAI-compatible model name used for staged LangGraph LLM enrichment",
    )
    parser.add_argument(
        "--staged-agentic-llm-base-url",
        help="OpenAI-compatible base URL for staged LangGraph LLM enrichment",
    )
    parser.add_argument(
        "--staged-agentic-llm-api-key-env",
        default="OPENAI_API_KEY",
        help="Env var name that stores API key for staged LangGraph LLM enrichment",
    )
    parser.add_argument(
        "--staged-agentic-llm-timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout in seconds for staged LangGraph LLM requests",
    )
    parser.add_argument(
        "--staged-agentic-dotenv-path",
        default=".env",
        help="Optional dotenv path used to preload keys for staged LangGraph generation",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="When resuming, retry units that were previously marked failed",
    )
    parser.add_argument(
        "--output-file",
        help="Optional path to write final JSON output",
    )
    parser.add_argument(
        "--results-jsonl",
        help="Optional path to append per-unit result rows as JSONL during the run",
    )
    parser.add_argument(
        "--results-jsonl-mode",
        choices=["full", "compact"],
        default="compact",
        help="Row shape for --results-jsonl. compact is recommended for large matrix runs.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default=".data/runs",
        help="Base directory for per-run artifacts",
    )
    parser.add_argument(
        "--fail-on-invalid",
        action="store_true",
        help="Exit with non-zero status if any target fails validation/extraction",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if bool(getattr(args, "request_only", False)):
        incompatible = []
        if bool(args.recon_only):
            incompatible.append("--recon-only")
        if bool(args.allow_heuristic_fallback):
            incompatible.append("--allow-heuristic-fallback")
        if bool(args.require_matched_options):
            incompatible.append("--require-matched-options")
        if incompatible:
            flags = ", ".join(incompatible)
            raise SystemExit(f"--request-only cannot be combined with {flags}")
        if bool(args.verbose):
            print("[request-only] enabled=true enforcing_http_replay_only=true")

    store = KnowledgeStore(args.knowledge_db)
    try:
        orchestrator = ExtractionOrchestrator(store)

        if args.manifest_file:
            target_specs = parse_manifest_file(args.manifest_file)
        elif args.manifest_csv:
            target_specs = parse_manifest_csv(
                args.manifest_csv,
                default_url=args.manifest_csv_url,
                default_site_name=args.site_name,
                default_product_type=args.product_type,
            )
        else:
            target_specs = [build_target_spec_from_args(args)]

        generated_specs = parse_optional_manifest_file(args.staged_generated_manifest_file)

        if args.staged_rollout:
            def _run_stage_unit(stage_args: argparse.Namespace, spec: dict[str, Any]) -> dict[str, Any]:
                local_store = KnowledgeStore(stage_args.knowledge_db)
                local_orchestrator = ExtractionOrchestrator(local_store)
                final_state: RunState | None = None
                bootstrap_artifacts: dict[str, Any] = {}
                artifact_dir: str | None = None
                try:
                    try:
                        summary, final_state, bootstrap_artifacts = run_single_target(local_orchestrator, stage_args, spec)
                    except Exception as exc:  # pragma: no cover - defensive boundary for long runs
                        summary = {
                            "site": str(spec.get("site_name") or "unknown"),
                            "url": str(spec.get("url") or ""),
                            "product_url": str(spec.get("url") or ""),
                            "final_status": "failed",
                            "validated": False,
                            "feasible": False,
                            "exception": str(exc),
                            "mismatches": ["request_error"],
                            "http_replay": {"attempts": []},
                        }

                    if stage_args.artifacts_dir:
                        unit_id = str(spec.get("_staged_unit_id") or build_unit_id(spec))
                        artifact_dir = _write_unit_artifacts(
                            stage_args.artifacts_dir,
                            unit_id,
                            spec,
                            summary,
                            final_state,
                            bootstrap_artifacts,
                        )
                        summary["artifact_dir"] = artifact_dir

                    if bool(getattr(stage_args, "staged_generate_agentic_proposals", False)) and artifact_dir:
                        staged_unit_id = str(spec.get("_staged_unit_id") or build_unit_id(spec))
                        stage_name = str(staged_unit_id.split("::")[1] if "::" in staged_unit_id else "unknown")
                        unit_slug = _slugify(staged_unit_id)

                        proposals_root = Path(str(getattr(stage_args, "staged_proposals_dir", ".data/proposals/staged") or ".data/proposals/staged"))

                        if bool(getattr(stage_args, "staged_agentic_use_langgraph", False)):
                            payload = build_langgraph_onboarding_payload(
                                run_dir=str(artifact_dir),
                                knowledge_db=str(stage_args.knowledge_db),
                                top_n=max(int(getattr(stage_args, "staged_proposal_top_n", 10) or 10), 1),
                                llm_enable=bool(getattr(stage_args, "staged_agentic_llm_enable", False)),
                                llm_model=str(getattr(stage_args, "staged_agentic_llm_model", "gpt-4.1-mini") or "gpt-4.1-mini"),
                                llm_base_url=getattr(stage_args, "staged_agentic_llm_base_url", None),
                                llm_api_key_env=str(getattr(stage_args, "staged_agentic_llm_api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY"),
                                llm_timeout_seconds=float(getattr(stage_args, "staged_agentic_llm_timeout_seconds", 60.0) or 60.0),
                                dotenv_path=str(getattr(stage_args, "staged_agentic_dotenv_path", ".env") or ".env"),
                            )

                            proposal_body = dict(payload.get("proposal") or {})
                            site_slug = _slugify(str(proposal_body.get("site_name") or summary.get("site") or "unknown-site"))
                            target_dir = proposals_root / site_slug / stage_name
                            target_dir.mkdir(parents=True, exist_ok=True)

                            payload_path = target_dir / f"{unit_slug}.langgraph_payload.json"
                            payload_path.write_text(
                                json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                                encoding="utf-8",
                            )

                            adapter_stub_path: str | None = None
                            adapter_stub_text = str(proposal_body.get("adapter_stub") or "")
                            if adapter_stub_text:
                                adapter_stub_file = target_dir / f"{unit_slug}.adapter_stub.py"
                                adapter_stub_file.write_text(adapter_stub_text, encoding="utf-8")
                                adapter_stub_path = str(adapter_stub_file)

                            patch_plan_file_path: str | None = None
                            patch_plan = dict(payload.get("patchPlan") or {})
                            if patch_plan:
                                patch_plan_file = target_dir / f"{unit_slug}.patch_plan.json"
                                patch_plan_file.write_text(
                                    json.dumps(patch_plan, indent=2, ensure_ascii=True) + "\n",
                                    encoding="utf-8",
                                )
                                patch_plan_file_path = str(patch_plan_file)

                            summary["agentic_proposal"] = {
                                "mode": "langgraph",
                                "framework": str(payload.get("framework") or "langgraph"),
                                "framework_detail": str(payload.get("frameworkDetail") or ""),
                                "llm": dict(payload.get("llm") or {}),
                                "payload_json": str(payload_path),
                                "adapter_stub": adapter_stub_path,
                                "patch_plan": patch_plan_file_path,
                            }

                            if bool(getattr(stage_args, "staged_generate_runnable_adapters", False)):
                                base_generated_dir = str(getattr(stage_args, "staged_generated_adapters_dir", "") or "").strip()
                                generated_root = Path(base_generated_dir) if base_generated_dir else target_dir
                                generated_dir = generated_root / "generated" / unit_slug
                                generated = generate_runnable_adapters(
                                    run_dir=str(artifact_dir),
                                    output_dir=str(generated_dir),
                                    proposal=proposal_body,
                                    llm_suggestions=dict(payload.get("llmSuggestions") or {}),
                                )
                                summary["agentic_proposal"]["generated_adapters"] = generated
                        else:
                            proposal = build_onboarding_proposal(
                                run_dir=str(artifact_dir),
                                knowledge_db=str(stage_args.knowledge_db),
                                top_n=max(int(getattr(stage_args, "staged_proposal_top_n", 10) or 10), 1),
                            )

                            site_slug = _slugify(str(proposal.site_name or summary.get("site") or "unknown-site"))
                            target_dir = proposals_root / site_slug / stage_name
                            target_dir.mkdir(parents=True, exist_ok=True)

                            proposal_json_path = target_dir / f"{unit_slug}.proposal.json"
                            proposal_json_path.write_text(
                                json.dumps(asdict(proposal), indent=2, ensure_ascii=True) + "\n",
                                encoding="utf-8",
                            )

                            adapter_stub_path = target_dir / f"{unit_slug}.adapter_stub.py"
                            adapter_stub_path.write_text(str(proposal.adapter_stub or ""), encoding="utf-8")

                            summary["agentic_proposal"] = {
                                "mode": "deterministic",
                                "proposal_json": str(proposal_json_path),
                                "adapter_stub": str(adapter_stub_path),
                            }

                            if bool(getattr(stage_args, "staged_generate_runnable_adapters", False)):
                                base_generated_dir = str(getattr(stage_args, "staged_generated_adapters_dir", "") or "").strip()
                                generated_root = Path(base_generated_dir) if base_generated_dir else target_dir
                                generated_dir = generated_root / "generated" / unit_slug
                                generated = generate_runnable_adapters(
                                    run_dir=str(artifact_dir),
                                    output_dir=str(generated_dir),
                                    proposal=asdict(proposal),
                                    llm_suggestions=None,
                                )
                                summary["agentic_proposal"]["generated_adapters"] = generated
                    return summary
                finally:
                    local_store.close()

            def _approve_pilot_promotion(next_stage: str) -> bool:
                if bool(args.staged_auto_approve):
                    return True
                prompt = (
                    f"[approval] pilot passed. Proceed to stage '{next_stage}'? "
                    "Type 'yes' to continue: "
                )
                try:
                    answer = input(prompt)
                except EOFError:
                    return False
                return str(answer or "").strip().lower() == "yes"

            staged_output = run_staged_rollout(
                source_specs=target_specs,
                generated_specs=generated_specs,
                args=args,
                run_unit=_run_stage_unit,
                is_successful=is_successful_summary,
                approve_pilot_promotion=_approve_pilot_promotion,
            )

            output_json = json.dumps(staged_output, indent=2)
            print(output_json)

            if args.output_file:
                output_path = Path(args.output_file)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(output_json + "\n", encoding="utf-8")

            if args.fail_on_invalid and str(staged_output.get("status")) != "completed":
                raise SystemExit(2)
            return

        checkpoint = load_checkpoint(args.checkpoint_file)
        resume_enabled = bool(args.manifest_file or args.manifest_csv)
        aggregate_results: list[dict[str, Any]] = []
        results_jsonl_path = Path(args.results_jsonl) if args.results_jsonl else None
        if results_jsonl_path is not None:
            results_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        skipped_count = 0
        failed_count = 0
        completed_count = 0
        started_at = time.perf_counter()

        for index, spec in enumerate(target_specs, start=1):
            unit_id = build_unit_id(spec)
            final_state: RunState | None = None
            bootstrap_artifacts: dict[str, Any] = {}
            if resume_enabled:
                decision = should_process_unit(checkpoint, unit_id, args.retry_failed)
                if not decision.should_process:
                    skipped_count += 1
                    mark_skipped(checkpoint, unit_id, decision.reason or "skipped")
                    save_checkpoint(args.checkpoint_file, checkpoint)
                    if results_jsonl_path is not None:
                        row = _build_results_jsonl_row(
                            unit_id=unit_id,
                            index=index,
                            spec=spec,
                            summary=None,
                            status="skipped",
                            reason=decision.reason or "skipped",
                            mode=str(args.results_jsonl_mode or "compact"),
                        )
                        with results_jsonl_path.open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps(row, ensure_ascii=True) + "\n")
                    print(
                        f"[skip] {index}/{len(target_specs)} {spec.get('url')} "
                        f"({decision.reason})"
                    )
                    continue

            print(f"[run] {index}/{len(target_specs)} {spec.get('url')}")
            try:
                summary, final_state, bootstrap_artifacts = run_single_target(orchestrator, args, spec)
            except Exception as exc:  # pragma: no cover - defensive boundary for long runs
                summary = {
                    "site": str(spec.get("site_name") or "unknown"),
                    "product_url": str(spec.get("url") or ""),
                    "final_status": "failed",
                    "validated": False,
                    "feasible": False,
                    "exception": str(exc),
                }

            if args.artifacts_dir:
                artifact_dir = _write_unit_artifacts(
                    args.artifacts_dir,
                    unit_id,
                    spec,
                    summary,
                    final_state,
                    bootstrap_artifacts,
                )
                summary["artifact_dir"] = artifact_dir

            if results_jsonl_path is not None:
                row = _build_results_jsonl_row(
                    unit_id=unit_id,
                    index=index,
                    spec=spec,
                    summary=summary,
                    status="completed" if is_successful_summary(summary) else "failed",
                    mode=str(args.results_jsonl_mode or "compact"),
                )
                with results_jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=True) + "\n")

            aggregate_results.append(summary)
            is_complete = is_successful_summary(summary)
            if is_complete:
                completed_count += 1
                mark_completed(checkpoint, unit_id, spec, summary)
            else:
                failed_count += 1
                mark_failed(checkpoint, unit_id, spec, summary)
            save_checkpoint(args.checkpoint_file, checkpoint)

        elapsed_s = max(time.perf_counter() - started_at, 1e-9)
        processed_count = completed_count + failed_count
        throughput = processed_count / elapsed_s
        remaining = max(len(target_specs) - processed_count - skipped_count, 0)
        eta_seconds = (remaining / throughput) if throughput > 0 else None

        output = {
            "run_type": "manifest" if (args.manifest_file or args.manifest_csv) else "single",
            "targets_total": len(target_specs),
            "processed": processed_count,
            "completed": completed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "elapsed_seconds": round(elapsed_s, 3),
            "throughput_units_per_second": round(throughput, 4),
            "eta_seconds": round(eta_seconds, 3) if eta_seconds is not None else None,
            "checkpoint_file": args.checkpoint_file,
            "results_jsonl": str(results_jsonl_path) if results_jsonl_path is not None else None,
            "retry_failed": bool(args.retry_failed),
            "results": aggregate_results,
        }

        output_json = json.dumps(output, indent=2)
        print(output_json)

        if args.output_file:
            output_path = Path(args.output_file)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(output_json + "\n", encoding="utf-8")

        if args.fail_on_invalid and failed_count > 0:
            raise SystemExit(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()
