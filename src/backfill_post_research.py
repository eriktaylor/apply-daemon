"""One-time backfill of the post-research columns from ``auto_assets.json`` (R-4).

Autopilot has always written its post-research re-score to
``output/<folder>/auto_assets.json`` and rendered it on the Slack card, but
never to the ``listings`` row. Every listing enriched before that write
existed therefore still shows and sorts by the pre-research Stage 5 score:
212 of 213 enriched rows disagreed with their own re-score, and 106 of 172
``status='auto'`` rows sat at exactly ``confidence=95`` — a band tie the feed
could not order.

This walks the asset folders, reads the re-score each one archived, and
copies it into ``post_research_verdict`` / ``post_research_confidence``.
Stage 5's own columns are never touched: the disagreement between the two is
what ``deep-dive`` reports.

**The JSON stays the source of truth.** ``eval.listwise_compare.load_gold``
reads these same files, deliberately, and must keep doing so — if the DB ever
became the gold source, a backfill would silently re-baseline every eval
number. This module only ever copies JSON → DB, never the reverse.

Dry-run is the default, and a dry run cannot write: it opens the database
through a read-only URI, so not even the schema migration runs.

Usage:
    python -m src.backfill_post_research                    # dry run
    python -m src.backfill_post_research --apply            # write
    python -m src.backfill_post_research --db path --output-dir path
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from src.db import Database, resolve_db_path
from src.file_utils import find_output_folder
from src.listing_card import parse_post_research

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path("output")
AUTO_ASSETS_FILE = "auto_assets.json"

# Statuses worth backfilling: everything a review surface can still show or
# rank. A passed row's re-score changes nothing anyone looks at.
BACKFILL_STATUSES = ("auto", "auto_queued", "triaged", "saved", "tailored")


@dataclass(frozen=True)
class Change:
    """One row's pending update. ``delta`` is post-research minus Stage 5."""

    job_id: str
    title: str
    company: str
    status: str
    stage5_verdict: str | None
    stage5_confidence: int | None
    verdict: str | None
    confidence: int | None

    @property
    def delta(self) -> int | None:
        if self.confidence is None or self.stage5_confidence is None:
            return None
        return self.confidence - self.stage5_confidence

    @property
    def leaves_the_feed(self) -> bool:
        """True when the re-score demotes a reviewable row out of the queue.

        The feed gates on the *effective* verdict, so backfilling a NO onto a
        row still sitting in `auto` removes it from review. That is correct —
        autopilot would have auto-passed it had the write existed — but it is
        the one outcome worth counting before writing.
        """
        return self.verdict == "NO" and self.status in ("auto", "auto_queued", "triaged")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the store read-only, so a dry run provably cannot write.

    ``Database()`` runs its additive migrations on connect, which is a write.
    A dry run must not do that to a database the user has not agreed to
    change, so the scan takes its own URI connection instead.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_post_research_columns(conn: sqlite3.Connection) -> bool:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    return "post_research_verdict" in cols


