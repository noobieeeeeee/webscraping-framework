from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

from ..agents import DiscoveryAgent
from ..knowledge_store import KnowledgeStore
from ..models import RunState, TargetInput


@dataclass(frozen=True)
class OnboardingProposal:
    site_name: str
    product_url: str
    run_dir: str
    has_request_templates: bool
    option_catalog_groups: int
    top_endpoints: list[dict[str, Any]]
    requires_session: bool
    token_indicators: list[str]
    blockers: list[str]
    replay_templates: list[dict[str, object]]
    option_normalizations: dict[str, dict[str, str]]
    trace_template_hints: list[dict[str, Any]]
    suggested_next_steps: list[str]
    adapter_stub: str


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


def _guess_site_name(spec: dict[str, Any], summary: dict[str, Any], bootstrap: dict[str, Any]) -> str:
    for candidate in [
        summary.get("site"),
        spec.get("site_name"),
        bootstrap.get("site_name"),
    ]:
        text = str(candidate or "").strip()
        if text:
            return text
    return "unknown"


def _guess_product_url(spec: dict[str, Any], summary: dict[str, Any], bootstrap: dict[str, Any]) -> str:
    for candidate in [
        summary.get("url"),
        summary.get("product_url"),
        spec.get("url"),
        bootstrap.get("product_url"),
    ]:
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _class_name_for_site(site_name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", str(site_name or "").strip())
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return "NewSite"
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _render_adapter_stub(site_name: str) -> str:
    class_name = _class_name_for_site(site_name)
    site_hint = str(site_name or "").strip().lower() or "example"

    return "\n".join(
        [
            "from __future__ import annotations",
            "",
            "import json",
            "from dataclasses import dataclass",
            "from typing import Any",
            "",
            "from .models import RunState",
            "",
            "",
            "@dataclass(frozen=True)",
            f"class {class_name}SiteAdapter:",
            "    def supports(self, state: RunState) -> bool:",
            f"        return '{site_hint}' in str(state.target.site_name or '').lower()",
            "",
            "    def build_http_replay_candidates(",
            "        self,",
            "        state: RunState,",
            "        *,",
            "        effective_options: dict[str, Any],",
            "        raw_requested_options: dict[str, Any],",
            "    ) -> list[dict[str, Any]]:",
            "        # TODO: pick the best pricing/config endpoint trace as a template",
            "        # TODO: synthesize a replay payload from captured request templates/option catalog",
            "        # TODO: return a candidate dict with keys: url, method, source, body_text, trace_override, request_template_applied",
            "        return []",
            "",
            "",
            "# Next steps:",
            "# 1) Paste this into src/price_extractor/site_adapters.py",
            "# 2) Add it to get_default_site_adapters()",
            "# 3) Add a regression test with offline artifacts",
        ]
    )


def _safe_json_loads(value: Any) -> dict[str, Any] | list[Any] | None:
    if not isinstance(value, str):
        return None
    text = str(value).strip()
    if not text or text[0] not in "[{":
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed
    return None


def _collect_json_paths(payload: Any, *, max_paths: int = 80) -> list[str]:
    paths: list[str] = []

    def walk(value: Any, prefix: str) -> None:
        if len(paths) >= max_paths:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                next_path = f"{prefix}.{key_text}" if prefix else key_text
                paths.append(next_path)
                walk(child, next_path)
                if len(paths) >= max_paths:
                    return
            return
        if isinstance(value, list):
            for index, child in enumerate(value[:3]):
                next_path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                paths.append(next_path)
                walk(child, next_path)
                if len(paths) >= max_paths:
                    return

    walk(payload, "")
    return paths[:max_paths]


def _extract_form_keys(form_body: str, *, max_keys: int = 24) -> list[str]:
    body = str(form_body or "")
    if not body:
        return []

    keys: list[str] = []
    for part in body.split("&"):
        if "=" not in part:
            continue
        key = unquote_plus(part.split("=", 1)[0]).strip()
        if not key or key in keys:
            continue
        keys.append(key)
        if len(keys) >= max_keys:
            break
    return keys


OPTION_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "quantity": ("quantity", "qty", "auflage", "menge", "stueck", "stuck", "print_runs", "printruns"),
    "format": ("format", "size", "groesse", "grosse", "dimension"),
    "material": ("material", "papier", "paper"),
    "pages": ("pages", "seiten", "seite"),
    "color": ("color", "colour", "farben", "farbe", "farbig", "farbigkeit"),
    "binding": ("binding", "bindung", "verarbeitung"),
    "finishing": ("finishing", "finish", "veredelung"),
    "orientation": ("orientation", "ausrichtung", "aspect_ratio", "aspectratio"),
}


