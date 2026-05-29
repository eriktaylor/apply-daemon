"""Speculative Agent — asynchronous Reaction Loop for autopilot mode.

Polls the SQLite queue for listings flagged ``pipeline_status='auto_queued'``,
runs Deep Research and a Claude match-analysis pass for each, then posts the
enriched evaluation to Slack. NO verdicts post-research are auto-passed.

Only the assets needed at the Slack triage level are produced:
    - deep_research_context (saved to disk for tailor reuse)
    - post_research_verdict
    - post_research_confidence
    - match_analysis
    - updated_skills_match

Heavier resume edits and cover letters are deferred to the manual Tailor run
(✏️ reaction). Output is written to the same ``output/<company>_<title>_<id>/``
folder used by ``src.tailor`` so subsequent tailor runs reuse the artifacts.

Usage:
    python -m src.process_queue                  # process the queue
    python -m src.process_queue --backfill       # backfill, then process
    python -m src.process_queue --backfill-only  # backfill, exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv

load_dotenv()

from src.compile import _serialize_safe
from src.db import Database
from src.file_utils import read_dropzone_file
from src.geo import get_distance
from src.notifications import _get_slack_config, _import_slack_app
from src.profile_loader import load_profile
from src.research import run_deep_research
from src.tailor import _find_existing_output
from src.triage import get_confidence_threshold

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("output")

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Ranking knobs for selecting the top-N out of the auto_queue.
# Lexicographic primary: confidence band (5-point wide).
# Within-band composite: verdict bonus + skill score + geo bonus.
_BAND_WIDTH = 5
_VERDICT_BONUS_YES = 3
# Geo bucket scores: 0=Remote, 1=Local(<=30mi), 2=Commute(<=60mi), 3=Relocation(>60mi)
_GEO_SCORES: dict[int, int] = {0: 5, 1: 4, 2: 3, 3: 1}
# Mileage bucket boundaries (inclusive upper)
_LOCAL_MILES_MAX = 30
_COMMUTE_MILES_MAX = 60

_MATCH_ANALYSIS_SCHEMA = (
    '"match_analysis": "<A structured 3-part analysis:\\n'
    "**The Opportunity:** Why this role matters based on the research and "
    "job description.\\n"
    "**The Reality Check:** Explicitly address the original skills gap and "
    "location/commute concerns, and explain how the candidate's background "
    "overcomes them.\\n"
    '**The Verdict:** A final strategic assessment of fit.>"'
)

_AUTO_PROMPT = """\
You are an expert career coach evaluating a candidate's fit for a specific role.
You have access to live Deep Research about the company.

## Candidate Profile
{profile}

## Base Resume
{resume}

## Target Job
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Job Summary: {job_summary}
Initial Reasoning: {reason}

## Company Research (Deep Research)
{research_context}

## Instructions
Re-score this match using the research context. Respond with ONLY a valid JSON
object (no markdown fences, no extra text) with this exact schema:

{{
    {match_analysis_schema},
    "post_research_verdict": "<YES, NO, or MAYBE>",
    "post_research_confidence": "<integer 0-100>",
    "updated_skills_match": {{"matching": ["<skill1>", ...], "missing": ["<skill1>", ...]}}
}}

