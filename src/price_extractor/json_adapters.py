from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit


def _normalize_host(value: str) -> str:
    host = str(value or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _resolve_default_adapter_dirs() -> list[Path]:
    dirs: list[Path] = []
    cwd_adapters = Path.cwd() / "adapters"
    repo_root_adapters = Path(__file__).resolve().parents[2] / "adapters"
    for path in [cwd_adapters, repo_root_adapters]:
        if path.exists() and path.is_dir() and path not in dirs:
            dirs.append(path)
    return dirs


def _is_truthy_env(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _validate_adapter_record(record: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(record, dict):
        return ["root must be a JSON object"]

    adapter_id = str(record.get("id") or "").strip()
    if not adapter_id:
        issues.append("missing required field: id")

    match = record.get("match")
    endpoint = record.get("endpoint")
    request_template = record.get("request_template")
    inject = record.get("inject")
    response_extract = record.get("response_extract")
    if not isinstance(match, dict):
        issues.append("missing or invalid required object: match")
    if not isinstance(endpoint, dict):
        issues.append("missing or invalid required object: endpoint")
    if not isinstance(request_template, dict):
        issues.append("missing or invalid required object: request_template")
    if inject is not None and not isinstance(inject, list):
        issues.append("inject must be an array when present")
    if not isinstance(response_extract, dict):
        issues.append("missing or invalid required object: response_extract")

    if issues:
        return issues

    domains = [str(item).strip() for item in list(match.get("domains") or []) if str(item).strip()]
    url_regex = str(match.get("url_regex") or "").strip()
    if not domains and not url_regex:
        issues.append("match requires at least one of: domains or url_regex")

    if url_regex:
        try:
            re.compile(url_regex)
        except re.error:
            issues.append("match.url_regex is not a valid regex")

    endpoint_url = str(endpoint.get("url") or "").strip()
    endpoint_method = str(endpoint.get("method") or "").strip().upper()
    if not endpoint_url:
        issues.append("endpoint.url must be a non-empty string")
    if endpoint_method not in {"GET", "POST"}:
        issues.append("endpoint.method must be GET or POST")

    price_path = str(response_extract.get("price_path") or "").strip()
    if not price_path:
        issues.append("response_extract.price_path must be a non-empty string")

    if isinstance(inject, list):
        allowed_casts = {"", "int", "float", "str"}
        for index, row in enumerate(inject):
            if not isinstance(row, dict):
                issues.append(f"inject[{index}] must be an object")
                continue
            input_key = str(row.get("input_key") or "").strip()
            path = str(row.get("path") or "").strip()
            cast_name = str(row.get("cast") or "").strip().lower()
            value_map = row.get("value_map")
            require_mapped = row.get("require_mapped", False)
            if not input_key:
                issues.append(f"inject[{index}].input_key must be non-empty")
            if not path:
                issues.append(f"inject[{index}].path must be non-empty")
            if cast_name not in allowed_casts:
                issues.append(f"inject[{index}].cast must be one of int|float|str")
            if value_map is not None and not isinstance(value_map, dict):
                issues.append(f"inject[{index}].value_map must be an object when present")
            if not isinstance(require_mapped, bool):
                issues.append(f"inject[{index}].require_mapped must be a boolean when present")

    return issues


def _is_valid_adapter_record(record: Any) -> bool:
    return len(_validate_adapter_record(record)) == 0


def load_json_adapters_with_diagnostics(
    adapters_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    search_dirs = [adapters_dir] if adapters_dir is not None else _resolve_default_adapter_dirs()
    adapters: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "searchedDirs": [str(path) for path in search_dirs if path is not None],
        "loadedCount": 0,
        "skippedCount": 0,
        "invalidCount": 0,
        "warnings": [],
    }

    for directory in search_dirs:
        if directory is None or not directory.exists() or not directory.is_dir():
            continue
        for file_path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception as exc:
                diagnostics["invalidCount"] = int(diagnostics.get("invalidCount", 0)) + 1
                diagnostics["warnings"].append(
                    {
                        "file": str(file_path),
                        "kind": "invalid_json",
                        "message": str(exc),
                    }
                )
                continue

            issues = _validate_adapter_record(payload)
            if issues:
                diagnostics["invalidCount"] = int(diagnostics.get("invalidCount", 0)) + 1
                diagnostics["warnings"].append(
                    {
                        "file": str(file_path),
                        "kind": "invalid_schema",
                        "issues": issues,
                    }
                )
                continue

            enabled_raw = payload.get("enabled", True)
            enabled = bool(enabled_raw)
            if not enabled:
                diagnostics["skippedCount"] = int(diagnostics.get("skippedCount", 0)) + 1
                diagnostics["warnings"].append(
                    {
                        "file": str(file_path),
                        "kind": "disabled_adapter",
                        "adapterId": str(payload.get("id") or ""),
                    }
                )
                continue

            env_gate = str(payload.get("enabled_when_env") or "").strip()
            if env_gate and not _is_truthy_env(os.getenv(env_gate)):
                diagnostics["skippedCount"] = int(diagnostics.get("skippedCount", 0)) + 1
                diagnostics["warnings"].append(
                    {
                        "file": str(file_path),
                        "kind": "disabled_by_env",
                        "adapterId": str(payload.get("id") or ""),
                        "envVar": env_gate,
                    }
                )
                continue

            payload["_adapter_file"] = str(file_path)
            adapters.append(payload)

    diagnostics["loadedCount"] = len(adapters)
    return adapters, diagnostics



def load_json_adapters(adapters_dir: Path | None = None) -> list[dict[str, Any]]:
    adapters, _diagnostics = load_json_adapters_with_diagnostics(adapters_dir=adapters_dir)
    return adapters


def match_json_adapter_with_diagnostics(
    adapters: list[dict[str, Any]],
    target_url: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    url_text = str(target_url or "").strip()
    target_host = _normalize_host(urlsplit(url_text).netloc)
    diagnostics: dict[str, Any] = {
        "targetUrl": url_text,
        "targetHost": target_host,
        "loadedCount": len(adapters),
        "matchedAdapterId": None,
        "reason": "",
    }

    if not url_text:
        diagnostics["reason"] = "missing_target_url"
        return None, diagnostics

    if not target_host:
        diagnostics["reason"] = "missing_target_host"
        return None, diagnostics

    if not adapters:
        diagnostics["reason"] = "no_adapters_loaded"
        return None, diagnostics

    host_mismatch_count = 0
    regex_mismatch_count = 0

    for adapter in adapters:
        match = dict(adapter.get("match") or {})
        domains = {
            _normalize_host(value)
            for value in list(match.get("domains") or [])
            if _normalize_host(str(value))
        }
        url_regex = str(match.get("url_regex") or "").strip()

        host_match = True
        if domains:
            host_match = target_host in domains
        if not host_match:
            host_mismatch_count += 1
            continue

        if url_regex:
            try:
                if re.search(url_regex, url_text) is None:
                    regex_mismatch_count += 1
                    continue
            except re.error:
                regex_mismatch_count += 1
                continue

        matched_id = str(adapter.get("id") or "")
        diagnostics["matchedAdapterId"] = matched_id
        diagnostics["reason"] = "matched"
        return adapter, diagnostics

    if host_mismatch_count == len(adapters):
        diagnostics["reason"] = "host_mismatch"
    elif regex_mismatch_count > 0:
        diagnostics["reason"] = "url_regex_mismatch"
    else:
        diagnostics["reason"] = "no_matching_adapter"
    return None, diagnostics


def match_json_adapter(adapters: list[dict[str, Any]], target_url: str) -> dict[str, Any] | None:
    matched, _diagnostics = match_json_adapter_with_diagnostics(adapters, target_url)
    return matched


def _tokenize_path(path: str) -> list[str | int]:
    text = str(path or "").strip()
    if text.startswith("$"):
        text = text[1:]
    if text.startswith("."):
        text = text[1:]
    if not text:
        return []

    tokens: list[str | int] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == ".":
            index += 1
            continue
        if ch == "[":
            end = text.find("]", index + 1)
            if end < 0:
                return []
            raw_idx = text[index + 1 : end].strip()
            if not raw_idx.isdigit():
                return []
            tokens.append(int(raw_idx))
            index = end + 1
            continue

        end = index
        while end < len(text) and text[end] not in ".[":
            end += 1
        key = text[index:end].strip()
        if not key:
            return []
        tokens.append(key)
        index = end

    return tokens


def json_get(payload: Any, path: str) -> Any | None:
    tokens = _tokenize_path(path)
    if not tokens:
        return None

    current: Any = payload
    for token in tokens:
        if isinstance(token, str):
            if not isinstance(current, dict) or token not in current:
                return None
            current = current[token]
            continue

        if not isinstance(current, list) or token < 0 or token >= len(current):
            return None
        current = current[token]

    return current


def json_set(payload: Any, path: str, value: Any) -> bool:
    tokens = _tokenize_path(path)
    if not tokens:
        return False

    current: Any = payload

    for idx, token in enumerate(tokens[:-1]):
        next_token = tokens[idx + 1]

        if isinstance(token, str):
            if not isinstance(current, dict):
                return False
            if token not in current or current[token] is None:
                current[token] = [] if isinstance(next_token, int) else {}
            elif isinstance(next_token, int) and not isinstance(current[token], list):
                current[token] = []
            elif isinstance(next_token, str) and not isinstance(current[token], dict):
                current[token] = {}
            current = current[token]
            continue

        if not isinstance(current, list):
            return False
        while len(current) <= token:
            current.append([] if isinstance(next_token, int) else {})
        if current[token] is None:
            current[token] = [] if isinstance(next_token, int) else {}
        elif isinstance(next_token, int) and not isinstance(current[token], list):
            current[token] = []
        elif isinstance(next_token, str) and not isinstance(current[token], dict):
            current[token] = {}
        current = current[token]

    last = tokens[-1]
    if isinstance(last, str):
        if not isinstance(current, dict):
            return False
        current[last] = value
        return True

    if not isinstance(current, list):
        return False
    while len(current) <= last:
        current.append(None)
    current[last] = value
    return True


def _cast_input_value(raw_value: Any, cast_name: str) -> Any:
    cast_type = str(cast_name or "").strip().lower()
    if cast_type == "int":
        return int(raw_value)
    if cast_type == "float":
        return float(raw_value)
    if cast_type == "str":
        return str(raw_value)
    return raw_value


def _apply_value_map(raw_value: Any, value_map: Any) -> tuple[Any, bool]:
    if not isinstance(value_map, dict) or not value_map:
        return raw_value, False

    sentinel = object()
    raw_text = str(raw_value)
    mapped = value_map.get(raw_text, sentinel)
    if mapped is not sentinel:
        return mapped, True

    normalized = raw_text.strip()
    mapped = value_map.get(normalized, sentinel)
    if mapped is not sentinel:
        return mapped, True

    lowered = normalized.lower()
    mapped = value_map.get(lowered, sentinel)
    if mapped is not sentinel:
        return mapped, True

    return raw_value, False


def apply_injections(
    request_template: dict[str, Any],
    inject_rules: list[dict[str, Any]],
    inputs: dict[str, Any],
    *,
    skipped: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = copy.deepcopy(request_template)
    applied: list[dict[str, Any]] = []

    for rule in inject_rules:
        if not isinstance(rule, dict):
            continue

        input_key = str(rule.get("input_key") or "").strip()
        path = str(rule.get("path") or "").strip()
        if not input_key or not path or input_key not in inputs:
            continue

        raw_value = inputs.get(input_key)
        value_map = rule.get("value_map")
        mapped_value, is_mapped = _apply_value_map(raw_value, value_map)
        if bool(rule.get("require_mapped", False)) and not is_mapped:
            if skipped is not None:
                skipped.append(
                    {
                        "inputKey": input_key,
                        "path": path,
                        "reason": "value_not_in_map",
                        "rawValue": str(raw_value),
                        "knownValues": sorted(value_map.keys()) if isinstance(value_map, dict) else [],
                    }
                )
            continue
        try:
            cast_value = _cast_input_value(mapped_value, str(rule.get("cast") or ""))
        except Exception:
            if skipped is not None:
                skipped.append(
                    {
                        "inputKey": input_key,
                        "path": path,
                        "reason": "cast_failed",
                        "rawValue": str(raw_value),
                        "cast": str(rule.get("cast") or ""),
                    }
                )
            continue

        if not json_set(payload, path, cast_value):
            if skipped is not None:
                skipped.append(
                    {
                        "inputKey": input_key,
                        "path": path,
                        "reason": "path_unreachable",
                        "rawValue": str(raw_value),
                    }
                )
            continue

        applied.append(
            {
                "inputKey": input_key,
                "path": path,
                "value": cast_value,
            }
        )

    return payload, applied


def _coerce_price(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None

    compact = text.replace(" ", "")
    if "," in compact and "." in compact:
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif "," in compact:
        compact = compact.replace(".", "").replace(",", ".")

    try:
        return float(compact)
    except Exception:
        return None


def build_adapter_http_candidate(
    adapter: dict[str, Any],
    *,
    target_url: str,
    effective_options: dict[str, Any],
    raw_requested_options: dict[str, Any],
) -> dict[str, Any] | None:
    endpoint = dict(adapter.get("endpoint") or {})
    endpoint_url_raw = str(endpoint.get("url") or "").strip()
    method = str(endpoint.get("method") or "POST").strip().upper()
    if not endpoint_url_raw or method not in {"GET", "POST"}:
        return None

    endpoint_url = endpoint_url_raw if "://" in endpoint_url_raw else urljoin(str(target_url or ""), endpoint_url_raw)

    headers = {
        str(key): str(value)
        for key, value in dict(endpoint.get("headers") or {}).items()
        if str(key).strip()
    }
    if method == "POST":
        header_keys = {str(key).lower() for key in headers}
        if "content-type" not in header_keys:
            headers["content-type"] = "application/json"

    request_template = dict(adapter.get("request_template") or {})
    merged_inputs = dict(raw_requested_options)
    merged_inputs.update(dict(effective_options))
    inject_rules = [row for row in list(adapter.get("inject") or []) if isinstance(row, dict)]
    skipped_injections: list[dict[str, Any]] = []
    payload, applied_injections = apply_injections(
        request_template,
        inject_rules,
        merged_inputs,
        skipped=skipped_injections,
    )

    rule_input_keys = {
        str(rule.get("input_key") or "").strip()
        for rule in inject_rules
        if str(rule.get("input_key") or "").strip()
    }
    unsupported_inputs = [
        {"inputKey": key, "rawValue": str(raw_requested_options.get(key))}
        for key in raw_requested_options
        if str(key).strip() and key not in rule_input_keys
    ]

    body_text: str | None = None
    if method == "POST":
        body_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    adapter_id = str(adapter.get("id") or "json_adapter")
    trace_override = {
        "url": endpoint_url,
        "method": method,
        "resource_type": "xhr",
        "status": 200,
        "request_headers": dict(headers),
        "response_content_type": "application/json",
        "post_data": body_text or "",
    }

    return {
        "url": endpoint_url,
        "method": method,
        "source": f"json_adapter:{adapter_id}",
        "request_headers": headers,
        "body_text": body_text,
        "trace_override": trace_override,
        "request_template_applied": {
            "kind": "json_adapter",
            "adapterId": adapter_id,
            "injected": applied_injections,
            "skipped": skipped_injections,
            "unsupportedInputs": unsupported_inputs,
        },
        "adapter_response_extract": dict(adapter.get("response_extract") or {}),
        "adapter_inputs_used": merged_inputs,
    }


def extract_adapter_result(
    payload: Any,
    *,
    response_extract: dict[str, Any],
    inputs_used: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(response_extract, dict):
        return None

    price_path = str(response_extract.get("price_path") or "").strip()
    if not price_path:
        return None

    price_raw = json_get(payload, price_path)
    price = _coerce_price(price_raw)
    if price is None:
        return None

    currency_const = str(response_extract.get("currency_const") or "").strip()
    currency_path = str(response_extract.get("currency_path") or "").strip()
    if currency_const:
        currency = currency_const
    elif currency_path:
        currency = str(json_get(payload, currency_path) or "").strip()
    else:
        currency = ""

    config_summary: Any = None
    config_path = str(response_extract.get("config_summary_path") or "").strip()
    if config_path:
        config_summary = json_get(payload, config_path)
    if config_summary is None:
        config_summary = dict(inputs_used) if inputs_used else None

    return {
        "price": round(float(price), 2),
        "currency": currency,
        "config_summary": config_summary,
        "source": "adapter",
    }
