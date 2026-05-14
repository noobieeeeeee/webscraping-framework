from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..json_adapters import json_get
from .onboarding import OPTION_KEY_ALIASES, _render_adapter_stub


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "unknown"


def _contains_sensitive_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    sensitive_markers = [
        "set-cookie",
        "cookie:",
        "authorization:",
        "bearer ",
        "x-csrf-token",
        "csrf_antiforge",
        "user_identifier_token",
    ]
    return any(marker in lowered for marker in sensitive_markers)


def _pick_selected_endpoint(
    proposal: dict[str, Any],
    llm_suggestions: dict[str, Any],
) -> dict[str, Any] | None:
    adapter_hints = _safe_dict(llm_suggestions.get("adapter_hints"))
    selected = _safe_dict(adapter_hints.get("selected_endpoint"))
    if selected.get("url"):
        return {
            "url": str(selected.get("url") or "").strip(),
            "method": str(selected.get("method") or "").strip().upper(),
        }

    top_endpoints = [
        row for row in _safe_list(proposal.get("top_endpoints")) if isinstance(row, dict)
    ]
    if top_endpoints:
        first = dict(top_endpoints[0])
        return {
            "url": str(first.get("url") or "").strip(),
            "method": str(first.get("method") or "").strip().upper(),
        }

    return None


