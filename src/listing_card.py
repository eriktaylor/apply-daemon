"""The listing card contract — one definition of what a review card shows.

Every review surface (Slack digest, CLI, the agent skill) presents the same
decision-relevant facts about a listing. Before this module each surface
assembled that set itself, which is how the Slack card silently lost its
skills block once: two renderers, one drifts, nothing fails.

``build_card(row)`` returns the canonical field set. Renderers choose
*presentation* — Block Kit, ANSI text, JSON — never *content*.

Two rules the contract enforces:

**Heuristics over LLM output wherever the value is derivable.** The skills
match percentage is computed from the lengths of the model's own lists, not
asked of the model: it costs no tokens and cannot hallucinate. Same for
distance bucketing, freshness, and age. The LLM is asked only for things
that genuinely require judgement — the verdict, the confidence, the TL;DR,
and which skills match.

**Missing data degrades to a stated absence, never an exception and never a
silent omission.** LLM output is probabilistic: a field will eventually be
absent or malformed. A card that raises kills the whole digest; a card that
quietly drops a field looks like a listing with no skills. Both have
happened. So every field is always present in the returned dict, with
``None``/``[]`` meaning "not available" — and ``tests/test_listing_card.py``
asserts that, because a contract nothing checks is a convention.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Fields every card carries. Renderers may present a subset, but the builder
# must always return all of them — see test_listing_card.py.
REQUIRED_FIELDS = (
    "id", "title", "company", "verdict", "confidence",
    "location", "distance", "url", "tldr",
    "skills_pct", "skills_matched", "skills_total",
    "matching_skills", "missing_skills",
    "age_days", "freshness", "tier", "research_cached",
)

# Coarse distance labels for `distance_bucket` (see process_queue).
DISTANCE_LABELS = {0: "Remote", 1: "Local", 2: "Commute", 3: "Far"}

# Freshness bands in days → label. Mirrors the digest's badge thresholds.
_FRESH_DAYS = 7
_AGING_DAYS = 30


def parse_skill_list(raw: object) -> list[str]:
    """Parse a skills column into a list, tolerating every observed shape.

    Accepts a real list, a JSON-encoded list, or junk. Never raises: an
    unparseable value yields ``[]`` rather than killing the card that
    contains it.
    """
    if isinstance(raw, list):
        return [str(s) for s in raw if str(s).strip()]
    if not raw or not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.debug("Unparseable skills value; treating as empty")
        return []
    if isinstance(parsed, list):
        return [str(s) for s in parsed if str(s).strip()]
    return []


def skills_match(matching: list[str], missing: list[str]) -> tuple[int | None, int, int]:
    """Return ``(percent, matched, total)`` — a heuristic, never asked of the LLM.

    ``percent`` is None when nothing was extracted, so a renderer can say
    "not specified" instead of printing a misleading 0% or 100%.
    """
    matched, total = len(matching), len(matching) + len(missing)
    if total == 0:
        return None, 0, 0
    return round(matched / total * 100), matched, total


def age_in_days(stamp: str | None) -> int | None:
    """Whole days since an ISO timestamp, or None if absent/unparseable."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    if when.tzinfo is None:
        # Date-only values ("2026-07-28") parse to naive midnight. Compare
        # whole UTC dates so the answer doesn't shift with local timezone —
        # mixing date.today() with a UTC now was off by one for anyone east
        # or west of UTC.
        if when.time() == datetime.min.time():
            return max(0, (now.date() - when.date()).days)
        when = when.replace(tzinfo=timezone.utc)
    return max(0, (now - when).days)


def freshness(age_days: int | None) -> str | None:
    """Coarse freshness label from an age in days."""
    if age_days is None:
        return None
    if age_days <= _FRESH_DAYS:
        return "new"
    if age_days <= _AGING_DAYS:
        return "recent"
    return "stale"


def _first_link(raw: object) -> str | None:
    links = parse_skill_list(raw)  # same tolerant JSON-list parse
    return links[0] if links else None


def _get(row: object, key: str, default=None):
    """Read a key from a sqlite3.Row or a dict, tolerating absence."""
    try:
        if hasattr(row, "keys"):
            return row[key] if key in row.keys() else default
        return row.get(key, default)
    except (KeyError, IndexError, TypeError):
        return default


def build_card(row: object, *, research_cached: bool = False) -> dict:
    """Assemble the canonical card for one listing row.

    ``research_cached`` is passed in rather than probed here: it requires
    filesystem access, and this module stays pure so it can be tested
    against synthetic rows without touching disk.

    Never emits ``raw_email_text`` — card output reaches Slack, a model
    context, and logs (CLAUDE.md security ground rules).
    """
    matching = parse_skill_list(_get(row, "matching_skills"))
    missing = parse_skill_list(_get(row, "missing_skills"))
    pct, matched, total = skills_match(matching, missing)

    # Prefer the listing's own posting date for freshness; fall back to when
    # we first saw it, which is all Track A rows have.
    age = age_in_days(_get(row, "date_posted")) or age_in_days(
        _get(row, "date_ingested")
    )

    bucket = _get(row, "distance_bucket")
    tier_rank = _get(row, "tier_rank")

    return {
        "id": _get(row, "id"),
        "title": _get(row, "title"),
        "company": _get(row, "company"),
        "verdict": _get(row, "verdict"),
        "confidence": _get(row, "confidence"),
        "location": _get(row, "location") or None,
        "distance": DISTANCE_LABELS.get(bucket) if bucket is not None else None,
        "url": _first_link(_get(row, "links")),
        "tldr": _get(row, "job_summary") or None,
        "skills_pct": pct,
        "skills_matched": matched,
        "skills_total": total,
        "matching_skills": matching,
        "missing_skills": missing,
        "age_days": age,
        "freshness": freshness(age),
        "tier": (
            {0: "auto", 1: "auto_queued", 2: "triaged"}.get(tier_rank)
            if tier_rank is not None
            else _get(row, "pipeline_status")
        ),
        "research_cached": research_cached,
    }


def format_skills_line(card: dict) -> str:
    """One-line skills summary, or a stated absence."""
    if card["skills_pct"] is None:
        return "Skills: not specified in listing"
    line = (
        f"Skills: {card['skills_pct']}% "
        f"({card['skills_matched']}/{card['skills_total']})"
    )
    if card["matching_skills"]:
        line += f"\n  match: {', '.join(card['matching_skills'])}"
    if card["missing_skills"]:
        line += f"\n  gaps:  {', '.join(card['missing_skills'])}"
    return line
