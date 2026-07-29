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
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db import Database
from src.profile_loader import load_profile

logger = logging.getLogger(__name__)

# Statuses that count as "advanced past triage" for the model save-rate view.
# Mirrors _print_conversions' `reviewed` set plus autopilot-finalized `auto`.
_ADVANCED_STATUSES = frozenset({"saved", "tailored", "applied", "interviewing", "auto"})

# Coarse confidence calibration bands (single-user volume won't support fine
# curves) — (inclusive low, inclusive high, label).
_CALIBRATION_BANDS = [
    (0, 59, " 0-59"),
    (60, 74, "60-74"),
    (75, 89, "75-89"),
    (90, 100, "90-100"),
]

# O-1's model-usage telemetry sink (see src/model_usage.py).
_MODEL_USAGE_LOG = Path(os.getenv("MODEL_USAGE_LOG_PATH", "logs/model_usage.log"))

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


def _print_model_breakdown(breakdown: dict[str, dict]) -> None:
    """Per-model outcome table: save-rate, interviews/YES, calibration."""
    print("\n  Per-Model Outcomes")
    print("  ------------------")
    if not breakdown:
        print("  (no listings with a recorded model_used in this window)")
        return

    # Order models by total volume, biggest first.
    def _total(entry: dict) -> int:
        return sum(entry["statuses"].values())

    for model, entry in sorted(breakdown.items(), key=lambda kv: -_total(kv[1])):
        total = _total(entry)
        advanced = sum(n for s, n in entry["statuses"].items() if s in _ADVANCED_STATUSES)
        yes = entry["verdicts"].get("YES", 0)
        interviewing = entry["statuses"].get("interviewing", 0)
        ivs_per_100_yes = f"{interviewing / yes * 100:.0f}" if yes else "-"

        print(f"\n  {model}  ({total} listings)")
        print(f"    Save rate:          {_pct(advanced, total)}  ({advanced}/{total} advanced)")
        print(f"    Interviews/100 YES: {ivs_per_100_yes:>4}  ({interviewing} iv / {yes} YES)")

        # Calibration: bucket surfaced listings by confidence, show save-rate
        # per band — a well-calibrated model's save-rate rises with confidence.
        print(f"    {'Confidence':<10} {'N':>4} {'SaveRate':>9}  Bar")
        band_maxes = [
            sum(1 for c, _ in entry["confidences"] if lo <= c <= hi)
            for lo, hi, _ in _CALIBRATION_BANDS
        ]
        max_n = max(band_maxes) if band_maxes else 0
        for (lo, hi, label), n_in_band in zip(_CALIBRATION_BANDS, band_maxes):
            if n_in_band == 0:
                continue
            saved_in_band = sum(
                1 for c, s in entry["confidences"]
                if lo <= c <= hi and s in _ADVANCED_STATUSES
            )
            bar = _bar(n_in_band, max_n, width=12)
            print(f"    {label:<10} {n_in_band:>4} {_pct(saved_in_band, n_in_band):>9}  {bar}")


def _parse_usage_log(path: Path = _MODEL_USAGE_LOG) -> dict[tuple[str, str], dict]:
    """Aggregate O-1's usage log into {(model, stage): {calls, tokens}}.

    Log schema (pipe-delimited): timestamp|stage|model|tokens. Malformed lines
    are skipped. Returns {} if the sink doesn't exist yet.
    """
    agg: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return agg
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        _ts, stage, model, tokens = parts
        try:
            tok = int(tokens)
        except ValueError:
            continue
        entry = agg.setdefault((model, stage), {"calls": 0, "tokens": 0})
        entry["calls"] += 1
        entry["tokens"] += tok
    return agg


def _print_model_costs(path: Path = _MODEL_USAGE_LOG) -> None:
    """Live cost view from O-1's token log, priced via eval/model_pricing."""
    agg = _parse_usage_log(path)
    print("\n  Live Model Cost (from logs/model_usage.log)")
    print("  -------------------------------------------")
    if not agg:
        print(f"  No usage log yet at {path} — costs appear after LLM calls run.")
        return
    try:
        from eval.model_pricing import PRICING_VERIFIED, cost_for_tokens
    except ImportError:
        cost_for_tokens = None
        PRICING_VERIFIED = False

    print(f"  {'Model':<32} {'Stage':<16} {'Calls':>6} {'Tokens':>10} {'Est $':>9}")
    print(f"  {'-' * 32} {'-' * 16} {'-' * 6} {'-' * 10} {'-' * 9}")
    total_cost = 0.0
    any_priced = False
    for (model, stage), e in sorted(agg.items(), key=lambda kv: -kv[1]["tokens"]):
        cost = cost_for_tokens(model, e["tokens"]) if cost_for_tokens else None
        if cost is not None:
            total_cost += cost
            any_priced = True
        cost_str = f"${cost:.4f}" if cost is not None else "n/a"
        print(f"  {model:<32} {stage:<16} {e['calls']:>6} {e['tokens']:>10,} {cost_str:>9}")
    if any_priced:
        note = "" if PRICING_VERIFIED else "  (UNVERIFIED pricing)"
        print(f"  {'-' * 32}")
        print(f"  Total est. spend logged: ${total_cost:.4f}{note}")


