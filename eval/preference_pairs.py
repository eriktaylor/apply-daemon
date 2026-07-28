"""Preference-pair extraction from Slack reactions / ChatOps (item E-3).

Every Slack reaction and ChatOps command is a human preference judgment the
sweeper already records to ``data/human_labels.jsonl`` (sweeper.py:54). That
ledger is the ground truth this miner reconstructs implicit *pairwise*
preferences from — the label format a pairwise ranker's validation wants,
and the primary signal E-4 checks ranking methods against.

Why the ledger, not ``pipeline_status``: with autopilot enabled, ``passed``
can come from a machine auto-pass and ``auto`` from machine tailoring, so the
status column conflates human intent with machine action. The ledger records
the actual human action, cleanly separating the two.

Polarity (from the recorded action string):
  * POSITIVE — save, tailor, and the asset commands (coverletter/prep/polish)
    plus applied/interview: further-funnel investment.
  * NEGATIVE — pass, rejected.
  * Excluded — questions/answer/expire/update/... : ops or listing-death, not
    a preference.

Pairs: within one ingestion batch (``date_ingested`` day), a positive-signal
listing is "preferred over" another in the same verdict tier (so the gate
itself isn't the confound). Strength:
  * STRONG — positive vs. explicit-negative (👎 / !pass / !rejected).
  * WEAK   — positive vs. un-reacted; an un-reacted card may simply never have
    been looked at (attention, not preference). E-4 reports strong-pair
    agreement separately before leaning on weak pairs.

Read-only. No schema change, no raw content stored beyond what's in the DB.

Usage:
    python -m eval.preference_pairs
    python -m eval.preference_pairs --out eval/preference_pairs.csv
    python -m eval.preference_pairs --labels data/human_labels.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from src.db import Database

DEFAULT_LABELS_PATH = Path("data/human_labels.jsonl")

POSITIVE_ACTIONS = frozenset({
    "save", "tailor", "coverletter", "prep", "polish", "applied", "interview",
})
NEGATIVE_ACTIONS = frozenset({"pass", "rejected"})
# Everything else (questions, answer, expire, update, regenerate, trend, …) is
# neither a preference for nor against the listing — excluded from polarity.


@dataclass
class Pair:
    batch_day: str
    verdict: str
    preferred_id: str
    preferred_confidence: int | None
    other_id: str
    other_confidence: int | None
    strength: str  # "strong" | "weak"


def _polarity(actions: set[str]) -> str | None:
    """Positive if any positive action; else negative if any negative; else None.

    Positive wins ties: a listing that was saved/tailored *and* later passed
    still represents real investment, and such contradictions are rare.
    """
    if actions & POSITIVE_ACTIONS:
        return "positive"
    if actions & NEGATIVE_ACTIONS:
        return "negative"
    return None


def load_labels(path: Path = DEFAULT_LABELS_PATH) -> dict[str, str]:
    """Map job_id → polarity ('positive'/'negative') from the ledger.

    Jobs whose only actions are neutral (questions, answer, …) are omitted.
    """
    actions_by_job: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        job_id = rec.get("job_id")
        action = (rec.get("human_reaction") or "").strip().lower()
        if job_id and action:
            actions_by_job[job_id].add(action)
    polarity: dict[str, str] = {}
    for job_id, actions in actions_by_job.items():
        pol = _polarity(actions)
        if pol is not None:
            polarity[job_id] = pol
    return polarity


def _batch_day(date_ingested: str) -> str:
    """Calendar-day bucket from an ISO date_ingested string."""
    return (date_ingested or "")[:10]


def build_pairs(polarity: dict[str, str], signals: list) -> list[Pair]:
    """Construct strong/weak preference pairs from ledger polarity + DB signals.

    ``signals`` rows expose id, verdict, confidence, date_ingested.
    """
    # Group jobs by (batch_day, verdict); carry confidence + reacted flag.
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in signals:
        verdict = (row["verdict"] or "").upper()
        if not verdict:
            continue  # untiered — can't pair without a verdict tier
        job_id = row["id"]
        groups[(_batch_day(row["date_ingested"]), verdict)].append({
            "id": job_id,
            "confidence": row["confidence"],
            "polarity": polarity.get(job_id),  # None == un-reacted
        })

    pairs: list[Pair] = []
    for (day, verdict), members in groups.items():
        positives = [m for m in members if m["polarity"] == "positive"]
        negatives = [m for m in members if m["polarity"] == "negative"]
        unreacted = [m for m in members if m["polarity"] is None]
        for p in positives:
            for other, strength in (
                [(n, "strong") for n in negatives]
                + [(u, "weak") for u in unreacted]
            ):
                pairs.append(Pair(
                    batch_day=day,
                    verdict=verdict,
                    preferred_id=p["id"],
                    preferred_confidence=p["confidence"],
                    other_id=other["id"],
                    other_confidence=other["confidence"],
                    strength=strength,
                ))
    return pairs


def summarize(pairs: list[Pair]) -> dict:
    strong = sum(1 for p in pairs if p.strength == "strong")
    weak = sum(1 for p in pairs if p.strength == "weak")
    by_verdict: dict[str, dict[str, int]] = defaultdict(lambda: {"strong": 0, "weak": 0})
    for p in pairs:
        by_verdict[p.verdict][p.strength] += 1
    return {
        "total": len(pairs),
        "strong": strong,
        "weak": weak,
        "by_verdict": {k: dict(v) for k, v in by_verdict.items()},
    }


def write_csv(pairs: list[Pair], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "batch_day", "verdict", "preferred_id", "preferred_confidence",
            "other_id", "other_confidence", "strength",
        ])
        for p in pairs:
            writer.writerow([
                p.batch_day, p.verdict, p.preferred_id, p.preferred_confidence,
                p.other_id, p.other_confidence, p.strength,
            ])


def print_summary(summary: dict) -> None:
    print(f"\n{'=' * 60}")
    print("  Preference Pairs — from human_labels.jsonl + DB batches")
    print(f"{'=' * 60}")
    print(f"  Total pairs:   {summary['total']}")
    print(f"  Strong pairs:  {summary['strong']}  (positive vs. explicit 👎/!pass)")
    print(f"  Weak pairs:    {summary['weak']}  (positive vs. un-reacted)")
    if summary["by_verdict"]:
        print(f"{'─' * 60}")
        print(f"  {'Verdict':<10} {'Strong':>8} {'Weak':>8}")
        for verdict, counts in sorted(summary["by_verdict"].items()):
            print(f"  {verdict:<10} {counts['strong']:>8} {counts['weak']:>8}")
    print(f"{'─' * 60}")
    # E-4's ship bar is measured on strong pairs (>=5 pts on >=100 pairs).
    if summary["strong"] < 100:
        print(f"  Note: {summary['strong']} strong pairs < 100 — below E-4's ship")
        print("  bar. Keep collecting reactions before deciding M-2 on noise.")
    print(f"{'=' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preference-pair extraction (E-3)")
    parser.add_argument(
        "--labels", default=str(DEFAULT_LABELS_PATH),
        help="Path to human_labels.jsonl (default: data/human_labels.jsonl)",
    )
    parser.add_argument("--out", default=None, help="Optional CSV output path")
    args = parser.parse_args()

    polarity = load_labels(Path(args.labels))
    with Database() as db:
        signals = db.get_listing_signals()
    pairs = build_pairs(polarity, signals)
    print_summary(summarize(pairs))
    if args.out:
        write_csv(pairs, Path(args.out))
        print(f"Pairs written to {args.out}")


if __name__ == "__main__":
    main()
