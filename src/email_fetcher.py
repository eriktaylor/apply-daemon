"""IMAP connection and email retrieval for the job alerts inbox."""

from __future__ import annotations

import email
import imaplib
import logging
import os
from email.message import Message

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


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


def fetch_unread_emails(
    folder: str = "INBOX",
    mark_as_read: bool = True,
) -> list[Message]:
    """Connect to Gmail via IMAP and fetch unread emails.

    Args:
        folder: IMAP folder to search. Defaults to INBOX.
        mark_as_read: Whether to mark fetched emails as read (set SEEN flag).

    Returns:
        List of email.message.Message objects.
    """
    address, password = get_imap_credentials()

    logger.info("Connecting to Gmail IMAP as %s...", address)
    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        conn.login(address, password)
        conn.select(folder)

        # Search for unread messages
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            return []

        message_ids = data[0].split()
        if not message_ids:
            logger.info("No unread emails found")
            return []

        logger.info("Found %d unread emails", len(message_ids))
        messages: list[Message] = []

        for msg_id in message_ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822)")
            if status != "OK":
                logger.warning("Failed to fetch email %s", msg_id)
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            messages.append(msg)

            if mark_as_read:
                conn.store(msg_id, "+FLAGS", "\\Seen")

        return messages

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
