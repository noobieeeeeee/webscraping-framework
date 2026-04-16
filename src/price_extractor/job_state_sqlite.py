from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SQLiteJobState:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self._init_schema()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS unit_state (
                unit_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stage_name TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                result_json TEXT,
                reason TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_unit_state_status ON unit_state(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_unit_state_stage ON unit_state(stage_name)")
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def should_process_unit(self, unit_id: str, retry_failed: bool) -> tuple[bool, str | None]:
        row = self.conn.execute(
            "SELECT status FROM unit_state WHERE unit_id = ?",
            (str(unit_id),),
        ).fetchone()
        if row is None:
            return True, None

        status = str(row[0] or "")
        if status == "completed":
            return False, "already_completed"
        if status == "failed" and not retry_failed:
            return False, "already_failed"
        if status == "skipped":
            return False, "already_skipped"
        return True, None

    def upsert_unit(
        self,
        *,
        unit_id: str,
        status: str,
        stage_name: str,
        spec: dict[str, Any],
        result: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> None:
        payload_spec = json.dumps(dict(spec or {}), ensure_ascii=True, separators=(",", ":"))
        payload_result = (
            json.dumps(dict(result or {}), ensure_ascii=True, separators=(",", ":"))
            if result is not None
            else None
        )
        self.conn.execute(
            """
            INSERT INTO unit_state (unit_id, status, stage_name, spec_json, result_json, reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_id)
            DO UPDATE SET
                status = excluded.status,
                stage_name = excluded.stage_name,
                spec_json = excluded.spec_json,
                result_json = excluded.result_json,
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (
                str(unit_id),
                str(status),
                str(stage_name),
                payload_spec,
                payload_result,
                str(reason or "") or None,
                self._utc_now(),
            ),
        )
        self.conn.commit()

    def mark_completed(self, unit_id: str, stage_name: str, spec: dict[str, Any], summary: dict[str, Any]) -> None:
        self.upsert_unit(
            unit_id=unit_id,
            status="completed",
            stage_name=stage_name,
            spec=spec,
            result=summary,
        )

    def mark_failed(self, unit_id: str, stage_name: str, spec: dict[str, Any], summary: dict[str, Any]) -> None:
        self.upsert_unit(
            unit_id=unit_id,
            status="failed",
            stage_name=stage_name,
            spec=spec,
            result=summary,
        )

    def mark_skipped(self, unit_id: str, stage_name: str, spec: dict[str, Any], reason: str) -> None:
        self.upsert_unit(
            unit_id=unit_id,
            status="skipped",
            stage_name=stage_name,
            spec=spec,
            reason=reason,
        )

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM unit_state GROUP BY status"
        ).fetchall()
        totals: dict[str, int] = {"completed": 0, "failed": 0, "skipped": 0}
        for status, count in rows:
            key = str(status or "")
            if key in totals:
                totals[key] = int(count or 0)
        totals["total"] = totals["completed"] + totals["failed"] + totals["skipped"]
        return totals