CRITICAL: If the Deep Research content does not match the Target Job company,
explicitly state 'Context Mismatch' in the match_analysis and lower confidence.
Do not invent facts beyond the candidate profile, resume, and research context.
"""


def _autopilot_enabled() -> bool:
    return os.getenv("AUTOPILOT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes",
    )


def _concurrency() -> int:
    try:
        return max(1, int(os.getenv("AUTOPILOT_CONCURRENCY", "3")))
    except ValueError:
        return 3


def _top_n() -> int:
    """Max listings autopilot will finalize per UTC day. 0 disables autopilot."""
    try:
        return max(0, int(os.getenv("AUTOPILOT_TOP_N", "10")))
    except ValueError:
        return 10


def _tailor_model() -> str:
    return os.getenv("OPENROUTER_TAILOR_MODEL", "anthropic/claude-sonnet-4.6")


def _band(confidence: int) -> int:
    """Map a 0-100 confidence to its 5-point band index (higher = better)."""
    return int(confidence) // _BAND_WIDTH


def _compute_distance_bucket(location: str) -> int:
    """Resolve a location string to a coarse geo bucket.

    Returns 0=Remote, 1=Local(<=30mi), 2=Commute(<=60mi), 3=Relocation(>60mi).
    Unknown or unparseable locations bucket as 3 (worst-case for ranking).
    """
    if not location:
        return 3
    distance_str = get_distance(location)
    if distance_str == "Remote":
        return 0
    if distance_str == "Distance unknown":
        return 3
    # Parse "N miles"
    try:
        miles = int(distance_str.split()[0])
    except (ValueError, IndexError):
        return 3
    if miles <= _LOCAL_MILES_MAX:
        return 1
    if miles <= _COMMUTE_MILES_MAX:
        return 2
    return 3


def _resolve_bucket(row: dict, db: Database) -> int:
    """Return the row's distance_bucket, computing + persisting it on first use."""
    cached = row.get("distance_bucket")
    if cached is not None:
        return int(cached)
    bucket = _compute_distance_bucket(row.get("location", "") or "")
    row["distance_bucket"] = bucket
    db.set_distance_bucket(row["id"], bucket)
    return bucket


def _skill_score(row: dict) -> int:
    """Net skill score: matching count - missing count. Tolerates malformed JSON."""
    def _count(field: str) -> int:
        raw = row.get(field) or ""
        if not raw:
            return 0
        try:
            value = json.loads(raw)
            return len(value) if isinstance(value, list) else 0
        except (json.JSONDecodeError, TypeError):
            return 0
    return _count("matching_skills") - _count("missing_skills")


def _composite_score(row: dict, bucket: int) -> int:
    """Within-band ordering signal. Higher = better."""
    verdict_bonus = _VERDICT_BONUS_YES if (row.get("verdict") or "").upper() == "YES" else 0
    return verdict_bonus + _skill_score(row) + _GEO_SCORES.get(bucket, 0)


def _select_top_n(rows: list[dict], top_n: int, db: Database) -> list[dict]:
    """Pick up to ``top_n`` rows using confidence bands + within-band composite.

    Walks bands high → low. For each band, lazily resolves geo for its rows,
    scores them, then slices the band by ``(composite DESC, date_ingested DESC)``.
    Bands beyond the quota are never geocoded.
    """
    if top_n <= 0 or not rows:
        return []

    # Group by band, preserving descending confidence (rows are already sorted
    # confidence DESC, date_ingested DESC by get_auto_queue).
    bands: dict[int, list[dict]] = {}
    band_order: list[int] = []
    for r in rows:
        b = _band(r.get("confidence", 0))
        if b not in bands:
            bands[b] = []
            band_order.append(b)
        bands[b].append(r)

    selected: list[dict] = []
    for band in band_order:
        if len(selected) >= top_n:
            break
        band_rows = bands[band]
        # Lazy geo resolution — only for rows in bands we actually consider.
        for r in band_rows:
            bucket = _resolve_bucket(r, db)
            r["_composite"] = _composite_score(r, bucket)
        # Sort within band: composite DESC, then date_ingested DESC. Two stable
        # passes (least significant first) — Python's sort is stable.
        band_rows.sort(key=lambda r: r.get("date_ingested", ""), reverse=True)
        band_rows.sort(key=lambda r: int(r["_composite"]), reverse=True)
        remaining = top_n - len(selected)
        selected.extend(band_rows[:remaining])

    return selected


def _job_output_dir(job_id: str, listing: dict) -> Path:
    """Return the canonical output folder for a job, creating it if needed."""
    existing = _find_existing_output(job_id)
    if existing:
        existing.mkdir(parents=True, exist_ok=True)
        return existing
    company = listing.get("company", "company")
    title = listing.get("title", "role")
    company_slug = re.sub(r"[^\w]+", "_", company).strip("_")
    title_slug = re.sub(r"[^\w]+", "_", title).strip("_")[:30]
    folder = OUTPUT_DIR / f"{company_slug}_{title_slug}_{job_id[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _load_or_run_research(company: str, job_desc: str, folder: Path) -> str:
    """Reuse cached research from disk if present; otherwise run it and persist."""
    cache = folder / "deep_research_context.txt"
    if cache.exists():
        text = cache.read_text(encoding="utf-8").strip()
        if text:
            logger.info("Autopilot: reusing cached research at %s", cache)
            return text
    text = run_deep_research(company, job_desc)
    if text:
        cache.write_text(text, encoding="utf-8")
    return text


