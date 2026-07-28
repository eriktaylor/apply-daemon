"""Review surface CLI — the deterministic layer a Claude skill drives.

Replaces Slack thread commands (frozen; see docs/CHATOPS.md) with verbs that
are testable in-process:

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
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.db import Database
from src.human_labels import SURFACE_CLI, append_human_label

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")
RESEARCH_FILE = "deep_research_context.txt"
AUTO_ASSETS_FILE = "auto_assets.json"

# Minutes a presented page stays "current". Long enough that `next` pages
# forward across a working sitting; short enough that a listing skipped this
# morning is offered again tonight.
SESSION_WINDOW_MINUTES = 120

DEFAULT_TOP = 3

_TIER_LABELS = {0: "auto", 1: "auto_queued", 2: "triaged"}

# Verb → (pipeline_status, ledger action). Ledger actions must match the
# vocabulary eval/preference_pairs.py scores (save → positive, pass →
# negative), or CLI decisions land in the ledger as neutral and vanish from
# the preference pairs.
_DECISIONS = {
    "save": ("saved", "save"),
    "pass": ("passed", "pass"),
}

# Past-tense forms for human output ("Saveed" otherwise).
_PAST_TENSE = {"save": "Saved", "pass": "Passed"}

# Statuses a listing cannot be saved back out of. Mirrors the documented
# Slack rule that 👎 is terminal (docs/CHATOPS.md) — reviving a passed
# listing goes through re-triage, not a save.
_TERMINAL_STATUSES = frozenset({"passed", "expired"})


def _json_list(raw: object) -> list:
    """Parse a TEXT column holding a JSON array; [] on anything unusable."""
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _output_folder(job_id: str) -> Path | None:
    """Locate this job's asset folder.

    Mirrors tailor._find_existing_output's ``job_id[:8]`` folder convention,
    reimplemented here so the CLI doesn't import tailor (which pulls openai
    and dotenv at import time and would slow every invocation).
    """
    if not OUTPUT_DIR.exists():
        return None
    for folder in OUTPUT_DIR.iterdir():
        if folder.is_dir() and job_id[:8] in folder.name:
            return folder
    return None


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


def _skill_list(raw: object) -> list:
    """Parse a skills value that may be a list, a JSON string, or junk.

    ``updated_skills_match`` values arrive as either shape depending on which
    model wrote them, so both are accepted.
    """
    if isinstance(raw, list):
        return [str(s) for s in raw]
    if isinstance(raw, str):
        return _json_list(raw)
    return []


def _post_research(folder: Path, triage_confidence: int | None) -> dict | None:
    """Read autopilot's post-research re-score, if autopilot has run.

    This is the large model's verdict after reading the research dossier, and
    it frequently disagrees with the Stage 5 score shown in `next` — that
    disagreement is the main thing a deep-dive exists to surface, so the
    delta is computed rather than left for the reader to eyeball.
    """
    raw = _read_text(folder / AUTO_ASSETS_FILE)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Malformed %s in %s", AUTO_ASSETS_FILE, folder.name)
        return None
    if not isinstance(data, dict):
        return None

    skills = data.get("updated_skills_match") or {}
    if not isinstance(skills, dict):
        skills = {}

    verdict = data.get("post_research_verdict")
    confidence = data.get("post_research_confidence")
    try:
        confidence = int(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    delta = None
    if confidence is not None and triage_confidence is not None:
        delta = confidence - triage_confidence

    return {
        "verdict": verdict,
        "confidence": confidence,
        "confidence_delta": delta,
        "match_analysis": data.get("match_analysis"),
        "matching_skills": _skill_list(skills.get("matching")),
        "missing_skills": _skill_list(skills.get("missing")),
    }


def _tier_of(row: sqlite3.Row) -> str:
    keys = row.keys()
    if "tier_rank" in keys:
        return _TIER_LABELS.get(row["tier_rank"], "triaged")
    return str(row["pipeline_status"])


def _card(row: sqlite3.Row, *, detail: bool = False) -> dict:
    """Serialize a listing row for JSON output.

    ``raw_email_text`` is deliberately absent — it is raw email content, and
    this output reaches a model context and potentially logs.
    """
    links = _json_list(row["links"])
    card = {
        "id": row["id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "salary": row["salary"],
        "verdict": row["verdict"],
        "confidence": row["confidence"],
        "status": row["pipeline_status"],
        "tier": _tier_of(row),
        "research_cached": _research_cached(row["id"]),
        "url": links[0] if links else None,
        "date_ingested": row["date_ingested"],
    }
    if detail:
        card["reason"] = row["reason"]
        card["job_summary"] = row["job_summary"]
        card["matching_skills"] = _json_list(row["matching_skills"])
        card["missing_skills"] = _json_list(row["missing_skills"])
    return card


def _emit(payload: dict, as_json: bool, human: str) -> None:
    print(json.dumps(payload, indent=2) if as_json else human)


def _fmt_card(card: dict, index: int | None = None) -> str:
    prefix = f"[{index}] " if index is not None else ""
    free = " (research cached — deep-dive is free)" if card["research_cached"] else ""
    lines = [
        f"{prefix}{card['title']} — {card['company']}",
        f"    {card['verdict']} {card['confidence']}%  ·  {card['tier']}{free}",
    ]
    if card.get("location"):
        lines.append(f"    {card['location']}")
    if card.get("salary"):
        lines.append(f"    {card['salary']}")
    if card.get("url"):
        lines.append(f"    {card['url']}")
    lines.append(f"    id: {card['id']}")
    return "\n".join(lines)


def cmd_next(db: Database, top: int, as_json: bool) -> int:
    rows = db.get_review_queue(
        limit=top, session_window_minutes=SESSION_WINDOW_MINUTES
    )
    cards = [_card(r) for r in rows]
    db.mark_presented([r["id"] for r in rows])

    if not cards:
        _emit(
            {"verb": "next", "count": 0, "listings": []},
            as_json,
            "Nothing left to review. Run the pipeline, or wait for the "
            f"{SESSION_WINDOW_MINUTES}-minute window to release skipped listings.",
        )
        return 0

    human = "\n\n".join(_fmt_card(c, i) for i, c in enumerate(cards, 1))
    human += "\n\nDeep-dive one, pass what doesn't fit, or run `next` for more."
    _emit({"verb": "next", "count": len(cards), "listings": cards}, as_json, human)
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
        human += f"\n\nWhy this scored {card['confidence']}%: {card['reason']}"
    if card.get("matching_skills"):
        human += f"\n\nMatching: {', '.join(card['matching_skills'])}"
    if card.get("missing_skills"):
        human += f"\nMissing:  {', '.join(card['missing_skills'])}"
    _emit({"verb": "show", "ok": True, "listing": card}, as_json, human)
    return 0


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
        db.update_pipeline_status(job_id, "tailored")
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
        db.update_pipeline_status(job_id, "tailored")
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
    """Apply one decision: pipeline_status + ledger. True if the status moved.

    ``db.update_pipeline_status`` is an unconditional UPDATE — it happily
    re-applies a status a row already has, and would let a save undo a pass.
    The guard therefore lives here: no-op when the row is already at the
    target status, and never save a listing back out of a terminal one.
    Without it a repeated `pass` would append a duplicate ledger row every
    time, inflating the preference-pair corpus with phantom decisions.
    """
    status, action = _DECISIONS[verb]
    current = row["pipeline_status"]
    if current == status:
        return False
    if verb == "save" and current in _TERMINAL_STATUSES:
        return False

    moved = db.update_pipeline_status(row["id"], status)
    if moved:
        append_human_label(
            row["id"], action, dict(row), surface=SURFACE_CLI, bulk=bulk
        )
    return moved


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
        status, _ = _DECISIONS[verb]
        _emit(
            {"verb": verb, "ok": False, "error": "no_transition", "id": job_id,
             "status": row["pipeline_status"]},
            as_json,
            f"Not moved — {row['title']} is already {row['pipeline_status']}.",
        )
        return 1

    status, _ = _DECISIONS[verb]
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

    if args.verb in _DECISIONS and not args.all and not args.id:
        print(f"`{args.verb}` needs a listing id, or --all for the current page.",
              file=sys.stderr)
        return 2

    db = Database()
    try:
        if args.verb == "next":
            return cmd_next(db, args.top, args.json)
        if args.verb == "show":
            return cmd_show(db, args.id, args.json)
        if args.verb == "deep-dive":
            return cmd_deep_dive(db, args.id, args.json)
        if args.verb == "tailor":
            return cmd_tailor(db, args.id, apply_from=args.apply_from,
                              via_api=args.via == "api", as_json=args.json)
        return cmd_decide(db, args.verb, args.id, args.all, args.json)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
