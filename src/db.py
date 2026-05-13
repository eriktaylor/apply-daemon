"""SQLite schema and access layer for the listings database."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from rapidfuzz.fuzz import token_set_ratio

from src.models import JobListing

DEFAULT_DB_PATH = Path("apply_pilot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    email_classification TEXT,
    title               TEXT NOT NULL,
    company             TEXT NOT NULL,
    location            TEXT,
    salary              TEXT,
    job_summary         TEXT,
    verdict             TEXT,
    confidence          INTEGER DEFAULT 0,
    reason              TEXT,
    links               TEXT,
    recruiter_name      TEXT,
    recruiter_title     TEXT,
    raw_email_text      TEXT,
    model_used          TEXT,
    model_scores        TEXT,
    skills_extracted    INTEGER DEFAULT 0,
    matching_skills      TEXT,
    missing_skills      TEXT,
    tokens_used         INTEGER,
    latency_ms          INTEGER,
    date_ingested       TEXT NOT NULL,
    pipeline_status     TEXT DEFAULT 'triaged',
    batch_id            TEXT,
    slack_notified      INTEGER DEFAULT 0,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verdict ON listings(verdict);
CREATE INDEX IF NOT EXISTS idx_pipeline_status ON listings(pipeline_status);
CREATE INDEX IF NOT EXISTS idx_date_ingested ON listings(date_ingested);
CREATE INDEX IF NOT EXISTS idx_title_company ON listings(title, company);

CREATE TABLE IF NOT EXISTS processed_emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email_text_hash TEXT NOT NULL,
    text_preview    TEXT,
    date_processed  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_email_text_hash ON processed_emails(email_text_hash);
"""


