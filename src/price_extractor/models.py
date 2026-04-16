from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .bootstrap_schema import BootstrapArtifacts


class Complexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Strategy(str, Enum):
    STATIC_HTML = "static_html"
    DIRECT_HTTP = "direct_http"
    BROWSER_AUTOMATION = "browser_automation"
    HYBRID = "hybrid"


class EndpointRole(str, Enum):
    SCHEMA = "schema"
    PRICING = "pricing"
    QUANTITY_MATRIX = "quantity_matrix"
    DELIVERY = "delivery"
    VALIDATION = "validation"
    NOISE = "noise"
    UNKNOWN = "unknown"


@dataclass
class TargetInput:
    site_name: str
    product_url: str
    product_type: str
    observed_requests: list[str] = field(default_factory=list)
    expected_price: float | None = None
    expected_price_tolerance: float = 0.01
    expected_currency: str = "EUR"
    options: dict[str, Any] = field(default_factory=dict)
    network_traces: list[dict[str, Any]] = field(default_factory=list)
    bootstrap_signals: BootstrapArtifacts = field(default_factory=dict)


@dataclass
class ObservationBundle:
    has_script_heavy_ui: bool
    has_api_calls: bool
    requires_session: bool
    token_indicators: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    endpoint_candidates: list[str] = field(default_factory=list)
    endpoint_roles: dict[str, str] = field(default_factory=dict)
    role_confidence: dict[str, float] = field(default_factory=dict)
    payload_signal_score: int = 0
    response_signal_score: int = 0
    quantity_behavior_hint: str = "unknown"
    option_group_count: int = 0
    dependency_edge_count: int = 0
    active_dependency_edge_count: int = 0
    has_quantity_threshold_behavior: bool = False
    has_manual_quantity_input: bool = False
    has_preset_quantities: bool = False
    endpoint_rankings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FeasibilityResult:
    feasible: bool
    complexity: Complexity
    recommended_strategy: Strategy
    rationale: str


@dataclass
class StrategyPlan:
    strategy: Strategy
    endpoint: str | None
    payload_template: dict[str, Any]
    confidence: float
    notes: str


@dataclass
class ExtractionResult:
    success: bool
    accepted_configuration: dict[str, Any]
    price_value: float | None
    currency: str | None
    price_matrix: list[dict[str, Any]] = field(default_factory=list)
    shipping_variants: list[dict[str, Any]] = field(default_factory=list)
    raw_response_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    is_valid: bool
    mismatches: list[str] = field(default_factory=list)
    inferred_failure_reason: str | None = None


@dataclass
class FailureRecord:
    reason: str
    strategy: Strategy
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunState:
    target: TargetInput
    observation: ObservationBundle | None = None
    feasibility: FeasibilityResult | None = None
    plan: StrategyPlan | None = None
    extraction: ExtractionResult | None = None
    validation: ValidationResult | None = None
    failures: list[FailureRecord] = field(default_factory=list)
    attempt_count: int = 0
    max_attempts: int = 3


@dataclass
class KnowledgeRecord:
    site_name: str
    endpoint: str
    strategy: Strategy
    success: bool
    failure_reason: str | None = None
