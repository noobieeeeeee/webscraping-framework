from __future__ import annotations

from dataclasses import asdict

from .agents import (
    DiscoveryAgent,
    ExecutionAgent,
    FeasibilityAgent,
    PlannerAgent,
    RepairAgent,
    ValidationAgent,
)
from .knowledge_store import KnowledgeStore
from .models import FailureRecord, KnowledgeRecord, RunState


class ExtractionOrchestrator:
    NORMALIZATION_PROMOTION_MIN_HITS = 3

    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store
        self.discovery = DiscoveryAgent()
        self.feasibility = FeasibilityAgent()
        self.planner = PlannerAgent(store)
        self.executor = ExecutionAgent()
        self.validator = ValidationAgent()
        self.repair = RepairAgent()

    @staticmethod
    def _value_equals(left: object, right: object) -> bool:
        if left == right:
            return True
        if left is None or right is None:
            return False
        return str(left).strip().lower() == str(right).strip().lower()

    @staticmethod
    def _normalize_value(value: object) -> str:
        return str(value).strip().lower()

    def _merge_learned_rules(
        self,
        existing: dict[str, dict[str, str]],
        mappings: list[tuple[str, object, object, bool]],
    ) -> dict[str, dict[str, str]]:
        merged: dict[str, dict[str, str]] = {
            str(key): dict(value)
            for key, value in (existing or {}).items()
            if isinstance(value, dict)
        }
        for option_key, requested_value, accepted_value, _trusted in mappings:
            key = str(option_key)
            req_norm = self._normalize_value(requested_value)
            accepted_text = str(accepted_value).strip()
            if not key or not req_norm or not accepted_text:
                continue
            merged.setdefault(key, {})[req_norm] = accepted_text
        return merged

    def _collect_normalization_mappings(self, state: RunState) -> list[tuple[str, object, object, bool]]:
        if state.extraction is None or not state.extraction.accepted_configuration:
            return []

        option_application = dict(state.target.bootstrap_signals.get("option_application") or {})
        matched_rows = list(option_application.get("matched") or [])
        matched_keys = {
            str(row.get("key", "")).strip().lower()
            for row in matched_rows
            if str(row.get("key", "")).strip()
        }

        requested_options = dict(
            state.target.bootstrap_signals.get("user_requested_options")
            or state.target.options
            or {}
        )
        accepted = dict(state.extraction.accepted_configuration)
        mappings: list[tuple[str, object, object, bool]] = []
        for key, requested_value in requested_options.items():
            if key not in accepted:
                continue
            accepted_value = accepted.get(key)
            if self._value_equals(requested_value, accepted_value):
                continue
            key_lower = str(key).strip().lower()
            learned_from_match = key_lower in matched_keys
            mappings.append((str(key), requested_value, accepted_value, learned_from_match))
        return mappings

    def _has_strong_api_price_evidence(self, state: RunState) -> bool:
        if state.extraction is None:
            return False
        summary = state.extraction.raw_response_summary
        replay_mode = str(summary.get("replay", "")).lower()
        endpoint = str(summary.get("endpoint", "")).lower()
        if replay_mode not in {"http", "captured_trace"}:
            return False
        if "/api/" not in endpoint:
            return False

        named_prices = summary.get("namedPrices")
        if not isinstance(named_prices, dict):
            return False
        return (
            "total_gross_value" in named_prices
            and "total_net_value" in named_prices
        )

    def _promote_untrusted_mappings_if_eligible(
        self,
        state: RunState,
        learned_mappings: list[tuple[str, object, object, bool]],
    ) -> list[dict[str, object]]:
        if not learned_mappings:
            return []
        if not self._has_strong_api_price_evidence(state):
            return []

        candidate_keys = {
            (str(key), self._normalize_value(requested_value), str(accepted_value).strip())
            for key, requested_value, accepted_value, trusted in learned_mappings
            if not trusted
        }
        if not candidate_keys:
            return []

        rows = self.store.get_site_option_normalization_rows(state.target.site_name)
        rows_by_key = {
            (str(row.get("optionKey", "")), str(row.get("requestedValueNorm", ""))): row
            for row in rows
        }

        promotions: list[tuple[str, str]] = []
        promoted_rows: list[dict[str, object]] = []
        for option_key, requested_norm, accepted_value in sorted(candidate_keys):
            row = rows_by_key.get((option_key, requested_norm))
            if row is None:
                continue
            if bool(row.get("trusted", False)):
                continue
            if int(row.get("hitCount", 0)) < self.NORMALIZATION_PROMOTION_MIN_HITS:
                continue
            if str(row.get("acceptedValue", "")).strip() != accepted_value:
                continue

            promotions.append((option_key, requested_norm))
            promoted_rows.append(
                {
                    "key": option_key,
                    "requested": requested_norm,
                    "accepted": accepted_value,
                    "hitCount": int(row.get("hitCount", 0)),
                    "promotion": "repeated_consistent_api_named_totals",
                }
            )

        if promotions:
            self.store.promote_option_normalizations(state.target.site_name, promotions)

        return promoted_rows

    def run(self, state: RunState) -> RunState:
        state.target.bootstrap_signals.setdefault("user_requested_options", dict(state.target.options))
        learned_rules = self.store.get_site_option_normalizations(state.target.site_name)
        if learned_rules:
            state.target.bootstrap_signals["learned_normalization_rules"] = learned_rules

        state.observation = self.discovery.run(state)
        state.feasibility = self.feasibility.run(state)

        if not state.feasibility.feasible:
            state.failures.append(
                FailureRecord(
                    reason="infeasible_target",
                    strategy=state.feasibility.recommended_strategy,
                    details={"rationale": state.feasibility.rationale},
                )
            )
            return state

        state.plan = self.planner.run(state)

        while state.attempt_count < state.max_attempts:
            state.attempt_count += 1
            state.extraction = self.executor.run(state)
            state.validation = self.validator.run(state)

            if state.extraction.success and state.validation.is_valid:
                learned_mappings = self._collect_normalization_mappings(state)
                if learned_mappings:
                    self.store.save_option_normalizations(state.target.site_name, learned_mappings)
                    promoted_rows = self._promote_untrusted_mappings_if_eligible(state, learned_mappings)
                    state.target.bootstrap_signals["learned_normalization_rules"] = self.store.get_site_option_normalizations(
                        state.target.site_name,
                        trusted_only=True,
                    )
                    response_summary = state.extraction.raw_response_summary
                    response_summary["learnedNormalizationMappings"] = [
                        {
                            "key": str(key),
                            "requested": requested,
                            "accepted": accepted,
                            "trusted": bool(learned_from_match),
                        }
                        for key, requested, accepted, learned_from_match in learned_mappings
                    ]
                    if promoted_rows:
                        response_summary["promotedNormalizationMappings"] = promoted_rows

            deterministic_success = (
                state.validation.is_valid
                and state.extraction.success
                and state.extraction.raw_response_summary.get("fallback") != "heuristic"
            )

            if deterministic_success and state.extraction is not None:
                template_applied = state.extraction.raw_response_summary.get("requestTemplateApplied")
                if isinstance(template_applied, dict):
                    kind = str(template_applied.get("kind") or "").strip()
                    if kind:
                        endpoint = (
                            str(state.extraction.raw_response_summary.get("endpoint") or "").strip()
                            or str(state.plan.endpoint or "unknown")
                        )
                        self.store.save_replay_template(
                            state.target.site_name,
                            kind,
                            endpoint,
                            template_applied,
                        )

            self.store.save_record(
                KnowledgeRecord(
                    site_name=state.target.site_name,
                    endpoint=state.plan.endpoint or "unknown",
                    strategy=state.plan.strategy,
                    success=deterministic_success,
                    failure_reason=state.validation.inferred_failure_reason,
                )
            )

            if state.validation.is_valid:
                return state

            state.failures.append(
                FailureRecord(
                    reason=state.validation.inferred_failure_reason or "validation_failure",
                    strategy=state.plan.strategy,
                    details={
                        "attempt": state.attempt_count,
                        "mismatches": list(state.validation.mismatches),
                    },
                )
            )
            state.plan = self.repair.run(state)

        return state


