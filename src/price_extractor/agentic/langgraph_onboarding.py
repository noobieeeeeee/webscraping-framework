from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib import request, error as urlerror
from urllib.parse import unquote_plus

from .onboarding import build_onboarding_proposal


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _infer_patch_change_scope(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./")
    if normalized == "src/price_extractor/site_adapters.py":
        return "adapter_only"
    if normalized == "tests/test_regressions.py" or normalized.startswith("tests/fixtures/"):
        return "adapter_validation"
    if normalized.startswith("src/price_extractor/"):
        return "core_framework"
    return "unknown"


def _safe_load_json(path: Path, *, default: Any) -> Any:
    if not path.exists() or not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return default


def _extract_form_keys(form_body: str, *, max_keys: int) -> list[str]:
    body = str(form_body or "")
    if not body:
        return []

    keys: list[str] = []
    for part in body.split("&"):
        if "=" not in part:
            continue
        key = unquote_plus(part.split("=", 1)[0])
        key = str(key or "").strip()
        if not key:
            continue
        if key not in keys:
            keys.append(key)
        if len(keys) >= max(1, int(max_keys)):
            break
    return keys


def _extract_response_signals(preview: str | None) -> dict[str, Any]:
    text = str(preview or "")
    if not text:
        return {
            "has_preview": False,
            "contains_ws_ajax": False,
            "contains_csrf_antiforge": False,
            "prPrice_values": [],
            "json_keys_sample": [],
        }

    json_keys: list[str] = []
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                json_keys = [str(k) for k in list(obj.keys())[:24]]
        except Exception:
            json_keys = []

    pr_prices = re.findall(r"prPrice='([0-9]+(?:\.[0-9]+)?)'", text)
    return {
        "has_preview": True,
        "contains_ws_ajax": "WS-Ajax" in text,
        "contains_csrf_antiforge": "csrf_antiforge" in text,
        "prPrice_values": pr_prices[:5],
        "json_keys_sample": json_keys,
    }


def _build_llm_run_evidence(
    *,
    run_dir: str,
    top_endpoints: list[dict[str, Any]],
    max_endpoint_evidence: int = 8,
    max_pricing_candidates: int = 4,
) -> dict[str, Any]:
    run_path = Path(str(run_dir))
    spec = _safe_load_json(run_path / "spec.json", default={})
    summary = _safe_load_json(run_path / "summary.json", default={})
    traces = _safe_load_json(run_path / "network_traces.json", default=[])
    if not isinstance(spec, dict):
        spec = {}
    if not isinstance(summary, dict):
        summary = {}
    if not isinstance(traces, list):
        traces = []

    trace_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in traces:
        if not isinstance(row, dict):
            continue
        method = str(row.get("method") or "").upper().strip()
        url = str(row.get("url") or "").strip()
        if not method or not url:
            continue
        trace_by_key.setdefault((method, url), row)

    endpoint_evidence: list[dict[str, Any]] = []
    for ep in list(top_endpoints or [])[: max(1, int(max_endpoint_evidence))]:
        if not isinstance(ep, dict):
            continue
        url = str(ep.get("url") or "").strip()
        method = str(ep.get("method") or "").upper().strip()
        trace = trace_by_key.get((method, url))
        trace_dict = trace if isinstance(trace, dict) else {}

        body_for_keys = str(trace_dict.get("post_data_preview") or trace_dict.get("post_data") or "")
        endpoint_evidence.append(
            {
                "score": int(ep.get("score", 0) or 0),
                "role": str(ep.get("role") or ""),
                "confidence": float(ep.get("confidence", 0.0) or 0.0),
                "method": method,
                "url": url,
                "status": int(trace_dict.get("status", ep.get("status", 0) or 0) or 0),
                "resource_type": str(trace_dict.get("resource_type") or ""),
                "request_content_type": str(trace_dict.get("request_content_type") or ""),
                "response_content_type": str(trace_dict.get("response_content_type") or ""),
                "post_data_keys_sample": _extract_form_keys(body_for_keys, max_keys=24),
                "response_signals": _extract_response_signals(trace_dict.get("response_body_preview")),
            }
        )

    pricing_candidates: list[dict[str, Any]] = []
    for row in traces:
        if not isinstance(row, dict):
            continue
        method = str(row.get("method") or "").upper().strip()
        url = str(row.get("url") or "").strip()
        if not method or not url:
            continue

        response_ct = str(row.get("response_content_type") or "")
        if "json" not in response_ct.lower():
            continue

        signals = _extract_response_signals(row.get("response_body_preview"))
        if not bool(signals.get("has_preview")):
            continue
        if not (bool(signals.get("contains_ws_ajax")) or list(signals.get("prPrice_values") or [])):
            continue

        body_for_keys = str(row.get("post_data_preview") or row.get("post_data") or "")
        pricing_candidates.append(
            {
                "method": method,
                "url": url,
                "status": int(row.get("status", 0) or 0),
                "resource_type": str(row.get("resource_type") or ""),
                "request_content_type": str(row.get("request_content_type") or ""),
                "response_content_type": response_ct,
                "post_data_keys_sample": _extract_form_keys(body_for_keys, max_keys=24),
                "response_signals": signals,
            }
        )

    def candidate_sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
        signals = dict(item.get("response_signals") or {})
        has_pr_price = 1 if list(signals.get("prPrice_values") or []) else 0
        has_ws_ajax = 1 if bool(signals.get("contains_ws_ajax")) else 0
        status = int(item.get("status", 0) or 0)
        return (-has_pr_price, -has_ws_ajax, -status)

    pricing_candidates = sorted(pricing_candidates, key=candidate_sort_key)[: max(1, int(max_pricing_candidates))]

    spec_options = spec.get("options")
    spec_options = spec_options if isinstance(spec_options, dict) else {}

    summary_mismatches = summary.get("mismatches")
    summary_mismatches = summary_mismatches if isinstance(summary_mismatches, list) else []

    return {
        "spec": {
            "product_type": str(spec.get("product_type") or ""),
            "expected_price": spec.get("expected_price"),
            "expected_price_tolerance": spec.get("expected_price_tolerance"),
            "expected_currency": str(spec.get("expected_currency") or ""),
            "requested_options": spec_options,
        },
        "run_summary": {
            "validated": bool(summary.get("validated")),
            "observed_price": summary.get("price"),
            "currency": str(summary.get("currency") or ""),
            "complexity": str(summary.get("complexity") or ""),
            "strategy": str(summary.get("strategy") or ""),
            "mismatches": [str(m) for m in summary_mismatches[:3]],
        },
        "endpoint_evidence": endpoint_evidence,
        "pricing_candidate_evidence": pricing_candidates,
    }


def _resolve_llm_settings(
    *,
    llm_enable: bool,
    llm_model: str,
    llm_base_url: str | None,
    llm_api_key_env: str,
    llm_timeout_seconds: float,
) -> dict[str, Any]:
    base_url = str(llm_base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
    api_key_name = str(llm_api_key_env or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
    api_key = str(os.getenv(api_key_name) or "").strip()
    return {
        "enabled": bool(llm_enable),
        "model": str(llm_model or "gpt-4.1-mini").strip() or "gpt-4.1-mini",
        "base_url": base_url,
        "api_key_env": api_key_name,
        "api_key": api_key,
        "api_key_present": bool(api_key),
        "timeout_seconds": max(float(llm_timeout_seconds or 60.0), 1.0),
    }


def _load_dotenv_file(path: str | None = None) -> dict[str, str]:
    dotenv_path = Path(str(path or ".env"))
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = str(key or "").strip()
        if not name:
            continue
        value_text = str(value or "").strip().strip('"').strip("'")
        if name not in os.environ:
            os.environ[name] = value_text
            loaded[name] = value_text
    return loaded


def _is_patch_target_allowed(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./")
    if not normalized:
        return False
    if normalized == "src/price_extractor/site_adapters.py":
        return True
    if normalized == "tests/test_regressions.py":
        return True
    if normalized.startswith("tests/fixtures/"):
        return True
    return False


def _contains_sensitive_patch_content(text: str) -> bool:
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


def _is_unresolvable_site_adapter_import_patch(path: str, content: str) -> bool:
    normalized_path = str(path or "").strip().replace("\\", "/").lstrip("./")
    if normalized_path != "src/price_extractor/site_adapters.py":
        return False
    text = str(content or "")
    if not text.strip():
        return False
    import_pattern = r"^\s*from\s+\.[A-Za-z_][A-Za-z0-9_]*\s+import\s+[A-Za-z_][A-Za-z0-9_]*SiteAdapter\b"
    return bool(re.search(import_pattern, text, flags=re.MULTILINE))


def _normalize_patch_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None

    path = str(record.get("path") or "").strip().replace("\\", "/").lstrip("./")
    if not _is_patch_target_allowed(path):
        return None

    operation = str(record.get("operation") or "update").strip().lower()
    if operation not in {"add", "update", "insert", "delete"}:
        operation = "update"

    content = str(record.get("content") or "")
    if content and _contains_sensitive_patch_content(content):
        return None

    rationale = str(record.get("rationale") or "").strip()
    intent = str(record.get("intent") or "").strip()
    anchor = str(record.get("anchor") or "").strip()
    position = str(record.get("position") or "").strip().lower()

    normalized: dict[str, Any] = {
        "path": path,
        "operation": operation,
        "changeScope": _infer_patch_change_scope(path),
    }
    if content:
        normalized["content"] = content
    if rationale:
        normalized["rationale"] = rationale
    if intent:
        normalized["intent"] = intent
    if anchor:
        normalized["anchor"] = anchor
    if position in {"before", "after"}:
        normalized["position"] = position
    return normalized


def _extract_generated_adapter_class_name(site_name: str, site_adapter_code: str) -> str:
    match = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*SiteAdapter)\b", str(site_adapter_code or ""))
    if match:
        return str(match.group(1))
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", str(site_name or "").strip())
    parts = [part for part in cleaned.split() if part]
    class_prefix = "".join(part[:1].upper() + part[1:] for part in parts) or "Generated"
    return f"{class_prefix}SiteAdapter"


def _build_structured_site_adapter_patches(
    *,
    site_name: str,
    site_adapter_code: str,
) -> list[dict[str, Any]]:
    class_name = _extract_generated_adapter_class_name(site_name, site_adapter_code)
    return [
        {
            "path": "src/price_extractor/site_adapters.py",
            "operation": "insert",
            "anchor": "_DEFAULT_ADAPTERS: list[SiteAdapter] | None = None",
            "position": "before",
            "intent": f"Insert generated SiteAdapter class for {site_name}",
            "content": str(site_adapter_code),
            "rationale": "Place generated adapter class near the adapter registry for easier review.",
        },
        {
            "path": "src/price_extractor/site_adapters.py",
            "operation": "insert",
            "anchor": "_DEFAULT_ADAPTERS = [",
            "position": "after",
            "intent": "Register generated adapter in default adapter list",
            "content": f"            {class_name}(),",
            "rationale": "Enable candidate synthesis through the normal site-adapter dispatch path.",
        },
    ]


def _evaluate_rewrite_evidence(
    *,
    selected_endpoint: str,
    trace_template_hints: list[dict[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    selected_hint: dict[str, Any] = {}
    matched_selected_endpoint = False

    for row in trace_template_hints:
        if not isinstance(row, dict):
            continue
        endpoint_url = str(row.get("endpoint_url") or "")
        if selected_endpoint and endpoint_url == selected_endpoint:
            selected_hint = row
            matched_selected_endpoint = True
            break

    if not selected_hint and trace_template_hints:
        selected_hint = dict(trace_template_hints[0])

    if not trace_template_hints:
        reasons.append("missing_trace_template_hints")
    if selected_endpoint and not matched_selected_endpoint:
        reasons.append("selected_endpoint_trace_hint_missing")

    candidate_mutations = [
        row
        for row in list(selected_hint.get("candidate_mutations") or [])
        if isinstance(row, dict) and str(row.get("path") or "").strip()
    ]
    if not candidate_mutations:
        reasons.append("missing_candidate_mutations")

    core_option_keys = {"quantity", "format", "material", "pages"}
    has_core_mutation = any(
        str(row.get("option_key") or "").strip().lower() in core_option_keys
        for row in candidate_mutations
    )
    if candidate_mutations and not has_core_mutation:
        reasons.append("missing_core_option_mutations")

    return {
        "sufficient": len(reasons) == 0,
        "reasons": reasons,
        "selectedEndpoint": selected_endpoint,
        "matchedSelectedEndpoint": matched_selected_endpoint,
        "selectedTraceTemplateHint": selected_hint,
    }


def _build_proposed_file_patches(
    site_name: str,
    llm_suggestions: dict[str, Any],
    *,
    rewrite_evidence_gate: dict[str, Any],
) -> list[dict[str, Any]]:
    patches: list[dict[str, Any]] = []
    evidence_sufficient = bool(rewrite_evidence_gate.get("sufficient", False))

    for raw in list(llm_suggestions.get("proposed_file_patches") or []):
        normalized = _normalize_patch_record(raw)
        if normalized is None:
            continue
        if _is_unresolvable_site_adapter_import_patch(
            str(normalized.get("path") or ""),
            str(normalized.get("content") or ""),
        ):
            continue
        if (
            str(normalized.get("path") or "") == "src/price_extractor/site_adapters.py"
            and not evidence_sufficient
        ):
            continue
        patches.append(normalized)

    site_adapter_code = str(llm_suggestions.get("site_adapter_code") or "").strip()
    has_site_adapter_patch = any(
        str(row.get("path") or "") == "src/price_extractor/site_adapters.py"
        for row in patches
        if isinstance(row, dict)
    )
    if (
        site_adapter_code
        and evidence_sufficient
        and not has_site_adapter_patch
        and not _contains_sensitive_patch_content(site_adapter_code)
    ):
        patches.extend(
            _build_structured_site_adapter_patches(
                site_name=site_name,
                site_adapter_code=site_adapter_code,
            )
        )

    regression_assertions = [
        str(item).strip()
        for item in list(llm_suggestions.get("regression_assertions") or [])
        if str(item).strip()
    ]
    has_regression_patch = any(
        str(row.get("path") or "") == "tests/test_regressions.py"
        for row in patches
        if isinstance(row, dict)
    )
    if regression_assertions and not has_regression_patch:
        rendered_assertions = "\n".join(f"# - {item}" for item in regression_assertions[:10])
        patches.append(
            {
                "path": "tests/test_regressions.py",
                "operation": "insert",
                "anchor": "class RegressionTests(unittest.TestCase):",
                "position": "after",
                "intent": "Add generated regression-test TODO scaffold",
                "content": (
                    "\n\n"
                    "    # TODO(agentic): convert onboarding regression assertions into concrete unit tests.\n"
                    f"{rendered_assertions}\n"
                ),
                "rationale": "Keep test additions explicit and reviewable before applying generated adapter code.",
            }
        )

    normalized_unique: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for row in patches:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("path") or ""),
            str(row.get("operation") or ""),
            str(row.get("anchor") or ""),
            str(row.get("content") or ""),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        normalized_unique.append(row)

    return normalized_unique[:8]


def _build_review_patch_plan(proposal: dict[str, Any], llm_suggestions: dict[str, Any]) -> dict[str, Any]:
    site_name = str(proposal.get("site_name") or "unknown")
    top_endpoints = list(proposal.get("top_endpoints") or [])
    selected_endpoint = str((top_endpoints[0] or {}).get("url") or "") if top_endpoints else ""
    trace_template_hints = [
        row
        for row in list(proposal.get("trace_template_hints") or [])
        if isinstance(row, dict)
    ]

    rewrite_evidence_gate = _evaluate_rewrite_evidence(
        selected_endpoint=selected_endpoint,
        trace_template_hints=trace_template_hints,
    )
    selected_trace_hint = dict(rewrite_evidence_gate.get("selectedTraceTemplateHint") or {})

    recommendations = list(llm_suggestions.get("recommendations") or []) if isinstance(llm_suggestions, dict) else []
    adapter_hints = dict(llm_suggestions.get("adapter_hints") or {}) if isinstance(llm_suggestions, dict) else {}
    run_strategy = dict(llm_suggestions.get("run_strategy") or {}) if isinstance(llm_suggestions, dict) else {}
    safety_checks = list(llm_suggestions.get("safety_checks") or []) if isinstance(llm_suggestions, dict) else []
    proposed_file_patches = _build_proposed_file_patches(
        site_name,
        llm_suggestions,
        rewrite_evidence_gate=rewrite_evidence_gate,
    )

    if not safety_checks:
        safety_checks = [
            "Validate in --request-only mode before any persistence/promotion.",
            "Do not auto-apply generated code changes.",
            "Add/extend regression fixtures before enabling at scale.",
            "Require approved proxy/egress policy before running live extraction.",
            "Keep safe concurrency defaults and bounded retries/backoff to avoid retry storms.",
            "Trip stop conditions on repeated 403/429 or sustained 5xx spikes.",
        ]
    if not bool(rewrite_evidence_gate.get("sufficient", False)):
        reasons = [str(item) for item in list(rewrite_evidence_gate.get("reasons") or []) if str(item)]
        if reasons:
            safety_checks.append(
                "Refuse generated SiteAdapter patch due to insufficient rewrite evidence: " + ", ".join(reasons)
            )
    if proposed_file_patches:
        safety_checks.append("Review proposedFilePatches content and apply manually; auto-apply remains disabled.")

    target_files = [
        {
            "path": "src/price_extractor/site_adapters.py",
            "intent": f"Add or extend adapter logic for {site_name}",
        },
        {
            "path": "tests/test_regressions.py",
            "intent": "Add fixture-driven synthesis regression tests",
        },
        {
            "path": "tests/fixtures/",
            "intent": "Store sanitized trace/template fixtures",
        },
    ]

    patch_outline = [
        "Identify best PRICING/QUANTITY endpoint from onboarding proposal.",
        "Implement minimal deterministic transform logic in site adapter.",
        "Keep request-only execution path unchanged except candidate synthesis.",
        "Add regression tests using offline fixtures and assert requestTemplateApplied metadata.",
    ]
    if recommendations:
        patch_outline.extend([str(item) for item in recommendations[:6]])
    if not bool(rewrite_evidence_gate.get("sufficient", False)):
        patch_outline.append("Collect additional traces/options before generating site-adapter rewrite code.")

    return {
        "kind": "review_patch_plan_v1",
        "patchSchema": "v2",
        "workflowStage": "patch_plan",
        "site": site_name,
        "selectedEndpoint": selected_endpoint,
        "selectedTraceTemplateHint": selected_trace_hint,
        "traceTemplateHintCount": len(trace_template_hints),
        "rewriteEvidenceGate": rewrite_evidence_gate,
        "targetFiles": target_files,
        "patchOutline": patch_outline,
        "proposedFilePatches": proposed_file_patches,
        "adapterHints": adapter_hints,
        "runStrategy": run_strategy,
        "safetyChecks": safety_checks,
        "reviewRequired": True,
        "autoApply": False,
    }


def _select_recipe_endpoint(
    proposal: dict[str, Any],
    llm_suggestions: dict[str, Any],
) -> dict[str, Any]:
    top_endpoints = [
        row
        for row in list(proposal.get("top_endpoints") or [])
        if isinstance(row, dict)
    ]
    if not top_endpoints:
        return {
            "method": "",
            "url": "",
            "source": "none",
            "confidence": 0.0,
            "score": 0,
        }

    adapter_hints = dict(llm_suggestions.get("adapter_hints") or {})
    selected = dict(adapter_hints.get("selected_endpoint") or {})
    selected_url = str(selected.get("url") or "").strip()
    selected_method = str(selected.get("method") or "").strip().upper()
    if selected_url:
        for row in top_endpoints:
            row_url = str(row.get("url") or "").strip()
            row_method = str(row.get("method") or "").strip().upper()
            if row_url == selected_url and (not selected_method or row_method == selected_method):
                return {
                    "method": row_method,
                    "url": row_url,
                    "source": "llm_selected_endpoint",
                    "confidence": float(row.get("confidence", 0.0) or 0.0),
                    "score": int(row.get("score", 0) or 0),
                }

    first = dict(top_endpoints[0])
    return {
        "method": str(first.get("method") or "").strip().upper(),
        "url": str(first.get("url") or "").strip(),
        "source": "top_endpoint_fallback",
        "confidence": float(first.get("confidence", 0.0) or 0.0),
        "score": int(first.get("score", 0) or 0),
    }


def _build_extraction_recipe(
    proposal: dict[str, Any],
    llm_suggestions: dict[str, Any],
    patch_plan: dict[str, Any],
) -> dict[str, Any]:
    site_name = str(proposal.get("site_name") or "unknown")
    product_url = str(proposal.get("product_url") or "")
    selected_endpoint = _select_recipe_endpoint(proposal, llm_suggestions)

    top_endpoints = [
        {
            "method": str(row.get("method") or "").strip().upper(),
            "url": str(row.get("url") or "").strip(),
            "score": int(row.get("score", 0) or 0),
            "confidence": float(row.get("confidence", 0.0) or 0.0),
        }
        for row in list(proposal.get("top_endpoints") or [])
        if isinstance(row, dict)
    ][:8]

    trace_template_hints = [
        row
        for row in list(proposal.get("trace_template_hints") or [])
        if isinstance(row, dict)
    ]
    selected_trace_hint = dict(patch_plan.get("selectedTraceTemplateHint") or {})
    candidate_mutations = [
        row
        for row in list(selected_trace_hint.get("candidate_mutations") or [])
        if isinstance(row, dict)
    ]

    quantity_paths = [
        str(row.get("path") or "").strip()
        for row in candidate_mutations
        if str(row.get("option_key") or "").strip().lower() == "quantity" and str(row.get("path") or "").strip()
    ]

    runtime_policy = {
        "requiresApprovedEgressProxy": True,
        "safeConcurrencyDefault": {
            "enabled": True,
            "maxWorkers": 4,
            "reason": "Avoid fan-out storms during live extraction.",
        },
        "retryPolicy": {
            "boundedRetries": True,
            "maxRetries": 3,
            "exponentialBackoff": True,
            "jitter": True,
            "perHostPacing": True,
            "tripOnRetryStorm": True,
        },
        "stopConditions": {
            "on403Spike": True,
            "on429Spike": True,
            "onSustained5xxSpike": True,
            "haltBeforePromotion": True,
        },
        "promotionGuards": {
            "forbidSafetyGateBypass": True,
            "requireRequestOnlyValidation": True,
        },
    }

    return {
        "kind": "extraction_recipe_v1",
        "workflow": [
            "evidence",
            "extraction_recipe",
            "recipe_critique",
            "patch_plan",
            "patch_critique",
            "deterministic_gate",
        ],
        "site": site_name,
        "productUrl": product_url,
        "evidenceBundleRefs": {
            "requiresSession": bool(proposal.get("requires_session")),
            "tokenIndicators": [str(item) for item in list(proposal.get("token_indicators") or []) if str(item)],
            "traceTemplateHintCount": len(trace_template_hints),
            "optionCatalogGroups": int(proposal.get("option_catalog_groups", 0) or 0),
            "topEndpointCount": len(top_endpoints),
        },
        "sessionStrategy": {
            "requiresSession": bool(proposal.get("requires_session")),
            "tokenIndicators": [str(item) for item in list(proposal.get("token_indicators") or []) if str(item)],
            "cookieAndHeaderProvenance": "bootstrap_and_trace",
            "refreshRule": "refresh_when_drift_or_expiry_detected",
        },
        "replayStrategy": {
            "mode": "request_only_http_or_adapter",
            "selectedEndpoint": selected_endpoint,
            "topEndpointCandidates": top_endpoints,
            "allowDomFallback": False,
            "allowHeuristicFallback": False,
        },
        "requestSynthesisStrategy": {
            "templateDriven": True,
            "selectedTraceTemplateHint": selected_trace_hint,
            "candidateMutations": candidate_mutations,
            "insufficientEvidence": not bool((patch_plan.get("rewriteEvidenceGate") or {}).get("sufficient")),
            "insufficientEvidenceReasons": [
                str(item)
                for item in list((patch_plan.get("rewriteEvidenceGate") or {}).get("reasons") or [])
                if str(item)
            ],
        },
        "quantityStrategy": {
            "required": True,
            "candidatePaths": quantity_paths,
            "semanticMatchRequired": True,
            "forbidInterpolationAssumptions": True,
        },
        "validationStrategy": {
            "requiresIntendedVsAcceptedComparison": True,
            "requiresAcceptedConfigProofForValidated": True,
            "priceWithoutAcceptedConfigIsOnlyObserved": True,
            "quantityValidationIsMandatory": True,
            "forbidInterpolationAssumptions": True,
        },
        "priceExtractionStrategy": {
            "requiresDeterministicSource": True,
            "priceSignals": [
                "response_json_price_path",
                "response_signal_prPrice_values",
            ],
            "currencyRule": "use_expected_or_response_currency_without_guessing",
        },
        "runtimeSafetyPolicy": runtime_policy,
        "persistencePolicy": {
            "persistOnlyOnDeterministicValidated": True,
            "promoteOnlyAfterDeterministicGate": True,
            "forbidPoisonedNormalizations": True,
        },
        "suggestedNextSteps": [
            str(item)
            for item in list(proposal.get("suggested_next_steps") or [])
            if str(item)
        ][:12],
    }


def _make_critic_result(
    *,
    critic_id: str,
    domain: str,
    verdict: str,
    score: int,
    summary: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "criticId": critic_id,
        "domain": domain,
        "verdict": verdict,
        "score": max(0, min(int(score), 100)),
        "summary": summary,
        "findings": findings,
    }


def _build_critic_prompt_registry() -> dict[str, Any]:
    system_quality_rules = (
        "You are a strict production readiness critic. "
        "Ground every judgement in provided evidence only. "
        "Do not speculate beyond context. "
        "Use 'block' for clear safety/correctness violations, 'warn' for ambiguity or missing proof, and 'pass' only when evidence is explicit."
    )
    user_evaluation_protocol = (
        "Evaluation protocol:\n"
        "- Apply a conservative reliability rubric, prioritizing correctness over optimism.\n"
        "- findings must be concrete and non-empty for non-pass verdicts.\n"
        "- If evidence is insufficient, prefer 'warn' with actionable detail.\n"
        "- score must reflect confidence (0=unsupported, 100=fully evidenced)."
    )

    output_schema = {
        "type": "object",
        "required": ["verdict", "score", "summary", "findings"],
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "warn", "block"]},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "title", "detail"],
                    "properties": {
                        "severity": {"type": "string", "enum": ["blocker", "warn", "info"]},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                },
            },
        },
    }

    recipe_critics = [
        {
            "criticId": "recipe_evidence_sufficiency",
            "domain": "evidence",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are an evidence sufficiency critic for deterministic extraction recipes. "
                "Be strict and reject claims not grounded in provided evidence. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate evidence sufficiency for the extraction recipe below. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "recipe_validation_integrity",
            "domain": "validation",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are a validation integrity critic. Enforce intended-vs-accepted comparison, "
                "quantity validation, and no price trust without accepted-config proof. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate validation integrity for this extraction recipe. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "recipe_determinism_request_only",
            "domain": "determinism",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are a determinism critic for request-only workflows. "
                "Reject any dependence on DOM fallback, heuristic fallback, or non-replay extraction. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate request-only determinism for this recipe and context. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "recipe_runtime_safety_compliance",
            "domain": "runtime_safety",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are a runtime safety critic. Enforce egress/proxy policy, retry storm prevention, "
                "safe concurrency, and stop conditions on 403/429/5xx spikes. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate runtime safety policy for this recipe. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "recipe_quantity_strategy_integrity",
            "domain": "quantity",
            "vetoOnBlock": False,
            "systemPrompt": (
                "You are a quantity strategy critic. Ensure quantity mutation and semantic acceptance validation "
                "are explicit and non-interpolative. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate quantity strategy integrity for this recipe. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "recipe_extraction_correctness",
            "domain": "extraction",
            "vetoOnBlock": False,
            "systemPrompt": (
                "You are an extraction correctness critic. Focus on endpoint selection, request synthesis fidelity, "
                "and price/currency extraction correctness. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate extraction correctness for this recipe. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
    ]

    patch_critics = [
        {
            "criticId": "patch_scope_router",
            "domain": "scope",
            "vetoOnBlock": False,
            "systemPrompt": (
                "You are a patch scope critic. Determine whether changes are adapter-only or affect framework/runtime/storage "
                "and adjust strictness accordingly. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate patch scope and routing risk. Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "patch_runtime_safety_guard",
            "domain": "runtime_safety",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are a patch runtime safety critic. Reject patches that bypass request-only constraints, "
                "rate limits, backoff, or circuit breakers. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate runtime safety bypass risk in proposed patches. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "patch_evidence_alignment",
            "domain": "evidence",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are a patch-evidence alignment critic. Reject patches that exceed available rewrite evidence "
                "or violate evidence gates. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate evidence alignment for proposed patch operations. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
        {
            "criticId": "patch_validation_integrity",
            "domain": "validation",
            "vetoOnBlock": True,
            "systemPrompt": (
                "You are a patch validation critic. Ensure patches preserve intended-vs-accepted validation integrity "
                "and quantity acceptance proof requirements. "
                + system_quality_rules
            ),
            "userPromptTemplate": (
                "Evaluate whether proposed patches preserve validation integrity. "
                "Return JSON matching the schema exactly.\n\n"
                + user_evaluation_protocol
                + "\n\n"
                "Context:\n{{CONTEXT_JSON}}"
            ),
        },
    ]

    return {
        "schemaVersion": "critic_prompt_registry_v1",
        "outputSchema": output_schema,
        "recipe": recipe_critics,
        "patch": patch_critics,
    }


def _coerce_llm_critic_result(
    *,
    critic_spec: dict[str, Any],
    response_obj: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    critic_id = str(critic_spec.get("criticId") or "unknown")
    domain = str(critic_spec.get("domain") or "unknown")

    if error:
        return {
            "criticId": critic_id,
            "domain": domain,
            "verdict": "warn",
            "score": 0,
            "summary": "llm_critic_error",
            "findings": [
                {
                    "severity": "warn",
                    "title": "LLM critic execution failed",
                    "detail": str(error),
                }
            ],
            "source": "llm_prompt",
            "status": "error",
            "errorCategory": "execution_error",
        }

    obj = dict(response_obj or {})
    verdict = str(obj.get("verdict") or "warn").strip().lower()
    if verdict not in {"pass", "warn", "block"}:
        verdict = "warn"
    score_value = obj.get("score", 0)
    try:
        score = max(0, min(int(score_value), 100))
    except Exception:
        score = 0
    summary = str(obj.get("summary") or "")

    findings_input = list(obj.get("findings") or []) if isinstance(obj.get("findings"), list) else []
    findings: list[dict[str, Any]] = []
    for row in findings_input[:20]:
        if not isinstance(row, dict):
            continue
        severity = str(row.get("severity") or "warn").strip().lower()
        if severity not in {"blocker", "warn", "info"}:
            severity = "warn"
        title = str(row.get("title") or "").strip()
        detail = str(row.get("detail") or "").strip()
        if not title and not detail:
            continue
        if not title:
            title = "unspecified_issue"
        if not detail:
            detail = "no_detail_provided_by_model"
        findings.append(
            {
                "severity": severity,
                "title": title,
                "detail": detail,
            }
        )

    nonpass_quality_issues: list[str] = []
    if verdict in {"warn", "block"}:
        if not summary.strip():
            nonpass_quality_issues.append("missing_summary_for_nonpass_verdict")
        if len(findings) == 0:
            nonpass_quality_issues.append("missing_findings_for_nonpass_verdict")

    if nonpass_quality_issues:
        return {
            "criticId": critic_id,
            "domain": domain,
            "verdict": "warn",
            "score": 0,
            "summary": "llm_critic_noncompliant_output",
            "findings": [
                {
                    "severity": "warn",
                    "title": "LLM critic output violated schema-quality rules",
                    "detail": ", ".join(nonpass_quality_issues),
                }
            ],
            "source": "llm_prompt",
            "status": "error",
            "errorCategory": "output_schema_quality",
        }

    return {
        "criticId": critic_id,
        "domain": domain,
        "verdict": verdict,
        "score": score,
        "summary": summary,
        "findings": findings,
        "source": "llm_prompt",
        "status": "ok",
    }


def _timestamp_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit_llm_critic_log(*, enabled: bool, stage_name: str, message: str) -> None:
    if not bool(enabled):
        return
    print(f"[llm-critics][{_timestamp_utc()}][{stage_name}] {message}", file=sys.stderr, flush=True)


def _extract_retry_delay_seconds(error_text: str) -> float | None:
    text = str(error_text or "")
    patterns = [
        r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s",
        r"\"retryDelay\"\s*:\s*\"([0-9]+)s\"",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return max(float(match.group(1)), 0.0)
        except Exception:
            continue
    return None


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    limit = max(32, int(max_chars or 0))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _first_findings(critic: dict[str, Any], *, max_items: int = 1, detail_chars: int = 240) -> list[dict[str, str]]:
    findings = []
    for row in list(critic.get("findings") or [])[: max(0, int(max_items))]:
        if not isinstance(row, dict):
            continue
        findings.append(
            {
                "severity": str(row.get("severity") or ""),
                "title": _truncate_text(row.get("title") or "", 80),
                "detail": _truncate_text(row.get("detail") or "", detail_chars),
            }
        )
    return findings


def _compact_critic_context(
    *,
    context_payload: dict[str, Any],
    stage_name: str,
    max_chars: int,
) -> dict[str, Any]:
    proposal = dict(context_payload.get("proposal") or {})
    extraction_recipe = dict(context_payload.get("extractionRecipe") or {})
    patch_plan = dict(context_payload.get("patchPlan") or {})

    top_endpoints = [
        {
            "method": str(row.get("method") or ""),
            "url": str(row.get("url") or ""),
            "score": int(row.get("score") or 0),
            "confidence": float(row.get("confidence") or 0.0),
        }
        for row in list(proposal.get("top_endpoints") or [])
        if isinstance(row, dict)
    ][:3]

    trace_hints = []
    for row in list(proposal.get("trace_template_hints") or [])[:2]:
        if not isinstance(row, dict):
            continue
        trace_hints.append(
            {
                "endpoint_url": str(row.get("endpoint_url") or ""),
                "method": str(row.get("method") or ""),
                "body_kind": str(row.get("body_kind") or ""),
                "candidate_mutations": [
                    {
                        "option_key": str(m.get("option_key") or ""),
                        "path": _truncate_text(m.get("path") or "", 90),
                    }
                    for m in list(row.get("candidate_mutations") or [])
                    if isinstance(m, dict)
                ][:4],
            }
        )

    compact: dict[str, Any] = {
        "stage": stage_name,
        "proposal": {
            "site_name": str(proposal.get("site_name") or ""),
            "product_url": str(proposal.get("product_url") or ""),
            "requires_session": bool(proposal.get("requires_session")),
            "blockers": [str(item) for item in list(proposal.get("blockers") or [])[:6]],
            "top_endpoints": top_endpoints,
            "trace_template_hints": trace_hints,
        },
        "extractionRecipe": {
            "replayStrategy": {
                "mode": str(dict(extraction_recipe.get("replayStrategy") or {}).get("mode") or ""),
                "selectedEndpoint": dict(dict(extraction_recipe.get("replayStrategy") or {}).get("selectedEndpoint") or {}),
            },
            "requestSynthesisStrategy": {
                "templateDriven": bool(dict(extraction_recipe.get("requestSynthesisStrategy") or {}).get("templateDriven")),
                "candidateMutations": [
                    {
                        "option_key": str(m.get("option_key") or ""),
                        "path": _truncate_text(m.get("path") or "", 90),
                    }
                    for m in list(dict(extraction_recipe.get("requestSynthesisStrategy") or {}).get("candidateMutations") or [])
                    if isinstance(m, dict)
                ][:4],
            },
            "quantityStrategy": dict(extraction_recipe.get("quantityStrategy") or {}),
            "validationStrategy": dict(extraction_recipe.get("validationStrategy") or {}),
            "runtimeSafetyPolicy": dict(extraction_recipe.get("runtimeSafetyPolicy") or {}),
        },
        "patchPlan": {
            "selectedEndpoint": str(patch_plan.get("selectedEndpoint") or ""),
            "rewriteEvidenceGate": dict(patch_plan.get("rewriteEvidenceGate") or {}),
            "proposedFilePatches": [
                {
                    "path": str(row.get("path") or ""),
                    "operation": str(row.get("operation") or ""),
                    "changeScope": str(row.get("changeScope") or ""),
                    "intent": _truncate_text(row.get("intent") or "", 120),
                    "anchor": _truncate_text(row.get("anchor") or "", 120),
                    "contentPreview": _truncate_text(row.get("content") or "", 260),
                }
                for row in list(patch_plan.get("proposedFilePatches") or [])
                if isinstance(row, dict)
            ][:4],
        },
    }

    if stage_name == "recipe":
        critique = dict(context_payload.get("recipeCritique") or {})
        compact["recipeCritique"] = {
            "overallVerdict": str(critique.get("overallVerdict") or ""),
            "blockerCount": int(critique.get("blockerCount") or 0),
            "warnCount": int(critique.get("warnCount") or 0),
            "critics": [
                {
                    "criticId": str(c.get("criticId") or ""),
                    "domain": str(c.get("domain") or ""),
                    "verdict": str(c.get("verdict") or ""),
                    "score": int(c.get("score") or 0),
                    "findings": _first_findings(c, max_items=1),
                }
                for c in list(critique.get("critics") or [])
                if isinstance(c, dict)
            ][:6],
        }
    elif stage_name == "patch":
        critique = dict(context_payload.get("patchCritique") or {})
        compact["patchCritique"] = {
            "overallVerdict": str(critique.get("overallVerdict") or ""),
            "blockerCount": int(critique.get("blockerCount") or 0),
            "warnCount": int(critique.get("warnCount") or 0),
            "critics": [
                {
                    "criticId": str(c.get("criticId") or ""),
                    "domain": str(c.get("domain") or ""),
                    "verdict": str(c.get("verdict") or ""),
                    "score": int(c.get("score") or 0),
                    "findings": _first_findings(c, max_items=1),
                }
                for c in list(critique.get("critics") or [])
                if isinstance(c, dict)
            ][:6],
        }

    while len(json.dumps(compact, ensure_ascii=True)) > max(1000, int(max_chars or 0)):
        patches = list(dict(compact.get("patchPlan") or {}).get("proposedFilePatches") or [])
        if patches:
            compact["patchPlan"]["proposedFilePatches"] = patches[:-1]
            continue
        endpoints = list(dict(compact.get("proposal") or {}).get("top_endpoints") or [])
        if endpoints:
            compact["proposal"]["top_endpoints"] = endpoints[:-1]
            continue
        hints = list(dict(compact.get("proposal") or {}).get("trace_template_hints") or [])
        if hints:
            compact["proposal"]["trace_template_hints"] = hints[:-1]
            continue
        break

    return compact


def _is_quota_exhaustion_error(error_text: str) -> bool:
    text = str(error_text or "").lower()
    if not text:
        return False
    markers = [
        "quota exceeded",
        "resource_exhausted",
        "generaterequestsperdayperprojectpermodel-freetier",
    ]
    return any(marker in text for marker in markers)


def _run_llm_critic_pass(
    *,
    stage_name: str,
    critic_specs: list[dict[str, Any]],
    context_payload: dict[str, Any],
    llm_settings: dict[str, Any],
    max_critics: int,
    inter_request_delay_seconds: float,
    retry_attempts: int,
    retry_wait_seconds: float,
    retry_backoff_multiplier: float,
    context_mode: str,
    context_max_chars: int,
    verbose_logs: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    limited_specs = [row for row in list(critic_specs or []) if isinstance(row, dict)][: max(1, int(max_critics))]
    base_delay = max(float(inter_request_delay_seconds or 0.0), 0.0)
    max_retry_attempts = max(int(retry_attempts or 0), 0)
    retry_wait = max(float(retry_wait_seconds or 0.0), 0.0)
    retry_backoff = max(float(retry_backoff_multiplier or 1.0), 1.0)
    quota_exhausted = False

    _emit_llm_critic_log(
        enabled=verbose_logs,
        stage_name=stage_name,
        message=(
            f"starting {len(limited_specs)} critic calls "
            f"(delay={base_delay:.2f}s, retry_attempts={max_retry_attempts}, retry_wait={retry_wait:.2f}s, backoff={retry_backoff:.2f})"
        ),
    )

    for index, spec in enumerate(limited_specs):
        critic_id = str(spec.get("criticId") or f"critic_{index + 1}")
        if quota_exhausted:
            skipped_result = _coerce_llm_critic_result(
                critic_spec=spec,
                response_obj=None,
                error="quota_exhausted_skip_remaining_critics",
            )
            results.append(skipped_result)
            _emit_llm_critic_log(
                enabled=verbose_logs,
                stage_name=stage_name,
                message=(
                    f"{index + 1}/{len(limited_specs)} {critic_id} -> "
                    "status=skipped (quota exhausted earlier in this stage)"
                ),
            )
            continue

        if index > 0 and base_delay > 0:
            _emit_llm_critic_log(
                enabled=verbose_logs,
                stage_name=stage_name,
                message=(
                    f"waiting {base_delay:.2f}s before next request "
                    f"({index + 1}/{len(limited_specs)}: {critic_id})"
                ),
            )
            time.sleep(base_delay)

        system_prompt = str(spec.get("systemPrompt") or "").strip()
        user_prompt_template = str(spec.get("userPromptTemplate") or "").strip()
        if str(context_mode or "full").strip().lower() == "compact":
            effective_context_payload = _compact_critic_context(
                context_payload=context_payload,
                stage_name=stage_name,
                max_chars=max(1000, int(context_max_chars or 0)),
            )
        else:
            effective_context_payload = dict(context_payload)
        context_json = json.dumps(effective_context_payload, ensure_ascii=True)
        user_prompt = user_prompt_template.replace("{{CONTEXT_JSON}}", context_json)

        if not system_prompt or not user_prompt:
            result = _coerce_llm_critic_result(
                critic_spec=spec,
                response_obj=None,
                error="missing_prompt_template",
            )
            results.append(result)
            _emit_llm_critic_log(
                enabled=verbose_logs,
                stage_name=stage_name,
                message=f"{index + 1}/{len(limited_specs)} {critic_id} -> status=error (missing prompt template)",
            )
            continue

        total_attempts = max_retry_attempts + 1
        final_result: dict[str, Any] | None = None
        last_error_text = ""

        for attempt_index in range(total_attempts):
            _emit_llm_critic_log(
                enabled=verbose_logs,
                stage_name=stage_name,
                message=(
                    f"dispatching {index + 1}/{len(limited_specs)} {critic_id} "
                    f"attempt {attempt_index + 1}/{total_attempts}"
                ),
            )
            try:
                response_obj = _call_openai_compatible_json(
                    base_url=str(llm_settings.get("base_url") or ""),
                    api_key=str(llm_settings.get("api_key") or ""),
                    model=str(llm_settings.get("model") or ""),
                    timeout_seconds=float(llm_settings.get("timeout_seconds") or 60.0),
                    system_prompt=system_prompt,
                    user_prompt=(
                        "You are running a single specialized critic. "
                        "Return only JSON matching the required schema.\n\n"
                        + user_prompt
                    ),
                )
                final_result = _coerce_llm_critic_result(
                    critic_spec=spec,
                    response_obj=response_obj if isinstance(response_obj, dict) else None,
                )
                if (
                    str(final_result.get("status") or "") == "error"
                    and str(final_result.get("errorCategory") or "") == "output_schema_quality"
                ):
                    _emit_llm_critic_log(
                        enabled=verbose_logs,
                        stage_name=stage_name,
                        message=(
                            f"{index + 1}/{len(limited_specs)} {critic_id} produced non-compliant output; "
                            "retrying once with stricter schema reminder"
                        ),
                    )
                    retry_response_obj = _call_openai_compatible_json(
                        base_url=str(llm_settings.get("base_url") or ""),
                        api_key=str(llm_settings.get("api_key") or ""),
                        model=str(llm_settings.get("model") or ""),
                        timeout_seconds=float(llm_settings.get("timeout_seconds") or 60.0),
                        system_prompt=system_prompt,
                        user_prompt=(
                            "You are running a single specialized critic. "
                            "Return only JSON matching the required schema. "
                            "For verdict=warn or verdict=block, summary must be non-empty and findings must include "
                            "at least one item with non-empty title and detail.\n\n"
                            + user_prompt
                        ),
                    )
                    final_result = _coerce_llm_critic_result(
                        critic_spec=spec,
                        response_obj=retry_response_obj if isinstance(retry_response_obj, dict) else None,
                    )
                _emit_llm_critic_log(
                    enabled=verbose_logs,
                    stage_name=stage_name,
                    message=(
                        f"{index + 1}/{len(limited_specs)} {critic_id} -> "
                        f"status={final_result.get('status')} verdict={final_result.get('verdict')}"
                    ),
                )
                break
            except Exception as exc:  # pragma: no cover - provider/network dependent
                last_error_text = f"{type(exc).__name__}:{exc}"
                if _is_quota_exhaustion_error(last_error_text):
                    final_result = _coerce_llm_critic_result(
                        critic_spec=spec,
                        response_obj=None,
                        error=last_error_text,
                    )
                    quota_exhausted = True
                    _emit_llm_critic_log(
                        enabled=verbose_logs,
                        stage_name=stage_name,
                        message=(
                            f"{index + 1}/{len(limited_specs)} {critic_id} -> "
                            "status=error (quota exhaustion detected; skipping remaining critics)"
                        ),
                    )
                    break
                is_last_attempt = attempt_index >= (total_attempts - 1)
                if is_last_attempt:
                    final_result = _coerce_llm_critic_result(
                        critic_spec=spec,
                        response_obj=None,
                        error=last_error_text,
                    )
                    _emit_llm_critic_log(
                        enabled=verbose_logs,
                        stage_name=stage_name,
                        message=(
                            f"{index + 1}/{len(limited_specs)} {critic_id} -> "
                            f"status=error (final failure: {last_error_text[:180]})"
                        ),
                    )
                    break

                suggested_retry = _extract_retry_delay_seconds(last_error_text)
                computed_wait = retry_wait * (retry_backoff ** attempt_index)
                retry_sleep = max(computed_wait, float(suggested_retry or 0.0))
                _emit_llm_critic_log(
                    enabled=verbose_logs,
                    stage_name=stage_name,
                    message=(
                        f"{index + 1}/{len(limited_specs)} {critic_id} transient error; "
                        f"retrying in {retry_sleep:.2f}s ({last_error_text[:180]})"
                    ),
                )
                if retry_sleep > 0:
                    time.sleep(retry_sleep)

        if final_result is None:
            final_result = _coerce_llm_critic_result(
                critic_spec=spec,
                response_obj=None,
                error=last_error_text or "unknown_critic_error",
            )
        results.append(final_result)

    _emit_llm_critic_log(
        enabled=verbose_logs,
        stage_name=stage_name,
        message=f"completed {len(results)} critic calls",
    )

    return results


def _summarize_llm_critic_pass(results: list[dict[str, Any]]) -> dict[str, Any]:
    critics = [row for row in list(results or []) if isinstance(row, dict)]
    block_count = sum(1 for row in critics if str(row.get("verdict") or "") == "block")
    warn_count = sum(1 for row in critics if str(row.get("verdict") or "") == "warn")
    error_count = sum(1 for row in critics if str(row.get("status") or "") == "error")

    if block_count > 0:
        overall = "block"
    elif warn_count > 0:
        overall = "warn"
    else:
        overall = "pass"

    return {
        "overallVerdict": overall,
        "blockerCount": block_count,
        "warnCount": warn_count,
        "errorCount": error_count,
        "critics": critics,
    }


def _build_llm_critics_bundle(
    *,
    proposal: dict[str, Any],
    patch_plan: dict[str, Any],
    extraction_recipe: dict[str, Any],
    recipe_critique: dict[str, Any],
    patch_critique: dict[str, Any],
    llm_settings: dict[str, Any],
    llm_critics_enable: bool,
    llm_critics_max: int,
    llm_critics_delay_seconds: float,
    llm_critics_retry_attempts: int,
    llm_critics_retry_wait_seconds: float,
    llm_critics_retry_backoff_multiplier: float,
    llm_critics_context_mode: str,
    llm_critics_context_max_chars: int,
    llm_critics_verbose_logs: bool,
) -> dict[str, Any]:
    registry = _build_critic_prompt_registry()
    execution_controls = {
        "delaySeconds": max(float(llm_critics_delay_seconds or 0.0), 0.0),
        "retryAttempts": max(int(llm_critics_retry_attempts or 0), 0),
        "retryWaitSeconds": max(float(llm_critics_retry_wait_seconds or 0.0), 0.0),
        "retryBackoffMultiplier": max(float(llm_critics_retry_backoff_multiplier or 1.0), 1.0),
        "contextMode": (
            "compact"
            if str(llm_critics_context_mode or "full").strip().lower() == "compact"
            else "full"
        ),
        "contextMaxChars": max(int(llm_critics_context_max_chars or 8000), 1000),
        "verboseLogs": bool(llm_critics_verbose_logs),
    }

    if not bool(llm_critics_enable):
        return {
            "enabled": False,
            "status": "disabled",
            "maxCritics": max(1, int(llm_critics_max)),
            "recipe": _summarize_llm_critic_pass([]),
            "patch": _summarize_llm_critic_pass([]),
            "execution": execution_controls,
            "registry": registry,
        }

    if not bool(llm_settings.get("enabled")) or not bool(llm_settings.get("api_key_present")):
        return {
            "enabled": True,
            "status": "skipped_llm_disabled_or_missing_key",
            "maxCritics": max(1, int(llm_critics_max)),
            "recipe": _summarize_llm_critic_pass([]),
            "patch": _summarize_llm_critic_pass([]),
            "execution": execution_controls,
            "registry": registry,
        }

    recipe_context = {
        "proposal": proposal,
        "extractionRecipe": extraction_recipe,
        "patchPlan": patch_plan,
        "recipeCritique": recipe_critique,
    }
    patch_context = {
        "proposal": proposal,
        "extractionRecipe": extraction_recipe,
        "patchPlan": patch_plan,
        "patchCritique": patch_critique,
    }

    recipe_results = _run_llm_critic_pass(
        stage_name="recipe",
        critic_specs=list(registry.get("recipe") or []),
        context_payload=recipe_context,
        llm_settings=llm_settings,
        max_critics=max(1, int(llm_critics_max)),
        inter_request_delay_seconds=execution_controls["delaySeconds"],
        retry_attempts=execution_controls["retryAttempts"],
        retry_wait_seconds=execution_controls["retryWaitSeconds"],
        retry_backoff_multiplier=execution_controls["retryBackoffMultiplier"],
        context_mode=execution_controls["contextMode"],
        context_max_chars=execution_controls["contextMaxChars"],
        verbose_logs=execution_controls["verboseLogs"],
    )
    patch_results = _run_llm_critic_pass(
        stage_name="patch",
        critic_specs=list(registry.get("patch") or []),
        context_payload=patch_context,
        llm_settings=llm_settings,
        max_critics=max(1, int(llm_critics_max)),
        inter_request_delay_seconds=execution_controls["delaySeconds"],
        retry_attempts=execution_controls["retryAttempts"],
        retry_wait_seconds=execution_controls["retryWaitSeconds"],
        retry_backoff_multiplier=execution_controls["retryBackoffMultiplier"],
        context_mode=execution_controls["contextMode"],
        context_max_chars=execution_controls["contextMaxChars"],
        verbose_logs=execution_controls["verboseLogs"],
    )

    recipe_summary = _summarize_llm_critic_pass(recipe_results)
    patch_summary = _summarize_llm_critic_pass(patch_results)
    status = "ok"
    if int(recipe_summary.get("errorCount") or 0) > 0 or int(patch_summary.get("errorCount") or 0) > 0:
        status = "partial_error"

    return {
        "enabled": True,
        "status": status,
        "maxCritics": max(1, int(llm_critics_max)),
        "recipe": recipe_summary,
        "patch": patch_summary,
        "execution": execution_controls,
        "registry": registry,
    }


def _build_recipe_critique(
    recipe: dict[str, Any],
) -> dict[str, Any]:
    critics: list[dict[str, Any]] = []

    evidence = dict(recipe.get("evidenceBundleRefs") or {})
    validation = dict(recipe.get("validationStrategy") or {})
    runtime_policy = dict(recipe.get("runtimeSafetyPolicy") or {})
    replay = dict(recipe.get("replayStrategy") or {})
    quantity = dict(recipe.get("quantityStrategy") or {})
    request_synthesis = dict(recipe.get("requestSynthesisStrategy") or {})

    findings: list[dict[str, Any]] = []
    if int(evidence.get("topEndpointCount", 0) or 0) <= 0:
        findings.append(
            {
                "severity": "blocker",
                "title": "No ranked replay endpoints",
                "detail": "Evidence bundle has no ranked endpoints for deterministic replay.",
            }
        )
    if int(evidence.get("traceTemplateHintCount", 0) or 0) <= 0:
        findings.append(
            {
                "severity": "warn",
                "title": "Missing trace template hints",
                "detail": "Template-driven request synthesis cannot be justified without trace template hints.",
            }
        )

    has_evidence_blocker = any(
        str(row.get("severity") or "") == "blocker"
        for row in findings
        if isinstance(row, dict)
    )
    has_evidence_warn = any(
        str(row.get("severity") or "") == "warn"
        for row in findings
        if isinstance(row, dict)
    )
    if has_evidence_blocker:
        evidence_verdict = "block"
        evidence_score = 45
    elif has_evidence_warn:
        evidence_verdict = "warn"
        evidence_score = 72
    else:
        evidence_verdict = "pass"
        evidence_score = 95

    critics.append(
        _make_critic_result(
            critic_id="recipe_evidence_sufficiency",
            domain="evidence",
            verdict=evidence_verdict,
            score=evidence_score,
            summary="Checks whether the extraction recipe has enough concrete replay evidence.",
            findings=findings,
        )
    )

    findings = []
    required_validation_flags = [
        "requiresIntendedVsAcceptedComparison",
        "requiresAcceptedConfigProofForValidated",
        "quantityValidationIsMandatory",
        "forbidInterpolationAssumptions",
    ]
    missing_validation_flags = [
        key for key in required_validation_flags if not bool(validation.get(key))
    ]
    if missing_validation_flags:
        findings.append(
            {
                "severity": "blocker",
                "title": "Validation integrity requirements are incomplete",
                "detail": "Missing required validation flags: " + ", ".join(sorted(missing_validation_flags)),
            }
        )
    critics.append(
        _make_critic_result(
            critic_id="recipe_validation_integrity",
            domain="validation",
            verdict="block" if findings else "pass",
            score=40 if findings else 96,
            summary="Enforces intended-vs-accepted comparison and quantity proof before trusting price.",
            findings=findings,
        )
    )

    findings = []
    if str(replay.get("mode") or "") != "request_only_http_or_adapter":
        findings.append(
            {
                "severity": "blocker",
                "title": "Replay mode is not request-only deterministic",
                "detail": "Recipe replay mode must remain request_only_http_or_adapter.",
            }
        )
    if bool(replay.get("allowDomFallback")) or bool(replay.get("allowHeuristicFallback")):
        findings.append(
            {
                "severity": "blocker",
                "title": "Fallbacks enabled in deterministic recipe",
                "detail": "DOM/heuristic fallbacks must be disabled in request-only validation workflows.",
            }
        )
    critics.append(
        _make_critic_result(
            critic_id="recipe_determinism_request_only",
            domain="determinism",
            verdict="block" if findings else "pass",
            score=35 if findings else 97,
            summary="Guards deterministic request-only replay semantics.",
            findings=findings,
        )
    )

    findings = []
    retry_policy = dict(runtime_policy.get("retryPolicy") or {})
    stop_conditions = dict(runtime_policy.get("stopConditions") or {})
    promotion_guards = dict(runtime_policy.get("promotionGuards") or {})
    if not bool(runtime_policy.get("requiresApprovedEgressProxy")):
        findings.append(
            {
                "severity": "blocker",
                "title": "Missing approved egress policy",
                "detail": "Live extraction safety requires approved proxy/egress enforcement.",
            }
        )
    if not all(
        bool(retry_policy.get(key))
        for key in ["boundedRetries", "exponentialBackoff", "perHostPacing", "tripOnRetryStorm"]
    ):
        findings.append(
            {
                "severity": "blocker",
                "title": "Retry storm protections incomplete",
                "detail": "Retry policy must include bounded retries, backoff, pacing, and retry-storm guard.",
            }
        )
    if not all(bool(stop_conditions.get(key)) for key in ["on403Spike", "on429Spike", "onSustained5xxSpike"]):
        findings.append(
            {
                "severity": "blocker",
                "title": "Stop conditions incomplete",
                "detail": "Runtime policy must halt on 403/429 spikes and sustained 5xx spikes.",
            }
        )
    if not bool(promotion_guards.get("forbidSafetyGateBypass")):
        findings.append(
            {
                "severity": "blocker",
                "title": "Safety-gate bypass protection missing",
                "detail": "Promotion policy must reject code that bypasses runtime safety gates.",
            }
        )
    critics.append(
        _make_critic_result(
            critic_id="recipe_runtime_safety_compliance",
            domain="runtime_safety",
            verdict="block" if findings else "pass",
            score=30 if findings else 98,
            summary="Checks egress/proxy, retry-storm prevention, and stop-condition safety rules.",
            findings=findings,
        )
    )

    findings = []
    quantity_paths = [str(item) for item in list(quantity.get("candidatePaths") or []) if str(item)]
    if not bool(quantity.get("required")):
        findings.append(
            {
                "severity": "blocker",
                "title": "Quantity strategy is optional",
                "detail": "Quantity strategy must be required for configurator pricing workflows.",
            }
        )
    if not quantity_paths and not bool(request_synthesis.get("insufficientEvidence")):
        findings.append(
            {
                "severity": "warn",
                "title": "No quantity mutation paths found",
                "detail": "Recipe should map at least one concrete quantity mutation path when evidence is sufficient.",
            }
        )
    critics.append(
        _make_critic_result(
            critic_id="recipe_quantity_strategy_integrity",
            domain="quantity",
            verdict="warn" if findings else "pass",
            score=70 if findings else 95,
            summary="Ensures quantity handling is explicit and validated semantically.",
            findings=findings,
        )
    )

    findings = []
    if bool(request_synthesis.get("insufficientEvidence")):
        reasons = [
            str(item)
            for item in list(request_synthesis.get("insufficientEvidenceReasons") or [])
            if str(item)
        ]
        findings.append(
            {
                "severity": "warn",
                "title": "Recipe indicates insufficient rewrite evidence",
                "detail": "Need additional capture evidence before promoting adapter rewrite: " + ", ".join(reasons),
            }
        )
        verdict = "warn"
        score = 65
    else:
        verdict = "pass"
        score = 94
    critics.append(
        _make_critic_result(
            critic_id="recipe_extraction_correctness",
            domain="extraction",
            verdict=verdict,
            score=score,
            summary="Checks that template-driven request synthesis has sufficient evidence.",
            findings=findings,
        )
    )

    blocker_count = sum(1 for c in critics if str(c.get("verdict") or "") == "block")
    warn_count = sum(1 for c in critics if str(c.get("verdict") or "") == "warn")
    if blocker_count > 0:
        overall = "block"
    elif warn_count > 0:
        overall = "warn"
    else:
        overall = "pass"

    return {
        "kind": "recipe_critique_v1",
        "overallVerdict": overall,
        "blockerCount": blocker_count,
        "warnCount": warn_count,
        "critics": critics,
    }


def _build_patch_critique(
    patch_plan: dict[str, Any],
    recipe: dict[str, Any],
) -> dict[str, Any]:
    patches = [
        row
        for row in list(patch_plan.get("proposedFilePatches") or [])
        if isinstance(row, dict)
    ]
    change_scopes = sorted(
        {
            str(row.get("changeScope") or _infer_patch_change_scope(str(row.get("path") or "")))
            for row in patches
            if str(row.get("path") or "")
        }
    )

    critics: list[dict[str, Any]] = []

    framework_scope = {
        "core_framework",
        "runtime_policy",
        "storage_persistence",
    }
    has_framework_scope = any(scope in framework_scope for scope in change_scopes)

    findings: list[dict[str, Any]] = []
    if not patches:
        findings.append(
            {
                "severity": "warn",
                "title": "No proposed code patch records",
                "detail": "Patch critique has no file-level changes to review.",
            }
        )
    if has_framework_scope:
        findings.append(
            {
                "severity": "warn",
                "title": "Framework-scope changes detected",
                "detail": "Core/runtime/storage changes require elevated review and regression depth.",
            }
        )
    critics.append(
        _make_critic_result(
            critic_id="patch_scope_router",
            domain="scope",
            verdict="warn" if findings else "pass",
            score=70 if findings else 96,
            summary="Routes critique policy based on adapter-only vs framework-level patch scope.",
            findings=findings,
        )
    )

    findings = []
    safety_bypass_markers = [
        "request_only = false",
        "request_only=False",
        '"allowHeuristicFallback": true',
        '"allowDomFallback": true',
        "proxy=false",
        "circuit breaker disabled",
        "max_retries = 999",
    ]
    for row in patches:
        content = str(row.get("content") or "")
        lowered = content.lower()
        matched = [marker for marker in safety_bypass_markers if marker.lower() in lowered]
        if matched:
            findings.append(
                {
                    "severity": "blocker",
                    "title": "Patch appears to bypass runtime safety policy",
                    "detail": f"Detected risky markers in {row.get('path')}: {', '.join(matched)}",
                }
            )
    critics.append(
        _make_critic_result(
            critic_id="patch_runtime_safety_guard",
            domain="runtime_safety",
            verdict="block" if findings else "pass",
            score=25 if findings else 97,
            summary="Blocks patches that weaken request-only or runtime safety controls.",
            findings=findings,
        )
    )

    findings = []
    rewrite_gate = dict(patch_plan.get("rewriteEvidenceGate") or {})
    if not bool(rewrite_gate.get("sufficient")):
        adapter_patches = [
            row for row in patches if str(row.get("path") or "") == "src/price_extractor/site_adapters.py"
        ]
        if adapter_patches:
            findings.append(
                {
                    "severity": "blocker",
                    "title": "Adapter patch emitted without rewrite evidence",
                    "detail": "Site adapter changes must be withheld when rewrite evidence gate is insufficient.",
                }
            )
    critics.append(
        _make_critic_result(
            critic_id="patch_evidence_alignment",
            domain="evidence",
            verdict="block" if findings else "pass",
            score=30 if findings else 96,
            summary="Checks patch plan alignment with rewrite evidence gate.",
            findings=findings,
        )
    )

    findings = []
    validation = dict(recipe.get("validationStrategy") or {})
    if not bool(validation.get("requiresAcceptedConfigProofForValidated")):
        findings.append(
            {
                "severity": "blocker",
                "title": "Recipe validation policy missing accepted-config proof",
                "detail": "Patch review cannot pass when recipe allows price trust without accepted config proof.",
            }
        )
    critics.append(
        _make_critic_result(
            critic_id="patch_validation_integrity",
            domain="validation",
            verdict="block" if findings else "pass",
            score=35 if findings else 97,
            summary="Ensures implementation remains bound to accepted-config validation policy.",
            findings=findings,
        )
    )

    blocker_count = sum(1 for c in critics if str(c.get("verdict") or "") == "block")
    warn_count = sum(1 for c in critics if str(c.get("verdict") or "") == "warn")
    if blocker_count > 0:
        overall = "block"
    elif warn_count > 0:
        overall = "warn"
    else:
        overall = "pass"

    return {
        "kind": "patch_critique_v1",
        "overallVerdict": overall,
        "blockerCount": blocker_count,
        "warnCount": warn_count,
        "changeScopes": change_scopes,
        "critics": critics,
    }


def _validate_extraction_recipe_schema(recipe: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    required_keys = {
        "kind",
        "workflow",
        "site",
        "productUrl",
        "evidenceBundleRefs",
        "sessionStrategy",
        "replayStrategy",
        "requestSynthesisStrategy",
        "quantityStrategy",
        "validationStrategy",
        "priceExtractionStrategy",
        "runtimeSafetyPolicy",
        "persistencePolicy",
    }
    for key in sorted(required_keys):
        if key not in recipe:
            issues.append(f"missing_recipe_key:{key}")

    if str(recipe.get("kind") or "") != "extraction_recipe_v1":
        issues.append("invalid_recipe_kind")
    if not isinstance(recipe.get("workflow"), list):
        issues.append("workflow_must_be_list")

    replay = dict(recipe.get("replayStrategy") or {})
    if str(replay.get("mode") or "") != "request_only_http_or_adapter":
        issues.append("invalid_replay_mode")

    validation = dict(recipe.get("validationStrategy") or {})
    required_validation_flags = [
        "requiresIntendedVsAcceptedComparison",
        "requiresAcceptedConfigProofForValidated",
        "quantityValidationIsMandatory",
        "forbidInterpolationAssumptions",
    ]
    for key in required_validation_flags:
        if not bool(validation.get(key)):
            issues.append(f"validation_flag_missing_or_false:{key}")

    runtime_policy = dict(recipe.get("runtimeSafetyPolicy") or {})
    if not bool(runtime_policy.get("requiresApprovedEgressProxy")):
        issues.append("runtime_policy_requiresApprovedEgressProxy_missing")
    retry = dict(runtime_policy.get("retryPolicy") or {})
    for key in ["boundedRetries", "exponentialBackoff", "perHostPacing", "tripOnRetryStorm"]:
        if not bool(retry.get(key)):
            issues.append(f"runtime_retry_flag_missing_or_false:{key}")
    stop = dict(runtime_policy.get("stopConditions") or {})
    for key in ["on403Spike", "on429Spike", "onSustained5xxSpike"]:
        if not bool(stop.get(key)):
            issues.append(f"runtime_stop_flag_missing_or_false:{key}")
    return issues


def _validate_critique_schema(name: str, critique: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not isinstance(critique, dict):
        return [f"{name}:not_object"]

    if str(critique.get("kind") or "") not in {"recipe_critique_v1", "patch_critique_v1"}:
        issues.append(f"{name}:invalid_kind")
    overall = str(critique.get("overallVerdict") or "")
    if overall not in {"pass", "warn", "block"}:
        issues.append(f"{name}:invalid_overall_verdict")
    critics = critique.get("critics")
    if not isinstance(critics, list):
        issues.append(f"{name}:critics_not_list")
        return issues

    for index, row in enumerate(critics):
        if not isinstance(row, dict):
            issues.append(f"{name}:critic_{index}_not_object")
            continue
        if str(row.get("criticId") or "").strip() == "":
            issues.append(f"{name}:critic_{index}_missing_id")
        if str(row.get("domain") or "").strip() == "":
            issues.append(f"{name}:critic_{index}_missing_domain")
        if str(row.get("verdict") or "") not in {"pass", "warn", "block"}:
            issues.append(f"{name}:critic_{index}_invalid_verdict")
        findings = row.get("findings")
        if not isinstance(findings, list):
            issues.append(f"{name}:critic_{index}_findings_not_list")
            continue
        for finding_index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                issues.append(f"{name}:critic_{index}_finding_{finding_index}_not_object")
                continue
            if str(finding.get("severity") or "") not in {"blocker", "warn", "info"}:
                issues.append(f"{name}:critic_{index}_finding_{finding_index}_invalid_severity")
    return issues


def _validate_patch_plan_schema(patch_plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if str(patch_plan.get("kind") or "") != "review_patch_plan_v1":
        issues.append("invalid_patch_plan_kind")
    if str(patch_plan.get("patchSchema") or "") != "v2":
        issues.append("invalid_patch_schema")
    if not bool(patch_plan.get("reviewRequired")):
        issues.append("review_required_false")
    if bool(patch_plan.get("autoApply")):
        issues.append("auto_apply_must_be_false")

    patches = patch_plan.get("proposedFilePatches")
    if not isinstance(patches, list):
        issues.append("proposed_file_patches_not_list")
        return issues

    for index, row in enumerate(patches):
        if not isinstance(row, dict):
            issues.append(f"patch_{index}_not_object")
            continue
        if not _is_patch_target_allowed(str(row.get("path") or "")):
            issues.append(f"patch_{index}_path_not_allowed")
        if str(row.get("operation") or "") not in {"add", "update", "insert", "delete"}:
            issues.append(f"patch_{index}_invalid_operation")
        if str(row.get("changeScope") or "") not in {
            "adapter_only",
            "adapter_validation",
            "core_framework",
            "unknown",
        }:
            issues.append(f"patch_{index}_invalid_change_scope")
    return issues


def _build_default_runtime_commands() -> list[dict[str, Any]]:
    return [
        {
            "id": "unit_tests_onboarding",
            "command": "python -m unittest -q tests.test_onboarding tests.test_langgraph_onboarding",
            "required": True,
            "status": "not_run",
        },
        {
            "id": "unit_tests_regressions",
            "command": "python -m unittest -q tests.test_regressions",
            "required": True,
            "status": "not_run",
        },
    ]


def _run_runtime_gate_command(
    *,
    command: str,
    timeout_seconds: float,
    max_output_chars: int,
) -> dict[str, Any]:
    command_text = str(command or "").strip()
    if not command_text:
        return {
            "status": "fail",
            "exitCode": None,
            "stdout": "",
            "stderr": "missing command",
        }

    try:
        completed = subprocess.run(
            command_text,
            shell=True,
            capture_output=True,
            text=True,
            timeout=max(float(timeout_seconds or 0.0), 1.0),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "fail",
            "exitCode": None,
            "stdout": str(exc.stdout or "")[: max(200, int(max_output_chars))],
            "stderr": (str(exc.stderr or "") + "\n<timeout>")[: max(200, int(max_output_chars))],
        }
    except Exception as exc:
        return {
            "status": "fail",
            "exitCode": None,
            "stdout": "",
            "stderr": str(exc),
        }

    stdout_text = str(completed.stdout or "")
    stderr_text = str(completed.stderr or "")
    max_chars = max(200, int(max_output_chars))
    return {
        "status": "pass" if int(completed.returncode or 0) == 0 else "fail",
        "exitCode": int(completed.returncode or 0),
        "stdout": stdout_text[:max_chars],
        "stderr": stderr_text[:max_chars],
    }


def _build_deterministic_gate(
    *,
    patch_plan: dict[str, Any],
    extraction_recipe: dict[str, Any],
    recipe_critique: dict[str, Any],
    patch_critique: dict[str, Any],
    run_runtime_checks: bool = False,
    runtime_check_timeout_seconds: float = 180.0,
    runtime_max_output_chars: int = 2000,
    runtime_commands_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    recipe_schema_issues = _validate_extraction_recipe_schema(extraction_recipe)
    checks.append(
        {
            "id": "schema_extraction_recipe",
            "status": "pass" if not recipe_schema_issues else "fail",
            "issues": recipe_schema_issues,
            "reason": "Extraction recipe must satisfy required strategy contract.",
        }
    )

    recipe_critique_issues = _validate_critique_schema("recipe_critique", recipe_critique)
    checks.append(
        {
            "id": "schema_recipe_critique",
            "status": "pass" if not recipe_critique_issues else "fail",
            "issues": recipe_critique_issues,
            "reason": "Recipe critique must satisfy structured critic schema.",
        }
    )

    patch_critique_issues = _validate_critique_schema("patch_critique", patch_critique)
    checks.append(
        {
            "id": "schema_patch_critique",
            "status": "pass" if not patch_critique_issues else "fail",
            "issues": patch_critique_issues,
            "reason": "Patch critique must satisfy structured critic schema.",
        }
    )

    patch_plan_issues = _validate_patch_plan_schema(patch_plan)
    checks.append(
        {
            "id": "schema_patch_plan",
            "status": "pass" if not patch_plan_issues else "fail",
            "issues": patch_plan_issues,
            "reason": "Patch plan must satisfy review-only and path-safety contract.",
        }
    )

    critique_blocking = (
        str(recipe_critique.get("overallVerdict") or "") == "block"
        or str(patch_critique.get("overallVerdict") or "") == "block"
    )
    checks.append(
        {
            "id": "critique_blocking_status",
            "status": "fail" if critique_blocking else "pass",
            "issues": [],
            "reason": "Blocker verdicts from recipe/patch critiques cannot be promoted.",
        }
    )

    runtime_commands = [
        dict(row)
        for row in list(runtime_commands_override if runtime_commands_override is not None else _build_default_runtime_commands())
        if isinstance(row, dict)
    ]
    for row in runtime_commands:
        if "status" not in row:
            row["status"] = "not_run"

    if bool(run_runtime_checks):
        for row in runtime_commands:
            command_text = str(row.get("command") or "").strip()
            command_result = _run_runtime_gate_command(
                command=command_text,
                timeout_seconds=runtime_check_timeout_seconds,
                max_output_chars=runtime_max_output_chars,
            )
            row["status"] = str(command_result.get("status") or "fail")
            row["exitCode"] = command_result.get("exitCode")
            row["stdout"] = str(command_result.get("stdout") or "")
            row["stderr"] = str(command_result.get("stderr") or "")

    runtime_failed = any(
        bool(row.get("required", True)) and str(row.get("status") or "") == "fail"
        for row in runtime_commands
        if isinstance(row, dict)
    )
    runtime_not_run = any(
        bool(row.get("required", True)) and str(row.get("status") or "") == "not_run"
        for row in runtime_commands
        if isinstance(row, dict)
    )

    checks.append(
        {
            "id": "runtime_checks_declared",
            "status": "pass" if runtime_commands else "fail",
            "issues": [],
            "reason": "Deterministic gate must declare required runtime verification commands.",
        }
    )
    checks.append(
        {
            "id": "runtime_checks_execution",
            "status": "fail" if runtime_failed else ("warn" if runtime_not_run else "pass"),
            "issues": [],
            "reason": (
                "At least one required runtime command failed."
                if runtime_failed
                else (
                    "Required runtime commands were not executed."
                    if runtime_not_run
                    else "All required runtime commands passed."
                )
            ),
        }
    )

    failed = [check for check in checks if str(check.get("status") or "") == "fail"]
    warned = [check for check in checks if str(check.get("status") or "") == "warn"]
    if failed:
        overall = "fail"
    elif warned:
        overall = "warn"
    else:
        overall = "pass"

    if overall == "fail":
        decision_hint = "reject"
    elif overall == "warn":
        decision_hint = "needs_more_evidence"
    else:
        decision_hint = "promote"

    return {
        "kind": "deterministic_gate_v1",
        "mode": "static_checks_plus_runtime_commands",
        "overallStatus": overall,
        "decisionHint": decision_hint,
        "checks": checks,
        "runtimeCommands": runtime_commands,
        "blockers": [
            str(check.get("reason") or "") for check in failed if str(check.get("reason") or "")
        ],
    }


def _build_promotion_decision(
    *,
    patch_plan: dict[str, Any],
    recipe_critique: dict[str, Any],
    patch_critique: dict[str, Any],
    extraction_recipe: dict[str, Any],
    deterministic_gate: dict[str, Any],
    llm_critics: dict[str, Any] | None = None,
    llm_critic_error_severity: str = "warn",
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    recipe_verdict = str(recipe_critique.get("overallVerdict") or "")
    patch_verdict = str(patch_critique.get("overallVerdict") or "")
    review_required = bool(patch_plan.get("reviewRequired"))
    auto_apply = bool(patch_plan.get("autoApply"))
    rewrite_gate = dict(patch_plan.get("rewriteEvidenceGate") or {})
    runtime_policy = dict(extraction_recipe.get("runtimeSafetyPolicy") or {})
    deterministic_status = str(deterministic_gate.get("overallStatus") or "")
    llm_critic_bundle = dict(llm_critics or {})
    llm_recipe_errors = int(dict(llm_critic_bundle.get("recipe") or {}).get("errorCount") or 0)
    llm_patch_errors = int(dict(llm_critic_bundle.get("patch") or {}).get("errorCount") or 0)
    llm_total_errors = max(llm_recipe_errors, 0) + max(llm_patch_errors, 0)
    llm_error_policy = str(llm_critic_error_severity or "warn").strip().lower()
    if llm_error_policy not in {"ignore", "warn", "fail"}:
        llm_error_policy = "warn"

    checks.append(
        {
            "id": "recipe_critique_status",
            "status": "pass" if recipe_verdict != "block" else "fail",
            "reason": f"recipe_critique={recipe_verdict or 'unknown'}",
        }
    )
    checks.append(
        {
            "id": "patch_critique_status",
            "status": "pass" if patch_verdict != "block" else "fail",
            "reason": f"patch_critique={patch_verdict or 'unknown'}",
        }
    )
    checks.append(
        {
            "id": "manual_review_required",
            "status": "pass" if review_required and not auto_apply else "fail",
            "reason": "reviewRequired must be true and autoApply must stay false.",
        }
    )
    checks.append(
        {
            "id": "runtime_safety_policy_present",
            "status": "pass" if bool(runtime_policy) else "fail",
            "reason": "runtime safety policy must be explicit in extraction recipe.",
        }
    )
    checks.append(
        {
            "id": "deterministic_gate_status",
            "status": (
                "pass"
                if deterministic_status == "pass"
                else ("warn" if deterministic_status == "warn" else "fail")
            ),
            "reason": f"deterministic_gate={deterministic_status or 'unknown'}",
        }
    )
    checks.append(
        {
            "id": "rewrite_evidence_gate",
            "status": "pass" if bool(rewrite_gate.get("sufficient")) else "warn",
            "reason": "site-adapter rewrites require sufficient trace mutation evidence.",
        }
    )
    checks.append(
        {
            "id": "llm_critic_execution_quality",
            "status": (
                "pass"
                if llm_total_errors <= 0 or llm_error_policy == "ignore"
                else ("fail" if llm_error_policy == "fail" else "warn")
            ),
            "reason": (
                f"llm_critic_errors={llm_total_errors} policy={llm_error_policy}"
                if llm_total_errors > 0
                else "llm_critic_errors=0"
            ),
        }
    )

    failed = [check for check in checks if str(check.get("status") or "") == "fail"]
    warned = [check for check in checks if str(check.get("status") or "") == "warn"]

    if failed:
        decision = "reject"
    elif warned:
        decision = "needs_more_evidence"
    else:
        decision = "promote"

    return {
        "kind": "promotion_decision_v1",
        "decision": decision,
        "llmCriticErrorSeverity": llm_error_policy,
        "checks": checks,
        "blockers": [
            str(check.get("reason") or "") for check in failed if str(check.get("reason") or "")
        ],
        "warnings": [
            str(check.get("reason") or "") for check in warned if str(check.get("reason") or "")
        ],
    }


def _call_openai_compatible_json(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any] | None:
    if not base_url or not api_key or not model:
        return None

    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    req = request.Request(endpoint, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header(
        "User-Agent",
        os.getenv("PRICE_EXTRACTOR_LLM_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
    )
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")

    try:
        with request.urlopen(req, data=body, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {detail[:2000]}") from exc

    parsed = json.loads(raw)
    choices = list(parsed.get("choices") or [])
    if not choices:
        return None
    content = str(((choices[0] or {}).get("message") or {}).get("content") or "").strip()
    if not content:
        return None
    return json.loads(content)


def build_langgraph_onboarding_payload(
    *,
    run_dir: str,
    knowledge_db: str = ".data/knowledge.db",
    top_n: int = 10,
    llm_enable: bool = False,
    llm_model: str = "gpt-4.1-mini",
    llm_base_url: str | None = None,
    llm_api_key_env: str = "OPENAI_API_KEY",
    llm_timeout_seconds: float = 60.0,
    dotenv_path: str | None = None,
    llm_critics_enable: bool = False,
    llm_critics_max: int = 10,
    llm_critics_delay_seconds: float = 0.0,
    llm_critics_retry_attempts: int = 0,
    llm_critics_retry_wait_seconds: float = 30.0,
    llm_critics_retry_backoff_multiplier: float = 1.25,
    llm_critics_context_mode: str = "full",
    llm_critics_context_max_chars: int = 8000,
    llm_critics_verbose_logs: bool = True,
    llm_critics_error_severity: str = "warn",
    deterministic_gate_run_checks: bool = False,
    deterministic_gate_check_timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Build onboarding proposal via a LangGraph pipeline.

    This is intentionally optional and default-off.
    - If LangGraph is installed, runs a tiny StateGraph that produces the proposal.
    - If not installed, falls back to the deterministic proposal builder.

    Output is a JSON-serializable dict.
    """

    def fallback_payload(reason: str) -> dict[str, Any]:
        llm_settings = _resolve_llm_settings(
            llm_enable=llm_enable,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_api_key_env=llm_api_key_env,
            llm_timeout_seconds=llm_timeout_seconds,
        )
        proposal = build_onboarding_proposal(
            run_dir=run_dir,
            knowledge_db=knowledge_db,
            top_n=top_n,
        )
        proposal_dict = asdict(proposal)
        patch_plan = _build_review_patch_plan(proposal_dict, {})
        extraction_recipe = _build_extraction_recipe(proposal_dict, {}, patch_plan)
        recipe_critique = _build_recipe_critique(extraction_recipe)
        patch_critique = _build_patch_critique(patch_plan, extraction_recipe)
        llm_critics = _build_llm_critics_bundle(
            proposal=proposal_dict,
            patch_plan=patch_plan,
            extraction_recipe=extraction_recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            llm_settings=llm_settings,
            llm_critics_enable=bool(llm_critics_enable),
            llm_critics_max=max(1, int(llm_critics_max)),
            llm_critics_delay_seconds=max(float(llm_critics_delay_seconds or 0.0), 0.0),
            llm_critics_retry_attempts=max(int(llm_critics_retry_attempts or 0), 0),
            llm_critics_retry_wait_seconds=max(float(llm_critics_retry_wait_seconds or 0.0), 0.0),
            llm_critics_retry_backoff_multiplier=max(float(llm_critics_retry_backoff_multiplier or 1.0), 1.0),
            llm_critics_context_mode=str(llm_critics_context_mode or "full"),
            llm_critics_context_max_chars=max(int(llm_critics_context_max_chars or 8000), 1000),
            llm_critics_verbose_logs=bool(llm_critics_verbose_logs),
        )
        deterministic_gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=extraction_recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=bool(deterministic_gate_run_checks),
            runtime_check_timeout_seconds=float(deterministic_gate_check_timeout_seconds),
        )
        promotion_decision = _build_promotion_decision(
            patch_plan=patch_plan,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            extraction_recipe=extraction_recipe,
            deterministic_gate=deterministic_gate,
            llm_critics=llm_critics,
            llm_critic_error_severity=str(llm_critics_error_severity or "warn"),
        )
        return {
            "framework": "langgraph_fallback",
            "frameworkDetail": reason,
            "workflow": {
                "chain": [
                    "evidence",
                    "extraction_recipe",
                    "recipe_critique",
                    "patch_plan",
                    "patch_critique",
                    "deterministic_gate",
                ]
            },
            "llm": {
                "enabled": bool(llm_settings.get("enabled")),
                "model": str(llm_settings.get("model") or ""),
                "base_url": str(llm_settings.get("base_url") or ""),
                "api_key_env": str(llm_settings.get("api_key_env") or ""),
                "api_key_present": bool(llm_settings.get("api_key_present")),
                "llm_critics_enable": bool(llm_critics_enable),
                "llm_critics_max": max(1, int(llm_critics_max)),
                "llm_critics_delay_seconds": max(float(llm_critics_delay_seconds or 0.0), 0.0),
                "llm_critics_retry_attempts": max(int(llm_critics_retry_attempts or 0), 0),
                "llm_critics_retry_wait_seconds": max(float(llm_critics_retry_wait_seconds or 0.0), 0.0),
                "llm_critics_retry_backoff_multiplier": max(float(llm_critics_retry_backoff_multiplier or 1.0), 1.0),
                "llm_critics_context_mode": (
                    "compact"
                    if str(llm_critics_context_mode or "full").strip().lower() == "compact"
                    else "full"
                ),
                "llm_critics_context_max_chars": max(int(llm_critics_context_max_chars or 8000), 1000),
                "llm_critics_verbose_logs": bool(llm_critics_verbose_logs),
                "llm_critics_error_severity": str(llm_critics_error_severity or "warn").strip().lower(),
            },
            "proposal": proposal_dict,
            "llmSuggestions": {},
            "patchPlan": patch_plan,
            "extractionRecipe": extraction_recipe,
            "recipeCritique": recipe_critique,
            "patchCritique": patch_critique,
            "llmCritics": llm_critics,
            "criticPromptRegistry": dict(llm_critics.get("registry") or {}),
            "deterministicGate": deterministic_gate,
            "promotionDecision": promotion_decision,
        }

    try:
        # Lazy import so repo stays usable without agentic extras.
        from langgraph.graph import END, StateGraph  # type: ignore
    except Exception as exc:  # pragma: no cover
        return fallback_payload(f"langgraph_import_failed: {exc}")

    # Keep state simple and JSON-friendly.
    GraphState = dict[str, Any]

    loaded_dotenv = _load_dotenv_file(dotenv_path)

    llm_settings = _resolve_llm_settings(
        llm_enable=llm_enable,
        llm_model=llm_model,
        llm_base_url=llm_base_url,
        llm_api_key_env=llm_api_key_env,
        llm_timeout_seconds=llm_timeout_seconds,
    )

    def node_compute_proposal(state: dict[str, Any]) -> dict[str, Any]:
        proposal = build_onboarding_proposal(
            run_dir=str(state["run_dir"]),
            knowledge_db=str(state["knowledge_db"]),
            top_n=int(state["top_n"]),
        )
        state["proposal"] = asdict(proposal)
        return state

    def node_llm_enrich(state: dict[str, Any]) -> dict[str, Any]:
        settings = dict(state.get("llm_settings") or {})
        if not bool(settings.get("enabled", False)):
            state["llm_status"] = "disabled"
            return state
        if not bool(settings.get("api_key_present", False)):
            state["llm_status"] = "missing_api_key"
            return state

        proposal = dict(state.get("proposal") or {})

        top_endpoints = list(proposal.get("top_endpoints") or [])[:8]
        run_evidence = _build_llm_run_evidence(
            run_dir=str(state.get("run_dir") or ""),
            top_endpoints=[row for row in top_endpoints if isinstance(row, dict)],
            max_endpoint_evidence=8,
            max_pricing_candidates=4,
        )

        prompt_payload = {
            "site_name": proposal.get("site_name"),
            "product_url": proposal.get("product_url"),
            "requires_session": proposal.get("requires_session"),
            "token_indicators": proposal.get("token_indicators"),
            "blockers": proposal.get("blockers"),
            "has_request_templates": proposal.get("has_request_templates"),
            "option_catalog_groups": proposal.get("option_catalog_groups"),
            "replay_templates": list(proposal.get("replay_templates") or [])[:5],
            "option_normalizations": proposal.get("option_normalizations"),
            "trace_template_hints": list(proposal.get("trace_template_hints") or [])[:4],
            "top_endpoints": top_endpoints,
            "run_evidence": run_evidence,
            "suggested_next_steps": list(proposal.get("suggested_next_steps") or []),
        }

        system_prompt = (
            "You are a senior extraction engineer optimizing deterministic HTTP replay and price parsing. "
            "Output STRICT JSON only (a single JSON object). "
            "No markdown, no prose outside JSON. "
            "If the context is insufficient, say so explicitly in JSON."
        )
        user_prompt = (
            "Given this deterministic extraction onboarding context, produce a JSON object with keys: "
            "recommendations (array of strings), adapter_hints (object), run_strategy (object), safety_checks (array of strings), "
            "site_adapter_code (string, optional), proposed_file_patches (array, optional), regression_assertions (array, optional).\n\n"
            "Hard requirements to avoid generic output:\n"
            "1) Every item in recommendations MUST cite concrete evidence from context by including at least one of: "
            "an endpoint URL, an HTTP method+URL pair, a request key name (e.g. a form key), "
            "or an extracted response signal (e.g. prPrice_values).\n"
            "2) adapter_hints MUST include selected_endpoint={url, method, why, evidence_signals}. "
            "The selected endpoint MUST be one of the provided top_endpoints.\n"
            "3) If you cannot justify a selected endpoint with evidence_signals from context, set "
            "adapter_hints.insufficient_evidence=true and list missing_artifacts.\n\n"
            "4) Use trace_template_hints.candidate_mutations when proposing payload rewrite paths; do not invent paths not present in hints unless clearly marked as hypothesis.\n\n"
            "Patch safety requirements:\n"
            "- proposed_file_patches paths are restricted to: src/price_extractor/site_adapters.py, tests/test_regressions.py, tests/fixtures/*.\n"
            "- proposed_file_patches operations should be one of add|update|insert|delete; for insert operations include anchor and position(before|after).\n"
            "- Never include cookie/token values or secret headers in generated content.\n"
            "- Prefer reviewable minimal patches over broad rewrites.\n\n"
            "Evidence gating:\n"
            "- If trace_template_hints are missing or do not include candidate_mutations for core options, set adapter_hints.insufficient_evidence=true and avoid site_adapter_code/proposed_file_patches for site_adapters.py.\n\n"
            "Focus on deterministic, request-only extraction reliability (no browser automation).\n\n"
            f"context={json.dumps(prompt_payload, ensure_ascii=True)}"
        )

        try:
            response_obj = _call_openai_compatible_json(
                base_url=str(settings.get("base_url") or ""),
                api_key=str(settings.get("api_key") or ""),
                model=str(settings.get("model") or ""),
                timeout_seconds=float(settings.get("timeout_seconds") or 60.0),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if isinstance(response_obj, dict):
                state["llm_suggestions"] = response_obj
                state["llm_status"] = "ok"
            else:
                state["llm_status"] = "empty_response"
        except Exception as exc:  # pragma: no cover - network/provider dependent
            state["llm_status"] = f"error:{type(exc).__name__}"
        return state

    def node_finalize(state: dict[str, Any]) -> dict[str, Any]:
        state["framework"] = "langgraph"
        state["frameworkDetail"] = "stategraph: compute_proposal -> llm_enrich -> finalize"
        proposal = dict(state.get("proposal") or {})
        llm_suggestions = dict(state.get("llm_suggestions") or {})
        patch_plan = _build_review_patch_plan(proposal, llm_suggestions)
        extraction_recipe = _build_extraction_recipe(proposal, llm_suggestions, patch_plan)
        recipe_critique = _build_recipe_critique(extraction_recipe)
        patch_critique = _build_patch_critique(patch_plan, extraction_recipe)
        llm_critics = _build_llm_critics_bundle(
            proposal=proposal,
            patch_plan=patch_plan,
            extraction_recipe=extraction_recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            llm_settings=dict(state.get("llm_settings") or {}),
            llm_critics_enable=bool(llm_critics_enable),
            llm_critics_max=max(1, int(llm_critics_max)),
            llm_critics_delay_seconds=max(float(llm_critics_delay_seconds or 0.0), 0.0),
            llm_critics_retry_attempts=max(int(llm_critics_retry_attempts or 0), 0),
            llm_critics_retry_wait_seconds=max(float(llm_critics_retry_wait_seconds or 0.0), 0.0),
            llm_critics_retry_backoff_multiplier=max(float(llm_critics_retry_backoff_multiplier or 1.0), 1.0),
            llm_critics_context_mode=str(llm_critics_context_mode or "full"),
            llm_critics_context_max_chars=max(int(llm_critics_context_max_chars or 8000), 1000),
            llm_critics_verbose_logs=bool(llm_critics_verbose_logs),
        )
        deterministic_gate = _build_deterministic_gate(
            patch_plan=patch_plan,
            extraction_recipe=extraction_recipe,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            run_runtime_checks=bool(deterministic_gate_run_checks),
            runtime_check_timeout_seconds=float(deterministic_gate_check_timeout_seconds),
        )
        promotion_decision = _build_promotion_decision(
            patch_plan=patch_plan,
            recipe_critique=recipe_critique,
            patch_critique=patch_critique,
            extraction_recipe=extraction_recipe,
            deterministic_gate=deterministic_gate,
            llm_critics=llm_critics,
            llm_critic_error_severity=str(llm_critics_error_severity or "warn"),
        )

        state["patch_plan"] = patch_plan
        state["extraction_recipe"] = extraction_recipe
        state["recipe_critique"] = recipe_critique
        state["patch_critique"] = patch_critique
        state["llm_critics"] = llm_critics
        state["critic_prompt_registry"] = dict(llm_critics.get("registry") or {})
        state["deterministic_gate"] = deterministic_gate
        state["promotion_decision"] = promotion_decision
        return state

    graph = StateGraph(GraphState)
    graph.add_node("compute_proposal", node_compute_proposal)
    graph.add_node("llm_enrich", node_llm_enrich)
    graph.add_node("finalize", node_finalize)
    graph.set_entry_point("compute_proposal")
    graph.add_edge("compute_proposal", "llm_enrich")
    graph.add_edge("llm_enrich", "finalize")
    graph.add_edge("finalize", END)

    app = graph.compile()
    final_state = app.invoke(
        {
            "run_dir": run_dir,
            "knowledge_db": knowledge_db,
            "top_n": top_n,
            "llm_settings": llm_settings,
        }
    )

    return {
        "framework": str(final_state.get("framework") or "langgraph"),
        "frameworkDetail": str(final_state.get("frameworkDetail") or ""),
        "workflow": {
            "chain": [
                "evidence",
                "extraction_recipe",
                "recipe_critique",
                "patch_plan",
                "patch_critique",
                "deterministic_gate",
            ]
        },
        "llm": {
            "enabled": bool(llm_settings.get("enabled")),
            "status": str(final_state.get("llm_status") or "not_run"),
            "model": str(llm_settings.get("model") or ""),
            "base_url": str(llm_settings.get("base_url") or ""),
            "api_key_env": str(llm_settings.get("api_key_env") or ""),
            "api_key_present": bool(llm_settings.get("api_key_present")),
            "dotenv_path": str(dotenv_path or ".env"),
            "dotenv_loaded_keys": sorted(list(loaded_dotenv.keys())),
            "llm_critics_enable": bool(llm_critics_enable),
            "llm_critics_max": max(1, int(llm_critics_max)),
            "llm_critics_delay_seconds": max(float(llm_critics_delay_seconds or 0.0), 0.0),
            "llm_critics_retry_attempts": max(int(llm_critics_retry_attempts or 0), 0),
            "llm_critics_retry_wait_seconds": max(float(llm_critics_retry_wait_seconds or 0.0), 0.0),
            "llm_critics_retry_backoff_multiplier": max(float(llm_critics_retry_backoff_multiplier or 1.0), 1.0),
            "llm_critics_context_mode": (
                "compact"
                if str(llm_critics_context_mode or "full").strip().lower() == "compact"
                else "full"
            ),
            "llm_critics_context_max_chars": max(int(llm_critics_context_max_chars or 8000), 1000),
            "llm_critics_verbose_logs": bool(llm_critics_verbose_logs),
            "llm_critics_error_severity": str(llm_critics_error_severity or "warn").strip().lower(),
            "deterministic_gate_run_checks": bool(deterministic_gate_run_checks),
            "deterministic_gate_check_timeout_seconds": float(deterministic_gate_check_timeout_seconds),
        },
        "proposal": dict(final_state.get("proposal") or {}),
        "llmSuggestions": dict(final_state.get("llm_suggestions") or {}),
        "patchPlan": dict(final_state.get("patch_plan") or {}),
        "extractionRecipe": dict(final_state.get("extraction_recipe") or {}),
        "recipeCritique": dict(final_state.get("recipe_critique") or {}),
        "patchCritique": dict(final_state.get("patch_critique") or {}),
        "llmCritics": dict(final_state.get("llm_critics") or {}),
        "criticPromptRegistry": dict(final_state.get("critic_prompt_registry") or {}),
        "deterministicGate": dict(final_state.get("deterministic_gate") or {}),
        "promotionDecision": dict(final_state.get("promotion_decision") or {}),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an onboarding proposal from an existing run directory using LangGraph. "
            "This is optional and does not affect deterministic extraction."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to a single run directory under .data/runs/<site>/<unit_id>",
    )
    parser.add_argument(
        "--knowledge-db",
        default=".data/knowledge.db",
        help="Path to sqlite knowledge database",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Number of top endpoints to include",
    )
    parser.add_argument(
        "--llm-enable",
        action="store_true",
        help="Enable optional OpenAI-compatible LLM enrichment for suggestions",
    )
    parser.add_argument(
        "--llm-model",
        default="gpt-4.1-mini",
        help="OpenAI-compatible model name for LLM enrichment",
    )
    parser.add_argument(
        "--llm-base-url",
        help="OpenAI-compatible API base URL (defaults to OPENAI_BASE_URL or https://api.openai.com/v1)",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable name that stores API key",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=60.0,
        help="LLM request timeout in seconds",
    )
    parser.add_argument(
        "--llm-critics-enable",
        action="store_true",
        help="Run dedicated per-critic LLM prompts for recipe and patch critique stages.",
    )
    parser.add_argument(
        "--llm-critics-max",
        type=int,
        default=10,
        help="Maximum number of critic prompts to execute per stage.",
    )
    parser.add_argument(
        "--llm-critics-delay-seconds",
        type=float,
        default=0.0,
        help="Delay between per-critic LLM calls in seconds.",
    )
    parser.add_argument(
        "--llm-critics-retry-attempts",
        type=int,
        default=0,
        help="Number of retry attempts per critic call after initial failure.",
    )
    parser.add_argument(
        "--llm-critics-retry-wait-seconds",
        type=float,
        default=30.0,
        help="Base wait time before retrying a failed critic call.",
    )
    parser.add_argument(
        "--llm-critics-retry-backoff-multiplier",
        type=float,
        default=1.25,
        help="Backoff multiplier applied to each subsequent retry wait.",
    )
    parser.add_argument(
        "--llm-critics-context-mode",
        choices=["full", "compact"],
        default="full",
        help="Context payload mode passed to each LLM critic prompt.",
    )
    parser.add_argument(
        "--llm-critics-context-max-chars",
        type=int,
        default=8000,
        help="Maximum serialized context characters when using compact critic context mode.",
    )
    parser.add_argument(
        "--llm-critics-quiet",
        action="store_true",
        help="Disable per-critic progress logs in terminal (stderr).",
    )
    parser.add_argument(
        "--llm-critics-error-severity",
        choices=["ignore", "warn", "fail"],
        default="warn",
        help="How promotion decision should treat LLM critic output-schema errors.",
    )
    parser.add_argument(
        "--dotenv-path",
        default=".env",
        help="Optional dotenv path to preload API keys before reading env vars",
    )
    parser.add_argument(
        "--out",
        help="Optional path to write proposal JSON",
    )
    parser.add_argument(
        "--patch-plan-out",
        help="Optional path to write patch-plan JSON artifact",
    )
    parser.add_argument(
        "--deterministic-gate-run-checks",
        action="store_true",
        help="Execute deterministic gate runtime commands (unit tests) and include results in payload.",
    )
    parser.add_argument(
        "--deterministic-gate-check-timeout-seconds",
        type=float,
        default=180.0,
        help="Timeout per deterministic-gate runtime command in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    payload = build_langgraph_onboarding_payload(
        run_dir=str(args.run_dir),
        knowledge_db=str(args.knowledge_db),
        top_n=int(args.top_n),
        llm_enable=bool(args.llm_enable),
        llm_model=str(args.llm_model),
        llm_base_url=args.llm_base_url,
        llm_api_key_env=str(args.llm_api_key_env),
        llm_timeout_seconds=float(args.llm_timeout_seconds),
        llm_critics_enable=bool(args.llm_critics_enable),
        llm_critics_max=max(int(args.llm_critics_max), 1),
        llm_critics_delay_seconds=max(float(args.llm_critics_delay_seconds), 0.0),
        llm_critics_retry_attempts=max(int(args.llm_critics_retry_attempts), 0),
        llm_critics_retry_wait_seconds=max(float(args.llm_critics_retry_wait_seconds), 0.0),
        llm_critics_retry_backoff_multiplier=max(float(args.llm_critics_retry_backoff_multiplier), 1.0),
        llm_critics_context_mode=str(args.llm_critics_context_mode),
        llm_critics_context_max_chars=max(int(args.llm_critics_context_max_chars), 1000),
        llm_critics_verbose_logs=not bool(args.llm_critics_quiet),
        llm_critics_error_severity=str(args.llm_critics_error_severity),
        dotenv_path=str(args.dotenv_path) if args.dotenv_path else None,
        deterministic_gate_run_checks=bool(args.deterministic_gate_run_checks),
        deterministic_gate_check_timeout_seconds=float(args.deterministic_gate_check_timeout_seconds),
    )

    rendered = json.dumps(payload, indent=2, ensure_ascii=True)

    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")

    if args.patch_plan_out:
        patch_payload = json.dumps(dict(payload.get("patchPlan") or {}), indent=2, ensure_ascii=True)
        Path(args.patch_plan_out).write_text(patch_payload + "\n", encoding="utf-8")

    print(rendered)


if __name__ == "__main__":
    main()
