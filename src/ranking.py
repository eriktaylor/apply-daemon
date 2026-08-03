"""Shared LLM ranking utility (ranking_upgrade.md item M-1).

Two places in the pipeline pick a "best top-N" from LLM point-estimates —
Track B's heuristic shortlist (A-5) and Stage 5 / autopilot's confidence
band + composite sort (M-2). This module gives both one relative-ranking
implementation instead of two: a single ``listwise`` LLM call that orders a
set of already-surviving candidates, with an extension point reserved for a
``swiss`` pairwise tournament (M-3).

Design mirrors ``mismatch_gate.py``: a small dedicated module with its own
kill-switch, imported by every consumer rather than each rolling its own
prompt.

Gating — per surface with a global default (same fallback pattern as the
model slots):

    RANKING_MODE           off | listwise | swiss   (default: off)
    RANKING_MODE_TRACK_B   overrides RANKING_MODE for the Track B shortlist
    RANKING_MODE_STAGE5    overrides RANKING_MODE for Stage 5 / autopilot

Per-surface flags matter because the two consumers ship on different
evidence: A-5 (Track B shortlist, low-stakes) may turn on before the E-4
gating experiment reports; M-2 (autopilot slots, real research budget) must
not. One global flag could not hold that line.

Data-safety invariants (ranking_upgrade.md):
  1. Ranking prompts carry only what the caller already has stored — this
     module formats the ``RankCandidate`` fields it is handed and fetches
     nothing itself.
  3. Ranking is additive to the gate, never a replacement. This module only
     *reorders* the candidates it receives; it never adds, drops, or filters
     one. Every failure path returns the input order unchanged, so a ranking
     fault can never silently remove a survivor.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from src.model_usage import log_response_usage

logger = logging.getLogger(__name__)

VALID_MODES = ("off", "listwise", "swiss")
DEFAULT_MODE = "off"

# Surface identifiers — used to resolve RANKING_MODE_<SURFACE> overrides.
SURFACE_TRACK_B = "TRACK_B"
SURFACE_STAGE5 = "STAGE5"


@dataclass
class RankCandidate:
    """One candidate to be ordered.

    ``signals`` is a freeform dict of already-stored scoring features the
    caller wants the ranker to see (verdict, confidence, freshness, novelty,
    skill_score, distance bucket, …). Per invariant 1 the caller must only
    populate this from data already in the DB — never a fresh scrape.
    """

    id: str
    title: str = ""
    company: str = ""
    location: str = ""
    signals: dict = field(default_factory=dict)


def ranking_mode(surface: str) -> str:
    """Resolve the effective mode for a surface.

    ``RANKING_MODE_<SURFACE>`` wins if set; otherwise the global
    ``RANKING_MODE``; otherwise ``off``. An unrecognized value falls back to
    ``off`` with a warning so a typo can never silently enable ranking.
    """
    raw = os.getenv(f"RANKING_MODE_{surface.upper()}")
    if raw is None or not raw.strip():
        raw = os.getenv("RANKING_MODE", DEFAULT_MODE)
    mode = (raw or DEFAULT_MODE).strip().lower()
    if mode not in VALID_MODES:
        logger.warning(
            "Unrecognized ranking mode %r for surface %s — defaulting to off",
            mode, surface,
        )
        return "off"
    return mode


def _rank_stage(surface: str) -> str:
    """Usage-log stage tag for a ranking call, e.g. ``rank_track_b``."""
    return f"rank_{surface.lower()}" if surface else "rank"


def _rank_model(fallback_model: str) -> str:
    """The ranking model slot, falling back to the consuming stage's model."""
    return os.getenv("OPENROUTER_RANK_MODEL", "").strip() or fallback_model