def _parse_auto_response(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse autopilot response: {e}\nRaw: {text[:500]}")
    for key in ("match_analysis", "post_research_verdict", "post_research_confidence"):
        if key not in data:
            raise RuntimeError(f"Autopilot response missing required field: {key}")
    return data


def _merge_assets_json(folder: Path, auto_json: dict, research_context: str) -> None:
    """Persist autopilot results into assets.json so later tailor runs can reuse them.

    If a prior assets.json already exists (manual tailor ran first), we only
    fill missing keys — never overwrite tailor-authored content.
    """
    path = folder / "assets.json"
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    merged = dict(existing)
    for key, value in auto_json.items():
        merged.setdefault(key, value)
    if research_context:
        merged.setdefault(
            "company_research_dossier",
            research_context[:2000],
        )
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def _build_slack_blocks(listing: dict, auto_json: dict, folder: Path) -> tuple[list[dict], dict]:
    """Build the Slack card + the threaded Deep Evaluation blocks."""
    title = listing.get("title", "Unknown")
    company = listing.get("company", "Unknown")
    location = listing.get("location", "")
    salary = listing.get("salary", "")
    confidence = listing.get("confidence", 0)
    verdict = listing.get("verdict", "")
    job_summary = listing.get("job_summary", "")
    listing_id = listing.get("id", "")

    links_raw = listing.get("links", "")
    links: list[str] = []
    if links_raw:
        try:
            links = json.loads(links_raw) if isinstance(links_raw, str) else links_raw
        except (json.JSONDecodeError, TypeError):
            links = []

    header_text = f"*{title}* — {company}"
    if links:
        header_text = f"<{links[0]}|*{title}*> — {company}"
    if location and location != "not specified":
        distance = get_distance(location)
        if distance == "Remote":
            header_text += "\n:round_pushpin: Remote"
        elif distance != "Distance unknown":
            header_text += f"\n:round_pushpin: {location} ({distance} from home)"
        else:
            header_text += f"\n:round_pushpin: {location}"

    detail_parts = [
        f":robot_face: Auto-evaluated  |  {verdict} ({confidence}%)",
    ]
    if salary and salary != "not listed":
        detail_parts.append(f":moneybag: {salary}")

    card_blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": d} for d in detail_parts]},
    ]
    if job_summary:
        card_blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":memo: *TL;DR:* {job_summary[:800]}"},
        })
    card_blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"React: :thumbsup: Save  |  :thumbsdown: Pass  |  "
                f":pencil2: Tailor  •  `{listing_id}`"
            ),
        }],
    })

    pr_verdict = auto_json.get("post_research_verdict", "MAYBE")
    pr_conf = auto_json.get("post_research_confidence", "?")
    match_analysis = auto_json.get("match_analysis", "")
    skills = auto_json.get("updated_skills_match", {}) or {}
    matching = skills.get("matching", []) if isinstance(skills, dict) else []
    missing = skills.get("missing", []) if isinstance(skills, dict) else []

    if len(match_analysis) > 2500:
        match_analysis = match_analysis[:2500] + "\n_(truncated)_"

    verdict_emoji = ":large_green_circle:" if pr_verdict == "YES" else (
        ":yellow_circle:" if pr_verdict == "MAYBE" else ":red_circle:"
    )

    thread_blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":mag: *Deep Evaluation (Autopilot): {title}* — {company}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{verdict_emoji} *Post-Research Verdict:* "
                    f"{pr_verdict} ({pr_conf}% confidence)"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Match Analysis:*\n{match_analysis}"},
        },
    ]
    skills_parts = []
    if matching:
        skills_parts.append(f":white_check_mark: *Matching:* {', '.join(matching)}")
    if missing:
        skills_parts.append(f":warning: *Gaps:* {', '.join(missing)}")
    if skills_parts:
        thread_blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(skills_parts)},
        })
    thread_blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f":page_facing_up: Assets cached to `{folder}`",
        }],
    })

    metadata = {
        "event_type": "apply_daemon_listing",
        "event_payload": {"job_id": listing_id},
    }
    return card_blocks, {"thread_blocks": thread_blocks, "metadata": metadata}


