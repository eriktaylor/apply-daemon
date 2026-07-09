"""Main pipeline orchestrator: fetch → classify → aggregate → select → triage → archive.

Track B is an aggregator + archiver of job-alert emails, not an email
reader: only confirmed job alerts from allowlisted senders are ever
processed, and the end-of-run archive pass is the only mailbox mutation.
Everything else — recruiter mail, social noise, personal email — is left
untouched and unread, ledger-recorded so it is never re-classified.

With ``top_n`` set in email_config.yaml, alert emails are parsed into
individual listing candidates, pooled, scored (tier + freshness + novelty),
probed for life, and only the top-N reach Stage 1 LLM triage. With top_n
blank, every alert email runs through email-level triage as before.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from src.db import Database, is_duplicate_email
from src.email_aggregator import parse_candidates, score_candidates, select_top_n
from src.email_classifier import (
    RECRUITER_OUTREACH,
    SKIP_CLASSIFICATIONS,
    SKIP_UNCLASSIFIED,
    classify_email,
)
from src.email_config import EmailConfigError, load_email_config
from src.email_fetcher import (
    FetchedEmail,
    archive_emails,
    fetch_inbox,
    sweep_stale_alerts,
)
from src.profile_loader import load_profile
from src.text_extractor import extract_links, extract_text, get_html_body
from src.triage import TriageSession, _scrape_url, get_confidence_threshold

logger = logging.getLogger(__name__)

DEBUG_DIR = Path("debug")


def _autopilot_enabled() -> bool:
    import os
    return os.getenv("AUTOPILOT_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _source_from_classification(classification: str) -> str:
    """Fallback source label when the sender isn't in the allowlist config."""
    return {
        "JOB_DIGEST": "linkedin",
        "GOOGLE_ALERT": "google_alerts",
    }.get(classification, "unknown")


