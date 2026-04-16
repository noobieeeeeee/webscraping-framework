from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from price_extractor.staged_runner import run_staged_rollout


def _base_args(tmp: str, *, policy_file: str) -> SimpleNamespace:
    return SimpleNamespace(
        staged_policy_file=policy_file,
        staged_job_db=str(Path(tmp) / "staged_state.db"),
        staged_auto_approve=False,
        staged_breaker_window=5,
        staged_breaker_min_events=3,
        staged_breaker_block_rate=0.5,
        staged_breaker_server_error_rate=0.6,
        retry_failed=False,
        results_jsonl=None,
        proxy_url=None,
        proxy_file=None,
        request_only=True,
        allow_heuristic_fallback=False,
        http_min_delay_ms=0,
        http_jitter_ms=0,
        http_max_retries=1,
        http_backoff_base_ms=400,
        http_backoff_max_ms=5000,
    )


class TestStagedRunner(unittest.TestCase):
    def test_parallel_stage_respects_max_inflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "name": "probe",
                                "unit_budget": 5,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {
                                    "workers": 4,
                                    "max_inflight": 2,
                                    "max_rps": 100.0,
                                    "jitter_ms": 0,
                                    "http_max_retries": 0,
                                    "http_backoff_base_ms": 0,
                                    "http_backoff_max_ms": 0,
                                },
                                "gate": {
                                    "min_units": 1,
                                    "min_success_rate": 0.0,
                                    "max_block_rate": 1.0,
                                    "max_server_error_rate": 1.0,
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = _base_args(tmp, policy_file=str(policy_path))
            args.staged_auto_approve = True

            source_specs = [
                {"url": f"https://example.test/{idx}", "site_name": "example", "product_type": "brochure", "options": {}}
                for idx in range(5)
            ]

            lock = threading.Lock()
            active = 0
            observed_max_active = 0

            def run_unit(_stage_args: Any, spec: dict[str, Any]) -> dict[str, Any]:
                nonlocal active, observed_max_active
                with lock:
                    active += 1
                    observed_max_active = max(observed_max_active, active)

                time.sleep(0.05)

                with lock:
                    active -= 1

                return {
                    "site": str(spec.get("site_name") or "example"),
                    "url": str(spec.get("url") or ""),
                    "final_status": "completed",
                    "validated": True,
                    "http_replay": {"attempts": [{"status": 200}]},
                }

            out = run_staged_rollout(
                source_specs=source_specs,
                generated_specs=[],
                args=args,
                run_unit=run_unit,
                is_successful=lambda summary: bool(summary.get("validated", False)),
                approve_pilot_promotion=lambda _next_stage: True,
            )
            self.assertEqual(out["status"], "completed")
            self.assertGreaterEqual(observed_max_active, 2)
            self.assertLessEqual(observed_max_active, 2)

    def test_manual_approval_can_pause_after_pilot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "name": "single_config",
                                "unit_budget": 1,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 0.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            },
                            {
                                "name": "pilot",
                                "unit_budget": 1,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 0.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            },
                            {
                                "name": "staging",
                                "unit_budget": 1,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 0.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = _base_args(tmp, policy_file=str(policy_path))

            source_specs = [
                {"url": "https://example.test/a", "site_name": "example", "product_type": "brochure", "options": {}},
                {"url": "https://example.test/b", "site_name": "example", "product_type": "brochure", "options": {}},
            ]

            def run_unit(_stage_args: Any, spec: dict[str, Any]) -> dict[str, Any]:
                return {
                    "site": str(spec.get("site_name") or "example"),
                    "url": str(spec.get("url") or ""),
                    "final_status": "completed",
                    "validated": True,
                    "http_replay": {"attempts": [{"status": 200}]},
                }

            out = run_staged_rollout(
                source_specs=source_specs,
                generated_specs=[],
                args=args,
                run_unit=run_unit,
                is_successful=lambda summary: bool(summary.get("validated", False)),
                approve_pilot_promotion=lambda _next_stage: False,
            )
            self.assertEqual(out["status"], "paused")
            self.assertEqual(out["reason"], "manual_approval_denied")
            stage_rows = list(out.get("stages") or [])
            self.assertTrue(stage_rows)
            self.assertEqual(str(stage_rows[-1].get("stage")), "staging")
            self.assertEqual(str(stage_rows[-1].get("status")), "skipped")

    def test_resume_skips_do_not_fail_gate_or_corrupt_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "name": "probe",
                                "unit_budget": 1,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 1.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            args = _base_args(tmp, policy_file=str(policy_path))
            args.staged_auto_approve = True

            source_specs = [{"url": "https://example.test/a", "site_name": "example", "product_type": "brochure", "options": {}}]
            run_counter = {"count": 0}

            def run_unit(_stage_args: Any, spec: dict[str, Any]) -> dict[str, Any]:
                run_counter["count"] += 1
                return {
                    "site": str(spec.get("site_name") or "example"),
                    "url": str(spec.get("url") or ""),
                    "final_status": "completed",
                    "validated": True,
                    "http_replay": {"attempts": [{"status": 200}]},
                }

            first = run_staged_rollout(
                source_specs=source_specs,
                generated_specs=[],
                args=args,
                run_unit=run_unit,
                is_successful=lambda summary: bool(summary.get("validated", False)),
                approve_pilot_promotion=lambda _next_stage: True,
            )
            self.assertEqual(first["status"], "completed")
            self.assertEqual(run_counter["count"], 1)

            second = run_staged_rollout(
                source_specs=source_specs,
                generated_specs=[],
                args=args,
                run_unit=run_unit,
                is_successful=lambda summary: bool(summary.get("validated", False)),
                approve_pilot_promotion=lambda _next_stage: True,
            )
            self.assertEqual(second["status"], "completed")
            self.assertEqual(run_counter["count"], 1)

            stage_rows = list(second.get("stages") or [])
            self.assertTrue(stage_rows)
            self.assertEqual(stage_rows[0].get("status"), "skipped")

            counts = dict((second.get("checkpoint") or {}).get("counts") or {})
            self.assertEqual(int(counts.get("completed", 0)), 1)
            self.assertEqual(int(counts.get("skipped", 0)), 0)

    def test_production_requires_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "name": "single_config",
                                "unit_budget": 1,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 0.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            },
                            {
                                "name": "production",
                                "unit_budget": 1,
                                "repeat_same_unit": False,
                                "requires_proxy": True,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 0.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = _base_args(tmp, policy_file=str(policy_path))
            args.staged_auto_approve = True

            source_specs = [{"url": "https://example.test/a", "site_name": "example", "product_type": "brochure", "options": {}}]

            def run_unit(_stage_args: Any, spec: dict[str, Any]) -> dict[str, Any]:
                return {
                    "site": str(spec.get("site_name") or "example"),
                    "url": str(spec.get("url") or ""),
                    "final_status": "completed",
                    "validated": True,
                    "http_replay": {"attempts": [{"status": 200}]},
                }

            out = run_staged_rollout(
                source_specs=source_specs,
                generated_specs=[],
                args=args,
                run_unit=run_unit,
                is_successful=lambda summary: bool(summary.get("validated", False)),
                approve_pilot_promotion=lambda _next_stage: True,
            )
            self.assertEqual(out["status"], "failed")
            self.assertEqual(out["reason"], "production_proxy_required")

    def test_breaker_opens_on_block_spike(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "stages": [
                            {
                                "name": "probe",
                                "unit_budget": 5,
                                "repeat_same_unit": False,
                                "requires_proxy": False,
                                "envelope": {"workers": 1, "max_inflight": 1, "max_rps": 1.0, "jitter_ms": 0, "http_max_retries": 1, "http_backoff_base_ms": 100, "http_backoff_max_ms": 500},
                                "gate": {"min_units": 1, "min_success_rate": 0.0, "max_block_rate": 1.0, "max_server_error_rate": 1.0},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            args = _base_args(tmp, policy_file=str(policy_path))
            args.staged_auto_approve = True

            source_specs = [
                {"url": f"https://example.test/{idx}", "site_name": "example", "product_type": "brochure", "options": {}}
                for idx in range(5)
            ]

            def run_unit(_stage_args: Any, spec: dict[str, Any]) -> dict[str, Any]:
                return {
                    "site": str(spec.get("site_name") or "example"),
                    "url": str(spec.get("url") or ""),
                    "final_status": "failed",
                    "validated": False,
                    "mismatches": ["request_error"],
                    "http_replay": {"attempts": [{"status": 429}]},
                }

            out = run_staged_rollout(
                source_specs=source_specs,
                generated_specs=[],
                args=args,
                run_unit=run_unit,
                is_successful=lambda summary: bool(summary.get("validated", False)),
                approve_pilot_promotion=lambda _next_stage: True,
            )
            self.assertEqual(out["status"], "failed")
            self.assertIn("circuit_open", str(out.get("reason") or ""))
            self.assertTrue(bool(dict(out.get("breaker") or {}).get("open", False)))
            stage_rows = list(out.get("stages") or [])
            self.assertTrue(stage_rows)
            self.assertEqual(str(stage_rows[0].get("status")), "failed")
            self.assertIn("circuit_open", str(stage_rows[0].get("reason") or ""))


if __name__ == "__main__":
    unittest.main()
