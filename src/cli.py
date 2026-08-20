"""Review surface CLI — the deterministic layer a Claude skill drives.

Replaces Slack thread commands (frozen; see docs/CHATOPS.md) with verbs that
are testable in-process:

    python -m src.cli status              # queue freshness + spend vs budget
    python -m src.cli refresh             # run the pipeline (budget-gated)
    python -m src.cli next [--top 3]      # next page of candidates
    python -m src.cli show <id>           # one listing in full
    python -m src.cli deep-dive <id>      # + post-research verdict, dossier
    python -m src.cli save <id>           # → saved
    python -m src.cli pass <id>           # → passed
    python -m src.cli pass --all          # pass the whole current page
    python -m src.cli tailor <id>         # emit prompt for in-session tailoring
    python -m src.cli tailor <id> --apply -   # write assets from model JSON
    python -m src.cli tailor <id> --via api   # fall back to OpenRouter

Every verb accepts ``--json``. The skill parses that; prose output is for
humans only, so changing wording never breaks the interface. The JSON schema
is a contract pinned by tests/test_cli.py — treat added keys as fine and
renamed/removed keys as breaking.

Design rules this module keeps:

- **No LLM calls, no network.** Every verb is a local DB read/write, so the
  conversational loop stays instant. Enrichment is autopilot's job, already
  done before a listing reaches here.
- **`raw_email_text` is never emitted** (see ``_card``). It is raw email
  content, and CLI output flows into a model context and possibly logs —
  CLAUDE.md forbids that.
- **Every decision writes the ledger** via ``append_human_label`` with
  ``surface="cli"``. See src/human_labels.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from src.db import (
    ENRICHED_STATUSES,
    REVIEW_STATUSES,
    SEEN_ONLY,
    UNSEEN_ONLY,
    Database,
)
from src.decisions import DECISIONS, target_status
from src.decisions import apply as apply_decision
from src.file_utils import find_output_folder
from src.human_labels import SURFACE_CLI, append_human_label
from src.listing_card import (
    build_card,
    format_skills_line,
    format_verdict_line,
    parse_post_research,
)

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")
RESEARCH_FILE = "deep_research_context.txt"
AUTO_ASSETS_FILE = "auto_assets.json"

# How far back `pass --all` / `save --all` look for "the current page".
# Bulk decisions act on what the user is looking at, so this is scoped to a
# sitting. It no longer affects eligibility — the feed retires a listing when
# it shows it (db.get_review_queue), rather than hiding it for a while.
SESSION_WINDOW_MINUTES = 120

DEFAULT_TOP = 3

# Ingestion-age bound for `next`, in days. Generous by design — see
# review_max_age_days(). Override per-call with --max-age, or globally with
# REVIEW_MAX_AGE_DAYS.
DEFAULT_MAX_AGE_DAYS = 30

_TIER_LABELS = {0: "auto", 1: "auto_queued", 2: "triaged"}

# Human labels for distance_bucket (populated by src/geo_backfill.py /
# autopilot). Surfaced on every card because distance now silently shapes
# the queue order — an invisible sort key reads as a broken sort.
_DISTANCE_LABELS = {0: "Remote", 1: "Local", 2: "Commute", 3: "Far"}

# Past-tense forms for human output ("Saveed" otherwise). Presentation only —
# the decision policy itself lives in src/decisions.py.
_PAST_TENSE = {"save": "Saved", "pass": "Passed"}

# Verbs that accept `--all` (act on the whole presented page). An
# argument-parsing concern, deliberately not derived from DECISIONS.
_BULK_CAPABLE_VERBS = ("save", "pass")

# On-demand asset verbs: (wire name, help). The asset key each maps to is the
# `generate_assets` vocabulary — src/tailor.py::ASSET_SPECS owns the specs, so
# this table carries presentation only.
_ASSET_VERBS = (
    ("polish", "Polish the resume into a final document (needs a prior tailor)"),
    ("cover-letter", "Write a cover letter from profile + resume + research"),
    ("interview-prep", "Build an interview prep guide"),
    ("answers", "Answer application questions (--questions)"),
)
_ASSET_VERB_NAMES = {verb: verb.replace("-", "_") for verb, _ in _ASSET_VERBS}

# The ingestion sequence, in order. THE definition — script.sh wraps this
# verb rather than repeating the chain (R-1). digest runs twice on purpose:
# once after each track, so Slack sees Track A's listings without waiting on
# IMAP. It no-ops harmlessly when Slack isn't configured.
REFRESH_STAGES: tuple[tuple[str, str], ...] = (
    ("track A scrape", "src.jobspy_ingest"),
    ("digest (A)", "src.digest"),
    ("track B email", "src.pipeline"),
    ("digest (B)", "src.digest"),
    ("autopilot", "src.process_queue"),
)

# Consecutive stage failures that abandon the chain. No stage feeds another —
# all five talk only through SQLite, and none reads a predecessor's exit code
# — so one failure says nothing about the next stage and the rest of the run
# is still worth having. Autopilot in particular runs last on already-stored
# rows, so fail-fast spent a routine Track A proxy error on the day's
# enrichment. Two failures in a row is the *systemic* signal (bad credential,
# provider down), and stopping there preserves the spend containment
# fail-fast was really buying.
#
# Same shape as triage._MAX_CONSECUTIVE_REJECTIONS, deliberately not the same
# constant: unrelated domains, and one number serving both would be drift the
# first time either is tuned.
_MAX_CONSECUTIVE_STAGE_FAILURES = 2

# Stages taken off the critical path: launched and never waited on. Slack
# posting is ~155s of a ~990s chain (measured 2026-08-20) and nothing
# downstream reads it — I-9 made autopilot Slack-independent on purpose.
# `--wait` restores in-line behavior for cron/CI.
_DETACHABLE_MODULES = frozenset({"src.digest"})

# Where a detached stage's output goes. It must be a file: a detached child
# that inherits the parent's pipes dies on its next write once `refresh`
# returns. Module-level so tests can redirect it; logs/ is gitignored, same
# as logs/model_usage.log and logs/run_log.
LOG_DIR = Path("logs")


def _output_folder(job_id: str) -> Path | None:
    """Locate this job's asset folder (see file_utils.find_output_folder).

    Wrapper rather than a direct call so this module's ``OUTPUT_DIR`` stays
    monkeypatchable. file_utils is import-light, so the CLI still avoids
    pulling openai via tailor.
    """
    return find_output_folder(job_id, OUTPUT_DIR)


def _research_cached(job_id: str) -> bool:
    """True when a Deep Research dossier already exists for this job."""
    folder = _output_folder(job_id)
    return bool(folder and (folder / RESEARCH_FILE).exists())


def _cached_research(job_id: str) -> str:
    """Return the cached Deep Research dossier, or "" when none exists."""
    folder = _output_folder(job_id)
    if folder:
        return _read_text(folder / RESEARCH_FILE) or ""
    return ""


# Passed to build_prompt when no dossier is cached. Must be NON-EMPTY:
# build_prompt treats an empty research_context_override as "run Deep
# Research live" (tailor.py:255), which is a token-spending network call —
# exactly what the in-session route promises not to make.
_NO_RESEARCH_PLACEHOLDER = (
    "(No cached company research is available for this listing. "
    "Ground the analysis in the job description alone.)"
)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _post_research(folder: Path, triage_confidence: int | None) -> dict | None:
    """Read autopilot's post-research re-score off disk, if autopilot has run.

    The dossier folder rather than the DB, because this verb also shows
    ``match_analysis``, which is archived only in ``auto_assets.json`` — and
    because a row the backfill never reached still has its re-score here.
    Shape and the delta arithmetic belong to the card contract
    (``listing_card.parse_post_research``); this function owns only the read.
    """
    raw = _read_text(folder / AUTO_ASSETS_FILE)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Malformed %s in %s", AUTO_ASSETS_FILE, folder.name)
        return None
    return parse_post_research(data, triage_confidence)


def _tier_of(row: sqlite3.Row) -> str:
    keys = row.keys()
    if "tier_rank" in keys:
        return _TIER_LABELS.get(row["tier_rank"], "triaged")
    return str(row["pipeline_status"])


def _card(row: sqlite3.Row, *, detail: bool = False) -> dict:
    """Serialize a listing row for output via the shared card contract.

    Content comes from src/listing_card.py so this surface cannot drift from
    the Slack digest — the two assembling their own field sets is how the
    skills block went missing once. Presentation is ours; content is not.
    """
    card = build_card(row, research_cached=_research_cached(row["id"]))
    card["status"] = row["pipeline_status"]
    card["date_ingested"] = row["date_ingested"]
    if detail:
        card["reason"] = _get_row(row, "reason")
    return card


def _get_row(row: sqlite3.Row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _emit(payload: dict, as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else human)


def _fmt_card(card: dict, index: int | None = None) -> str:
    """Render the canonical card. Field set is the contract's, not ours."""
    prefix = f"[{index}] " if index is not None else ""
    verdict = card.get("effective_verdict") or "?"
    lines = [f"{prefix}{verdict}: {card['title']} — {card['company']}"]

    loc = card.get("location") or "location unknown"
    if card.get("distance"):
        loc += f" ({card['distance']})"
    meta = [loc]
    if card.get("freshness"):
        age = card.get("age_days")
        meta.append(f"{card['freshness']}" + (f" · {age}d" if age is not None else ""))
    # Which score this is, and what it displaced — the contract's wording, so
    # the Slack card and this line cannot disagree about the same listing.
    meta.append(format_verdict_line(card))
    lines.append("    " + "  |  ".join(meta))

    if card.get("tldr"):
        lines.append(f"    TL;DR: {card['tldr'][:400]}")

    for skill_line in format_skills_line(card).splitlines():
        lines.append("    " + skill_line.strip() if skill_line.startswith(" ")
                     else "    " + skill_line)

    tier = card.get("tier")
    extras = [tier] if tier else []
    if card.get("research_cached"):
        extras.append("research cached — deep-dive is free")
    if card.get("salary"):
        extras.append(card["salary"])
    if extras:
        lines.append("    " + "  ·  ".join(extras))
    if card.get("url"):
        lines.append(f"    {card['url']}")
    lines.append(f"    id: {card['id']}")
    return "\n".join(lines)


