from __future__ import annotations

from typing import Any, Mapping


def extract_option_catalog_and_templates(
    bootstrap_artifacts: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = dict(bootstrap_artifacts or {})
    option_catalog = list(source.get("optionCatalog") or [])
    request_templates = dict(source.get("requestTemplates") or {})
    return option_catalog, request_templates


def set_request_template_debug(
    request_templates: dict[str, Any],
    *,
    key: str,
    payload: Any,
) -> None:
    if not isinstance(request_templates, dict):
        return
    request_templates[key] = payload


def set_dropdown_maps(
    request_templates: dict[str, Any],
    dropdown_maps: list[dict[str, Any]],
    *,
    limit: int,
) -> None:
    if not isinstance(request_templates, dict):
        return
    request_templates["dropdownMaps"] = list(dropdown_maps[:limit])
