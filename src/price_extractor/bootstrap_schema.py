from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .browser_bootstrap import BootstrapResult


class BootstrapArtifacts(TypedDict, total=False):
    site_name: str
    observed_requests: list[str]
    network_traces: list[dict[str, Any]]
    option_groups: list[dict[str, Any]]
    option_catalog: list[dict[str, Any]]
    option_dependencies: list[dict[str, Any]]
    dependency_probe: dict[str, Any]
    option_application: dict[str, Any]
    option_prevalidation: dict[str, Any]
    option_url_variants: list[dict[str, Any]]
    request_templates: dict[str, Any]
    quantity_signal: dict[str, Any]
    dom_price_candidates_before: list[float]
    dom_price_candidates_after: list[float]
    dom_price_candidates: list[float]
    selected_configuration: dict[str, Any]
    selected_configuration_rows: list[dict[str, Any]]
    price_summary_lines: list[str]
    consent_state: dict[str, Any]
    token_indicators: list[str]
    cookie_names: list[str]
    cookies: dict[str, str]
    requires_session: bool
    anti_bot_suspected: bool
    warnings: list[str]
    recon_cache: dict[str, Any]
    recon_refresh_reason: str | None
    drift_report: dict[str, Any]
    allow_heuristic_fallback: bool
    require_matched_options: bool
    request_only: bool
    http_runtime: dict[str, Any]


def normalize_bootstrap_artifacts(raw: Mapping[str, Any] | None) -> BootstrapArtifacts:
    source = dict(raw or {})
    normalized: BootstrapArtifacts = {
        "site_name": str(source.get("site_name") or "unknown-site"),
        "observed_requests": list(source.get("observed_requests") or []),
        "network_traces": list(source.get("network_traces") or []),
        "option_groups": list(source.get("option_groups") or []),
        "option_catalog": list(source.get("option_catalog") or []),
        "option_dependencies": list(source.get("option_dependencies") or []),
        "dependency_probe": dict(source.get("dependency_probe") or {}),
        "option_application": dict(source.get("option_application") or {}),
        "option_prevalidation": dict(source.get("option_prevalidation") or {}),
        "option_url_variants": list(source.get("option_url_variants") or []),
        "request_templates": dict(source.get("request_templates") or {}),
        "quantity_signal": dict(source.get("quantity_signal") or {}),
        "dom_price_candidates_before": list(source.get("dom_price_candidates_before") or []),
        "dom_price_candidates_after": list(source.get("dom_price_candidates_after") or []),
        "dom_price_candidates": list(source.get("dom_price_candidates") or []),
        "selected_configuration": dict(source.get("selected_configuration") or {}),
        "selected_configuration_rows": list(source.get("selected_configuration_rows") or []),
        "price_summary_lines": list(source.get("price_summary_lines") or []),
        "consent_state": dict(source.get("consent_state") or {}),
        "token_indicators": list(source.get("token_indicators") or []),
        "cookie_names": list(source.get("cookie_names") or []),
        "cookies": dict(source.get("cookies") or {}),
        "requires_session": bool(source.get("requires_session", False)),
        "anti_bot_suspected": bool(source.get("anti_bot_suspected", False)),
        "warnings": list(source.get("warnings") or []),
        "recon_cache": dict(source.get("recon_cache") or {}),
        "recon_refresh_reason": source.get("recon_refresh_reason"),
        "drift_report": dict(source.get("drift_report") or {}),
    }

    # Preserve unknown keys for forward compatibility with cached snapshots.
    for key, value in source.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def bootstrap_result_to_artifacts(bootstrap: "BootstrapResult") -> BootstrapArtifacts:
    return normalize_bootstrap_artifacts(
        {
            "site_name": bootstrap.site_name,
            "observed_requests": list(bootstrap.observed_requests),
            "network_traces": list(bootstrap.network_traces),
            "option_groups": list(bootstrap.option_groups),
            "option_catalog": list(bootstrap.option_catalog),
            "option_dependencies": list(bootstrap.option_dependencies),
            "dependency_probe": dict(bootstrap.dependency_probe),
            "option_application": dict(bootstrap.option_application),
            "option_url_variants": list(bootstrap.option_url_variants),
            "request_templates": dict(bootstrap.request_templates),
            "quantity_signal": dict(bootstrap.quantity_signal),
            "dom_price_candidates_before": list(bootstrap.dom_price_candidates_before),
            "dom_price_candidates_after": list(bootstrap.dom_price_candidates_after),
            "dom_price_candidates": list(bootstrap.dom_price_candidates),
            "selected_configuration": dict(bootstrap.selected_configuration),
            "selected_configuration_rows": list(bootstrap.selected_configuration_rows),
            "price_summary_lines": list(bootstrap.price_summary_lines),
            "consent_state": dict(bootstrap.consent_state),
            "token_indicators": list(bootstrap.token_indicators),
            "cookie_names": list(bootstrap.cookie_names),
            "cookies": dict(bootstrap.cookies),
            "requires_session": bool(bootstrap.requires_session),
            "anti_bot_suspected": bool(bootstrap.anti_bot_suspected),
            "warnings": list(bootstrap.warnings),
        }
    )