def review_max_age_days() -> int:
    """Ingestion-age bound for the review queue. 0 disables.

    This is the freshness surface, so it needs a bound — `digest.py` has
    limited Slack to 14 days all along while this had no check at all. The
    default is deliberately generous rather than matching the digest: a tight
    bound would empty an aged queue with no explanation, which reads as a
    broken tool. What it hides is always reported.
    """
    raw = os.getenv("REVIEW_MAX_AGE_DAYS", "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("REVIEW_MAX_AGE_DAYS=%r is not an integer; using %d",
                       raw, DEFAULT_MAX_AGE_DAYS)
        return DEFAULT_MAX_AGE_DAYS


def high_signal_only() -> bool:
    """Whether `next` shows enriched rows only.

    Follows `AUTOPILOT_POST_STAGE_5`, the knob that already governs this for
    the Slack digest — raising CONFIDENCE_THRESHOLD and enabling autopilot is
    a statement that raw Stage 5 output is noise, and the review surface
    should honor it rather than re-surfacing what was filtered out.
    """
    # Fallback matches digest.py and .env.example: unset means high-signal.
    return os.getenv("AUTOPILOT_POST_STAGE_5", "false").strip().lower() not in (
        "1", "true", "yes",
    )


def review_page(db: Database, *, top: int, max_age: int | None = None,
                all_tiers: bool = False, seen: str = UNSEEN_ONLY) -> dict:
    """Fetch and mark one page of review candidates.

    Shared by `next` and `refresh`'s auto-chain (C-5) so the two cannot
    disagree about what "the top N" means — the chain reimplementing this
    would be the same drift R-1 collapsed elsewhere.

    ``seen`` selects the feed (never shown) or the backlog (shown, still
    undecided). The feed is the default: a run that repeats the previous
    run's page is stale, not helpful.
    """
    max_age = review_max_age_days() if max_age is None else max_age
    statuses = REVIEW_STATUSES if (all_tiers or not high_signal_only()) \
        else ENRICHED_STATUSES
    rows = db.get_review_queue(
        limit=top,
        max_age_days=max_age or None,
        statuses=statuses,
        seen=seen,
    )
    cards = [_card(r) for r in rows]
    # Re-stamping the backlog would reorder it by re-presentation rather than
    # by quality, so only the feed records delivery.
    if seen == UNSEEN_ONLY:
        db.mark_presented([r["id"] for r in rows])

    awaiting = 0
    if statuses == ENRICHED_STATUSES:
        fresh = db.fresh_counts_by_status(max_age or 0)
        awaiting = sum(n for st, n in fresh.items() if st not in ENRICHED_STATUSES)

    return {
        "count": len(cards),
        "listings": cards,
        "max_age_days": max_age,
        "seen": seen,
        # Counted against the SAME tier filter as the view — an all-tier count
        # here once blamed staleness for what was an enrichment shortfall.
        "hidden_stale": (
            db.count_stale_reviewable(max_age, statuses) if max_age else 0
        ),
        "awaiting_enrichment": awaiting,
        # The backlog stays visible without costing a page slot — this is what
        # makes retiring a shown listing safe rather than lossy.
        "backlog": db.count_seen_undecided(
            max_age_days=max_age or None, statuses=statuses,
        ),
        "tiers": list(statuses),
    }