_LISTWISE_PROMPT = """\
You are ranking job listings for a single candidate from best to worst fit.
All listings below have already passed the candidate's screening gate — your
job is only to order them, not to reject any.

Each listing is given with an id and its known signals. Consider fit quality,
seniority match, and the provided signals together.

## Listings
{candidates_block}

Respond with ONLY a valid JSON object (no markdown, no extra text):
{{"order": ["<best id>", "<next id>", ...]}}
Include every id exactly once, best first.
"""


def _format_candidate(index: int, c: RankCandidate) -> str:
    """One compact block per candidate for the listwise prompt."""
    lines = [f"[{index}] id={c.id}"]
    if c.title:
        lines.append(f"    title: {c.title}")
    if c.company:
        lines.append(f"    company: {c.company}")
    if c.location:
        lines.append(f"    location: {c.location}")
    if c.signals:
        signal_str = ", ".join(f"{k}={v}" for k, v in c.signals.items())
        lines.append(f"    signals: {signal_str}")
    return "\n".join(lines)


def _reorder_by_ids(
    candidates: list[RankCandidate], ordered_ids: list[str]
) -> list[RankCandidate]:
    """Reorder ``candidates`` by ``ordered_ids``.

    Fail-safe against a lossy model response: ids the model returned are
    placed first in the given order; any candidate the model omitted or
    duplicated is appended in its original relative order. The output is
    always a permutation of the input — never shorter, never longer.
    """
    by_id = {c.id: c for c in candidates}
    seen: set[str] = set()
    ordered: list[RankCandidate] = []
    for cid in ordered_ids:
        if cid in by_id and cid not in seen:
            ordered.append(by_id[cid])
            seen.add(cid)
    for c in candidates:
        if c.id not in seen:
            ordered.append(c)
            seen.add(c.id)
    return ordered


def rank_listwise(
    client,
    model: str,
    candidates: list[RankCandidate],
    surface: str = "",
) -> list[RankCandidate]:
    """Order ``candidates`` best→worst with one listwise LLM call.

    Fails open: on a missing client, an LLM error, or an unparseable
    response, returns ``candidates`` unchanged.
    """
    if client is None or len(candidates) < 2:
        return candidates
    block = "\n\n".join(
        _format_candidate(i, c) for i, c in enumerate(candidates, 1)
    )
    prompt = _LISTWISE_PROMPT.format(candidates_block=block)
    try:
        rank_model = _rank_model(model)
        resp = client.chat.completions.create(
            model=rank_model,
            messages=[{"role": "user", "content": prompt}],
            # One id per candidate, and UUIDs cost ~18 tokens each. Sized to
            # the request rather than fixed: a fixed budget silently truncates
            # on a large pool, and a truncated JSON object does not degrade —
            # it fails to parse and the whole ranking is discarded.
            max_tokens=max(500, 24 * len(candidates) + 200),
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        log_response_usage(resp, rank_model, _rank_stage(surface))
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        ordered_ids = data.get("order")
        if not isinstance(ordered_ids, list):
            logger.warning("Listwise rank returned no 'order' list — keeping input order")
            return candidates
        return _reorder_by_ids(candidates, [str(x) for x in ordered_ids])
    except Exception:
        logger.warning("Listwise rank failed — keeping input order", exc_info=True)
        return candidates


def rank_candidates(
    *,
    client,
    model: str,
    surface: str,
    candidates: list[RankCandidate],
) -> list[RankCandidate]:
    """Order candidates for a surface, honoring that surface's ranking mode.

    ``off`` (default) returns the input order unchanged — the caller keeps
    its existing sort. ``listwise`` reorders via one LLM call. ``swiss`` is
    a reserved extension point (M-3); until implemented it logs and falls
    back to the input order, so enabling it early is safe.
    """
    mode = ranking_mode(surface)
    if mode == "off" or len(candidates) < 2:
        return candidates
    if mode == "listwise":
        return rank_listwise(client, model, candidates, surface=surface)
    if mode == "swiss":
        logger.warning(
            "RANKING_MODE swiss not yet implemented (M-3) — keeping input order",
        )
        return candidates
    return candidates
