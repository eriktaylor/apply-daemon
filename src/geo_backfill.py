"""One-time backfill of ``listings.distance_bucket`` (D-3).

``process_queue._resolve_bucket`` computes the bucket lazily — only for rows
autopilot actually considers — so most of the queue has none. With
``AUTOPILOT_TOP_N=3`` that gap closes at three rows per run, which is far too
slow to make location a usable sort key.

This walks the reviewable backlog, geocodes each *distinct* location once,
and persists the bucket. No LLM, no tokens: Nominatim only, which is free and
rate-limited to ~1 request/second. Distinct locations are heavily clustered
in practice (one metro dominates), so the wall-clock cost is a couple of
minutes at most.

Usage:
    python -m src.geo_backfill              # backfill reviewable rows
    python -m src.geo_backfill --all        # every row, any status
    python -m src.geo_backfill --dry-run    # show what would be geocoded
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from src.db import REVIEW_STATUSES, Database
from src.process_queue import _compute_distance_bucket

logger = logging.getLogger(__name__)

# Nominatim's usage policy is 1 request/second. geo.py caches per location
# string, so this only sleeps on genuine cache misses.
_RATE_LIMIT_SECONDS = 1.1

_BUCKET_LABELS = {0: "Remote", 1: "Local (<=30mi)", 2: "Commute (<=60mi)",
                  3: "Relocation / unknown"}


def pending_locations(db: Database, *, all_rows: bool = False) -> dict[str, list[str]]:
    """Map location string → listing ids still missing a distance_bucket."""
    if all_rows:
        sql = "SELECT id, location FROM listings WHERE distance_bucket IS NULL"
        params: list = []
    else:
        placeholders = ", ".join("?" for _ in REVIEW_STATUSES)
        sql = (
            "SELECT id, location FROM listings "
            f"WHERE pipeline_status IN ({placeholders}) "
            "AND verdict IN ('YES', 'MAYBE') "
            "AND distance_bucket IS NULL"
        )
        params = list(REVIEW_STATUSES)

    groups: dict[str, list[str]] = {}
    for row in db.conn.execute(sql, params):
        groups.setdefault(row["location"] or "", []).append(row["id"])
    return groups


def backfill(db: Database, *, all_rows: bool = False, dry_run: bool = False,
             sleep_seconds: float = _RATE_LIMIT_SECONDS) -> dict:
    """Geocode each distinct pending location once and persist the buckets."""
    groups = pending_locations(db, all_rows=all_rows)
    total_rows = sum(len(ids) for ids in groups.values())
    logger.info(
        "Backfill: %d distinct location(s) covering %d listing(s)",
        len(groups), total_rows,
    )
    if dry_run:
        for loc, ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(ids):>4}  {loc[:60] or '(blank)'}")
        return {"locations": len(groups), "rows": total_rows, "updated": 0}

    updated = 0
    counts: dict[int, int] = {}
    for i, (location, ids) in enumerate(
        sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1
    ):
        # A blank location is genuinely unknown — bucket 3, no network call.
        bucket = _compute_distance_bucket(location) if location else 3
        for job_id in ids:
            db.set_distance_bucket(job_id, bucket)
            updated += 1
        counts[bucket] = counts.get(bucket, 0) + len(ids)
        logger.info(
            "[%d/%d] %-40s → %s (%d row(s))",
            i, len(groups), location[:40] or "(blank)",
            _BUCKET_LABELS.get(bucket, bucket), len(ids),
        )
        if location and sleep_seconds:
            time.sleep(sleep_seconds)

    return {
        "locations": len(groups), "rows": total_rows,
        "updated": updated, "by_bucket": counts,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m src.geo_backfill",
        description="Backfill distance_bucket so the review queue can sort by location.",
    )
    parser.add_argument("--all", action="store_true",
                        help="Backfill every row, not just reviewable ones")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the locations that would be geocoded, then exit")
    args = parser.parse_args()

    with Database() as db:
        result = backfill(db, all_rows=args.all, dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n  {result['locations']} distinct location(s), "
              f"{result['rows']} listing(s) — nothing written.")
        return 0

    print(f"\n  Updated {result['updated']} listing(s) "
          f"across {result['locations']} location(s).")
    for bucket, n in sorted(result.get("by_bucket", {}).items()):
        print(f"    {_BUCKET_LABELS.get(bucket, bucket):<22} {n:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