def _canonical_option_key(token: str) -> str | None:
    lowered = str(token or "").strip().lower()
    if not lowered:
        return None
    for canonical, aliases in OPTION_KEY_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                return canonical
    return None


def _infer_option_key_from_path(path: str) -> str | None:
    return _canonical_option_key(path)


def _build_trace_template_hints(
    top_endpoints: list[dict[str, Any]],
    traces: list[dict[str, Any]],
    *,
    max_hints: int = 4,
) -> list[dict[str, Any]]:
    trace_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in traces:
        if not isinstance(row, dict):
            continue
        method = str(row.get("method") or "").strip().upper()
        url = str(row.get("url") or "").strip()
        if not method or not url:
            continue
        trace_by_key.setdefault((method, url), row)

    hints: list[dict[str, Any]] = []
    for endpoint in list(top_endpoints or [])[: max(1, int(max_hints))]:
        if not isinstance(endpoint, dict):
            continue

        method = str(endpoint.get("method") or "").strip().upper()
        url = str(endpoint.get("url") or "").strip()
        if not method or not url:
            continue

        trace = trace_by_key.get((method, url), {})
        if not isinstance(trace, dict) or not trace:
            continue

        body_text = str(trace.get("post_data") or trace.get("post_data_preview") or "")
        json_payload = _safe_json_loads(body_text)

        body_kind = "raw"
        request_keys_sample: list[str] = []
        candidate_mutations: list[dict[str, str]] = []

        if isinstance(json_payload, (dict, list)):
            body_kind = "json"
            json_paths = _collect_json_paths(json_payload, max_paths=90)
            request_keys_sample = json_paths[:24]

            seen_paths: set[str] = set()
            for path in json_paths:
                option_key = _infer_option_key_from_path(path)
                if not option_key or path in seen_paths:
                    continue
                seen_paths.add(path)
                candidate_mutations.append(
                    {
                        "option_key": option_key,
                        "path": path,
                        "reason": "path_token_match",
                    }
                )

            if isinstance(json_payload, dict):
                properties = json_payload.get("properties")
                if isinstance(properties, list):
                    canonical_seen: set[str] = set()
                    for row in properties:
                        if not isinstance(row, dict):
                            continue
                        name = str(row.get("name") or "").strip()
                        if not name:
                            continue
                        canonical = _canonical_option_key(name)
                        if canonical is None or canonical in canonical_seen:
                            continue
                        semantic_path = f"properties[name={name}].id"
                        if semantic_path in seen_paths:
                            continue
                        seen_paths.add(semantic_path)
                        canonical_seen.add(canonical)
                        candidate_mutations.append(
                            {
                                "option_key": canonical,
                                "path": semantic_path,
                                "reason": "properties_name_id_pattern",
                            }
                        )
        elif "=" in body_text and "&" in body_text:
            body_kind = "form"
            form_keys = _extract_form_keys(body_text, max_keys=24)
            request_keys_sample = form_keys
            for key in form_keys:
                option_key = _infer_option_key_from_path(key)
                if not option_key:
                    continue
                candidate_mutations.append(
                    {
                        "option_key": option_key,
                        "path": key,
                        "reason": "form_key_match",
                    }
                )

        hints.append(
            {
                "endpoint_url": url,
                "method": method,
                "body_kind": body_kind,
                "request_keys_sample": request_keys_sample,
                "candidate_mutations": candidate_mutations[:16],
            }
        )

    return hints


