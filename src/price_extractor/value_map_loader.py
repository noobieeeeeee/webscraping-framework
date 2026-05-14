"""Sideloaded option-value maps.

Loads `value_maps/<site>.json` files that provide (option_key, requested_value)
-> backend_id mappings. The shape is site-agnostic: every site contributes the
same JSON schema (see value_maps/saxoprint.de.json or print24.com.json for the
contract). Used as a fallback resolution path between the bootstrap-harvested
catalog and any hardcoded site-adapter dicts.

Match semantics reuse the NFKD + lowercase + collapse-whitespace + exact-or-
substring matcher that already ships in Print24SiteAdapter, so requested values
behave the same whether they hit catalog or sideload.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


SCHEMA_VERSION_SUPPORTED = 1

_DEFAULT_ACCEPTED_CONFIDENCES: frozenset[str] = frozenset({"confirmed", "partial"})
_ALL_CONFIDENCES: frozenset[str] = frozenset({"confirmed", "partial", "ui_only"})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _value_maps_dir(base_dir: Path | None) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    return _repo_root() / "value_maps"


def load_value_map(site_name: str, *, base_dir: Path | None = None) -> dict[str, Any] | None:
    """Load `<base_dir>/<site_name>.json`. Returns the parsed dict or None when
    the file is absent / unreadable / schema-mismatched. Logging is the
    caller's responsibility — the loader emits a structured `_loadError` field
    when something is wrong so the CLI can print it without re-parsing the file.
    """
    site = str(site_name or "").strip().lower()
    if not site:
        return None
    path = _value_maps_dir(base_dir) / f"{site}.json"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "_loadError": f"read_failed: {exc}",
            "_path": str(path),
            "site": site,
            "properties": [],
        }
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "_loadError": f"json_parse_failed: {exc}",
            "_path": str(path),
            "site": site,
            "properties": [],
        }
    if not isinstance(data, dict):
        return {
            "_loadError": "schema_root_not_object",
            "_path": str(path),
            "site": site,
            "properties": [],
        }
    schema_version = data.get("schemaVersion")
    if schema_version != SCHEMA_VERSION_SUPPORTED:
        data["_loadError"] = f"unsupported_schema_version: {schema_version!r}"
    data["_path"] = str(path)
    data.setdefault("site", site)
    data.setdefault("properties", [])
    return data


def _normalize_label(text: Any) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKD", s)).strip().lower()


def value_matches_label(requested: Any, captured_label: Any) -> bool:
    """NFKD + lowercase + collapse-whitespace + (exact OR substring).

    Identical semantics to Print24SiteAdapter._value_matches_captured_label —
    duplicated here so the loader has no dependency on site_adapters. Tests
    pin the cross-equivalence.
    """
    norm_req = _normalize_label(requested)
    norm_cap = _normalize_label(captured_label)
    if not norm_req or not norm_cap:
        return False
    if norm_req == norm_cap:
        return True
    return norm_req in norm_cap


def _accepted_confidences(allow_ui_only: bool) -> frozenset[str]:
    return _ALL_CONFIDENCES if allow_ui_only else _DEFAULT_ACCEPTED_CONFIDENCES


def _filter_properties_by_key(
    value_map: dict[str, Any], canonical_key: str
) -> list[dict[str, Any]]:
    key = str(canonical_key or "").strip().lower()
    if not key:
        return []
    out: list[dict[str, Any]] = []
    for prop in list(value_map.get("properties") or []):
        if not isinstance(prop, dict):
            continue
        if str(prop.get("canonicalKey") or "").strip().lower() == key:
            out.append(prop)
    return out


def _property_candidate_summary(prop: dict[str, Any], *, max_labels: int = 5) -> list[str]:
    labels: list[str] = []
    for option in list(prop.get("options") or [])[:max_labels]:
        if isinstance(option, dict):
            label = str(option.get("label") or "").strip()
            if label:
                labels.append(label)
    return labels


def resolve_value(
    value_map: dict[str, Any] | None,
    canonical_key: str,
    requested_value: Any,
    *,
    allow_ui_only: bool = False,
) -> dict[str, Any]:
    """Resolve a single `(canonical_key, requested_value)` against the loaded
    value_map. Returns a discriminated dict with one of these `status` values:

      - "resolved":      single property × single option matched
      - "ambiguous":     single property × multiple options matched
      - "key_collision": multiple properties × at least one option each
      - "no_match":      property exists but no option label matched
      - "no_property":   value_map carries no property with this canonical key
      - "no_map":        value_map is None / failed to load
    """
    if not value_map or value_map.get("_loadError"):
        return {"status": "no_map"}

    properties = _filter_properties_by_key(value_map, canonical_key)
    if not properties:
        return {"status": "no_property", "canonicalKey": canonical_key}

    accepted = _accepted_confidences(allow_ui_only)
    per_property_hits: list[dict[str, Any]] = []

    for prop in properties:
        matches: list[dict[str, Any]] = []
        for option in list(prop.get("options") or []):
            if not isinstance(option, dict):
                continue
            confidence = str(option.get("confidence") or "").strip().lower()
            if confidence not in accepted:
                continue
            label = option.get("label")
            if not value_matches_label(requested_value, label):
                continue
            matches.append(
                {
                    "backendId": str(option.get("backendId") or ""),
                    "label": str(label or ""),
                    "confidence": confidence,
                }
            )
        if matches:
            per_property_hits.append({"property": prop, "matches": matches})

    if not per_property_hits:
        return {
            "status": "no_match",
            "canonicalKey": canonical_key,
            "candidateLabels": [
                label
                for prop in properties
                for label in _property_candidate_summary(prop)
            ][:8],
        }

    if len(per_property_hits) > 1:
        flat: list[dict[str, Any]] = []
        for hit in per_property_hits:
            prop = hit["property"]
            for m in hit["matches"]:
                flat.append(
                    {
                        "propertyId": str(prop.get("propertyId") or ""),
                        "sourceLabel": str(prop.get("sourceLabel") or ""),
                        "backendId": m["backendId"],
                        "label": m["label"],
                        "confidence": m["confidence"],
                    }
                )
        return {
            "status": "key_collision",
            "canonicalKey": canonical_key,
            "candidates": flat,
        }

    hit = per_property_hits[0]
    prop = hit["property"]
    matches = hit["matches"]

    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "canonicalKey": canonical_key,
            "propertyId": str(prop.get("propertyId") or ""),
            "sourceLabel": str(prop.get("sourceLabel") or ""),
            "candidates": matches,
        }

    only = matches[0]
    return {
        "status": "resolved",
        "canonicalKey": canonical_key,
        "propertyId": str(prop.get("propertyId") or ""),
        "sourceLabel": str(prop.get("sourceLabel") or ""),
        "backendId": only["backendId"],
        "matchedLabel": only["label"],
        "confidence": only["confidence"],
        "productScope": list(prop.get("productScope") or ["*"]),
        "propertyNote": str(prop.get("note") or "") or None,
    }


def describe_value_map(value_map: dict[str, Any] | None) -> dict[str, Any]:
    """Compact summary for the [sideload-loaded] CLI diagnostic line."""
    if not value_map:
        return {"loaded": False}
    properties = list(value_map.get("properties") or [])
    return {
        "loaded": True,
        "site": value_map.get("site", ""),
        "path": value_map.get("_path", ""),
        "schemaVersion": value_map.get("schemaVersion"),
        "captureMethod": value_map.get("captureMethod", ""),
        "generatedAt": value_map.get("generatedAt", ""),
        "propertyCount": len(properties),
        "canonicalKeys": sorted({
            str(p.get("canonicalKey") or "").strip()
            for p in properties
            if isinstance(p, dict) and str(p.get("canonicalKey") or "").strip()
        }),
        "loadError": value_map.get("_loadError"),
    }
