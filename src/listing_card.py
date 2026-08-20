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

**Two scores, one of them effective.** A listing carries Stage 5's verdict
and confidence, and — once autopilot has re-scored it against a research
dossier — a second pair that frequently disagrees. Both are kept: the
disagreement is what a deep-dive exists to show. The card names which one a
renderer should present (``effective_verdict`` / ``effective_confidence`` /
``confidence_source``) so no surface has to decide for itself, which is
exactly how the CLI came to show ``YES 95%`` for a listing Slack was calling
``MAYBE 58%``.
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
    "post_research_verdict", "post_research_confidence", "confidence_delta",
    "confidence_source", "effective_verdict", "effective_confidence",
    "location", "distance", "distance_detail", "salary", "url", "tldr",
    "skills_pct", "skills_matched", "skills_total",
    "matching_skills", "missing_skills",
    "age_days", "freshness", "tier", "research_cached",
)

# Keys of the normalized re-score envelope (`parse_post_research`). Named so
# the CLI's `deep-dive` payload shape is the contract's, not one verb's.
POST_RESEARCH_FIELDS = (
    "verdict", "confidence", "confidence_delta",
    "match_analysis", "matching_skills", "missing_skills",
)

# Which score a card is presenting.
SOURCE_POST_RESEARCH = "post_research"
SOURCE_STAGE5 = "stage5"

# Human labels for the two sources — rendered by `format_verdict_line`, so
# both surfaces print the same words for the same provenance.
_SOURCE_LABELS = {SOURCE_POST_RESEARCH: "post-research", SOURCE_STAGE5: "stage 5"}

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


def _as_int(raw: object) -> int | None:
    """Coerce a confidence to int, or None. Models emit ``85`` and ``"85"``."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _delta(post_research: object, stage5: object) -> int | None:
    """Post-research confidence minus Stage 5's, or None if either is absent.

    One subtraction, because the sign is load-bearing: the skill is told the
    delta "skews negative — the first pass is optimistic", and two copies
    would eventually disagree about which way it points.
    """
    after, before = _as_int(post_research), _as_int(stage5)
    if after is None or before is None:
        return None
    return after - before


def _as_verdict(raw: object) -> str | None:
    """Normalize a verdict to upper case, or None when absent/blank."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return text or None


def parse_post_research(data: object, stage5_confidence: object = None) -> dict | None:
    """Normalize autopilot's re-score envelope into the card's vocabulary.

    ``data`` is the parsed ``auto_assets.json`` — or the same dict still in
    memory inside ``process_queue``. One parser for both, because the two
    surfaces reading that envelope separately is how they came to disagree
    about the same listing.

    Returns ``None`` for anything that isn't a mapping. Every other input
    yields the full :data:`POST_RESEARCH_FIELDS` key set, values ``None`` /
    ``[]`` where the model omitted something.

    ``confidence_delta`` is post-research minus Stage 5 — computed here so
    the reader never has to subtract two numbers in their head, and computed
    once so `deep-dive`, the CLI feed and the Slack card cannot disagree
    about the sign.
    """
    if not isinstance(data, dict):
        return None

    skills = data.get("updated_skills_match") or {}
    if not isinstance(skills, dict):
        skills = {}

    confidence = _as_int(data.get("post_research_confidence"))
    delta = _delta(confidence, stage5_confidence)

    return {
        "verdict": _as_verdict(data.get("post_research_verdict")),
        "confidence": confidence,
        "confidence_delta": delta,
        "match_analysis": data.get("match_analysis"),
        "matching_skills": parse_skill_list(skills.get("matching")),
        "missing_skills": parse_skill_list(skills.get("missing")),
    }


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


def build_card(
    row: object,
    *,
    research_cached: bool = False,
    post_research: dict | None = None,
    distance_detail: str | None = None,
) -> dict:
    """Assemble the canonical card for one listing row.

    ``research_cached`` and ``distance_detail`` are passed in rather than
    derived here: one needs filesystem access and the other a geocoder, and
    this module stays pure so it can be tested against synthetic rows without
    touching disk or the network.

    ``post_research`` is the normalized re-score (:func:`parse_post_research`)
    for a caller that already holds the envelope — autopilot, mid-run, before
    the row is re-read. Omit it and the row's own ``post_research_*`` columns
    are used, which is the CLI's path. Either way the effective score is
    resolved here, once.

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

    stage5_verdict = _as_verdict(_get(row, "verdict"))
    stage5_conf = _as_int(_get(row, "confidence"))
    if post_research is None:
        pr_verdict = _as_verdict(_get(row, "post_research_verdict"))
        pr_conf = _as_int(_get(row, "post_research_confidence"))
        delta = _delta(pr_conf, stage5_conf)
    else:
        pr_verdict = _as_verdict(post_research.get("verdict"))
        pr_conf = _as_int(post_research.get("confidence"))
        delta = post_research.get("confidence_delta")

    # The re-score wins where it exists — it read a research dossier the
    # Stage 5 pass never saw. Confidence and verdict are resolved
    # independently so a half-written envelope degrades to a stated absence
    # rather than to a blended score no model ever produced.
    source = SOURCE_POST_RESEARCH if pr_conf is not None else SOURCE_STAGE5

    return {
        "id": _get(row, "id"),
        "title": _get(row, "title"),
        "company": _get(row, "company"),
        "verdict": stage5_verdict,
        "confidence": stage5_conf,
        "post_research_verdict": pr_verdict,
        "post_research_confidence": pr_conf,
        "confidence_delta": delta,
        "confidence_source": source,
        "effective_verdict": pr_verdict or stage5_verdict,
        "effective_confidence": pr_conf if pr_conf is not None else stage5_conf,
        "location": _get(row, "location") or None,
        "distance": DISTANCE_LABELS.get(bucket) if bucket is not None else None,
        "distance_detail": distance_detail or None,
        "salary": _get(row, "salary") or None,
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


def format_verdict_line(card: dict) -> str:
    """The score, its provenance, and what it displaced.

    ``MAYBE 58% (post-research · was YES 95%, -37)`` or ``YES 95% (stage 5)``.
    One rendering, so "which number is this?" has the same answer on Slack,
    in the CLI feed, and in anything the skill reads back to the user.
    """
    verdict = card.get("effective_verdict") or "?"
    confidence = card.get("effective_confidence")
    score = f"{verdict} {confidence}%" if confidence is not None else verdict
    source = card.get("confidence_source") or SOURCE_STAGE5
    note = _SOURCE_LABELS.get(source, source)
    if source == SOURCE_POST_RESEARCH:
        stage5_verdict = card.get("verdict")
        stage5_conf = card.get("confidence")
        if stage5_verdict or stage5_conf is not None:
            was = " ".join(
                p for p in (stage5_verdict, f"{stage5_conf}%"
                            if stage5_conf is not None else None) if p
            )
            note += f" · was {was}"
        delta = card.get("confidence_delta")
        if delta:
            note += f", {delta:+d}"
    return f"{score} ({note})"


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