def _post_results_to_slack(
    app,
    channel: str,
    listing: dict,
    auto_json: dict,
    folder: Path,
    existing_ts: str | None,
) -> tuple[bool, str | None]:
    """Post the auto-evaluated card + threaded Deep Evaluation.

    When ``existing_ts`` is set, edits the prior digest card in place (mirroring
    sweeper._handle_tailor) and posts the Deep Evaluation as a thread reply.
    Otherwise posts a fresh card. Returns ``(success, message_ts)``.
    """
    title = listing.get("title", "Unknown")
    company = listing.get("company", "Unknown")
    pr_verdict = auto_json.get("post_research_verdict", "MAYBE")
    pr_conf = auto_json.get("post_research_confidence", "?")
    card_blocks, thread_info = _build_slack_blocks(listing, auto_json, folder)
    attachment = {
        "color": (
            "#2eb67d" if pr_verdict == "YES"
            else "#36c5f0" if pr_verdict == "MAYBE"
            else "#ddd"
        ),
        "blocks": card_blocks,
    }
    card_text = (
        f":robot_face: Auto-evaluated: {title} at {company} — "
        f"{pr_verdict} ({pr_conf}%)"
    )
    thread_text = f"Deep Evaluation: {title} at {company} — {pr_verdict} ({pr_conf}%)"
    try:
        if existing_ts:
            app.client.chat_update(
                channel=channel,
                ts=existing_ts,
                text=card_text,
                attachments=[attachment],
            )
            ts = existing_ts
        else:
            resp = app.client.chat_postMessage(
                channel=channel,
                text=card_text,
                attachments=[attachment],
                metadata=thread_info["metadata"],
            )
            ts = resp.get("ts")
        if ts:
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=thread_text,
                blocks=thread_info["thread_blocks"],
            )
        return True, ts
    except Exception:
        logger.error(
            "Failed to post autopilot result for %s",
            listing.get("id", "")[:8], exc_info=True,
        )
        return False, None


def _replace_card_with_passed(app, channel: str, ts: str) -> None:
    """Overwrite an existing card with the gray 'Passed' block (mirrors sweeper)."""
    try:
        app.client.chat_update(
            channel=channel,
            ts=ts,
            text="Passed",
            blocks=[{
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":no_entry_sign: *Passed*"},
            }],
            attachments=[],
        )
    except Exception:
        logger.error("Autopilot: failed to gray-pass card at ts=%s", ts, exc_info=True)


def _build_prompt(
    listing: dict, research_context: str, profile_text: str, resume_text: str,
) -> str:
    return _AUTO_PROMPT.format(
        profile=profile_text,
        resume=resume_text,
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        location=listing.get("location", "not specified"),
        salary=listing.get("salary", "not listed"),
        job_summary=listing.get("job_summary", ""),
        reason=listing.get("reason", ""),
        research_context=research_context or "(No research context available)",
        match_analysis_schema=_MATCH_ANALYSIS_SCHEMA,
    )


