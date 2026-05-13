"""Slack bot integration for posting pipeline results and handling interactions.

Optional module — if SLACK_BOT_TOKEN is not set, all functions are no-ops.
Uses Block Kit with color-coded attachments for AUTO_MATCH / ESCALATE statuses,
per-model score breakdowns, and actionable buttons.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class PipelineSummary:
    """Summary statistics from a pipeline run."""

    emails_fetched: int = 0
    emails_processed: int = 0
    emails_skipped: int = 0
    emails_deduped: int = 0
    listings_stored: int = 0
    verdict_counts: dict[str, int] = field(default_factory=dict)
    yes_listings: list[dict] | None = None
    maybe_listings: list[dict] | None = None
    escalate_listings: list[dict] | None = None
    auto_match_listings: list[dict] | None = None


def _get_slack_config() -> tuple[str | None, str | None]:
    """Load Slack configuration from environment."""
    load_dotenv()
    token = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("SLACK_CHANNEL_ID")
    return token, channel


def is_slack_configured() -> bool:
    """Check if Slack integration is configured."""
    token, channel = _get_slack_config()
    return bool(token and channel)


def _import_slack_app(token: str) -> object:
    """Create a Slack Bolt App instance. Separated for testability."""
    from slack_bolt import App
    return App(token=token)


def post_pipeline_summary(summary: PipelineSummary) -> bool:
    """Post a pipeline run summary to Slack.

    Returns True if posted successfully. No-op if Slack is not configured.
    """
    token, channel = _get_slack_config()
    if not token or not channel:
        logger.debug("Slack not configured, skipping notification")
        return False

    try:
        app = _import_slack_app(token)
    except ImportError:
        logger.warning("slack-bolt not installed. Run: pip install slack-bolt")
        return False

    try:

        # Post header + stats as the main message
        blocks = _build_header_blocks(summary)
        text = _build_summary_text(summary)
        app.client.chat_postMessage(channel=channel, text=text, blocks=blocks)

        # Post each listing category as a color-coded attachment
        auto_match = summary.auto_match_listings or []
        escalate = summary.escalate_listings or []
        yes_listings = summary.yes_listings or []
        maybe_listings = summary.maybe_listings or []

        for listing in auto_match:
            attachment = _build_listing_attachment(listing, status="auto_match")
            app.client.chat_postMessage(
                channel=channel, text=f"AUTO_MATCH: {listing.get('title', '')}",
                attachments=[attachment],
            )

        for listing in escalate:
            attachment = _build_listing_attachment(listing, status="escalate")
            app.client.chat_postMessage(
                channel=channel, text=f"ESCALATE: {listing.get('title', '')}",
                attachments=[attachment],
            )

        for listing in yes_listings:
            if listing.get("final_status") not in ("auto_match", "escalate"):
                attachment = _build_listing_attachment(listing, status="yes")
                app.client.chat_postMessage(
                    channel=channel, text=f"YES: {listing.get('title', '')}",
                    attachments=[attachment],
                )

        for listing in maybe_listings:
            if listing.get("final_status") not in ("auto_match", "escalate"):
                attachment = _build_listing_attachment(listing, status="maybe")
                app.client.chat_postMessage(
                    channel=channel, text=f"MAYBE: {listing.get('title', '')}",
                    attachments=[attachment],
                )

        logger.info("Posted pipeline summary to Slack channel %s", channel)
        return True
    except Exception as exc:
        error_str = str(exc)
        if "not_in_channel" in error_str:
            logger.warning(
                "\n"
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  SLACK ERROR: Bot is not in the channel.                    ║\n"
                "║                                                             ║\n"
                "║  Please go to your Slack channel and type:                  ║\n"
                "║    /invite @YourBotName                                     ║\n"
                "║                                                             ║\n"
                "║  The bot must be a member of the channel to post messages.  ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
        elif "channel_not_found" in error_str:
            logger.warning(
                "\n"
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  SLACK ERROR: Channel not found.                            ║\n"
                "║                                                             ║\n"
                "║  Verify SLACK_CHANNEL_ID in your .env file.                 ║\n"
                "║  Use the channel ID (e.g. C0123456789), not the name.       ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
        elif "invalid_auth" in error_str or "token_revoked" in error_str:
            logger.warning(
                "\n"
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  SLACK ERROR: Invalid or revoked bot token.                 ║\n"
                "║                                                             ║\n"
                "║  Verify SLACK_BOT_TOKEN in your .env file.                  ║\n"
                "║  Regenerate the token at https://api.slack.com/apps         ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
        else:
            logger.error("Failed to post Slack message", exc_info=True)
        return False


def _build_summary_text(summary: PipelineSummary) -> str:
    """Build plain-text fallback for Slack notification."""
    vc = summary.verdict_counts
    return (
        f"Pipeline run complete: "
        f"{summary.emails_fetched} emails -> {summary.listings_stored} listings. "
        f"Verdicts: {vc.get('yes', 0)} YES, {vc.get('maybe', 0)} MAYBE, {vc.get('no', 0)} NO"
    )


def _build_header_blocks(summary: PipelineSummary) -> list[dict]:
    """Build Block Kit blocks for the pipeline summary header."""
    vc = summary.verdict_counts
    auto_count = len(summary.auto_match_listings or [])
    escalate_count = len(summary.escalate_listings or [])

    blocks: list[dict] = []

    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "Pipeline Run Complete"}
    })

    stats_lines = [
        f":inbox_tray: *{summary.emails_fetched}* emails "
        f"-> *{summary.emails_processed}* processed "
        f"-> *{summary.listings_stored}* listings stored",
        f":white_check_mark: *{vc.get('yes', 0)}* YES  |  "
        f":grey_question: *{vc.get('maybe', 0)}* MAYBE  |  "
        f":x: *{vc.get('no', 0)}* NO",
    ]

    if auto_count or escalate_count:
        stats_lines.append(
            f":large_green_circle: *{auto_count}* AUTO_MATCH  |  "
            f":large_yellow_circle: *{escalate_count}* ESCALATE"
        )

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(stats_lines)}
    })

    if summary.emails_deduped:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f":recycle: {summary.emails_deduped} duplicate emails skipped",
            }]
        })

    blocks.append({"type": "divider"})
    return blocks


def _build_listing_attachment(listing: dict, status: str) -> dict:
    """Build a Slack attachment (color-coded) with Block Kit blocks for a listing."""
    listing_id = listing.get("id", "")
    title = listing.get("title", "Unknown")
    company = listing.get("company", "Unknown")
    location = listing.get("location", "")
    salary = listing.get("salary", "")
    confidence = listing.get("confidence", 0)
    reason = listing.get("reason", "")

    # Color coding
    color_map = {
        "auto_match": "#2eb67d",  # Green
        "escalate": "#ecb22e",    # Yellow
        "yes": "#2eb67d",         # Green
        "maybe": "#36c5f0",       # Blue
    }
    color = color_map.get(status, "#ddd")

    # Status badge
    status_badges = {
        "auto_match": ":large_green_circle: AUTO_MATCH",
        "escalate": ":large_yellow_circle: ESCALATE",
        "yes": ":white_check_mark: YES",
        "maybe": ":grey_question: MAYBE",
    }
    badge = status_badges.get(status, status.upper())

    # Build the text body
    header_text = f"*{title}* — {company}"
    if location and location != "not specified":
        header_text += f" ({location})"

    detail_parts = [f"{badge}  |  Confidence: *{confidence}%*"]
    if salary and salary != "not listed":
        detail_parts.append(f":moneybag: {salary}")

    # Per-model scores
    model_scores_str = listing.get("model_scores", "")
    if model_scores_str:
        try:
            scores = (
                json.loads(model_scores_str)
                if isinstance(model_scores_str, str)
                else model_scores_str
            )
            if isinstance(scores, list) and len(scores) > 0:
                score_parts = []
                for s in scores:
                    model_name = s.get("model", "?")
                    # Shorten model names for display
                    short_name = (
                        model_name.split(":")[0].title()
                        if ":" in model_name
                        else model_name.title()
                    )
                    score_parts.append(
                        f"{short_name}: {s.get('verdict', '?')} ({s.get('confidence', '?')}%)"
                    )
                detail_parts.append(":robot_face: " + " | ".join(score_parts))
        except (json.JSONDecodeError, TypeError):
            pass

    # Links
    links = listing.get("links")
    if links:
        if isinstance(links, str):
            try:
                links = json.loads(links)
            except (json.JSONDecodeError, TypeError):
                links = []
        if links and isinstance(links, list):
            detail_parts.append(f":link: <{links[0]}|View listing>")

    # Recruiter info
    recruiter_name = listing.get("recruiter_name", "")
    if recruiter_name:
        detail_parts.append(f":bust_in_silhouette: Recruiter: {recruiter_name}")

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_text},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": line} for line in detail_parts],
        },
    ]

    # Job summary TL;DR
    job_summary = listing.get("job_summary", "")
    if job_summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":memo: *TL;DR:* {job_summary[:800]}"},
        })

    if reason:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {reason[:300]}"},
        })

    # Action buttons
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Save"},
            "action_id": f"save_{listing_id}",
            "value": listing_id,
            "style": "primary",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Pass"},
            "action_id": f"pass_{listing_id}",
            "value": listing_id,
            "style": "danger",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Escalate to Cloud LLM"},
            "action_id": f"escalate_{listing_id}",
            "value": listing_id,
        },
    ]
    blocks.append({"type": "actions", "elements": buttons})

    return {"color": color, "blocks": blocks}


def create_slack_app() -> object | None:
    """Create a Slack Bolt app with button handlers registered."""
    token, channel = _get_slack_config()
    if not token or not channel:
        return None

    try:
        app = _import_slack_app(token)
    except ImportError:
        logger.warning("slack-bolt not installed")
        return None

    import re as re_mod

    def _handle_save(ack, body, say):
        ack()
        listing_id = _extract_listing_id(body)
        if listing_id:
            _update_listing_status(listing_id, "saved")
            say(f":white_check_mark: Saved listing `{listing_id[:8]}...`")

    def _handle_pass(ack, body, say):
        ack()
        listing_id = _extract_listing_id(body)
        if listing_id:
            _update_listing_status(listing_id, "passed")
            say(f":file_folder: Passed on listing `{listing_id[:8]}...`")

    def _handle_escalate(ack, body, say):
        ack()
        listing_id = _extract_listing_id(body)
        if listing_id:
            from src.tailor import generate_application_assets
            say(f":cloud: Generating application assets for `{listing_id[:8]}...`")
            try:
                output_dir = generate_application_assets(listing_id)
                say(f":white_check_mark: Assets saved to `{output_dir}`")
            except Exception as e:
                logger.error("Tailor failed for %s", listing_id[:8], exc_info=True)
                say(f":x: Tailoring failed: {e}")

    app.action(re_mod.compile(r"^save_"))(_handle_save)
    app.action(re_mod.compile(r"^pass_"))(_handle_pass)
    app.action(re_mod.compile(r"^escalate_"))(_handle_escalate)

    return app


def _extract_listing_id(body: dict) -> str | None:
    """Extract listing ID from Slack action payload."""
    try:
        actions = body.get("actions", [])
        if actions:
            return actions[0].get("value")
    except (KeyError, IndexError):
        pass
    return None


def _update_listing_status(listing_id: str, status: str) -> None:
    """Update a listing's pipeline_status in the database."""
    from src.db import Database

    try:
        with Database() as db:
            updated = db.update_pipeline_status(listing_id, status)
            if updated:
                logger.info("Updated listing %s status to %s", listing_id[:8], status)
            else:
                logger.warning(
                    "Failed to update listing %s — not found or invalid status",
                    listing_id[:8],
                )
    except Exception:
        logger.error("Failed to update listing status", exc_info=True)
