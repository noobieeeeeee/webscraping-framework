from __future__ import annotations

import json
from typing import Any


def find_saxoprint_product_group_id_from_traces(
    network_traces: list[dict[str, Any]] | None,
) -> int | None:
    try:
        traces = list(network_traces or [])
    except Exception:
        traces = []

    for trace in reversed(traces):
        if not isinstance(trace, dict):
            continue
        url = str(trace.get("url") or "")
        lowered_url = url.lower()
        if "api.saxoprint.de" not in lowered_url:
            continue
        if ("get-product-values" not in lowered_url) and ("get-product-prices" not in lowered_url):
            continue
        if str(trace.get("method") or "").upper() != "POST":
            continue
        raw_post = str(trace.get("post_data") or "").strip()
        if not raw_post:
            continue
        try:
            payload = json.loads(raw_post)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            pgid = int(payload.get("productGroupId") or 0)
        except Exception:
            pgid = 0
        if pgid > 0:
            return pgid
    return None


def find_saxoprint_get_product_values_body_from_traces(
    network_traces: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    try:
        traces = list(network_traces or [])
    except Exception:
        return None

    best: tuple[int, dict[str, Any]] | None = None
    for trace in traces:
        try:
            if str(trace.get("url") or "") != "https://api.saxoprint.de/product-configuration/get-product-values":
                continue
            if str(trace.get("method") or "").upper() != "POST":
                continue
            if int(trace.get("status") or 0) != 200:
                continue

            post_data = trace.get("post_data")
            if not isinstance(post_data, str) or not post_data.strip():
                continue

            body = json.loads(post_data)
            if not isinstance(body, dict):
                continue
            if "productGroupId" not in body:
                continue

            cfg = body.get("propertyConfiguration")
            cfg_len = len(cfg) if isinstance(cfg, list) else 999999

            # Prefer the least-specific body (typically empty configuration), but keep
            # a non-empty config as fallback.
            if best is None or cfg_len < best[0]:
                best = (cfg_len, body)
        except Exception:
            continue

    if best is None:
        return None
    return best[1]


def find_saxoprint_portalcode_from_traces(
    network_traces: list[dict[str, Any]] | None,
) -> str | None:
    try:
        traces = list(network_traces or [])
    except Exception:
        return None

    for trace in traces:
        try:
            url_lower = str(trace.get("url") or "").lower()
            if "api.saxoprint.de" not in url_lower:
                continue
            if ("get-product-values" not in url_lower) and ("get-product-prices" not in url_lower):
                continue
            if str(trace.get("method") or "").upper() != "POST":
                continue
            if int(trace.get("status") or 0) != 200:
                continue

            headers = trace.get("request_headers")
            if not isinstance(headers, dict):
                continue
            value = headers.get("portalcode") or headers.get("portal-code")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:
            continue

    # Saxoprint DE expects a portalcode-like locale; default seen in traces is de-DE.
    return "de-DE"