async def _process_one(
    client: openai.AsyncOpenAI,
    listing: dict,
    profile_text: str,
    resume_text: str,
    slack_ctx: tuple[Any, str] | None,
    semaphore: asyncio.Semaphore,
) -> str:
    """Process a single auto_queued listing end-to-end. Returns final status."""
    job_id = listing["id"]
    company = listing.get("company", "")
    title = listing.get("title", "")
    short = job_id[:8]

    async with semaphore:
        folder = _job_output_dir(job_id, listing)

        # Persist the pre-research Stage 5 snapshot before any LLM work runs.
        # Idempotent — never clobbers a prior tailor's snapshot.
        triage_path = folder / "original_triage.json"
        if not triage_path.exists():
            try:
                triage_path.write_text(_serialize_safe(listing), encoding="utf-8")
            except OSError:
                logger.warning("Autopilot: could not write original_triage for %s",
                               short, exc_info=True)

        # 1) Re-check state at the moment of work — another runner may have claimed it
        with Database() as db:
            row = db.get_listing_by_id(job_id)
            if not row or row["pipeline_status"] != "auto_queued":
                logger.info("Autopilot: %s no longer queued, skipping", short)
                return "skipped"
            existing_ts = db.get_slack_message_ts(job_id)

        # 2) Deep research (cached if present)
        job_desc = listing.get("job_summary", "") or listing.get("reason", "")
        try:
            research_context = await asyncio.to_thread(
                _load_or_run_research, company, job_desc, folder,
            )
        except Exception:
            logger.error("Autopilot research failed for %s", short, exc_info=True)
            with Database() as db:
                db.update_pipeline_status(job_id, "failed_api")
            return "failed"

        # 3) LLM match analysis (reuse if already saved this run)
        auto_path = folder / "auto_assets.json"
        if auto_path.exists():
            try:
                auto_json = json.loads(auto_path.read_text(encoding="utf-8"))
                logger.info("Autopilot: reusing cached auto_assets for %s", short)
            except (json.JSONDecodeError, OSError):
                auto_json = None
        else:
            auto_json = None

        if auto_json is None:
            prompt = _build_prompt(listing, research_context, profile_text, resume_text)
            try:
                resp = await client.chat.completions.create(
                    model=_tailor_model(),
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2048,
                    response_format={"type": "json_object"},
                )
                raw = resp.choices[0].message.content or ""
                auto_json = _parse_auto_response(raw)
            except Exception:
                logger.error("Autopilot LLM call failed for %s", short, exc_info=True)
                with Database() as db:
                    db.update_pipeline_status(job_id, "failed_api")
                return "failed"
            auto_path.write_text(json.dumps(auto_json, indent=2), encoding="utf-8")
            _merge_assets_json(folder, auto_json, research_context)

        # 4) Auto-pass on NO verdict — gray out the existing card in place.
        pr_verdict = (auto_json.get("post_research_verdict") or "").upper()
        if pr_verdict == "NO":
            if slack_ctx is not None and existing_ts:
                app, channel = slack_ctx
                await asyncio.to_thread(
                    _replace_card_with_passed, app, channel, existing_ts,
                )
            with Database() as db:
                db.update_pipeline_status(job_id, "passed")
                db.mark_slack_notified(job_id)
                db.mark_autopilot_processed(job_id)
            logger.info(
                "Autopilot: auto-passed %s ('%s' at '%s') on post-research NO",
                short, title, company,
            )
            return "passed"

        # 5) Edit the existing card (or post fresh if digest never ran) + threaded
        # reply. Idempotent re-runs find pipeline_status != 'auto_queued' on the
        # second pass and skip.
        new_ts: str | None = None
        if slack_ctx is not None:
            app, channel = slack_ctx
            posted, new_ts = await asyncio.to_thread(
                _post_results_to_slack,
                app, channel, listing, auto_json, folder, existing_ts,
            )
            if not posted:
                logger.warning("Autopilot: leaving %s in auto_queued for retry", short)
                return "retry"

        with Database() as db:
            if new_ts and not existing_ts:
                db.set_slack_message_ts(job_id, new_ts)
            db.update_pipeline_status(job_id, "auto")
            db.mark_slack_notified(job_id)
            db.mark_autopilot_processed(job_id)
        logger.info(
            "Autopilot: %s ('%s' at '%s') → auto (post-research %s)",
            short, title, company, pr_verdict or "MAYBE",
        )
        return "auto"