def _print_cascade_summary(output_root: str = "output") -> None:
    """Footer: O-3 cascade agreement rate (small Stage 5 vs. large post-research)."""
    try:
        from eval.cascade_agreement import summarize, walk
    except ImportError:
        return
    try:
        summary = summarize(walk(Path(output_root)))
    except Exception:
        return
    if summary.get("n", 0) == 0:
        return
    stage5 = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite")
    print("\n  Cascade Agreement (O-3, autopilot rows only)")
    print("  --------------------------------------------")
    print(f"  Stage 5 slug:        {stage5}")
    print(f"  Pairs analyzed:      {summary['n']}")
    print(f"  Verdict agreement:   {summary['verdict_agreement_rate']:.1%}")
    gap = summary.get("mean_confidence_gap")
    if gap is not None:
        print(f"  Mean confidence gap: {gap:+.1f}  (large − small; blind to false negatives)")


def spend_report(days: int | None = None) -> None:
    """Per-day metered spend from the usage log (C-4).

    Answers "what has this cost me, and is today unusual?" — the number
    C-3's ceiling is enforced against and the one the control plane reports
    before firing a run.
    """
    from src.model_usage import iter_usage

    try:
        from eval.model_pricing import (
            LAST_UPDATED,
            PRICING_VERIFIED,
            cost_for_tokens,
        )
    except ImportError:
        print("\n  Pricing table unavailable — cannot cost the usage log.\n")
        return

    cutoff = None
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    by_day: dict[str, dict] = {}
    by_stage: dict[str, dict] = {}
    unpriced: set[str] = set()

    for day, stage, model, tokens in iter_usage():
        if cutoff and day < cutoff:
            continue
        usd = cost_for_tokens(model, tokens)
        if usd is None:
            unpriced.add(model)
        for bucket, key in ((by_day, day), (by_stage, stage)):
            entry = bucket.setdefault(key, {"calls": 0, "tokens": 0, "usd": 0.0})
            entry["calls"] += 1
            entry["tokens"] += tokens
            entry["usd"] += usd or 0.0

    print()
    print("  " + "═" * 56)
    print("    APPLY-DAEMON SPEND REPORT")
    print(f"    ({f'Last {days} days' if days else 'All-Time'})")
    print("  " + "═" * 56)

    if not by_day:
        print("\n  No metered calls recorded yet.")
        print("  (logs/model_usage.log is written from the next run onward;")
        print("   spend before it existed was never recorded.)\n")
        return

    print(f"\n  {'Day':<12} {'Calls':>7} {'Tokens':>12} {'Est $':>10}")
    print("  " + "─" * 44)
    for day in sorted(by_day):
        e = by_day[day]
        print(f"  {day:<12} {e['calls']:>7} {e['tokens']:>12,} {e['usd']:>10.4f}")

    total_tokens = sum(e["tokens"] for e in by_day.values())
    total_usd = sum(e["usd"] for e in by_day.values())
    total_calls = sum(e["calls"] for e in by_day.values())
    print("  " + "─" * 44)
    print(f"  {'TOTAL':<12} {total_calls:>7} {total_tokens:>12,} {total_usd:>10.4f}")
    if by_day:
        print(f"  {'per day':<12} {'':>7} {'':>12} "
              f"{total_usd / len(by_day):>10.4f}")

    print(f"\n  {'Stage':<24} {'Calls':>7} {'Tokens':>12} {'Est $':>10}")
    print("  " + "─" * 56)
    for stage, e in sorted(by_stage.items(), key=lambda kv: -kv[1]["usd"]):
        print(f"  {stage:<24} {e['calls']:>7} {e['tokens']:>12,} {e['usd']:>10.4f}")

    if unpriced:
        print(f"\n  ⚠ Unpriced models costed as $0: {', '.join(sorted(unpriced))}")
        print("    Add them to eval/model_pricing.py — the total above is a floor.")
    banner = "verified" if PRICING_VERIFIED else "UNVERIFIED — do not trust $"
    print(f"\n  Pricing {banner}, dated {LAST_UPDATED}.")
    print("  In-session (subscription) work is not metered and not shown.\n")


def models_report(days: int | None = None) -> None:
    """Print the per-model report: outcomes, calibration, live cost, cascade."""
    with Database() as db:
        breakdown = db.get_model_breakdown(max_age_days=days)

    print()
    print("  " + "═" * 56)
    print("    APPLY-DAEMON MODEL REPORT")
    label = f"Last {days} days" if days else "All-Time"
    print(f"    ({label})")
    print("  " + "═" * 56)

    _print_model_breakdown(breakdown)
    _print_model_costs()
    _print_cascade_summary()
    print()


def report(days: int | None = None) -> None:
    """Generate and print the funnel report."""
    batch_days = _get_batch_days()

    with Database() as db:
        batch_counts = db.get_funnel_counts(max_age_days=batch_days)
        reference_counts = db.get_funnel_counts(max_age_days=days)

    # Header
    print()
    print("  " + "\u2550" * 56)
    print("    APPLY-DAEMON FUNNEL REPORT")
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
        description="Application funnel report from the apply-daemon database.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Reference period in days (default: all-time)",
    )
    parser.add_argument(
        "--models",
        action="store_true",
        help="Show the per-model report (outcomes, calibration, cost, cascade) "
             "instead of the funnel",
    )
    parser.add_argument(
        "--spend",
        action="store_true",
        help="Show metered spend per day and per stage from the usage log",
    )
    args = parser.parse_args()
    if args.spend:
        spend_report(days=args.days)
    elif args.models:
        models_report(days=args.days)
    else:
        report(days=args.days)


if __name__ == "__main__":
    main()
