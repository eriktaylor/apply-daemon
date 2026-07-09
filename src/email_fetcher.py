"""IMAP connection and email retrieval for the job alerts inbox.

Touch-nothing contract: fetching NEVER mutates mailbox state. All fetches
use BODY.PEEK (a plain RFC822 fetch would implicitly set \\Seen). The only
function that mutates the mailbox is archive_emails(), which the pipeline
calls at the end of a run for confirmed job-alert emails only — it marks
them read and moves them to the archive folder. Moves only, never deletes.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
from dataclasses import dataclass
from email.message import Message
from typing import Callable

from dotenv import load_dotenv

from src.email_config import EmailConfig

logger = logging.getLogger(__name__)


@dataclass
class FetchedEmail:
    """A fetched message plus the identifiers later stages need.

    ``uid`` is the IMAP UID (valid for the selected folder within this
    run) used by archive_emails(). ``gmail_message_id`` is the RFC 5322
    Message-ID header that keys the processed-emails ledger.
    """

    message: Message
    uid: bytes
    gmail_message_id: str


def get_imap_credentials() -> tuple[str, str]:
    """Load Gmail IMAP credentials from environment."""
    load_dotenv()
    address = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env. "
            "See .env.example for the expected format."
        )
    return address, password


def _use_x_gm_raw() -> bool:
    return os.getenv("GMAIL_USE_X_GM_RAW", "true").strip().lower() in ("1", "true", "yes")


def _archive_enabled() -> bool:
    return os.getenv("EMAIL_ARCHIVE_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def build_gmail_query(config: EmailConfig) -> str:
    """Build the X-GM-RAW search string from the sender allowlist.

    From-only on purpose: the allowlist is the touch contract, so there is
    no value in fetching digest-shaped mail from senders the classifier
    can never accept. E-1's mining report is the path for discovering new
    senders, not a wider query.
    """
    froms = " OR ".join(f"from:{a}" for a in config.all_addresses())
    return f"newer_than:{config.lookback_days}d ({froms})"


def _connect(folder: str = "INBOX") -> imaplib.IMAP4_SSL:
    address, password = get_imap_credentials()
    logger.info("Connecting to Gmail IMAP as %s...", address)
    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(address, password)
    conn.select(folder)
    return conn


def _search_uids(conn: imaplib.IMAP4_SSL, config: EmailConfig | None) -> list[bytes]:
    """UID search: allowlist X-GM-RAW query when enabled, else UNSEEN."""
    if config is not None and config.all_addresses() and _use_x_gm_raw():
        query = build_gmail_query(config)
        logger.info("IMAP search: X-GM-RAW allowlist query (%d senders)",
                    len(config.all_addresses()))
        status, data = conn.uid("SEARCH", "X-GM-RAW", f'"{query}"')
    else:
        logger.info("IMAP search: UNSEEN (X-GM-RAW disabled or no config)")
        status, data = conn.uid("SEARCH", "UNSEEN")
    if status != "OK":
        logger.warning("IMAP search failed: %s", status)
        return []
    return data[0].split() if data and data[0] else []


def _peek_message_id(conn: imaplib.IMAP4_SSL, uid: bytes) -> str | None:
    """Fetch only the Message-ID header, without touching any flags.

    Returns None when the header fetch itself fails (caller should fall
    through to the full fetch rather than skip the email).
    """
    status, data = conn.uid(
        "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
    )
    if status != "OK" or not data or not isinstance(data[0], tuple):
        return None
    header_msg = email.message_from_bytes(data[0][1])
    return (header_msg.get("Message-ID", "") or "").strip() or None


def fetch_inbox(
    folder: str = "INBOX",
    config: EmailConfig | None = None,
    already_seen: Callable[[str], bool] | None = None,
) -> list[FetchedEmail]:
    """Fetch candidate emails without mutating any mailbox state.

    Two-phase fetch: a header-only peek retrieves the Message-ID first, and
    ``already_seen`` (the processed-emails ledger) short-circuits before the
    full body is downloaded — read-but-unarchived mail costs one header
    round-trip per run, not a body download.
    """
    conn = _connect(folder)
    try:
        uids = _search_uids(conn, config)
        if not uids:
            logger.info("No matching emails found")
            return []

        logger.info("Found %d candidate email(s)", len(uids))
        fetched: list[FetchedEmail] = []
        ledger_skipped = 0

        for uid in uids:
            message_id = _peek_message_id(conn, uid)
            if message_id and already_seen and already_seen(message_id):
                ledger_skipped += 1
                continue

            status, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                logger.warning("Failed to fetch email uid=%s", uid)
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            fetched.append(
                FetchedEmail(
                    message=msg,
                    uid=uid,
                    gmail_message_id=message_id
                    or (msg.get("Message-ID", "") or "").strip(),
                )
            )

        if ledger_skipped:
            logger.info("Skipped %d already-processed email(s) at fetch", ledger_skipped)
        return fetched

    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


def archive_emails(uids: list[bytes], folder: str) -> int:
    """Mark the given emails read and move them to the archive folder.

    The pipeline's ONLY mailbox mutation. Move-only — never deletes or
    expunges; a wrongly-moved email is recoverable from the folder by hand.
    Per-email failures are logged and non-fatal: the ledger already prevents
    re-processing, so an email stranded in the inbox costs nothing next run.

    Returns the number of emails successfully moved.
    """
    if not uids:
        return 0
    if not _archive_enabled():
        logger.info("EMAIL_ARCHIVE_ENABLED=false — leaving %d email(s) in place", len(uids))
        return 0

    conn = _connect("INBOX")
    try:
        # Gmail creates the label on demand via CREATE; "already exists" is fine.
        try:
            conn.create(folder)
        except Exception:
            pass

        moved = 0
        for uid in uids:
            try:
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                status, _ = conn.uid("MOVE", uid, folder)
                if status == "OK":
                    moved += 1
                else:
                    logger.warning("MOVE failed for uid=%s (status=%s)", uid, status)
            except Exception:
                logger.warning("Archive failed for uid=%s", uid, exc_info=True)

        logger.info("Archived %d/%d email(s) to %s", moved, len(uids), folder)
        return moved

    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


def sweep_stale_alerts(
    config: EmailConfig,
    ledger: Callable[[str], None] | None = None,
) -> int:
    """Archive allowlisted alert emails older than the lookback window.

    The main fetch is bounded to newer_than:lookback_days, so alerts that
    predate it would sit in the inbox forever. This sweep moves them to the
    archive folder unprocessed — they are stale by definition, matching the
    ARCHIVED_STALE policy for in-window stale mail. Same touch contract as
    archive_emails: allowlisted senders only, mark read + move, never
    delete, per-email failures non-fatal. ``ledger`` (called with each
    moved email's Message-ID) lets the pipeline record the outcome.

    Needs X-GM-RAW for the age filter; no-ops when archiving or X-GM-RAW
    is disabled, or the allowlist is empty.
    """
    if not _archive_enabled() or not _use_x_gm_raw() or not config.all_addresses():
        return 0

    conn = _connect("INBOX")
    try:
        froms = " OR ".join(f"from:{a}" for a in config.all_addresses())
        query = f"older_than:{config.lookback_days}d ({froms})"
        status, data = conn.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        if status != "OK":
            logger.warning("Stale sweep search failed: %s", status)
            return 0
        uids = data[0].split() if data and data[0] else []
        if not uids:
            logger.info("Stale sweep: no alert emails older than %dd", config.lookback_days)
            return 0

        logger.info(
            "Stale sweep: %d alert email(s) older than %dd",
            len(uids), config.lookback_days,
        )
        try:
            conn.create(config.archive_folder)
        except Exception:
            pass

        moved = 0
        for uid in uids:
            try:
                message_id = _peek_message_id(conn, uid)
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
                status, _ = conn.uid("MOVE", uid, config.archive_folder)
                if status == "OK":
                    moved += 1
                    if ledger and message_id:
                        ledger(message_id)
                else:
                    logger.warning("Stale sweep MOVE failed for uid=%s (status=%s)", uid, status)
            except Exception:
                logger.warning("Stale sweep failed for uid=%s", uid, exc_info=True)

        logger.info(
            "Stale sweep archived %d/%d email(s) to %s",
            moved, len(uids), config.archive_folder,
        )
        return moved

    finally:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()


def detect_source(msg: Message) -> str | None:
    """Detect the email source from sender address or headers.

    Returns one of: 'linkedin', 'google_alerts', 'indeed', 'glassdoor', or None.
    """
    sender = (msg.get("From", "") or "").lower()

    source_patterns = {
        "linkedin": ["linkedin.com"],
        "google_alerts": ["googlealerts-noreply@google.com"],
        "indeed": ["indeed.com"],
        "glassdoor": ["glassdoor.com"],
    }

    for source, patterns in source_patterns.items():
        if any(p in sender for p in patterns):
            return source

    logger.warning("Unknown email source: %s", sender)
    return None
