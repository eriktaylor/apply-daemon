"""Daily Slack digest — posts a summary of triaged/saved listings.

Intended to run via cron at 8:00 AM:
    0 8 * * * cd /path/to/apply-daemon && python -m src.digest

Queries the DB for all listings where pipeline_status is 'triaged' or 'saved'
from the last 14 days, sorted by confidence descending.
"""

from __future__ import annotations

import json
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from src.db import Database
from src.geo import get_distance
from src.notifications import _get_slack_config, _import_slack_app

logger = logging.getLogger(__name__)


def build_digest_blocks(listings: list[dict]) -> list[dict]:
    """Build Block Kit blocks for the daily digest header."""
    total = len(listings)
    yes_count = sum(1 for listing in listings if listing.get("verdict") == "YES")
    maybe_count = sum(1 for listing in listings if listing.get("verdict") == "MAYBE")
    saved_count = sum(1 for listing in listings if listing.get("pipeline_status") == "saved")
    triaged_count = sum(1 for listing in listings if listing.get("pipeline_status") == "triaged")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Daily Job Digest"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":newspaper: *{total}* listings awaiting review\n"
                    f":white_check_mark: *{yes_count}* YES  |  "
                    f":grey_question: *{maybe_count}* MAYBE\n"
                    f":new: *{triaged_count}* new  |  "
                    f":floppy_disk: *{saved_count}* saved"
                ),
            },
        },
        {"type": "divider"},
    ]
    return blocks


def build_digest_listing_attachment(listing: dict, history: str = "") -> dict:
    """Build a digest listing attachment with reaction legend and optional history.

    Args:
        listing: Dict with listing data.
        history: Pre-formatted timeline string from db.get_listing_history().
            Empty string means no prior encounters.
    """
    listing_id = listing.get("id", "")
    title = listing.get("title", "Unknown")
    company = listing.get("company", "Unknown")
    location = listing.get("location", "")
    salary = listing.get("salary", "")
    confidence = listing.get("confidence", 0)
    verdict = listing.get("verdict", "")
    job_summary = listing.get("job_summary", "")
    pipeline_status = listing.get("pipeline_status", "triaged")

    # Color by verdict + confidence. AUTO_MATCH cutoff = max(CONFIDENCE_THRESHOLD, 0.8).
    from src.triage import auto_match_cutoff, get_confidence_threshold
    auto_match_pct = auto_match_cutoff(get_confidence_threshold())
    if verdict == "YES" and confidence >= auto_match_pct:
        color = "#2eb67d"  # Green
    elif verdict == "YES":
        color = "#ecb22e"  # Yellow — low confidence YES
    elif verdict == "MAYBE":
        color = "#36c5f0"  # Blue
    else:
        color = "#ddd"

    # Header with geo distance
    header_text = f"*{title}* — {company}"

    # Add first job link if available
    links_raw = listing.get("links", "")
    links = []
    if links_raw:
        try:
            links = json.loads(links_raw) if isinstance(links_raw, str) else links_raw
        except (json.JSONDecodeError, TypeError):
            links = []
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

    # Status line
    status_icon = ":floppy_disk: Saved" if pipeline_status == "saved" else ":new: New"
    detail_parts = [
        f"{status_icon}  |  {verdict} ({confidence}%)",
    ]
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
            if isinstance(scores, list) and len(scores) > 1:
                parts = []
                for s in scores:
                    name = s.get("model", "?")
                    short = name.split(":")[0].title() if ":" in name else name.title()
                    parts.append(f"{short}: {s.get('verdict', '?')} ({s.get('confidence', '?')}%)")
                detail_parts.append(":robot_face: " + " | ".join(parts))
        except (json.JSONDecodeError, TypeError):
            pass

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": d} for d in detail_parts]},
    ]

    # Job summary TL;DR
    if job_summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":memo: *TL;DR:* {job_summary[:800]}"},
        })

    # Skills match
    skills_extracted = listing.get("skills_extracted", False)
    # DB stores as integer 0/1
    if isinstance(skills_extracted, int):
        skills_extracted = bool(skills_extracted)

    if not skills_extracted:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":dart: *Skills Match:* N/A (Not specified in listing)",
            },
        })
    else:
        matching_raw = listing.get("matching_skills", "")
        missing_raw = listing.get("missing_skills", "")
        matching = json.loads(matching_raw) if matching_raw else []
        missing = json.loads(missing_raw) if missing_raw else []
        total = len(matching) + len(missing)
        if total > 0:
            pct = round(len(matching) / total * 100)
            skills_text = f":dart: *Skills Match:* {pct}% ({len(matching)}/{total})"
            parts = []
            if matching:
                parts.append(f":white_check_mark: *Matching:* {', '.join(matching)}")
            if missing:
                parts.append(f":x: *Gaps:* {', '.join(missing)}")
            skills_text += "\n" + "  |  ".join(parts)
        else:
            skills_text = ":dart: *Skills Match:* 100% (0/0)"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": skills_text},
        })

    # Historical context (if any prior encounters)
    if history:
        # Count encounters by counting backtick-wrapped statuses
        encounter_count = history.count("`") // 2  # each entry has opening+closing backtick
        if encounter_count == 1:
            history_text = f":clock1: *History (1 prior):* {history}"
        else:
            history_text = f":clock1: *History (Seen {encounter_count} times):* {history}"
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": history_text}],
        })

    # Footer with reaction legend
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"React: :thumbsup: Save  |  :thumbsdown: Pass / Expire  |  "
                    f":pencil2: Tailor  |  :grey_question: Smart Router  •  `{listing_id}`"
                ),
            },
        ],
    })

    return {"color": color, "blocks": blocks}