def scan_assets(output_dir: Path) -> tuple[dict[str, dict], int]:
    """Map folder name → normalized re-score, plus a count of unusable files."""
    found: dict[str, dict] = {}
    unreadable = 0
    if not output_dir.exists():
        return found, unreadable
    for path in sorted(output_dir.glob(f"*/{AUTO_ASSETS_FILE}")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unreadable += 1
            logger.debug("Unreadable %s in %s", AUTO_ASSETS_FILE, path.parent.name)
            continue
        post = parse_post_research(data)
        if post is None or (post["verdict"] is None and post["confidence"] is None):
            unreadable += 1
            continue
        found[path.parent.name] = post
    return found, unreadable


def plan(conn: sqlite3.Connection, output_dir: Path) -> dict:
    """Work out what would change, touching nothing."""
    assets, unreadable = scan_assets(output_dir)
    has_columns = _has_post_research_columns(conn)

    placeholders = ", ".join("?" for _ in BACKFILL_STATUSES)
    columns = "id, title, company, verdict, confidence, pipeline_status"
    if has_columns:
        columns += ", post_research_verdict, post_research_confidence"
    rows = conn.execute(
        f"SELECT {columns} FROM listings "
        f"WHERE pipeline_status IN ({placeholders})",
        list(BACKFILL_STATUSES),
    ).fetchall()

    changes: list[Change] = []
    already: int = 0
    no_assets: int = 0
    claimed: set[str] = set()
    for row in rows:
        folder = find_output_folder(row["id"], output_dir)
        post = assets.get(folder.name) if folder else None
        if post is None:
            no_assets += 1
            continue
        claimed.add(folder.name)
        if has_columns and (
            row["post_research_verdict"] == post["verdict"]
            and row["post_research_confidence"] == post["confidence"]
        ):
            already += 1
            continue
        changes.append(Change(
            job_id=row["id"],
            title=row["title"] or "",
            company=row["company"] or "",
            status=row["pipeline_status"] or "",
            stage5_verdict=row["verdict"],
            stage5_confidence=row["confidence"],
            verdict=post["verdict"],
            confidence=post["confidence"],
        ))

    return {
        "changes": changes,
        "asset_folders": len(assets),
        "unreadable_assets": unreadable,
        "rows_considered": len(rows),
        "rows_without_assets": no_assets,
        "already_current": already,
        "orphan_assets": len(assets) - len(claimed),
        "has_columns": has_columns,
    }


def apply(db: Database, changes: list[Change]) -> int:
    """Write the planned changes. Returns the number of rows updated."""
    written = 0
    for change in changes:
        if db.set_post_research_score(change.job_id, change.verdict, change.confidence):
            written += 1
        else:
            logger.warning("Backfill: %s vanished before write", change.job_id[:8])
    return written


def _print_summary(result: dict, sample: int) -> None:
    changes: list[Change] = result["changes"]
    demotions = sorted(
        (c for c in changes if c.delta is not None and c.delta < 0),
        key=lambda c: c.delta or 0,
    )
    promotions = [c for c in changes if c.delta is not None and c.delta > 0]
    unchanged_score = [c for c in changes if c.delta == 0]
    leaving = [c for c in changes if c.leaves_the_feed]

    print(f"\n  Asset folders with a re-score : {result['asset_folders']}")
    if result["unreadable_assets"]:
        print(f"  Unusable {AUTO_ASSETS_FILE:<21}: {result['unreadable_assets']}")
    print(f"  Rows considered               : {result['rows_considered']}")
    print(f"  Rows with no cached re-score  : {result['rows_without_assets']}")
    print(f"  Rows already current          : {result['already_current']}")
    print(f"  Re-scores matching no row     : {result['orphan_assets']}")
    print(f"  Rows to update                : {len(changes)}")
    print(f"    demoted / promoted / equal  : "
          f"{len(demotions)} / {len(promotions)} / {len(unchanged_score)}")
    print(f"    leaving the review feed (NO): {len(leaving)}")

    if demotions:
        print(f"\n  Largest demotions (top {min(sample, len(demotions))}):")
        for c in demotions[:sample]:
            print(f"    {c.job_id[:8]}  {c.stage5_verdict} {c.stage5_confidence}%"
                  f" → {c.verdict} {c.confidence}%  ({c.delta:+d})"
                  f"  [{c.status}]  {c.title[:42]} — {c.company[:24]}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m src.backfill_post_research",
        description="Copy autopilot's archived re-score into the listings row.",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes. Without this, nothing is written.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicitly request the default behavior.")
    parser.add_argument("--db", type=Path, default=None,
                        help="Database path (default: $APPLY_DAEMON_DB or apply_daemon.db)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Asset folder root (default: output/)")
    parser.add_argument("--sample", type=int, default=10,
                        help="How many of the largest demotions to list (default: 10)")
    args = parser.parse_args(argv)

    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"No database at {db_path}")
        return 1

    conn = _connect_readonly(db_path)
    try:
        result = plan(conn, args.output_dir)
    finally:
        conn.close()

    _print_summary(result, args.sample)

    if not args.apply:
        print("\n  Dry run — nothing written. Re-run with --apply to write.")
        return 0

    if not result["changes"]:
        print("\n  Nothing to write.")
        return 0

    with Database(db_path) as db:
        written = apply(db, result["changes"])
    print(f"\n  Wrote {written} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