def _email_age_days(msg) -> float | None:
    """Age of the email in days from its Date header, or None if unparseable."""
    try:
        sent = parsedate_to_datetime(msg.get("Date", "") or "")
        if sent is None:
            return None
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - sent).total_seconds() / 86400)
    except (TypeError, ValueError):
        return None


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run_pipeline() -> None:
    """Execute the full Track B run. See module docstring for the flow."""
    profile = load_profile()
    settings = profile["settings"]
    max_listings = settings.get("max_listings_per_run", 200)
    dedup_window = settings.get("dedup_window_days", 30)
    pass_window = settings.get("pass_window_days", 180)
    logger.info("Pipeline starting — profile loaded for %s", profile["name"])

    try:
        email_cfg = load_email_config()
    except EmailConfigError:
        logger.error(
            "email_config.yaml is invalid — refusing to run Track B", exc_info=True
        )
        return
    aggregate = email_cfg.top_n is not None
    logger.info(
        "Email config: %s (%d senders, top_n=%s, lookback=%dd)",
        email_cfg.origin, len(email_cfg.all_addresses()),
        email_cfg.top_n, email_cfg.lookback_days,
    )

    with Database() as db:
        fetched = fetch_inbox(config=email_cfg, already_seen=db.is_email_id_seen)
        if not fetched:
            logger.info("No new emails to process")
            _sweep_stale(db, email_cfg)
            return

        existing_texts = db.get_recent_email_texts(days=dedup_window)

        stats = {
            "fetched": 0, "ledger_skipped": 0, "recruiter_skipped": 0,
            "skipped": 0, "deduped": 0, "stale_archived": 0,
            "candidates": 0, "selected": 0, "probe_dropped": 0,
            "parse_fallback": 0, "processed": 0, "listings": 0,
            "yes": 0, "maybe": 0, "no": 0, "auto_queued": 0, "archived": 0,
        }

        autopilot_on = _autopilot_enabled()
        autopilot_cutoff = int(round(get_confidence_threshold() * 100))

        pool = []
        pooled_emails: list[tuple[FetchedEmail, str, str]] = []  # (item, text, classification)
        to_archive: list[bytes] = []

        def duplicate_check(title: str, company: str) -> bool:
            return db.is_duplicate_listing(title, company, window_days=dedup_window)

        def store_listings(listings) -> None:
            """Step 5: upsert with listing-level dedup + autopilot queueing."""
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
                    if (
                        autopilot_on
                        and listing.verdict in ("YES", "MAYBE")
                        and listing.confidence >= autopilot_cutoff
                    ):
                        if db.mark_auto_queued(listing.id):
                            stats["auto_queued"] += 1

        def ledger_and_archive(item: FetchedEmail, text: str, classification: str) -> None:
            """Record a processed alert email and queue it for the archive pass."""
            text_hash = hashlib.sha256(text[:500].encode()).hexdigest()[:16]
            db.record_processed_email(
                text_hash, text[:500],
                gmail_message_id=item.gmail_message_id or None,
                classification=classification,
            )
            existing_texts.append(text[:500])
            to_archive.append(item.uid)
            stats["processed"] += 1

        with TriageSession(profile["llm_context"]) as session:
            for item in fetched:
                msg = item.message
                stats["fetched"] += 1
                message_id = item.gmail_message_id

                # Step 0: ledger — never re-classify what we've already seen.
                if message_id and db.is_email_id_seen(message_id):
                    stats["ledger_skipped"] += 1
                    continue

                # Step 1: classify (allowlist-gated)
                classification = classify_email(msg, config=email_cfg)

                if classification == RECRUITER_OUTREACH:
                    # Handled offline by the user — untouched, unread.
                    _record_in_ledger(db, message_id, "SKIP_RECRUITER")
                    stats["recruiter_skipped"] += 1
                    continue

                if classification in SKIP_CLASSIFICATIONS:
                    _record_in_ledger(db, message_id, classification)
                    stats["skipped"] += 1
                    continue

                # --- Confirmed job alert from an allowlisted sender ---
                sender = (msg.get("From", "") or "").lower()
                source = (
                    email_cfg.source_for(sender)
                    or _source_from_classification(classification)
                )
                age_days = _email_age_days(msg)

                # Stale alerts: archive without spending Stage 1 tokens.
                if age_days is not None and age_days > email_cfg.lookback_days:
                    _record_in_ledger(db, message_id, "ARCHIVED_STALE")
                    to_archive.append(item.uid)
                    stats["stale_archived"] += 1
                    continue

                # Step 2: extract text (generic, template-agnostic)
                html = get_html_body(msg)
                if not html:
                    logger.warning("No HTML body in alert email, skipping")
                    _record_in_ledger(db, message_id, SKIP_UNCLASSIFIED)
                    stats["skipped"] += 1
                    continue

                text = extract_text(html)
                links = extract_links(html)

                if not text or len(text) < 20:
                    logger.warning("Email text too short (%d chars), skipping", len(text))
                    _record_in_ledger(db, message_id, SKIP_UNCLASSIFIED)
                    stats["skipped"] += 1
                    continue

                # Step 3: dedup at email level
                if is_duplicate_email(text, existing_texts):
                    logger.info("Duplicate email detected, skipping")
                    _record_in_ledger(db, message_id, "DUPLICATE")
                    to_archive.append(item.uid)
                    stats["deduped"] += 1
                    continue

                # Step 4a: aggregate — parse listing candidates into the pool.
                if aggregate:
                    candidates = parse_candidates(
                        html,
                        source=source,
                        tier=email_cfg.tier_for(sender) or "ok",
                        email_uid=item.uid,
                        gmail_message_id=message_id,
                        email_age_days=age_days or 0.0,
                    )
                    if candidates:
                        pool.extend(candidates)
                        pooled_emails.append((item, text, classification))
                        continue
                    # Structural parse found nothing — email-level fallback so
                    # parser fragility never loses alerts.
                    stats["parse_fallback"] += 1
                    _triage_email_level(
                        session, db, item, msg, text, links, classification, source,
                        duplicate_check, store_listings, ledger_and_archive,
                        ledger_classification="PARSE_FALLBACK",
                    )
                    continue

                # Step 4b: no aggregation (top_n blank) — email-level triage.
                _triage_email_level(
                    session, db, item, msg, text, links, classification, source,
                    duplicate_check, store_listings, ledger_and_archive,
                    ledger_classification=classification,
                )

            # Step 5: staged selection over the pooled candidates.
            if aggregate and pool:
                stats["candidates"] = len(pool)
                score_candidates(pool, db.get_recent_titles(days=dedup_window))
                selected, probe_dropped = select_top_n(pool, email_cfg.top_n)
                stats["selected"] = len(selected)
                stats["probe_dropped"] = probe_dropped

                for cand in selected:
                    if stats["listings"] >= max_listings:
                        break
                    page_text = _scrape_url(cand.url) or ""
                    cand_text = page_text or f"{cand.title}\n{cand.snippet}"
                    try:
                        listings = session.triage_email(
                            cand_text, [cand.url], "JOB_DIGEST", cand.source,
                            duplicate_check=duplicate_check,
                        )
                    except Exception:
                        logger.error(
                            "Triage failed for candidate '%s'", cand.title[:60],
                            exc_info=True,
                        )
                        continue
                    if not listings:
                        logger.info("No listing survived for candidate '%s'", cand.title[:60])
                    store_listings(listings)

                # Pool flush: ledger + archive the emails whose candidates
                # entered the pool, selected or not — no backlog carryover.
                for item, text, classification in pooled_emails:
                    ledger_and_archive(item, text, classification)

        # Archive pass — the run's ONLY mailbox mutation. Failure is
        # non-fatal: the ledger already prevents re-processing.
        if to_archive:
            try:
                stats["archived"] = archive_emails(to_archive, email_cfg.archive_folder)
            except Exception:
                logger.error(
                    "Archive pass failed — emails remain in inbox", exc_info=True
                )

        stats["stale_swept"] = _sweep_stale(db, email_cfg)

        # --- Summary log ---
        logger.info("Pipeline run complete:")
        logger.info("  Emails fetched:   %d", stats["fetched"])
        logger.info("  Ledger-skipped:   %d", stats["ledger_skipped"])
        logger.info("  Recruiter (skip): %d", stats["recruiter_skipped"])
        logger.info("  Skipped:          %d", stats["skipped"])
        logger.info("  Deduped:          %d", stats["deduped"])
        logger.info("  Stale archived:   %d", stats["stale_archived"])
        logger.info("  Stale swept:      %d", stats.get("stale_swept", 0))
        if aggregate:
            logger.info(
                "  Pool:             %d candidate(s) → %d selected (%d probe-dropped, "
                "%d parse-fallback)",
                stats["candidates"], stats["selected"],
                stats["probe_dropped"], stats["parse_fallback"],
            )
        logger.info("  Processed:        %d", stats["processed"])
        logger.info("  Archived:         %d", stats["archived"])
        logger.info("  Listings stored:  %d", stats["listings"])
        logger.info(
            "  Verdicts:         YES=%d, MAYBE=%d, NO=%d",
            stats["yes"], stats["maybe"], stats["no"],
        )
        if autopilot_on:
            logger.info("  Autopilot queued: %d", stats["auto_queued"])


