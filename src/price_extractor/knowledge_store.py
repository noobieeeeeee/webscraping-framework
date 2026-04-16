from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from .models import KnowledgeRecord, Strategy


class KnowledgeStore:
    def __init__(self, db_path: str = ".data/knowledge.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_name TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                strategy TEXT NOT NULL,
                success INTEGER NOT NULL,
                failure_reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_normalizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_name TEXT NOT NULL,
                option_key TEXT NOT NULL,
                requested_value_norm TEXT NOT NULL,
                accepted_value TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1,
                learned_from_match INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(site_name, option_key, requested_value_norm)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS recon_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cache_key TEXT NOT NULL UNIQUE,
                site_name TEXT NOT NULL,
                product_url TEXT NOT NULL,
                bootstrap_artifacts_json TEXT NOT NULL,
                captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS replay_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_name TEXT NOT NULL,
                template_kind TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                template_json TEXT NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(site_name, template_kind, endpoint)
            )
            """
        )
        existing_columns = {
            str(row[1]).lower()
            for row in cur.execute("PRAGMA table_info(option_normalizations)").fetchall()
        }
        if "learned_from_match" not in existing_columns:
            cur.execute(
                "ALTER TABLE option_normalizations ADD COLUMN learned_from_match INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.commit()

    def save_replay_template(
        self,
        site_name: str,
        template_kind: str,
        endpoint: str,
        template: dict[str, object],
    ) -> None:
        kind = str(template_kind or "").strip()
        endpoint_text = str(endpoint or "").strip()
        if not str(site_name or "").strip() or not kind or not endpoint_text:
            return

        payload_json = json.dumps(template or {}, ensure_ascii=True)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO replay_templates (site_name, template_kind, endpoint, template_json, hit_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(site_name, template_kind, endpoint)
            DO UPDATE SET
                template_json = excluded.template_json,
                hit_count = CASE
                    WHEN replay_templates.template_json = excluded.template_json
                        THEN replay_templates.hit_count + 1
                    ELSE 1
                END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(site_name), kind, endpoint_text, payload_json),
        )
        self.conn.commit()

    def get_site_replay_templates(self, site_name: str) -> list[dict[str, object]]:
        cur = self.conn.cursor()
        rows = cur.execute(
            """
            SELECT template_kind, endpoint, template_json, hit_count
            FROM replay_templates
            WHERE site_name = ?
            ORDER BY hit_count DESC, id DESC
            """,
            (str(site_name),),
        ).fetchall()

        results: list[dict[str, object]] = []
        for template_kind, endpoint, payload_json, hit_count in rows:
            try:
                payload = json.loads(str(payload_json or "{}"))
            except Exception:
                payload = {}
            results.append(
                {
                    "templateKind": str(template_kind or ""),
                    "endpoint": str(endpoint or ""),
                    "template": payload if isinstance(payload, dict) else {},
                    "hitCount": int(hit_count or 0),
                }
            )
        return results

    @staticmethod
    def _normalize_value(value: object) -> str:
        return str(value).strip().lower()

    @staticmethod
    def _recon_cache_key(product_url: str) -> str:
        parsed = urlparse(str(product_url or "").strip())
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path or "/"
        normalized_path = path.rstrip("/") or "/"
        return f"{host}{normalized_path}" if host else normalized_path

    def save_record(self, record: KnowledgeRecord) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO knowledge_runs (site_name, endpoint, strategy, success, failure_reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.site_name,
                record.endpoint,
                record.strategy.value,
                int(record.success),
                record.failure_reason,
            ),
        )
        self.conn.commit()

    def get_site_success_endpoints(self, site_name: str) -> list[tuple[str, Strategy]]:
        cur = self.conn.cursor()
        rows = cur.execute(
            """
            SELECT endpoint, strategy
            FROM knowledge_runs
            WHERE site_name = ? AND success = 1
            ORDER BY id DESC
            LIMIT 10
            """,
            (site_name,),
        ).fetchall()
        return [(endpoint, Strategy(strategy)) for endpoint, strategy in rows]

    def save_option_normalizations(
        self,
        site_name: str,
        mappings: list[tuple[str, object, object, bool]],
    ) -> None:
        if not mappings:
            return

        cur = self.conn.cursor()
        for option_key, requested_value, accepted_value, learned_from_match in mappings:
            if option_key is None:
                continue
            requested_norm = self._normalize_value(requested_value)
            accepted_text = str(accepted_value).strip()
            if not requested_norm or not accepted_text:
                continue

            cur.execute(
                """
                INSERT INTO option_normalizations (site_name, option_key, requested_value_norm, accepted_value, hit_count, learned_from_match)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(site_name, option_key, requested_value_norm)
                DO UPDATE SET
                    accepted_value = excluded.accepted_value,
                    hit_count = CASE
                        WHEN option_normalizations.accepted_value = excluded.accepted_value
                            THEN option_normalizations.hit_count + 1
                        ELSE 1
                    END,
                    learned_from_match = CASE
                        WHEN option_normalizations.learned_from_match = 1 OR excluded.learned_from_match = 1 THEN 1
                        ELSE 0
                    END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    site_name,
                    str(option_key),
                    requested_norm,
                    accepted_text,
                    int(bool(learned_from_match)),
                ),
            )
        self.conn.commit()

    def get_site_option_normalizations(self, site_name: str, trusted_only: bool = True) -> dict[str, dict[str, str]]:
        cur = self.conn.cursor()
        query = (
            """
            SELECT option_key, requested_value_norm, accepted_value
            FROM option_normalizations
            WHERE site_name = ?
            """
        )
        if trusted_only:
            query += " AND learned_from_match = 1"
        query += " ORDER BY hit_count DESC, id DESC"
        rows = cur.execute(
            query,
            (site_name,),
        ).fetchall()

        grouped: dict[str, dict[str, str]] = defaultdict(dict)
        for option_key, requested_norm, accepted_value in rows:
            grouped[str(option_key)][str(requested_norm)] = str(accepted_value)
        return dict(grouped)

    def get_site_option_normalization_rows(self, site_name: str) -> list[dict[str, object]]:
        cur = self.conn.cursor()
        rows = cur.execute(
            """
            SELECT option_key, requested_value_norm, accepted_value, hit_count, learned_from_match
            FROM option_normalizations
            WHERE site_name = ?
            ORDER BY hit_count DESC, id DESC
            """,
            (site_name,),
        ).fetchall()
        return [
            {
                "optionKey": str(option_key),
                "requestedValueNorm": str(requested_value_norm),
                "acceptedValue": str(accepted_value),
                "hitCount": int(hit_count or 0),
                "trusted": bool(int(learned_from_match or 0)),
            }
            for option_key, requested_value_norm, accepted_value, hit_count, learned_from_match in rows
        ]

    def promote_option_normalizations(self, site_name: str, promotions: list[tuple[str, str]]) -> None:
        if not promotions:
            return

        cur = self.conn.cursor()
        for option_key, requested_value_norm in promotions:
            if not option_key or not requested_value_norm:
                continue
            cur.execute(
                """
                UPDATE option_normalizations
                SET learned_from_match = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE site_name = ?
                  AND option_key = ?
                  AND requested_value_norm = ?
                """,
                (site_name, str(option_key), str(requested_value_norm)),
            )
        self.conn.commit()

    def save_recon_snapshot(
        self,
        site_name: str,
        product_url: str,
        bootstrap_artifacts: dict[str, object],
        ttl_days: int = 7,
    ) -> None:
        ttl = max(int(ttl_days or 7), 1)
        cache_key = self._recon_cache_key(product_url)
        payload_json = json.dumps(bootstrap_artifacts or {}, ensure_ascii=True)
        ttl_modifier = f"+{ttl} days"

        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO recon_snapshots (
                cache_key,
                site_name,
                product_url,
                bootstrap_artifacts_json,
                captured_at,
                expires_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?,
                CURRENT_TIMESTAMP,
                datetime('now', ?),
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(cache_key)
            DO UPDATE SET
                site_name = excluded.site_name,
                product_url = excluded.product_url,
                bootstrap_artifacts_json = excluded.bootstrap_artifacts_json,
                captured_at = CURRENT_TIMESTAMP,
                expires_at = datetime('now', ?),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                cache_key,
                str(site_name),
                str(product_url),
                payload_json,
                ttl_modifier,
                ttl_modifier,
            ),
        )
        self.conn.commit()

    def get_recon_snapshot(self, product_url: str) -> dict[str, object] | None:
        cache_key = self._recon_cache_key(product_url)
        cur = self.conn.cursor()
        row = cur.execute(
            """
            SELECT site_name, product_url, bootstrap_artifacts_json, captured_at, expires_at
            FROM recon_snapshots
            WHERE cache_key = ?
              AND datetime(expires_at) >= datetime('now')
            ORDER BY id DESC
            LIMIT 1
            """,
            (cache_key,),
        ).fetchone()
        if row is None:
            return None

        site_name, stored_url, payload_json, captured_at, expires_at = row
        try:
            payload = json.loads(str(payload_json or "{}"))
        except Exception:
            payload = {}

        return {
            "cache_key": cache_key,
            "site_name": str(site_name or ""),
            "product_url": str(stored_url or product_url),
            "bootstrap_artifacts": payload if isinstance(payload, dict) else {},
            "captured_at": str(captured_at or ""),
            "expires_at": str(expires_at or ""),
        }

    def close(self) -> None:
        self.conn.close()
