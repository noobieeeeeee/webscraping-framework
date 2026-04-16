from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


DEFAULT_COLUMNS = [
    "unit_id",
    "index",
    "url",
    "status",
    "site",
    "validated",
    "price",
    "currency",
    "replay_mode",
    "fallback_mode",
    "extraction_reason",
    "artifact_dir",
]


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    base = dict(row)
    if isinstance(base.get("summary"), dict):
        summary = dict(base.get("summary") or {})
        base.setdefault("site", summary.get("site"))
        base.setdefault("validated", summary.get("validated"))
        base.setdefault("price", summary.get("price"))
        base.setdefault("currency", summary.get("currency"))
        base.setdefault("replay_mode", summary.get("replay_mode"))
        base.setdefault("fallback_mode", summary.get("fallback_mode"))
        base.setdefault("extraction_reason", summary.get("extraction_reason"))
        base.setdefault("artifact_dir", summary.get("artifact_dir"))
    return base


def export_results_jsonl_to_csv(
    *,
    jsonl_path: str,
    csv_path: str,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    source = Path(jsonl_path)
    if not source.exists():
        raise SystemExit(f"results jsonl not found: {jsonl_path}")

    selected_columns = [str(col).strip() for col in (columns or DEFAULT_COLUMNS) if str(col).strip()]
    if not selected_columns:
        selected_columns = list(DEFAULT_COLUMNS)

    target = Path(csv_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    processed = 0
    with source.open("r", encoding="utf-8") as in_handle:
        with target.open("w", encoding="utf-8", newline="") as out_handle:
            writer = csv.DictWriter(out_handle, fieldnames=selected_columns)
            writer.writeheader()
            for line_no, raw_line in enumerate(in_handle, start=1):
                line = str(raw_line or "").strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except Exception:
                    continue
                if not isinstance(parsed, dict):
                    continue
                flattened = _flatten_row(parsed)
                out_row: dict[str, Any] = {}
                for key in selected_columns:
                    value = flattened.get(key)
                    if isinstance(value, (dict, list)):
                        out_row[key] = json.dumps(value, ensure_ascii=True)
                    else:
                        out_row[key] = value
                writer.writerow(out_row)
                processed += 1

    return {
        "source": str(source),
        "target": str(target),
        "rows": processed,
        "columns": selected_columns,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export per-row JSONL results to CSV")
    parser.add_argument("--jsonl", required=True, help="Path to results JSONL file")
    parser.add_argument("--csv", required=True, help="Path to output CSV file")
    parser.add_argument(
        "--columns",
        help="Comma-separated column names to export. Defaults to a compact analytics-friendly schema.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    columns: list[str] | None = None
    if args.columns:
        columns = [part.strip() for part in str(args.columns).split(",") if part.strip()]

    result = export_results_jsonl_to_csv(
        jsonl_path=str(args.jsonl),
        csv_path=str(args.csv),
        columns=columns,
    )
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
