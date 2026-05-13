"""Main pipeline orchestrator: fetch → classify → extract text → dedup → LLM triage → store."""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.db import Database, is_duplicate_email
from src.email_classifier import SKIP, classify_email
from src.email_fetcher import fetch_unread_emails
from src.profile_loader import load_profile
from src.text_extractor import extract_links, extract_text, get_html_body
from src.triage import TriageSession

logger = logging.getLogger(__name__)

DEBUG_DIR = Path("debug")


def _source_from_classification(classification: str) -> str:
    """Map email classification to a source label."""
    return {
        "JOB_DIGEST": "linkedin",
        "RECRUITER_OUTREACH": "recruiter",
        "GOOGLE_ALERT": "google_alerts",
    }.get(classification, "unknown")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run_pipeline() -> None:
    """Execute the full pipeline: fetch → classify → extract → dedup → LLM → store."""
    profile = load_profile()
    settings = profile["settings"]
    max_listings = settings.get("max_listings_per_run", 200)
    dedup_window = settings.get("dedup_window_days", 30)
    pass_window = settings.get("pass_window_days", 180)
    logger.info("Pipeline starting — profile loaded for %s", profile["name"])

    with Database() as db:
        messages = fetch_unread_emails()
        if not messages:
            logger.info("No new emails to process")
            return

        existing_texts = db.get_recent_email_texts(days=dedup_window)

        stats = {
            "fetched": 0, "skipped": 0, "deduped": 0, "processed": 0,
            "listings": 0, "yes": 0, "maybe": 0, "no": 0,
        }

        with TriageSession(profile["llm_context"]) as session:
            for msg in messages:
                stats["fetched"] += 1

                # Step 1: Classify
                classification = classify_email(msg)
                if classification == SKIP:
                    stats["skipped"] += 1
                    continue

                # Step 2: Extract text (generic, template-agnostic)
                html = get_html_body(msg)
                if not html:
                    logger.warning("No HTML body in email, skipping")
                    stats["skipped"] += 1
                    continue

                text = extract_text(html)
                links = extract_links(html)

                if not text or len(text) < 20:
                    logger.warning("Email text too short (%d chars), skipping", len(text))
                    stats["skipped"] += 1
                    continue

                # Step 3: Dedup at email level
                if is_duplicate_email(text, existing_texts):
                    logger.info("Duplicate email detected, skipping")
                    stats["deduped"] += 1
                    continue

                # Step 4: LLM — extract listings + match + score (one call)
                source = _source_from_classification(classification)
                try:
                    listings = session.triage_email(
                        text, links, classification, source,
                        duplicate_check=lambda t, c: db.is_duplicate_listing(
                            t, c, window_days=dedup_window
                        ),
                    )
                except Exception:
                    logger.error("Triage failed for email", exc_info=True)
                    _save_debug_email(msg, text, "triage_error")
                    continue

                if not listings:
                    logger.info("No listings found in %s email", classification)
                    _save_debug_email(msg, text, "no_listings")

                # Step 5: Store results (with listing-level dedup)
                for listing in listings:
                    if stats["listings"] >= max_listings:
                        logger.warning("Hit max_listings_per_run cap (%d)", max_listings)
                        break

                    was_update, _ = db.upsert_listing(
                        listing,
                        window_days=dedup_window,
                        pass_window_days=pass_window,
                    )
                    if was_update:
                        logger.info(
                            "Updated existing listing: '%s' at '%s'",
                            listing.title, listing.company,
                        )
                    else:
                        stats["listings"] += 1
                        verdict_key = listing.verdict.lower()
                        if verdict_key in stats:
                            stats[verdict_key] += 1

                # Record email as processed for future dedup
                text_hash = hashlib.sha256(text[:500].encode()).hexdigest()[:16]
                db.record_processed_email(text_hash, text[:500])
                existing_texts.append(text[:500])
                stats["processed"] += 1

        # --- Summary log ---
        logger.info("Pipeline run complete:")
        logger.info("  Emails fetched:   %d", stats["fetched"])
        logger.info("  Skipped:          %d", stats["skipped"])
        logger.info("  Deduped:          %d", stats["deduped"])
        logger.info("  Processed:        %d", stats["processed"])
        logger.info("  Listings stored:  %d", stats["listings"])
        logger.info(
            "  Verdicts:         YES=%d, MAYBE=%d, NO=%d",
            stats["yes"], stats["maybe"], stats["no"],
        )


def _save_debug_email(msg, text: str, reason: str) -> None:
    """Save email text to debug/ for inspection."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filepath = DEBUG_DIR / f"{reason}_{timestamp}.txt"
        subject = (msg.get("Subject", "") or "")[:60]
        sender = (msg.get("From", "") or "")[:60]
        header = f"Subject: {subject}\nFrom: {sender}\nReason: {reason}\n\n"
        filepath.write_text(header + text, encoding="utf-8")
        logger.info("Saved debug email to %s", filepath)
    except Exception:
        logger.error("Failed to save debug email", exc_info=True)


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="apply-pilot job search pipeline")
    parser.add_argument(
        "--dry-run",
        type=str,
        default=None,
        metavar="FILE",
        help="Path to a raw email .eml file to process (skips IMAP fetch)",
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("Dry-run mode not yet reimplemented for new architecture")
        return

    run_pipeline()


if __name__ == "__main__":
    main()