def _backlog_note(page: dict) -> str:
    """One line keeping shown-but-undecided listings visible.

    This is what pays for retirement: the feed never repeats itself, and the
    rows it retired are still counted and one command away.
    """
    n = page.get("backlog") or 0
    if not n:
        return ""
    return (f"{n} listing(s) shown earlier are still undecided — "
            "`next --seen` to revisit.")


def _fmt_page(page: dict) -> str:
    """Human rendering of a review page, with the stale-hidden footnote."""
    backlog = _backlog_note(page)
    if not page["listings"]:
        if page.get("seen") == SEEN_ONLY:
            return "Nothing shown earlier is still undecided."
        # Steer by the dominant cause, in order of usefulness: fresh listings
        # awaiting enrichment beat stale ones, which beat a genuinely empty
        # queue. The wrong steer here sent users to `--max-age 0` (stale rows)
        # when 35 fresh un-enriched listings were the actual opportunity.
        if page.get("awaiting_enrichment"):
            out = (
                f"No new enriched listings — {page['awaiting_enrichment']} fresh "
                "listing(s) are awaiting autopilot enrichment.\n"
                "Run a refresh to enrich the next batch, or "
                "`next --all-tiers` to review them raw."
            )
        elif page["hidden_stale"]:
            out = (
                f"Nothing new to review — {page['hidden_stale']} listing(s) are "
                f"older than {page['max_age_days']} days and were hidden.\n"
                "Run a refresh for new listings, or `next --max-age 0` to see them."
            )
        else:
            out = "Nothing new to review. Run a refresh to bring in more."
        return f"{out}\n{backlog}" if backlog else out
    out = "\n\n".join(
        _fmt_card(c, i) for i, c in enumerate(page["listings"], 1)
    )
    if page["hidden_stale"]:
        out += (f"\n({page['hidden_stale']} older than "
                f"{page['max_age_days']}d hidden — `--max-age 0` to include.)")
    if backlog and page.get("seen") != SEEN_ONLY:
        out += f"\n{backlog}"
    return out


def cmd_next(db: Database, top: int, as_json: bool, max_age: int | None,
             all_tiers: bool = False, seen: bool = False) -> int:
    page = review_page(db, top=top, max_age=max_age, all_tiers=all_tiers,
                       seen=SEEN_ONLY if seen else UNSEEN_ONLY)
    payload = {"verb": "next", **page}

    human = _fmt_page(page)
    if page["listings"]:
        human += "\n\nDeep-dive one, pass what doesn't fit, or run `next` for more."
    _emit(payload, as_json, human)
    return 0


def cmd_sweep(db: Database, limit: int, as_json: bool) -> int:
    """Process Slack reactions from here, billing ✏️ to the subscription.

    Same reaction logic as `python -m src.sweeper` — it *is* that code, with
    the tailor step deferred. 👍/👎 apply immediately (no LLM, no spend); ✏️
    ids come back in `pending_tailors` for `cli tailor <id>` to answer
    in-session, which is the only difference: Slack's own sweeper has no
    session to hand a prompt to, so its ✏️ costs ~$0.11 through OpenRouter.
    """
    from src.sweeper import sweep

    try:
        counts = sweep(limit=limit, defer_tailor=True)
    except Exception as exc:
        logger.error("Sweep failed: %s", exc)
        _emit({"verb": "sweep", "ok": False, "error": "sweep_failed",
               "detail": str(exc)},
              as_json, f"Sweep failed: {exc}")
        return 1

    pending = counts.get("deferred_tailors", [])
    payload = {"verb": "sweep", "ok": True,
               "passed": counts.get("passed", 0),
               "saved": counts.get("saved", 0),
               "skipped": counts.get("skipped", 0),
               "pending_tailors": pending}

    lines = [f"Swept Slack: {payload['passed']} passed, {payload['saved']} saved"
             f", {payload['skipped']} already current."]
    if pending:
        lines.append(
            f"{len(pending)} listing(s) reacted ✏️ and are waiting to be "
            "tailored in-session (no API cost):")
        lines += [f"  python -m src.cli tailor {jid}" for jid in pending]
    _emit(payload, as_json, "\n".join(lines))
    return 0


def cmd_saved(db: Database, top: int, as_json: bool) -> int:
    """Listings you decided to keep — saved or already tailored."""
    rows = db.get_decided(("saved", "tailored"), limit=top)
    cards = [_card(r) for r in rows]
    human = "\n\n".join(_fmt_card(c, i) for i, c in enumerate(cards, 1)) \
        or "Nothing saved yet."
    _emit({"verb": "saved", "ok": True, "count": len(cards), "listings": cards},
          as_json, human)
    return 0


def cmd_show(db: Database, job_id: str, as_json: bool) -> int:
    row = db.get_listing_by_id(job_id)
    if row is None:
        _emit(
            {"verb": "show", "ok": False, "error": "not_found", "id": job_id},
            as_json,
            f"No listing with id {job_id}",
        )
        return 1

    card = _card(row, detail=True)
    human = _fmt_card(card)
    if card.get("job_summary"):
        human += f"\n\n{card['job_summary']}"
    if card.get("reason"):
        human += f"\n\nWhy Stage 5 scored {card['confidence']}%: {card['reason']}"
    if card.get("matching_skills"):
        human += f"\n\nMatching: {', '.join(card['matching_skills'])}"
    if card.get("missing_skills"):
        human += f"\nMissing:  {', '.join(card['missing_skills'])}"
    _emit({"verb": "show", "ok": True, "listing": card}, as_json, human)
    return 0


