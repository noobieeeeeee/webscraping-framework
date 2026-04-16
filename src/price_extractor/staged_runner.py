from __future__ import annotations

import copy
import concurrent.futures
import json
import math
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .job_state import build_unit_id
from .job_state_sqlite import SQLiteJobState


@dataclass(frozen=True)
class StageEnvelope:
    workers: int
    max_inflight: int
    max_rps: float
    jitter_ms: int
    http_max_retries: int
    http_backoff_base_ms: int
    http_backoff_max_ms: int


@dataclass(frozen=True)
class StageGate:
    min_units: int
    min_success_rate: float
    max_block_rate: float
    max_server_error_rate: float


@dataclass(frozen=True)
class StagePolicy:
    name: str
    unit_budget: int
    repeat_same_unit: bool
    requires_proxy: bool
    envelope: StageEnvelope
    gate: StageGate


@dataclass
class BreakerConfig:
    window_size: int = 40
    min_events: int = 10
    trip_block_rate: float = 0.20
    trip_server_error_rate: float = 0.30


class RollingSafetyMonitor:
    def __init__(self, config: BreakerConfig) -> None:
        self.config = config
        self._events: deque[tuple[int, int]] = deque(maxlen=max(1, int(config.window_size)))
        self.open_reason: str | None = None

    @staticmethod
    def _extract_event(summary: dict[str, Any]) -> tuple[int, int]:
        replay = dict(summary.get("http_replay") or {})
        attempts = list(replay.get("attempts") or [])
        blocked = 0
        server_error = 0
        for row in attempts:
            if not isinstance(row, dict):
                continue
            status = int(row.get("status", 0) or 0)
            if status in {403, 429}:
                blocked = 1
            if 500 <= status <= 599:
                server_error = 1
        mismatches = [str(value) for value in list(summary.get("mismatches") or [])]
        if any("request_error" in value for value in mismatches):
            server_error = 1
        return blocked, server_error

    def add_summary(self, summary: dict[str, Any]) -> bool:
        blocked, server_error = self._extract_event(summary)
        self._events.append((blocked, server_error))

        event_count = len(self._events)
        if event_count < max(1, int(self.config.min_events)):
            return False

        blocked_total = sum(blocked for blocked, _ in self._events)
        server_total = sum(server for _, server in self._events)

        blocked_rate = blocked_total / float(event_count)
        server_rate = server_total / float(event_count)

        if blocked_rate >= float(self.config.trip_block_rate):
            self.open_reason = (
                f"circuit_open_block_rate rate={blocked_rate:.3f} threshold={self.config.trip_block_rate:.3f} "
                f"window={event_count}"
            )
            return True

        if server_rate >= float(self.config.trip_server_error_rate):
            self.open_reason = (
                f"circuit_open_server_error_rate rate={server_rate:.3f} threshold={self.config.trip_server_error_rate:.3f} "
                f"window={event_count}"
            )
            return True

        return False


class TokenBucketRateLimiter:
    def __init__(self, rate_per_second: float, capacity: float = 1.0) -> None:
        self.rate_per_second = max(float(rate_per_second or 0.0), 0.1)
        self.capacity = max(float(capacity or 0.0), 1.0)
        self.tokens = self.capacity
        self.last_refill_ts = time.monotonic()

    def wait_for_token(self) -> None:
        while True:
            now = time.monotonic()
            elapsed = max(now - self.last_refill_ts, 0.0)
            self.last_refill_ts = now
            self.tokens = min(self.capacity, self.tokens + (elapsed * self.rate_per_second))
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            needed = (1.0 - self.tokens) / self.rate_per_second
            time.sleep(max(needed, 0.001))


