"""Reaction-based sweeper — polls Slack for emoji reactions and mutates state.

Designed to run on a fast cron schedule (every 2 minutes). Scans the configured
Slack channel for user reactions on digest messages:

    :thumbsdown: (-1)     → pipeline_status = 'passed',  UI replaced with gray "Passed"
    :thumbsup:   (+1)     → pipeline_status = 'saved',   bot adds checkmark receipt
    :pencil2:    (pencil) → triggers tailor, status = 'tailored', UI updated with assets

Slack message metadata tag: digest cards are written with
``event_type='apply_daemon_listing'``. Cards posted before the
apply-pilot → apply-daemon rename carry the legacy
``event_type='apply_pilot_listing'``; both tags are accepted on read
via ``_LISTING_EVENT_TYPES``. The legacy entry can be retired once the
oldest still-actionable card has aged out.

Usage:
    python -m src.sweeper

Crontab:
    */2 * * * * cd /path/to/apply-daemon && .venv/bin/python -m src.sweeper
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk.errors import SlackApiError

load_dotenv()

from src.db import Database
from src.notifications import _get_slack_config, _import_slack_app

logger = logging.getLogger(__name__)

_LABELS_DIR = Path("data")
_LABELS_PATH = _LABELS_DIR / "human_labels.jsonl"

# Slack message metadata event_type values recognized as digest listings.
# New cards are written with "apply_daemon_listing"; the legacy
# "apply_pilot_listing" tag is still accepted on read so existing cards
# remain actionable until they age out.
_LISTING_EVENT_TYPES = frozenset({"apply_daemon_listing", "apply_pilot_listing"})


def _append_human_label(job_id: str, action: str, listing: dict) -> None:
    """Append a human feedback record to data/human_labels.jsonl."""
    _LABELS_DIR.mkdir(parents=True, exist_ok=True)

    def _default(o: object) -> str:
        if isinstance(o, (datetime,)):
            return o.isoformat()
        # sqlite3.Row or date-like objects
        if hasattr(o, "isoformat"):
            return o.isoformat()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    record = {
        "job_id": job_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "human_reaction": action,
        "listing": dict(listing),
    }
    with open(_LABELS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=_default) + "\n")
    logger.debug("Appended human label: %s → %s", job_id[:8], action)


# Reaction name mappings — Slack normalizes these
_PASS_REACTIONS = {"-1", "thumbsdown"}
_SAVE_REACTIONS = {"+1", "thumbsup"}
_TAILOR_REACTIONS = {"pencil2", "pencil"}
_QUESTIONS_REACTIONS = {"question", "grey_question"}

# Bot's own receipts — skip these when scanning
_BOT_RECEIPT_REACTIONS = {"white_check_mark", "eyes", "arrows_counterclockwise"}

# Unique substring in the bot's scrape-failure reply for !triage commands.
# _scan_triage_fallback_commands uses this to identify threads awaiting !update.
_TRIAGE_FALLBACK_MARKER = "Reply to this thread with `!update"


def _post_triage_status(verdict: str) -> str:
    """Pipeline status to apply after a manual / !update re-triage.

    A ``NO`` verdict is auto-dismissed as ``passed`` so the Slack card lands
    in the rejected lane immediately — a NO is a NO regardless of how it
    reached Slack. Anything else returns to active triage.
    """
    return "passed" if (verdict or "").upper() == "NO" else "triaged"

# ChatOps thread commands — state transitions
_CHATOPS_STATE_COMMANDS = {
    "!applied": "applied",
    "!pass": "passed",
    "!expire": "expired",
    "!expired": "expired",   # alias — same behavior as !expire
    "!interview": "interviewing",
    "!rejected": "rejected",
}

# ChatOps thread commands — on-demand asset generation
_CHATOPS_ASSET_COMMANDS = {"!coverletter", "!prep", "!polish"}

# All recognized ChatOps commands (state + asset + answer + update + triage + trend + regenerate)
_ALL_CHATOPS_PREFIXES = (
    set(_CHATOPS_STATE_COMMANDS) | _CHATOPS_ASSET_COMMANDS
    | {"!answer", "!update", "!triage", "!trend", "!regenerate"}
)

# Status badges for Slack message updates
_STATUS_BADGES = {
    "applied": ":large_green_circle: Status: APPLIED",
    "passed": ":no_entry_sign: Status: PASSED",
    "expired": ":no_entry_sign: Status: EXPIRED",
    "interviewing": ":star2: Status: INTERVIEWING",
    "rejected": ":red_circle: Status: REJECTED",
}

# Jobs in these states are eligible for ChatOps thread commands
_CHATOPS_ELIGIBLE_STATUSES = {"tailored", "saved", "applied"}


def _extract_job_id(message: dict) -> str | None:
    """Extract job_id from Slack message metadata.

    The digest embeds metadata with event_type='apply_daemon_listing'
    (legacy: 'apply_pilot_listing') and event_payload={'job_id': '<listing_id>'}.
    """
    metadata = message.get("metadata")
    if not metadata:
        return None
    if metadata.get("event_type") not in _LISTING_EVENT_TYPES:
        return None
    return metadata.get("event_payload", {}).get("job_id")


def _get_user_reactions(message: dict) -> list[tuple[str, str]]:
    """Extract (reaction_name, first_reacting_user) pairs from a message.

    Filters out bot receipt reactions so we don't re-process our own emoji.
    """
    reactions = message.get("reactions", [])
    result = []
    for r in reactions:
        name = r.get("name", "")
        if name in _BOT_RECEIPT_REACTIONS:
            continue
        users = r.get("users", [])
        if users:
            result.append((name, users[0]))
    return result


def _classify_reaction(name: str) -> str | None:
    """Map a reaction name to an action: 'pass', 'save', 'tailor', or 'questions'."""
    if name in _PASS_REACTIONS:
        return "pass"
    if name in _SAVE_REACTIONS:
        return "save"
    if name in _TAILOR_REACTIONS:
        return "tailor"
    if name in _QUESTIONS_REACTIONS:
        return "questions"
    return None


def _check_tailor_checkpoint(folder: "Path") -> str:
    """Classify tailor checkpoint state for an existing output folder.

    Called when pipeline_status == "tailored" but we want to verify disk
    state before skipping. Returns one of three values:

      "complete"             — folder has both sentinel files; no action needed.
      "error_deep_research"  — deep_research_context.txt is missing; surface
                               an error to the user (don't loop — research
                               failure is unexpected per the healing pipeline).
      "resume_from_research" — research file present but assets.json absent;
                               safe to resume from the checkpoint using the
                               cached research context.
    """
    research_file = folder / "deep_research_context.txt"
    if not research_file.exists():
        return "error_deep_research"
    assets_file = folder / "assets.json"
    if not assets_file.exists():
        return "resume_from_research"
    return "complete"


def _auto_pass_no_verdict_cards(
    app, db: Database, channel: str, messages: list, counts: dict,
) -> None:
    """Convert any surfaced NO-verdict card to "Passed".

    Scans the fetched Slack messages for digest cards (job_id metadata
    present), looks up the DB row, and if the verdict is NO and the
    pipeline_status is still in an active lane, flips the row to "passed"
    and rewrites the Slack card via ``_handle_pass``.

    Idempotency: ``_handle_pass`` strips the listing metadata when it
    rewrites the card, so subsequent sweeps return ``job_id=None`` for the
    same message and skip it naturally. Cards already at status='passed'
    are also short-circuited as a belt-and-braces guard.
    """
    for msg in messages:
        job_id = _extract_job_id(msg)
        if not job_id:
            continue

        row = db.get_listing_by_id(job_id)
        if not row:
            continue

        verdict = (row["verdict"] or "").upper()
        if verdict != "NO":
            continue

        if (row["pipeline_status"] or "") == "passed":
            continue

        ts = msg.get("ts", "")
        _handle_pass(app, db, channel, ts, job_id, msg)
        counts["passed"] += 1


def _dispatch_reactions(
    app, db: Database, channel: str, messages: list, counts: dict,
) -> None:
    """Priority-first reaction dispatch for a batch of Slack messages.

    Exclusive-action priority order: pass > tailor > save.
    Only the highest-priority reaction present on each card fires; lower-priority
    co-reactions are no-ops (prevents backwards state clobbering across sweeps).
    The ❓ Smart Router reaction is orthogonal and dispatched independently,
    except when pass wins (which removes the listing from active processing).
    """
    for msg in messages:
        job_id = _extract_job_id(msg)
        if not job_id:
            continue

        user_reactions = _get_user_reactions(msg)
        if not user_reactions:
            continue

        ts = msg.get("ts", "")

        # Map each distinct action to the first emoji name seen for it.
        # tailor/smart_router handlers need the emoji name to remove the reaction.
        action_to_reaction: dict[str, str] = {}
        for reaction_name, _user in user_reactions:
            action = _classify_reaction(reaction_name)
            if action and action not in action_to_reaction:
                action_to_reaction[action] = reaction_name

        if not action_to_reaction:
            continue

        row = db.get_listing_by_id(job_id)
        if not row:
            logger.warning("Listing %s not found in DB, skipping", job_id[:8])
            continue

        current_status = row["pipeline_status"]

        _PRIORITY = ["pass", "tailor", "save"]
        highest = next((a for a in _PRIORITY if a in action_to_reaction), None)

        if highest == "pass":
            if current_status == "passed":
                counts["skipped"] += 1
            else:
                _append_human_label(job_id, "pass", row)
                _handle_pass(app, db, channel, ts, job_id, msg)
                counts["passed"] += 1

        elif highest == "tailor":
            if current_status in ("triaged", "saved"):
                # Standard fresh tailor
                _append_human_label(job_id, "tailor", row)
                _handle_tailor(
                    app, db, channel, ts, job_id, msg,
                    action_to_reaction["tailor"],
                )
                counts["tailored"] += 1

            elif current_status == "tailored":
                # DB says tailored — cross-check disk before skipping.
                from src.tailor import _find_existing_output
                folder = _find_existing_output(job_id)

                if folder is None:
                    # Checkpoint 1: folder gone (user deleted it, or status stale)
                    logger.info(
                        "Tailor checkpoint 1: output folder missing for %s — regenerating",
                        job_id[:8],
                    )
                    _append_human_label(job_id, "tailor", row)
                    _handle_tailor(
                        app, db, channel, ts, job_id, msg,
                        action_to_reaction["tailor"],
                    )
                    counts["tailored"] += 1

                else:
                    chk = _check_tailor_checkpoint(folder)

                    if chk == "complete":
                        counts["skipped"] += 1

                    elif chk == "error_deep_research":
                        # Checkpoint 2: research file missing — surface error, don't loop
                        logger.warning(
                            "Tailor checkpoint 2: deep_research_context.txt missing for %s",
                            job_id[:8],
                        )
                        _post_thread_reply(
                            app, channel, ts,
                            ":warning: Deep research did not complete for this role. "
                            "Reply `!regenerate` to retry from scratch.",
                        )
                        counts["skipped"] += 1

                    elif chk == "resume_from_research":
                        # Checkpoint 3: research present, assets.json absent — resume
                        logger.info(
                            "Tailor checkpoint 3: resuming from research for %s",
                            job_id[:8],
                        )
                        cached_research = (
                            folder / "deep_research_context.txt"
                        ).read_text(encoding="utf-8")
                        _append_human_label(job_id, "tailor", row)
                        _handle_tailor(
                            app, db, channel, ts, job_id, msg,
                            action_to_reaction["tailor"],
                            research_context_cache=cached_research,
                        )
                        counts["tailored"] += 1

            else:
                counts["skipped"] += 1

        elif highest == "save":
            if current_status == "triaged":
                _append_human_label(job_id, "save", row)
                _handle_save(app, db, channel, ts, job_id)
                counts["saved"] += 1
            else:
                counts["skipped"] += 1

        if "questions" in action_to_reaction and highest != "pass":
            _append_human_label(job_id, "questions", row)
            result = _handle_smart_router(
                app, db, channel, ts, job_id, msg,
                action_to_reaction["questions"],
            )
            if result == "skipped":
                counts["skipped"] += 1
            elif result == "tailor":
                counts["tailored"] += 1
            else:
                counts["questions"] += 1


def sweep(limit: int = 50) -> dict:
    """Scan the Slack channel for reactions and process them.

    Args:
        limit: Total number of messages to scan. When > 200 (Slack's per-page
               max), cursor pagination fetches additional pages automatically.
               Pass a higher value via ``--deep N`` on the CLI.

    Returns a summary dict with counts of each action taken.
    """
    token, channel = _get_slack_config()
    if not token or not channel:
        logger.warning("Slack not configured, cannot sweep")
        return {
            "passed": 0, "saved": 0, "tailored": 0, "questions": 0,
            "chatops": 0, "triage": 0, "trend": 0, "skipped": 0, "regenerate": 0,
        }

    try:
        app = _import_slack_app(token)
    except ImportError:
        logger.warning("slack-bolt not installed. Run: pip install slack-bolt")
        return {
            "passed": 0, "saved": 0, "tailored": 0, "questions": 0,
            "chatops": 0, "triage": 0, "trend": 0, "skipped": 0, "regenerate": 0,
        }

    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=3)
    app.client.retry_handlers.append(rate_limit_handler)

    counts = {
        "passed": 0, "saved": 0, "tailored": 0, "questions": 0,
        "chatops": 0, "triage": 0, "trend": 0, "skipped": 0, "regenerate": 0,
    }

    # Fetch messages with cursor pagination so --deep N can reach beyond the
    # default 50. Slack's per-page cap is 200; we loop until we have `limit`
    # messages or the channel is exhausted.
    _PAGE_MAX = 200  # Slack hard cap per conversations.history call
    messages: list[dict] = []
    cursor: str | None = None
    remaining = limit

    while remaining > 0:
        page_size = min(remaining, _PAGE_MAX)
        try:
            kwargs: dict = {
                "channel": channel,
                "limit": page_size,
                "include_all_metadata": True,
            }
            if cursor:
                kwargs["cursor"] = cursor
            result = app.client.conversations_history(**kwargs)
        except Exception:
            logger.error("Failed to fetch channel history", exc_info=True)
            if not messages:
                return counts
            break

        page = result.get("messages", [])
        messages.extend(page)
        remaining -= len(page)

        cursor = (result.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor or not page:
            break  # exhausted channel history

    logger.info("Sweeper scanning %d messages (limit=%d)", len(messages), limit)

    with Database() as db:
        # Auto-pass any card whose DB row carries verdict='NO'. These are
        # stragglers from previous runs that surfaced before the Stage 5 gate
        # was in place — convert them to "Passed" so they leave the active lane.
        _auto_pass_no_verdict_cards(app, db, channel, messages, counts)

        _dispatch_reactions(app, db, channel, messages, counts)

        # --- ChatOps pass: scan threads for !commands ---
        chatops_count = _scan_chatops_commands(app, db, channel, messages)
        counts["chatops"] = chatops_count

        # --- Manual triage pass: scan for !triage messages ---
        triage_count = _scan_triage_commands(app, db, channel, messages)
        # --- !triage scrape-failure fallback: !update replies in warning threads ---
        triage_count += _scan_triage_fallback_commands(app, db, channel, messages)
        counts["triage"] = triage_count

        # --- Trend analysis: scan for !trend messages ---
        trend_count = _scan_trend_commands(app, db, channel, messages)
        counts["trend"] = trend_count

    logger.info(
        "Sweep complete: %d passed, %d saved, %d tailored, %d questions, "
        "%d chatops, %d triage, %d trend, %d skipped, %d regenerate",
        counts["passed"], counts["saved"], counts["tailored"],
        counts["questions"], counts["chatops"], counts["triage"],
        counts["trend"], counts["skipped"], counts["regenerate"],
    )
    return counts


def _scan_chatops_commands(
    app, db: Database, channel: str, messages: list[dict],
) -> int:
    """Scan thread replies for ChatOps commands.

    Handles four categories:
      - !update <text>  — merge new text with existing description, re-score (Stage 5)
      - State commands: !applied, !pass, !interview, !rejected
      - Asset commands: !coverletter, !prep
      - Answer command: !answer <questions text>

    The status gate for state/asset/answer commands is checked per-reply rather than
    per-message so that !update works for jobs in any pipeline status.

    Idempotency: after processing, the bot adds a white_check_mark reaction to the
    reply. Subsequent sweeps skip replies that already carry that reaction.

    Returns the number of commands processed.
    """
    processed = 0

    for msg in messages:
        job_id = _extract_job_id(msg)
        if not job_id:
            continue

        row = db.get_listing_by_id(job_id)
        if not row:
            continue
        current_status = row["pipeline_status"]

        ts = msg.get("ts", "")
        if not ts:
            continue

        # Fetch thread replies — skip if no thread exists
        try:
            result = app.client.conversations_replies(
                channel=channel, ts=ts, limit=20,
            )
        except Exception:
            logger.debug("Failed to fetch thread for ChatOps scan on %s", ts, exc_info=True)
            continue

        replies = result.get("messages", [])
        for reply in replies:
            # Skip parent message and bot messages
            if reply.get("ts") == ts:
                continue
            if reply.get("bot_id") or reply.get("subtype"):
                continue

            raw_text = (reply.get("text") or "").strip()
            text_lower = raw_text.lower()
            reply_ts = reply.get("ts")

            # --- !update <text> (any status) ---
            # Merges the pasted text with the existing job description, then
            # re-runs Stage 5 scoring so the LLM sees full combined context.
            if text_lower.startswith("!update"):
                if _reply_is_processed(reply):
                    continue
                payload = raw_text[len("!update"):].strip()
                if not payload:
                    continue
                if _is_url(payload):
                    _post_thread_reply(
                        app, channel, ts,
                        ":warning: `!update` expects pasted job description text, not a URL. "
                        "To triage a new URL, post `!triage <url>` in the main channel.",
                    )
                    continue
                _handle_update(app, db, channel, ts, reply_ts, job_id, row, payload, msg=msg)
                _mark_reply_done(app, channel, reply_ts)
                processed += 1
                break

            # --- !regenerate (any non-passed status, requires ✏️ on parent card) ---
            # Force-deletes the output folder and triggers a full fresh tailor.
            # Uses :arrows_counterclockwise: as the idempotency receipt.
            if text_lower == "!regenerate":
                if _reply_is_processed(reply):
                    continue

                # Verify pencil emoji is on the parent card
                user_reactions = _get_user_reactions(msg)
                card_action_map: dict[str, str] = {}
                for rname, _user in user_reactions:
                    action = _classify_reaction(rname)
                    if action and action not in card_action_map:
                        card_action_map[action] = rname

                if "tailor" not in card_action_map:
                    _post_thread_reply(
                        app, channel, ts,
                        ":warning: `!regenerate` requires the ✏️ (pencil) emoji on this card. "
                        "Add the pencil reaction first, then reply `!regenerate`.",
                    )
                    _mark_regenerate_done(app, channel, reply_ts)
                    processed += 1
                    break

                if current_status == "passed":
                    _post_thread_reply(
                        app, channel, ts,
                        ":warning: `!regenerate` cannot run on a passed listing. "
                        "Use `!triage <url>` to revive it first.",
                    )
                    _mark_regenerate_done(app, channel, reply_ts)
                    processed += 1
                    break

                # Remove existing output folder
                import shutil

                from src.tailor import _find_existing_output
                folder = _find_existing_output(job_id)
                if folder and folder.exists():
                    shutil.rmtree(folder)
                    logger.info(
                        "!regenerate: removed output folder %s for %s",
                        folder.name, job_id[:8],
                    )

                # Reset to triaged so the standard tailor gate fires
                db.update_pipeline_status(job_id, "triaged")
                row = db.get_listing_by_id(job_id)  # refresh row

                # Mark reply processed before blocking call (idempotency even on exception)
                _mark_regenerate_done(app, channel, reply_ts)

                # Fire tailor immediately in this same sweep pass
                _append_human_label(job_id, "tailor", row)
                _handle_tailor(
                    app, db, channel, ts, job_id, msg,
                    card_action_map["tailor"],
                )
                processed += 1
                break

            # --- !expire (works from any non-terminal status, no eligibility gate) ---
            # Marks a listing as expired — same dedup/cooldown behavior as !pass
            # but semantically distinct: the role was removed/filled, not rejected.
            if text_lower in ("!expire", "!expired"):
                if _reply_is_processed(reply):
                    continue
                if current_status not in ("expired", "applied", "interviewing"):
                    db.update_pipeline_status(job_id, "expired")
                    _append_human_label(job_id, "expire", row)
                    logger.info("ChatOps: !expire → expired for %s", job_id[:8])
                    _apply_status_badge(app, channel, ts, msg, "expired")
                _mark_reply_done(app, channel, reply_ts)
                processed += 1
                break

            # Commands below require eligible statuses
            if current_status not in _CHATOPS_ELIGIBLE_STATUSES:
                continue

            # --- !applied (DB-only, idempotent, quiet confirmation) ---
            if text_lower == "!applied":
                if _reply_is_processed(reply):
                    continue
                if current_status != "applied":
                    db.update_pipeline_status(job_id, "applied")
                    _append_human_label(job_id, "applied", row)
                    logger.info("ChatOps: !applied → applied for %s", job_id[:8])
                _post_thread_reply(
                    app, channel, ts,
                    "[ System ] Pipeline status updated to: APPLIED",
                )
                _mark_reply_done(app, channel, reply_ts)
                processed += 1
                break

            # --- Other state commands (!pass, !interview, !rejected) ---
            if text_lower in _CHATOPS_STATE_COMMANDS:
                new_status = _CHATOPS_STATE_COMMANDS[text_lower]
                if current_status == new_status:
                    continue
                db.update_pipeline_status(job_id, new_status)
                _append_human_label(job_id, text_lower.lstrip("!"), row)
                logger.info("ChatOps: %s → %s for %s", text_lower, new_status, job_id[:8])
                _apply_status_badge(app, channel, ts, msg, new_status)
                processed += 1
                break

            # --- On-demand asset commands ---
            if text_lower == "!coverletter":
                if _reply_is_processed(reply):
                    continue
                _handle_ondemand_asset(app, channel, ts, job_id, "coverletter")
                _append_human_label(job_id, "coverletter", row)
                _mark_reply_done(app, channel, reply_ts)
                processed += 1
                break

            if text_lower == "!prep":
                if _reply_is_processed(reply):
                    continue
                _handle_ondemand_asset(app, channel, ts, job_id, "prep")
                _append_human_label(job_id, "prep", row)
                _mark_reply_done(app, channel, reply_ts)
                processed += 1
                break

            if text_lower == "!polish":
                if _reply_is_processed(reply):
                    continue
                # Require completed tailor assets; surface helpful error if missing
                if current_status != "tailored":
                    _post_thread_reply(
                        app, channel, ts,
                        ":warning: `!polish` requires a completed tailor pass. "
                        "Add the ✏️ (pencil) emoji to this card first, then reply `!polish` "
                        "after tailoring completes.",
                    )
                    _mark_reply_done(app, channel, reply_ts)
                    processed += 1
                    break
                _handle_ondemand_asset(app, channel, ts, job_id, "polish")
                _append_human_label(job_id, "polish", row)
                _mark_reply_done(app, channel, reply_ts)
                processed += 1
                break

            # --- !answer <text> (idempotency: white_check_mark reaction after processing) ---
            if text_lower.startswith("!answer"):
                if _reply_is_processed(reply):
                    continue
                questions = raw_text[len("!answer"):].strip()
                if not questions:
                    continue
                _handle_answers_fast_from_chatops(
                    app, db, channel, ts, job_id, msg, questions,
                )
                _mark_reply_done(app, channel, reply_ts)
                _append_human_label(job_id, "answer", row)
                processed += 1
                break

    return processed


def _handle_ondemand_asset(
    app, channel: str, ts: str, job_id: str, asset_type: str,
) -> None:
    """Generate a single asset on demand and post confirmation in thread."""
    # Signal processing
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="eyes")
    except SlackApiError as e:
        if e.response["error"] == "already_reacted":
            pass
        else:
            raise

    try:
        if asset_type == "coverletter":
            from src.tailor import generate_cover_letter_only
            output_dir, _ = generate_cover_letter_only(job_id)
            confirm_text = ":white_check_mark: Generated on-demand cover letter"
        elif asset_type == "prep":
            from src.tailor import generate_interview_prep_only
            output_dir, _ = generate_interview_prep_only(job_id)
            confirm_text = ":white_check_mark: Generated on-demand interview prep"
        elif asset_type == "polish":
            from src.tailor import generate_polish_resume
            output_dir, _ = generate_polish_resume(job_id)
            confirm_text = ":white_check_mark: Generated polished resume"
        else:
            return

        app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=confirm_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": confirm_text},
                },
                {
                    "type": "context",
                    "elements": [{
                        "type": "mrkdwn",
                        "text": f":page_facing_up: Saved to `{output_dir}`",
                    }],
                },
            ],
        )
        logger.info("On-demand %s generated for %s → %s", asset_type, job_id[:8], output_dir)
    except Exception:
        logger.error("On-demand %s failed for %s", asset_type, job_id[:8], exc_info=True)

    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
    except Exception:
        pass


def _handle_answers_fast_from_chatops(
    app, db: Database, channel: str, ts: str, job_id: str, msg: dict,
    custom_questions: str,
) -> None:
    """Handle !answer <text> from a ChatOps thread reply.

    Routes to the answers-only fast path (same as Route B in Smart Router).
    """
    _handle_answers_fast(app, db, channel, ts, job_id, msg, "question", custom_questions)


def _reply_is_processed(reply: dict) -> bool:
    """Return True if the bot has already added a receipt reaction to this reply.

    Checks for both the standard white_check_mark (all ChatOps commands) and
    arrows_counterclockwise (!regenerate). Either marker means the reply was
    already processed and should be skipped on subsequent sweeps.
    """
    existing = {r.get("name") for r in reply.get("reactions", [])}
    return bool(existing & {"white_check_mark", "arrows_counterclockwise"})


def _mark_regenerate_done(app, channel: str, reply_ts: str | None) -> None:
    """Apply :arrows_counterclockwise: receipt to a processed !regenerate reply.

    Distinct from the standard white_check_mark so the user can see at a glance
    that a full regeneration (not just a routine command) was processed.
    """
    if not reply_ts:
        return
    try:
        app.client.reactions_add(
            channel=channel, timestamp=reply_ts, name="arrows_counterclockwise",
        )
    except Exception:
        logger.debug("Could not add regenerate receipt to %s", reply_ts, exc_info=True)


def _mark_reply_done(app, channel: str, reply_ts: str | None) -> None:
    """Add a white_check_mark reaction to a thread reply to mark it as processed.

    This is the idempotency marker for !answer and thread-mode !triage. On future
    sweeps, _reply_is_processed() will detect this reaction and skip the reply.
    """
    if not reply_ts:
        return
    try:
        app.client.reactions_add(channel=channel, timestamp=reply_ts, name="white_check_mark")
    except SlackApiError as e:
        if e.response["error"] != "already_reacted":
            logger.debug("Failed to mark reply done (reactions_add)", exc_info=True)
    except Exception:
        logger.debug("Failed to mark reply done", exc_info=True)


def _handle_update(
    app,
    db: Database,
    channel: str,
    parent_ts: str,
    reply_ts: str | None,
    job_id: str,
    parent_row: dict,
    payload: str,
    msg: dict | None = None,
) -> None:
    """Handle !update <text> — merge new context with existing description, re-score.

    Unlike a fresh !triage, this command augments the existing record rather than
    replacing it. The LLM receives the original job description concatenated with
    the pasted text so historical context (prior skills match, original JD) is
    preserved. The re-scored listing overwrites the DB row via upsert.
    """
    try:
        app.client.reactions_add(channel=channel, timestamp=parent_ts, name="eyes")
    except SlackApiError as e:
        if e.response["error"] != "already_reacted":
            raise

    try:
        import json as _json

        from src.profile_loader import load_profile
        from src.triage import ExtractedListing, TriageSession

        profile = load_profile()
        settings = profile["settings"]
        dedup_window = settings.get("dedup_window_days", 30)
        pass_window = settings.get("pass_window_days", 180)

        # Merge existing description with new text — preserves historical context
        existing_text = (parent_row["raw_email_text"] or parent_row["job_summary"] or "").strip()
        if existing_text:
            combined_text = (
                f"{existing_text}\n\n--- ADDITIONAL MANUAL CONTEXT ---\n\n{payload}"
            )
        else:
            combined_text = payload

        # Restore original links from the DB row
        existing_links: list[str] = []
        if parent_row["links"]:
            try:
                existing_links = _json.loads(parent_row["links"])
            except Exception:
                existing_links = []

        anchor = ExtractedListing(
            title=parent_row["title"] or "",
            company=parent_row["company"] or "",
            location=parent_row["location"] or "",
            salary=parent_row["salary"] or "not listed",
            job_summary=combined_text[:300].strip(),
            description=combined_text[:4000],
            links=existing_links,
        )

        _post_thread_reply(
            app, channel, parent_ts,
            ":hourglass_flowing_sand: Merging context and re-scoring (Stage 5)...",
        )

        with TriageSession(
            profile_llm_context=profile["llm_context"],
            bypass_rejection=True,
        ) as session:
            listing = session.evaluate_listing(
                anchor=anchor,
                job_text=combined_text,
                job_links=existing_links,
                classification="MANUAL_TRIAGE",
                source="manual",
            )

        if listing is None:
            _post_thread_reply(
                app, channel, parent_ts,
                ":x: Scoring returned no result — the description may be too short or ambiguous.",
            )
            return

        was_update, effective_id = db.upsert_listing(
            listing, window_days=dedup_window, pass_window_days=pass_window,
        )
        effective_id = effective_id if was_update else listing.id
        logger.info(
            "!update: %s %s at %s (id=%s)",
            "overwrote" if was_update else "inserted",
            listing.title, listing.company,
            effective_id[:8] if effective_id else "?",
        )
        # Smart Upsert preserves pipeline_status — reset based on verdict so the
        # re-scored listing re-enters the active triage flow, EXCEPT when the
        # verdict is NO: those are auto-dismissed as "passed" regardless of how
        # they reached Slack.
        db.update_pipeline_status(
            effective_id or job_id, _post_triage_status(listing.verdict),
        )

        # Edit the original job card in-place rather than posting a new message
        if msg is not None:
            try:
                import json as _json

                from src.digest import build_digest_listing_attachment

                current_row = db.get_listing_by_id(effective_id or job_id)
                current_status = current_row["pipeline_status"] if current_row else "triaged"

                listing_dict = {
                    "id": effective_id or job_id,
                    "title": listing.title,
                    "company": listing.company,
                    "location": listing.location,
                    "salary": listing.salary,
                    "job_summary": listing.job_summary,
                    "verdict": listing.verdict,
                    "confidence": listing.confidence,
                    "pipeline_status": current_status,
                    "skills_extracted": listing.skills_extracted,
                    "matching_skills": listing.matching_skills,
                    "missing_skills": listing.missing_skills,
                    "model_scores": listing.model_scores,
                    "links": _json.dumps(listing.links) if listing.links else "",
                }
                new_attachment = build_digest_listing_attachment(listing_dict)

                update_kwargs = {
                    "channel": channel,
                    "ts": parent_ts,
                    "text": f"{listing.verdict}: {listing.title} at {listing.company}",
                    "blocks": msg.get("blocks", []),
                    "attachments": [new_attachment],
                }
                if msg.get("metadata"):
                    update_kwargs["metadata"] = msg["metadata"]

                app.client.chat_update(**update_kwargs)
                logger.info("!update: edited original Slack card for %s", job_id[:8])
            except Exception:
                logger.debug("!update: could not edit original message", exc_info=True)

        # Minimal thread receipt — full detail is now in the edited card above
        import json as _json
        verdict_emoji = (
            ":large_green_circle:" if listing.verdict == "YES"
            else ":yellow_circle:" if listing.verdict == "MAYBE"
            else ":red_circle:"
        )
        receipt = (
            f"{verdict_emoji} *Re-scored:* {listing.verdict} "
            f"({listing.confidence}%) — card updated."
        )
        _post_thread_reply(app, channel, parent_ts, receipt)

    except Exception:
        logger.error("!update failed for %s", job_id[:8], exc_info=True)
        _post_thread_reply(app, channel, parent_ts, ":x: !update failed — check logs.")

    finally:
        try:
            app.client.reactions_remove(channel=channel, timestamp=parent_ts, name="eyes")
        except Exception:
            pass


def _apply_status_badge(
    app, channel: str, ts: str, msg: dict, status: str,
) -> None:
    """Append a visible status badge to the parent Slack message."""
    badge_text = _STATUS_BADGES.get(status, f":white_circle: Status: {status.upper()}")
    try:
        blocks = msg.get("blocks", [])
        # Remove any existing status badge (identified by our marker prefix)
        blocks = [b for b in blocks if not _is_chatops_badge(b)]
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"[ {badge_text} ]"}],
        })
        app.client.chat_update(
            channel=channel,
            ts=ts,
            text=f"Status: {status.upper()}",
            blocks=blocks,
        )
    except Exception:
        logger.debug("Failed to apply status badge for %s", ts, exc_info=True)


def _is_chatops_badge(block: dict) -> bool:
    """Check if a block is a ChatOps status badge."""
    if block.get("type") != "context":
        return False
    elements = block.get("elements", [])
    if not elements:
        return False
    text = elements[0].get("text", "")
    return text.startswith("[ ") and "Status:" in text


def _handle_pass(app, db: Database, channel: str, ts: str, job_id: str, msg: dict) -> None:
    """Process a thumbsdown: update DB and replace message with gray receipt."""
    db.update_pipeline_status(job_id, "passed")
    logger.info("Passed listing %s", job_id[:8])

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
        logger.error("Failed to update Slack message for pass", exc_info=True)
        _post_thread_reply(
            app, channel, ts,
            ":no_entry_sign: *Passed* — status recorded, but the card could not be updated.",
        )

    # Rate-limit valve: cap UI-update throughput to ~45/min (Tier 3 ceiling)
    time.sleep(1.2)


def _handle_save(app, db: Database, channel: str, ts: str, job_id: str) -> None:
    """Process a thumbsup: update DB and add checkmark receipt."""
    db.update_pipeline_status(job_id, "saved")
    logger.info("Saved listing %s", job_id[:8])

    try:
        app.client.reactions_add(
            channel=channel,
            timestamp=ts,
            name="white_check_mark",
        )
    except SlackApiError as e:
        if e.response["error"] == "already_reacted":
            logger.debug("white_check_mark already present on %s", ts)
        else:
            logger.error("Failed to add save receipt reaction", exc_info=True)
    except Exception:
        logger.error("Failed to add save receipt reaction", exc_info=True)


def _update_status_block(app, channel: str, ts: str, msg: dict, status_text: str) -> None:
    """Append or update a status context block on a Slack message."""
    try:
        blocks = msg.get("blocks", [])
        # Remove any existing status block (identified by our marker)
        blocks = [b for b in blocks if not _is_status_block(b)]
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": status_text}],
        })
        app.client.chat_update(
            channel=channel,
            ts=ts,
            text="Status update",
            blocks=blocks,
        )
    except Exception:
        logger.debug("Failed to update status block", exc_info=True)


def _is_status_block(block: dict) -> bool:
    """Check if a block is one of our progressive status blocks."""
    if block.get("type") != "context":
        return False
    elements = block.get("elements", [])
    if not elements:
        return False
    text = elements[0].get("text", "")
    return text.startswith(":hourglass_flowing_sand:") or text.startswith(":writing_hand:")


def _fetch_thread_questions(app, channel: str, ts: str, bot_user_id: str = "") -> str:
    """Fetch user-authored thread replies and concatenate them as custom questions.

    Skips bot messages (identified by bot_id or subtype). Returns empty string
    if no user replies exist.
    """
    try:
        result = app.client.conversations_replies(
            channel=channel,
            ts=ts,
            limit=50,
        )
    except Exception:
        logger.debug("Failed to fetch thread replies for %s", ts, exc_info=True)
        return ""

    messages = result.get("messages", [])
    user_texts = []
    for msg in messages:
        # Skip the parent message itself
        if msg.get("ts") == ts:
            continue
        # Skip bot messages
        if msg.get("bot_id") or msg.get("subtype"):
            continue
        text = (msg.get("text") or "").strip()
        if text and not any(text.lower().startswith(p) for p in _ALL_CHATOPS_PREFIXES):
            user_texts.append(text)

    return "\n\n".join(user_texts)


def _handle_tailor(
    app, db: Database, channel: str, ts: str, job_id: str, msg: dict, reaction_name: str,
    *,
    research_context_cache: str = "",
) -> None:
    """Process a pencil reaction: progressive UI, run tailor, post results."""
    # Signal processing
    try:
        app.client.reactions_add(
            channel=channel,
            timestamp=ts,
            name="eyes",
        )
    except SlackApiError as e:
        if e.response["error"] == "already_reacted":
            logger.debug("Eyes reaction already present on %s", ts)
        else:
            raise

    # Progressive status callback — updates the Slack message in place
    def _status_callback(stage: str) -> None:
        if stage == "researching":
            _update_status_block(
                app, channel, ts, msg,
                ":hourglass_flowing_sand: Running Deep Research (Scraping company data)...",
            )
        elif stage == "tailoring":
            _update_status_block(
                app, channel, ts, msg,
                ":writing_hand: Tailoring Assets with Claude...",
            )

    # Unified Intake: check for user-posted questions in the thread
    custom_questions = _fetch_thread_questions(app, channel, ts)
    if custom_questions:
        logger.info("Found custom questions in thread for %s", job_id[:8])

    # Run the tailor engine (synchronous immediate path)
    try:
        from src.tailor import generate_immediate
        output_dir, claude_json = generate_immediate(
            job_id,
            status_callback=_status_callback,
            custom_questions=custom_questions,
            research_context_cache=research_context_cache,
        )
    except Exception:
        logger.error("Tailor failed for %s", job_id[:8], exc_info=True)
        try:
            app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass
        return

    logger.info("Tailored listing %s → %s", job_id[:8], output_dir)

    # Deep-research sanity check: if the file was not written, research silently
    # failed. Surface a warning immediately so the user knows to !regenerate.
    if not (output_dir / "deep_research_context.txt").exists():
        logger.warning("Deep research did not complete for %s", job_id[:8])
        try:
            _post_thread_reply(
                app, channel, ts,
                ":warning: Deep research did not complete for this role. "
                "Resume tailoring finished with available data, but research context is missing. "
                "Reply `!regenerate` to retry from scratch.",
            )
        except Exception:
            logger.debug("Could not post deep-research warning", exc_info=True)

    # --- Slack UX: preserve original message, post threaded Deep Evaluation ---
    try:
        row = db.get_listing_by_id(job_id)
        title = row["title"] if row else "Unknown"
        company = row["company"] if row else "Unknown"

        # Minor update to original message — only change the status indicator
        orig_blocks = msg.get("blocks", [])
        if orig_blocks and orig_blocks[0].get("type") == "section":
            first_text = orig_blocks[0].get("text", {}).get("text", "")
            if first_text:
                updated_text = first_text.replace("New |", ":white_check_mark: Tailored |")
                orig_blocks[0]["text"]["text"] = updated_text
        try:
            app.client.chat_update(
                channel=channel,
                ts=ts,
                text=f"Tailored: {title} at {company}",
                blocks=orig_blocks,
            )
        except Exception:
            logger.debug("Failed to update status indicator on original message", exc_info=True)

        # Post Deep Evaluation as a threaded reply
        verdict = claude_json.get("post_research_verdict", "MAYBE")
        confidence = claude_json.get("post_research_confidence", "?")
        match_analysis = claude_json.get("match_analysis", "")
        skills = claude_json.get("updated_skills_match", {})
        matching = skills.get("matching", [])
        missing = skills.get("missing", [])

        verdict_emoji = ":large_green_circle:" if verdict == "YES" else (
            ":yellow_circle:" if verdict == "MAYBE" else ":red_circle:"
        )

        if len(match_analysis) > 2500:
            match_analysis = match_analysis[:2500] + "\n_(truncated)_"

        thread_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":mag: *Deep Evaluation: {title}* — {company}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{verdict_emoji} *Post-Research Verdict:* "
                        f"{verdict} ({confidence}% confidence)"
                    ),
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Match Analysis:*\n{match_analysis}",
                },
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

        # Custom question answers (unified intake)
        custom_answers = claude_json.get("custom_question_answers", [])
        if custom_answers:
            qa_lines = []
            for qa in custom_answers:
                q = qa.get("question", "")
                a = qa.get("answer", "")
                qa_lines.append(f"*Q:* {q}\n*A:* {a}")
            qa_text = "\n\n".join(qa_lines)
            if len(qa_text) > 2500:
                qa_text = qa_text[:2500] + "\n_(truncated)_"
            thread_blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":question: *Application Answers:*\n{qa_text}"},
            })

        thread_blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":page_facing_up: Assets saved to `{output_dir}`",
            }],
        })

        app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f"Deep Evaluation: {title} at {company} — {verdict} ({confidence}%)",
            blocks=thread_blocks,
        )

    except Exception:
        logger.error("Failed to post Deep Evaluation thread", exc_info=True)

    # Swap reactions: remove user's pencil + bot's eyes, add checkmark
    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name=reaction_name)
    except Exception:
        pass
    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
    except Exception:
        pass
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
    except Exception:
        pass

    # Rate-limit valve: cap UI-update throughput to ~45/min (Tier 3 ceiling)
    time.sleep(1.2)


def _handle_smart_router(
    app, db: Database, channel: str, ts: str, job_id: str, msg: dict, reaction_name: str,
) -> str:
    """Smart Router for ❓ reactions — decides between tailor and answers-only.

    Fallback: If no custom text exists in the thread, treat as a ✏️ Tailor
    reaction (generate default assets, update state to tailored).

    Route A: If custom text exists AND status is triaged/saved → full pipeline
    with custom questions included. Updates state to tailored.

    Route B: If custom text exists AND status is tailored/applied/interviewing →
    lightweight answers-only fast-path using cached research.

    Returns:
        The action taken: "tailor", "answers_full", or "answers_fast"
    """
    custom_questions = _fetch_thread_questions(app, channel, ts)
    row = db.get_listing_by_id(job_id)
    current_status = row["pipeline_status"] if row else "triaged"

    # Fallback: no questions → treat as ✏️ Tailor
    if not custom_questions:
        if current_status == "tailored":
            logger.debug("❓ fallback on %s but already tailored, skipping", job_id[:8])
            return "skipped"
        logger.info("❓ fallback (no questions) for %s → running tailor", job_id[:8])
        _handle_tailor(app, db, channel, ts, job_id, msg, reaction_name)
        return "tailor"

    # Route A: unprocessed job → full pipeline with questions
    if current_status in ("triaged", "saved"):
        logger.info("Smart Router: Route A (full pipeline + answers) for %s", job_id[:8])
        _handle_tailor(app, db, channel, ts, job_id, msg, reaction_name)
        return "answers_full"

    # Route B: already processed → lightweight answers-only
    logger.info("Smart Router: Route B (fast-path answers) for %s", job_id[:8])
    _handle_answers_fast(app, db, channel, ts, job_id, msg, reaction_name, custom_questions)
    return "answers_fast"


def _handle_answers_fast(
    app, db: Database, channel: str, ts: str, job_id: str, msg: dict,
    reaction_name: str, custom_questions: str,
) -> None:
    """Fast-path: generate answers only using cached context (Route B)."""
    # Signal processing
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="eyes")
    except SlackApiError as e:
        if e.response["error"] == "already_reacted":
            pass
        else:
            raise

    try:
        from src.tailor import generate_answers_only
        output_dir, answers_json = generate_answers_only(job_id, custom_questions)
    except Exception:
        logger.error("Answers-only failed for %s", job_id[:8], exc_info=True)
        try:
            app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass
        return

    logger.info("Answers generated for %s → %s", job_id[:8], output_dir)

    # Post answers as a threaded reply
    try:
        row = db.get_listing_by_id(job_id)
        company = row["company"] if row else "Unknown"

        answers = answers_json.get("custom_question_answers", [])
        qa_lines = []
        for qa in answers:
            q = qa.get("question", "")
            a = qa.get("answer", "")
            qa_lines.append(f"*Q:* {q}\n*A:* {a}")
        qa_text = "\n\n".join(qa_lines)
        if len(qa_text) > 2800:
            qa_text = qa_text[:2800] + "\n_(truncated)_"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":white_check_mark: *Custom answers generated (Fast Pass) "
                        f"— {company}*"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": qa_text},
            },
            {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f":page_facing_up: Saved to `{output_dir}`",
                }],
            },
        ]

        app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f"Custom answers generated (Fast Pass): {company}",
            blocks=blocks,
        )
    except Exception:
        logger.error("Failed to post answers thread for %s", job_id[:8], exc_info=True)

    # Swap reactions
    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name=reaction_name)
    except Exception:
        pass
    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
    except Exception:
        pass
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
    except Exception:
        pass


def _scan_triage_commands(
    app, db: Database, channel: str, messages: list[dict],
) -> int:
    """Scan channel messages for !triage commands.

    Unlike other ChatOps commands that live in threads, !triage is posted
    directly as a channel message. Scans all recent messages for the
    !triage prefix.
    """
    processed = 0

    for msg in messages:
        # Skip messages that already have listing metadata (already processed)
        if msg.get("metadata", {}).get("event_type") in _LISTING_EVENT_TYPES:
            continue
        # Skip bot messages
        if msg.get("bot_id") or msg.get("subtype"):
            continue

        raw_text = (msg.get("text") or "").strip()
        if not raw_text.lower().startswith("!triage"):
            continue

        ts = msg.get("ts", "")
        if not ts:
            continue

        # Idempotency: check if we've already replied to this message
        # by checking for a thread reply from the bot
        try:
            replies = app.client.conversations_replies(
                channel=channel, ts=ts, limit=5,
            ).get("messages", [])
            already_handled = any(r.get("bot_id") for r in replies if r.get("ts") != ts)
            if already_handled:
                continue
        except Exception:
            pass

        after_command = raw_text[len("!triage"):].strip()
        url = _extract_triage_url(after_command) if after_command else None

        if not url:
            _post_thread_reply(
                app, channel, ts,
                ":warning: `!triage` requires a URL. "
                "To enrich an existing listing with pasted text, "
                "post `!update <text>` as a thread reply within that job's Slack card.",
            )
            continue

        _handle_triage(app, db, channel, ts, url)
        processed += 1

    return processed


def _scan_triage_fallback_commands(
    app, db: Database, channel: str, messages: list[dict],
) -> int:
    """Scan !triage scrape-failure threads for !update fallback replies.

    When ``!triage <URL>`` cannot scrape the page, the bot posts a warning
    containing ``_TRIAGE_FALLBACK_MARKER`` as a thread reply.  On subsequent
    sweeps this function detects that warning and processes any ``!update
    <text>`` replies the user has posted in the same thread.

    Flow per qualifying thread:
      1. Confirm the bot warning (``_TRIAGE_FALLBACK_MARKER``) exists.
      2. Find the first unprocessed ``!update <text>`` user reply.
      3. Delegate to ``_handle_triage_jit`` with the pasted text + original URL.
      4. ``_mark_reply_done`` places ✅ on the reply; future sweeps skip it.

    Returns the number of fallback commands processed.
    """
    processed = 0

    for msg in messages:
        # Only consider plain !triage messages — not existing job cards
        if msg.get("metadata", {}).get("event_type") in _LISTING_EVENT_TYPES:
            continue
        if msg.get("bot_id") or msg.get("subtype"):
            continue

        raw_text = (msg.get("text") or "").strip()
        if not raw_text.lower().startswith("!triage"):
            continue

        # Extract the original URL from the !triage message itself
        after_command = raw_text[len("!triage"):].strip()
        source_url = _extract_triage_url(after_command) if after_command else None
        if not source_url:
            continue

        triage_ts = msg.get("ts", "")
        if not triage_ts:
            continue

        # Fetch thread replies
        try:
            thread_result = app.client.conversations_replies(
                channel=channel, ts=triage_ts, limit=20,
            )
        except Exception:
            logger.debug(
                "Failed to fetch thread for triage fallback scan on %s", triage_ts,
                exc_info=True,
            )
            continue

        replies = thread_result.get("messages", [])

        # Only proceed if the bot has already posted a scrape-failure warning here
        has_warning = any(
            r.get("bot_id") and _TRIAGE_FALLBACK_MARKER in (r.get("text") or "")
            for r in replies
            if r.get("ts") != triage_ts
        )
        if not has_warning:
            continue

        # Find the first unprocessed !update reply from the user
        for reply in replies:
            if reply.get("ts") == triage_ts:
                continue
            if reply.get("bot_id") or reply.get("subtype"):
                continue
            if _reply_is_processed(reply):
                continue

            reply_text = (reply.get("text") or "").strip()
            if not reply_text.lower().startswith("!update"):
                continue

            payload = reply_text[len("!update"):].strip()
            if not payload:
                continue

            reply_ts = reply.get("ts")

            if _is_url(payload):
                _post_thread_reply(
                    app, channel, triage_ts,
                    ":warning: `!update` expects pasted job description text, not a URL.",
                )
                _mark_reply_done(app, channel, reply_ts)
                continue

            _handle_triage_jit(app, db, channel, triage_ts, reply_ts, source_url, payload)
            _mark_reply_done(app, channel, reply_ts)
            processed += 1
            break  # One !update per thread per sweep

    return processed


def _handle_triage(
    app, db: Database, channel: str, ts: str, payload: str,
) -> None:
    """Process a !triage command: scrape URL or use raw text, run local LLM triage.

    Performs Smart Upsert: if a fuzzy match exists, overwrites data fields
    but preserves pipeline_status.
    """
    # Signal processing
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="eyes")
    except SlackApiError as e:
        if e.response["error"] == "already_reacted":
            pass
        else:
            raise

    # Determine if payload is a URL or raw text
    job_text = ""
    source_url = ""
    if _is_url(payload):
        source_url = payload
        _post_thread_reply(app, channel, ts, ":hourglass_flowing_sand: Scraping URL...")
        job_text = _scrape_for_triage(payload)
        if not job_text:
            _post_thread_reply(
                app, channel, ts,
                f":warning: Could not scrape that URL. "
                f"{_TRIAGE_FALLBACK_MARKER} <job description text>` to parse this job manually.",
            )
            try:
                app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
            except Exception:
                pass
            return
    else:
        job_text = payload

    # Run local LLM triage
    _post_thread_reply(app, channel, ts, ":hourglass_flowing_sand: Running local LLM triage...")
    try:
        from src.profile_loader import load_profile
        from src.triage import TriageSession

        profile = load_profile()
        profile_text = profile["llm_context"]
        settings = profile["settings"]
        dedup_window = settings.get("dedup_window_days", 30)
        pass_window = settings.get("pass_window_days", 180)

        links = [source_url] if source_url else []
        with TriageSession(
            profile_llm_context=profile_text,
            bypass_rejection=True,
        ) as session:
            listings = session.triage_email(
                email_text=job_text,
                email_links=links,
                classification="MANUAL_TRIAGE",
                source="manual",
                source_is_scraped_url=bool(source_url),
            )
            failure_reason = session.last_failure_reason
    except Exception:
        logger.error("Manual triage failed", exc_info=True)
        _post_thread_reply(app, channel, ts, ":x: Triage failed — check logs for details.")
        try:
            app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass
        return

    if not listings:
        if failure_reason == "stage2_missing_required_fields":
            _post_thread_reply(
                app, channel, ts,
                ":rotating_light: Ingestion aborted: Could not identify company/role "
                "from the source text.",
            )
        else:
            _post_thread_reply(
                app, channel, ts,
                f":warning: Could not extract a job from that URL (likely bot protection). "
                f"{_TRIAGE_FALLBACK_MARKER} <job description text>` to parse this job manually.",
            )
        try:
            app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass
        return

    # Process each extracted listing
    for listing in listings:
        # Smart Upsert
        was_update, existing_id = db.upsert_listing(
            listing, window_days=dedup_window, pass_window_days=pass_window,
        )
        effective_id = existing_id if was_update else listing.id

        # A NO verdict — even surfaced via manual !triage — should be auto-passed
        # so it shows up in the rejected lane rather than as an active triage card.
        new_status = _post_triage_status(listing.verdict)
        if was_update:
            # Smart Upsert preserves pipeline_status — reset so a listing previously
            # in a terminal state (passed, rejected) is fully revived, unless the
            # current verdict is NO (then it stays/becomes "passed").
            db.update_pipeline_status(effective_id, new_status)
            logger.info(
                "Smart Upsert: overwrote %s with manual triage for %s at %s",
                existing_id[:8], listing.title, listing.company,
            )
        elif new_status == "passed":
            # Brand new NO insert — flip "triaged" (the default) → "passed".
            db.update_pipeline_status(effective_id, new_status)

        # Post triage result as a Slack message with metadata
        _post_triage_result(app, db, channel, ts, listing, effective_id, was_update)

    # Clean up eyes reaction
    try:
        app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
    except Exception:
        pass
    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
    except Exception:
        pass


def _handle_triage_jit(
    app,
    db: Database,
    channel: str,
    triage_ts: str,
    reply_ts: str | None,
    source_url: str,
    pasted_text: str,
) -> None:
    """JIT triage: run the full Stage 1-5 pipeline on user-pasted text.

    Called by ``_scan_triage_fallback_commands`` when the user replies
    ``!update <text>`` to a ``!triage`` scrape-failure warning.  The pasted
    text is treated as already-scraped content (``source_is_scraped_url=True``)
    so Stage 3 judges it directly instead of attempting another scrape.
    The original URL is forwarded as a link hint so it is preserved in the
    listing and the Slack card.

    Late deduplication — the DB check happens AFTER the LLM has structured the
    data and generated a canonical job ID:
      - New listing   → post a standard digest card in the channel.
      - Duplicate     → post the updated card with the overwrite badge and
                        notify the user in the triage thread.

    Idempotency: ``_mark_reply_done`` (called by the parent scanner) places ✅
    on the ``!update`` reply so subsequent sweeps skip it.
    """
    try:
        app.client.reactions_add(channel=channel, timestamp=triage_ts, name="eyes")
    except SlackApiError as e:
        if e.response["error"] != "already_reacted":
            raise

    try:
        from src.profile_loader import load_profile
        from src.triage import TriageSession

        profile = load_profile()
        settings = profile["settings"]
        dedup_window = settings.get("dedup_window_days", 30)
        pass_window = settings.get("pass_window_days", 180)

        _post_thread_reply(
            app, channel, triage_ts,
            ":hourglass_flowing_sand: Running full LLM triage on pasted text...",
        )

        with TriageSession(
            profile_llm_context=profile["llm_context"],
            bypass_rejection=True,
        ) as session:
            listings = session.triage_email(
                email_text=pasted_text,
                email_links=[source_url],
                classification="MANUAL_TRIAGE",
                source="manual",
                source_is_scraped_url=True,
            )
            failure_reason = session.last_failure_reason

    except Exception:
        logger.error("JIT triage failed", exc_info=True)
        _post_thread_reply(app, channel, triage_ts, ":x: Triage failed — check logs for details.")
        try:
            app.client.reactions_remove(channel=channel, timestamp=triage_ts, name="eyes")
        except Exception:
            pass
        return

    if not listings:
        if failure_reason == "stage2_missing_required_fields":
            _post_thread_reply(
                app, channel, triage_ts,
                ":rotating_light: Could not identify company/role from the pasted text. "
                "Try including more of the job description.",
            )
        else:
            _post_thread_reply(
                app, channel, triage_ts,
                ":x: No job listing could be extracted. Try pasting more of the description.",
            )
        try:
            app.client.reactions_remove(channel=channel, timestamp=triage_ts, name="eyes")
        except Exception:
            pass
        return

    for listing in listings:
        # Late deduplication — check AFTER the LLM has structured the data
        was_update, existing_id = db.upsert_listing(
            listing, window_days=dedup_window, pass_window_days=pass_window,
        )
        effective_id = existing_id if was_update else listing.id

        new_status = _post_triage_status(listing.verdict)
        if was_update:
            # Smart Upsert preserves the old pipeline_status, but the JIT card carries
            # fresh, complete data — reset so tailor reactions aren't blocked by a
            # stale status, unless the current verdict is NO (auto-dismiss as "passed").
            db.update_pipeline_status(effective_id, new_status)
            logger.info(
                "JIT triage: overwrote %s with manual triage for %s at %s",
                existing_id[:8], listing.title, listing.company,
            )
            _post_thread_reply(
                app, channel, triage_ts,
                ":arrows_counterclockwise: Duplicate listing detected "
                "— updated the existing record.",
            )
        elif new_status == "passed":
            # Brand new NO insert — flip "triaged" (the default) → "passed".
            db.update_pipeline_status(effective_id, new_status)

        _post_triage_result(app, db, channel, triage_ts, listing, effective_id, was_update)

    try:
        app.client.reactions_remove(channel=channel, timestamp=triage_ts, name="eyes")
    except Exception:
        pass
    try:
        app.client.reactions_add(channel=channel, timestamp=triage_ts, name="white_check_mark")
    except Exception:
        pass


def _is_url(text: str) -> bool:
    """Check if text looks like a URL."""
    text = text.strip()
    return text.startswith("http://") or text.startswith("https://")


def _extract_triage_url(payload: str) -> str | None:
    """Extract and sanitize the first http/https URL from a !triage payload.

    Handles Slack's angle-bracket wrapping (<https://...>) and ignores any
    trailing text after the URL (unfurled previews, copy-paste artifacts).
    Returns the clean URL string, or None if no URL is found.
    """
    import re
    match = re.search(r"https?://[^\s>]+", payload)
    if not match:
        return None
    return match.group(0).rstrip(">")


def _scrape_for_triage(url: str) -> str:
    """Scrape a URL for job description text, fail-fast (5s timeout, no retries).

    Returns extracted text (≥100 chars), or empty string on any failure.
    Uses a requests Session with Retry(total=0) to prevent urllib3 from
    sleeping on 429/503 anti-bot responses, mirroring triage._scrape_url.
    """
    try:
        import requests
        import trafilatura
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(total=0))
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        try:
            response = session.get(
                url, timeout=5, verify=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; apply-pilot/1.0)"},
            )
        except Exception:
            logger.debug("HTTP fetch failed for %s", url)
            return ""

        if response.status_code in (429, 503):
            logger.debug("Anti-bot response %d for %s — skipping", response.status_code, url)
            return ""

        html = response.text
        if not html:
            return ""
        text = trafilatura.extract(html) or ""
        return text if len(text.strip()) >= 100 else ""
    except Exception:
        logger.debug("Triage scrape failed for %s", url, exc_info=True)
        return ""


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


_TREND_DEFAULT_LIMIT = 100
_TREND_MIN_LIMIT = 10
_TREND_MAX_LIMIT = 500
_TREND_DEEP_RE = re.compile(r"--deep\s+(\d+)", re.IGNORECASE)

# Sentinel values emitted by the upstream skill extractor that should not
# be counted as real skills in the trend report.
_SKILL_STOPLIST = {
    "none explicitly stated",
    "none stated",
    "none",
    "n/a",
    "na",
    "unknown",
    "not specified",
    "not applicable",
}


def _parse_trend_args(text: str) -> int:
    """Parse the requested trend window from a !trend message body.

    Recognizes ``--deep N`` (case-insensitive). Clamps N to
    [_TREND_MIN_LIMIT, _TREND_MAX_LIMIT]. Returns _TREND_DEFAULT_LIMIT
    when no --deep flag is present.
    """
    m = _TREND_DEEP_RE.search(text or "")
    if not m:
        return _TREND_DEFAULT_LIMIT
    n = int(m.group(1))
    return max(_TREND_MIN_LIMIT, min(n, _TREND_MAX_LIMIT))


def _is_sentinel_skill(name: str) -> bool:
    """True if `name` looks like a "no skills listed" placeholder.

    Used both to filter raw extractor output and to scrub canonicalized
    LLM output, since the LLM occasionally re-emits sentinels as a
    canonical group.
    """
    n = (name or "").strip().lower()
    if not n:
        return True
    if n in _SKILL_STOPLIST:
        return True
    # Catch phrasings the LLM tends to invent: "none explicitly mentioned",
    # "not specified explicitly", "no skills listed", etc.
    if n.startswith("none ") or n.startswith("not "):
        return True
    if "explicitly stated" in n or "explicitly mentioned" in n:
        return True
    return False


def _parse_skills_csv(s: str | None) -> list[str]:
    """Split a comma-separated skills string into a cleaned list.

    Filters out sentinel placeholders (e.g. "None explicitly stated") that
    the upstream extractor sometimes emits when no skills were found.
    """
    if not s:
        return []
    return [v for v in (x.strip() for x in s.split(",")) if not _is_sentinel_skill(v)]


def _drop_sentinels(d: dict[str, int]) -> dict[str, int]:
    """Strip sentinel keys from a canonical skill→count dict."""
    return {k: v for k, v in d.items() if not _is_sentinel_skill(k)}


def _classify_trend_cohort(row) -> str | None:
    """Assign a DB row to a trend cohort.

    Returns 'high_intent', 'pipeline', 'rejected', or None to skip.
    """
    status = (row["pipeline_status"] or "").lower()
    verdict = (row["verdict"] or "").upper()
    if status in ("saved", "tailored", "applied", "interviewing"):
        return "high_intent"
    if verdict in ("YES", "MAYBE") and status == "triaged":
        return "pipeline"
    if verdict == "NO" or status in ("passed", "rejected"):
        return "rejected"
    return None


async def _canonicalize_cohort(
    matched_raw: list[str],
    missing_raw: list[str],
    cohort_label: str,
    model: str,
    api_key: str,
    max_tokens: int = 600,
) -> tuple[dict[str, int], dict[str, int]]:
    """Call OpenRouter to group skill variants and return canonical frequencies.

    Returns (matched_canonical_freq, missing_canonical_freq).
    Falls back to raw Counter frequencies if the LLM call fails or lists are empty.
    """
    from collections import Counter

    import openai

    def _raw_fallback() -> tuple[dict[str, int], dict[str, int]]:
        m = dict(Counter(s.strip() for s in matched_raw if s.strip()).most_common(20))
        mi = dict(Counter(s.strip() for s in missing_raw if s.strip()).most_common(20))
        return _drop_sentinels(m), _drop_sentinels(mi)

    if not matched_raw and not missing_raw:
        return {}, {}

    def _freq_lines(skills: list[str]) -> str:
        counts = Counter(s.strip() for s in skills if s.strip())
        if not counts:
            return "(none)"
        return "\n".join(f"  {k} ({v}x)" for k, v in counts.most_common(50))

    prompt = (
        f"You are a technical skill canonicalization engine analyzing job pipeline data.\n"
        f"Cohort: {cohort_label}\n\n"
        "Group semantically identical skill strings and sum their frequencies.\n"
        "Examples: 'GenAI'/'Generative AI'/'LLMs' → 'Generative AI / LLMs'; "
        "'k8s'/'Kubernetes'/'K8s' → 'Kubernetes'; 'ML'/'Machine Learning' → 'Machine Learning'.\n\n"
        "OMIT any placeholder/sentinel entries that mean 'no skills listed' "
        "(e.g. 'None', 'None explicitly stated', 'Not specified', 'N/A', "
        "'Unknown'). Do not emit these as canonical groups under any label.\n\n"
        "MATCHED SKILLS (skills the candidate has that the job requires):\n"
        f"{_freq_lines(matched_raw)}\n\n"
        "MISSING SKILLS (skills the job requires that the candidate lacks):\n"
        f"{_freq_lines(missing_raw)}\n\n"
        'Return ONLY valid JSON with this exact structure (no markdown, no commentary):\n'
        '{"matched": {"Canonical Skill": count, ...}, "missing": {"Canonical Skill": count, ...}}'
    )

    try:
        client = openai.AsyncOpenAI(
            base_url=_OPENROUTER_BASE_URL,
            api_key=api_key,
        )
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=max_tokens,
        )
        text = (response.choices[0].message.content or "").strip()
        # Strip markdown code fences if the model wraps output in them
        if "```" in text:
            inner = text.split("```")
            text = next(
                (p.lstrip("json").strip() for p in inner if "{" in p),
                text,
            )
        result = json.loads(text)
        matched_canon = {str(k): int(v) for k, v in result.get("matched", {}).items()}
        missing_canon = {str(k): int(v) for k, v in result.get("missing", {}).items()}
        return _drop_sentinels(matched_canon), _drop_sentinels(missing_canon)
    except Exception:
        logger.warning(
            "Skill canonicalization failed for cohort '%s', falling back to raw counts",
            cohort_label, exc_info=True,
        )
        return _raw_fallback()


def _fit_skill(name: str, width: int) -> str:
    """Left-pad a skill name to `width` chars, using an ellipsis when truncating."""
    if len(name) <= width:
        return name.ljust(width)
    return (name[: width - 1] + "…").ljust(width)


def _format_trend_section(
    matched: dict[str, int],
    missing: dict[str, int],
    count: int,
    header: str,
    subtitle: str,
) -> str:
    """Format a single cohort section as a two-column monospace block.

    Each row shows the skill name, raw count, and share-of-cohort percentage,
    so totals across cohorts of different sizes are comparable.
    """
    _COL = 28        # skill-name width (was 22; widened to fit "Cross-functional Leadership")
    _CNT = 4
    _PCT = 5         # e.g. " 100%"
    _IND = "   "
    _GAP = "   "

    lines = [f"── {header}  ({count} jobs) ──", f"{_IND}{subtitle}", ""]

    if not matched and not missing:
        lines.append(f"{_IND}(no skill data for this cohort)")
        return "\n".join(lines)

    top_m = sorted(matched.items(), key=lambda x: -x[1])[:10]
    top_mi = sorted(missing.items(), key=lambda x: -x[1])[:10]

    cell_w = _COL + _CNT + _PCT
    sep = "-" * cell_w
    lines.append(
        f"{_IND}{'Matched (top 10)'.ljust(cell_w)}"
        f"{_GAP}{'Missing / Gaps (top 10)'}"
    )
    lines.append(f"{_IND}{sep}{_GAP}{sep}")

    def _pct(cnt: int) -> str:
        if count <= 0:
            return "    -"
        return f"{round(100 * cnt / count):>4d}%"

    max_rows = max(len(top_m), len(top_mi), 1)
    for i in range(max_rows):
        if i < len(top_m):
            skill, cnt = top_m[i]
            left = (
                f"{_IND}{_fit_skill(skill, _COL)}"
                f"{str(cnt).rjust(_CNT)}{_pct(cnt)}"
            )
        else:
            left = " " * (len(_IND) + cell_w)

        if i < len(top_mi):
            skill, cnt = top_mi[i]
            right = (
                f"{_GAP}{_fit_skill(skill, _COL)}"
                f"{str(cnt).rjust(_CNT)}{_pct(cnt)}"
            )
        else:
            right = ""

        lines.append(left + right)

    return "\n".join(lines)


def _format_trend_report(
    high_intent: tuple[dict, dict], high_n: int,
    pipeline: tuple[dict, dict], pipeline_n: int,
    rejected: tuple[dict, dict], rejected_n: int,
    total: int,
) -> str:
    """Assemble the full trend report as a single monospace string.

    Kept for tests and logging; the Slack post path uses
    :func:`_format_trend_chunks` instead so each cohort fits within Slack's
    per-section text limit.
    """
    return "\n\n".join(
        _format_trend_chunks(
            high_intent, high_n, pipeline, pipeline_n,
            rejected, rejected_n, total,
        )
    )


def _format_trend_chunks(
    high_intent: tuple[dict, dict], high_n: int,
    pipeline: tuple[dict, dict], pipeline_n: int,
    rejected: tuple[dict, dict], rejected_n: int,
    total: int,
) -> list[str]:
    """Return the report as a list of monospace chunks (one per Slack block).

    Slack section blocks cap text at 3000 chars; on `--deep` runs the
    rejected cohort alone can exceed the old 2900-char single-block limit.
    Splitting per cohort keeps every chunk well under the cap.
    """
    summary = (
        f"High Intent: {high_n}  │  "
        f"Pipeline: {pipeline_n}  │  "
        f"Rejected: {rejected_n}"
    )
    return [
        f"SKILL TRENDS — Last {total} Jobs\n{summary}",
        _format_trend_section(
            high_intent[0], high_intent[1], high_n,
            "HIGH INTENT", "Saved / Tailored / Applied",
        ),
        _format_trend_section(
            pipeline[0], pipeline[1], pipeline_n,
            "PIPELINE", "Scored YES/MAYBE, not saved",
        ),
        _format_trend_section(
            rejected[0], rejected[1], rejected_n,
            "REJECTED", "Scored NO / User passed",
        ),
    ]


# Slack section block text cap is 3000; leave headroom for the surrounding
# triple backticks and any future formatting tweaks.
_SLACK_SECTION_TEXT_MAX = 2900


def _handle_trend(
    app,
    db: Database,
    channel: str,
    ts: str,
    limit: int = _TREND_DEFAULT_LIMIT,
) -> None:
    """Handle !trend: query skill data, canonicalize via LLM, post monospace report.

    ``limit`` controls how many recent jobs are scanned (default 100). When
    invoked as ``!trend --deep N`` the caller passes the parsed N here.
    """
    import asyncio
    import os

    try:
        app.client.reactions_add(channel=channel, timestamp=ts, name="eyes")
    except SlackApiError as e:
        if e.response["error"] != "already_reacted":
            raise

    try:
        rows = db.get_trend_skills(limit=limit)
        if not rows:
            app.client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=":bar_chart: No skill data found yet — run the pipeline first.",
            )
            return

        cohort_matched: dict[str, list[str]] = {
            "high_intent": [], "pipeline": [], "rejected": [],
        }
        cohort_missing: dict[str, list[str]] = {
            "high_intent": [], "pipeline": [], "rejected": [],
        }
        cohort_counts: dict[str, int] = {"high_intent": 0, "pipeline": 0, "rejected": 0}

        for row in rows:
            cohort = _classify_trend_cohort(row)
            if not cohort:
                continue
            cohort_counts[cohort] += 1
            cohort_matched[cohort].extend(_parse_skills_csv(row["matching_skills"]))
            cohort_missing[cohort].extend(_parse_skills_csv(row["missing_skills"]))

        api_key = os.getenv("OPENROUTER_API_KEY", "")
        model = os.getenv("OPENROUTER_TREND_MODEL", "openai/gpt-4o-mini")
        # Output JSON grows with the number of canonical groups; give the
        # model more headroom on deeper windows.
        canon_max_tokens = 1200 if limit > 200 else 600

        async def _run_all():
            return await asyncio.gather(
                _canonicalize_cohort(
                    cohort_matched["high_intent"], cohort_missing["high_intent"],
                    "High Intent", model, api_key, canon_max_tokens,
                ),
                _canonicalize_cohort(
                    cohort_matched["pipeline"], cohort_missing["pipeline"],
                    "Pipeline", model, api_key, canon_max_tokens,
                ),
                _canonicalize_cohort(
                    cohort_matched["rejected"], cohort_missing["rejected"],
                    "Rejected", model, api_key, canon_max_tokens,
                ),
            )

        hi_result, pipe_result, rej_result = asyncio.run(_run_all())

        total = len(rows)
        chunks = _format_trend_chunks(
            hi_result, cohort_counts["high_intent"],
            pipe_result, cohort_counts["pipeline"],
            rej_result, cohort_counts["rejected"],
            total,
        )

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":bar_chart: *Skill Trend Analysis*"},
            }
        ]
        for chunk in chunks:
            block_text = f"```\n{chunk}\n```"
            if len(block_text) > _SLACK_SECTION_TEXT_MAX:
                # Defensive: a single cohort should never hit this with the
                # current top-10 layout, but truncate gracefully if it does.
                keep = _SLACK_SECTION_TEXT_MAX - len("```\n\n...(truncated)\n```")
                block_text = f"```\n{chunk[:keep]}\n...(truncated)\n```"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": block_text},
            })

        app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text="Skill Trends",
            blocks=blocks,
        )
        logger.info(
            "!trend: posted report (limit=%d, %d jobs, hi=%d pipe=%d rej=%d)",
            limit, total, cohort_counts["high_intent"],
            cohort_counts["pipeline"], cohort_counts["rejected"],
        )

    except Exception:
        logger.error("!trend failed", exc_info=True)
        try:
            app.client.chat_postMessage(
                channel=channel, thread_ts=ts, text=":x: !trend failed — check logs.",
            )
        except Exception:
            pass

    finally:
        try:
            app.client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass
        try:
            app.client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
        except Exception:
            pass


def _scan_trend_commands(
    app, db: Database, channel: str, messages: list[dict],
) -> int:
    """Scan channel messages for !trend commands.

    !trend is a channel-level command (not a thread reply). Posts the trend
    report as a thread reply to the !trend message. Idempotency: skips any
    !trend message that already has a bot reply in its thread.

    Returns the number of !trend commands processed.
    """
    processed = 0

    for msg in messages:
        if msg.get("metadata", {}).get("event_type") in _LISTING_EVENT_TYPES:
            continue
        if msg.get("bot_id") or msg.get("subtype"):
            continue

        raw_text = (msg.get("text") or "").strip()
        if not raw_text.lower().startswith("!trend"):
            continue

        ts = msg.get("ts", "")
        if not ts:
            continue

        # Idempotency: skip if bot already replied in the thread
        try:
            replies = app.client.conversations_replies(
                channel=channel, ts=ts, limit=5,
            ).get("messages", [])
            if any(r.get("bot_id") for r in replies if r.get("ts") != ts):
                continue
        except Exception:
            pass

        limit = _parse_trend_args(raw_text)
        _handle_trend(app, db, channel, ts, limit=limit)
        processed += 1

    return processed


def _post_thread_reply(app, channel: str, ts: str, text: str) -> None:
    """Post a simple text reply in a thread."""
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=ts, text=text)
    except Exception:
        logger.debug("Failed to post thread reply", exc_info=True)


def _post_triage_result(
    app, db: Database, channel: str, parent_ts: str,
    listing, effective_id: str, was_update: bool,
) -> None:
    """Post the triage result as a Slack message with reaction legend and metadata."""
    from src.digest import build_digest_listing_attachment

    # Build a listing dict for the digest attachment builder
    listing_dict = {
        "id": effective_id,
        "title": listing.title,
        "company": listing.company,
        "location": listing.location,
        "salary": listing.salary,
        "job_summary": listing.job_summary,
        "verdict": listing.verdict,
        "confidence": listing.confidence,
        "pipeline_status": "triaged",
        "skills_extracted": listing.skills_extracted,
        "matching_skills": listing.matching_skills,
        "missing_skills": listing.missing_skills,
        "model_scores": listing.model_scores,
        "links": json.dumps(listing.links) if listing.links else "",
    }

    # Check current status if this was an upsert
    if was_update:
        row = db.get_listing_by_id(effective_id)
        if row:
            listing_dict["pipeline_status"] = row["pipeline_status"]

    attachment = build_digest_listing_attachment(listing_dict)

    # Prepend upsert badge if this was an update
    upsert_badge = ""
    if was_update:
        upsert_badge = "[ :arrows_counterclockwise: Overwrote existing truncated listing ]\n"

    # Build the message blocks
    blocks = []
    if upsert_badge:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": upsert_badge.strip()}],
        })
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f":clipboard: *Manual Triage: {listing.title}* — {listing.company}",
        },
    })

    metadata = {
        "event_type": "apply_daemon_listing",
        "event_payload": {"job_id": effective_id},
    }

    try:
        app.client.chat_postMessage(
            channel=channel,
            text=f"{listing.verdict}: {listing.title} at {listing.company} (manual triage)",
            blocks=blocks,
            attachments=[attachment],
            metadata=metadata,
        )
        db.mark_slack_notified(effective_id)
    except Exception:
        logger.error("Failed to post manual triage result", exc_info=True)


def _format_diff_text(assets_json: dict) -> str:
    """Format resume and cover letter diffs into readable Slack markdown.

    Returns empty string if no diffs are available.
    """
    parts: list[str] = []

    # Resume bullet diffs
    bullet_edits = assets_json.get("resume_bullet_edits", [])
    if bullet_edits and isinstance(bullet_edits, list):
        diff_lines: list[str] = []
        for edit in bullet_edits:
            if isinstance(edit, dict):
                diff = edit.get("slack_diff", "")
                if diff:
                    diff_lines.append(f"  • {diff}")
                else:
                    diff_lines.append("  • _(Diff unavailable)_")
            elif isinstance(edit, str):
                diff_lines.append(f"  • {edit}")
        if diff_lines:
            parts.append("*Resume Edits:*\n" + "\n".join(diff_lines))

    # Cover letter diff summary
    cl_diff = assets_json.get("cover_letter_diff_summary", "")
    if cl_diff:
        parts.append(f"*Cover Letter Updates:*\n{cl_diff}")

    return "\n\n".join(parts)


def _post_diff_thread(app, channel: str, ts: str, output_dir) -> None:
    """Post a diff summary as a threaded reply to the tailored message."""
    import json as _json
    from pathlib import Path

    assets_path = Path(output_dir) / "assets.json"
    if not assets_path.exists():
        return

    try:
        assets_json = _json.loads(assets_path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Could not read assets.json for diff thread", exc_info=True)
        return

    diff_text = _format_diff_text(assets_json)
    if not diff_text:
        return

    try:
        # Truncate for Slack block limit
        if len(diff_text) > 2900:
            diff_text = diff_text[:2900] + "\n\n_(truncated — see full assets.json)_"

        app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text="Diff Summary",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":pencil2: *Tailoring Diff Summary*",
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": diff_text},
                },
            ],
        )
        logger.info("Posted diff summary thread")
    except Exception:
        logger.error("Failed to post diff thread", exc_info=True)


def _post_interview_prep_thread(
    app, channel: str, ts: str, output_dir, company: str,
) -> None:
    """Post interview prep guide as a threaded reply if the file exists."""
    from pathlib import Path
    prep_path = Path(output_dir) / f"Interview_Prep_{company}.md"
    if not prep_path.exists():
        return
    try:
        prep_text = prep_path.read_text(encoding="utf-8")
        if not prep_text.strip():
            return
        # Slack has a 3000 char limit per message block; truncate if needed
        if len(prep_text) > 2900:
            prep_text = prep_text[:2900] + "\n\n_(truncated — see full file)_"
        app.client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=f"Interview Prep: {company}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":mortar_board: *Interview Prep Guide — {company}*",
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": prep_text},
                },
            ],
        )
        logger.info("Posted interview prep thread for %s", company)
    except Exception:
        logger.error("Failed to post interview prep thread", exc_info=True)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Apply Pilot Slack sweeper")
    parser.add_argument(
        "--deep",
        metavar="N",
        type=int,
        default=None,
        help="Look back N messages (cursor-paginated). Defaults to 50.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sweep(limit=args.deep or 50)


if __name__ == "__main__":
    main()