def post_digest() -> bool:
    """Query DB and post the daily digest to Slack.

    Returns True if posted successfully.
    """
    token, channel = _get_slack_config()
    if not token or not channel:
        logger.warning("Slack not configured, cannot post digest")
        return False

    try:
        app = _import_slack_app(token)
    except ImportError:
        logger.warning("slack-bolt not installed. Run: pip install slack-bolt")
        return False

    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
    rate_limit_handler = RateLimitErrorRetryHandler(max_retry_count=3)
    app.client.retry_handlers.append(rate_limit_handler)

    with Database() as db:
        rows = db.get_digest_listings(days=14)

        if not rows:
            logger.info("No listings for digest — nothing to post")
            return True

        listings = [dict(r) for r in rows]
        logger.info("Building digest for %d listings", len(listings))

        try:
            # Post header
            header_blocks = build_digest_blocks(listings)
            app.client.chat_postMessage(
                channel=channel,
                text=f"Daily digest: {len(listings)} listings awaiting review",
                blocks=header_blocks,
            )

            # Post each listing as a color-coded attachment with job_id metadata
            for listing in listings:
                history = db.get_listing_history(
                    listing.get("title", ""),
                    listing.get("company", ""),
                    listing.get("id", ""),
                )
                attachment = build_digest_listing_attachment(listing, history=history)
                listing_id = listing.get("id", "")
                metadata = {
                    "event_type": "apply_daemon_listing",
                    "event_payload": {"job_id": listing_id},
                }
                app.client.chat_postMessage(
                    channel=channel,
                    text=(
                        f"{listing.get('verdict', '?')}: {listing.get('title', '')} "
                        f"at {listing.get('company', '')}"
                    ),
                    attachments=[attachment],
                    metadata=metadata,
                )
                db.mark_slack_notified(listing_id)
                time.sleep(1.5)

            logger.info("Posted daily digest to Slack (%d listings)", len(listings))
            return True

        except Exception as exc:
            error_str = str(exc)
            if "not_in_channel" in error_str:
                logger.warning(
                    "\n"
                    "╔══════════════════════════════════════════════════════════════╗\n"
                    "║  SLACK ERROR: Bot is not in the channel.                    ║\n"
                    "║  Please type /invite @YourBotName in the Slack channel.     ║\n"
                    "╚══════════════════════════════════════════════════════════════╝"
                )
            else:
                logger.error("Failed to post digest", exc_info=True)
            return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    post_digest()


if __name__ == "__main__":
    main()