def default_stage_policies() -> list[StagePolicy]:
    conservative = StageEnvelope(
        workers=1,
        max_inflight=2,
        max_rps=0.75,
        jitter_ms=150,
        http_max_retries=1,
        http_backoff_base_ms=500,
        http_backoff_max_ms=5000,
    )
    moderate = StageEnvelope(
        workers=2,
        max_inflight=3,
        max_rps=1.0,
        jitter_ms=200,
        http_max_retries=1,
        http_backoff_base_ms=600,
        http_backoff_max_ms=6000,
    )
    staging = StageEnvelope(
        workers=2,
        max_inflight=4,
        max_rps=2.0,
        jitter_ms=250,
        http_max_retries=1,
        http_backoff_base_ms=700,
        http_backoff_max_ms=7000,
    )
    production = StageEnvelope(
        workers=2,
        max_inflight=5,
        max_rps=3.0,
        jitter_ms=300,
        http_max_retries=1,
        http_backoff_base_ms=800,
        http_backoff_max_ms=8000,
    )

    strict_gate = StageGate(min_units=1, min_success_rate=1.0, max_block_rate=0.0, max_server_error_rate=0.20)
    pilot_gate = StageGate(min_units=10, min_success_rate=0.95, max_block_rate=0.02, max_server_error_rate=0.10)
    stage_gate = StageGate(min_units=20, min_success_rate=0.93, max_block_rate=0.02, max_server_error_rate=0.10)

    return [
        StagePolicy("single_config", unit_budget=3, repeat_same_unit=True, requires_proxy=False, envelope=conservative, gate=strict_gate),
        StagePolicy("probe", unit_budget=20, repeat_same_unit=False, requires_proxy=False, envelope=conservative, gate=pilot_gate),
        StagePolicy("pilot", unit_budget=50, repeat_same_unit=False, requires_proxy=False, envelope=moderate, gate=pilot_gate),
        StagePolicy("staging", unit_budget=1000, repeat_same_unit=False, requires_proxy=False, envelope=staging, gate=stage_gate),
        StagePolicy("production", unit_budget=5000, repeat_same_unit=False, requires_proxy=True, envelope=production, gate=stage_gate),
    ]


def _coerce_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return parsed


def _coerce_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    return parsed


def _build_stage_policy_from_dict(row: dict[str, Any]) -> StagePolicy:
    envelope_data = dict(row.get("envelope") or {})
    gate_data = dict(row.get("gate") or {})

    envelope = StageEnvelope(
        workers=max(_coerce_int(envelope_data.get("workers"), 1), 1),
        max_inflight=max(_coerce_int(envelope_data.get("max_inflight"), 2), 1),
        max_rps=max(_coerce_float(envelope_data.get("max_rps"), 0.75), 0.1),
        jitter_ms=max(_coerce_int(envelope_data.get("jitter_ms"), 150), 0),
        http_max_retries=max(_coerce_int(envelope_data.get("http_max_retries"), 1), 0),
        http_backoff_base_ms=max(_coerce_int(envelope_data.get("http_backoff_base_ms"), 500), 0),
        http_backoff_max_ms=max(_coerce_int(envelope_data.get("http_backoff_max_ms"), 5000), 0),
    )
    gate = StageGate(
        min_units=max(_coerce_int(gate_data.get("min_units"), 1), 1),
        min_success_rate=min(max(_coerce_float(gate_data.get("min_success_rate"), 0.95), 0.0), 1.0),
        max_block_rate=min(max(_coerce_float(gate_data.get("max_block_rate"), 0.05), 0.0), 1.0),
        max_server_error_rate=min(max(_coerce_float(gate_data.get("max_server_error_rate"), 0.20), 0.0), 1.0),
    )

    name = str(row.get("name") or "").strip().lower()
    if not name:
        raise ValueError("stage policy item requires non-empty name")

    return StagePolicy(
        name=name,
        unit_budget=max(_coerce_int(row.get("unit_budget"), 1), 1),
        repeat_same_unit=bool(row.get("repeat_same_unit", False)),
        requires_proxy=bool(row.get("requires_proxy", False)),
        envelope=envelope,
        gate=gate,
    )


