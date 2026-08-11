#!/usr/bin/env python3
"""Write ETG Bm sidecars from a WorldReasoner Polymarket snapshot.

The WorldReasoner database stays read-only. This exporter uses the ETG market
resolver so every record carries a pre-cutoff Yes-token quote and its full
provenance. It requires this repository layout because the resolver is shared
rather than copied.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts" / "event-thinking-graph"))

from etg_bench import market_prices  # noqa: E402

SLOT_FRACTIONS = {"early": 0.20, "mid": 0.50, "late": 0.80}


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def cutoff_for(row: sqlite3.Row, slot: str) -> tuple[datetime | None, str]:
    resolution = parse_datetime(row["resolution_date"])
    if resolution is None:
        return None, "missing_resolution_date"
    end = resolution - timedelta(seconds=1)
    start = parse_datetime(row["estimated_start_time"])
    branch = "estimated_start_time"
    if start is None or start >= end:
        start = end - timedelta(days=30)
        branch = "resolution_minus_30d_fallback"
    if start > end - timedelta(days=7):
        start = end - timedelta(days=7)
        branch = f"{branch}_expanded_to_7d"
    return start + (end - start) * SLOT_FRACTIONS[slot], branch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="WorldReasoner SQLite snapshot, read-only")
    parser.add_argument("--out", required=True, help="ETG Bm JSONL sidecar")
    parser.add_argument("--summary-out", required=True)
    parser.add_argument("--slot", choices=sorted(SLOT_FRACTIONS), default="mid")
    parser.add_argument("--min-interval", type=float, default=0.35)
    arguments = parser.parse_args()

    database = Path(arguments.db)
    if not database.is_file():
        raise SystemExit(f"database not found: {database}")
    if Path(arguments.out).resolve() == database.resolve():
        raise SystemExit("refusing to write Bm records into the WorldReasoner database")

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, estimated_start_time, resolution_date, metadata FROM questions "
            "WHERE source = 'polymarket'"
        ).fetchall()
    finally:
        connection.close()

    limiter = market_prices.RateLimiter(arguments.min_interval)
    records: list[dict] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        cutoff, branch = cutoff_for(row, arguments.slot)
        if cutoff is None:
            record = {
                "question_id": row["id"],
                "status": "missing_resolution_date",
                "cutoff": None,
                "cutoff_branch": branch,
            }
        else:
            record = market_prices.resolve_for_member(
                row["id"],
                str(metadata.get("gamma_market_id")) if metadata.get("gamma_market_id") else None,
                metadata.get("market_slug"),
                cutoff,
                limiter=limiter,
            ).to_json()
            record["cutoff_branch"] = branch
        records.append(record)

    records.sort(key=lambda record: record["question_id"])
    output = Path(arguments.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    statuses = Counter(record["status"] for record in records)
    summary = {
        "source_db": str(database),
        "slot": arguments.slot,
        "records": len(records),
        "priced": sum(record.get("price") is not None for record in records),
        "status_counts": dict(sorted(statuses.items())),
    }
    Path(arguments.summary_out).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