async def _run_async(rows: list[dict]) -> dict[str, int]:
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set. Add it to your .env file.")
    profile = load_profile()
    profile_text = profile["llm_context"]
    resume_text = read_dropzone_file("base_resume") or ""

    token, channel = _get_slack_config()
    slack_ctx: tuple[Any, str] | None = None
    if token and channel:
        try:
            slack_ctx = (_import_slack_app(token), channel)
        except ImportError:
            logger.warning("slack-bolt not installed; autopilot will skip Slack posts")
            slack_ctx = None
    else:
        logger.warning("Slack not configured; autopilot will skip Slack posts")

    client = openai.AsyncOpenAI(base_url=_OPENROUTER_BASE_URL, api_key=api_key)
    semaphore = asyncio.Semaphore(_concurrency())
    tasks = [
        _process_one(client, row, profile_text, resume_text, slack_ctx, semaphore)
        for row in rows
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    counts = {"auto": 0, "passed": 0, "failed": 0, "skipped": 0, "retry": 0, "error": 0}
    for r in results:
        if isinstance(r, Exception):
            logger.error("Autopilot task crashed", exc_info=r)
            counts["error"] += 1
        else:
            counts[r] = counts.get(r, 0) + 1
    return counts


def backfill(min_confidence: float | None = None) -> int:
    """Promote existing triaged/saved YES/MAYBE listings into the autopilot queue.

    Use this when enabling autopilot after listings have already been ingested
    through the normal pipeline. Honors lane priority — rows in passed,
    tailored, auto, applied, etc. are left untouched.

    Args:
        min_confidence: Confidence cutoff as a 0.0–1.0 fraction. Defaults to
            ``CONFIDENCE_THRESHOLD`` from the environment.

    Returns:
        Number of listings moved to ``auto_queued``.
    """
    threshold = min_confidence if min_confidence is not None else get_confidence_threshold()
    cutoff_pct = int(round(threshold * 100))
    with Database() as db:
        promoted = db.backfill_auto_queue(cutoff_pct)
    logger.info(
        "Autopilot backfill: promoted %d triaged/saved listings to auto_queued "
        "(confidence >= %d%%)",
        promoted, cutoff_pct,
    )
    return promoted


def run() -> int:
    """Process the autopilot queue under the daily top-N cap.

    Returns the number of listings dispatched.
    """
    if not _autopilot_enabled():
        logger.info("Autopilot disabled (AUTOPILOT_ENABLED=false); nothing to do")
        return 0

    top_n = _top_n()
    if top_n <= 0:
        logger.info("Autopilot: AUTOPILOT_TOP_N=%d; nothing to do", top_n)
        return 0

    cutoff_pct = int(round(get_confidence_threshold() * 100))
    with Database() as db:
        processed_today = db.count_autopilot_processed_today()
        remaining = max(0, top_n - processed_today)
        if remaining == 0:
            logger.info(
                "Autopilot daily cap (%d) reached (%d processed today); skipping run",
                top_n, processed_today,
            )
            return 0
        # Fetch the full eligible pool (no SQL LIMIT) so Python-side banding +
        # lazy-geo selection can pick the most worth-enriching listings. The
        # geo lookup is gated to bands that actually contribute to the quota.
        eligible = [
            dict(r) for r in db.get_auto_queue(
                top_n=None, min_confidence_pct=cutoff_pct,
            )
        ]
        rows = _select_top_n(eligible, remaining, db)

    if not rows:
        logger.info(
            "Autopilot queue empty after scoring (eligible=%d, cutoff=%d%%, remaining=%d)",
            len(eligible), cutoff_pct, remaining,
        )
        return 0

    logger.info(
        "Autopilot: processing %d/%d eligible listings (concurrency=%d, "
        "remaining today=%d/%d, cutoff=%d%%, top band=%d)",
        len(rows), len(eligible), _concurrency(),
        remaining, top_n, cutoff_pct, _band(rows[0].get("confidence", 0)),
    )
    counts = asyncio.run(_run_async(rows))
    logger.info(
        "Autopilot run complete: auto=%d passed=%d failed=%d skipped=%d retry=%d error=%d",
        counts.get("auto", 0), counts.get("passed", 0), counts.get("failed", 0),
        counts.get("skipped", 0), counts.get("retry", 0), counts.get("error", 0),
    )
    return len(rows)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    parser = argparse.ArgumentParser(description="Autopilot Speculative Agent")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "Promote existing triaged/saved listings (verdict YES/MAYBE with "
            "confidence >= CONFIDENCE_THRESHOLD) into the autopilot queue "
            "before processing. Useful when enabling autopilot after a batch "
            "has already been ingested."
        ),
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help="Run the backfill step and exit without processing the queue.",
    )
    args = parser.parse_args()

    if args.backfill or args.backfill_only:
        backfill()
        if args.backfill_only:
            return
    run()


if __name__ == "__main__":
    main()
