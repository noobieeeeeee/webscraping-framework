from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SkipDecision:
    should_process: bool
    reason: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(spec.get("url", "")).strip(),
        "site_name": str(spec.get("site_name", "")).strip(),
        "product_type": str(spec.get("product_type", "unknown")).strip(),
        "expected_currency": str(spec.get("expected_currency", "EUR")).strip(),
        "expected_price": spec.get("expected_price"),
        "options": spec.get("options") if isinstance(spec.get("options"), dict) else {},
    }


def build_unit_id(spec: dict[str, Any]) -> str:
    normalized = _normalize_spec(spec)
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"unit_{digest}"


def load_checkpoint(path: str) -> dict[str, Any]:
    checkpoint_path = Path(path)
    if not checkpoint_path.exists():
        return {
            "version": 1,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "completed": {},
            "failed": {},
            "skipped": {},
        }

    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Checkpoint file must contain a JSON object")

    payload.setdefault("version", 1)
    payload.setdefault("created_at", _utc_now())
    payload.setdefault("updated_at", _utc_now())
    payload.setdefault("completed", {})
    payload.setdefault("failed", {})
    payload.setdefault("skipped", {})
    return payload


def save_checkpoint(path: str, checkpoint: dict[str, Any]) -> None:
    checkpoint_path = Path(path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint["updated_at"] = _utc_now()
    checkpoint_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")


def should_process_unit(checkpoint: dict[str, Any], unit_id: str, retry_failed: bool) -> SkipDecision:
    completed = checkpoint.get("completed", {})
    failed = checkpoint.get("failed", {})

    if unit_id in completed:
        return SkipDecision(False, "already_completed")
    if (not retry_failed) and unit_id in failed:
        return SkipDecision(False, "already_failed")
    return SkipDecision(True, None)


def mark_completed(
    checkpoint: dict[str, Any],
    unit_id: str,
    spec: dict[str, Any],
    result_summary: dict[str, Any],
) -> None:
    completed = checkpoint.setdefault("completed", {})
    failed = checkpoint.setdefault("failed", {})
    skipped = checkpoint.setdefault("skipped", {})
    failed.pop(unit_id, None)
    skipped.pop(unit_id, None)
    completed[unit_id] = {
        "updated_at": _utc_now(),
        "spec": _normalize_spec(spec),
        "result": result_summary,
    }


def mark_failed(
    checkpoint: dict[str, Any],
    unit_id: str,
    spec: dict[str, Any],
    result_summary: dict[str, Any],
) -> None:
    failed = checkpoint.setdefault("failed", {})
    skipped = checkpoint.setdefault("skipped", {})
    skipped.pop(unit_id, None)
    failed[unit_id] = {
        "updated_at": _utc_now(),
        "spec": _normalize_spec(spec),
        "result": result_summary,
    }


def mark_skipped(checkpoint: dict[str, Any], unit_id: str, reason: str) -> None:
    skipped = checkpoint.setdefault("skipped", {})
    skipped[unit_id] = {
        "updated_at": _utc_now(),
        "reason": reason,
    }
