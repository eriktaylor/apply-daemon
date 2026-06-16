"""SQLite schema and access layer for the listings database."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from rapidfuzz.fuzz import token_set_ratio

from src.models import JobListing

DEFAULT_DB_PATH = Path("apply_daemon.db")

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
        migrations = (
            ("slack_message_ts", "ALTER TABLE listings ADD COLUMN slack_message_ts TEXT"),
            (
                "autopilot_processed_at",
                "ALTER TABLE listings ADD COLUMN autopilot_processed_at TEXT",
            ),
            (
                "distance_bucket",
                "ALTER TABLE listings ADD COLUMN distance_bucket INTEGER",
            ),
        )
        for col, ddl in migrations:
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if f"duplicate column name: {col}" not in str(exc):
                    raise
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
        # Autopilot lanes — speculative execution after Stage 5.
        "auto_queued", "auto",
    }

    # Lanes that should not be demoted by autopilot queueing.
    _AUTOPILOT_BLOCKED_STATUSES = frozenset(
        {"passed", "tailored", "auto", "applied", "rejected", "interviewing", "expired"}
    )

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

    def get_digest_listings(
        self,
        days: int = 14,
        min_confidence_pct: int | None = None,
        include_auto_queued: bool = True,
    ) -> list[sqlite3.Row]:
        """Get listings for the daily digest.

        Includes ``triaged``, ``saved``, and ``auto_queued`` rows so every
        Stage-5-approved listing surfaces in Slack — autopilot enrichment later
        edits the card in place rather than gating its initial visibility.
        Sorted by confidence descending so highest-scoring listings come first.

        Only returns listings that have NOT yet been posted to Slack
        (``slack_notified = 0``). This makes the digest self-healing: a row
        stranded with ``slack_notified=0`` by a crashed run is picked up on the
        next invocation.

        Defense in depth: any active-status row with verdict='NO' is flipped to
        ``pipeline_status='passed'`` before the query so it cannot surface in
        the digest — a NO is a NO regardless of how it reached the DB.

        Args:
            days: Only consider rows ingested within the last N days.
            min_confidence_pct: If set, re-gate against the current confidence
                threshold so a later threshold raise drops sub-threshold
                un-notified rows from the digest.
        """
        self.conn.execute(
            """
            UPDATE listings
            SET pipeline_status = 'passed',
                updated_at = ?
            WHERE verdict = 'NO'
              AND pipeline_status IN ('triaged', 'saved', 'auto_queued')
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()
        statuses = ("'triaged', 'saved', 'auto_queued'"
                    if include_auto_queued else "'triaged', 'saved'")
        sql = (
            "SELECT * FROM listings "
            f"WHERE pipeline_status IN ({statuses}) "
            "AND slack_notified = 0 "
            "AND date_ingested >= datetime('now', ?)"
        )
        params: list = [f"-{days} days"]
        if min_confidence_pct is not None:
            sql += " AND confidence >= ?"
            params.append(min_confidence_pct)
        sql += " ORDER BY confidence DESC, date_ingested DESC"
        return self.conn.execute(sql, params).fetchall()

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

    def set_slack_message_ts(self, listing_id: str, ts: str) -> bool:
        """Persist the Slack message timestamp for later chat.update calls."""
        cursor = self.conn.execute(
            "UPDATE listings SET slack_message_ts = ? WHERE id = ?",
            (ts, listing_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_slack_message_ts(self, listing_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT slack_message_ts FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if row is None:
            return None
        return row["slack_message_ts"]

    def set_distance_bucket(self, listing_id: str, bucket: int) -> bool:
        """Persist the geo distance bucket (0=Remote, 1=Local, 2=Commute, 3=Relocation)."""
        cursor = self.conn.execute(
            "UPDATE listings SET distance_bucket = ? WHERE id = ?",
            (bucket, listing_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_autopilot_processed(self, listing_id: str) -> bool:
        """Stamp the moment autopilot finalized this listing (auto or auto-pass)."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            "UPDATE listings SET autopilot_processed_at = ? WHERE id = ?",
            (now, listing_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def count_autopilot_processed_today(self) -> int:
        """Count listings autopilot has finalized since 00:00 UTC today."""
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM listings WHERE autopilot_processed_at >= ?",
            (start,),
        ).fetchone()
        return int(row["cnt"]) if row else 0

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
        "auto_queued": "auto-queued",
        "auto": "auto-evaluated",
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


    # --- Autopilot queue ---

    def mark_auto_queued(self, listing_id: str) -> bool:
        """Flag a listing for the Speculative Agent (autopilot mode).

        Honors lane priority: pass > tailored > auto > auto_queued > triaged.
        A row already in a higher-priority lane is left untouched.
        """
        row = self.conn.execute(
            "SELECT pipeline_status FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if row is None:
            return False
        if row["pipeline_status"] in self._AUTOPILOT_BLOCKED_STATUSES:
            return False
        return self.update_pipeline_status(listing_id, "auto_queued")

    def get_auto_queue(
        self,
        top_n: int | None = None,
        min_confidence_pct: int = 0,
    ) -> list[sqlite3.Row]:
        """Return listings queued for the Speculative Agent, highest confidence first.

        Filters to YES/MAYBE verdicts and ``confidence >= min_confidence_pct``.
        When ``top_n`` is set, caps the result at that many rows.
        """
        sql = (
            "SELECT * FROM listings "
            "WHERE pipeline_status = 'auto_queued' "
            "AND verdict IN ('YES', 'MAYBE') "
            "AND confidence >= ? "
            "ORDER BY confidence DESC, date_ingested DESC"
        )
        params: list = [min_confidence_pct]
        if top_n is not None:
            sql += " LIMIT ?"
            params.append(top_n)
        return self.conn.execute(sql, params).fetchall()

    def backfill_auto_queue(self, min_confidence_pct: int) -> int:
        """Promote existing triaged/saved listings to auto_queued.

        Only rows with verdict YES/MAYBE and confidence >= ``min_confidence_pct``
        are moved. Higher-priority lanes (passed, tailored, auto, applied,
        rejected, interviewing, expired) are never demoted. Returns the number
        of rows promoted.
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """
            UPDATE listings
            SET pipeline_status = 'auto_queued', updated_at = ?
            WHERE pipeline_status IN ('triaged', 'saved')
              AND verdict IN ('YES', 'MAYBE')
              AND confidence >= ?
            """,
            (now, min_confidence_pct),
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
