"""CLI funnel report — application pipeline metrics from SQLite.

Standalone read-only tool. Does NOT import any LLM, web scraping, or Slack logic.

Usage:
    python -m src.report             # Actionable batch vs All-Time
    python -m src.report --days 7    # Actionable batch vs Last 7 days
    python -m src.report --days 30   # Actionable batch vs Last 30 days
"""

from __future__ import annotations

import argparse
import logging

from src.db import Database
from src.profile_loader import load_profile

logger = logging.getLogger(__name__)

# Ordered funnel stages for display — mirrors the user journey
_FUNNEL_ORDER = [
    "triaged",
    "passed",
    "saved",
    "processing_batch",
    "tailored",
    "applied",
    "interviewing",
    "rejected",
    "expired",
    "failed_api",
    "failed_compilation",
]

# Human-readable labels for display
_DISPLAY_LABELS = {
    "triaged": "NEW (Triaged)",
    "passed": "PASSED",
    "saved": "SAVED",
    "processing_batch": "PROCESSING",
    "tailored": "TAILORED",
    "applied": "APPLIED",
    "interviewing": "INTERVIEWING",
    "rejected": "REJECTED",
    "expired": "EXPIRED",
    "failed_api": "FAILED (API)",
    "failed_compilation": "FAILED (Compile)",
}


def _get_batch_days() -> int:
    """Load batch_process_days from profile settings, default 3."""
    try:
        profile = load_profile()
        return profile["settings"].get("batch_process_days", 3)
    except FileNotFoundError:
        return 3


def _pct(numerator: int, denominator: int) -> str:
    """Format a percentage string, or '-' if denominator is zero."""
    if denominator == 0:
        return "  -"
    return f"{numerator / denominator * 100:3.0f}%"


def _bar(count: int, max_count: int, width: int = 20) -> str:
    """Render a simple ASCII bar."""
    if max_count == 0:
        return ""
    filled = round(count / max_count * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _print_funnel(counts: dict[str, int], title: str) -> None:
    """Print a single funnel table to stdout."""
    total = sum(counts.values())
    max_count = max(counts.values()) if counts else 0

    print(f"\n  {title}")
    print(f"  {'=' * len(title)}")
    print(f"  {'Stage':<20} {'Count':>6}  {'%':>4}  Bar")
    print(f"  {'-' * 20} {'-' * 6}  {'-' * 4}  {'-' * 20}")

    for status in _FUNNEL_ORDER:
        count = counts.get(status, 0)
        if count == 0:
            continue
        label = _DISPLAY_LABELS.get(status, status.upper())
        bar = _bar(count, max_count)
        print(f"  {label:<20} {count:>6}  {_pct(count, total)}  {bar}")

    print(f"  {'-' * 20} {'-' * 6}")
    print(f"  {'TOTAL':<20} {total:>6}")


def _print_conversions(counts: dict[str, int]) -> None:
    """Print conversion rate summary."""
    saved = counts.get("saved", 0)
    tailored = counts.get("tailored", 0)
    applied = counts.get("applied", 0)
    passed = counts.get("passed", 0)
    interviewing = counts.get("interviewing", 0)
    total = sum(counts.values())

    print("\n  Conversion Rates")
    print("  ----------------")
    if total > 0:
        print(f"  Pass rate:           {_pct(passed, total)}  ({passed}/{total} listings passed)")
    reviewed = saved + tailored + applied + interviewing
    if total > 0:
        print(
            f"  Save rate:           {_pct(reviewed, total)}  "
            f"({reviewed}/{total} advanced past triage)"
        )
    if reviewed > 0:
        print(
            f"  Saved -> Tailored:   {_pct(tailored + applied + interviewing, reviewed)}"
            f"  ({tailored + applied + interviewing}/{reviewed})"
        )
    if tailored + applied + interviewing > 0:
        applied_or_later = applied + interviewing
        total_tailored = tailored + applied_or_later
        print(
            f"  Tailored -> Applied: {_pct(applied_or_later, total_tailored)}"
            f"  ({applied_or_later}/{total_tailored})"
        )


def report(days: int | None = None) -> None:
    """Generate and print the funnel report."""
    batch_days = _get_batch_days()

    with Database() as db:
        batch_counts = db.get_funnel_counts(max_age_days=batch_days)
        reference_counts = db.get_funnel_counts(max_age_days=days)

    # Header
    print()
    print("  " + "\u2550" * 56)
    print("    APPLY-PILOT FUNNEL REPORT")
    print("  " + "\u2550" * 56)

    # Actionable batch
    _print_funnel(batch_counts, f"Actionable Batch (Last {batch_days} days)")

    # Reference period
    ref_label = f"Reference Period (Last {days} days)" if days else "Reference Period (All-Time)"
    _print_funnel(reference_counts, ref_label)

    # Conversion rates (reference period gives more meaningful rates)
    _print_conversions(reference_counts)

    # Pre-flight check
    batch_total = sum(batch_counts.values())
    batch_saved = batch_counts.get("saved", 0)
    batch_tailored = batch_counts.get("tailored", 0)
    print("\n  Pre-Flight Check")
    print("  ----------------")
    print(f"  Batch window:        {batch_days} days")
    print(f"  Ready to batch:      {batch_saved} saved listings awaiting bulk tailoring")
    print(f"  Recently tailored:   {batch_tailored} listings completed")
    print(f"  Total in window:     {batch_total} listings")
    print()


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Application funnel report from the apply-pilot database.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Reference period in days (default: all-time)",
    )
    args = parser.parse_args()
    report(days=args.days)


if __name__ == "__main__":
    main()
