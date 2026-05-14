from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
import json
import random
import re
import time
import unicodedata
from pathlib import Path
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote

from .knowledge_store import KnowledgeStore
from .models import (
    Complexity,
    EndpointRole,
    ExtractionResult,
    FailureRecord,
    FeasibilityResult,
    ObservationBundle,
    RunState,
    Strategy,
    StrategyPlan,
    ValidationResult,
)
from .json_adapters import (
    build_adapter_http_candidate,
    extract_adapter_result,
    load_json_adapters_with_diagnostics,
    match_json_adapter_with_diagnostics,
)
from .site_adapters import build_site_replay_candidates, load_generated_site_adapters


class DiscoveryAgent:
    _REQUEST_FAMILY_TOKENS = {
        "pricing_pipeline": {
            "price",
            "pricing",
            "quote",
            "calculate",
            "productdetails",
            "final_prices",
            "total_gross",
            "total_net",
            "price-matrix",
            "price_matrix",
            "pricescale",
            "get-product-prices",
            "product-configuration",
        },
        "schema_pipeline": {
            "schema",
            "options",
            "option",
            "properties",
            "property",
            "propertyconfiguration",
            "repo",
            "catalog",
            "values",
            "value",
        },
        "quantity_pipeline": {
            "quantity",
            "qty",
            "auflage",
            "matrix",
            "scale",
            "pricescale",
            "price-matrix",
            "price_matrix",
        },
        "validation_pipeline": {
            "validate",
            "validation",
            "summary",
            "selected",
            "configuration",
            "check",
            "details",
            "verify",
        },
    }

    _INFRASTRUCTURE_TOKENS = {
        "translation",
        "translations",
        "i18n",
        "widget",
        "config",
        "auth",
        "auth-check",
        "countries",
        "country",
        "chatbot",
        "analytics",
        "consent",
        "tracking",
        "pixel",
        "gtm",
        "segment",
        "doubleclick",
        "usercentrics",
    }

    _NOISE_TOKENS = {
        "analytics",
        "tracking",
        "pixel",
        "gtm",
        "fonts",
        "doubleclick",
        "/_nuxt/",
        "/builds/meta/",
        "/meta/",
        "deeplink",
        "kameleoon",
        "useinsider",
        "userlike",
        "linkedin",
        "googletagmanager",
        "consent",
        "segment",
        "bing.com",
        "usercentrics",
        "privacy-proxy",
        "consent-api",
        "typekit",
    }

    _PRICING_SEMANTIC_TOKENS = {
        "price",
        "pricing",
        "quote",
        "calculate",
        "final_prices",
        "total_gross",
        "total_net",
        "total",
        "gross",
        "net",
        "amount",
        "productdetails",
    }

    _QUANTITY_SEMANTIC_TOKENS = {
        "quantity",
        "qty",
        "auflage",
        "pricescale",
        "price_matrix",
        "price-matrix",
        "matrix",
        "scale",
    }

    _CONFIG_SEMANTIC_TOKENS = {
        "schema",
        "options",
        "option",
        "properties",
        "property",
        "propertyconfiguration",
        "configuration",
        "repo",
        "catalog",
        "values",
        "value",
    }

    _VALIDATION_SEMANTIC_TOKENS = {
        "validate",
        "validation",
        "summary",
        "selected",
        "check",
        "details",
        "verify",
    }

    @staticmethod
    def _is_api_like(url: str) -> bool:
        lowered = url.lower()
        return any(token in lowered for token in ["/api", "graphql", "pricing", "quote", "calculate"])

    @staticmethod
    def _safe_json_loads(raw: str | None) -> Any | None:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    def _normalize_replay_url(raw_url: str) -> dict[str, Any]:
        text = str(raw_url or "").strip()
        lowered = text.lower()
        if not lowered:
            return {
                "raw": "",
                "normalizedEndpoint": "",
                "normalizedPath": "",
                "pathTokens": [],
                "urlTokens": [],
                "pathSegments": [],
            }

        parsed = urlsplit(lowered)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        path = re.sub(r"/+", "/", unquote(parsed.path or "")).strip()
        path = re.sub(r"/+", "/", path)
        if path != "/":
            path = path.rstrip("/")

        normalized_segments: list[str] = []
        for segment in [part for part in path.split("/") if part]:
            token = re.sub(r"[^a-z0-9]+", "", segment.lower())
            if token:
                normalized_segments.append(token)

        normalized_path = "/" + "/".join(normalized_segments) if normalized_segments else ""
        normalized_endpoint = urlunsplit((scheme, netloc, normalized_path, "", ""))
        path_tokens = normalized_segments
        url_tokens = [token for token in re.split(r"[^a-z0-9]+", lowered) if token]
        return {
            "raw": text,
            "normalizedEndpoint": normalized_endpoint,
            "normalizedPath": normalized_path,
            "pathTokens": path_tokens,
            "urlTokens": url_tokens,
            "pathSegments": normalized_segments,
            "scheme": scheme,
            "netloc": netloc,
        }

    @staticmethod
    def _trace_contains_semantic_tokens(
        trace: dict[str, Any],
        tokens: set[str],
    ) -> bool:
        if not tokens:
            return False
        url = str(trace.get("url", "")).lower()
        post_data = str(trace.get("post_data") or "").lower()
        response_preview = str(trace.get("response_body_preview") or "").lower()
        response_type = str(trace.get("response_content_type", "")).lower()
        request_type = str(trace.get("request_content_type", "")).lower()
        haystacks = [url, post_data, response_preview, response_type, request_type]
        return any(any(token in haystack for haystack in haystacks) for token in tokens)

    @classmethod
    def _score_trace_match_candidate(
        cls,
        endpoint_info: dict[str, Any],
        trace: dict[str, Any],
    ) -> tuple[float, str, list[str]]:
        trace_info = cls._normalize_replay_url(str(trace.get("url", "")))
        endpoint_normalized = str(endpoint_info.get("normalizedEndpoint") or "")
        trace_normalized = str(trace_info.get("normalizedEndpoint") or "")
        endpoint_path = str(endpoint_info.get("normalizedPath") or "")
        trace_path = str(trace_info.get("normalizedPath") or "")
        endpoint_tokens = list(endpoint_info.get("pathTokens") or [])
        trace_tokens = list(trace_info.get("pathTokens") or [])

        trace_url = str(trace.get("url", ""))
        method = str(trace.get("method", "GET")).upper()
        resource_type = str(trace.get("resource_type", "")).lower()
        status = int(trace.get("status", 0) or 0)
        response_type = str(trace.get("response_content_type", "") or "").lower()
        request_type = str(trace.get("request_content_type", "") or "").lower()
        has_post_data = bool(trace.get("post_data"))
        has_preview = bool(str(trace.get("response_body_preview") or "").strip())

        candidate_tags: set[str] = set()
        for token in endpoint_tokens + trace_tokens:
            if token in cls._PRICING_SEMANTIC_TOKENS:
                candidate_tags.add("pricing_semantics")
            if token in cls._QUANTITY_SEMANTIC_TOKENS:
                candidate_tags.add("quantity_semantics")
            if token in cls._CONFIG_SEMANTIC_TOKENS:
                candidate_tags.add("config_semantics")

        if cls._trace_contains_semantic_tokens(trace, cls._PRICING_SEMANTIC_TOKENS):
            candidate_tags.add("pricing_semantics")
        if cls._trace_contains_semantic_tokens(trace, cls._QUANTITY_SEMANTIC_TOKENS):
            candidate_tags.add("quantity_semantics")
        if cls._trace_contains_semantic_tokens(trace, cls._CONFIG_SEMANTIC_TOKENS):
            candidate_tags.add("config_semantics")

        match_strategy = "semantic_overlap"
        confidence = 0.0
        reasons: list[str] = []

        if endpoint_normalized and endpoint_normalized == trace_normalized:
            match_strategy = "normalized_exact"
            confidence = 1.0
            reasons.append("normalized_endpoint_match")
        elif endpoint_path and endpoint_path == trace_path:
            match_strategy = "normalized_path_exact"
            confidence = 0.95
            reasons.append("normalized_path_match")
        else:
            endpoint_prefix = endpoint_path.rstrip("/")
            trace_prefix = trace_path.rstrip("/")
            if endpoint_prefix and trace_prefix and (
                trace_prefix.startswith(endpoint_prefix) or endpoint_prefix.startswith(trace_prefix)
            ):
                match_strategy = "path_prefix"
                confidence = 0.84
                reasons.append("path_prefix_overlap")
            else:
                endpoint_base = endpoint_prefix.split("/")[-1] if endpoint_prefix else ""
                trace_base = trace_prefix.split("/")[-1] if trace_prefix else ""
                if endpoint_base and trace_base and (endpoint_base in trace_base or trace_base in endpoint_base):
                    match_strategy = "path_containment"
                    confidence = 0.78
                    reasons.append("path_token_containment")
                else:
                    endpoint_token_set = set(endpoint_tokens)
                    trace_token_set = set(trace_tokens)
                    shared_tokens = endpoint_token_set.intersection(trace_token_set)
                    if shared_tokens:
                        union = max(len(endpoint_token_set.union(trace_token_set)), 1)
                        overlap = len(shared_tokens) / union
                        confidence = min(0.72, 0.42 + overlap)
                        reasons.append(f"shared_path_tokens:{len(shared_tokens)}")
                        if shared_tokens.intersection(cls._PRICING_SEMANTIC_TOKENS):
                            reasons.append("pricing_semantics")
                        if shared_tokens.intersection(cls._QUANTITY_SEMANTIC_TOKENS):
                            reasons.append("quantity_semantics")
                        if shared_tokens.intersection(cls._CONFIG_SEMANTIC_TOKENS):
                            reasons.append("config_semantics")
                    else:
                        confidence = 0.0

        if confidence <= 0.0:
            return 0.0, "no_semantic_match", ["no_path_overlap"]

        if method == "POST":
            confidence = min(1.0, confidence + 0.08)
            reasons.append("post_request")
        elif method == "GET":
            confidence = min(1.0, confidence + 0.02)

        if resource_type in {"xhr", "fetch"}:
            confidence = min(1.0, confidence + 0.07)
            reasons.append("xhr_or_fetch")
        if 200 <= status < 300:
            confidence = min(1.0, confidence + 0.05)
            reasons.append("successful_status")
        elif status >= 400:
            confidence = max(0.0, confidence - 0.12)
            reasons.append("error_status")

        if "json" in response_type:
            confidence = min(1.0, confidence + 0.06)
            reasons.append("json_response")
        if "json" in request_type:
            confidence = min(1.0, confidence + 0.03)
            reasons.append("json_request")
        if has_post_data:
            confidence = min(1.0, confidence + 0.05)
            reasons.append("post_body_present")
        if has_preview:
            confidence = min(1.0, confidence + 0.02)

        if candidate_tags:
            reasons.extend(sorted(candidate_tags))

        if not reasons:
            reasons.append("semantic_match")

        return confidence, match_strategy, reasons

    @classmethod
    def _build_trace_match_diagnostics(
        cls,
        *,
        endpoint: str,
        endpoint_info: dict[str, Any],
        traces: list[dict[str, Any]],
        matched_trace: dict[str, Any] | None,
        match_strategy: str,
        match_confidence: float,
        mismatch_reason: str | None,
    ) -> dict[str, Any]:
        return {
            "normalizedEndpoint": str(endpoint_info.get("normalizedEndpoint") or ""),
            "candidateTraceCount": len([trace for trace in traces if str(trace.get("url", "")).strip()]),
            "matchedTraceUrl": str(matched_trace.get("url", "")) if matched_trace else None,
            "matchStrategy": match_strategy,
            "matchConfidence": round(float(match_confidence), 3),
            "mismatchReason": mismatch_reason,
            "matchedEndpoint": str(endpoint or ""),
        }

    @staticmethod
    def _collect_json_keys(node: Any, limit: int = 100) -> set[str]:
        keys: set[str] = set()

        def walk(value: Any) -> None:
            if len(keys) >= limit:
                return
            if isinstance(value, dict):
                for key, nested in value.items():
                    keys.add(str(key).lower())
                    walk(nested)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(node)
        return keys

    @staticmethod
    def _tokenize_url(url: str) -> set[str]:
        lowered = str(url or "").lower()
        tokens = {t for t in re.split(r"[^a-z0-9]+", lowered) if t}
        for hint in [
            "productdetails",
            "price-matrix",
            "price_matrix",
            "pricescale",
            "product-configuration",
            "get-product-prices",
        ]:
            if hint in lowered:
                tokens.add(hint)
        return tokens

    @staticmethod
    def _url_prefix_key(url: str, *, max_segments: int = 2) -> str:
        parsed = urlsplit(str(url or ""))
        netloc = str(parsed.netloc or "").lower()
        segments = [seg for seg in str(parsed.path or "").split("/") if seg]
        prefix = "/".join(segments[: max(int(max_segments), 1)])
        return f"{netloc}/{prefix}" if prefix else netloc

    @classmethod
    def _classify_request_family(
        cls,
        *,
        url: str,
        payload_keys: set[str],
        response_keys: set[str],
        response_preview: str,
    ) -> tuple[str, dict[str, Any]]:
        url_tokens = cls._tokenize_url(url)
        payload_tokens = {str(k).lower() for k in payload_keys}
        response_tokens = {str(k).lower() for k in response_keys}
        all_tokens = url_tokens | payload_tokens | response_tokens

        family_scores: dict[str, int] = {k: 0 for k in cls._REQUEST_FAMILY_TOKENS}
        family_hits: dict[str, list[str]] = {k: [] for k in cls._REQUEST_FAMILY_TOKENS}

        def bump(family: str, tokens: set[str], weight: int) -> None:
            hits = sorted(all_tokens.intersection(tokens))
            if hits:
                family_scores[family] += weight * len(hits)
                family_hits[family].extend(hits)

        for family, tokens in cls._REQUEST_FAMILY_TOKENS.items():
            bump(family, tokens, 2)

        preview_lower = str(response_preview or "").lower()
        if any(token in preview_lower for token in ["final_prices", "total_gross", "total_net"]):
            family_scores["pricing_pipeline"] += 3
        if any(token in preview_lower for token in ["price_matrix", "pricescale", "quantity"]):
            family_scores["quantity_pipeline"] += 2

        infra_hits = sorted(url_tokens.intersection(cls._INFRASTRUCTURE_TOKENS))
        best_family, best_score = max(family_scores.items(), key=lambda row: row[1])
        if infra_hits and best_score < 4:
            return "infrastructure", {
                "familyScores": family_scores,
                "familyHits": family_hits,
                "infraHits": infra_hits,
                "urlTokens": sorted(url_tokens)[:40],
            }

        if best_score <= 0:
            best_family = "unknown"

        return best_family, {
            "familyScores": family_scores,
            "familyHits": family_hits,
            "infraHits": infra_hits,
            "urlTokens": sorted(url_tokens)[:40],
        }

    @classmethod
    def _build_penalties(cls, *, url: str, status: int) -> list[dict[str, Any]]:
        lowered = str(url or "").lower()
        penalties: list[dict[str, Any]] = []
        noise_hits = [token for token in cls._NOISE_TOKENS if token in lowered]
        if noise_hits:
            penalties.append({"kind": "noise_tokens", "tokens": noise_hits[:8]})
        if int(status) >= 400:
            penalties.append({"kind": "http_error", "status": int(status)})
        if "premium-filecheck-price" in lowered:
            penalties.append({"kind": "known_noise_endpoint", "token": "premium-filecheck-price"})
        return penalties

    @classmethod
    def _build_payload_signals(
        cls,
        *,
        payload_keys: set[str],
        response_keys: set[str],
        post_data: Any,
        response_preview: str,
        response_type: str,
    ) -> dict[str, Any]:
        payload_tokens = {str(k).lower() for k in payload_keys}
        response_tokens = {str(k).lower() for k in response_keys}
        preview_lower = str(response_preview or "").lower()

        price_tokens = {
            "price",
            "total",
            "gross",
            "net",
            "amount",
            "final_prices",
            "total_gross_value",
        }
        quantity_tokens = {"quantity", "qty", "auflage", "pricescale", "price_matrix"}

        return {
            "hasPostData": bool(post_data),
            "payloadKeyCount": len(payload_keys),
            "responseKeyCount": len(response_keys),
            "payloadHasPriceTokens": bool(payload_tokens.intersection(price_tokens)),
            "responseHasPriceTokens": bool(response_tokens.intersection(price_tokens))
            or any(token in preview_lower for token in price_tokens),
            "payloadHasQuantityTokens": bool(payload_tokens.intersection(quantity_tokens)),
            "responseHasQuantityTokens": bool(response_tokens.intersection(quantity_tokens))
            or any(token in preview_lower for token in quantity_tokens),
            "responseIsJson": "json" in str(response_type or "").lower(),
        }

    @classmethod
    def _build_relationship_signature(
        cls,
        *,
        url: str,
        payload_keys: set[str],
        response_keys: set[str],
        response_preview: str,
        request_family: str,
    ) -> dict[str, Any]:
        url_tokens = cls._tokenize_url(url)
        payload_tokens = {str(k).lower() for k in payload_keys}
        response_tokens = {str(k).lower() for k in response_keys}
        preview_lower = str(response_preview or "").lower()
        combined = url_tokens | payload_tokens | response_tokens

        pricing_hits = combined.intersection(cls._PRICING_SEMANTIC_TOKENS) or {
            token for token in cls._PRICING_SEMANTIC_TOKENS if token in preview_lower
        }
        quantity_hits = combined.intersection(cls._QUANTITY_SEMANTIC_TOKENS) or {
            token for token in cls._QUANTITY_SEMANTIC_TOKENS if token in preview_lower
        }
        config_hits = combined.intersection(cls._CONFIG_SEMANTIC_TOKENS) or {
            token for token in cls._CONFIG_SEMANTIC_TOKENS if token in preview_lower
        }
        validation_hits = combined.intersection(cls._VALIDATION_SEMANTIC_TOKENS) or {
            token for token in cls._VALIDATION_SEMANTIC_TOKENS if token in preview_lower
        }

        semantic_tokens = set()
        semantic_tokens.update(pricing_hits)
        semantic_tokens.update(quantity_hits)
        semantic_tokens.update(config_hits)
        semantic_tokens.update(validation_hits)

        infra_hits = sorted(url_tokens.intersection(cls._INFRASTRUCTURE_TOKENS))

        return {
            "url": str(url),
            "url_tokens": url_tokens,
            "payload_keys": payload_tokens,
            "response_keys": response_tokens,
            "semantic_tokens": semantic_tokens,
            "pricing_semantic": bool(pricing_hits),
            "quantity_semantic": bool(quantity_hits),
            "config_semantic": bool(config_hits),
            "validation_semantic": bool(validation_hits),
            "infra_hits": infra_hits,
            "request_family": str(request_family or "unknown"),
            "url_prefix": cls._url_prefix_key(url),
        }

    @staticmethod
    def _merge_relationship_signature(
        current: dict[str, Any] | None,
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        if current is None:
            return dict(incoming)

        current["url_tokens"] = set(current.get("url_tokens", set())) | set(incoming.get("url_tokens", set()))
        current["payload_keys"] = set(current.get("payload_keys", set())) | set(incoming.get("payload_keys", set()))
        current["response_keys"] = set(current.get("response_keys", set())) | set(incoming.get("response_keys", set()))
        current["semantic_tokens"] = set(current.get("semantic_tokens", set())) | set(incoming.get("semantic_tokens", set()))
        current["pricing_semantic"] = bool(current.get("pricing_semantic") or incoming.get("pricing_semantic"))
        current["quantity_semantic"] = bool(current.get("quantity_semantic") or incoming.get("quantity_semantic"))
        current["config_semantic"] = bool(current.get("config_semantic") or incoming.get("config_semantic"))
        current["validation_semantic"] = bool(current.get("validation_semantic") or incoming.get("validation_semantic"))
        current["infra_hits"] = sorted(set(current.get("infra_hits", [])) | set(incoming.get("infra_hits", [])))

        if str(current.get("request_family") or "unknown") == "unknown":
            current["request_family"] = str(incoming.get("request_family") or "unknown")

        return current

    @classmethod
    def _relationship_score(
        cls,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        if left.get("url_prefix") and left.get("url_prefix") == right.get("url_prefix"):
            score += 0.25
            reasons.append("shared_url_prefix")

        left_family = str(left.get("request_family") or "unknown")
        right_family = str(right.get("request_family") or "unknown")
        if left_family == right_family and left_family != "unknown":
            weight = 0.12 if left_family == "infrastructure" else 0.18
            score += weight
            reasons.append("shared_request_family")

        shared_semantic = set(left.get("semantic_tokens", set())) & set(right.get("semantic_tokens", set()))
        if len(shared_semantic) >= 2:
            score += min(0.3, 0.08 + (0.02 * len(shared_semantic)))
            reasons.append(f"shared_semantic_tokens:{len(shared_semantic)}")

        shared_payload = set(left.get("payload_keys", set())) & set(right.get("payload_keys", set()))
        if len(shared_payload) >= 2:
            score += min(0.25, 0.05 + (0.02 * len(shared_payload)))
            reasons.append(f"shared_payload_keys:{len(shared_payload)}")

        shared_response = set(left.get("response_keys", set())) & set(right.get("response_keys", set()))
        if len(shared_response) >= 2:
            score += min(0.25, 0.05 + (0.02 * len(shared_response)))
            reasons.append(f"shared_response_keys:{len(shared_response)}")

        if left.get("pricing_semantic") and right.get("pricing_semantic"):
            score += 0.1
            reasons.append("pricing_semantics")

        if left.get("quantity_semantic") and right.get("quantity_semantic"):
            score += 0.1
            reasons.append("quantity_semantics")

        if left.get("config_semantic") and right.get("config_semantic"):
            score += 0.08
            reasons.append("config_semantics")

        if left.get("validation_semantic") and right.get("validation_semantic"):
            score += 0.06
            reasons.append("validation_semantics")

        return min(1.0, score), reasons

    @classmethod
    def _build_family_cluster_summaries(
        cls,
        rows: list[dict[str, Any]],
        context_by_url: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        family_members: dict[str, list[str]] = {}
        for row in rows:
            url = str(row.get("url", ""))
            family = str(row.get("request_family") or "unknown")
            family_members.setdefault(family, []).append(url)

        summaries: dict[str, dict[str, Any]] = {}
        for family, members in family_members.items():
            token_counts: dict[str, int] = {}
            pricing_hits = 0
            quantity_hits = 0
            infra_hits = 0
            for url in members:
                context = context_by_url.get(url) or {}
                semantic_tokens = set(context.get("semantic_tokens", set()))
                for token in semantic_tokens:
                    token_counts[token] = token_counts.get(token, 0) + 1
                if context.get("pricing_semantic"):
                    pricing_hits += 1
                if context.get("quantity_semantic"):
                    quantity_hits += 1
                if context.get("infra_hits"):
                    infra_hits += 1

            member_count = max(len(members), 1)
            dominant = [
                token
                for token, _count in sorted(token_counts.items(), key=lambda row: row[1], reverse=True)
            ][:8]

            summaries[family] = {
                "requestFamily": family,
                "memberEndpoints": members[:40],
                "dominantSignals": dominant,
                "pricingRelevance": round(pricing_hits / member_count, 3),
                "quantityRelevance": round(quantity_hits / member_count, 3),
                "infrastructureLikelihood": round(infra_hits / member_count, 3),
            }

        return summaries

    def _classify_trace_role(self, trace: dict[str, Any]) -> tuple[EndpointRole, float]:
        url = str(trace.get("url", "")).lower()
        method = str(trace.get("method", "GET")).upper()
        status = int(trace.get("status", 0) or 0)
        response_type = str(trace.get("response_content_type", "")).lower()
        post_data = trace.get("post_data") or trace.get("post_data_preview")
        response_preview = str(trace.get("response_body_preview") or "")
        payload_obj = self._safe_json_loads(post_data if isinstance(post_data, str) else None)
        payload_keys = self._collect_json_keys(payload_obj) if payload_obj is not None else set()

        response_obj = self._safe_json_loads(response_preview if isinstance(response_preview, str) else None)
        response_keys = self._collect_json_keys(response_obj) if response_obj is not None else set()

        scores: dict[EndpointRole, int] = {
            EndpointRole.SCHEMA: 0,
            EndpointRole.PRICING: 0,
            EndpointRole.QUANTITY_MATRIX: 0,
            EndpointRole.DELIVERY: 0,
            EndpointRole.VALIDATION: 0,
            EndpointRole.NOISE: 0,
        }

        if any(
            token in url
            for token in [
                "analytics",
                "tracking",
                "pixel",
                "gtm",
                "fonts",
                "doubleclick",
                "/_nuxt/",
                "/builds/meta/",
                "/meta/",
                "deeplink",
                "kameleoon",
                "useinsider",
                "userlike",
                "linkedin",
                "doubleclick",
                "googletagmanager",
                "consent",
                "segment",
                "bing.com",
                "usercentrics",
                "privacy-proxy",
                "consent-api",
                "typekit",
            ]
        ):
            scores[EndpointRole.NOISE] += 5
        if any(token in url for token in ["repo", "values", "schema", "options", "property"]):
            scores[EndpointRole.SCHEMA] += 3
        if any(token in url for token in ["price", "pricing", "quote", "calculate", "get-product-prices"]):
            scores[EndpointRole.PRICING] += 4
        if any(token in url for token in ["price-matrix", "pricescale", "scale", "auflage", "quantity"]):
            scores[EndpointRole.QUANTITY_MATRIX] += 4
        if any(token in url for token in ["delivery", "shipping", "dispatch"]):
            scores[EndpointRole.DELIVERY] += 3
        if any(token in url for token in ["details", "summary", "selected", "validate"]):
            scores[EndpointRole.VALIDATION] += 3
        if any(token in url for token in ["productdetails", "price-matrix", "pricescale", "/repo"]):
            scores[EndpointRole.PRICING] += 5
        if any(token in url for token in ["price-matrix", "pricescale"]):
            scores[EndpointRole.QUANTITY_MATRIX] += 4
        if any(token in url for token in ["premium-filecheck-price", "file-check/select-boxes"]):
            scores[EndpointRole.PRICING] -= 6
            scores[EndpointRole.VALIDATION] += 2
            scores[EndpointRole.NOISE] += 4

        if method == "POST":
            scores[EndpointRole.PRICING] += 1
            scores[EndpointRole.SCHEMA] += 1

        if payload_keys:
            if payload_keys.intersection({"properties", "propertyconfiguration", "propertyid", "value", "id", "name"}):
                scores[EndpointRole.SCHEMA] += 2
                scores[EndpointRole.PRICING] += 1
            if payload_keys.intersection({"quantity", "qty", "auflage", "pricescale"}):
                scores[EndpointRole.QUANTITY_MATRIX] += 2
            if payload_keys.intersection({"delivery", "shipping", "delivery_country_code"}):
                scores[EndpointRole.DELIVERY] += 2

            if response_keys.intersection({"price", "total", "gross", "net", "amount", "vat"}):
                scores[EndpointRole.PRICING] += 2
            if response_keys.intersection({"quantity", "qty", "auflage", "scale", "pricescale"}):
                scores[EndpointRole.QUANTITY_MATRIX] += 2
            if response_keys.intersection({"properties", "propertyconfiguration", "selectedproperties"}):
                scores[EndpointRole.SCHEMA] += 2

            if any(token in response_preview.lower() for token in ['"price"', '"total"', '"amount"', '"gross"']):
                scores[EndpointRole.PRICING] += 1

            lowered_preview = response_preview.lower()
            if any(token in lowered_preview for token in ["final_prices", "total_gross", "total_gross_value", "price_matrix", "pricescale"]):
                scores[EndpointRole.PRICING] += 5
            if any(token in lowered_preview for token in ["price_matrix", "pricescale", "quantity_heading"]):
                scores[EndpointRole.QUANTITY_MATRIX] += 3

            if (
                "premium-filecheck-price" in url
                and re.search(r'^\s*"?\d+[\.,]\d{2}\s*(?:€|eur)?\s*"?\s*$', response_preview.strip(), re.IGNORECASE)
            ):
                scores[EndpointRole.PRICING] -= 5
                scores[EndpointRole.NOISE] += 3

        if "json" in response_type:
            for role in [
                EndpointRole.SCHEMA,
                EndpointRole.PRICING,
                EndpointRole.QUANTITY_MATRIX,
                EndpointRole.DELIVERY,
                EndpointRole.VALIDATION,
            ]:
                scores[role] += 1

        if status and not (200 <= status < 300):
            for role in [
                EndpointRole.SCHEMA,
                EndpointRole.PRICING,
                EndpointRole.QUANTITY_MATRIX,
                EndpointRole.DELIVERY,
                EndpointRole.VALIDATION,
            ]:
                scores[role] -= 1

        top_role, top_score = max(scores.items(), key=lambda item: item[1])
        if top_score <= 0:
            return EndpointRole.UNKNOWN, 0.2
        confidence = min(1.0, top_score / 8.0)
        return top_role, confidence

    @staticmethod
    def _score_endpoint_candidate(
        trace: dict[str, Any],
        role: EndpointRole,
        confidence: float,
    ) -> int:
        url = str(trace.get("url", "")).lower()
        method = str(trace.get("method", "GET")).upper()
        status = int(trace.get("status", 0) or 0)
        resource_type = str(trace.get("resource_type", "")).lower()
        response_type = str(trace.get("response_content_type", "")).lower()

        role_weights = {
            EndpointRole.PRICING: 16,
            EndpointRole.QUANTITY_MATRIX: 13,
            EndpointRole.VALIDATION: 10,
            EndpointRole.SCHEMA: 9,
            EndpointRole.DELIVERY: 4,
            EndpointRole.NOISE: -20,
            EndpointRole.UNKNOWN: 0,
        }
        score = role_weights.get(role, 0)
        score += int(confidence * 8)

        if method == "POST":
            score += 4
        if resource_type in {"xhr", "fetch"}:
            score += 4
        if "json" in response_type:
            score += 3
        if 200 <= status < 300:
            score += 2
        elif status >= 400:
            score -= 3

        if any(token in url for token in ["pricing", "price", "quote", "calculate", "config", "product-configuration"]):
            score += 6
        if any(token in url for token in ["productdetails", "price-matrix", "pricescale", "/repo"]):
            score += 10
        if "premium-filecheck-price" in url:
            score -= 22
        if "file-check/select-boxes" in url:
            score -= 10
        if any(
            token in url
            for token in [
                "deeplink",
                "builds/meta",
                "/_nuxt/",
                ".js",
                ".css",
                "media-by-file-name",
                "gtm",
                "kameleoon",
                "useinsider",
                "userlike",
                "linkedin",
                "doubleclick",
                "googletagmanager",
                "consent",
                "segment",
                "bing.com",
                "usercentrics",
                "privacy-proxy",
                "consent-api",
                "typekit",
            ]
        ):
            score -= 10
        if "api." in url or "/api/" in url:
            score += 3

        body_preview = str(trace.get("response_body_preview") or "").lower()
        if body_preview and any(token in body_preview for token in ['"price"', '"total"', '"amount"', '"gross"']):
            score += 3
        if any(token in body_preview for token in ["final_prices", "total_gross", "total_gross_value"]):
            score += 14
        if any(token in body_preview for token in ["price_matrix", "pricescale", "quantity_heading"]):
            score += 12
        if "premium-filecheck-price" in url and re.search(r'^\s*"?\d+[\.,]\d{2}\s*(?:€|eur)?\s*"?\s*$', body_preview.strip(), re.IGNORECASE):
            score -= 10

        return score

    def run(self, state: RunState) -> ObservationBundle:
        url = state.target.product_url.lower()
        observed = [r.lower() for r in state.target.observed_requests]
        traces = state.target.network_traces
        trace_urls = [str(t.get("url", "")).lower() for t in traces]
        signals = state.target.bootstrap_signals
        option_groups = list(signals.get("option_groups") or [])
        option_dependencies = list(signals.get("option_dependencies") or [])
        dependency_probe = dict(signals.get("dependency_probe") or {})
        active_probe_dependencies = list(dependency_probe.get("activeProbeDependencies") or [])
        quantity_signal = dict(signals.get("quantity_signal") or {})

        has_api_calls = any(self._is_api_like(r) for r in observed + trace_urls)
        has_script_heavy_ui = any(k in url for k in ["config", "product", "shop", "/p/", "druck", "broschu", "print"])
        requires_session = (
            has_api_calls
            or any("cookie" in r for r in observed + trace_urls)
            or bool(signals.get("requires_session", False))
        )

        token_indicators = list(signals.get("token_indicators", []))
        if any("csrf" in r for r in observed):
            token_indicators.append("csrf")
        if any("bearer" in r for r in observed):
            token_indicators.append("bearer")
        token_indicators = sorted(set(token_indicators))

        blockers = []
        if requires_session:
            blockers.append("session_required")
        if token_indicators:
            blockers.append("token_management")
        if has_script_heavy_ui:
            blockers.append("stateful_configurator")
        if signals.get("anti_bot_suspected", False):
            blockers.append("anti_bot_or_waf")
        if option_groups:
            blockers.append("dependency_driven_options")
        if option_dependencies:
            blockers.append("cross_option_dependencies")
        if active_probe_dependencies:
            blockers.append("active_dependency_probe")

        endpoint_roles: dict[str, str] = {}
        role_confidence: dict[str, float] = {}
        endpoint_rankings: list[dict[str, Any]] = []
        relationship_context: dict[str, dict[str, Any]] = {}
        payload_signal_score = 0
        response_signal_score = 0
        saw_quantity_token = False
        saw_quantity_matrix = False

        for trace in traces:
            trace_url = str(trace.get("url", ""))
            if not trace_url:
                continue

            post_data = trace.get("post_data") or trace.get("post_data_preview")
            payload_obj = self._safe_json_loads(post_data if isinstance(post_data, str) else None)
            payload_keys = self._collect_json_keys(payload_obj) if payload_obj is not None else set()
            response_preview = str(trace.get("response_body_preview") or "")
            response_obj = self._safe_json_loads(response_preview if isinstance(response_preview, str) else None)
            response_keys = self._collect_json_keys(response_obj) if response_obj is not None else set()

            role, confidence = self._classify_trace_role(trace)
            endpoint_roles[trace_url] = role.value
            role_confidence[trace_url] = confidence
            endpoint_score = self._score_endpoint_candidate(trace, role, confidence)
            request_family, semantic_signals = self._classify_request_family(
                url=trace_url,
                payload_keys=payload_keys,
                response_keys=response_keys,
                response_preview=response_preview,
            )
            relationship_signature = self._build_relationship_signature(
                url=trace_url,
                payload_keys=payload_keys,
                response_keys=response_keys,
                response_preview=response_preview,
                request_family=request_family,
            )
            relationship_context[trace_url] = self._merge_relationship_signature(
                relationship_context.get(trace_url),
                relationship_signature,
            )
            payload_signals = self._build_payload_signals(
                payload_keys=payload_keys,
                response_keys=response_keys,
                post_data=post_data,
                response_preview=response_preview,
                response_type=str(trace.get("response_content_type", "")),
            )
            penalties = self._build_penalties(url=trace_url, status=int(trace.get("status", 0) or 0))
            endpoint_rankings.append(
                {
                    "url": trace_url,
                    "method": str(trace.get("method", "GET")).upper(),
                    "status": int(trace.get("status", 0) or 0),
                    "resourceType": str(trace.get("resource_type", "")),
                    "role": role.value,
                    "confidence": round(confidence, 3),
                    "score": endpoint_score,
                    "request_family": request_family,
                    "semantic_signals": semantic_signals,
                    "penalties": penalties,
                    "payload_signals": payload_signals,
                }
            )

            if post_data:
                payload_signal_score += 1
            if payload_keys.intersection({"properties", "propertyconfiguration", "propertyid", "value", "id", "name"}):
                payload_signal_score += 1
            if payload_keys.intersection({"product_alias_id", "productgroupid", "uid", "type"}):
                payload_signal_score += 1
            if payload_keys.intersection({"quantity", "qty", "auflage", "pricescale"}):
                payload_signal_score += 1
                saw_quantity_token = True

            response_type = str(trace.get("response_content_type", "")).lower()
            status = int(trace.get("status", 0) or 0)
            if "json" in response_type:
                response_signal_score += 1
            if 200 <= status < 300:
                response_signal_score += 1
            if role in {EndpointRole.PRICING, EndpointRole.SCHEMA, EndpointRole.VALIDATION, EndpointRole.QUANTITY_MATRIX}:
                response_signal_score += 1
            if role == EndpointRole.QUANTITY_MATRIX:
                saw_quantity_matrix = True

        quantity_behavior_hint = "unknown"
        if saw_quantity_matrix:
            quantity_behavior_hint = "tiered_matrix"
        elif saw_quantity_token:
            quantity_behavior_hint = "explicit_quantity"

        signal_quantity_mode = str(quantity_signal.get("mode", "")).strip().lower()
        if signal_quantity_mode:
            quantity_behavior_hint = signal_quantity_mode

        has_manual_quantity_input = bool(quantity_signal.get("hasManualInput", False))
        preset_values = list(quantity_signal.get("presetValues") or [])
        has_preset_quantities = len(preset_values) > 0
        has_quantity_threshold_behavior = bool(quantity_signal.get("hasThresholdBehavior", False))

        dedup_rankings: dict[str, dict[str, Any]] = {}
        for row in endpoint_rankings:
            key = str(row.get("url", ""))
            if not key:
                continue
            previous = dedup_rankings.get(key)
            if previous is None or int(row.get("score", 0)) > int(previous.get("score", 0)):
                dedup_rankings[key] = row

        endpoint_rankings = sorted(
            dedup_rankings.values(),
            key=lambda row: (int(row.get("score", 0)), float(row.get("confidence", 0.0))),
            reverse=True,
        )

        ranked_output = list(endpoint_rankings[:30])
        if ranked_output:
            family_summaries = self._build_family_cluster_summaries(ranked_output, relationship_context)
            for row in ranked_output:
                url_value = str(row.get("url", ""))
                context = relationship_context.get(url_value)
                if not context:
                    continue

                relationships: list[tuple[float, str, list[str]]] = []
                for other in ranked_output:
                    other_url = str(other.get("url", ""))
                    if not other_url or other_url == url_value:
                        continue
                    other_context = relationship_context.get(other_url)
                    if not other_context:
                        continue
                    confidence, reasons = self._relationship_score(context, other_context)
                    if confidence >= 0.35:
                        relationships.append((confidence, other_url, reasons))

                relationships.sort(key=lambda item: item[0], reverse=True)
                top_relationships = relationships[:4]
                row["relatedEndpoints"] = [item[1] for item in top_relationships]
                row["relationshipReasons"] = {item[1]: item[2] for item in top_relationships}
                row["relationshipConfidence"] = {item[1]: round(item[0], 3) for item in top_relationships}

                family = str(row.get("request_family") or "unknown")
                if family in family_summaries:
                    row["family_cluster_summary"] = family_summaries[family]

        endpoint_candidates = [
            str(row.get("url"))
            for row in endpoint_rankings
            if int(row.get("score", 0)) > 0 and str(row.get("role")) != EndpointRole.NOISE.value
        ]
        endpoint_candidates = list(dict.fromkeys(endpoint_candidates))[:30]

        return ObservationBundle(
            has_script_heavy_ui=has_script_heavy_ui,
            has_api_calls=has_api_calls,
            requires_session=requires_session,
            token_indicators=token_indicators,
            blockers=blockers,
            endpoint_candidates=endpoint_candidates,
            endpoint_roles=endpoint_roles,
            role_confidence=role_confidence,
            payload_signal_score=payload_signal_score,
            response_signal_score=response_signal_score,
            quantity_behavior_hint=quantity_behavior_hint,
            option_group_count=len(option_groups),
            dependency_edge_count=len(option_dependencies),
            active_dependency_edge_count=len(active_probe_dependencies),
            has_quantity_threshold_behavior=has_quantity_threshold_behavior,
            has_manual_quantity_input=has_manual_quantity_input,
            has_preset_quantities=has_preset_quantities,
            endpoint_rankings=ranked_output,
        )


class FeasibilityAgent:
    def run(self, state: RunState) -> FeasibilityResult:
        if state.observation is None:
            raise ValueError("Observation is required before feasibility analysis")

        obs = state.observation
        roles = set(obs.endpoint_roles.values())
        has_pricing_role = EndpointRole.PRICING.value in roles or EndpointRole.QUANTITY_MATRIX.value in roles
        has_schema_role = EndpointRole.SCHEMA.value in roles
        has_validation_role = EndpointRole.VALIDATION.value in roles
        strong_payload_response = obs.payload_signal_score >= 3 and obs.response_signal_score >= 3

        if not obs.has_api_calls and not obs.has_script_heavy_ui:
            return FeasibilityResult(True, Complexity.LOW, Strategy.STATIC_HTML, "Static page likely sufficient")

        if obs.has_api_calls and has_pricing_role and not obs.requires_session and strong_payload_response:
            return FeasibilityResult(
                True,
                Complexity.MEDIUM,
                Strategy.DIRECT_HTTP,
                "Pricing endpoints with sufficient payload/response signals and low session complexity",
            )

        if obs.requires_session and obs.has_api_calls and has_pricing_role:
            if has_schema_role or has_validation_role:
                return FeasibilityResult(
                    True,
                    Complexity.HIGH,
                    Strategy.HYBRID,
                    "Session/bootstrap required with identifiable pricing and schema/validation endpoints",
                )
            return FeasibilityResult(
                True,
                Complexity.HIGH,
                Strategy.BROWSER_AUTOMATION,
                "Session required but payload/response signals are weak for direct API replay",
            )

        if obs.has_api_calls:
            return FeasibilityResult(
                True,
                Complexity.HIGH,
                Strategy.HYBRID if obs.requires_session else Strategy.DIRECT_HTTP,
                "API-like traffic exists but endpoint-role confidence is limited; start with conservative extraction mode",
            )

        if obs.option_group_count > 3 and (obs.has_manual_quantity_input or obs.has_preset_quantities):
            return FeasibilityResult(
                True,
                Complexity.HIGH,
                Strategy.BROWSER_AUTOMATION,
                "Multiple dependent option groups and quantity controls detected from DOM signals",
            )

        if obs.dependency_edge_count > 0 and not obs.has_api_calls:
            return FeasibilityResult(
                True,
                Complexity.HIGH,
                Strategy.BROWSER_AUTOMATION,
                "Inferred option dependency edges from DOM without stable API signals",
            )

        if obs.active_dependency_edge_count > 0:
            return FeasibilityResult(
                True,
                Complexity.HIGH,
                Strategy.BROWSER_AUTOMATION,
                "Active probing observed downstream option-state changes, indicating strong configurator dependencies",
            )

        if obs.payload_signal_score > 0 or obs.response_signal_score > 0:
            return FeasibilityResult(
                True,
                Complexity.HIGH,
                Strategy.BROWSER_AUTOMATION,
                "Observed payload/response signals indicate dynamic behavior; browser-driven extraction recommended",
            )

        if obs.has_script_heavy_ui:
            return FeasibilityResult(True, Complexity.HIGH, Strategy.BROWSER_AUTOMATION, "Script-heavy configurator requires browser automation")

        return FeasibilityResult(False, Complexity.HIGH, Strategy.BROWSER_AUTOMATION, "Insufficient deterministic signals")


class PlannerAgent:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    @staticmethod
    def _is_unusable_endpoint(endpoint: str) -> bool:
        lowered = endpoint.lower()
        return any(
            token in lowered
            for token in [
                "deeplink",
                "builds/meta",
                "/_nuxt/",
                ".js",
                ".css",
                "kameleoon",
                "useinsider",
                "userlike",
                "linkedin",
                "doubleclick",
                "googletagmanager",
                "consent",
                "segment",
                "bing.com",
                "premium-filecheck-price",
                "file-check/select-boxes",
            ]
        )

    def run(self, state: RunState) -> StrategyPlan:
        if state.feasibility is None or state.observation is None:
            raise ValueError("Feasibility and observation are required before planning")

        remembered = self.store.get_site_success_endpoints(state.target.site_name)
        if remembered:
            for endpoint, strategy in remembered:
                if self._is_unusable_endpoint(endpoint):
                    continue
                return StrategyPlan(
                    strategy=strategy,
                    endpoint=endpoint,
                    payload_template={"productType": state.target.product_type, "options": state.target.options},
                    confidence=0.85,
                    notes="Using remembered successful endpoint",
                )

        trace_endpoint = None
        trace_hints: dict[str, str] = {}
        if state.observation.endpoint_rankings:
            best_ranked = next(
                (row for row in state.observation.endpoint_rankings if int(row.get("score", 0)) > 0),
                None,
            )
            if best_ranked is not None:
                trace_endpoint = str(best_ranked.get("url", ""))
                endpoint_role = str(best_ranked.get("role", EndpointRole.UNKNOWN.value))
                trace_hints = {
                    "method": str(best_ranked.get("method", "")),
                    "endpointRole": endpoint_role,
                    "roleConfidence": str(best_ranked.get("confidence", 0.0)),
                    "endpointScore": str(best_ranked.get("score", 0)),
                    "payloadSignalScore": str(state.observation.payload_signal_score),
                    "responseSignalScore": str(state.observation.response_signal_score),
                    "quantityBehaviorHint": state.observation.quantity_behavior_hint,
                    "optionGroupCount": str(state.observation.option_group_count),
                    "dependencyEdgeCount": str(state.observation.dependency_edge_count),
                    "activeDependencyEdgeCount": str(state.observation.active_dependency_edge_count),
                    "hasQuantityThresholdBehavior": str(state.observation.has_quantity_threshold_behavior),
                    "hasManualQuantityInput": str(state.observation.has_manual_quantity_input),
                    "hasPresetQuantities": str(state.observation.has_preset_quantities),
                }

        endpoint = trace_endpoint or (state.observation.endpoint_candidates[0] if state.observation.endpoint_candidates else None)
        return StrategyPlan(
            strategy=state.feasibility.recommended_strategy,
            endpoint=endpoint,
            payload_template={
                "productType": state.target.product_type,
                "options": state.target.options,
                "networkHints": trace_hints,
            },
            confidence=0.75 if trace_endpoint else 0.65,
            notes="Generated from feasibility output and bootstrap traces" if trace_endpoint else "Generated from feasibility output",
        )


class ExecutionAgent:
    def __init__(self) -> None:
        # Track per-host request timing across runs in this process to support polite pacing.
        self._host_last_request_ts: dict[str, float] = {}
        self._proxy_round_robin_index: int = 0
        self._proxy_consecutive_failures: dict[str, int] = {}
        self._proxy_quarantined_until: dict[str, float] = {}

    def _get_http_runtime(self, state: RunState) -> dict[str, Any]:
        runtime = dict(state.target.bootstrap_signals.get("http_runtime") or {})
        timeout_seconds = max(float(runtime.get("timeout_seconds", 15.0) or 15.0), 1.0)
        min_delay_ms = max(int(runtime.get("min_delay_ms", 0) or 0), 0)
        jitter_ms = max(int(runtime.get("jitter_ms", 0) or 0), 0)
        max_retries = max(int(runtime.get("max_retries", 0) or 0), 0)
        backoff_base_ms = max(int(runtime.get("backoff_base_ms", 400) or 400), 0)
        backoff_max_ms = max(int(runtime.get("backoff_max_ms", 5000) or 5000), 0)
        proxy_failure_threshold = max(int(runtime.get("proxy_failure_threshold", 3) or 3), 1)
        proxy_cooldown_seconds = max(int(runtime.get("proxy_cooldown_seconds", 300) or 300), 1)
        proxy_rotation = str(runtime.get("proxy_rotation", "none") or "none").strip().lower()
        if proxy_rotation not in {"none", "round_robin", "random"}:
            proxy_rotation = "none"
        proxy_pool = [
            str(proxy).strip()
            for proxy in list(runtime.get("proxy_pool") or [])
            if str(proxy).strip()
        ]
        return {
            "timeout_seconds": timeout_seconds,
            "min_delay_ms": min_delay_ms,
            "jitter_ms": jitter_ms,
            "max_retries": max_retries,
            "backoff_base_ms": backoff_base_ms,
            "backoff_max_ms": backoff_max_ms,
            "proxy_failure_threshold": proxy_failure_threshold,
            "proxy_cooldown_seconds": proxy_cooldown_seconds,
            "proxy_rotation": proxy_rotation,
            "proxy_pool": proxy_pool,
        }

    def _apply_request_pacing(self, endpoint: str, runtime: dict[str, Any]) -> float:
        min_delay_s = max(float(runtime.get("min_delay_ms", 0) or 0) / 1000.0, 0.0)
        jitter_s = max(float(runtime.get("jitter_ms", 0) or 0) / 1000.0, 0.0)
        if min_delay_s <= 0.0 and jitter_s <= 0.0:
            return 0.0

        host = str(urlsplit(endpoint).netloc or "").strip().lower()
        if not host:
            return 0.0

        now = time.monotonic()
        target_delay = min_delay_s + (random.uniform(0.0, jitter_s) if jitter_s > 0.0 else 0.0)
        previous = self._host_last_request_ts.get(host)

        sleep_s = 0.0
        if previous is not None:
            elapsed = max(now - previous, 0.0)
            sleep_s = max(target_delay - elapsed, 0.0)
            if sleep_s > 0.0:
                time.sleep(sleep_s)

        self._host_last_request_ts[host] = time.monotonic()
        return sleep_s

    def _choose_proxy(self, runtime: dict[str, Any]) -> tuple[str | None, int | None]:
        pool = [str(value).strip() for value in list(runtime.get("proxy_pool") or []) if str(value).strip()]
        if not pool:
            return None, None

        now = time.monotonic()
        available: list[tuple[int, str]] = []
        for idx, proxy_url in enumerate(pool):
            until_ts = float(self._proxy_quarantined_until.get(proxy_url, 0.0) or 0.0)
            if until_ts > now:
                continue
            available.append((idx, proxy_url))

        if not available:
            return None, None

        strategy = str(runtime.get("proxy_rotation", "none") or "none").strip().lower()
        if strategy == "random":
            selected_idx, selected_proxy = random.choice(available)
        elif strategy == "round_robin":
            pos = self._proxy_round_robin_index % len(available)
            selected_idx, selected_proxy = available[pos]
            self._proxy_round_robin_index += 1
        else:
            selected_idx, selected_proxy = available[0]
        return selected_proxy, selected_idx

    def _record_proxy_attempt_result(self, proxy_url: str | None, runtime: dict[str, Any], success: bool) -> None:
        proxy = str(proxy_url or "").strip()
        if not proxy:
            return

        if success:
            self._proxy_consecutive_failures.pop(proxy, None)
            self._proxy_quarantined_until.pop(proxy, None)
            return

        current = int(self._proxy_consecutive_failures.get(proxy, 0) or 0) + 1
        self._proxy_consecutive_failures[proxy] = current

        threshold = max(int(runtime.get("proxy_failure_threshold", 3) or 3), 1)
        if current < threshold:
            return

        cooldown_s = max(float(runtime.get("proxy_cooldown_seconds", 300) or 300), 1.0)
        self._proxy_quarantined_until[proxy] = time.monotonic() + cooldown_s
        self._proxy_consecutive_failures[proxy] = 0

    @staticmethod
    def _build_proxy_opener(proxy_url: str | None):
        if not proxy_url:
            return None
        handler = request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        return request.build_opener(handler)

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return int(status) in {408, 425, 429, 500, 502, 503, 504}

    @staticmethod
    def _is_retryable_transport_error(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, error.URLError):
            return True
        return False

    @staticmethod
    def _retry_backoff_seconds(runtime: dict[str, Any], retry_number: int) -> float:
        base_s = max(float(runtime.get("backoff_base_ms", 0) or 0) / 1000.0, 0.0)
        cap_s = max(float(runtime.get("backoff_max_ms", 0) or 0) / 1000.0, 0.0)
        if base_s <= 0.0:
            return 0.0
        delay = base_s * (2 ** max(int(retry_number), 0))
        if cap_s > 0.0:
            delay = min(delay, cap_s)
        return delay

    @staticmethod
    def _redact_proxy(proxy_url: str | None) -> str | None:
        value = str(proxy_url or "").strip()
        if not value:
            return None
        parts = urlsplit(value)
        if not parts.netloc:
            return "<configured>"
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        scheme = f"{parts.scheme}://" if parts.scheme else ""
        return f"{scheme}{host}{port}" if host else "<configured>"

    @staticmethod
    def _redact_request_headers(headers: dict[str, Any]) -> dict[str, str]:
        redacted: dict[str, str] = {}
        for key, value in dict(headers or {}).items():
            key_text = str(key)
            lowered = key_text.lower().strip()
            if lowered in {"cookie", "authorization", "proxy-authorization"}:
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = str(value)
        return redacted

    @staticmethod
    def _summarize_request_payload(
        *,
        url: str,
        method: str,
        headers: dict[str, Any],
        body: bytes | None,
    ) -> dict[str, Any]:
        body_text = ""
        if isinstance(body, (bytes, bytearray)) and body:
            body_text = bytes(body).decode("utf-8", errors="replace")

        content_type = ""
        for key, value in dict(headers or {}).items():
            if str(key).lower().strip() == "content-type":
                content_type = str(value)
                break

        parsed_body: Any = body_text
        if body_text and "json" in content_type.lower():
            try:
                parsed_body = json.loads(body_text)
            except Exception:
                parsed_body = body_text

        return {
            "url": str(url),
            "method": str(method),
            "headers": ExecutionAgent._redact_request_headers(headers),
            "body": parsed_body,
        }

    @staticmethod
    def _safe_path_fragment(value: str, *, fallback: str = "unknown") -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9._-]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-._")
        return text or fallback

    @staticmethod
    def _dump_http_response(
        *,
        site_name: str,
        endpoint: str,
        status: int,
        content_type: str,
        response_bytes: bytes,
        max_bytes: int = 5_000_000,
    ) -> dict[str, Any]:
        safe_site = ExecutionAgent._safe_path_fragment(site_name, fallback="unknown-site")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        digest = hashlib.sha1(str(endpoint).encode("utf-8")).hexdigest()[:12]

        ext = "json" if "json" in str(content_type).lower() else "txt"
        base_dir = Path(".data") / "http-responses" / safe_site
        base_dir.mkdir(parents=True, exist_ok=True)
        output_path = base_dir / f"{timestamp}_{digest}_status{int(status)}.{ext}"

        truncated = False
        to_write = response_bytes
        if isinstance(max_bytes, int) and max_bytes > 0 and len(response_bytes) > max_bytes:
            truncated = True
            to_write = response_bytes[:max_bytes]

        try:
            if ext == "json":
                decoded = to_write.decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(decoded)
                    output_path.write_text(
                        json.dumps(parsed, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:
                    output_path.write_bytes(to_write)
            else:
                output_path.write_bytes(to_write)
        except Exception as exc:
            return {
                "attempted": True,
                "ok": False,
                "errorType": type(exc).__name__,
                "error": str(exc),
            }

        return {
            "attempted": True,
            "ok": True,
            "path": str(output_path).replace("\\", "/"),
            "status": int(status),
            "contentType": str(content_type),
            "bytes": int(len(response_bytes)),
            "writtenBytes": int(len(to_write)),
            "truncated": bool(truncated),
        }

    def _find_print24_template_trace(self, state: RunState) -> dict[str, Any] | None:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for trace in state.target.network_traces:
            url = str(trace.get("url", "")).strip().lower()
            method = str(trace.get("method", "GET")).upper()
            post_data = str(trace.get("post_data") or "")
            if "print24.com" not in url:
                continue
            if "/api/de/itemmaster/calculation/productdetails/" not in url:
                continue
            if method != "POST":
                continue
            if "\"properties\"" not in post_data:
                continue

            score = 0
            if str(trace.get("resource_type", "")).lower() in {"xhr", "fetch"}:
                score += 6
            if "application/json" in str(trace.get("request_content_type", "")).lower():
                score += 4
            if "application/json" in str(trace.get("response_content_type", "")).lower():
                score += 4
            if str(trace.get("response_body_preview") or "").strip():
                score += 2
            candidates.append((score, trace))

        if not candidates:
            return None
        return max(candidates, key=lambda row: row[0])[1]

    _PRINT24_HARDCODED_FORMAT_MAP: dict[str, str] = {
        "a5": "268", "din a5": "268", "din-a5": "268", "din_a5": "268",
        "a6": "222", "din a6": "222", "din-a6": "222", "din_a6": "222",
    }
    _PRINT24_HARDCODED_QUANTITY_MAP: dict[str, str] = {
        "250": "438",
        "10": "339",
    }

    @staticmethod
    def _print24_value_matches_captured_label(requested: Any, captured_label: str) -> bool:
        req_text = str(requested or "").strip()
        cap_text = str(captured_label or "").strip()
        if not req_text or not cap_text:
            return False
        norm_req = re.sub(r"\s+", " ", unicodedata.normalize("NFKD", req_text)).strip().lower()
        norm_cap = re.sub(r"\s+", " ", unicodedata.normalize("NFKD", cap_text)).strip().lower()
        if not norm_req:
            return False
        if norm_req == norm_cap:
            return True
        return norm_req in norm_cap

    def _consume_print24_catalog(
        self,
        state: RunState,
        requested_options: dict[str, Any],
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
        desired_ids: dict[str, str] = {}
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        for row in list(prevalidation.get("matched") or []):
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if not key or key not in requested_options:
                continue
            co = dict(row.get("catalogOption") or {})
            box_name = str(co.get("name") or "").strip()
            bh = dict(co.get("backendHints") or {})
            prop_id = str(bh.get("dataPropertyId") or "").strip()
            if not box_name or not prop_id:
                continue
            captured_label = str(co.get("visibleLabel") or co.get("visibleValue") or "")
            requested_value = requested_options.get(key)
            if self._print24_value_matches_captured_label(requested_value, captured_label):
                desired_ids[box_name] = prop_id
                applied.append({"key": key, "boxName": box_name, "propId": prop_id, "capturedLabel": captured_label, "via": "harvested_catalog"})
            else:
                skipped.append({
                    "key": key,
                    "rawValue": str(requested_value),
                    "reason": "prop_id_unknown_for_value",
                    "boxName": box_name,
                    "capturedValue": captured_label,
                    "capturedPropId": prop_id,
                    "note": (
                        "Print24 catalog harvest only carries the currently-captured option per "
                        "group. The prop_id for the requested value is unknown. Re-bootstrap with "
                        "the desired value pre-selected, supply --option-id explicitly, or wait "
                        "for the `repo` probe (print24_feasibility_notes.md §3.1/§9)."
                    ),
                })
        return desired_ids, applied, skipped

    def _build_print24_synthesized_candidate(
        self,
        state: RunState,
        effective_options: dict[str, Any],
    ) -> dict[str, Any] | None:
        site_name = str(state.target.site_name or "").lower()
        if "print24" not in site_name:
            return None

        template_trace = self._find_print24_template_trace(state)
        if template_trace is None:
            return None

        raw_body = str(template_trace.get("post_data") or "").strip()
        if not raw_body:
            return None

        try:
            payload = json.loads(raw_body)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None

        desired_ids, catalog_applied, catalog_skipped = self._consume_print24_catalog(
            state, effective_options
        )
        catalog_applied_keys = {entry["key"] for entry in catalog_applied}
        catalog_covered = catalog_applied_keys | {
            entry["key"] for entry in catalog_skipped
        }

        sideload_applied: list[dict[str, Any]] = []
        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        for row in list(prevalidation.get("matched") or []):
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if not key or key not in effective_options:
                continue
            if key in catalog_applied_keys:
                continue
            sr = dict(row.get("sideloadResolution") or {})
            box_name = str(sr.get("propertyId") or "").strip()
            backend_id = str(sr.get("backendId") or "").strip()
            if not box_name or not backend_id:
                continue
            desired_ids[box_name] = backend_id
            sideload_applied.append(
                {
                    "key": key,
                    "boxName": box_name,
                    "propId": backend_id,
                    "matchedLabel": sr.get("matchedLabel"),
                    "confidence": sr.get("confidence"),
                    "productScope": list(sr.get("productScope") or ["*"]),
                    "via": "sideload",
                }
            )

        sideload_keys = {entry["key"] for entry in sideload_applied}
        resolved_keys = catalog_covered | sideload_keys
        hardcoded_applied: list[dict[str, Any]] = []
        if "format" not in resolved_keys:
            fmt_norm = str(effective_options.get("format") or "").strip().lower()
            mapped = self._PRINT24_HARDCODED_FORMAT_MAP.get(fmt_norm)
            if mapped:
                desired_ids["format"] = mapped
                hardcoded_applied.append({"key": "format", "boxName": "format", "propId": mapped, "via": "hardcoded_fallback"})
        if "quantity" not in resolved_keys:
            qty_norm = str(effective_options.get("quantity") or "").strip()
            mapped = self._PRINT24_HARDCODED_QUANTITY_MAP.get(qty_norm)
            if mapped:
                desired_ids["quantity"] = mapped
                hardcoded_applied.append({"key": "quantity", "boxName": "quantity", "propId": mapped, "via": "hardcoded_fallback"})

        updated: list[dict[str, Any]] = []
        updated_any = False
        properties = payload.get("properties")
        if not isinstance(properties, list):
            return None

        for row in properties:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name or name not in desired_ids:
                continue
            desired = desired_ids[name]
            current = str(row.get("id") or "").strip()
            if current == desired:
                continue
            row["id"] = desired
            updated_any = True
            updated.append({"name": name, "from": current, "to": desired})

        covered_keys = (
            catalog_covered | sideload_keys | {entry["key"] for entry in hardcoded_applied}
        )
        unsupported_inputs = [
            {"inputKey": key, "rawValue": str(effective_options.get(key))}
            for key in effective_options
            if str(key).strip() and key not in covered_keys
        ]

        if not updated_any and not catalog_skipped and not unsupported_inputs:
            return None

        endpoint = str(template_trace.get("url") or state.target.product_url)
        return {
            "url": endpoint,
            "method": "POST",
            "source": "print24_synthesized_template",
            "body_text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            "trace_override": template_trace,
            "request_template_applied": {
                "kind": "print24_property_ids",
                "propertiesUpdated": updated,
                "productAliasId": payload.get("product_alias_id"),
                "unsupportedInputs": unsupported_inputs,
                "catalogApplied": catalog_applied,
                "sideloadApplied": sideload_applied,
                "hardcodedApplied": hardcoded_applied,
                "skipped": catalog_skipped,
            },
        }

    @staticmethod
    def _safe_json_loads(raw: str | None) -> Any | None:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _find_trace(
        self,
        endpoint: str,
        traces: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        endpoint_text = str(endpoint or "").strip()
        endpoint_info = DiscoveryAgent._normalize_replay_url(endpoint_text)
        if not endpoint_text:
            return None, DiscoveryAgent._build_trace_match_diagnostics(
                endpoint=endpoint_text,
                endpoint_info=endpoint_info,
                traces=traces,
                matched_trace=None,
                match_strategy="missing_endpoint",
                match_confidence=0.0,
                mismatch_reason="missing_endpoint",
            )

        candidates: list[tuple[float, int, dict[str, Any], str, list[str]]] = []
        for idx, trace in enumerate(traces):
            if not str(trace.get("url", "")).strip():
                continue
            confidence, match_strategy, reasons = DiscoveryAgent._score_trace_match_candidate(endpoint_info, trace)
            if confidence <= 0.0:
                continue
            candidates.append((confidence, idx, trace, match_strategy, reasons))

        if not candidates:
            diagnostics = DiscoveryAgent._build_trace_match_diagnostics(
                endpoint=endpoint_text,
                endpoint_info=endpoint_info,
                traces=traces,
                matched_trace=None,
                match_strategy="no_match",
                match_confidence=0.0,
                mismatch_reason="no_semantic_overlap",
            )
            return None, diagnostics

        def candidate_priority(row: tuple[float, int, dict[str, Any], str, list[str]]) -> tuple[float, int, int, int, int, int, int]:
            confidence, idx, trace, _match_strategy, reasons = row
            method = str(trace.get("method", "GET")).upper()
            resource_type = str(trace.get("resource_type", "")).lower()
            status = int(trace.get("status", 0) or 0)
            response_type = str(trace.get("response_content_type", "") or "").lower()
            request_type = str(trace.get("request_content_type", "") or "").lower()
            body_present = 1 if trace.get("post_data") else 0
            semantic_bonus = 0
            reason_text = " ".join(reasons).lower()
            if any(token in reason_text for token in ["pricing_semantics", "quantity_semantics"]):
                semantic_bonus += 2
            if "config_semantics" in reason_text:
                semantic_bonus += 1
            if method == "POST":
                semantic_bonus += 3
            if resource_type in {"xhr", "fetch"}:
                semantic_bonus += 2
            if "json" in response_type:
                semantic_bonus += 2
            if "json" in request_type:
                semantic_bonus += 1
            if 200 <= status < 300:
                semantic_bonus += 1
            if body_present:
                semantic_bonus += 2
            return (confidence, semantic_bonus, body_present, 1 if method == "POST" else 0, 1 if resource_type in {"xhr", "fetch"} else 0, -status, -idx)

        confidence, idx, best, match_strategy, reasons = max(candidates, key=candidate_priority)
        diagnostics = DiscoveryAgent._build_trace_match_diagnostics(
            endpoint=endpoint_text,
            endpoint_info=endpoint_info,
            traces=traces,
            matched_trace=best,
            match_strategy=match_strategy,
            match_confidence=confidence,
            mismatch_reason=None,
        )
        diagnostics["matchReasons"] = reasons[:10]
        return best, diagnostics

    @staticmethod
    def _cookie_header(cookies: dict[str, str]) -> str:
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    @staticmethod
    def _extract_price_candidates(payload: Any) -> list[float]:
        prices: list[float] = []

        def walk(node: Any, path: str = "") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    next_path = f"{path}.{key}" if path else str(key)
                    if isinstance(value, (int, float)):
                        lowered = str(key).lower()
                        if any(token in lowered for token in ["price", "total", "amount", "gross", "net"]):
                            prices.append(float(value))
                    walk(value, next_path)
            elif isinstance(node, list):
                for item in node:
                    walk(item, path)

        walk(payload)
        return [p for p in prices if p >= 0]

    @staticmethod
    def _parse_price_string(raw_value: str) -> float | None:
        cleaned = raw_value.strip().replace(" ", "")
        if not cleaned:
            return None

        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                normalized = cleaned.replace(".", "").replace(",", ".")
            else:
                normalized = cleaned.replace(",", "")
        elif "," in cleaned:
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            parts = cleaned.split(".")
            normalized = "".join(parts) if len(parts) > 2 else cleaned

        try:
            value = float(normalized)
        except ValueError:
            return None

        if not (0.0 < value <= 1_000_000):
            return None
        return value

    @staticmethod
    def _normalize_rule_value(value: Any) -> str:
        return str(value).strip().lower()

    @staticmethod
    def _value_equals(left: Any, right: Any) -> bool:
        if left == right:
            return True
        if left is None or right is None:
            return False
        return str(left).strip().lower() == str(right).strip().lower()

    @staticmethod
    def _apply_learned_normalization_rules(
        requested_options: dict[str, Any],
        learned_rules: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not requested_options:
            return {}, []
        if not isinstance(learned_rules, dict):
            return dict(requested_options), []

        normalized_options = dict(requested_options)
        applied: list[dict[str, Any]] = []
        rules_by_key: dict[str, dict[str, str]] = {}
        for key, value in learned_rules.items():
            if isinstance(value, dict):
                rules_by_key[str(key).lower()] = {str(k): str(v) for k, v in value.items()}

        for key, raw_value in requested_options.items():
            key_rules = rules_by_key.get(str(key).lower())
            if not key_rules:
                continue
            normalized_requested = ExecutionAgent._normalize_rule_value(raw_value)
            accepted_value = key_rules.get(normalized_requested)
            if accepted_value is None:
                continue
            if ExecutionAgent._value_equals(raw_value, accepted_value):
                continue
            normalized_options[key] = accepted_value
            applied.append(
                {
                    "key": str(key),
                    "requested": raw_value,
                    "accepted": accepted_value,
                    "mode": "learned_rule",
                }
            )

        return normalized_options, applied

    @staticmethod
    def _candidate_path_priority(source: str) -> int:
        lowered = source.lower()
        score = 0
        if any(token in lowered for token in [
            "final_prices.total_gross_value",
            "final_prices.total_net_value",
            "total_gross_value",
            "total_net_value",
        ]):
            score += 40
        elif any(token in lowered for token in [
            "final_prices.total_gross",
            "final_prices.total_net",
            "total_gross",
            "total_net",
            "grandtotal",
            "gesamtpreis",
            "endpreis",
        ]):
            score += 24
        elif any(token in lowered for token in ["product_gross", "product_net", "shipping", "delivery", "unit", "single"]):
            score -= 12

        if any(token in lowered for token in ["cover", "umschlag", "surcharge", "aufschlag", "addon", "add_on", "option"]):
            score -= 10
        return score

    @staticmethod
    def _compact_price_candidates(candidates: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
        by_key: dict[tuple[float, str], dict[str, Any]] = {}
        for row in candidates:
            value = round(float(row.get("value", 0.0)), 2)
            source = str(row.get("source", "unknown"))
            score = int(row.get("score", 0))
            key = (value, source)
            existing = by_key.get(key)
            if existing is None or score > int(existing.get("score", 0)):
                by_key[key] = {
                    "value": value,
                    "score": score,
                    "source": source,
                }
        rows = sorted(
            by_key.values(),
            key=lambda item: (int(item.get("score", 0)), float(item.get("value", 0.0))),
            reverse=True,
        )
        return rows[:limit]

    @staticmethod
    def _candidate_group(source: str) -> str:
        lowered = str(source).lower()

        if any(token in lowered for token in ["shipping", "delivery", "versand", "fracht"]):
            return "shipping"

        if lowered.startswith("derived_vat:"):
            return "top_level_total"

        if any(token in lowered for token in [
            "total_gross_value",
            "total_net_value",
            "total_gross",
            "total_net",
            "finalprice",
            "endpreis",
            "grandtotal",
            "gesamtpreis",
            "order_total",
            "named:total",
        ]):
            return "top_level_total"

        if any(token in lowered for token in [
            "unit",
            "single",
            "stueck",
            "stück",
            "product_",
            "net",
            "vat",
            "tax",
            "cover",
            "umschlag",
            "surcharge",
            "aufschlag",
            "addon",
            "add_on",
            "component",
        ]):
            return "component"

        return "other"

    @staticmethod
    def _candidate_group_priority(group: str) -> int:
        priorities = {
            "top_level_total": 8,
            "other": 2,
            "component": -6,
            "shipping": -10,
        }
        return priorities.get(group, 0)

    def _build_price_candidate_views(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        raw_compacted = self._compact_price_candidates(candidates, limit=300)
        grouped: dict[str, list[dict[str, Any]]] = {
            "top_level_total": [],
            "component": [],
            "shipping": [],
            "other": [],
        }
        for row in raw_compacted:
            group = self._candidate_group(str(row.get("source", "unknown")))
            grouped[group].append(row)

        reduced_pool: list[dict[str, Any]] = []
        reduced_pool.extend(grouped["top_level_total"][:60])
        reduced_pool.extend(grouped["other"][:35])
        reduced_pool.extend(grouped["component"][:20])
        reduced_pool.extend(grouped["shipping"][:10])
        reduced_compacted = self._compact_price_candidates(reduced_pool, limit=120)

        return {
            "priceCandidates": reduced_compacted,
            "priceCandidatesRaw": raw_compacted,
            "priceCandidateGroups": grouped,
            "priceCandidateGroupCounts": {
                "top_level_total": len(grouped["top_level_total"]),
                "component": len(grouped["component"]),
                "shipping": len(grouped["shipping"]),
                "other": len(grouped["other"]),
            },
        }

    def _extract_named_prices_from_text(self, raw_text: str) -> dict[str, float]:
        named: dict[str, float] = {}
        numeric_fields = [
            "total_gross_value",
            "total_net_value",
            "product_gross_value",
            "product_net_value",
            "shipping_gross_value",
            "shipping_net_value",
            "total_vat_value",
        ]
        for key in numeric_fields:
            pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*(-?\d+(?:\.\d+)?)', re.IGNORECASE)
            match = pattern.search(raw_text)
            if not match:
                continue
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if 0.0 < value <= 1_000_000:
                named[key] = round(value, 2)

        text_fields = [
            "total_gross",
            "total_net",
            "product_gross",
            "product_net",
            "shipping_gross",
            "shipping_net",
            "total_vat",
        ]
        for key in text_fields:
            pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"', re.IGNORECASE)
            match = pattern.search(raw_text)
            if not match:
                continue
            parsed = self._parse_price_string(match.group(1))
            if parsed is not None:
                named[key] = round(parsed, 2)

        return named

    def _extract_price_candidates_from_selected_configuration(
        self,
        selected_config: dict[str, Any],
        selected_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        texts: list[tuple[str, str]] = []

        for group, value in selected_config.items():
            texts.append((f"group:{group}", str(group)))
            texts.append((f"value:{group}", str(value)))

        for row in selected_rows:
            group = str(row.get("group", ""))
            value = str(row.get("value", ""))
            if group:
                texts.append((f"row-group:{group}", group))
            if value:
                texts.append((f"row-value:{group}", value))

        seen: set[tuple[str, float]] = set()
        for source_hint, text in texts:
            lowered = text.lower()
            if not any(token in lowered for token in ["€", "eur", "preis", "price", "brutto", "gross", "netto", "net"]):
                continue

            values = self._extract_number_prices_from_text(text)
            if not values:
                continue

            base_score = 12
            if any(token in lowered for token in ["gesamt", "total", "endpreis", "brutto", "gross"]):
                base_score += 6
            if any(token in lowered for token in ["stueck", "stück", "unit", "single"]):
                base_score -= 4

            for value in values:
                key = (source_hint, round(value, 2))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "value": round(value, 2),
                        "score": base_score,
                        "source": f"selected_config:{source_hint}",
                    }
                )

        return candidates

    def _extract_price_candidates_from_summary_lines(self, lines: list[str]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if not lines:
            return candidates

        seen: set[tuple[int, float]] = set()
        for idx, line in enumerate(lines):
            text = str(line or "").strip()
            if not text:
                continue

            context_window = lines[max(0, idx - 1): min(len(lines), idx + 2)]
            context = " ".join(str(v or "") for v in context_window)
            lowered = context.lower()

            values = self._extract_number_prices_from_text(text)
            if not values and ("€" in text or "eur" in text.lower()):
                values = self._extract_number_prices_from_text(context)
            if not values:
                continue

            base_score = 10
            if any(token in lowered for token in ["inkl", "mwst", "vat", "versand", "shipping", "brutto", "gross"]):
                base_score += 2
            if any(token in lowered for token in ["netto", "net"]):
                base_score += 1
            if any(token in lowered for token in ["stueck", "stück", "single", "unit"]):
                base_score -= 3

            for value in values:
                key = (idx, round(float(value), 2))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "value": round(float(value), 2),
                        "score": base_score,
                        "source": f"price_summary:line:{idx}",
                    }
                )

        return candidates

    @staticmethod
    def _extract_vat_percent(lines: list[str]) -> float | None:
        for raw in lines:
            text = str(raw or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if not any(token in lowered for token in ["mwst", "vat", "inkl", "tax"]):
                continue
            match = re.search(r"(\d{1,2}(?:[\.,]\d{1,2})?)\s*%", lowered)
            if not match:
                continue
            value = ExecutionAgent._parse_price_string(match.group(1))
            if value is None:
                continue
            if 0 < value <= 35:
                return float(value)
        return None

    def _derive_vat_gross_candidates(
        self,
        base_candidates: list[dict[str, Any]],
        summary_lines: list[str],
    ) -> list[dict[str, Any]]:
        vat_percent = self._extract_vat_percent(summary_lines)
        if vat_percent is None:
            return []

        seeds = [
            row for row in base_candidates
            if float(row.get("value", 0.0)) > 0.0
            and str(row.get("source", "")).startswith("selected_config:")
        ]
        if not seeds:
            return []

        dedup_seed: dict[float, dict[str, Any]] = {}
        for row in seeds:
            value = round(float(row.get("value", 0.0)), 2)
            if value <= 0:
                continue
            score = int(row.get("score", 0))
            existing = dedup_seed.get(value)
            if existing is None or score > int(existing.get("score", 0)):
                dedup_seed[value] = row

        rows = sorted(
            dedup_seed.values(),
            key=lambda item: (int(item.get("score", 0)), float(item.get("value", 0.0))),
            reverse=True,
        )
        max_seed_value = max(float(row.get("value", 0.0)) for row in rows) if rows else 0.0

        multiplier = 1.0 + (vat_percent / 100.0)
        derived: list[dict[str, Any]] = []
        for row in rows[:6]:
            net_value = round(float(row.get("value", 0.0)), 2)
            if net_value <= 0:
                continue
            if max_seed_value > 0 and net_value < (max_seed_value * 0.6):
                continue
            gross_value = round(net_value * multiplier, 2)
            if gross_value <= 0 or abs(gross_value - net_value) < 0.01:
                continue
            derived.append(
                {
                    "value": gross_value,
                    "score": max(1, int(row.get("score", 0)) - 2),
                    "source": f"derived_vat:{vat_percent:.2f}%:{row.get('source', 'unknown')}",
                }
            )

        return derived

    @staticmethod
    def _is_probable_noise_url(url: str) -> bool:
        lowered = url.lower()
        return any(
            token in lowered
            for token in [
                "kameleoon",
                "usercentrics",
                "consent",
                "privacy-proxy",
                "googletagmanager",
                "google-analytics",
                "doubleclick",
                "bing.com",
                "typekit",
                "facebook",
                "linkedin",
                "analytics",
                "track-traffic-source",
            ]
        )

    @staticmethod
    def _price_keyword_weight(path_or_text: str) -> int:
        lowered = str(path_or_text).lower()
        weights = [
            ("finalprice", 18),
            ("endpreis", 18),
            ("grandtotal", 16),
            ("gesamtpreis", 16),
            ("totalgross", 15),
            ("gross", 12),
            ("brutto", 12),
            ("total", 10),
            ("sum", 9),
            ("price", 8),
            ("preis", 8),
            ("amount", 7),
            ("net", 4),
            ("netto", 4),
            ("unit", 3),
            ("stueck", 2),
        ]
        best = 0
        for token, score in weights:
            if token in lowered:
                best = max(best, score)
        return best

    def _extract_keyword_prices_from_text(self, raw_text: str, source_prefix: str) -> list[dict[str, Any]]:
        number_pattern = r"\d{1,3}(?:[\.\s]\d{3})*(?:[,\.]\d{2})|\d+(?:[,\.]\d{2})"
        keyword_groups: list[tuple[str, int]] = [
            (r"finalprice|endpreis|gesamtpreis|grandtotal", 18),
            (r"total\s*gross|gross|brutto", 13),
            (r"total|sum", 10),
            (r"price|preis|amount", 8),
            (r"net|netto|unit|stueck", 4),
        ]

        candidates: list[dict[str, Any]] = []
        for kw_pattern, kw_score in keyword_groups:
            pattern_forward = re.compile(fr"({kw_pattern}).{{0,60}}?({number_pattern})", re.IGNORECASE | re.DOTALL)
            pattern_reverse = re.compile(fr"({number_pattern}).{{0,40}}?({kw_pattern})", re.IGNORECASE | re.DOTALL)

            for match in pattern_forward.finditer(raw_text):
                value = self._parse_price_string(match.group(2))
                if value is None:
                    continue
                candidates.append(
                    {
                        "value": value,
                        "score": kw_score,
                        "source": f"{source_prefix}:kw_forward",
                    }
                )

            for match in pattern_reverse.finditer(raw_text):
                value = self._parse_price_string(match.group(1))
                if value is None:
                    continue
                candidates.append(
                    {
                        "value": value,
                        "score": max(1, kw_score - 1),
                        "source": f"{source_prefix}:kw_reverse",
                    }
                )

        euro_pattern = re.compile(fr"(?:€|eur)\s*({number_pattern})|({number_pattern})\s*(?:€|eur)", re.IGNORECASE)
        for match in euro_pattern.finditer(raw_text):
            token = match.group(1) or match.group(2)
            value = self._parse_price_string(token)
            if value is None:
                continue
            candidates.append(
                {
                    "value": value,
                    "score": 7,
                    "source": f"{source_prefix}:currency",
                }
            )

        return candidates

    def _extract_weighted_prices_from_payload(self, payload: Any) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        def walk(node: Any, path: str = "") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    next_path = f"{path}.{key}" if path else str(key)
                    key_weight = self._price_keyword_weight(str(key))

                    if isinstance(value, (int, float)):
                        if key_weight > 0 and value >= 0:
                            path_bonus = self._candidate_path_priority(next_path)
                            candidates.append(
                                {
                                    "value": float(value),
                                    "score": key_weight + path_bonus,
                                    "source": f"json:{next_path}",
                                }
                            )
                    elif isinstance(value, str):
                        if key_weight > 0:
                            parsed = self._parse_price_string(value)
                            if parsed is not None:
                                path_bonus = self._candidate_path_priority(next_path)
                                candidates.append(
                                    {
                                        "value": parsed,
                                        "score": key_weight + path_bonus,
                                        "source": f"json_str:{next_path}",
                                    }
                                )

                        if ("<" in value and ">" in value) or key_weight > 0:
                            text_hits = self._extract_keyword_prices_from_text(value[:12000], source_prefix=f"json_html:{next_path}")
                            for hit in text_hits:
                                hit["score"] = int(hit.get("score", 0)) + max(0, key_weight - 2)
                                candidates.append(hit)

                    walk(value, next_path)
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    next_path = f"{path}[{index}]" if path else f"[{index}]"
                    walk(item, next_path)

        walk(payload)
        return [c for c in candidates if 0.0 < float(c.get("value", 0.0)) <= 1_000_000]

    @staticmethod
    def _select_best_price_candidate(
        candidates: list[dict[str, Any]],
        expected_price: float | None,
    ) -> dict[str, Any] | None:
        if not candidates:
            return None

        filtered: list[dict[str, Any]] = []
        for row in candidates:
            try:
                value = float(row.get("value", 0.0))
            except Exception:
                continue
            if not (0.0 < value <= 1_000_000):
                continue
            filtered.append(row)

        if not filtered:
            return None

        best_by_value: dict[float, dict[str, Any]] = {}
        for row in filtered:
            value = round(float(row.get("value", 0.0)), 2)
            source = str(row.get("source", "unknown"))
            group = ExecutionAgent._candidate_group(source)
            score = (
                int(row.get("score", 0))
                + ExecutionAgent._candidate_path_priority(source)
                + ExecutionAgent._candidate_group_priority(group)
            )
            existing = best_by_value.get(value)
            if existing is None or score > int(existing.get("score", 0)):
                best_by_value[value] = {
                    "value": value,
                    "score": score,
                    "source": source,
                    "group": group,
                }

        deduped = list(best_by_value.values())
        if expected_price is not None:
            return max(
                deduped,
                key=lambda row: (
                    int(row.get("score", 0)),
                    ExecutionAgent._candidate_group_priority(str(row.get("group", "other"))),
                    -abs(float(row.get("value", 0.0)) - float(expected_price)),
                ),
            )

        return max(
            deduped,
            key=lambda row: (
                int(row.get("score", 0)),
                ExecutionAgent._candidate_group_priority(str(row.get("group", "other"))),
                float(row.get("value", 0.0)),
            ),
        )

    @staticmethod
    def _extract_number_prices_from_text(raw_text: str) -> list[float]:
        prices: list[float] = []
        for match in re.findall(r"(?<!\d)(?:\d{1,3}(?:[\.\s]\d{3})*[,\.]\d{2}|\d+[,\.]\d{2})(?!\d)", raw_text):
            value = ExecutionAgent._parse_price_string(match)
            if value is not None:
                prices.append(value)

        return prices

    @staticmethod
    def _extract_embedded_json_object(raw_text: str, key: str) -> dict[str, Any] | None:
        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*\{{(.*?)\}}', re.DOTALL)
        match = pattern.search(raw_text)
        if not match:
            return None
        blob = "{" + match.group(1) + "}"
        try:
            parsed = json.loads(blob)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _extract_configuration_from_preview_text(self, preview_text: str) -> dict[str, Any]:
        config: dict[str, Any] = {}
        for key in [
            "propGroupsTranslatedWithPropsTranslated",
            "propGroupsWithPropsTranslated",
            "propsTranslated",
            "propGroupWithPropertyName",
        ]:
            parsed = self._extract_embedded_json_object(preview_text, key)
            if not parsed:
                continue
            for field_name, value in parsed.items():
                text = str(value).strip()
                if text:
                    config[str(field_name)] = text
        return config

    @staticmethod
    def _values_match(left: Any, right: Any) -> bool:
        if left == right:
            return True
        if left is None or right is None:
            return False
        return str(left).strip().lower() == str(right).strip().lower()

    def _extract_server_accepted_configuration(
        self,
        payload: Any,
        requested_options: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        discovered: dict[str, Any] = {}
        requested_keys = {str(k).lower(): str(k) for k in requested_options.keys()}

        def consume_row(row: dict[str, Any]) -> None:
            lower = {str(k).lower(): k for k in row.keys()}
            name_key = next((lower[k] for k in ["name", "property", "propertyname", "key", "label"] if k in lower), None)
            value_key = next((lower[k] for k in ["value", "id", "selected", "selectedid", "selectedvalue"] if k in lower), None)

            if name_key is not None and value_key is not None:
                name = str(row[name_key])
                discovered[name] = row[value_key]

            if "propertyid" in lower and "value" in lower:
                discovered[f"propertyId:{row[lower['propertyid']]}"] = row[lower["value"]]

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    lowered = str(key).lower()
                    if lowered in requested_keys and not isinstance(value, (dict, list)):
                        discovered[requested_keys[lowered]] = value
                    if lowered in {"properties", "propertyconfiguration", "selectedproperties", "configuration"} and isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                consume_row(item)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, dict):
                        consume_row(item)
                    walk(item)

        walk(payload)

        accepted = dict(requested_options)
        accepted.update(discovered)

        normalization_signals: list[str] = []
        for key, requested_value in requested_options.items():
            accepted_value = accepted.get(key)
            if self._values_match(requested_value, accepted_value):
                continue

            key_lower = str(key).strip().lower()
            if key_lower == "quantity" and ValidationAgent._quantity_semantically_matches(requested_value, accepted_value):
                continue
            if key_lower == "format" and ValidationAgent._format_semantically_matches(requested_value, accepted_value):
                continue

            if not self._values_match(requested_value, accepted_value):
                normalization_signals.append(
                    f"config_normalized {key}: requested={requested_value} accepted={accepted_value}"
                )

        return accepted, normalization_signals

    @staticmethod
    def _normalize_catalog_text(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = text.replace("Â²", "2")
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return " ".join(text.split())

    @staticmethod
    def _decode_template_text(raw_value: Any) -> str:
        text = html.unescape(str(raw_value or "").strip())
        if not text:
            return ""
        decoded = text
        for _ in range(3):
            next_value = unquote(decoded)
            if next_value == decoded:
                break
            decoded = next_value
        return decoded

    @staticmethod
    def _replace_form_pair(pairs: list[tuple[str, str]], key: str, value: Any) -> list[tuple[str, str]]:
        updated: list[tuple[str, str]] = []
        replaced = False
        for existing_key, existing_value in pairs:
            if existing_key == key:
                updated.append((existing_key, str(value)))
                replaced = True
            else:
                updated.append((existing_key, existing_value))
        if not replaced:
            updated.append((key, str(value)))
        return updated

    @staticmethod
    def _apply_interpolation_quantity(
        pairs: list[tuple[str, str]],
        *,
        qty_field_name: str,
        typed_value: str,
    ) -> tuple[list[tuple[str, str]], bool, int]:
        if not qty_field_name or typed_value == "":
            return list(pairs), False, -1
        qty_field_index = -1
        for index, (key, _value) in enumerate(pairs):
            if key == qty_field_name:
                qty_field_index = index
                break
        if qty_field_index < 0:
            return list(pairs), False, -1
        target_qty1_index = -1
        for index in range(qty_field_index + 1, len(pairs)):
            if pairs[index][0] == "input_qty_1":
                target_qty1_index = index
                break
        new_pairs = [
            (key, "Interpolation") if index == qty_field_index else (key, value)
            for index, (key, value) in enumerate(pairs)
        ]
        if target_qty1_index >= 0:
            new_pairs[target_qty1_index] = ("input_qty_1", str(typed_value))
        else:
            new_pairs.insert(qty_field_index + 1, ("input_qty_1", str(typed_value)))
        return new_pairs, True, qty_field_index

    @staticmethod
    def _onlineprinters_group_code(
        option_row: dict[str, Any],
        group_row: dict[str, Any],
    ) -> str:
        option_name = str(option_row.get("name") or "").strip()
        match = re.match(r"input_var_([A-Za-z0-9]+)_", option_name)
        if match:
            return match.group(1)

        group_attributes = {str(k).lower(): v for k, v in dict(group_row.get("groupAttributes") or {}).items()}
        data_varindex = str(group_attributes.get("data-varindex") or "").strip()
        if data_varindex and "." not in data_varindex:
            return data_varindex
        return ""

    @staticmethod
    def _update_onlineprinters_setlink(
        current_setlink: str,
        group_code: str,
        option_code: str,
        variant_url: str | None = None,
    ) -> str:
        decoded_link = ExecutionAgent._decode_template_text(current_setlink)
        if not decoded_link:
            return ""

        parsed = urlsplit(decoded_link)
        base_parts = parsed
        if variant_url:
            resolved_variant = ExecutionAgent._decode_template_text(variant_url)
            variant_parts = urlsplit(resolved_variant)
            if variant_parts.scheme and variant_parts.netloc:
                base_parts = base_parts._replace(
                    scheme=variant_parts.scheme,
                    netloc=variant_parts.netloc,
                    path=variant_parts.path or base_parts.path,
                )

        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        depvar_value = f"<{group_code}><{option_code}>" if group_code else option_code
        replaced = False
        updated_pairs: list[tuple[str, str]] = []

        for key, value in query_pairs:
            if group_code:
                if key == "depvar_index_setparent" and value.startswith(f"<{group_code}>"):
                    updated_pairs.append((key, depvar_value))
                    replaced = True
                    continue
                if value.startswith(f"<{group_code}>"):
                    updated_pairs.append((key, depvar_value))
                    replaced = True
                    continue
            updated_pairs.append((key, value))

        if group_code and not replaced:
            param_name = "depvar_index_setparent" if any(key == "depvar_index_setparent" for key, _ in query_pairs) else "depvar_index_set"
            updated_pairs.append((param_name, depvar_value))

        return urlunsplit(
            (
                base_parts.scheme,
                base_parts.netloc,
                base_parts.path,
                urlencode(updated_pairs, doseq=True),
                base_parts.fragment,
            )
        )

    def _find_onlineprinters_template_trace(self, state: RunState) -> dict[str, Any] | None:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for trace in state.target.network_traces:
            url = str(trace.get("url", "")).strip().lower()
            method = str(trace.get("method", "GET")).upper()
            post_data = str(trace.get("post_data") or "")
            if "onlineprinters" not in url:
                continue
            if method != "POST":
                continue
            if "setlink=" not in post_data.lower():
                continue

            score = 0
            if url == state.target.product_url.lower():
                score += 8
            if str(trace.get("resource_type", "")).lower() in {"xhr", "fetch"}:
                score += 6
            if "application/json" in str(trace.get("response_content_type", "")).lower():
                score += 4
            if str(trace.get("response_body_preview") or "").strip():
                score += 2
            candidates.append((score, trace))

        if not candidates:
            return None
        return max(candidates, key=lambda row: row[0])[1]

    def _build_onlineprinters_synthesized_candidate(
        self,
        state: RunState,
        effective_options: dict[str, Any],
    ) -> dict[str, Any] | None:
        site_name = str(state.target.site_name or "").lower()
        if "onlineprinters" not in site_name:
            return None

        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        matched_rows = list(prevalidation.get("matched") or [])
        if not matched_rows:
            return None

        template_trace = self._find_onlineprinters_template_trace(state)
        if template_trace is None:
            return None

        raw_body = str(template_trace.get("post_data") or "").strip()
        if not raw_body:
            return None

        form_pairs = parse_qsl(raw_body, keep_blank_values=True)
        current_setlink = next((value for key, value in form_pairs if key.lower() == "setlink" and value), "")
        if not current_setlink:
            request_templates = dict(state.target.bootstrap_signals.get("request_templates") or {})
            setlink_candidate = request_templates.get("currentSetLink")
            if isinstance(setlink_candidate, dict):
                current_setlink = str(setlink_candidate.get("value") or "")

        endpoint = str(template_trace.get("url") or state.target.product_url)
        updated_fields: list[dict[str, Any]] = []
        skipped_fields: list[dict[str, Any]] = []
        updated_any = False
        variant_url_applied = ""

        for matched_row in matched_rows:
            option_row = dict(matched_row.get("catalogOption") or {})
            group_row = dict(matched_row.get("catalogGroup") or {})
            key = str(matched_row.get("key") or "")
            requested_value = effective_options.get(key, matched_row.get("requestedValue"))
            match_type = str(matched_row.get("matchType") or "catalog_option")

            control_name = str(option_row.get("name") or "").strip()
            if match_type == "quantity_manual" and not control_name:
                numeric_field = next(
                    (
                        name
                        for name, value in form_pairs
                        if name.startswith("input_var_")
                        and re.fullmatch(r"\d+(?:[\.,]\d+)?", str(value).strip())
                    ),
                    "",
                )
                control_name = numeric_field

            control_value = str(
                option_row.get("visibleValue")
                or option_row.get("visibleLabel")
                or requested_value
            ).strip()
            if match_type == "quantity_manual":
                control_value = str(requested_value)

            option_backend = dict(option_row.get("backendHints") or {})
            group_code = self._onlineprinters_group_code(option_row, group_row)
            option_code = str(option_backend.get("dataVarindex") or "").strip()
            variant_url = str(option_row.get("variantUrl") or option_row.get("href") or "").strip()
            if variant_url:
                variant_url_applied = urljoin(state.target.product_url, variant_url)

            # Interpolation-mode handling for quantity_manual (per
            # onlineprinters_request_modification.md §5B). See OnlineprintersSiteAdapter
            # for the design.
            interpolation_mode = False
            interpolation_note = ""
            if match_type == "quantity_manual" and control_name:
                tile_signals = dict(matched_row.get("quantityTileSignals") or {})
                tile_presets = list(tile_signals.get("tilePresets") or [])
                typed_qty_norm = str(requested_value or "").strip()
                in_presets = typed_qty_norm in tile_presets
                supports_interp = bool(tile_signals.get("supportsInterpolation"))
                interp_code = str(tile_signals.get("interpolationVarindex") or "")
                max_preset = int(tile_signals.get("maxTilePreset") or 0)
                if not in_presets and supports_interp and interp_code:
                    interpolation_mode = True
                    control_value = "Interpolation"
                    option_code = interp_code
                    if max_preset:
                        try:
                            requested_int = int(typed_qty_norm)
                            if requested_int > max_preset:
                                interpolation_note = (
                                    f"requested qty {requested_int} > max tile preset {max_preset}; "
                                    "configurator-specific interpolation upper bound applies"
                                )
                        except ValueError:
                            pass
                elif in_presets:
                    tile_var_map = dict(tile_signals.get("tileVarindexByValue") or {})
                    tile_code = str(tile_var_map.get(typed_qty_norm) or "")
                    if tile_code:
                        option_code = tile_code

            option_code_unknown = bool(option_row.get("optionCodeUnknown"))
            is_currently_selected = bool(option_row.get("selected"))
            block_write_due_to_unknown_code = (
                option_code_unknown
                and not is_currently_selected
                and match_type != "quantity_manual"
            )

            row_changed_form = False
            if interpolation_mode:
                form_pairs, did_apply, _qty_idx = self._apply_interpolation_quantity(
                    form_pairs,
                    qty_field_name=control_name,
                    typed_value=str(requested_value),
                )
                if did_apply:
                    updated_any = True
                    row_changed_form = True
            elif control_name and control_value and not block_write_due_to_unknown_code:
                form_pairs = self._replace_form_pair(form_pairs, control_name, control_value)
                updated_any = True
                row_changed_form = True

            row_changed_setlink = False
            if current_setlink and option_code and not block_write_due_to_unknown_code:
                updated_setlink = self._update_onlineprinters_setlink(
                    current_setlink,
                    group_code=group_code,
                    option_code=option_code,
                    variant_url=variant_url_applied or None,
                )
                if updated_setlink and updated_setlink != current_setlink:
                    current_setlink = updated_setlink
                    updated_any = True
                    row_changed_setlink = True

            field_record_entry: dict[str, Any] = {
                "key": key,
                "requested": requested_value,
                "controlName": control_name,
                "controlValue": control_value,
                "groupCode": group_code,
                "optionCode": option_code,
                "variantUrl": variant_url_applied or None,
                "quantityMode": ("interpolation" if interpolation_mode else ("tile" if match_type == "quantity_manual" else None)),
            }
            if interpolation_note:
                field_record_entry["interpolationNote"] = interpolation_note
            updated_fields.append(field_record_entry)

            if block_write_due_to_unknown_code:
                skipped_fields.append(
                    {
                        "key": key,
                        "requested": requested_value,
                        "reason": "option_code_unknown_for_value",
                        "currentlySelectedValue": str(option_row.get("visibleLabel") or option_row.get("visibleValue") or ""),
                        "controlName": control_name,
                        "note": "Form field name was learned from the captured POST, but the depvar option code for the requested value is unknown. Bootstrap would need to probe alternative options or scrape variant URLs to learn it.",
                    }
                )
            elif not row_changed_form and not row_changed_setlink:
                missing: list[str] = []
                if not control_name:
                    missing.append("control_name")
                if not option_code:
                    missing.append("option_code")
                if not variant_url and not current_setlink:
                    missing.append("variant_url_or_setlink")
                skipped_fields.append(
                    {
                        "key": key,
                        "requested": requested_value,
                        "reason": "catalog_row_missing_form_metadata",
                        "missing": missing,
                    }
                )

        if current_setlink:
            setlink_key = next((key for key, _value in form_pairs if key.lower() == "setlink"), "SetLink")
            form_pairs = self._replace_form_pair(form_pairs, setlink_key, current_setlink)

        if variant_url_applied:
            endpoint = variant_url_applied

        matched_keys = {str(row.get("key") or "") for row in matched_rows if str(row.get("key") or "")}
        unsupported_inputs = [
            {"inputKey": key, "rawValue": str(effective_options.get(key))}
            for key in effective_options
            if str(key).strip() and key not in matched_keys
        ]

        if not updated_any:
            return None

        return {
            "url": endpoint,
            "source": "onlineprinters_synthesized_template",
            "body_text": urlencode(form_pairs, doseq=True),
            "trace_override": template_trace,
            "request_template_applied": {
                "kind": "onlineprinters_setlink_form",
                "fieldsUpdated": updated_fields,
                "setLinkApplied": bool(current_setlink),
                "variantUrlApplied": variant_url_applied or None,
                "skipped": skipped_fields,
                "unsupportedInputs": unsupported_inputs,
            },
        }

    @staticmethod
    def _build_http_replay_candidates(state: RunState) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(url: str | None, source: str) -> None:
            value = str(url or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            candidates.append({"url": value, "source": source})

        if state.plan is not None:
            add(state.plan.endpoint, "plan_endpoint")

        rankings = state.observation.endpoint_rankings if state.observation else []

        for index, row in enumerate(rankings):
            score = int(row.get("score", 0) or 0)
            if score <= 0:
                continue
            add(str(row.get("url", "")), f"ranked_endpoint_{index}")
            if len(candidates) >= 6:
                break

        # Endpoint ranking is noisy for multi-call pricing flows: a true pricing
        # endpoint can score <=0 because infrastructure tokens penalize it or because
        # a single captured trace doesn't carry strong pricing signals on its own.
        # The request-family classifier is computed independently of that score, so
        # we rescue pricing-family endpoints that the score gate would otherwise drop.
        pricing_families = {"pricing_pipeline", "quantity_pipeline", "schema_pipeline"}
        for index, row in enumerate(rankings):
            if len(candidates) >= 6:
                break
            score = int(row.get("score", 0) or 0)
            if score > 0:
                continue
            family = str(row.get("request_family") or "")
            if family not in pricing_families:
                continue
            add(str(row.get("url", "")), f"family_rescued_{family}_{index}")

        add(state.target.product_url, "target_product_url")

        for trace in state.target.network_traces:
            resource_type = str(trace.get("resource_type", "")).lower()
            status = int(trace.get("status", 0) or 0)
            if resource_type == "document" and 200 <= status < 400:
                add(str(trace.get("url", "")), "document_trace")
            if len(candidates) >= 8:
                break

        return candidates

    @staticmethod
    def _summarize_http_replay(
        decision: str,
        attempts: list[dict[str, Any]],
        selected_endpoint: str | None = None,
        selected_via: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "decision": decision,
            "attemptCount": len(attempts),
            "attempts": attempts[:8],
        }
        if selected_endpoint:
            summary["selectedEndpoint"] = selected_endpoint
        if selected_via:
            summary["selectedVia"] = selected_via
        if reason:
            summary["reason"] = reason
        return summary

    def _try_http_replay(self, state: RunState) -> tuple[ExtractionResult | None, dict[str, Any]]:
        if state.plan is None:
            return None, self._summarize_http_replay(
                decision="skipped",
                attempts=[],
                reason="missing_plan",
            )

        request_only = bool(state.target.bootstrap_signals.get("request_only", False))

        raw_requested_options = dict(
            state.target.bootstrap_signals.get("user_requested_options")
            or state.target.options
            or {}
        )

        learned_rules = dict(state.target.bootstrap_signals.get("learned_normalization_rules") or {})
        normalization_skip_keys = {"quantity", "format"}
        normalization_input = {
            key: value
            for key, value in raw_requested_options.items()
            if str(key).strip().lower() not in normalization_skip_keys
        }
        normalized_subset, applied_rules = self._apply_learned_normalization_rules(
            normalization_input,
            learned_rules,
        )
        effective_options = dict(raw_requested_options)
        effective_options.update(normalized_subset)

        adapter_candidates: list[dict[str, Any]] = []
        adapter_diagnostics: dict[str, Any] = {
            "loadedCount": 0,
            "matchedAdapterId": None,
            "reason": "disabled",
            "warnings": [],
            "siteAdapterDiagnostics": {},
        }
        site_adapter_dir = str(state.target.bootstrap_signals.get("site_adapters_dir") or "").strip()
        allow_generated_site_adapters = bool(
            state.target.bootstrap_signals.get("allow_generated_site_adapters", False)
        )
        generated_site_adapters, site_adapter_diag = load_generated_site_adapters(
            site_adapter_dir if site_adapter_dir else None,
            allow_untrusted=allow_generated_site_adapters,
        )
        try:
            json_adapters_dir = str(state.target.bootstrap_signals.get("json_adapters_dir") or "").strip()
            json_adapters_path = Path(json_adapters_dir) if json_adapters_dir else None
            json_adapters, load_diag = load_json_adapters_with_diagnostics(adapters_dir=json_adapters_path)
            matched_adapter, match_diag = match_json_adapter_with_diagnostics(
                json_adapters,
                state.target.product_url,
            )
            adapter_diagnostics = {
                "loadedCount": int(load_diag.get("loadedCount", 0) or 0),
                "matchedAdapterId": match_diag.get("matchedAdapterId"),
                "reason": str(match_diag.get("reason") or "no_matching_adapter"),
                "warnings": list(load_diag.get("warnings") or [])[:5],
                "siteAdapterDiagnostics": site_adapter_diag,
            }
            if matched_adapter is not None:
                adapter_candidate = build_adapter_http_candidate(
                    matched_adapter,
                    target_url=state.target.product_url,
                    effective_options=effective_options,
                    raw_requested_options=raw_requested_options,
                )
                if adapter_candidate is not None:
                    adapter_candidates.append(adapter_candidate)
        except Exception:
            adapter_candidates = []
            adapter_diagnostics = {
                "loadedCount": 0,
                "matchedAdapterId": None,
                "reason": "adapter_runtime_error",
                "warnings": [],
                "siteAdapterDiagnostics": site_adapter_diag,
            }

        endpoint_candidates: list[dict[str, Any]] = list(adapter_candidates)
        endpoint_candidates.extend(
            build_site_replay_candidates(
                state,
                effective_options=effective_options,
                raw_requested_options=raw_requested_options,
                additional_adapters=generated_site_adapters,
            )
        )
        endpoint_candidates.extend(self._build_http_replay_candidates(state))
        if not endpoint_candidates:
            return None, self._summarize_http_replay(
                decision="skipped",
                attempts=[],
                reason="missing_endpoint_candidates",
            )

        attempts: list[dict[str, Any]] = []
        runtime = self._get_http_runtime(state)

        for candidate in endpoint_candidates:
            endpoint = str(candidate.get("url", "")).strip()
            endpoint_source = str(candidate.get("source", "candidate"))
            if not endpoint:
                continue

            endpoint_lower = endpoint.lower()
            allow_bootstrap_candidates = (
                (not request_only)
                and ("/api/" not in endpoint_lower)
                and ("://api." not in endpoint_lower)
            )
            trace_override = candidate.get("trace_override")
            if trace_override is not None:
                trace = trace_override
                trace_match_diag = DiscoveryAgent._build_trace_match_diagnostics(
                    endpoint=endpoint,
                    endpoint_info=DiscoveryAgent._normalize_replay_url(endpoint),
                    traces=[trace_override],
                    matched_trace=trace_override,
                    match_strategy="trace_override",
                    match_confidence=1.0,
                    mismatch_reason=None,
                )
                trace_match_diag["matchReasons"] = ["trace_override"]
            else:
                trace, trace_match_diag = self._find_trace(endpoint, state.target.network_traces)
            if trace is None:
                attempts.append(
                    {
                        "endpoint": endpoint,
                        "source": endpoint_source,
                        "result": "skipped",
                        "reason": "no_matching_trace",
                        **trace_match_diag,
                    }
                )
                continue

            # When the candidate URL is a partial/templated version of the matched trace
            # (path_prefix / path_containment), the candidate URL would hit a stale or
            # incomplete server path. The matched trace URL is from the current session
            # and was validated by the browser, so use it for the actual HTTP request.
            # Exact matches (normalized_exact, normalized_path_exact, trace_override) are
            # left unchanged because the candidate URL already carries the right path.
            match_strategy_used = str(trace_match_diag.get("matchStrategy") or "")
            if match_strategy_used in {"path_prefix", "path_containment"}:
                matched_trace_url = str(trace.get("url") or "").strip()
                if matched_trace_url and matched_trace_url != endpoint:
                    trace_match_diag["originalCandidateEndpoint"] = endpoint
                    trace_match_diag["endpointSubstituted"] = True
                    trace_match_diag["substitutionReason"] = match_strategy_used
                    endpoint = matched_trace_url
                    endpoint_lower = endpoint.lower()
                    allow_bootstrap_candidates = (
                        (not request_only)
                        and ("/api/" not in endpoint_lower)
                        and ("://api." not in endpoint_lower)
                    )

            method = str(candidate.get("method") or trace.get("method", "GET")).upper()
            if method not in {"GET", "POST"}:
                attempts.append(
                    {
                        "endpoint": endpoint,
                        "source": endpoint_source,
                        "result": "skipped",
                        "reason": "unsupported_method",
                        "method": method,
                        **trace_match_diag,
                    }
                )
                continue

            request_headers = dict(trace.get("request_headers") or {})
            request_headers.update(dict(candidate.get("request_headers") or {}))
            cookies = dict(state.target.bootstrap_signals.get("cookies") or {})
            if cookies:
                request_headers["cookie"] = self._cookie_header(cookies)

            portal_cookie = cookies.get("portalName") or cookies.get("portalname")
            if portal_cookie:
                header_keys = {str(k).lower() for k in request_headers}
                if "portal" not in header_keys:
                    request_headers["portal"] = str(portal_cookie)

            raw_body = candidate.get("body_text")
            if raw_body is None:
                raw_body = trace.get("post_data")
            body: bytes | None = None
            if isinstance(raw_body, str) and raw_body:
                body = raw_body.encode("utf-8")
            elif method == "POST":
                generated = {
                    "productType": state.target.product_type,
                    "options": effective_options,
                }
                request_headers.setdefault("content-type", "application/json")
                body = json.dumps(generated).encode("utf-8")

            response_bytes: bytes | None = None
            response_text = ""
            content_type = ""
            status = 0
            request_payload_summary: dict[str, Any] = {}
            max_retries = max(int(runtime.get("max_retries", 0) or 0), 0)
            request_succeeded = False

            for retry_index in range(max_retries + 1):
                paced_sleep_s = self._apply_request_pacing(endpoint, runtime)
                proxy_url, proxy_index = self._choose_proxy(runtime)

                req = request.Request(endpoint, data=body, method=method)
                for key, value in request_headers.items():
                    req.add_header(str(key), str(value))

                request_payload_summary = self._summarize_request_payload(
                    url=endpoint,
                    method=method,
                    headers=request_headers,
                    body=body,
                )
                request_payload_summary["runtime"] = {
                    "timeoutSeconds": float(runtime.get("timeout_seconds", 15.0)),
                    "pacedSleepMs": int(round(paced_sleep_s * 1000.0)),
                    "retryIndex": int(retry_index),
                    "maxRetries": int(max_retries),
                    "proxy": {
                        "used": bool(proxy_url),
                        "index": proxy_index,
                        "value": self._redact_proxy(proxy_url),
                    },
                }

                opener = self._build_proxy_opener(proxy_url)
                open_fn = opener.open if opener is not None else request.urlopen
                timeout_seconds = float(runtime.get("timeout_seconds", 15.0))

                retry_reason: str | None = None
                retry_error_type: str | None = None

                try:
                    with open_fn(req, timeout=timeout_seconds) as resp:
                        status = int(getattr(resp, "status", 200))
                        response_bytes = resp.read()
                        response_text = response_bytes.decode("utf-8", errors="replace")
                        content_type = str(resp.headers.get("content-type", "")).lower()
                        if status >= 400:
                            retry_reason = "http_status"
                except error.HTTPError as exc:
                    status = int(getattr(exc, "code", 0) or 0)
                    response_bytes = exc.read() if hasattr(exc, "read") else b""
                    response_text = bytes(response_bytes).decode("utf-8", errors="replace")
                    content_type = str(getattr(exc, "headers", {}).get("content-type", "")).lower()
                    retry_reason = "http_status"
                    retry_error_type = type(exc).__name__
                except Exception as exc:
                    response_bytes = None
                    retry_reason = "request_error"
                    retry_error_type = type(exc).__name__
                    if not self._is_retryable_transport_error(exc):
                        self._record_proxy_attempt_result(proxy_url, runtime, success=False)
                        attempts.append(
                            {
                                "endpoint": endpoint,
                                "source": endpoint_source,
                                "result": "failed",
                                "reason": "request_error",
                                "errorType": type(exc).__name__,
                                "retryIndex": int(retry_index),
                                "proxy": self._redact_proxy(proxy_url),
                                **trace_match_diag,
                            }
                        )
                        break

                if response_bytes is not None and not retry_reason:
                    self._record_proxy_attempt_result(proxy_url, runtime, success=True)
                    request_succeeded = True
                    break

                can_retry = retry_index < max_retries
                retryable_status = retry_reason == "http_status" and self._is_retryable_status(status)
                retryable_transport = retry_reason == "request_error"

                if can_retry and (retryable_status or retryable_transport):
                    self._record_proxy_attempt_result(proxy_url, runtime, success=False)
                    backoff_s = self._retry_backoff_seconds(runtime, retry_index)
                    attempts.append(
                        {
                            "endpoint": endpoint,
                            "source": endpoint_source,
                            "result": "retry",
                            "reason": retry_reason,
                            "status": int(status),
                            "errorType": retry_error_type,
                            "retryIndex": int(retry_index),
                            "retryInMs": int(round(backoff_s * 1000.0)),
                            "proxy": self._redact_proxy(proxy_url),
                            **trace_match_diag,
                        }
                    )
                    if backoff_s > 0:
                        time.sleep(backoff_s)
                    continue

                if retry_reason == "http_status":
                    self._record_proxy_attempt_result(proxy_url, runtime, success=False)
                    attempts.append(
                        {
                            "endpoint": endpoint,
                            "source": endpoint_source,
                            "result": "failed",
                            "reason": "http_status",
                            "status": int(status),
                            "contentType": content_type,
                            "retryIndex": int(retry_index),
                            "proxy": self._redact_proxy(proxy_url),
                            **trace_match_diag,
                        }
                    )
                elif retry_reason == "request_error":
                    self._record_proxy_attempt_result(proxy_url, runtime, success=False)
                    attempts.append(
                        {
                            "endpoint": endpoint,
                            "source": endpoint_source,
                            "result": "failed",
                            "reason": "request_error",
                            "errorType": retry_error_type,
                            "retryIndex": int(retry_index),
                            "proxy": self._redact_proxy(proxy_url),
                            **trace_match_diag,
                        }
                    )
                break

            if not request_succeeded or response_bytes is None:
                continue

            response_dump = self._dump_http_response(
                site_name=state.target.site_name,
                endpoint=endpoint,
                status=status,
                content_type=content_type,
                response_bytes=response_bytes,
            )

            payload: Any = None
            if "json" in content_type:
                try:
                    payload = json.loads(response_text)
                except Exception:
                    payload = None

            extracted_price: float | None = None
            extracted_source: str | None = None
            extracted_score: int | None = None
            all_price_candidates: list[dict[str, Any]] = []
            named_prices: dict[str, float] = {}
            price_matrix: list[dict[str, Any]] = []
            shipping_variants: list[dict[str, Any]] = []
            accepted_configuration = dict(state.target.options)
            normalization_signals: list[str] = []
            adapter_result: dict[str, Any] | None = None

            adapter_extract = dict(candidate.get("adapter_response_extract") or {})
            adapter_inputs = dict(candidate.get("adapter_inputs_used") or {})

            if payload is not None:
                if adapter_extract:
                    adapter_result = extract_adapter_result(
                        payload,
                        response_extract=adapter_extract,
                        inputs_used=adapter_inputs,
                    )
                    if adapter_result is not None:
                        extracted_price = float(adapter_result.get("price"))
                        extracted_source = str(adapter_result.get("source") or "adapter")
                        extracted_score = 1000
                        all_price_candidates.append(
                            {
                                "value": extracted_price,
                                "score": extracted_score,
                                "source": extracted_source,
                            }
                        )

                accepted_configuration, normalization_signals = self._extract_server_accepted_configuration(
                    payload,
                    effective_options,
                )
                if adapter_result is not None:
                    config_summary = adapter_result.get("config_summary")
                    if isinstance(config_summary, dict) and config_summary:
                        accepted_configuration = dict(config_summary)
                candidates = self._extract_weighted_prices_from_payload(payload)
                all_price_candidates.extend(candidates)
                best_candidate = self._select_best_price_candidate(candidates, state.target.expected_price)
                if best_candidate is not None:
                    extracted_price = float(best_candidate["value"])
                    extracted_source = str(best_candidate.get("source", "json"))
                    extracted_score = int(best_candidate.get("score", 0))

                if extracted_price is None:
                    text_hits = self._extract_keyword_prices_from_text(response_text[:15000], source_prefix="http_json_text")
                    all_price_candidates.extend(text_hits)
                    best_text = self._select_best_price_candidate(text_hits, state.target.expected_price)
                    if best_text is not None:
                        extracted_price = float(best_text["value"])
                        extracted_source = str(best_text.get("source", "http_json_text"))
                        extracted_score = int(best_text.get("score", 0))

                named_prices = self._extract_named_prices_from_text(response_text[:16000])
                if named_prices:
                    for key, value in named_prices.items():
                        candidate_score = 20 + self._candidate_path_priority(key)
                        all_price_candidates.append(
                            {
                                "value": value,
                                "score": candidate_score,
                                "source": f"named:{key}",
                            }
                        )

                    preferred_key = next(
                        (k for k in ["total_gross_value", "total_net_value", "total_gross", "total_net"] if k in named_prices),
                        None,
                    )
                    if preferred_key is not None:
                        extracted_price = float(named_prices[preferred_key])
                        extracted_source = f"named:{preferred_key}"
                        extracted_score = 30 + self._candidate_path_priority(preferred_key)

                # Infer simple quantity/price rows from response objects.
                if isinstance(payload, dict):
                    for value in payload.values():
                        if isinstance(value, list):
                            for row in value:
                                if not isinstance(row, dict):
                                    continue
                                keys = {k.lower() for k in row}
                                qty_key = next((k for k in row if str(k).lower() in {"quantity", "qty"}), None)
                                price_key = next((k for k in row if "price" in str(k).lower()), None)
                                if qty_key and price_key:
                                    price_matrix.append({"quantity": row[qty_key], "price": row[price_key]})
                                if "delivery" in keys or "shipping" in keys:
                                    shipping_variants.append(row)
            else:
                text_hits = self._extract_keyword_prices_from_text(response_text[:15000], source_prefix="http_text")
                all_price_candidates.extend(text_hits)
                best_text = self._select_best_price_candidate(text_hits, state.target.expected_price)
                if best_text is not None:
                    extracted_price = float(best_text["value"])
                    extracted_source = str(best_text.get("source", "http_text"))
                    extracted_score = int(best_text.get("score", 0))

            if allow_bootstrap_candidates:
                selected_config_candidates = self._extract_price_candidates_from_selected_configuration(
                    dict(state.target.bootstrap_signals.get("selected_configuration") or {}),
                    list(state.target.bootstrap_signals.get("selected_configuration_rows") or []),
                )
                summary_line_candidates = self._extract_price_candidates_from_summary_lines(
                    list(state.target.bootstrap_signals.get("price_summary_lines") or [])
                )
                all_price_candidates.extend(selected_config_candidates)
                all_price_candidates.extend(summary_line_candidates)
                all_price_candidates.extend(
                    self._derive_vat_gross_candidates(
                        all_price_candidates,
                        list(state.target.bootstrap_signals.get("price_summary_lines") or []),
                    )
                )

            candidate_views = self._build_price_candidate_views(all_price_candidates)
            selection_pool = list(
                candidate_views.get("priceCandidates")
                or candidate_views.get("priceCandidatesRaw")
                or all_price_candidates
            )
            best_overall = self._select_best_price_candidate(selection_pool, state.target.expected_price)
            if best_overall is not None:
                extracted_price = float(best_overall["value"])
                extracted_source = str(best_overall.get("source", extracted_source or "unknown"))
                extracted_score = int(best_overall.get("score", extracted_score or 0))

            if extracted_price is None:
                attempts.append(
                    {
                        "endpoint": endpoint,
                        "source": endpoint_source,
                        "result": "failed",
                        "reason": "no_price_extracted",
                        "status": status,
                        "contentType": content_type,
                        **trace_match_diag,
                    }
                )
                continue

            attempts.append(
                {
                    "endpoint": endpoint,
                    "source": endpoint_source,
                    "result": "success",
                    "status": status,
                    "contentType": content_type,
                    **trace_match_diag,
                }
            )
            replay_summary = self._summarize_http_replay(
                decision="used",
                attempts=attempts,
                selected_endpoint=endpoint,
                selected_via=endpoint_source,
            )

            return ExtractionResult(
                success=True,
                accepted_configuration=accepted_configuration,
                price_value=round(float(extracted_price), 2),
                currency=(
                    str(adapter_result.get("currency") or "").strip()
                    if adapter_result is not None
                    else state.target.expected_currency
                )
                or state.target.expected_currency,
                price_matrix=price_matrix,
                shipping_variants=shipping_variants,
                raw_response_summary={
                    "strategy": state.plan.strategy.value,
                    "endpoint": endpoint,
                    "replay": "adapter" if adapter_result is not None else "http",
                    "status": status,
                    "contentType": content_type,
                    "responseDump": response_dump,
                    "normalizationSignals": normalization_signals,
                    "normalizationAppliedRules": applied_rules,
                    "effectiveRequestedOptions": effective_options,
                    "requestPayload": request_payload_summary,
                    "priceSource": extracted_source,
                    "priceScore": extracted_score,
                    "namedPrices": named_prices,
                    "httpReplay": replay_summary,
                    "requestTemplateApplied": dict(candidate.get("request_template_applied") or {}),
                    "adapterResult": adapter_result,
                    "adapterDiagnostics": adapter_diagnostics,
                    **candidate_views,
                },
            ), replay_summary

        replay_summary = self._summarize_http_replay(
            decision="failed",
            attempts=attempts,
            reason="all_http_candidates_failed",
        )
        return None, replay_summary

    def _extract_price_from_traces(self, state: RunState) -> ExtractionResult | None:
        if state.observation is None:
            return None

        effective_options, applied_rules = self._apply_learned_normalization_rules(
            state.target.options,
            dict(state.target.bootstrap_signals.get("learned_normalization_rules") or {}),
        )

        ranked_urls = [
            str(row.get("url", ""))
            for row in state.observation.endpoint_rankings
            if int(row.get("score", 0)) > 0
        ]
        if not ranked_urls:
            ranked_urls = [str(t.get("url", "")) for t in state.target.network_traces]

        trace_by_url: dict[str, dict[str, Any]] = {
            str(t.get("url", "")): t for t in state.target.network_traces if str(t.get("url", ""))
        }
        endpoint_score_by_url = {
            str(row.get("url", "")): int(row.get("score", 0))
            for row in state.observation.endpoint_rankings
            if str(row.get("url", ""))
        }
        selected_config_candidates = self._extract_price_candidates_from_selected_configuration(
            dict(state.target.bootstrap_signals.get("selected_configuration") or {}),
            list(state.target.bootstrap_signals.get("selected_configuration_rows") or []),
        )
        summary_line_candidates = self._extract_price_candidates_from_summary_lines(
            list(state.target.bootstrap_signals.get("price_summary_lines") or [])
        )

        for url in ranked_urls:
            if self._is_probable_noise_url(url):
                continue
            url_lower = url.lower()
            allow_bootstrap_candidates = ("/api/" not in url_lower) and ("://api." not in url_lower)
            trace = trace_by_url.get(url)
            if trace is None:
                continue

            preview = str(trace.get("response_body_preview") or "").strip()
            if not preview:
                continue

            extracted_price: float | None = None
            extracted_source: str | None = None
            extracted_score: int | None = None
            accepted_configuration = dict(state.target.options)
            normalization_signals: list[str] = []
            response_configuration_snapshot: dict[str, Any] = {}
            all_price_candidates: list[dict[str, Any]] = []
            named_prices: dict[str, float] = {}
            response_type = str(trace.get("response_content_type", "")).lower()
            parsed = self._safe_json_loads(preview)
            if parsed is not None:
                accepted_configuration, normalization_signals = self._extract_server_accepted_configuration(
                    parsed,
                    effective_options,
                )
                if accepted_configuration:
                    response_configuration_snapshot = dict(accepted_configuration)
                weighted = self._extract_weighted_prices_from_payload(parsed)
                all_price_candidates.extend(weighted)
                best = self._select_best_price_candidate(weighted, state.target.expected_price)
                if best is not None:
                    extracted_price = float(best["value"])
                    extracted_source = str(best.get("source", "captured_trace_json"))
                    extracted_score = int(best.get("score", 0))

                if extracted_price is None:
                    lower_preview = preview.lower()
                    if any(token in lower_preview for token in ["price", "total", "gross", "net", "amount", "eur", "€"]):
                        text_hits = self._extract_keyword_prices_from_text(preview, source_prefix="captured_trace_json_text")
                        all_price_candidates.extend(text_hits)
                        best_text = self._select_best_price_candidate(text_hits, state.target.expected_price)
                        if best_text is not None:
                            extracted_price = float(best_text["value"])
                            extracted_source = str(best_text.get("source", "captured_trace_json_text"))
                            extracted_score = int(best_text.get("score", 0))

                named_prices = self._extract_named_prices_from_text(preview)
                if named_prices:
                    for key, value in named_prices.items():
                        candidate_score = 20 + self._candidate_path_priority(key)
                        all_price_candidates.append(
                            {
                                "value": value,
                                "score": candidate_score,
                                "source": f"named:{key}",
                            }
                        )

                    preferred_key = next(
                        (k for k in ["total_gross_value", "total_net_value", "total_gross", "total_net"] if k in named_prices),
                        None,
                    )
                    if preferred_key is not None:
                        extracted_price = float(named_prices[preferred_key])
                        extracted_source = f"named:{preferred_key}"
                        extracted_score = 30 + self._candidate_path_priority(preferred_key)
            else:
                lower_preview = preview.lower()
                if not any(token in lower_preview for token in ["price", "total", "amount", "gross", "net", "eur", "€"]):
                    continue

                preview_config = self._extract_configuration_from_preview_text(preview)
                if preview_config:
                    response_configuration_snapshot = preview_config

                text_hits = self._extract_keyword_prices_from_text(
                    preview,
                    source_prefix="captured_trace_html" if "html" in response_type else "captured_trace_text",
                )
                all_price_candidates.extend(text_hits)
                best_text = self._select_best_price_candidate(text_hits, state.target.expected_price)
                if best_text is not None:
                    extracted_price = float(best_text["value"])
                    extracted_source = str(best_text.get("source", "captured_trace_text"))
                    extracted_score = int(best_text.get("score", 0))

                named_prices = self._extract_named_prices_from_text(preview)
                if named_prices:
                    for key, value in named_prices.items():
                        candidate_score = 20 + self._candidate_path_priority(key)
                        all_price_candidates.append(
                            {
                                "value": value,
                                "score": candidate_score,
                                "source": f"named:{key}",
                            }
                        )

                    preferred_key = next(
                        (k for k in ["total_gross_value", "total_net_value", "total_gross", "total_net"] if k in named_prices),
                        None,
                    )
                    if preferred_key is not None:
                        extracted_price = float(named_prices[preferred_key])
                        extracted_source = f"named:{preferred_key}"
                        extracted_score = 30 + self._candidate_path_priority(preferred_key)

            if allow_bootstrap_candidates:
                all_price_candidates.extend(selected_config_candidates)
                all_price_candidates.extend(summary_line_candidates)
                all_price_candidates.extend(
                    self._derive_vat_gross_candidates(
                        all_price_candidates,
                        list(state.target.bootstrap_signals.get("price_summary_lines") or []),
                    )
                )
            candidate_views = self._build_price_candidate_views(all_price_candidates)
            selection_pool = list(
                candidate_views.get("priceCandidates")
                or candidate_views.get("priceCandidatesRaw")
                or all_price_candidates
            )
            best_overall = self._select_best_price_candidate(selection_pool, state.target.expected_price)
            if best_overall is not None:
                extracted_price = float(best_overall["value"])
                extracted_source = str(best_overall.get("source", extracted_source or "unknown"))
                extracted_score = int(best_overall.get("score", extracted_score or 0))

            if extracted_price is None:
                continue

            return ExtractionResult(
                success=True,
                accepted_configuration=accepted_configuration,
                price_value=round(float(extracted_price), 2),
                currency=state.target.expected_currency,
                price_matrix=[],
                shipping_variants=[],
                raw_response_summary={
                    "strategy": state.plan.strategy.value if state.plan else None,
                    "endpoint": url,
                    "replay": "captured_trace",
                    "fallback": "none",
                    "priceSource": extracted_source,
                    "priceScore": extracted_score,
                    "responseType": response_type,
                    "endpointScore": endpoint_score_by_url.get(url),
                    "normalizationSignals": normalization_signals,
                    "normalizationAppliedRules": applied_rules,
                    "effectiveRequestedOptions": effective_options,
                    "configurationSnapshot": response_configuration_snapshot,
                    "namedPrices": named_prices,
                    **candidate_views,
                },
            )

        return None

    def _extract_price_from_dom(self, state: RunState) -> ExtractionResult | None:
        effective_options, applied_rules = self._apply_learned_normalization_rules(
            state.target.options,
            dict(state.target.bootstrap_signals.get("learned_normalization_rules") or {}),
        )
        sources = [
            ("browser_dom_post_interaction", state.target.bootstrap_signals.get("dom_price_candidates_after", [])),
            ("browser_dom_pre_interaction", state.target.bootstrap_signals.get("dom_price_candidates_before", [])),
            ("browser_dom", state.target.bootstrap_signals.get("dom_price_candidates", [])),
        ]

        for replay_label, raw_candidates in sources:
            candidates = [float(v) for v in raw_candidates if isinstance(v, (int, float)) and v > 0]
            if not candidates:
                continue

            selected = min(candidates)
            selected_from_dom = dict(state.target.bootstrap_signals.get("selected_configuration") or {})
            selected_rows = list(state.target.bootstrap_signals.get("selected_configuration_rows") or [])
            selected_config_candidates = self._extract_price_candidates_from_selected_configuration(
                selected_from_dom,
                selected_rows,
            )
            summary_line_candidates = self._extract_price_candidates_from_summary_lines(
                list(state.target.bootstrap_signals.get("price_summary_lines") or [])
            )
            all_candidates = [
                {"value": float(v), "score": 1, "source": f"dom:{replay_label}"}
                for v in candidates
            ] + selected_config_candidates + summary_line_candidates
            all_candidates.extend(
                self._derive_vat_gross_candidates(
                    all_candidates,
                    list(state.target.bootstrap_signals.get("price_summary_lines") or []),
                )
            )
            candidate_views = self._build_price_candidate_views(all_candidates)
            selection_pool = list(
                candidate_views.get("priceCandidates")
                or candidate_views.get("priceCandidatesRaw")
                or all_candidates
            )
            best_dom = self._select_best_price_candidate(selection_pool, state.target.expected_price)
            if best_dom is not None:
                selected = float(best_dom["value"])
            accepted_configuration = selected_from_dom if selected_from_dom else dict(effective_options)
            return ExtractionResult(
                success=True,
                accepted_configuration=accepted_configuration,
                price_value=round(selected, 2),
                currency=state.target.expected_currency,
                price_matrix=[],
                shipping_variants=[],
                raw_response_summary={
                    "strategy": state.plan.strategy.value if state.plan else None,
                    "endpoint": state.plan.endpoint if state.plan else None,
                    "replay": replay_label,
                    "fallback": "none",
                    "domCandidateCount": len(candidates),
                    "domSelectedGroupCount": len(selected_from_dom),
                    "normalizationAppliedRules": applied_rules,
                    "effectiveRequestedOptions": effective_options,
                    "namedPrices": {},
                    **candidate_views,
                },
            )

        return None

    def run(self, state: RunState) -> ExtractionResult:
        if state.plan is None:
            raise ValueError("Plan is required before execution")

        request_only = bool(state.target.bootstrap_signals.get("request_only", False))

        if request_only:
            replay_result, http_replay_summary = self._try_http_replay(state)
            if replay_result is not None:
                replay_result.raw_response_summary.setdefault("requestOnly", True)
                return replay_result
            return ExtractionResult(
                success=False,
                accepted_configuration=dict(state.target.options),
                price_value=None,
                currency=state.target.expected_currency,
                price_matrix=[],
                shipping_variants=[],
                raw_response_summary={
                    "strategy": state.plan.strategy.value,
                    "endpoint": state.plan.endpoint,
                    "replay": "none",
                    "fallback": "request_only",
                    "reason": "request_only_http_replay_failed",
                    "httpReplay": http_replay_summary,
                    "requestOnly": True,
                },
            )

        http_replay_summary: dict[str, Any] | None = None
        if state.plan.strategy in (Strategy.DIRECT_HTTP, Strategy.HYBRID):
            replay_result, http_replay_summary = self._try_http_replay(state)
            if replay_result is not None:
                return replay_result

        trace_result = self._extract_price_from_traces(state)
        if trace_result is not None:
            if http_replay_summary is not None:
                trace_result.raw_response_summary.setdefault("httpReplay", http_replay_summary)
            return trace_result

        dom_result = self._extract_price_from_dom(state)
        if dom_result is not None:
            if http_replay_summary is not None:
                dom_result.raw_response_summary.setdefault("httpReplay", http_replay_summary)
            return dom_result

        allow_heuristic_fallback = bool(
            state.target.bootstrap_signals.get("allow_heuristic_fallback", False)
        )
        if not allow_heuristic_fallback:
            return ExtractionResult(
                success=False,
                accepted_configuration=dict(state.target.options),
                price_value=None,
                currency=state.target.expected_currency,
                price_matrix=[],
                shipping_variants=[],
                raw_response_summary={
                    "strategy": state.plan.strategy.value,
                    "endpoint": state.plan.endpoint,
                    "replay": "none",
                    "fallback": "disabled",
                    "reason": "no_replay_price_extracted",
                },
            )

        plan = state.plan
        strict_validation = bool(plan.payload_template.get("strictValidation", False))
        effective_options, applied_rules = self._apply_learned_normalization_rules(
            state.target.options,
            dict(state.target.bootstrap_signals.get("learned_normalization_rules") or {}),
        )
        accepted_config = dict(effective_options)
        normalized = False

        if "quantity" not in accepted_config:
            accepted_config["quantity"] = 100
            normalized = True

        if strict_validation and plan.strategy != Strategy.STATIC_HTML:
            base_price = 10.5
        elif plan.strategy in (Strategy.DIRECT_HTTP, Strategy.HYBRID) and plan.endpoint:
            base_price = 12.5
        elif plan.strategy == Strategy.STATIC_HTML:
            base_price = 14.0
        else:
            base_price = 13.2

        if normalized:
            base_price += 1.0

        quantity = int(accepted_config.get("quantity", 100))
        unit_factor = max(quantity / 100.0, 1.0)
        final_price = round(base_price * unit_factor, 2)

        return ExtractionResult(
            success=True,
            accepted_configuration=accepted_config,
            price_value=final_price,
            currency=state.target.expected_currency,
            price_matrix=[
                {"quantity": 100, "price": round(base_price, 2)},
                {"quantity": 250, "price": round(base_price * 2.1, 2)},
                {"quantity": 500, "price": round(base_price * 3.8, 2)},
            ],
            shipping_variants=[
                {"delivery": "standard", "price": 0.0},
                {"delivery": "express", "price": 9.99},
            ],
            raw_response_summary={
                "strategy": plan.strategy.value,
                "endpoint": plan.endpoint,
                "normalized": normalized,
                "strictValidation": strict_validation,
                "normalizationAppliedRules": applied_rules,
                "effectiveRequestedOptions": effective_options,
                "fallback": "heuristic",
                "heuristicFallback": True,
            },
        )


class ValidationAgent:
    @staticmethod
    def _extract_first_int(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return int(value)

        text = str(value).strip()
        if not text:
            return None
        match = re.search(r"\d+", text)
        if not match:
            return None
        try:
            return int(match.group(0))
        except Exception:
            return None

    @staticmethod
    def _quantity_semantically_matches(requested: Any, accepted: Any) -> bool:
        requested_int = ValidationAgent._extract_first_int(requested)
        if requested_int is None:
            return False
        accepted_int = ValidationAgent._extract_first_int(accepted)
        if accepted_int is None:
            return False
        return requested_int == accepted_int

    @staticmethod
    def _format_semantically_matches(requested: Any, accepted: Any) -> bool:
        requested_text = str(requested or "").strip().upper()
        accepted_text = str(accepted or "").strip().upper()
        if not requested_text or not accepted_text:
            return False
        if re.fullmatch(r"A\d+", requested_text):
            return requested_text in accepted_text
        return False

    @staticmethod
    def _values_match(left: Any, right: Any) -> bool:
        if left == right:
            return True
        if left is None or right is None:
            return False
        return str(left).strip().lower() == str(right).strip().lower()

    def run(self, state: RunState) -> ValidationResult:
        if state.extraction is None or state.plan is None:
            raise ValueError("Extraction and plan are required before validation")

        mismatches: list[str] = []
        reason = None

        effective_requested_options, _applied_rules = ExecutionAgent._apply_learned_normalization_rules(
            state.target.options,
            dict(state.target.bootstrap_signals.get("learned_normalization_rules") or {}),
        )
        response_effective = state.extraction.raw_response_summary.get("effectiveRequestedOptions")
        if isinstance(response_effective, dict) and response_effective:
            effective_requested_options = dict(response_effective)

        if not state.extraction.success or state.extraction.price_value is None:
            mismatches.append("price_not_extracted")
            reason = "price_not_extracted"

        request_only = bool(state.target.bootstrap_signals.get("request_only", False))
        if request_only:
            replay_mode = str(state.extraction.raw_response_summary.get("replay") or "none").lower()
            fallback_mode = str(state.extraction.raw_response_summary.get("fallback") or "none").lower()
            if replay_mode not in {"http", "adapter"}:
                mismatches.append(f"request_only_replay_mismatch expected=http_or_adapter actual={replay_mode}")
                if reason == "price_not_extracted":
                    reason = "request_only_http_replay_failed"
                else:
                    reason = reason or "request_only_violation"
            if fallback_mode not in {"none", ""}:
                mismatches.append(f"request_only_fallback_used fallback={fallback_mode}")
                reason = reason or "request_only_violation"

            template_applied = state.extraction.raw_response_summary.get("requestTemplateApplied")
            if isinstance(template_applied, dict) and str(template_applied.get("kind") or "") == "saxoprint_payload":
                issues = template_applied.get("synthesisIssues")
                if isinstance(issues, list) and issues:
                    keys = sorted(
                        {
                            str(row.get("key", "")).strip()
                            for row in issues
                            if isinstance(row, dict) and str(row.get("key", "")).strip()
                        }
                    )
                    detail = ",".join(keys[:10]) if keys else "unknown"
                    mismatches.append(
                        f"request_only_option_synthesis_failed count={len(issues)} keys={detail}"
                    )
                    reason = reason or "request_only_option_synthesis_failed"

        expected = state.target.expected_price
        actual = state.extraction.price_value
        tolerance = max(0.0, float(state.target.expected_price_tolerance or 0.01))
        if expected is not None and actual is None:
            mismatches.append(f"price_mismatch expected={expected} actual=None tolerance={tolerance}")
            reason = reason or "payload_or_normalization_mismatch"
        elif expected is not None and actual is not None:
            if abs(expected - actual) > tolerance:
                mismatches.append(
                    f"price_mismatch expected={expected} actual={actual} tolerance={tolerance}"
                )
                reason = "payload_or_normalization_mismatch"

        requested_qty = effective_requested_options.get("quantity")
        accepted_qty = state.extraction.accepted_configuration.get("quantity")
        if requested_qty is not None and not (
            self._values_match(requested_qty, accepted_qty)
            or self._quantity_semantically_matches(requested_qty, accepted_qty)
        ):
            mismatches.append(
                f"quantity_normalized requested={requested_qty} accepted={accepted_qty}"
            )
            reason = reason or "server_normalized_configuration"

        for key, requested_value in effective_requested_options.items():
            accepted_value = state.extraction.accepted_configuration.get(key)
            key_lower = str(key).strip().lower()
            matches = self._values_match(requested_value, accepted_value)
            if not matches and key_lower == "quantity":
                matches = self._quantity_semantically_matches(requested_value, accepted_value)
            if not matches and key_lower == "format":
                matches = self._format_semantically_matches(requested_value, accepted_value)
            if not matches:
                mismatches.append(
                    f"config_normalized key={key} requested={requested_value} accepted={accepted_value}"
                )
                reason = reason or "server_normalized_configuration"

        normalization_signals = state.extraction.raw_response_summary.get("normalizationSignals", [])
        for signal in normalization_signals:
            if signal not in mismatches:
                mismatches.append(str(signal))
                reason = reason or "server_normalized_configuration"

        require_matched_options = bool(
            state.target.bootstrap_signals.get("require_matched_options", False)
        )
        if require_matched_options:
            option_prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
            if option_prevalidation.get("available"):
                unmatched_rows = list(option_prevalidation.get("unmatched") or [])
            else:
                option_application = dict(state.target.bootstrap_signals.get("option_application") or {})
                unmatched_rows = list(option_application.get("unmatched") or [])
            if unmatched_rows:
                unmatched_keys = sorted(
                    {
                        str(row.get("key", "")).strip()
                        for row in unmatched_rows
                        if str(row.get("key", "")).strip()
                    }
                )
                detail = ",".join(unmatched_keys) if unmatched_keys else "unknown"
                mismatches.append(
                    f"required_options_unmatched count={len(unmatched_rows)} keys={detail}"
                )
                reason = reason or "required_options_unmatched"

        return ValidationResult(is_valid=not mismatches, mismatches=mismatches, inferred_failure_reason=reason)


class RepairAgent:
    def run(self, state: RunState) -> StrategyPlan:
        if state.plan is None or state.validation is None:
            raise ValueError("Plan and validation are required before repair")

        reason = state.validation.inferred_failure_reason or "unknown"
        current = state.plan

        if reason == "payload_or_normalization_mismatch":
            updated_payload = dict(current.payload_template)
            updated_payload["strictValidation"] = True
            return StrategyPlan(
                strategy=Strategy.HYBRID if current.strategy != Strategy.HYBRID else current.strategy,
                endpoint=current.endpoint,
                payload_template=updated_payload,
                confidence=min(current.confidence + 0.1, 0.95),
                notes="Enabled strict validation and escalated strategy where possible",
            )

        if reason == "server_normalized_configuration":
            updated_payload = dict(current.payload_template)
            options = dict(updated_payload.get("options", {}))
            options.setdefault("quantity", state.target.options.get("quantity", 100))
            updated_payload["options"] = options
            return StrategyPlan(
                strategy=current.strategy,
                endpoint=current.endpoint,
                payload_template=updated_payload,
                confidence=min(current.confidence + 0.05, 0.95),
                notes="Injected missing dependent option fields",
            )

        return StrategyPlan(
            strategy=Strategy.BROWSER_AUTOMATION,
            endpoint=current.endpoint,
            payload_template=current.payload_template,
            confidence=0.4,
            notes="Fallback to browser automation due to unknown failure",
        )


def state_snapshot(state: RunState) -> dict:
    payload = asdict(state)
    if state.plan is not None:
        payload["plan"]["strategy"] = state.plan.strategy.value
    if state.feasibility is not None:
        payload["feasibility"]["complexity"] = state.feasibility.complexity.value
        payload["feasibility"]["recommended_strategy"] = state.feasibility.recommended_strategy.value
    return payload
