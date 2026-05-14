from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar, Protocol
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlencode, urlsplit, urlunsplit

from .models import RunState


class SiteAdapter(Protocol):
    def supports(self, state: RunState) -> bool:
        ...


def _looks_like_site_adapter(value: Any) -> bool:
    if not isinstance(value, type):
        return False
    supports = getattr(value, "supports", None)
    build_candidates = getattr(value, "build_http_replay_candidates", None)
    return callable(supports) and callable(build_candidates)


def load_generated_site_adapters(
    adapters_dir: str | Path | None,
    *,
    allow_untrusted: bool,
) -> tuple[list[SiteAdapter], dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "loadedCount": 0,
        "fileCount": 0,
        "reason": "disabled",
        "warnings": [],
        "errors": [],
    }
    if not allow_untrusted:
        return [], diagnostics

    if adapters_dir is None:
        diagnostics["reason"] = "missing_dir"
        return [], diagnostics

    path = Path(adapters_dir)
    if not path.exists() or not path.is_dir():
        diagnostics["reason"] = "missing_dir"
        return [], diagnostics

    adapters: list[SiteAdapter] = []
    diagnostics["reason"] = "ok"
    files = sorted(path.glob("*.py"))
    diagnostics["fileCount"] = len(files)

    for index, file_path in enumerate(files):
        module_name = f"generated_site_adapter_{index}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                diagnostics["warnings"].append({"file": str(file_path), "kind": "load_failed"})
                continue
            module = importlib.util.module_from_spec(spec)
            loader = spec.loader
            if isinstance(module, ModuleType):
                loader.exec_module(module)
            else:
                diagnostics["warnings"].append({"file": str(file_path), "kind": "invalid_module"})
                continue
        except Exception as exc:
            diagnostics["warnings"].append({"file": str(file_path), "kind": "import_error", "error": str(exc)})
            continue

        discovered = 0
        for name in dir(module):
            value = getattr(module, name, None)
            if not _looks_like_site_adapter(value):
                continue
            try:
                adapters.append(value())
                discovered += 1
            except Exception as exc:
                diagnostics["warnings"].append(
                    {"file": str(file_path), "kind": "init_error", "class": str(name), "error": str(exc)}
                )

        if discovered == 0:
            diagnostics["warnings"].append({"file": str(file_path), "kind": "no_adapter_found"})

    diagnostics["loadedCount"] = len(adapters)
    return adapters, diagnostics

    def build_http_replay_candidates(
        self,
        state: RunState,
        *,
        effective_options: dict[str, Any],
        raw_requested_options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class WirMachenDruckSiteAdapter:
    def supports(self, state: RunState) -> bool:
        site_name = str(state.target.site_name or "").lower()
        target_url = str(state.target.product_url or "").lower()
        return ("wir-machen-druck" in site_name) or ("wir-machen-druck" in target_url)

    @staticmethod
    def _find_template_trace(state: RunState, path_token: str) -> dict[str, Any] | None:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for trace in state.target.network_traces:
            url = str(trace.get("url", "")).strip().lower()
            method = str(trace.get("method", "GET")).upper()
            if "wir-machen-druck.de" not in url:
                continue
            if path_token not in url:
                continue
            if method != "POST":
                continue

            score = 0
            if str(trace.get("resource_type", "")).lower() in {"xhr", "fetch"}:
                score += 6
            if "application/json" in str(trace.get("response_content_type", "")).lower():
                score += 3
            if str(trace.get("post_data") or "").strip():
                score += 2
            if str(trace.get("response_body_preview") or "").strip():
                score += 1
            candidates.append((score, trace))

        if not candidates:
            return None
        return max(candidates, key=lambda row: row[0])[1]

    @staticmethod
    def _infer_price_scale_id(preview_text: str, quantity: int) -> str | None:
        if not preview_text or quantity <= 0:
            return None

        qty_token = re.escape(str(quantity))
        patterns = [
            re.compile(rf'"id"\s*:\s*(\d+)\s*,\s*"wert"\s*:\s*{qty_token}\b'),
            re.compile(rf'"wert"\s*:\s*{qty_token}\b[^\}}]{{0,200}}?"id"\s*:\s*(\d+)'),
        ]
        for pattern in patterns:
            match = pattern.search(preview_text)
            if match:
                return str(match.group(1))
        return None

    def build_http_replay_candidates(
        self,
        state: RunState,
        *,
        effective_options: dict[str, Any],
        raw_requested_options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        qty_value = raw_requested_options.get("quantity", effective_options.get("quantity"))
        try:
            qty_int = int(str(qty_value).strip())
        except Exception:
            return []
        if qty_int <= 0:
            return []

        price_trace = self._find_template_trace(state, "/wmdrest/article/get-price")
        if price_trace is None:
            return []

        raw_body = str(price_trace.get("post_data") or "").strip()
        if not raw_body:
            return []

        try:
            payload = json.loads(raw_body)
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []

        sorten_trace = self._find_template_trace(state, "/wmdrest/article/get-sorten-auflage")
        preview = str((sorten_trace or {}).get("response_body_preview") or "")
        scale_id = self._infer_price_scale_id(preview, qty_int)

        candidates: list[dict[str, Any]] = []
        endpoint = str(price_trace.get("url") or state.target.product_url)

        if scale_id:
            scaled_payload = dict(payload)
            scaled_payload["isIndividualQuantity"] = False
            scaled_payload["quantity"] = str(qty_int)
            scaled_payload["priceScaleId"] = str(scale_id)
            candidates.append(
                {
                    "url": endpoint,
                    "method": "POST",
                    "source": "wir_machen_druck_price_scale",
                    "body_text": json.dumps(scaled_payload, separators=(",", ":"), ensure_ascii=False),
                    "trace_override": price_trace,
                    "request_template_applied": {
                        "kind": "wir_machen_druck_price_scale",
                        "quantity": qty_int,
                        "priceScaleId": str(scale_id),
                    },
                }
            )

        manual_payload = dict(payload)
        manual_payload["isIndividualQuantity"] = True
        manual_payload["quantity"] = str(qty_int)
        candidates.append(
            {
                "url": endpoint,
                "method": "POST",
                "source": "wir_machen_druck_quantity_manual",
                "body_text": json.dumps(manual_payload, separators=(",", ":"), ensure_ascii=False),
                "trace_override": price_trace,
                "request_template_applied": {
                    "kind": "wir_machen_druck_quantity_manual",
                    "quantity": qty_int,
                },
            }
        )

        return candidates


@dataclass(frozen=True)
class SaxoprintSiteAdapter:
    def supports(self, state: RunState) -> bool:
        site_name = str(state.target.site_name or "").lower()
        target_url = str(state.target.product_url or "").lower()
        return ("saxoprint" in site_name) or ("saxoprint" in target_url)

    @staticmethod
    def _find_template_trace(state: RunState, path_token: str) -> dict[str, Any] | None:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for trace in state.target.network_traces:
            url = str(trace.get("url", "")).strip()
            lowered = url.lower()
            method = str(trace.get("method", "GET")).upper()
            if "api.saxoprint.de" not in lowered:
                continue
            if path_token not in lowered:
                continue
            if method != "POST":
                continue

            score = 0
            if str(trace.get("resource_type", "")).lower() in {"xhr", "fetch"}:
                score += 6
            if "application/json" in str(trace.get("request_content_type", "")).lower():
                score += 3
            if "application/json" in str(trace.get("response_content_type", "")).lower():
                score += 2
            if str(trace.get("post_data") or "").strip():
                score += 2
            if str(trace.get("response_body_preview") or "").strip():
                score += 1
            candidates.append((score, trace))

        if not candidates:
            return None
        return max(candidates, key=lambda row: row[0])[1]

    @staticmethod
    def _parse_quantity_value_options(preview_text: str, quantity_property_id: int) -> list[int]:
        text = str(preview_text or "")
        if not text:
            return []

        # Fast path: preview is valid JSON.
        try:
            payload = json.loads(text)
        except Exception:
            payload = None

        if isinstance(payload, dict):
            options = payload.get("propertyOptions")
            if isinstance(options, list):
                for opt in options:
                    if not isinstance(opt, dict):
                        continue
                    if int(opt.get("id") or 0) != int(quantity_property_id):
                        continue
                    values = opt.get("propertyValueOptions")
                    if not isinstance(values, list):
                        continue
                    ids: list[int] = []
                    for row in values:
                        if not isinstance(row, dict):
                            continue
                        try:
                            ids.append(int(row.get("id")))
                        except Exception:
                            continue
                    return sorted({v for v in ids if v > 0})

        # Fallback: try to extract the propertyValueOptions array segment for the given property.
        lowered = text.lower()
        anchor = f'"id":{int(quantity_property_id)}'
        start = lowered.find(anchor)
        if start < 0:
            return []
        window = text[start : start + 120000]

        array_key = '"propertyvalueoptions"'
        array_pos = window.lower().find(array_key)
        if array_pos < 0:
            return []

        bracket_pos = window.find("[", array_pos)
        if bracket_pos < 0:
            return []
        depth = 0
        end_pos = -1
        for idx in range(bracket_pos, len(window)):
            ch = window[idx]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end_pos = idx
                    break
        if end_pos < 0:
            return []
        array_text = window[bracket_pos : end_pos + 1]
        ids = [int(m.group(1)) for m in re.finditer(r'"id"\s*:\s*(\d+)', array_text)]
        return sorted({v for v in ids if v > 0})

    @staticmethod
    def _compute_print_runs(allowed: list[int], requested_qty: int) -> list[int] | None:
        if requested_qty <= 0:
            return None
        if not allowed:
            return None
        allowed_sorted = sorted({v for v in allowed if v > 0})
        if requested_qty not in allowed_sorted:
            return None
        idx = allowed_sorted.index(requested_qty)
        return allowed_sorted[idx : idx + 4]

    @staticmethod
    def _extract_property_overrides(raw_requested_options: dict[str, Any]) -> dict[int, int]:
        overrides: dict[int, int] = {}
        for raw_key, raw_value in (raw_requested_options or {}).items():
            key = str(raw_key or "").strip().lower().replace("-", "_")
            match = re.fullmatch(r"(?:property|prop|p)_(\d+)", key) or re.fullmatch(r"p(\d+)", key)
            if not match:
                continue
            try:
                prop_id = int(match.group(1))
                value_id = int(raw_value)
            except Exception:
                continue
            if prop_id > 0 and value_id > 0:
                overrides[prop_id] = value_id
        return overrides

    @staticmethod
    def _extract_sideload_overrides(state: RunState) -> list[dict[str, Any]]:
        """Read prevalidation `matched[*].sideloadResolution` rows produced by
        `cli._augment_with_sideload` and return one candidate record per
        resolved row. Each record carries `propertyId` / `backendId` (both
        coerced to int) plus diagnostics for the rollup. Caller decides
        precedence against explicit / label overrides.
        """
        records: list[dict[str, Any]] = []
        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        for row in list(prevalidation.get("matched") or []):
            if not isinstance(row, dict):
                continue
            sr = dict(row.get("sideloadResolution") or {})
            if not sr:
                continue
            try:
                prop_id = int(str(sr.get("propertyId") or "").strip())
                backend_id = int(str(sr.get("backendId") or "").strip())
            except (ValueError, TypeError):
                continue
            if prop_id <= 0 or backend_id <= 0:
                continue
            records.append(
                {
                    "key": str(row.get("key") or ""),
                    "propertyId": prop_id,
                    "backendId": backend_id,
                    "matchedLabel": sr.get("matchedLabel"),
                    "confidence": sr.get("confidence"),
                    "productScope": list(sr.get("productScope") or ["*"]),
                    "sourceLabel": sr.get("sourceLabel"),
                    "canonicalKey": str(row.get("key") or ""),
                    "via": "sideload",
                }
            )
        return records

    @staticmethod
    def _extract_property_label_overrides(raw_requested_options: dict[str, Any]) -> dict[int, str]:
        overrides: dict[int, str] = {}
        for raw_key, raw_value in (raw_requested_options or {}).items():
            key = str(raw_key or "").strip().lower().replace("-", "_")
            match = re.fullmatch(r"(?:property|prop|p)_(\d+)", key) or re.fullmatch(r"p(\d+)", key)
            if not match:
                continue
            try:
                prop_id = int(match.group(1))
            except Exception:
                continue
            if prop_id <= 0:
                continue

            if raw_value is None:
                continue

            # If it's numeric, it is handled by _extract_property_overrides.
            try:
                int(str(raw_value).strip())
                continue
            except Exception:
                pass

            label = str(raw_value).strip()
            if label:
                overrides[prop_id] = label
        return overrides

    @staticmethod
    def _normalize_loose(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = html.unescape(text)
        text = unicodedata.normalize("NFKD", text)
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        # Common shorthand: people often type "FSCr" to mean "FSC®".
        # We normalize both to the same token so matching is stable.
        text = re.sub(r"\bfsc\s*r\b", "fsc", text)
        text = re.sub(r"\bfscr\b", "fsc", text)
        text = text.replace("&", " and ").replace("²", "2")
        # Common user shorthand (e.g. "130gsm") should match labels like "130 g/m2".
        text = text.replace("gsm", " g m2 ")
        cleaned = "".join(ch if ch.isalnum() else " " for ch in text)
        return " ".join(cleaned.split())

    @classmethod
    def _normalize_compact(cls, value: Any) -> str:
        return cls._normalize_loose(value).replace(" ", "")

    @staticmethod
    def _extract_dropdown_maps(state: RunState) -> list[dict[str, Any]]:
        request_templates = dict(state.target.bootstrap_signals.get("request_templates") or {})
        raw = request_templates.get("dropdownMaps")
        if not isinstance(raw, list):
            return []
        dropdowns: list[dict[str, Any]] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            trigger = str(row.get("triggerText") or row.get("triggerLabel") or "").strip()
            context_hint = str(row.get("contextHint") or "").strip().lower()
            if context_hint not in {"cover", "content"}:
                context_hint = ""
            tooltip_preview = row.get("tooltipPreview")
            if not isinstance(tooltip_preview, str):
                tooltip_preview = ""
            try:
                property_id = int(row.get("propertyId") or 0)
            except Exception:
                property_id = 0
            value_ids_raw = row.get("valueIds")
            if not isinstance(value_ids_raw, list):
                value_ids_raw = []
            value_ids: list[int] = []
            for v in value_ids_raw:
                try:
                    vi = int(v)
                except Exception:
                    continue
                if vi > 0:
                    value_ids.append(vi)
            options = row.get("options")
            if not isinstance(options, list):
                options = []
            cleaned_options = [str(opt).strip() for opt in options if str(opt or "").strip()]
            dropdowns.append(
                {
                    "triggerText": trigger,
                    "options": cleaned_options,
                    "propertyId": property_id if property_id > 0 else None,
                    "valueIds": value_ids,
                    "contextHint": context_hint or None,
                    "tooltipPreview": tooltip_preview[:220] if tooltip_preview else None,
                }
            )
        return dropdowns

    @staticmethod
    def _is_brochure_product_group(product_group_id: Any) -> bool:
        try:
            return int(product_group_id or 0) == 305
        except Exception:
            return False

    @classmethod
    def _resolve_brochure_property_id_for_key(cls, key_norm: str, product_group_id: Any) -> int | None:
        if not cls._is_brochure_product_group(product_group_id):
            return None

        cover_prefix = "umschlag_"
        is_cover = key_norm.startswith(cover_prefix)
        base = key_norm[len(cover_prefix) :] if is_cover else key_norm
        base = base.strip("_")
        if not base:
            return None

        aliases = {
            "seiten": "seitenanzahl",
            "seitenzahl": "seitenanzahl",
            "farbe": "farbigkeit",
            "farben": "farbigkeit",
        }
        canonical = aliases.get(base, base)

        mapping: dict[str, tuple[int, int]] = {
            "material": (8, 36),
            "farbigkeit": (7, 37),
            "seitenanzahl": (10, 38),
        }
        pair = mapping.get(canonical)
        if not pair:
            return None
        return int(pair[1] if is_cover else pair[0])

    @classmethod
    def _build_value_options(cls, value_ids: list[int], labels: list[str]) -> list[dict[str, Any]]:
        if not value_ids or not labels:
            return []
        if len(value_ids) != len(labels):
            return []

        options: list[dict[str, Any]] = []
        for label, value_id in zip(labels, value_ids, strict=False):
            label_text = str(label or "").strip()
            try:
                vid = int(value_id)
            except Exception:
                continue
            if not label_text or vid <= 0:
                continue
            options.append(
                {
                    "label": label_text,
                    "valueId": vid,
                    "labelLoose": cls._normalize_loose(label_text),
                    "labelCompact": cls._normalize_compact(label_text),
                }
            )
        return options

    @classmethod
    def _match_value_id(
        cls,
        value_options: list[dict[str, Any]],
        raw_value: Any,
    ) -> tuple[int | None, dict[str, Any]]:
        if not value_options:
            return None, {"match": "no_value_options"}
        if raw_value is None:
            return None, {"match": "missing_value"}

        token_norm = cls._normalize_loose(raw_value)
        if not token_norm:
            return None, {"match": "empty_value"}
        token_compact = token_norm.replace(" ", "")

        # Exact compact match.
        exact_hits = [opt for opt in value_options if opt.get("labelCompact") == token_compact]
        if len(exact_hits) == 1:
            return int(exact_hits[0]["valueId"]), {"match": "exact"}
        if len(exact_hits) > 1:
            return None, {
                "match": "ambiguous_exact",
                "suggestions": [str(opt.get("label") or "") for opt in exact_hits[:8]],
            }

        # Numeric input often appears inside labels like "4 Seiten".
        if re.fullmatch(r"\d+", token_norm):
            numeric_hits = [opt for opt in value_options if token_norm in str(opt.get("labelLoose") or "").split()]
            if len(numeric_hits) == 1:
                return int(numeric_hits[0]["valueId"]), {"match": "numeric"}
            if len(numeric_hits) > 1:
                return None, {
                    "match": "ambiguous_numeric",
                    "suggestions": [str(opt.get("label") or "") for opt in numeric_hits[:8]],
                }

        # Unique substring match (safe fuzzy). Reject if ambiguous.
        if token_compact and len(token_compact) >= 3:
            contains_hits = [opt for opt in value_options if token_compact in str(opt.get("labelCompact") or "")]
            if len(contains_hits) == 1:
                return int(contains_hits[0]["valueId"]), {"match": "contains"}
            if len(contains_hits) > 1:
                return None, {
                    "match": "ambiguous_contains",
                    "suggestions": [str(opt.get("label") or "") for opt in contains_hits[:8]],
                }

        # Suggestions: prefer substring hits, then token overlap.
        suggestions: list[str] = []
        if token_compact:
            suggestions = [
                str(opt.get("label") or "")
                for opt in value_options
                if token_compact in str(opt.get("labelCompact") or "")
            ]
        if not suggestions:
            wanted_tokens = set(token_norm.split())
            scored: list[tuple[int, str]] = []
            for opt in value_options:
                label = str(opt.get("label") or "")
                label_tokens = set(str(opt.get("labelLoose") or "").split())
                overlap = len(wanted_tokens.intersection(label_tokens))
                if overlap:
                    scored.append((overlap, label))
            scored.sort(reverse=True)
            suggestions = [label for _score, label in scored]

        deduped: list[str] = []
        for label in suggestions:
            if label and label not in deduped:
                deduped.append(label)

        return None, {"match": "no_match", "suggestions": deduped[:8]}

    @staticmethod
    def _extract_property_options(values_preview: str) -> list[dict[str, Any]]:
        text = str(values_preview or "")
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []
        options = payload.get("propertyOptions")
        if not isinstance(options, list):
            return []

        extracted: list[dict[str, Any]] = []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            try:
                prop_id = int(opt.get("id") or 0)
            except Exception:
                continue
            if prop_id <= 0:
                continue
            raw_values = opt.get("propertyValueOptions")
            if not isinstance(raw_values, list):
                raw_values = []
            value_ids: list[int] = []
            for row in raw_values:
                if not isinstance(row, dict):
                    continue
                try:
                    value_ids.append(int(row.get("id")))
                except Exception:
                    continue

            extracted.append(
                {
                    "propertyId": prop_id,
                    "isRange": bool(opt.get("isRange", False)),
                    "valueIds": [v for v in value_ids if v > 0],
                }
            )

        return extracted

    @classmethod
    def _assign_dropdowns_to_properties(
        cls,
        property_options: list[dict[str, Any]],
        dropdown_maps: list[dict[str, Any]],
        *,
        exclude_property_ids: set[int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        exclude_property_ids = exclude_property_ids or set()

        props = [p for p in property_options if int(p.get("propertyId") or 0) > 0]
        dropdowns = [d for d in dropdown_maps if isinstance(d.get("options"), list) and len(list(d.get("options") or [])) >= 2]
        if not props or not dropdowns:
            return {}

        # Candidate edges scored by how well option counts align.
        edges: list[tuple[int, int, int]] = []  # (score, dropdown_idx, prop_idx)
        for di, d in enumerate(dropdowns):
            opt_count = len(list(d.get("options") or []))
            for pi, p in enumerate(props):
                prop_id = int(p.get("propertyId") or 0)
                if prop_id in exclude_property_ids:
                    continue
                value_ids = list(p.get("valueIds") or [])
                if not value_ids:
                    continue
                if bool(p.get("isRange", False)):
                    continue

                diff = abs(len(value_ids) - opt_count)
                score = 70 - min(50, diff * 3)
                if diff == 0:
                    score += 45
                # Prefer medium-sized dropdowns (less likely to be noisy menus).
                if 3 <= opt_count <= 40:
                    score += 6
                edges.append((score, di, pi))

        if not edges:
            return {}
        edges.sort(reverse=True)

        assigned_dropdowns: set[int] = set()
        assigned_props: set[int] = set()
        mapping: dict[int, dict[str, Any]] = {}
        for score, di, pi in edges:
            if score < 30:
                break
            if di in assigned_dropdowns or pi in assigned_props:
                continue
            assigned_dropdowns.add(di)
            assigned_props.add(pi)
            prop_id = int(props[pi].get("propertyId") or 0)
            mapping[prop_id] = dropdowns[di]
        return mapping

    @classmethod
    def _build_label_to_value_id(cls, value_ids: list[int], labels: list[str]) -> dict[str, int]:
        if not value_ids or not labels:
            return {}
        if len(value_ids) != len(labels):
            return {}

        mapping: dict[str, int] = {}
        for label, value_id in zip(labels, value_ids, strict=False):
            normalized = cls._normalize_compact(label)
            if normalized and value_id and normalized not in mapping:
                mapping[normalized] = int(value_id)
        return mapping

    @classmethod
    def _lookup_value_id(cls, label_to_id: dict[str, int], raw_value: Any) -> int | None:
        if not label_to_id:
            return None
        if raw_value is None:
            return None

        # Direct match.
        desired = cls._normalize_compact(raw_value)
        if desired and desired in label_to_id:
            return int(label_to_id[desired])

        # Numeric input often appears inside labels like "20 Seiten".
        token = str(raw_value).strip()
        token_norm = cls._normalize_loose(token)
        if not token_norm:
            return None
        token_compact = token_norm.replace(" ", "")
        if token_compact and token_compact in label_to_id:
            return int(label_to_id[token_compact])

        # Fuzzy contains (safe): accept only when exactly one label matches.
        hits: list[int] = []
        for label_norm, value_id in label_to_id.items():
            if token_compact and token_compact in label_norm:
                hits.append(int(value_id))
        if len(hits) == 1:
            return int(hits[0])
        return None

    @classmethod
    def _pick_dropdown_for_key(cls, dropdowns: list[dict[str, Any]], raw_key: Any) -> dict[str, Any] | None:
        key_norm = cls._normalize_loose(raw_key)
        if not key_norm:
            return None
        key_compact = key_norm.replace(" ", "")

        best: tuple[int, dict[str, Any]] | None = None
        for d in dropdowns:
            trigger = str(d.get("triggerText") or "")
            trig_norm = cls._normalize_loose(trigger)
            if not trig_norm:
                continue
            trig_compact = trig_norm.replace(" ", "")

            score = 0
            if trig_norm == key_norm or trig_compact == key_compact:
                score += 40
            if key_compact and key_compact in trig_compact:
                score += 24
            if trig_compact and trig_compact in key_compact:
                score += 12

            # Token overlap bonus.
            trig_tokens = set(trig_norm.split())
            key_tokens = set(key_norm.split())
            overlap = len(trig_tokens.intersection(key_tokens))
            score += min(18, overlap * 6)

            if best is None or score > best[0]:
                best = (score, d)
        if best is None or best[0] < 18:
            return None
        return best[1]

    def build_http_replay_candidates(
        self,
        state: RunState,
        *,
        effective_options: dict[str, Any],
        raw_requested_options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        price_trace = self._find_template_trace(state, "/product-configuration/get-product-prices")
        if price_trace is None:
            return []

        raw_body = str(price_trace.get("post_data") or "").strip()
        if not raw_body:
            return []

        try:
            template_payload = json.loads(raw_body)
        except Exception:
            return []
        if not isinstance(template_payload, dict):
            return []

        # Explicit full payload override (best fidelity).
        override_payload = raw_requested_options.get("saxoprint_payload")
        if override_payload is None:
            override_payload = raw_requested_options.get("saxoprintPayload")
        if override_payload is not None:
            if isinstance(override_payload, dict):
                payload = dict(override_payload)
            else:
                return []
        else:
            payload = dict(template_payload)

        updates: list[dict[str, Any]] = []

        # Apply per-property overrides (property_44=600, property_9=51, ...).
        property_overrides = self._extract_property_overrides(raw_requested_options)
        property_label_overrides = self._extract_property_label_overrides(raw_requested_options)

        quantity_property_id = 44
        requested_qty: int | None = None
        qty_value = raw_requested_options.get("quantity", effective_options.get("quantity"))
        if qty_value is not None:
            try:
                requested_qty = int(str(qty_value).strip())
            except Exception:
                requested_qty = None
        if requested_qty is not None and requested_qty > 0:
            property_overrides.setdefault(quantity_property_id, requested_qty)

        # Best-effort: learn label→ID mappings by pairing bootstrap dropdown labels with
        # get-product-values' ordered IDs.
        dropdown_maps = self._extract_dropdown_maps(state)
        values_trace = self._find_template_trace(state, "/product-configuration/get-product-values")
        values_preview = str((values_trace or {}).get("response_body_preview") or "")
        property_options = self._extract_property_options(values_preview)
        label_maps_by_property: dict[int, dict[str, int]] = {}
        value_options_by_property: dict[int, list[dict[str, Any]]] = {}

        # Preferred path: bootstrap provides propertyId + aligned valueIds + labels.
        dropdown_by_prop: dict[int, dict[str, Any]] = {}
        for dropdown in dropdown_maps:
            try:
                prop_id = int(dropdown.get("propertyId") or 0)
            except Exception:
                prop_id = 0
            if prop_id <= 0 or prop_id == quantity_property_id:
                continue
            value_ids = list(dropdown.get("valueIds") or [])
            labels = list(dropdown.get("options") or [])
            mapping = self._build_label_to_value_id(value_ids, labels)
            if mapping:
                label_maps_by_property[prop_id] = mapping
                dropdown_by_prop[prop_id] = dropdown
                value_options_by_property[prop_id] = self._build_value_options(value_ids, labels)

        # Fallback path: heuristic assignment based on option-count alignment.
        dropdown_assignment: dict[int, dict[str, Any]] = {}
        if property_options and dropdown_maps:
            dropdown_assignment = self._assign_dropdowns_to_properties(
                property_options,
                dropdown_maps,
                exclude_property_ids={quantity_property_id} | set(dropdown_by_prop.keys()),
            )

        if dropdown_assignment and property_options:
            prop_by_id = {int(p.get("propertyId") or 0): p for p in property_options}
            for prop_id, dropdown in dropdown_assignment.items():
                prop = prop_by_id.get(int(prop_id))
                if not prop:
                    continue
                value_ids = list(prop.get("valueIds") or [])
                labels = list(dropdown.get("options") or [])
                mapping = self._build_label_to_value_id(value_ids, labels)
                if mapping and int(prop_id) not in label_maps_by_property:
                    label_maps_by_property[int(prop_id)] = mapping
                    dropdown_by_prop[int(prop_id)] = dropdown
                    value_options_by_property[int(prop_id)] = self._build_value_options(value_ids, labels)

        # Resolve label-based property_* overrides like property_9="DIN A5 ...".
        for prop_id, label in sorted(property_label_overrides.items()):
            if prop_id in property_overrides:
                continue
            value_map = label_maps_by_property.get(int(prop_id))
            value_id = self._lookup_value_id(value_map or {}, label)
            if value_id:
                property_overrides[prop_id] = int(value_id)
                updates.append(
                    {
                        "propertyId": prop_id,
                        "from": None,
                        "to": int(value_id),
                        "source": "saxoprint_label_mapping",
                        "requestedLabel": label,
                    }
                )

        # SIDELOAD: from value_maps/saxoprint.de.json (loaded into
        # option_prevalidation.matched[*].sideloadResolution by
        # cli._augment_with_sideload). Wins over the dropdown-trigger
        # heuristic; loses to explicit property_*=N and label-resolved
        # property_*="..." (both already in property_overrides above).
        sideload_candidates = self._extract_sideload_overrides(state)
        sideload_applied: list[dict[str, Any]] = []
        for record in sideload_candidates:
            pid = int(record["propertyId"])
            if pid in property_overrides:
                # Explicit or label-resolved already won.
                continue
            value_id = int(record["backendId"])
            property_overrides[pid] = value_id
            sideload_applied.append(record)
            updates.append(
                {
                    "propertyId": pid,
                    "from": None,
                    "to": value_id,
                    "source": "saxoprint_sideload",
                    "key": record["key"],
                    "matchedLabel": record["matchedLabel"],
                    "confidence": record["confidence"],
                    "productScope": record["productScope"],
                }
            )

        # Resolve friendly option keys (e.g. format/bindung/material) if they appear as dropdown triggers.
        # This is best-effort; manual property_* overrides always win.
        synthesis_issues: list[dict[str, Any]] = []
        requested_friendly: list[str] = []
        applied_friendly: list[str] = []

        product_group_id = payload.get("productGroupId")
        try:
            product_group_id_int: int | None = int(product_group_id or 0)
        except Exception:
            product_group_id_int = None
        if product_group_id_int is not None and product_group_id_int <= 0:
            product_group_id_int = None
        if dropdown_maps and (label_maps_by_property or value_options_by_property):
            for raw_key, raw_value in (raw_requested_options or {}).items():
                key_norm = str(raw_key or "").strip().lower().replace("-", "_")
                if not key_norm:
                    continue
                if key_norm in {"quantity", "saxoprint_payload", "saxoprintpayload"}:
                    continue
                if re.fullmatch(r"(?:property|prop|p)_(\d+)", key_norm) or re.fullmatch(r"p(\d+)", key_norm):
                    continue

                requested_friendly.append(str(raw_key))

                if raw_value is None or str(raw_value).strip() == "":
                    synthesis_issues.append(
                        {
                            "key": str(raw_key),
                            "requestedValue": raw_value,
                            "reason": "missing_value",
                        }
                    )
                    continue

                # Brochure-specific deterministic disambiguation for duplicated triggers.
                resolved_prop_id = self._resolve_brochure_property_id_for_key(key_norm, product_group_id)

                # Context hint fallback (when key includes umschlag_ prefix).
                is_cover = key_norm.startswith("umschlag_")
                base_key = key_norm[len("umschlag_") :] if is_cover else key_norm
                desired_context: str | None = None
                if resolved_prop_id is None:
                    if is_cover:
                        desired_context = "cover"
                    elif base_key in {"material", "farbigkeit", "seitenanzahl", "seiten", "seitenzahl"}:
                        desired_context = "content"

                dropdown: dict[str, Any] | None = None
                prop_id: int | None = resolved_prop_id
                trigger_text: str | None = None
                context_hint: str | None = None

                if prop_id is not None:
                    dropdown = dropdown_by_prop.get(int(prop_id))
                    if dropdown:
                        trigger_text = str(dropdown.get("triggerText") or "") or None
                        context_hint = str(dropdown.get("contextHint") or "") or None
                else:
                    dropdown_candidates = dropdown_maps
                    if desired_context:
                        filtered = [
                            d
                            for d in dropdown_maps
                            if str(d.get("contextHint") or "").strip().lower() == desired_context
                        ]
                        if filtered:
                            dropdown_candidates = filtered

                    dropdown = self._pick_dropdown_for_key(dropdown_candidates, base_key)
                    if not dropdown and dropdown_candidates is not dropdown_maps:
                        dropdown = self._pick_dropdown_for_key(dropdown_maps, base_key)
                    if dropdown:
                        trigger_text = str(dropdown.get("triggerText") or "") or None
                        context_hint = str(dropdown.get("contextHint") or "") or None
                        for candidate_prop_id, candidate_dropdown in dropdown_by_prop.items():
                            if candidate_dropdown is dropdown:
                                prop_id = int(candidate_prop_id)
                                break

                if not prop_id:
                    synthesis_issues.append(
                        {
                            "key": str(raw_key),
                            "requestedValue": raw_value,
                            "reason": "no_matching_dropdown",
                        }
                    )
                    continue

                # Manual property overrides win, but still count as handled.
                if int(prop_id) in property_overrides:
                    applied_friendly.append(str(raw_key))
                    continue

                value_options = value_options_by_property.get(int(prop_id)) or []
                value_id, match_info = self._match_value_id(value_options, raw_value)
                if value_id:
                    property_overrides[int(prop_id)] = int(value_id)
                    applied_friendly.append(str(raw_key))
                    updates.append(
                        {
                            "propertyId": int(prop_id),
                            "from": None,
                            "to": int(value_id),
                            "source": "saxoprint_dropdown_trigger_mapping",
                            "match": str(match_info.get("match") or ""),
                            "key": str(raw_key),
                            "requestedValue": raw_value,
                            "triggerText": trigger_text,
                            "contextHint": context_hint,
                        }
                    )
                else:
                    synthesis_issues.append(
                        {
                            "key": str(raw_key),
                            "requestedValue": raw_value,
                            "reason": str(match_info.get("match") or "no_match"),
                            "propertyId": int(prop_id),
                            "triggerText": trigger_text,
                            "contextHint": context_hint,
                            "suggestions": list(match_info.get("suggestions") or [])[:8],
                        }
                    )

        prop_rows = payload.get("propertyConfiguration")
        if not isinstance(prop_rows, list):
            prop_rows = []

        existing_by_id: dict[int, dict[str, Any]] = {}
        for row in prop_rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("propertyId"))
            except Exception:
                continue
            existing_by_id[pid] = row

        for prop_id, value_id in sorted(property_overrides.items()):
            previous = existing_by_id.get(prop_id)
            if previous is None:
                prop_rows.append({"propertyId": prop_id, "value": value_id})
                existing_by_id[prop_id] = prop_rows[-1]
                updates.append({"propertyId": prop_id, "from": None, "to": value_id})
            else:
                prev_value = previous.get("value")
                if prev_value != value_id:
                    previous["value"] = value_id
                    updates.append({"propertyId": prop_id, "from": prev_value, "to": value_id})

        payload["propertyConfiguration"] = prop_rows

        # If we have a requested quantity, recompute printRuns to match Saxoprint’s own tier list.
        if requested_qty is not None and requested_qty > 0:
            allowed = self._parse_quantity_value_options(values_preview, quantity_property_id)
            print_runs = self._compute_print_runs(allowed, requested_qty)
            if print_runs:
                prev = payload.get("printRuns")
                payload["printRuns"] = print_runs
                updates.append({"field": "printRuns", "from": prev, "to": print_runs})

        payload.setdefault("deliverySplitPrintRuns", [])

        endpoint = str(price_trace.get("url") or state.target.product_url)
        return [
            {
                "url": endpoint,
                "method": "POST",
                "source": "saxoprint_synthesized_payload",
                "body_text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "request_headers": {"content-type": "application/json"},
                "trace_override": price_trace,
                "request_template_applied": {
                    "kind": "saxoprint_payload",
                    "updates": updates,
                    "usedOverridePayload": bool(override_payload is not None),
                    "quantityPropertyId": quantity_property_id,
                    "productGroupId": product_group_id_int,
                    "requestedFriendlyOptions": requested_friendly[:50],
                    "appliedFriendlyOptions": applied_friendly[:50],
                    "synthesisIssues": synthesis_issues[:50],
                    "sideloadApplied": sideload_applied[:50],
                },
            }
        ]


@dataclass(frozen=True)
class Print24SiteAdapter:
    def supports(self, state: RunState) -> bool:
        site_name = str(state.target.site_name or "").lower()
        return "print24" in site_name

    def _find_template_trace(self, state: RunState) -> dict[str, Any] | None:
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
            if '"properties"' not in post_data:
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

    # Hardcoded fallback mapping per option_key — used when the bootstrap's
    # productDetails harvest didn't run (older runs / runs without traces).
    # These remain correct for the values they cover; broader coverage now
    # comes from the harvested catalog. See `_consume_print24_catalog`.
    _HARDCODED_FORMAT_MAP: ClassVar[dict[str, str]] = {
        "a5": "268", "din a5": "268", "din-a5": "268", "din_a5": "268",
        "a6": "222", "din a6": "222", "din-a6": "222", "din_a6": "222",
    }
    _HARDCODED_QUANTITY_MAP: ClassVar[dict[str, str]] = {
        "250": "438",
        "10": "339",
    }

    @staticmethod
    def _value_matches_captured_label(requested: Any, captured_label: str) -> bool:
        """Return True iff the user's requested value is the SAME option as the
        captured visibleLabel of the prevalidation-matched catalog row.

        Print24's prevalidation matches groups loosely (alias-based scoring),
        so a `material` request for "115 g/m² Bilderdruckpapier" will "match"
        the captured row for "130 g/m² Recycling-Bilderdruckpapier" — same
        group, different option. Using the captured prop_id in that case
        would write the WRONG option to the server and return the wrong
        price silently.

        Strict check: after normalizing whitespace and lowercasing, require
        the request to be either equal to the captured label or a substring
        of it. Numeric-overlap heuristics (e.g., "A5" vs "A6" share no
        numbers; "DIN A5" vs "105 x 148 mm DIN A6" share "148") have proven
        unreliable — substring is the cleanest signal that the user typed
        the captured label (in full or in part).
        """
        req_text = str(requested or "").strip()
        cap_text = str(captured_label or "").strip()
        if not req_text or not cap_text:
            return False
        # NFKD decomposes Unicode superscripts (²/³) into their digit
        # equivalents and splits precomposed accents, so users who type
        # `g/m2` match captured `g/m²` and `stück` matches `Stück`.
        norm_req = re.sub(r"\s+", " ", unicodedata.normalize("NFKD", req_text)).strip().lower()
        norm_cap = re.sub(r"\s+", " ", unicodedata.normalize("NFKD", cap_text)).strip().lower()
        if not norm_req:
            return False
        if norm_req == norm_cap:
            return True
        return norm_req in norm_cap

    @staticmethod
    def _consume_print24_catalog(
        state: RunState,
        raw_requested_options: dict[str, Any],
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[dict[str, Any]]]:
        """Pull desired property prop_ids from the productDetails-harvested
        catalog (`option_prevalidation.matched`). Returns (desired_ids,
        catalog_applied, catalog_skipped) where desired_ids maps box_name
        to prop_id ONLY for options whose user-requested value actually
        matches the captured label. Mismatches are recorded in
        catalog_skipped with reason=prop_id_unknown_for_value.
        """
        desired_ids: dict[str, str] = {}
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        matched_rows = list(prevalidation.get("matched") or [])
        for row in matched_rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if not key or key not in raw_requested_options:
                continue
            catalog_option = dict(row.get("catalogOption") or {})
            box_name = str(catalog_option.get("name") or "").strip()
            backend_hints = dict(catalog_option.get("backendHints") or {})
            prop_id = str(backend_hints.get("dataPropertyId") or "").strip()
            if not box_name or not prop_id:
                continue
            captured_label = str(catalog_option.get("visibleLabel") or catalog_option.get("visibleValue") or "")
            requested_value = raw_requested_options.get(key)
            if Print24SiteAdapter._value_matches_captured_label(requested_value, captured_label):
                desired_ids[box_name] = prop_id
                applied.append(
                    {
                        "key": key,
                        "boxName": box_name,
                        "propId": prop_id,
                        "capturedLabel": captured_label,
                        "via": "harvested_catalog",
                    }
                )
            else:
                skipped.append(
                    {
                        "key": key,
                        "rawValue": str(requested_value),
                        "reason": "prop_id_unknown_for_value",
                        "boxName": box_name,
                        "capturedValue": captured_label,
                        "capturedPropId": prop_id,
                        "note": (
                            "Print24 catalog harvest only carries the currently-captured option per "
                            "group (one prop_id from `productDetails.prop_details`). The prop_id for "
                            "the requested value is unknown. Re-bootstrap with the desired value "
                            "pre-selected, supply --option-id explicitly, or wait for the `repo` "
                            "probe (print24_feasibility_notes.md §3.1/§9) that harvests all alternatives."
                        ),
                    }
                )
        return desired_ids, applied, skipped

    def build_http_replay_candidates(
        self,
        state: RunState,
        *,
        effective_options: dict[str, Any],
        raw_requested_options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        template_trace = self._find_template_trace(state)
        if template_trace is None:
            return []

        raw_body = str(template_trace.get("post_data") or "").strip()
        if not raw_body:
            return []

        try:
            payload = json.loads(raw_body)
        except Exception:
            return []
        if not isinstance(payload, dict):
            return []

        # PRIMARY: harvested productDetails catalog (per print24_feasibility_notes.md §3.3).
        desired_ids, catalog_applied, catalog_skipped = self._consume_print24_catalog(
            state, raw_requested_options
        )
        catalog_applied_keys = {entry["key"] for entry in catalog_applied}
        catalog_covered_keys = catalog_applied_keys | {
            entry["key"] for entry in catalog_skipped
        }

        # SECONDARY: sideloaded value-map (`value_maps/print24.com.json`).
        # Fills slots that the harvested catalog couldn't resolve cleanly. Does
        # NOT override catalog_applied (catalog is freshest, per-session). Does
        # override catalog_skipped (catalog matched the group but couldn't
        # resolve the requested value — sideload may know the prop_id).
        sideload_applied: list[dict[str, Any]] = []
        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        for row in list(prevalidation.get("matched") or []):
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if not key or key not in raw_requested_options:
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

        # LEGACY FALLBACK: hardcoded A5/A6 + 250/10 mappings for keys the
        # catalog harvest AND sideload couldn't cover (preserves behavior for
        # older runs without value_maps/print24.com.json).
        sideload_keys = {entry["key"] for entry in sideload_applied}
        resolved_keys = catalog_covered_keys | sideload_keys
        hardcoded_applied: list[dict[str, Any]] = []
        if "format" not in resolved_keys:
            fmt_norm = str(raw_requested_options.get("format") or "").strip().lower()
            mapped_format = self._HARDCODED_FORMAT_MAP.get(fmt_norm)
            if mapped_format:
                desired_ids["format"] = mapped_format
                hardcoded_applied.append({"key": "format", "boxName": "format", "propId": mapped_format, "via": "hardcoded_fallback"})
        if "quantity" not in resolved_keys:
            qty_norm = str(raw_requested_options.get("quantity") or "").strip()
            mapped_qty = self._HARDCODED_QUANTITY_MAP.get(qty_norm)
            if mapped_qty:
                desired_ids["quantity"] = mapped_qty
                hardcoded_applied.append({"key": "quantity", "boxName": "quantity", "propId": mapped_qty, "via": "hardcoded_fallback"})

        updated: list[dict[str, Any]] = []
        updated_any = False
        properties = payload.get("properties")
        if not isinstance(properties, list):
            return []

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

        # Compute unsupportedInputs across ALL requested keys not covered by
        # catalog (applied OR skipped), sideload, or the hardcoded fallback.
        covered_keys = (
            catalog_covered_keys
            | sideload_keys
            | {entry["key"] for entry in hardcoded_applied}
        )
        unsupported_inputs = [
            {"inputKey": key, "rawValue": str(raw_requested_options.get(key))}
            for key in raw_requested_options
            if str(key).strip() and key not in covered_keys
        ]

        # Emit a candidate even if no payload row changed, so diagnostics
        # surface when every requested option fell into catalog_skipped /
        # unsupported_inputs (user must see why nothing was applied).
        if not updated_any and not catalog_skipped and not unsupported_inputs:
            return []

        endpoint = str(template_trace.get("url") or state.target.product_url)
        return [
            {
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
        ]


@dataclass(frozen=True)
class OnlineprintersSiteAdapter:
    def supports(self, state: RunState) -> bool:
        site_name = str(state.target.site_name or "").lower()
        return "onlineprinters" in site_name

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
        """Per onlineprinters_request_modification.md §5B: write the literal
        string "Interpolation" into the quantity tile field AND write the
        typed quantity into the input_qty_1 field that immediately follows
        the qty field in form-pair order. Preserve everything else in place;
        do NOT delete/append, that breaks the request.

        Returns (new_pairs, did_write, qty_field_index)."""
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
            # No trailing input_qty_1 exists; insert one immediately after the
            # qty field rather than appending at the end (the configurator is
            # sensitive to placement per the MD).
            new_pairs.insert(qty_field_index + 1, ("input_qty_1", str(typed_value)))
        return new_pairs, True, qty_field_index

    @staticmethod
    def _onlineprinters_group_code(option_row: dict[str, Any], group_row: dict[str, Any]) -> str:
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
        *,
        group_code: str,
        option_code: str,
        variant_url: str | None = None,
    ) -> str:
        decoded_link = OnlineprintersSiteAdapter._decode_template_text(current_setlink)
        if not decoded_link:
            return ""

        parsed = urlsplit(decoded_link)
        base_parts = parsed
        if variant_url:
            resolved_variant = OnlineprintersSiteAdapter._decode_template_text(variant_url)
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
            param_name = (
                "depvar_index_setparent"
                if any(key == "depvar_index_setparent" for key, _ in query_pairs)
                else "depvar_index_set"
            )
            updated_pairs.append((param_name, depvar_value))

        updated_link = urlunsplit(
            (
                base_parts.scheme,
                base_parts.netloc,
                base_parts.path,
                urlencode(updated_pairs, doseq=True),
                base_parts.fragment,
            )
        )
        # Encode once so we remain compatible with the form POST body format.
        return quote(updated_link, safe="/:?&=%<>")

    def _find_template_trace(self, state: RunState) -> dict[str, Any] | None:
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

    def build_http_replay_candidates(
        self,
        state: RunState,
        *,
        effective_options: dict[str, Any],
        raw_requested_options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
        matched_rows = list(prevalidation.get("matched") or [])
        if not matched_rows:
            return []

        template_trace = self._find_template_trace(state)
        if template_trace is None:
            return []

        raw_body = str(template_trace.get("post_data") or "").strip()
        if not raw_body:
            return []

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
                        if name.startswith("input_var_") and re.fullmatch(r"\d+(?:[\.,]\d+)?", str(value).strip())
                    ),
                    "",
                )
                control_name = numeric_field

            control_value = str(option_row.get("visibleValue") or option_row.get("visibleLabel") or requested_value).strip()
            if match_type == "quantity_manual":
                control_value = str(requested_value)

            option_backend = dict(option_row.get("backendHints") or {})
            group_code = self._onlineprinters_group_code(option_row, group_row)
            option_code = str(option_backend.get("dataVarindex") or "").strip()
            variant_url = str(option_row.get("variantUrl") or option_row.get("href") or "").strip()
            if variant_url:
                variant_url_applied = urljoin(state.target.product_url, variant_url)

            # Onlineprinters interpolation mode (per onlineprinters_request_modification.md
            # §5B): when the requested quantity is not in the captured tile presets,
            # the configurator expects input_var_<PROD>_<qty_group>_1 = "Interpolation"
            # plus the typed quantity carried in input_qty_1 (positionally after the
            # qty input_var). The Interpolation tile's data-varindex from the DOM
            # is the setlink option_code for this mode.
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
                        # Best-effort note when requested qty exceeds even the
                        # max preset — interpolation usually has a server-side
                        # upper bound; if the response normalizes, this is why.
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

            # If the catalog row carries `optionCodeUnknown` AND it is NOT the
            # currently-selected option, writing only the visible label would
            # let the backend silently normalize back to the current selection
            # (per onlineprinters_request_modification.md §3.3). Skip with a
            # precise reason so the user sees what's needed (a probed mapping).
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

            field_record = {
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
                field_record["interpolationNote"] = interpolation_note
            updated_fields.append(field_record)

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
            {"inputKey": key, "rawValue": str(raw_requested_options.get(key))}
            for key in raw_requested_options
            if str(key).strip() and key not in matched_keys
        ]

        if not updated_any:
            return []

        return [
            {
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
        ]


@dataclass(frozen=True)
class ViaprintoSiteAdapter:
    def supports(self, state: RunState) -> bool:
        site_name = str(state.target.site_name or "").lower()
        target_url = str(state.target.product_url or "").lower()
        return ("viaprinto" in site_name) or ("viaprinto" in target_url)

    @staticmethod
    def _find_template_url(state: RunState) -> str | None:
        for trace in state.target.network_traces:
            url = str(trace.get("url") or "").strip()
            if not url:
                continue
            lowered = url.lower()
            if "viaprinto" in lowered and "/-/content_size" in lowered:
                return url
        product_url = str(state.target.product_url or "").strip()
        if product_url and "viaprinto" in product_url.lower():
            return product_url
        return None

    @staticmethod
    def _normalize_option_map(options: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in dict(options or {}).items():
            key_text = str(key or "").strip().upper().replace(" ", "_")
            if not key_text:
                continue
            normalized[key_text] = value
        return normalized

    def build_http_replay_candidates(
        self,
        state: RunState,
        *,
        effective_options: dict[str, Any],
        raw_requested_options: dict[str, Any],
    ) -> list[dict[str, Any]]:
        template_url = self._find_template_url(state)
        if not template_url:
            return []

        parsed = urlsplit(template_url)
        current_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query_map = {str(k): str(v) for k, v in current_pairs}

        normalized_effective = self._normalize_option_map(effective_options)
        normalized_raw = self._normalize_option_map(raw_requested_options)
        merged = dict(normalized_effective)
        merged.update(normalized_raw)

        key_aliases: dict[str, list[str]] = {
            "AMOUNT": ["AMOUNT", "QUANTITY", "AUFLAGE", "MENGE"],
            "CONTENT_SIZE": ["CONTENT_SIZE", "FORMAT", "SIZE", "END_FORMAT", "DIN"],
            "PAGES": ["PAGES", "SEITEN", "SEITIG", "UMFANG"],
            "CONTENT_PAPER": ["CONTENT_PAPER", "PAPIER", "MATERIAL", "PAPER"],
            "COVER_PAPER": ["COVER_PAPER", "COVER", "UMSCHLAG", "COVER_MATERIAL"],
            "CONTENT_COLOR": ["CONTENT_COLOR", "FARBE", "PRINT", "DRUCK"],
            "COVER_COLOR": ["COVER_COLOR", "COVER_PRINT", "UMSCHLAG_FARBE"],
            "AUSRICHTUNG": ["AUSRICHTUNG", "ORIENTATION"],
        }

        updates: list[dict[str, Any]] = []
        for param_key, aliases in key_aliases.items():
            matched_value: Any = None
            for alias in aliases:
                if alias in merged and str(merged[alias]).strip() != "":
                    matched_value = merged[alias]
                    break
            if matched_value is None:
                continue
            value_text = str(matched_value).strip()
            if not value_text:
                continue
            previous = query_map.get(param_key)
            if previous == value_text:
                continue
            query_map[param_key] = value_text
            updates.append({"param": param_key, "from": previous, "to": value_text})

        if not updates:
            return []

        new_query = urlencode(sorted(query_map.items()), doseq=True)
        endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))

        return [
            {
                "url": endpoint,
                "method": "GET",
                "source": "viaprinto_synthesized_query",
                "request_template_applied": {
                    "kind": "viaprinto_query_params",
                    "paramsUpdated": updates,
                    "templatePath": parsed.path,
                },
            }
        ]


_DEFAULT_ADAPTERS: list[SiteAdapter] | None = None


def get_default_site_adapters() -> list[SiteAdapter]:
    global _DEFAULT_ADAPTERS
    if _DEFAULT_ADAPTERS is None:
        _DEFAULT_ADAPTERS = [
            SaxoprintSiteAdapter(),
            Print24SiteAdapter(),
            OnlineprintersSiteAdapter(),
            WirMachenDruckSiteAdapter(),
            ViaprintoSiteAdapter(),
        ]
    return list(_DEFAULT_ADAPTERS)


def build_site_replay_candidates(
    state: RunState,
    *,
    effective_options: dict[str, Any],
    raw_requested_options: dict[str, Any],
    additional_adapters: list[SiteAdapter] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    adapters = list(get_default_site_adapters())
    if additional_adapters:
        adapters.extend(additional_adapters)
    for adapter in adapters:
        if adapter.supports(state):
            candidates.extend(
                adapter.build_http_replay_candidates(
                    state,
                    effective_options=effective_options,
                    raw_requested_options=raw_requested_options,
                )
            )
    return candidates