class Database:
    """SQLite access layer for the listings pipeline."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # --- Listings ---

    def insert_listing(self, listing: JobListing) -> None:
        """Insert a new listing into the database."""
        now = datetime.now(timezone.utc).isoformat()
        links_json = json.dumps(listing.links) if listing.links else None
        self.conn.execute(
            """
            INSERT OR IGNORE INTO listings (
                id, source, email_classification, title, company, location,
                salary, job_summary, verdict, confidence, reason, links,
                recruiter_name, recruiter_title, raw_email_text,
                model_used, model_scores, skills_extracted, matching_skills,
                missing_skills, tokens_used, latency_ms,
                date_ingested, pipeline_status, slack_notified, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.id,
                listing.source,
                listing.email_classification,
                listing.title,
                listing.company,
                listing.location,
                listing.salary,
                listing.job_summary or None,
                listing.verdict,
                listing.confidence,
                listing.reason,
                links_json,
                listing.recruiter_name,
                listing.recruiter_title,
                listing.raw_email_text,
                listing.model_used,
                listing.model_scores or None,
                int(listing.skills_extracted),
                listing.matching_skills or None,
                listing.missing_skills or None,
                listing.tokens_used,
                listing.latency_ms,
                listing.date_ingested,
                listing.final_status,  # maps to pipeline_status column
                0,  # slack_notified default
                now,
            ),
        )
        self.conn.commit()

    def upsert_listing(
        self,
        listing: JobListing,
        window_days: int = 30,
        threshold: float = 85.0,
        pass_window_days: int = 180,
    ) -> tuple[bool, str | None]:
        """Insert a listing, or update an existing fuzzy-match if found.

        Smart Upsert: if a listing with a similar title+company exists within
        the dedup window, overwrites data fields (description, skills, score,
        url) but preserves pipeline_status and slack_notified.

        Status-aware dedup behavior:
          - ``passed`` or ``expired`` within ``pass_window_days`` → blocked,
            return (True, existing_id) without updating data fields.
          - ``passed`` or ``expired`` older than ``pass_window_days`` → fresh
            INSERT (long-cooldown reset; listing may resurface in digest).
          - All other statuses → standard UPDATE preserving pipeline_status.

        Args:
            listing: The new listing to insert or merge.
            window_days: How far back to check for duplicates (non-pass statuses).
            threshold: Fuzzy match threshold (0-100).
            pass_window_days: How long passed/expired listings stay blocked.

        Returns:
            (was_update, existing_id) — True + the existing row's ID if an
            UPDATE was performed, False + None for a fresh INSERT.
        """
        now = datetime.now(timezone.utc).isoformat()
        incoming_title = (listing.title or "").strip().lower()
        incoming_company = (listing.company or "").strip().lower()

        # Search across the wider pass_window so we can also catch pass/expired rows.
        search_window = max(window_days, pass_window_days)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=search_window)).isoformat()
        pass_cutoff = (datetime.now(timezone.utc) - timedelta(days=pass_window_days)).isoformat()
        active_cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

        rows = self.conn.execute(
            "SELECT id, title, company, pipeline_status, date_ingested "
            "FROM listings WHERE date_ingested >= ?",
            (cutoff,),
        ).fetchall()

        matched_id = None
        matched_status = None
        matched_date = None
        for row in rows:
            existing_title = (row["title"] or "").strip().lower()
            existing_company = (row["company"] or "").strip().lower()
            title_score = token_set_ratio(incoming_title, existing_title)
            company_score = token_set_ratio(incoming_company, existing_company)
            if title_score >= threshold and company_score >= threshold:
                matched_id = row["id"]
                matched_status = row["pipeline_status"]
                matched_date = row["date_ingested"]
                break

        # Status-aware decision for a matched row
        if matched_id and matched_status in ("passed", "expired"):
            if matched_date >= pass_cutoff:
                # Still within the pass/expire cooldown — block silently
                return True, matched_id
            else:
                # Cooldown expired — fall through to fresh INSERT below
                matched_id = None

        if matched_id and matched_status not in ("passed", "expired"):
            # Only consider non-pass/expire matches within the standard window
            if matched_date < active_cutoff:
                matched_id = None

        links_json = json.dumps(listing.links) if listing.links else None

        if matched_id:
            # UPDATE — overwrite data fields but preserve status and slack_notified
            self.conn.execute(
                """
                UPDATE listings SET
                    title = ?, company = ?, location = ?, salary = ?,
                    job_summary = ?, verdict = ?, confidence = ?, reason = ?,
                    links = ?, raw_email_text = ?, model_used = ?, model_scores = ?,
                    skills_extracted = ?, matching_skills = ?, missing_skills = ?,
                    tokens_used = ?, latency_ms = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    listing.title, listing.company, listing.location, listing.salary,
                    listing.job_summary or None, listing.verdict, listing.confidence,
                    listing.reason, links_json, listing.raw_email_text,
                    listing.model_used, listing.model_scores or None,
                    int(listing.skills_extracted), listing.matching_skills or None,
                    listing.missing_skills or None, listing.tokens_used,
                    listing.latency_ms, now, matched_id,
                ),
            )
            self.conn.commit()
            return True, matched_id

        # INSERT — new listing
        self.insert_listing(listing)
        return False, None

    def is_duplicate_listing(
        self, title: str, company: str, window_days: int = 30, threshold: float = 85.0
    ) -> bool:
        """Check if a similar listing exists within the dedup window using fuzzy matching.

        Uses rapidfuzz token_set_ratio to compare incoming title and company
        against recent entries. Both must score above threshold to be a duplicate.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        rows = self.conn.execute(
            """
            SELECT title, company FROM listings
            WHERE date_ingested >= ?
            """,
            (cutoff,),
        ).fetchall()

        incoming_title = (title or "").strip().lower()
        incoming_company = (company or "").strip().lower()

        for row in rows:
            existing_title = (row["title"] or "").strip().lower()
            existing_company = (row["company"] or "").strip().lower()
            title_score = token_set_ratio(incoming_title, existing_title)
            company_score = token_set_ratio(incoming_company, existing_company)
            if title_score >= threshold and company_score >= threshold:
                return True

        return False

    def get_recent_email_texts(self, days: int = 30) -> list[str]:
        """Get text previews of recently processed emails for dedup."""
        rows = self.conn.execute(
            """
            SELECT text_preview FROM processed_emails
            WHERE date_processed >= datetime('now', ?)
            ORDER BY date_processed DESC
            """,
            (f"-{days} days",),
        ).fetchall()
        return [row["text_preview"] for row in rows if row["text_preview"]]

    def record_processed_email(self, text_hash: str, text_preview: str) -> None:
        """Record that an email has been processed (for email-level dedup)."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO processed_emails (email_text_hash, text_preview, date_processed)
            VALUES (?, ?, ?)
            """,
            (text_hash, text_preview[:500], now),
        )
        self.conn.commit()

    def get_listings_by_verdict(self, verdict: str) -> list[sqlite3.Row]:
        """Get all listings with a given verdict."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE verdict = ? ORDER BY date_ingested DESC",
            (verdict,),
        ).fetchall()

    def get_recent_listings(self, hours: int = 1) -> list[sqlite3.Row]:
        """Get listings ingested within the last N hours."""
        return self.conn.execute(
            """
            SELECT * FROM listings
            WHERE date_ingested >= datetime('now', ?)
            ORDER BY date_ingested DESC
            """,
            (f"-{hours} hours",),
        ).fetchall()

    # --- State machine ---

    VALID_STATUSES = {
        "triaged", "saved", "passed", "processing_batch", "tailored", "applied",
        "rejected", "interviewing",
        "expired", "failed_api", "failed_compilation",
    }

    def update_pipeline_status(self, listing_id: str, status: str) -> bool:
        """Transition a listing to a new pipeline status.

        Returns True if the row was updated, False if the listing was not found
        or the status is invalid.
        """
        if status not in self.VALID_STATUSES:
            return False
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE listings SET pipeline_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, now, listing_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_listing_by_id(self, listing_id: str) -> sqlite3.Row | None:
        """Fetch a single listing by its ID."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()

    def get_digest_listings(self, days: int = 14) -> list[sqlite3.Row]:
        """Get listings for the daily digest (triaged or saved, last N days).

        Sorted by confidence descending so highest-scoring listings come first.
        Only returns listings that have NOT yet been posted to Slack.

        Defense in depth: any active-status row with verdict='NO' is flipped to
        ``pipeline_status='passed'`` before the query so it cannot surface in
        the digest — a NO is a NO regardless of how it reached the DB.
        """
        self.conn.execute(
            """
            UPDATE listings
            SET pipeline_status = 'passed',
                updated_at = ?
            WHERE verdict = 'NO'
              AND pipeline_status IN ('triaged', 'saved')
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()
        return self.conn.execute(
            """
            SELECT * FROM listings
            WHERE pipeline_status IN ('triaged', 'saved')
            AND slack_notified = 0
            AND date_ingested >= datetime('now', ?)
            ORDER BY confidence DESC, date_ingested DESC
            """,
            (f"-{days} days",),
        ).fetchall()

    def mark_slack_notified(self, listing_id: str) -> bool:
        """Mark a listing as having been posted to Slack.

        Returns True if the row was updated.
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "UPDATE listings SET slack_notified = 1, updated_at = ? WHERE id = ?",
            (now, listing_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    # --- Historical context ---

    # Map pipeline_status to user-friendly display labels
    _STATUS_DISPLAY = {
        "triaged": "ignored",
        "saved": "saved",
        "passed": "passed",
        "processing_batch": "processing",
        "tailored": "tailored",
        "applied": "applied",
        "rejected": "rejected",
        "interviewing": "interviewing",
        "expired": "expired",
        "failed_api": "failed",
        "failed_compilation": "failed",
    }

    def get_funnel_counts(self, max_age_days: int | None = None) -> dict[str, int]:
        """Count listings grouped by pipeline_status.

        Args:
            max_age_days: If set, only count listings ingested within the past N days.
                If None, count all listings (all-time).

        Returns:
            Dict mapping pipeline_status → count.
        """
        if max_age_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            rows = self.conn.execute(
                "SELECT pipeline_status, COUNT(*) as cnt FROM listings "
                "WHERE date_ingested >= ? GROUP BY pipeline_status",
                (cutoff,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT pipeline_status, COUNT(*) as cnt FROM listings "
                "GROUP BY pipeline_status",
            ).fetchall()
        return {row["pipeline_status"]: row["cnt"] for row in rows}

    def get_listing_history(
        self, title: str, company: str, current_job_id: str, threshold: float = 85.0
    ) -> str:
        """Find all historical listings matching this title+company via fuzzy matching.

        Excludes the current listing. Returns a formatted timeline string, or
        empty string if no prior encounters exist.

        Examples:
            "`passed` (Oct 12)"
            "`passed` (Oct 12) ➔ `saved` (Nov 15) ➔ `expired` (Dec 01)"
        """
        rows = self.conn.execute(
            "SELECT id, title, company, pipeline_status, date_ingested "
            "FROM listings ORDER BY date_ingested ASC",
        ).fetchall()

        incoming_title = (title or "").strip().lower()
        incoming_company = (company or "").strip().lower()

        matches = []
        for row in rows:
            if row["id"] == current_job_id:
                continue
            existing_title = (row["title"] or "").strip().lower()
            existing_company = (row["company"] or "").strip().lower()
            title_score = token_set_ratio(incoming_title, existing_title)
            company_score = token_set_ratio(incoming_company, existing_company)
            if title_score >= threshold and company_score >= threshold:
                matches.append(row)

        if not matches:
            return ""

        return _format_history_timeline(matches, self._STATUS_DISPLAY)

    # --- Batch processing ---

    def set_batch_id(self, listing_id: str, batch_id: str) -> bool:
        """Assign a batch_id to a listing and move it to processing_batch."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE listings SET batch_id = ?, pipeline_status = 'processing_batch',
                updated_at = ?
            WHERE id = ?
            """,
            (batch_id, now, listing_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_listings_by_batch(self, batch_id: str) -> list[sqlite3.Row]:
        """Get all listings associated with a batch_id."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()

    def get_processing_batch_ids(self) -> list[str]:
        """Get distinct batch_ids for listings in processing_batch state."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT batch_id FROM listings
            WHERE pipeline_status = 'processing_batch' AND batch_id IS NOT NULL
            """,
        ).fetchall()
        return [row["batch_id"] for row in rows]

    def get_saved_listings(self, max_age_days: int | None = None) -> list[sqlite3.Row]:
        """Get listings with pipeline_status == 'saved'.

        Args:
            max_age_days: If set, only return listings saved within the past N days.
        """
        if max_age_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            return self.conn.execute(
                "SELECT * FROM listings WHERE pipeline_status = 'saved' "
                "AND updated_at >= ? ORDER BY confidence DESC",
                (cutoff,),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM listings WHERE pipeline_status = 'saved' ORDER BY confidence DESC",
        ).fetchall()

    def get_trend_skills(self, limit: int = 100) -> list[sqlite3.Row]:
        """Fetch the most recent processed jobs for skill trend analysis.

        Returns rows with verdict, pipeline_status, matching_skills, missing_skills.
        Only includes rows where at least one skills field is populated.
        """
        return self.conn.execute(
            """
            SELECT verdict, pipeline_status, matching_skills, missing_skills
            FROM listings
            WHERE matching_skills IS NOT NULL OR missing_skills IS NOT NULL
            ORDER BY date_ingested DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def expire_stale_saved(self, max_age_days: int = 7) -> int:
        """Expire saved listings older than max_age_days (queue rot GC).

        Returns the number of listings expired.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=max_age_days)).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE listings SET pipeline_status = 'expired', updated_at = ?
            WHERE pipeline_status = 'saved'
            AND date_ingested < ?
            """,
            (now.isoformat(), cutoff),
        )
        self.conn.commit()
        return cursor.rowcount

    def revert_stuck_batches(self, max_age_hours: int = 48) -> int:
        """Revert processing_batch listings stuck longer than max_age_hours.

        Resets them to 'saved' with batch_id = NULL so they can be retried.

        Returns the number of listings reverted.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE listings SET pipeline_status = 'saved', batch_id = NULL, updated_at = ?
            WHERE pipeline_status = 'processing_batch'
            AND updated_at < ?
            """,
            (now.isoformat(), cutoff),
        )
        self.conn.commit()
        return cursor.rowcount


def _format_history_timeline(matches: list, status_display: dict) -> str:
    """Format a list of historical matches into a timeline string.

    Applies truncation if more than 4 encounters: shows first + most recent 3,
    joined with ➔ and an ellipsis in the middle.
    """
    entries = []
    for row in matches:
        status = status_display.get(row["pipeline_status"], row["pipeline_status"])
        try:
            dt = datetime.fromisoformat(row["date_ingested"])
            date_str = dt.strftime("%b %d")
        except (ValueError, TypeError):
            date_str = "?"
        entries.append(f"`{status}` ({date_str})")

    if len(entries) <= 4:
        return " ➔ ".join(entries)

    # Truncate: first + ... + last 3
    return " ➔ ".join([entries[0], "...", entries[-3], entries[-2], entries[-1]])


def is_duplicate_email(
    new_text: str, existing_texts: list[str], threshold: float = 0.85
) -> bool:
    """Check if new_text is too similar to any previously processed email."""
    new_preview = new_text[:500]
    for existing in existing_texts:
        ratio = SequenceMatcher(None, new_preview, existing[:500]).ratio()
        if ratio > threshold:
            return True
    return False