def _age_hours(stamp: str | None) -> float | None:
    """Hours since an ISO timestamp, or None if absent/unparseable."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600.0


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{hours * 60:.0f}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def cmd_status(db: Database, as_json: bool) -> int:
    """Is it worth running the pipeline today, and can it afford to?

    Read-only and free. Exists because the batch is run by hand — the daily
    decision deserves numbers rather than reflex.
    """
    from src.budget import check_run_allowed

    stats = db.get_queue_stats()
    decision = check_run_allowed()
    ingest_age = _age_hours(stats["last_ingest"])
    max_age = review_max_age_days()
    stale = db.count_stale_reviewable(max_age) if max_age else 0
    fresh = max(0, stats["reviewable"] - stale)
    fresh_by = db.fresh_counts_by_status(max_age) if max_age else {}
    ready = sum(n for st, n in fresh_by.items() if st in ENRICHED_STATUSES)
    awaiting = fresh - ready if max_age else 0
    # The feed retires what it shows, so the backlog must be reported
    # somewhere or those listings become invisible rather than merely quiet.
    tiers = ENRICHED_STATUSES if high_signal_only() else REVIEW_STATUSES
    backlog = db.count_seen_undecided(
        max_age_days=max_age or None, statuses=tiers,
    )
    # Enrichment capacity. Autopilot is what puts cards in Slack and rows in
    # the enriched feed, so when its daily cap is spent a refresh still costs
    # money and still ingests, but produces no new cards anywhere — which
    # looks like a broken integration unless something says so.
    from src.process_queue import _top_n
    enrich_cap = _top_n()
    enriched_today = db.count_autopilot_processed_today()

    payload = {
        "verb": "status",
        "queue": {
            "reviewable": stats["reviewable"],
            "fresh": fresh,
            "ready": ready,
            "backlog": backlog,
            "awaiting_enrichment": awaiting,
            "enrichment_cap": enrich_cap,
            "enriched_today": enriched_today,
            "enrichment_remaining": max(0, enrich_cap - enriched_today),
            "stale_hidden": stale,
            "max_age_days": max_age,
            "by_tier": stats["by_status"],
            "total_listings": stats["total_listings"],
            "last_ingest": stats["last_ingest"],
            "last_ingest_age_hours": round(ingest_age, 1) if ingest_age else None,
            "last_decision": stats["last_decision"],
        },
        "budget": {
            "can_run": decision.allowed,
            "reason": decision.reason,
            "spent_usd_today": (
                round(decision.spent_usd, 4) if decision.spent_usd is not None else None
            ),
            "spent_tokens_today": decision.spent_tokens,
            "budget_usd": decision.budget_usd,
            "remaining_usd": (
                round(decision.remaining_usd, 4)
                if decision.remaining_usd is not None else None
            ),
            "minutes_since_run": (
                round(decision.minutes_since_run)
                if decision.minutes_since_run is not None else None
            ),
        },
    }

    remaining = max(0, enrich_cap - enriched_today)
    lines = [
        f"Queue:   {ready} new  ·  {backlog} undecided from earlier  ·  "
        f"{awaiting} awaiting enrichment  ·  {stale} stale  "
        f"({stats['reviewable']} total)",
        f"Enrich:  {enriched_today}/{enrich_cap} used today"
        + ("   ⚠️  cap reached — a refresh will ingest but produce no new "
           "cards until tomorrow" if remaining == 0 else
           f"   ·   {remaining} left"),
        f"Ingest:  {_fmt_age(ingest_age)}"
        f"   ·   last decision {_fmt_age(_age_hours(stats['last_decision']))}",
    ]
    spent = payload["budget"]["spent_usd_today"]
    if spent is not None and decision.budget_usd > 0:
        lines.append(
            f"Spend:   ${spent:.2f} of ${decision.budget_usd:.2f} today"
            f"  ({decision.spent_tokens:,} tokens)"
        )
    else:
        lines.append(f"Spend:   {decision.spent_tokens:,} tokens today")
    lines.append(("Run:     ✅ allowed — " if decision.allowed
                  else "Run:     ⛔ blocked — ") + decision.reason)

    # The hint keys off READY work: stale rows must not stop status from
    # recommending a refresh, and a fresh-but-unenriched backlog should steer
    # to enrichment rather than to `--max-age 0`.
    if ready == 0 and awaiting > 0:
        lines.append(
            f"\nNothing enriched yet — a refresh would enrich the top of the "
            f"{awaiting} fresh listing(s) waiting."
        )
    elif ready == 0:
        tail = f" ({stale} stale hidden)" if stale else ""
        lines.append(
            f"\nNothing fresh to review{tail} — a refresh would give you new listings."
        )
    elif ingest_age is not None and ingest_age < 12:
        lines.append("\nQueue is fresh; `next` before spending on another run.")

    _emit(payload, as_json, "\n".join(lines))
    return 0


def _detached_log_path(stage: str) -> Path:
    """One log file per detachable stage, e.g. ``logs/refresh-digest-a.log``.

    Per stage rather than per run so the two digests never interleave, and
    append rather than truncate so yesterday's failure is still there when
    someone finally reads it (same convention as logs/model_usage.log).
    """
    slug = "-".join("".join(c if c.isalnum() else " " for c in stage).split())
    return LOG_DIR / f"refresh-{slug.lower()}.log"


def _launch_detached(stage: str, module: str, env: dict) -> dict:
    """Start a stage and return without waiting for it.

    Output goes to a file, never to an inherited pipe: the pipe closes when
    ``refresh`` returns and the child dies on its next write. ``start_new_session``
    puts the child in its own process group so a Ctrl-C in the terminal — or
    cron reaping the shell — doesn't take Slack posting with it.
    """
    path = _detached_log_path(stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as log:
        log.write(f"\n=== {stamp} refresh detached {module} ===\n")
        log.flush()
        subprocess.Popen(
            [sys.executable, "-m", module],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {"stage": stage, "module": module, "status": "detached",
            "returncode": None, "seconds": 0.0, "log": str(path)}


def _fmt_stage(result: dict) -> str:
    marks = {"ok": "ok", "failed": "FAIL", "detached": ".."}
    line = f"  {marks[result['status']]:<4}  {result['stage']}"
    if result["status"] == "detached":
        line += f" — detached → {result['log']}"
    return line


def cmd_refresh(db: Database, *, top_n: int | None, force: bool,
                dry_run: bool, as_json: bool, no_next: bool = False,
                wait: bool = False) -> int:
    """Fire the ingestion pipeline, budget permitting.

    **Owns the stage sequence.** ``script.sh`` is a thin wrapper over this verb
    so the sequence exists once (R-1): duplicating it in shell would put the
    budget gate on only one of two paths.

    Orchestrates, never reimplements — each stage runs as its own subprocess
    exactly as ``script.sh`` used to invoke it, so a stage's behavior is
    unchanged and a crash is attributable to one module.

    **Failure policy (C-9).** A failed stage does not cancel the ones after it;
    see ``_MAX_CONSECUTIVE_STAGE_FAILURES`` for why, and for the one case that
    does stop the chain. The budget is deliberately *not* re-checked between
    stages: it is a pre-run admission gate, ``record_run`` has already fired,
    and killing an admitted run halfway on its own spending is a different
    policy nobody asked for.

    **Detached stages (C-10).** Slack posting leaves the critical path but not
    the product: the digests are launched and not awaited, so Track B and
    autopilot start immediately. Their outcome lands in their log, not in this
    verb's envelope — which is why they can neither trip nor reset the breaker.
    """
    from src.budget import check_run_allowed, record_run

    decision = check_run_allowed()
    stages = list(REFRESH_STAGES)
    # Detaching lets the digest post while autopilot works. Safe only while the
    # two are looking at different rows: high-signal mode posts triaged/saved
    # and autopilot enriches auto_queued, so the populations are disjoint. With
    # AUTOPILOT_POST_STAGE_5=true the digest also posts auto_queued rows, and a
    # concurrent autopilot that read `slack_message_ts` before the digest wrote
    # it would post a second card for the same listing — a window only
    # process_queue can close. Run them in-line instead until it does.
    detach = not wait and high_signal_only()

    if dry_run:
        def _label(name: str, module: str) -> str:
            return (f"{name} [detached]"
                    if detach and module in _DETACHABLE_MODULES else name)
        _emit(
            {"verb": "refresh", "ok": True, "dry_run": True,
             "would_run": [s[0] for s in stages],
             "allowed": decision.allowed, "reason": decision.reason,
             "spent_usd_today": decision.spent_usd,
             "budget_usd": decision.budget_usd},
            as_json,
            f"Would run: {' → '.join(_label(n, m) for n, m in stages)}\n"
            f"Budget: {decision.reason}\n"
            + ("Allowed." if decision.allowed else "BLOCKED — would refuse."),
        )
        return 0

    if not decision.allowed and not force:
        _emit(
            {"verb": "refresh", "ok": False, "error": "budget_blocked",
             "reason": decision.reason,
             "spent_usd_today": decision.spent_usd,
             "budget_usd": decision.budget_usd},
            as_json,
            f"Refused — {decision.reason}\n"
            "Override with --force if you mean it.",
        )
        return 1

    env = dict(os.environ)
    if top_n is not None:
        # Per-run enrichment budget without editing .env.
        env["AUTOPILOT_TOP_N"] = str(top_n)

    # Recorded BEFORE the stages run: the cooldown must apply to an attempt,
    # not only a success, or a crashing run could be retried without limit.
    record_run("cli")

    # Progress goes to stderr so it never pollutes --json on stdout, and so a
    # human watching a 10-minute scrape can tell the difference between "slow"
    # and "hung". Stage output streams through in human mode: capturing it
    # (as this did originally) turned `./script.sh` into a silent wait, which
    # is worse than the noisy chain it replaced.
    stream = not as_json

    def _progress(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    _progress(f"Running {len(stages)} stages — {' → '.join(n for n, _ in stages)}\n")
    if not detach and not wait:
        _progress("Digests run in-line: AUTOPILOT_POST_STAGE_5 posts the same "
                  "rows autopilot enriches.\n")

    results: list[dict] = []
    failed: list[str] = []
    skipped: list[str] = []
    consecutive = 0
    broke = False
    for i, (name, module) in enumerate(stages, 1):
        _progress(f"[{i}/{len(stages)}] {name} ({module}) …")

        if detach and module in _DETACHABLE_MODULES:
            try:
                record = _launch_detached(name, module, env)
            except OSError:
                logger.error("Stage %s could not be launched", name, exc_info=True)
                record = {"stage": name, "module": module, "status": "failed",
                          "returncode": -1, "seconds": 0.0, "log": None}
            results.append(record)
            if record["status"] == "detached":
                _progress(f"[{i}/{len(stages)}] {name} — detached, logging to "
                          f"{record['log']}\n")
                # Not observed, so neither trips nor resets the breaker.
                continue
            failed.append(name)
            consecutive += 1
        else:
            started = time.monotonic()
            proc = subprocess.run(
                [sys.executable, "-m", module],
                env=env,
                capture_output=not stream,
                text=True,
            )
            elapsed = time.monotonic() - started
            ok = proc.returncode == 0
            results.append({"stage": name, "module": module,
                            "status": "ok" if ok else "failed",
                            "returncode": proc.returncode,
                            "seconds": round(elapsed, 1), "log": None})
            mark = "ok" if ok else f"FAILED rc={proc.returncode}"
            _progress(f"[{i}/{len(stages)}] {name} — {mark} in {elapsed:.0f}s\n")
            if ok:
                consecutive = 0
                continue
            failed.append(name)
            consecutive += 1
            if not stream and proc.stderr:
                _progress(proc.stderr[-800:])
            logger.error("Stage %s failed (rc=%d)", name, proc.returncode)

        if consecutive >= _MAX_CONSECUTIVE_STAGE_FAILURES:
            broke = True
            skipped = [s for s, _ in stages[i:]]
            logger.error("Circuit breaker: %d consecutive stage failures — "
                         "abandoning %s", consecutive, ", ".join(skipped) or "nothing")
            _progress(f"Circuit breaker: {consecutive} stages failed in a row — "
                      "stopping.\n")
            break

    after = check_run_allowed()
    spent = None
    if after.spent_usd is not None and decision.spent_usd is not None:
        spent = round(after.spent_usd - decision.spent_usd, 4)

    succeeded = [r["stage"] for r in results if r["status"] == "ok"]
    detached = [r for r in results if r["status"] == "detached"]
    # `ok` still means "everything succeeded", so the existing contract holds.
    # `partial` is the new state fail-fast used to hide: work landed AND
    # something broke. Detached stages are neither — their result is in their
    # log, not here.
    payload = {"verb": "refresh", "ok": not failed,
               "partial": bool(failed and succeeded),
               "stages": results,
               "failed_stage": failed[0] if failed else None,
               "failed_stages": failed,
               "skipped_stages": skipped,
               "spent_usd_this_run": spent,
               "spent_usd_today": after.spent_usd, "page": None}

    # C-5: chain straight into the first page, partial run included. The
    # listings that did land are real, and the user asked what's good today —
    # reporting a failure with nothing to show is the regression C-5 closed.
    # --no-next opts out for scripting.
    page = None
    if not no_next:
        page = review_page(db, top=DEFAULT_TOP)
        payload["page"] = page

    human = "\n".join(_fmt_stage(r) for r in results)
    if broke:
        tail = f"{', '.join(skipped)} did not run" if skipped else "nothing was left to run"
        human += (f"\n\nStopped after {_MAX_CONSECUTIVE_STAGE_FAILURES} stages "
                  f"failed in a row ({', '.join(failed[-2:])}) — {tail}. "
                  "Consecutive failures usually mean a credential or a "
                  "provider, not a bad listing.")
    elif failed:
        human += (f"\n\n{', '.join(failed)} failed — see the logs. "
                  "Every later stage still ran.")
    if detached:
        human += ("\n\nSlack posting is still running in the background — "
                  "this verb does not wait for it.")
    if spent is not None:
        human += f"\n\nThis run cost ~${spent:.4f}"
        if after.budget_usd > 0 and after.spent_usd is not None:
            human += f" (${after.spent_usd:.2f} of ${after.budget_usd:.2f} today)"
    if page is not None:
        human += "\n\n" + _fmt_page(page)
        if page["listings"]:
            human += "\n\nDeep-dive one, or pass what doesn't fit."
    else:
        human += "\n\nRun `next` to review what came in."
    _emit(payload, as_json, human)
    # Non-zero only when nothing worked or the breaker abandoned the chain. A
    # run that scraped and enriched but failed to post to Slack is not a failed
    # run — script.sh execs this verb, so this is also the shell's exit code.
    return 0 if succeeded and not broke else 1


def cmd_deep_dive(db: Database, job_id: str, as_json: bool) -> int:
    """Everything known about one listing, from local cache only.

    Never runs Deep Research on a miss: that is a multi-second, token-spending
    network call, and this verb sits inside a conversational loop. A miss is
    reported so the user can choose.
    """
    row = db.get_listing_by_id(job_id)
    if row is None:
        _emit(
            {"verb": "deep-dive", "ok": False, "error": "not_found", "id": job_id},
            as_json,
            f"No listing with id {job_id}",
        )
        return 1

    card = _card(row, detail=True)
    folder = _output_folder(row["id"])
    context = _read_text(folder / RESEARCH_FILE) if folder else None
    post = _post_research(folder, row["confidence"]) if folder else None

    payload = {
        "verb": "deep-dive",
        "ok": True,
        "listing": card,
        "research": {
            "cached": context is not None,
            "folder": str(folder) if folder else None,
            "context": context,
        },
        "post_research": post,
    }

    human = [_fmt_card(card)]
    if card.get("job_summary"):
        human.append(card["job_summary"])
    if card.get("reason"):
        human.append(f"Stage 5 ({card['confidence']}%): {card['reason']}")

    if post:
        delta = post["confidence_delta"]
        arrow = ""
        if delta is not None:
            arrow = f" ({delta:+d} vs Stage 5)"
        human.append(
            f"Post-research: {post['verdict']} {post['confidence']}%{arrow}"
        )
        if post["match_analysis"]:
            human.append(post["match_analysis"])
        if post["matching_skills"]:
            human.append("Matching: " + ", ".join(post["matching_skills"]))
        if post["missing_skills"]:
            human.append("Missing:  " + ", ".join(post["missing_skills"]))
    if context:
        human.append(f"--- Research dossier ({folder.name}) ---\n{context}")
    else:
        human.append(
            "No research cached for this listing — autopilot hasn't reached it. "
            "Ask if you want it researched."
        )

    _emit(payload, as_json, "\n\n".join(human))
    return 0


def cmd_asset(db: Database, job_id: str, asset: str, *, apply_from: str | None,
              via_api: bool, questions: str, as_json: bool) -> int:
    """Generate one on-demand asset — same handshake as the full tailor.

    polish / cover_letter / interview_prep / answers were previously
    reachable only through Slack ChatOps, which is unattended and therefore
    metered. They are the expensive half of a listing's cost, so the route
    that matters is the in-session one; ``src/tailor.py``'s asset registry
    owns everything except which side answers the prompt.
    """
    from src.tailor import (
        ASSET_SPECS,
        build_asset_prompt,
        generate_asset_via_api,
        parse_asset_response,
        write_asset,
    )

    spec = ASSET_SPECS[asset]
    if db.get_listing_by_id(job_id) is None:
        _emit({"verb": asset, "ok": False, "error": "not_found", "id": job_id},
              as_json, f"No listing with id {job_id}")
        return 1
    if spec.takes_questions and not questions and apply_from is None:
        _emit({"verb": asset, "ok": False, "error": "questions_required",
               "id": job_id},
              as_json, "This asset needs --questions '<the application questions>'")
        return 1

    def _fail(error: str, exc: Exception) -> int:
        _emit({"verb": asset, "ok": False, "error": error, "id": job_id,
               "detail": str(exc)},
              as_json, f"{asset} failed: {exc}")
        return 1

    if via_api:
        try:
            folder, _ = generate_asset_via_api(
                job_id, asset, custom_questions=questions)
        except Exception as exc:
            logger.error("%s via API failed for %s: %s", asset, job_id[:8], exc)
            return _fail("api_failed", exc)
        _emit({"verb": asset, "ok": True, "id": job_id, "route": "api",
               "folder": str(folder)},
              as_json, f"{asset} via OpenRouter → {folder}")
        return 0

    if apply_from is not None:
        raw = sys.stdin.read() if apply_from == "-" else _read_text(Path(apply_from))
        if not raw:
            _emit({"verb": asset, "ok": False, "error": "empty_input",
                   "id": job_id},
                  as_json, "No JSON received to apply.")
            return 1
        try:
            parsed = parse_asset_response(asset, raw)
            folder = write_asset(job_id, asset, parsed)
        except (RuntimeError, ValueError) as exc:
            return _fail("invalid_response", exc)
        _emit({"verb": asset, "ok": True, "id": job_id, "route": "in_session",
               "folder": str(folder)},
              as_json, f"{asset} written in-session → {folder}")
        return 0

    try:
        prompt, _listing = build_asset_prompt(
            job_id, asset, custom_questions=questions)
    except (RuntimeError, ValueError) as exc:
        return _fail("unavailable", exc)
    apply_cmd = f"python -m src.cli {asset.replace('_', '-')} {job_id} --apply -"
    _emit(
        {"verb": asset, "ok": True, "id": job_id, "route": "in_session",
         "stage": "prompt", "prompt": prompt,
         "research_cached": bool(_cached_research(job_id)),
         "apply_with": apply_cmd},
        as_json,
        f"{prompt}\n\n---\nAnswer the above as JSON, then pipe it to:\n  {apply_cmd}",
    )
    return 0


def cmd_tailor(db: Database, job_id: str, *, apply_from: str | None,
               via_api: bool, as_json: bool) -> int:
    """Tailor a resume — in-session by default, via OpenRouter on request.

    Tailoring is one big prompt and one big completion. `tailor.py` already
    separates the three stages (build_prompt → LLM → generate_assets), so the
    middle stage can be served by whoever is cheapest:

    - **default (in-session):** emit the prompt, let the calling Claude
      session answer it, then `--apply` the JSON back. Subscription-billed,
      zero metered cost — which is why research is read from the cache only
      and NEVER run live on this route (see _NO_RESEARCH_PLACEHOLDER).
    - **``--via api``:** the original path through OPENROUTER_TAILOR_MODEL,
      including live Deep Research when uncached. Metered, but works
      headless — cron, batch, no session attached.

    Assets land in ``output/`` and the listing reaches ``tailored`` on either
    route, so downstream (batch_process, the eval harness, Slack) can't tell
    which was used.
    """
    # Imported lazily: tailor pulls openai + dotenv at import time, and the
    # read verbs must stay instant.
    from src.tailor import (
        _parse_tailor_response,
        build_prompt,
        generate_immediate,
    )

    row = db.get_listing_by_id(job_id)
    if row is None:
        _emit({"verb": "tailor", "ok": False, "error": "not_found", "id": job_id},
              as_json, f"No listing with id {job_id}")
        return 1

    if via_api:
        try:
            folder, parsed = generate_immediate(job_id)
        except Exception as exc:
            logger.error("Tailor via API failed for %s: %s", job_id[:8], exc)
            _emit({"verb": "tailor", "ok": False, "error": "api_failed",
                   "id": job_id, "detail": str(exc)},
                  as_json, f"Tailoring failed: {exc}")
            return 1
        # generate_immediate already sets this on its own connection; repeating
        # it here is idempotent and keeps the verb's contract self-contained —
        # "ok means tailored" shouldn't depend on a callee's side effect.
        db.update_pipeline_status(job_id, target_status("tailor"))
        append_human_label(job_id, "tailor", dict(row), surface=SURFACE_CLI)
        _emit({"verb": "tailor", "ok": True, "id": job_id, "route": "api",
               "folder": str(folder), "status": "tailored"},
              as_json, f"Tailored via OpenRouter → {folder}")
        return 0

    if apply_from is not None:
        raw = sys.stdin.read() if apply_from == "-" else _read_text(Path(apply_from))
        if not raw:
            _emit({"verb": "tailor", "ok": False, "error": "empty_input",
                   "id": job_id},
                  as_json, "No JSON received to apply.")
            return 1
        try:
            parsed = _parse_tailor_response(raw)
        except RuntimeError as exc:
            _emit({"verb": "tailor", "ok": False, "error": "invalid_response",
                   "id": job_id, "detail": str(exc)},
                  as_json, f"Could not apply: {exc}")
            return 1

        from src.compile import generate_assets
        research = _cached_research(job_id)
        _, listing, _ = build_prompt(
            job_id,
            research_context_override=research or _NO_RESEARCH_PLACEHOLDER,
        )
        folder = generate_assets(job_id, parsed, listing,
                                 research_context=research)
        db.update_pipeline_status(job_id, target_status("tailor"))
        append_human_label(job_id, "tailor", dict(row), surface=SURFACE_CLI)
        _emit({"verb": "tailor", "ok": True, "id": job_id, "route": "in_session",
               "folder": str(folder), "status": "tailored"},
              as_json, f"Tailored in-session → {folder}")
        return 0

    # Default: emit the prompt for the session to answer. Research comes
    # from the cache only — the in-session route never spends tokens, so a
    # missing dossier is reported, not repaired (--via api runs it live).
    research = _cached_research(job_id)
    prompt, listing, _ = build_prompt(
        job_id,
        research_context_override=research or _NO_RESEARCH_PLACEHOLDER,
    )
    _emit(
        {"verb": "tailor", "ok": True, "id": job_id, "route": "in_session",
         "stage": "prompt", "prompt": prompt,
         "research_cached": bool(research),
         "apply_with": f"python -m src.cli tailor {job_id} --apply -"},
        as_json,
        f"{prompt}\n\n---\nAnswer the above as JSON, then pipe it to:\n"
        f"  python -m src.cli tailor {job_id} --apply -",
    )
    return 0


def _decide(db: Database, row: sqlite3.Row, verb: str, *, bulk: bool) -> bool:
    """Apply one decision. True if the status moved. See src/decisions.py."""
    return apply_decision(db, row, verb, surface=SURFACE_CLI, bulk=bulk)


def cmd_decide(db: Database, verb: str, job_id: str | None, all_flag: bool,
               as_json: bool) -> int:
    if all_flag:
        rows = db.get_presented_page(SESSION_WINDOW_MINUTES)
        decided = [r["id"] for r in rows if _decide(db, r, verb, bulk=True)]
        _emit(
            {"verb": verb, "ok": True, "ids": decided, "count": len(decided),
             "bulk": True},
            as_json,
            f"{_PAST_TENSE[verb]} {len(decided)} listing(s) from the current page."
            if decided else "Nothing on the current page to act on.",
        )
        return 0

    row = db.get_listing_by_id(job_id)
    if row is None:
        _emit(
            {"verb": verb, "ok": False, "error": "not_found", "id": job_id},
            as_json,
            f"No listing with id {job_id}",
        )
        return 1

    if not _decide(db, row, verb, bulk=False):
        status, _ = DECISIONS[verb]
        _emit(
            {"verb": verb, "ok": False, "error": "no_transition", "id": job_id,
             "status": row["pipeline_status"]},
            as_json,
            f"Not moved — {row['title']} is already {row['pipeline_status']}.",
        )
        return 1

    status, _ = DECISIONS[verb]
    _emit(
        {"verb": verb, "ok": True, "id": job_id, "status": status, "bulk": False},
        as_json,
        f"{_PAST_TENSE[verb]}: {row['title']} — {row['company']}",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Review surface for triaged job listings.",
    )
    _JSON_HELP = "Emit machine-readable JSON (the skill's interface)"
    parser.add_argument("--json", action="store_true", help=_JSON_HELP)

    # --json is accepted on either side of the verb: `--json next` and
    # `next --json` both work, because a skill (or a human) will reach for
    # whichever reads naturally. SUPPRESS is what makes that safe — without
    # it the subparser's default would overwrite a flag set before the verb.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS, help=_JSON_HELP)

    sub = parser.add_subparsers(dest="verb", required=True)

    p_next = sub.add_parser("next", parents=[common],
                            help="Show the next page of candidates")
    p_next.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"How many to show (default: {DEFAULT_TOP})")
    p_next.add_argument("--all-tiers", action="store_true", dest="all_tiers",
                        help="Include un-enriched Stage 5 rows (debugging)")
    p_next.add_argument("--max-age", type=int, default=None, dest="max_age",
                        metavar="DAYS",
                        help=f"Hide listings ingested more than DAYS ago "
                             f"(default: {DEFAULT_MAX_AGE_DAYS}; 0 disables)")
    p_next.add_argument("--seen", action="store_true",
                        help="The backlog: shown earlier, still undecided")

    p_saved = sub.add_parser("saved", parents=[common],
                             help="Listings you saved or tailored")
    p_saved.add_argument("--top", type=int, default=10,
                         help="How many to show (default: 10)")

    p_sweep = sub.add_parser(
        "sweep", parents=[common],
        help="Process Slack reactions; ✏️ comes back for in-session tailoring")
    p_sweep.add_argument("--limit", type=int, default=50,
                         help="Messages to scan (default: 50)")

    sub.add_parser("status", parents=[common],
                   help="Queue freshness and today's spend against budget")

    p_refresh = sub.add_parser(
        "refresh", parents=[common],
        help="Run the ingestion pipeline (budget-gated) and report what it cost")
    p_refresh.add_argument("--top-n", type=int, default=None, dest="top_n",
                           metavar="N",
                           help="Autopilot enrichment budget for this run only")
    p_refresh.add_argument("--force", action="store_true",
                           help="Run even if the budget check refuses")
    p_refresh.add_argument("--dry-run", action="store_true", dest="dry_run",
                           help="Show the stages and budget verdict, run nothing")
    p_refresh.add_argument("--no-next", action="store_true", dest="no_next",
                           help="Don't show the first page afterwards")
    p_refresh.add_argument("--wait", action="store_true",
                           help="Run the Slack digests in-line instead of "
                                "detaching them (cron/CI)")

    p_show = sub.add_parser("show", parents=[common],
                            help="Show one listing in full")
    p_show.add_argument("id")

    p_dive = sub.add_parser("deep-dive", parents=[common],
                            help="Full detail: post-research verdict + dossier")
    p_dive.add_argument("id")

    p_tailor = sub.add_parser(
        "tailor", parents=[common],
        help="Tailor a resume (in-session by default; --via api to spend tokens)")
    p_tailor.add_argument("id")
    p_tailor.add_argument(
        "--apply", dest="apply_from", metavar="PATH",
        help="Apply model JSON from PATH ('-' for stdin) and write assets")
    p_tailor.add_argument(
        "--via", choices=("session", "api"), default="session",
        help="session (default, subscription-billed) or api (OpenRouter)")

    # On-demand assets. Same three flags as `tailor` because they are the same
    # handshake — a verb that is correct alone but tells a different story
    # than its neighbours is a defect. Hyphens on the wire, underscores
    # internally (the generate_assets vocabulary).
    for verb, helptext in _ASSET_VERBS:
        p = sub.add_parser(verb, parents=[common], help=helptext)
        p.add_argument("id")
        p.add_argument(
            "--apply", dest="apply_from", metavar="PATH",
            help="Apply model JSON from PATH ('-' for stdin) and write assets")
        p.add_argument(
            "--via", choices=("session", "api"), default="session",
            help="session (default, subscription-billed) or api (OpenRouter)")
        if verb == "answers":
            p.add_argument(
                "--questions", default="",
                help="The application questions to answer")

    for verb, helptext in (("save", "Mark a listing saved"),
                           ("pass", "Mark a listing passed")):
        p = sub.add_parser(verb, parents=[common], help=helptext)
        p.add_argument("id", nargs="?", default=None)
        p.add_argument("--all", action="store_true",
                       help="Apply to every listing on the current page")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-8s %(name)s - %(message)s")
    load_dotenv()
    args = build_parser().parse_args(argv)

    # Which verbs accept `--all` is an argument-parsing fact, not decision
    # policy — keying it off DECISIONS coupled it to that table's contents and
    # broke `tailor` the moment tailor was added there.
    if args.verb in _BULK_CAPABLE_VERBS and not args.all and not args.id:
        print(f"`{args.verb}` needs a listing id, or --all for the current page.",
              file=sys.stderr)
        return 2

    db = Database()
    try:
        if args.verb == "status":
            return cmd_status(db, args.json)
        if args.verb == "refresh":
            return cmd_refresh(db, top_n=args.top_n, force=args.force,
                               dry_run=args.dry_run, as_json=args.json,
                               no_next=args.no_next, wait=args.wait)
        if args.verb == "next":
            return cmd_next(db, args.top, args.json, args.max_age,
                            args.all_tiers, args.seen)
        if args.verb == "saved":
            return cmd_saved(db, args.top, args.json)
        if args.verb == "sweep":
            return cmd_sweep(db, args.limit, args.json)
        if args.verb == "show":
            return cmd_show(db, args.id, args.json)
        if args.verb == "deep-dive":
            return cmd_deep_dive(db, args.id, args.json)
        if args.verb in _ASSET_VERB_NAMES:
            return cmd_asset(
                db, args.id, _ASSET_VERB_NAMES[args.verb],
                apply_from=args.apply_from,
                via_api=(args.via == "api"),
                questions=getattr(args, "questions", ""),
                as_json=args.json,
            )
        if args.verb == "tailor":
            return cmd_tailor(db, args.id, apply_from=args.apply_from,
                              via_api=args.via == "api", as_json=args.json)
        return cmd_decide(db, args.verb, args.id, args.all, args.json)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
