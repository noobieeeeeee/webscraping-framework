"""Print24 catalog harvesting from captured `productDetails` AJAX responses.

Per `print24_feasibility_notes.md`:
- §3.1 `repo` returns the full configuration schema (propGroupName, all
  property alternatives, pgrId per option). We don't currently capture
  `repo` in bootstrap recon because the product page doesn't auto-load
  it; a future bootstrap-side probe could trigger it explicitly.
- §3.3 `productDetails` returns the currently-selected option per group as
  `prop_details`, each with `box_name` (canonical key like "format",
  "papierI", "quantity"), `prop_id` (the integer option id used in the
  POST body's `properties[].id`), `prop_translated` (human label), and
  `box_name_title` (display group name).

This module harvests the v1 subset — one option per group, the currently-
selected one — and produces catalog rows in the same shape used by the
Onlineprinters pipeline so `_catalog_groups_from_bootstrap` and the
existing pre-validation logic can consume them without special-casing.

It's a partial fix: users requesting the *captured* configuration get a
correct catalog (right canonical name, right prop_id). Users requesting
alternative property values still need either the prop_id supplied via
adapter inject rules OR a future `repo` probe that surfaces the full
alternatives list.
"""

from __future__ import annotations

import json
import re
from typing import Any


PRODUCT_DETAILS_URL_RE = re.compile(r"product[-_]?details", re.IGNORECASE)


def _safe_json_loads(text: str) -> Any | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _pick_best_product_details_trace(traces: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most recent / most-complete `productDetails` POST response.

    Prefers POSTs with JSON content-type, status 200, and a response body
    containing `prop_details` (the structured per-group selection). If multiple
    are captured (e.g. the page reloaded mid-bootstrap), the LAST one wins
    because it reflects the final state.
    """
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, trace in enumerate(traces or []):
        if not isinstance(trace, dict):
            continue
        if str(trace.get("method") or "").upper() != "POST":
            continue
        url = str(trace.get("url") or "")
        if not PRODUCT_DETAILS_URL_RE.search(url):
            continue
        status = int(trace.get("status") or 0)
        if status != 200:
            continue
        if "json" not in str(trace.get("response_content_type") or "").lower():
            continue
        rb = str(trace.get("response_body_preview") or "")
        if not rb or "prop_details" not in rb:
            continue
        score = 4
        score += min(2, len(rb) // 4000)
        candidates.append((score, index, trace))
    if not candidates:
        return None
    # Highest score; on ties, latest trace (highest index) wins.
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return candidates[0][2]


def harvest_print24_catalog_from_traces(
    network_traces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return catalog rows in the standard shape, harvested from the best
    captured `productDetails` response. Each row has one option (the currently-
    selected one), with `name` set to the canonical Print24 property key (the
    `box_name` field), the prop_id stored under `backendHints.dataPropertyId`,
    and the translated label as `visibleLabel`.

    Returns [] if no usable productDetails response is captured (e.g. the
    bootstrap didn't visit the product or the response was truncated).
    """
    trace = _pick_best_product_details_trace(network_traces or [])
    if trace is None:
        return []
    data = _safe_json_loads(str(trace.get("response_body_preview") or ""))
    if not isinstance(data, dict):
        return []
    prop_details = data.get("prop_details")
    if not isinstance(prop_details, list):
        return []

    catalog: list[dict[str, Any]] = []
    for entry in prop_details:
        if not isinstance(entry, dict):
            continue
        box_name = str(entry.get("box_name") or "").strip()
        prop_id = str(entry.get("prop_id") or "").strip()
        if not box_name or not prop_id:
            continue
        label_translated = str(entry.get("prop_translated") or "").strip()
        label_canonical = str(entry.get("prop_name") or "").strip()
        group_title = str(entry.get("box_name_title") or "").strip() or box_name

        option = {
            "visibleLabel": label_translated or label_canonical or prop_id,
            "visibleValue": label_canonical or label_translated or prop_id,
            "selected": True,
            "controlTag": "print24_property",
            "controlType": "print24_property",
            "name": box_name,
            "backendHints": {
                "dataPropertyId": prop_id,
                "boxName": box_name,
                "boxNameTitle": group_title,
            },
        }
        catalog.append(
            {
                "groupLabel": group_title,
                "normalizedGroupLabel": group_title.lower(),
                "visibleGroupLabel": group_title,
                "controlTypes": ["print24_property"],
                "options": [option],
                "truncated": False,
                "backendHints": {"boxName": box_name},
                "sourceHints": {
                    "boxName": box_name,
                    "boxNameTitle": group_title,
                    "harvestedFromProductDetails": True,
                    "productDetailsUrl": str(trace.get("url") or ""),
                },
            }
        )
    return catalog