def _triage_email_level(
    session, db, item, msg, text, links, classification, source,
    duplicate_check, store_listings, ledger_and_archive,
    ledger_classification: str,
) -> None:
    """Email-level Stage 1 → 5 (the pre-aggregator path, kept for top_n=blank
    and for alerts whose structural parse yields no candidates)."""
    try:
        listings = session.triage_email(
            text, links, classification, source, duplicate_check=duplicate_check
        )
    except Exception:
        # Not ledgered: transient LLM failures retry on the next run.
        logger.error("Triage failed for email", exc_info=True)
        _save_debug_email(msg, text, "triage_error")
        return

    if not listings:
        logger.info("No listings found in %s email", classification)
        _save_debug_email(msg, text, "no_listings")

    store_listings(listings)
    ledger_and_archive(item, text, ledger_classification)


def _sweep_stale(db: Database, email_cfg) -> int:
    """Archive allowlisted alert mail older than the lookback window.

    Runs every pipeline run (even when the fetch finds nothing new) —
    old alerts never enter the fetch, so this is the only path that
    clears them out. They are archived unprocessed (ARCHIVED_STALE),
    matching the policy for in-window stale mail. Failure is non-fatal.
    """
    try:
        swept = sweep_stale_alerts(
            email_cfg,
            ledger=lambda mid: (
                None if db.is_email_id_seen(mid)
                else _record_in_ledger(db, mid, "ARCHIVED_STALE")
            ),
        )
    except Exception:
        logger.error("Stale sweep failed — old alerts remain in inbox", exc_info=True)
        return 0
    return swept


def _record_in_ledger(db: Database, message_id: str, classification: str) -> None:
    """Ledger a skipped/duplicate email so it is never re-classified.

    No email text is stored — the hash is derived from the Message-ID and the
    preview stays empty, so these rows never participate in text-level dedup
    (get_recent_email_texts filters empty previews).
    """
    if not message_id:
        return
    id_hash = hashlib.sha256(message_id.encode()).hexdigest()[:16]
    db.record_processed_email(
        id_hash, "", gmail_message_id=message_id, classification=classification
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

    parser = argparse.ArgumentParser(description="apply-daemon job search pipeline")
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
