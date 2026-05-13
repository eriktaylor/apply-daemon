"""Scheduled batch processor — runs concurrent OpenRouter tailor requests.

Designed to run daily at 5:00 PM:
    0 17 * * * cd /path/to/apply-pilot && .venv/bin/python -m src.batch_process

Execution:
  Phase A (Housekeeping): Revert stuck batches and expire stale saved listings.
  Phase B (Submit):       Gather saved listings and tailor them concurrently via
                          OpenRouter. All requests complete before this process exits.

Usage:
    python -m src.batch_process
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

load_dotenv()

from pathlib import Path

from src.db import Database
from src.profile_loader import load_profile
from src.tailor import submit_batch

logger = logging.getLogger(__name__)


def run_batch_process() -> dict:
    """Execute both phases of the batch processor.

    Returns:
        Summary dict with counts.
    """
    summary = {"submitted": 0, "reverted": 0, "expired": 0}

    # --- Phase A: Housekeeping ---
    with Database() as db:
        reverted = db.revert_stuck_batches(max_age_hours=48)
        if reverted:
            logger.info("Phase A: reverted %d stuck batch job(s) back to saved", reverted)
        summary["reverted"] = reverted

        expired = db.expire_stale_saved(max_age_days=7)
        if expired:
            logger.info("Phase A: expired %d stale saved listing(s) older than 7 days", expired)
        summary["expired"] = expired

    # --- Phase B: Tailor saved listings concurrently via OpenRouter ---
    profile = load_profile()
    settings = profile["settings"]
    batch_days = settings.get("batch_process_days")
    if isinstance(batch_days, str):
        batch_days = int(batch_days) if batch_days.strip() else None

    with Database() as db:
        saved_rows = db.get_saved_listings(max_age_days=batch_days)

    if saved_rows:
        job_ids = [row["id"] for row in saved_rows]
        logger.info("Phase B: tailoring %d saved listing(s) via OpenRouter", len(job_ids))
        try:
            batch_id = submit_batch(job_ids)
            summary["submitted"] = len(job_ids)
            logger.info("Batch complete: %s (%d jobs)", batch_id, len(job_ids))
            _notify_batch_completions(batch_id)
        except Exception:
            logger.error("Failed to run batch tailor", exc_info=True)
    else:
        logger.info("Phase B: no saved listings to submit")

    logger.info(
        "Batch process complete: %d submitted, %d reverted, %d expired",
        summary["submitted"], summary["reverted"], summary["expired"],
    )
    return summary


def _notify_batch_completions(batch_id: str) -> None:
    """Post diff summaries and interview prep guides for batch-completed listings.

    Batch path has no parent Slack message ts, so these are standalone messages.
    """
    try:
        from src.notifications import _get_slack_config, _import_slack_app
    except ImportError:
        return

    token, channel = _get_slack_config()
    if not token or not channel:
        return

    output_dir = Path("output")
    if not output_dir.exists():
        return

    with Database() as db:
        rows = db.conn.execute(
            "SELECT id, company FROM listings WHERE batch_id = ? AND pipeline_status = 'tailored'",
            (batch_id,),
        ).fetchall()

    if not rows:
        return

    try:
        app = _import_slack_app(token)
    except ImportError:
        return

    import json as _json

    from src.sweeper import _format_diff_text

    for row in rows:
        job_id = row["id"]
        company = row["company"]

        # Find the output subdirectory for this job
        job_dirs = list(output_dir.glob(f"*{job_id[:8]}*"))
        if not job_dirs:
            continue
        job_dir = job_dirs[0]

        # Post diff summary
        assets_path = job_dir / "assets.json"
        if assets_path.exists():
            try:
                assets_json = _json.loads(assets_path.read_text(encoding="utf-8"))
                diff_text = _format_diff_text(assets_json)
                if diff_text:
                    if len(diff_text) > 2900:
                        diff_text = diff_text[:2900] + "\n\n_(truncated)_"
                    app.client.chat_postMessage(
                        channel=channel,
                        text=f"Diff Summary: {company}",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":pencil2: *Tailoring Diff Summary — {company}*",
                                },
                            },
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": diff_text},
                            },
                        ],
                    )
                    logger.info("Posted batch diff summary for %s", company)
            except Exception:
                logger.error("Failed to post diff summary for %s", company, exc_info=True)

        # Post interview prep guide
        for prep_path in job_dir.glob("Interview_Prep_*.md"):
            try:
                prep_text = prep_path.read_text(encoding="utf-8")
                if not prep_text.strip():
                    continue
                if len(prep_text) > 2900:
                    prep_text = prep_text[:2900] + "\n\n_(truncated — see full file)_"
                app.client.chat_postMessage(
                    channel=channel,
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
                logger.info("Posted batch interview prep for %s", company)
            except Exception:
                logger.error("Failed to post interview prep for %s", company, exc_info=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    run_batch_process()


if __name__ == "__main__":
    main()