def _find_trace_for_endpoint(
    traces: list[dict[str, Any]],
    endpoint: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not endpoint:
        return None
    target_url = str(endpoint.get("url") or "").strip()
    target_method = str(endpoint.get("method") or "").strip().upper()
    if not target_url:
        return None

    for trace in traces:
        if not isinstance(trace, dict):
            continue
        url = str(trace.get("url") or "").strip()
        method = str(trace.get("method") or "").strip().upper()
        if url == target_url and (not target_method or method == target_method):
            return trace
    return None


def _select_price_path(summary: dict[str, Any]) -> str | None:
    candidates = [
        row
        for row in _safe_list(summary.get("price_candidates") or summary.get("priceCandidates"))
        if isinstance(row, dict)
    ]
    scored: list[tuple[int, str]] = []
    for row in candidates:
        source = str(row.get("source") or "").strip()
        if not source.startswith("json:"):
            continue
        if source.startswith("json_html:"):
            continue
        score = int(row.get("score", 0) or 0)
        scored.append((score, source[5:]))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def _normalize_header_map(headers: dict[str, Any]) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for key, value in dict(headers or {}).items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        lowered = key_text.lower()
        if lowered in {"cookie", "authorization"}:
            continue
        filtered[key_text] = str(value)
    return filtered


def _build_url_regex(product_url: str) -> str:
    parts = urlsplit(product_url)
    host = re.escape(parts.netloc.lstrip("www."))
    path = re.escape(parts.path or "")
    return rf"https?://(www\.)?{host}{path}(?:[/?#].*)?$"


def _convert_semantic_path(path: str, payload: dict[str, Any]) -> str | None:
    text = str(path or "").strip()
    match = re.match(r"^properties\[name=([^\]]+)\]\.([A-Za-z0-9_\[\]\.]+)$", text)
    if not match:
        return text if text else None

    target_name = match.group(1).strip().lower()
    remainder = match.group(2).strip()
    properties = payload.get("properties") if isinstance(payload, dict) else None
    if not isinstance(properties, list):
        return None

    for index, row in enumerate(properties):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip().lower()
        if name == target_name:
            return f"properties[{index}].{remainder}"

    return None


def _group_label_matches_option(group_label: str, option_key: str) -> bool:
    label = str(group_label or "").strip().lower()
    canonical_key = str(option_key or "").strip().lower()
    if not label or not canonical_key:
        return False
    aliases = OPTION_KEY_ALIASES.get(canonical_key)
    if not aliases:
        return canonical_key in label or label in canonical_key
    for alias in aliases:
        if alias in label:
            return True
    return False


def _build_value_map_from_catalog(
    option_catalog: list[dict[str, Any]],
    option_key: str,
) -> dict[str, str]:
    if not option_catalog:
        return {}

    key_norm = str(option_key or "").strip().lower()
    if not key_norm:
        return {}

    preferred_backend_keys = [
        "dataOptionId",
        "dataVarindex",
        "dataPropertyId",
        "dataPrnumber",
        "dataPimvarnodekey",
    ]

    value_map: dict[str, str] = {}
    for group in option_catalog:
        if not isinstance(group, dict):
            continue
        group_label = str(group.get("groupLabel") or group.get("group") or "").strip()
        if not _group_label_matches_option(group_label, key_norm):
            continue

        for option in list(group.get("options") or []):
            if not isinstance(option, dict):
                continue
            label = str(option.get("visibleLabel") or option.get("visibleValue") or "").strip()
            if not label:
                continue
            backend_hints = dict(option.get("backendHints") or {})
            mapped_value = ""
            for backend_key in preferred_backend_keys:
                candidate = backend_hints.get(backend_key)
                if candidate:
                    mapped_value = str(candidate)
                    break
            if not mapped_value:
                continue
            value_map[label] = mapped_value
            value_map[label.lower()] = mapped_value

    return value_map


def _build_inject_rules(
    request_template: dict[str, Any],
    proposal: dict[str, Any],
    spec: dict[str, Any],
    option_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    inject_rules: list[dict[str, Any]] = []
    if not request_template:
        return inject_rules

    trace_template_hints = [
        row for row in _safe_list(proposal.get("trace_template_hints")) if isinstance(row, dict)
    ]

    supported_option_keys = list(OPTION_KEY_ALIASES.keys())
    candidate_paths: dict[str, list[str]] = {key: [] for key in supported_option_keys}
    for hint in trace_template_hints:
        for mutation in _safe_list(hint.get("candidate_mutations")):
            if not isinstance(mutation, dict):
                continue
            option_key = str(mutation.get("option_key") or "").strip().lower()
            path = str(mutation.get("path") or "").strip()
            if option_key in candidate_paths and path:
                candidate_paths[option_key].append(path)

    requested_options = _safe_dict(spec.get("options"))

    for option_key in supported_option_keys:
        resolved_path = ""
        for candidate in candidate_paths.get(option_key, []):
            resolved = _convert_semantic_path(candidate, request_template)
            if resolved:
                resolved_path = resolved
                break
        if not resolved_path:
            continue

        value_map = _build_value_map_from_catalog(option_catalog, option_key)
        if not value_map and option_key in requested_options:
            current_value = json_get(request_template, resolved_path)
            if current_value is not None:
                value_map = {str(requested_options[option_key]): str(current_value)}

        rule = {
            "input_key": option_key,
            "path": resolved_path,
            "cast": "str",
            "require_mapped": True,
            "value_map": value_map,
        }
        inject_rules.append(rule)

    return inject_rules


def _build_json_adapter_record(
    run_dir: Path,
    proposal: dict[str, Any],
    llm_suggestions: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    spec = _safe_dict(_load_json(run_dir / "spec.json", default={}))
    summary = _safe_dict(_load_json(run_dir / "summary.json", default={}))
    traces = [row for row in _safe_list(_load_json(run_dir / "network_traces.json", default=[])) if isinstance(row, dict)]
    option_catalog = [
        row for row in _safe_list(_load_json(run_dir / "option_catalog.json", default=[])) if isinstance(row, dict)
    ]

    product_url = str(spec.get("url") or summary.get("url") or "").strip()
    if not product_url:
        warnings.append("missing_product_url")
        return None, warnings

    endpoint = _pick_selected_endpoint(proposal, llm_suggestions)
    trace = _find_trace_for_endpoint(traces, endpoint)
    if trace is None:
        warnings.append("missing_matching_trace")
        return None, warnings

    method = str(trace.get("method") or "").strip().upper()
    if method not in {"GET", "POST"}:
        warnings.append("unsupported_method")
        return None, warnings

    body_text = str(trace.get("post_data") or "").strip()
    request_template = {}
    if method == "POST":
        try:
            request_template = json.loads(body_text) if body_text else {}
        except Exception:
            warnings.append("post_data_not_json")
            return None, warnings
        if not isinstance(request_template, dict):
            warnings.append("post_data_not_object")
            return None, warnings

    if _contains_sensitive_text(body_text):
        warnings.append("post_data_contains_sensitive_markers")
        return None, warnings

    headers = _normalize_header_map(dict(trace.get("request_headers") or {}))
    if any(_contains_sensitive_text(f"{key}: {value}") for key, value in headers.items()):
        warnings.append("headers_contain_sensitive_markers")
        return None, warnings

    price_path = _select_price_path(summary)
    if not price_path:
        warnings.append("missing_price_path")
        return None, warnings

    inject_rules = _build_inject_rules(request_template, proposal, spec, option_catalog)
    if not inject_rules:
        warnings.append("missing_inject_rules")

    adapter_id = f"generated_{_slugify(str(proposal.get('site_name') or summary.get('site') or 'site'))}_{_slugify(run_dir.name)}"
    record = {
        "id": adapter_id,
        "enabled": True,
        "match": {
            "domains": [urlsplit(product_url).netloc.lstrip("www.")],
            "url_regex": _build_url_regex(product_url),
        },
        "endpoint": {
            "url": str(trace.get("url") or "").strip(),
            "method": method,
            "headers": headers,
        },
        "request_template": request_template,
        "inject": inject_rules,
        "response_extract": {
            "price_path": price_path,
            "currency_const": str(summary.get("currency") or spec.get("expected_currency") or "")
        },
    }

    return record, warnings


def generate_runnable_adapters(
    *,
    run_dir: str,
    output_dir: str,
    proposal: dict[str, Any],
    llm_suggestions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_path = Path(run_dir)
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    json_dir = out_root / "json_adapters"
    site_dir = out_root / "site_adapters"
    json_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)

    llm_payload = _safe_dict(llm_suggestions)
    adapter_record, warnings = _build_json_adapter_record(run_path, proposal, llm_payload)

    json_path: str | None = None
    if adapter_record is not None:
        adapter_id = str(adapter_record.get("id") or "generated_adapter")
        json_path = str(json_dir / f"{adapter_id}.json")
        Path(json_path).write_text(
            json.dumps(adapter_record, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    site_adapter_code = str(llm_payload.get("site_adapter_code") or "").strip()
    python_path: str | None = None
    site_adapter_kind = "none"
    if site_adapter_code and not _contains_sensitive_text(site_adapter_code):
        class_name = "GeneratedSiteAdapter"
        match = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*SiteAdapter)\b", site_adapter_code)
        if match:
            class_name = match.group(1)
        python_path = str(site_dir / f"{class_name}.py")
        Path(python_path).write_text(site_adapter_code + "\n", encoding="utf-8")
        site_adapter_kind = "llm_generated"
    else:
        stub = _render_adapter_stub(str(proposal.get("site_name") or "unknown"))
        python_path = str(site_dir / "adapter_stub.py")
        Path(python_path).write_text(stub + "\n", encoding="utf-8")
        site_adapter_kind = "stub_only"

    manifest = {
        "run_dir": str(run_path),
        "proposal_site": proposal.get("site_name"),
        "json_adapter": json_path,
        "site_adapter": python_path,
        "site_adapter_kind": site_adapter_kind,
        "warnings": warnings,
    }
    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    return {
        "json_adapter": json_path,
        "site_adapter": python_path,
        "site_adapter_kind": site_adapter_kind,
        "manifest": str(manifest_path),
        "warnings": warnings,
    }