def load_stage_policies(policy_file: str | None) -> list[StagePolicy]:
    if not policy_file:
        return default_stage_policies()

    path = Path(policy_file)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("--staged-policy-file must be a JSON object")

    rows = payload.get("stages")
    if not isinstance(rows, list) or not rows:
        raise ValueError("--staged-policy-file must contain {\"stages\": [...]} with at least one stage")

    policies: list[StagePolicy] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each stage item in --staged-policy-file must be an object")
        policies.append(_build_stage_policy_from_dict(row))
    return policies


def _has_proxy_config(args: Any) -> bool:
    if str(getattr(args, "proxy_url", "") or "").strip():
        return True
    if str(getattr(args, "proxy_file", "") or "").strip():
        return True
    return False


def _apply_stage_http_runtime(base_args: Any, stage: StagePolicy) -> Any:
    updated = copy.copy(base_args)
    # Use stage-safe runtime floors/ceilings.
    min_delay_ms_from_rps = int(math.ceil(1000.0 / max(stage.envelope.max_rps, 0.1)))
    updated.http_min_delay_ms = max(int(getattr(base_args, "http_min_delay_ms", 0) or 0), min_delay_ms_from_rps)
    updated.http_jitter_ms = max(int(getattr(base_args, "http_jitter_ms", 0) or 0), int(stage.envelope.jitter_ms))
    updated.http_max_retries = min(int(getattr(base_args, "http_max_retries", 0) or 0), int(stage.envelope.http_max_retries))
    updated.http_backoff_base_ms = max(
        int(getattr(base_args, "http_backoff_base_ms", 0) or 0),
        int(stage.envelope.http_backoff_base_ms),
    )
    updated.http_backoff_max_ms = max(
        int(getattr(base_args, "http_backoff_max_ms", 0) or 0),
        int(stage.envelope.http_backoff_max_ms),
    )
    # Main extraction phase should remain request-only and deterministic.
    updated.request_only = True
    updated.allow_heuristic_fallback = False
    return updated


def _event_from_summary(summary: dict[str, Any], success: bool) -> tuple[int, int, int]:
    replay = dict(summary.get("http_replay") or {})
    attempts = [row for row in list(replay.get("attempts") or []) if isinstance(row, dict)]

    blocked = 0
    server_error = 0
    for row in attempts:
        status = int(row.get("status", 0) or 0)
        if status in {403, 429}:
            blocked = 1
        if 500 <= status <= 599:
            server_error = 1

    if any("request_error" in str(item) for item in list(summary.get("mismatches") or [])):
        server_error = 1

    return (1 if success else 0), blocked, server_error


