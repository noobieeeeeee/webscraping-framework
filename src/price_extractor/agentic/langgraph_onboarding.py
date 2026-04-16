from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib import request, error as urlerror
from urllib.parse import unquote_plus

from .onboarding import build_onboarding_proposal


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


def _build_review_patch_plan(proposal: dict[str, Any], llm_suggestions: dict[str, Any]) -> dict[str, Any]:
    site_name = str(proposal.get("site_name") or "unknown")
    top_endpoints = list(proposal.get("top_endpoints") or [])
    selected_endpoint = str((top_endpoints[0] or {}).get("url") or "") if top_endpoints else ""

    recommendations = list(llm_suggestions.get("recommendations") or []) if isinstance(llm_suggestions, dict) else []
    adapter_hints = dict(llm_suggestions.get("adapter_hints") or {}) if isinstance(llm_suggestions, dict) else {}
    run_strategy = dict(llm_suggestions.get("run_strategy") or {}) if isinstance(llm_suggestions, dict) else {}
    safety_checks = list(llm_suggestions.get("safety_checks") or []) if isinstance(llm_suggestions, dict) else []

    if not safety_checks:
        safety_checks = [
            "Validate in --request-only mode before any persistence/promotion.",
            "Do not auto-apply generated code changes.",
            "Add/extend regression fixtures before enabling at scale.",
        ]

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

    return {
        "kind": "review_patch_plan_v1",
        "site": site_name,
        "selectedEndpoint": selected_endpoint,
        "targetFiles": target_files,
        "patchOutline": patch_outline,
        "adapterHints": adapter_hints,
        "runStrategy": run_strategy,
        "safetyChecks": safety_checks,
        "reviewRequired": True,
        "autoApply": False,
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
        return {
            "framework": "langgraph_fallback",
            "frameworkDetail": reason,
            "llm": {
                "enabled": bool(llm_settings.get("enabled")),
                "model": str(llm_settings.get("model") or ""),
                "base_url": str(llm_settings.get("base_url") or ""),
                "api_key_env": str(llm_settings.get("api_key_env") or ""),
                "api_key_present": bool(llm_settings.get("api_key_present")),
            },
            "proposal": asdict(proposal),
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
            "recommendations (array of strings), adapter_hints (object), run_strategy (object), safety_checks (array of strings).\n\n"
            "Hard requirements to avoid generic output:\n"
            "1) Every item in recommendations MUST cite concrete evidence from context by including at least one of: "
            "an endpoint URL, an HTTP method+URL pair, a request key name (e.g. a form key), "
            "or an extracted response signal (e.g. prPrice_values).\n"
            "2) adapter_hints MUST include selected_endpoint={url, method, why, evidence_signals}. "
            "The selected endpoint MUST be one of the provided top_endpoints.\n"
            "3) If you cannot justify a selected endpoint with evidence_signals from context, set "
            "adapter_hints.insufficient_evidence=true and list missing_artifacts.\n\n"
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
        state["patch_plan"] = _build_review_patch_plan(proposal, llm_suggestions)
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
        "llm": {
            "enabled": bool(llm_settings.get("enabled")),
            "status": str(final_state.get("llm_status") or "not_run"),
            "model": str(llm_settings.get("model") or ""),
            "base_url": str(llm_settings.get("base_url") or ""),
            "api_key_env": str(llm_settings.get("api_key_env") or ""),
            "api_key_present": bool(llm_settings.get("api_key_present")),
            "dotenv_path": str(dotenv_path or ".env"),
            "dotenv_loaded_keys": sorted(list(loaded_dotenv.keys())),
        },
        "proposal": dict(final_state.get("proposal") or {}),
        "llmSuggestions": dict(final_state.get("llm_suggestions") or {}),
        "patchPlan": dict(final_state.get("patch_plan") or {}),
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
        dotenv_path=str(args.dotenv_path) if args.dotenv_path else None,
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