def build_onboarding_proposal(
    *,
    run_dir: str,
    knowledge_db: str = ".data/knowledge.db",
    top_n: int = 10,
) -> OnboardingProposal:
    run_path = Path(run_dir)
    if not run_path.exists() or not run_path.is_dir():
        raise SystemExit(f"run dir not found: {run_dir}")

    spec = _safe_dict(_load_json(run_path / "spec.json", default={}))
    summary = _safe_dict(_load_json(run_path / "summary.json", default={}))
    bootstrap = _safe_dict(_load_json(run_path / "bootstrap.json", default={}))

    traces = _safe_list(_load_json(run_path / "network_traces.json", default=[]))
    option_catalog = _safe_list(_load_json(run_path / "option_catalog.json", default=[]))

    site_name = _guess_site_name(spec, summary, bootstrap)
    product_url = _guess_product_url(spec, summary, bootstrap)

    requested_options = _safe_dict(spec.get("options"))

    # Mirror how CLI constructs state:
    bootstrap_signals = dict(bootstrap)
    bootstrap_signals.pop("network_traces", None)
    bootstrap_signals.pop("option_catalog", None)

    target = TargetInput(
        site_name=site_name,
        product_url=product_url,
        product_type=str(spec.get("product_type") or "unknown"),
        options=requested_options,
        network_traces=[row for row in traces if isinstance(row, dict)],
        bootstrap_signals=bootstrap_signals,
        observed_requests=[str(url) for url in _safe_list(bootstrap.get("observed_requests")) if str(url)],
    )

    state = RunState(target=target)
    obs = DiscoveryAgent().run(state)

    top_endpoints = []
    for row in list(obs.endpoint_rankings or [])[: max(1, int(top_n))]:
        if not isinstance(row, dict):
            continue
        top_endpoints.append(
            {
                "score": int(row.get("score", 0) or 0),
                "role": str(row.get("role") or ""),
                "confidence": float(row.get("confidence", 0.0) or 0.0),
                "method": str(row.get("method") or ""),
                "status": int(row.get("status", 0) or 0),
                "url": str(row.get("url") or ""),
            }
        )

    trace_template_hints = _build_trace_template_hints(
        top_endpoints,
        [row for row in traces if isinstance(row, dict)],
        max_hints=4,
    )

    store = KnowledgeStore(knowledge_db)
    try:
        replay_templates = store.get_site_replay_templates(site_name)
        option_normalizations = store.get_site_option_normalizations(site_name, trusted_only=True)
    finally:
        store.close()

    has_request_templates = bool(_safe_dict(bootstrap.get("request_templates")))
    option_catalog_groups = 0
    if option_catalog:
        # option_catalog.json stores groups as a list
        option_catalog_groups = len([row for row in option_catalog if isinstance(row, dict)])

    suggested_next_steps: list[str] = []
    if obs.requires_session:
        suggested_next_steps.append(
            "Confirm cookie/session headers needed for HTTP replay (avoid HTML responses / redirects)."
        )
    if obs.token_indicators:
        suggested_next_steps.append(
            "Capture/forward CSRF/Bearer tokens consistently (bootstrap + replay injection)."
        )
    suggested_next_steps.append(
        "Pick the top PRICING/QUANTITY_MATRIX endpoint and ensure replay is deterministic (request-only mode)."
    )
    if not has_request_templates:
        suggested_next_steps.append(
            "Improve bootstrap request-template capture so payload synthesis can be template-driven (no hard-coded IDs)."
        )
    if option_catalog_groups <= 0:
        suggested_next_steps.append(
            "Improve option catalog extraction so requested options can be mapped to backend codes."
        )
    suggested_next_steps.append(
        "Implement a new SiteAdapter that synthesizes replay candidates for this site/product type."
    )
    if trace_template_hints:
        suggested_next_steps.append(
            "Use trace_template_hints.candidate_mutations to drive deterministic payload rewrite logic before adding fallback heuristics."
        )
    suggested_next_steps.append(
        "Add an offline regression fixture from this run directory (network_traces + option_catalog + templates)."
    )

    adapter_stub = _render_adapter_stub(site_name)

    return OnboardingProposal(
        site_name=site_name,
        product_url=product_url,
        run_dir=str(run_path),
        has_request_templates=has_request_templates,
        option_catalog_groups=option_catalog_groups,
        top_endpoints=top_endpoints,
        requires_session=bool(obs.requires_session),
        token_indicators=list(obs.token_indicators or []),
        blockers=list(obs.blockers or []),
        replay_templates=replay_templates,
        option_normalizations=option_normalizations,
        trace_template_hints=trace_template_hints,
        suggested_next_steps=suggested_next_steps,
        adapter_stub=adapter_stub,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an onboarding proposal from an existing run directory. "
            "This is default-off and does not affect deterministic extraction."
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
        "--out",
        help="Optional path to write proposal JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    proposal = build_onboarding_proposal(
        run_dir=str(args.run_dir),
        knowledge_db=str(args.knowledge_db),
        top_n=int(args.top_n),
    )
    payload = asdict(proposal)
    rendered = json.dumps(payload, indent=2, ensure_ascii=True)

    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")

    print(rendered)


if __name__ == "__main__":
    main()