def _evaluate_stage_gate(stage: StagePolicy, rows: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    executable_rows = [
        row
        for row in rows
        if str(row.get("status", "")).strip().lower() in {"completed", "failed"}
    ]
    skipped_rows = [
        row
        for row in rows
        if str(row.get("status", "")).strip().lower() == "skipped"
    ]

    total = len(executable_rows)
    successes = sum(int(row.get("success", False)) for row in executable_rows)
    blocked = sum(int(row.get("blocked", False)) for row in executable_rows)
    server_errors = sum(int(row.get("server_error", False)) for row in executable_rows)

    if total <= 0:
        metrics = {
            "total": 0,
            "skipped": len(skipped_rows),
            "success_rate": 0.0,
            "block_rate": 0.0,
            "server_error_rate": 0.0,
            "passed": False,
            "reason": "stage_no_executed_units",
        }
        return False, metrics

    success_rate = successes / float(total)
    block_rate = blocked / float(total)
    server_error_rate = server_errors / float(total)

    passed = (
        total >= stage.gate.min_units
        and success_rate >= stage.gate.min_success_rate
        and block_rate <= stage.gate.max_block_rate
        and server_error_rate <= stage.gate.max_server_error_rate
    )

    reason = None
    if not passed:
        if total < stage.gate.min_units:
            reason = f"stage_gate_min_units total={total} required={stage.gate.min_units}"
        elif success_rate < stage.gate.min_success_rate:
            reason = (
                f"stage_gate_success_rate rate={success_rate:.3f} "
                f"required={stage.gate.min_success_rate:.3f}"
            )
        elif block_rate > stage.gate.max_block_rate:
            reason = (
                f"stage_gate_block_rate rate={block_rate:.3f} "
                f"limit={stage.gate.max_block_rate:.3f}"
            )
        else:
            reason = (
                f"stage_gate_server_error_rate rate={server_error_rate:.3f} "
                f"limit={stage.gate.max_server_error_rate:.3f}"
            )

    metrics = {
        "total": total,
        "skipped": len(skipped_rows),
        "successes": successes,
        "blocked": blocked,
        "server_errors": server_errors,
        "success_rate": round(success_rate, 4),
        "block_rate": round(block_rate, 4),
        "server_error_rate": round(server_error_rate, 4),
        "passed": bool(passed),
        "reason": reason,
    }
    return passed, metrics


def _select_stage_units(
    stage: StagePolicy,
    source_specs: list[dict[str, Any]],
    consumed_base_unit_ids: set[str],
) -> list[dict[str, Any]]:
    if not source_specs:
        return []

    if stage.repeat_same_unit:
        first = source_specs[0]
        return [dict(first) for _ in range(max(1, stage.unit_budget))]

    selected: list[dict[str, Any]] = []
    for spec in source_specs:
        base_id = build_unit_id(spec)
        if base_id in consumed_base_unit_ids:
            continue
        selected.append(spec)
        if len(selected) >= stage.unit_budget:
            break
    return [dict(row) for row in selected]


def _append_jsonl_row(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def run_staged_rollout(
    *,
    source_specs: list[dict[str, Any]],
    generated_specs: list[dict[str, Any]] | None,
    args: Any,
    run_unit: Callable[[Any, dict[str, Any]], dict[str, Any]],
    is_successful: Callable[[dict[str, Any]], bool],
    approve_pilot_promotion: Callable[[str], bool],
) -> dict[str, Any]:
    combined_specs = [dict(row) for row in list(source_specs or [])]
    for row in list(generated_specs or []):
        if isinstance(row, dict):
            combined_specs.append(dict(row))

    if not combined_specs:
        raise ValueError("staged rollout requires at least one source unit")

    policies = load_stage_policies(getattr(args, "staged_policy_file", None))
    breaker = RollingSafetyMonitor(
        BreakerConfig(
            window_size=max(int(getattr(args, "staged_breaker_window", 40) or 40), 1),
            min_events=max(int(getattr(args, "staged_breaker_min_events", 10) or 10), 1),
            trip_block_rate=min(max(float(getattr(args, "staged_breaker_block_rate", 0.20) or 0.20), 0.0), 1.0),
            trip_server_error_rate=min(max(float(getattr(args, "staged_breaker_server_error_rate", 0.30) or 0.30), 0.0), 1.0),
        )
    )

    job_state = SQLiteJobState(str(getattr(args, "staged_job_db", ".data/checkpoints/staged_job_state.db")))
    consumed_base_unit_ids: set[str] = set()
    stage_reports: list[dict[str, Any]] = []
    post_pilot_approved = bool(getattr(args, "staged_auto_approve", False))
    results_jsonl_path = Path(str(args.results_jsonl)) if getattr(args, "results_jsonl", None) else None

    started_at = time.perf_counter()
    final_status = "completed"
    final_reason = None

    try:
        for stage in policies:
            if stage.name in {"staging", "production"} and not post_pilot_approved:
                approved = bool(approve_pilot_promotion(stage.name))
                if not approved:
                    final_status = "paused"
                    final_reason = "manual_approval_denied"
                    stage_reports.append(
                        {
                            "stage": stage.name,
                            "policy": asdict(stage),
                            "status": "skipped",
                            "reason": final_reason,
                            "metrics": {},
                        }
                    )
                    break
                post_pilot_approved = True

            if stage.requires_proxy and not _has_proxy_config(args):
                final_status = "failed"
                final_reason = "production_proxy_required"
                stage_reports.append(
                    {
                        "stage": stage.name,
                        "policy": asdict(stage),
                        "status": "skipped",
                        "reason": final_reason,
                        "metrics": {},
                    }
                )
                break

            stage_specs = _select_stage_units(stage, combined_specs, consumed_base_unit_ids)
            if not stage_specs:
                stage_reports.append(
                    {
                        "stage": stage.name,
                        "policy": asdict(stage),
                        "status": "skipped",
                        "reason": "no_eligible_units",
                        "metrics": {},
                    }
                )
                continue

            stage_args = _apply_stage_http_runtime(args, stage)
            stage_rows: list[dict[str, Any]] = []
            stage_started_at = time.perf_counter()
            max_workers = max(1, min(int(stage.envelope.workers), int(stage.envelope.max_inflight)))
            max_inflight = max(1, int(stage.envelope.max_inflight))
            limiter = TokenBucketRateLimiter(rate_per_second=float(stage.envelope.max_rps), capacity=1.0)

            next_spec_index = 0
            pending: dict[concurrent.futures.Future[dict[str, Any]], tuple[str, str, dict[str, Any], str]] = {}
            stop_submissions = False

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                while ((not stop_submissions) and next_spec_index < len(stage_specs)) or pending:
                    while (
                        (not stop_submissions)
                        and next_spec_index < len(stage_specs)
                        and len(pending) < max_inflight
                    ):
                        idx = next_spec_index + 1
                        spec = dict(stage_specs[next_spec_index])
                        next_spec_index += 1

                        base_unit_id = build_unit_id(spec)
                        stage_unit_id = (
                            f"{base_unit_id}::{stage.name}::{idx}"
                            if stage.repeat_same_unit
                            else f"{base_unit_id}::{stage.name}"
                        )

                        should_process, skip_reason = job_state.should_process_unit(
                            stage_unit_id,
                            bool(getattr(args, "retry_failed", False)),
                        )
                        if not should_process:
                            # Keep existing checkpoint state intact (do not overwrite completed/failed).
                            skip_row = {
                                "unit_id": stage_unit_id,
                                "stage": stage.name,
                                "status": "skipped",
                                "reason": skip_reason or "already_processed",
                            }
                            stage_rows.append({"success": False, "blocked": False, "server_error": False, **skip_row})
                            _append_jsonl_row(results_jsonl_path, skip_row)
                            continue

                        limiter.wait_for_token()
                        spec_with_meta = dict(spec)
                        spec_with_meta["_staged_unit_id"] = stage_unit_id

                        future = pool.submit(run_unit, stage_args, spec_with_meta)
                        pending[future] = (stage_unit_id, base_unit_id, spec, stage.name)

                        if not stage.repeat_same_unit:
                            consumed_base_unit_ids.add(base_unit_id)

                    if not pending:
                        continue

                    done, _pending = concurrent.futures.wait(
                        set(pending.keys()),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )

                    for finished in done:
                        stage_unit_id, base_unit_id, original_spec, stage_name = pending.pop(finished)
                        try:
                            summary = finished.result()
                        except Exception as exc:  # pragma: no cover - defensive boundary
                            summary = {
                                "site": str(original_spec.get("site_name") or "unknown"),
                                "url": str(original_spec.get("url") or ""),
                                "product_url": str(original_spec.get("url") or ""),
                                "final_status": "failed",
                                "validated": False,
                                "feasible": False,
                                "exception": str(exc),
                                "mismatches": ["request_error"],
                                "http_replay": {"attempts": []},
                            }

                        success = bool(is_successful(summary))
                        if success:
                            job_state.mark_completed(stage_unit_id, stage_name, original_spec, summary)
                        else:
                            job_state.mark_failed(stage_unit_id, stage_name, original_spec, summary)

                        event_success, blocked, server_error = _event_from_summary(summary, success)
                        unit_row = {
                            "unit_id": stage_unit_id,
                            "stage": stage_name,
                            "status": "completed" if success else "failed",
                            "success": bool(event_success),
                            "blocked": bool(blocked),
                            "server_error": bool(server_error),
                            "summary": summary,
                        }
                        stage_rows.append(unit_row)

                        _append_jsonl_row(
                            results_jsonl_path,
                            {
                                "unit_id": stage_unit_id,
                                "stage": stage_name,
                                "status": "completed" if success else "failed",
                                "site": str(summary.get("site") or ""),
                                "url": str(summary.get("url") or summary.get("product_url") or ""),
                                "validated": bool(summary.get("validated", False)),
                                "price": summary.get("price"),
                                "currency": summary.get("currency"),
                                "replay_mode": summary.get("replay_mode"),
                                "fallback_mode": summary.get("fallback_mode"),
                                "mismatches": list(summary.get("mismatches") or []),
                            },
                        )

                        if breaker.add_summary(summary):
                            final_status = "failed"
                            final_reason = str(breaker.open_reason or "circuit_open")
                            stop_submissions = True
                            # Keep draining already-submitted futures, but submit no new work.

                    if final_status != "completed" and not pending:
                        break

            if stage_rows and not any(
                str(row.get("status", "")).strip().lower() in {"completed", "failed"}
                for row in stage_rows
            ):
                stage_reports.append(
                    {
                        "stage": stage.name,
                        "policy": asdict(stage),
                        "status": "skipped",
                        "reason": "already_processed",
                        "metrics": {
                            "total": 0,
                            "skipped": len(stage_rows),
                            "passed": True,
                        },
                        "elapsed_seconds": round(max(time.perf_counter() - stage_started_at, 0.0), 3),
                    }
                )
                continue

            passed, metrics = _evaluate_stage_gate(stage, stage_rows)
            stage_failed_due_to_run_state = final_status != "completed"
            stage_status = "completed"
            stage_reason: str | None = None
            if (not passed) or stage_failed_due_to_run_state:
                stage_status = "failed"
                if stage_failed_due_to_run_state:
                    stage_reason = str(final_reason or metrics.get("reason") or "stage_failed")
                else:
                    stage_reason = str(metrics.get("reason") or "stage_failed")
            stage_reports.append(
                {
                    "stage": stage.name,
                    "policy": asdict(stage),
                    "status": stage_status,
                    "reason": stage_reason,
                    "metrics": metrics,
                    "elapsed_seconds": round(max(time.perf_counter() - stage_started_at, 0.0), 3),
                }
            )

            if final_status != "completed":
                break

            if not passed:
                final_status = "failed"
                final_reason = str(metrics.get("reason") or "stage_gate_failed")
                break

        elapsed = round(max(time.perf_counter() - started_at, 0.0), 3)
        return {
            "run_type": "staged_rollout",
            "status": final_status,
            "reason": final_reason,
            "elapsed_seconds": elapsed,
            "stages": stage_reports,
            "checkpoint": {
                "backend": "sqlite",
                "path": str(getattr(args, "staged_job_db", "")),
                "counts": job_state.counts(),
            },
            "breaker": {
                "open": bool(breaker.open_reason),
                "reason": breaker.open_reason,
            },
            "results_jsonl": str(results_jsonl_path) if results_jsonl_path is not None else None,
            "source_counts": {
                "manifest_units": len(list(source_specs or [])),
                "generated_units": len(list(generated_specs or [])),
                "combined_units": len(combined_specs),
            },
        }
    finally:
        job_state.close()