def summarize_run(state: RunState) -> dict:
    response_summary = state.extraction.raw_response_summary if state.extraction else {}
    option_application = dict(state.target.bootstrap_signals.get("option_application") or {})
    option_prevalidation = dict(state.target.bootstrap_signals.get("option_prevalidation") or {})
    option_matched = list(option_application.get("matched") or [])
    option_unmatched = list(option_application.get("unmatched") or [])
    option_requested_count = int(option_application.get("requestedCount") or len(state.target.options))
    extracted_price = state.extraction.price_value if state.extraction else None
    tolerance = max(0.0, float(state.target.expected_price_tolerance or 0.01))

    site_reference_price: float | None = None
    site_reference_source: str | None = None
    site_reference_kind = "none"

    grouped = response_summary.get("priceCandidateGroups")
    if isinstance(grouped, dict):
        top_level = grouped.get("top_level_total")
        if isinstance(top_level, list) and top_level:
            row = top_level[0]
            if isinstance(row, dict):
                try:
                    site_reference_price = round(float(row.get("value", 0.0)), 2)
                except (TypeError, ValueError):
                    site_reference_price = None
                site_reference_source = str(row.get("source", "")) or None
                if site_reference_price is not None:
                    site_reference_kind = "grouped_top_level_total"

    if site_reference_price is None:
        named_prices = response_summary.get("namedPrices")
        if isinstance(named_prices, dict):
            for key in ["total_gross_value", "total_gross", "total_net_value", "total_net"]:
                if key not in named_prices:
                    continue
                try:
                    site_reference_price = round(float(named_prices[key]), 2)
                except (TypeError, ValueError):
                    site_reference_price = None
                    continue
                site_reference_source = f"named:{key}"
                site_reference_kind = "named_total"
                break

    site_price_delta: float | None = None
    site_price_within_tolerance: bool | None = None
    if extracted_price is not None and site_reference_price is not None:
        site_price_delta = round(float(extracted_price) - float(site_reference_price), 2)
        site_price_within_tolerance = abs(site_price_delta) <= tolerance

    result = {
        "site": state.target.site_name,
        "url": state.target.product_url,
        "final_status": "completed" if (state.validation.is_valid if state.validation else False) else "failed",
        "request_only": bool(state.target.bootstrap_signals.get("request_only", False)),
        "attempts": state.attempt_count,
        "feasible": state.feasibility.feasible if state.feasibility else False,
        "complexity": state.feasibility.complexity.value if state.feasibility else None,
        "strategy": state.plan.strategy.value if state.plan else None,
        "validated": state.validation.is_valid if state.validation else False,
        "price": extracted_price,
        "currency": state.extraction.currency if state.extraction else None,
        "mismatches": state.validation.mismatches if state.validation else [],
        "failures": [asdict(f) for f in state.failures],
        "quantity_behavior_hint": state.observation.quantity_behavior_hint if state.observation else None,
        "option_group_count": state.observation.option_group_count if state.observation else 0,
        "dependency_edge_count": state.observation.dependency_edge_count if state.observation else 0,
        "active_dependency_edge_count": state.observation.active_dependency_edge_count if state.observation else 0,
        "has_quantity_threshold_behavior": state.observation.has_quantity_threshold_behavior if state.observation else False,
        "has_manual_quantity_input": state.observation.has_manual_quantity_input if state.observation else False,
        "has_preset_quantities": state.observation.has_preset_quantities if state.observation else False,
        "option_requested_count": option_requested_count,
        "option_matched_count": len(option_matched),
        "option_unmatched_count": len(option_unmatched),
        "option_matched": option_matched,
        "option_unmatched": option_unmatched,
        "replay_mode": response_summary.get("replay", "none"),
        "fallback_mode": response_summary.get("fallback", "none"),
        "adapter_result": response_summary.get("adapterResult"),
        "http_replay": response_summary.get("httpReplay", {}),
        "extraction_reason": response_summary.get("reason"),
        "named_prices": response_summary.get("namedPrices", {}),
        "price_candidates": response_summary.get("priceCandidates", []),
        "price_candidates_raw": response_summary.get("priceCandidatesRaw", []),
        "price_candidate_groups": response_summary.get("priceCandidateGroups", {}),
        "price_candidate_group_counts": response_summary.get("priceCandidateGroupCounts", {}),
        "site_reference_price": site_reference_price,
        "site_reference_source": site_reference_source,
        "site_reference_kind": site_reference_kind,
        "price_vs_site_delta": site_price_delta,
        "price_vs_site_within_tolerance": site_price_within_tolerance,
        "recon_cache": dict(state.target.bootstrap_signals.get("recon_cache") or {}),
        "recon_refresh_reason": state.target.bootstrap_signals.get("recon_refresh_reason"),
        "drift_report": dict(state.target.bootstrap_signals.get("drift_report") or {}),
        "option_prevalidation": {
            "available": bool(option_prevalidation.get("available", False)),
            "valid": bool(option_prevalidation.get("valid", True)),
            "matchedCount": len(list(option_prevalidation.get("matched") or [])),
            "unmatchedCount": len(list(option_prevalidation.get("unmatched") or [])),
            "warnings": list(option_prevalidation.get("warnings") or []),
            "errors": list(option_prevalidation.get("errors") or []),
            "unmatched": list(option_prevalidation.get("unmatched") or [])[:12],
        },
    }
    return result
