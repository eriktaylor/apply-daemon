"""Email classifier — categorizes inbox emails before parsing.

Classifies each email into JOB_DIGEST, RECRUITER_OUTREACH, GOOGLE_ALERT, or SKIP
using only Subject, From, and a quick body scan. No LLM calls — pure regex/heuristic.

All patterns are defined as constants at the top of this module for easy tuning.
"""

from __future__ import annotations

import logging
import re
from email.message import Message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification buckets
# ---------------------------------------------------------------------------

JOB_DIGEST = "JOB_DIGEST"
RECRUITER_OUTREACH = "RECRUITER_OUTREACH"
GOOGLE_ALERT = "GOOGLE_ALERT"
SKIP = "SKIP"

# ---------------------------------------------------------------------------
# Sender patterns (lowercased for matching)
# ---------------------------------------------------------------------------

# Job board digest senders
JOB_DIGEST_SENDERS = [
    "jobs-noreply@linkedin.com",
    "jobalerts-noreply@linkedin.com",
    "jobalerts@linkedin.com",
    "alert@indeed.com",
    "jobalert@indeed.com",
    "jobalert.indeed.com",
    "noreply@glassdoor.com",
]

# Google Alerts senders (multiple variants in use)
GOOGLE_ALERTS_SENDERS = [
    "googlealerts-noreply@google.com",
    "notify-noreply@google.com",
]

# LinkedIn notification senders (used for both outreach and social — disambiguated by subject)
LINKEDIN_NOTIFICATION_SENDERS = [
    "notifications-noreply@linkedin.com",
    "inmail-hit-reply@linkedin.com",
    "inmails-noreply@linkedin.com",
    "messaging-digest-noreply@linkedin.com",
]

# Senders that are always SKIP
SKIP_SENDERS = [
    "noreply@google.com",
    "no-reply@accounts.google.com",
    "security-noreply@google.com",
    "googlecommunityteam-noreply@google.com",
]

# ---------------------------------------------------------------------------
# Subject patterns
# ---------------------------------------------------------------------------

# Job digest subject patterns (case-insensitive)
JOB_DIGEST_SUBJECT_PATTERNS = [
    re.compile(r"job alert", re.IGNORECASE),
    re.compile(r"new jobs? for you", re.IGNORECASE),
    re.compile(r"jobs? that match", re.IGNORECASE),
    re.compile(r"matching jobs?", re.IGNORECASE),
    re.compile(r"jobs? you might", re.IGNORECASE),
    re.compile(r"recommended jobs?", re.IGNORECASE),
    re.compile(r"\d+ new jobs?", re.IGNORECASE),
    re.compile(r"\d+ new .+ jobs? in ", re.IGNORECASE),
    re.compile(r"jobs? in your area", re.IGNORECASE),
]

# Recruiter outreach subject patterns
OUTREACH_SUBJECT_PATTERNS = [
    re.compile(r"sent you a message", re.IGNORECASE),
    re.compile(r"inmail", re.IGNORECASE),
    re.compile(r"new message from", re.IGNORECASE),
    re.compile(r"reaching out", re.IGNORECASE),
    re.compile(r"opportunity at", re.IGNORECASE),
    re.compile(r"role at", re.IGNORECASE),
    re.compile(r"position at", re.IGNORECASE),
    re.compile(r"interested in .+ at", re.IGNORECASE),
]

# LinkedIn social/engagement subjects — always SKIP
LINKEDIN_SKIP_SUBJECT_PATTERNS = [
    re.compile(r"viewed your profile", re.IGNORECASE),
    re.compile(r"your post .*(reaction|comment|like|view)", re.IGNORECASE),
    re.compile(r"appeared in .* search", re.IGNORECASE),
    re.compile(r"connection request", re.IGNORECASE),
    re.compile(r"accepted your invitation", re.IGNORECASE),
    re.compile(r"endorsed you", re.IGNORECASE),
    re.compile(r"congratulate", re.IGNORECASE),
    re.compile(r"work anniversary", re.IGNORECASE),
    re.compile(r"birthday", re.IGNORECASE),
    re.compile(r"trending in your network", re.IGNORECASE),
    re.compile(r"people you may know", re.IGNORECASE),
    re.compile(r"your weekly digest", re.IGNORECASE),
]

# Body patterns for recruiter outreach (first ~500 chars)
OUTREACH_BODY_PATTERNS = [
    re.compile(r"(hi|hello|hey)\s+\w+", re.IGNORECASE),
    re.compile(r"i noticed your (background|profile|experience)", re.IGNORECASE),
    re.compile(r"are you open to", re.IGNORECASE),
    re.compile(r"we.re (hiring|looking for)", re.IGNORECASE),
    re.compile(r"i.?d love to (chat|connect|discuss|talk)", re.IGNORECASE),
    re.compile(r"reaching out .* (role|position|opportunity)", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

def classify_email(msg: Message) -> str:
    """Classify an email into JOB_DIGEST, RECRUITER_OUTREACH, GOOGLE_ALERT, or SKIP.

    Uses Subject, From, and optionally the first ~500 chars of the body.
    Defaults to SKIP if no confident match is found.
    """
    sender = (msg.get("From", "") or "").lower()
    subject = msg.get("Subject", "") or ""
    subject_lower = subject.lower()

    # --- Google Alerts: specific sender check first ---
    if any(s in sender for s in GOOGLE_ALERTS_SENDERS):
        logger.info("Classified as GOOGLE_ALERT: %s", _log_subject(subject))
        return GOOGLE_ALERT

    # --- Always-SKIP senders ---
    for skip_sender in SKIP_SENDERS:
        if skip_sender in sender:
            logger.debug("Classified as SKIP (known skip sender): %s", _log_subject(subject))
            return SKIP

    # --- LinkedIn emails need careful disambiguation ---
    if "linkedin.com" in sender:
        return _classify_linkedin(msg, sender, subject, subject_lower)

    # --- Job board digest senders ---
    for digest_sender in JOB_DIGEST_SENDERS:
        if digest_sender in sender:
            logger.info("Classified as JOB_DIGEST: %s", _log_subject(subject))
            return JOB_DIGEST

    # --- Subject-based digest detection (non-LinkedIn senders) ---
    for pattern in JOB_DIGEST_SUBJECT_PATTERNS:
        if pattern.search(subject):
            logger.info("Classified as JOB_DIGEST (subject match): %s", _log_subject(subject))
            return JOB_DIGEST

    # --- Corporate domain outreach (not bulk senders) ---
    # If the sender is from a corporate domain and subject hints at outreach
    if _is_corporate_sender(sender):
        for pattern in OUTREACH_SUBJECT_PATTERNS:
            if pattern.search(subject):
                logger.info(
                    "Classified as RECRUITER_OUTREACH (corporate sender): %s",
                    _log_subject(subject),
                )
                return RECRUITER_OUTREACH

    # --- Unclassified — default to SKIP ---
    logger.warning(
        "Unclassified email, defaulting to SKIP: from=%s subject='%s'",
        sender[:50],
        _log_subject(subject),
    )
    return SKIP


def _classify_linkedin(msg: Message, sender: str, subject: str, subject_lower: str) -> str:
    """Disambiguate LinkedIn email types.

    LinkedIn sends many email types from similar addresses. Subject line patterns
    are the most reliable differentiator.
    """
    # Check SKIP patterns first — social/engagement notifications
    for pattern in LINKEDIN_SKIP_SUBJECT_PATTERNS:
        if pattern.search(subject):
            logger.debug("Classified as SKIP (LinkedIn social): %s", _log_subject(subject))
            return SKIP

    # Job alert digest senders
    for digest_sender in JOB_DIGEST_SENDERS:
        if digest_sender in sender:
            logger.info("Classified as JOB_DIGEST (LinkedIn jobs): %s", _log_subject(subject))
            return JOB_DIGEST

    # Job digest by subject pattern
    for pattern in JOB_DIGEST_SUBJECT_PATTERNS:
        if pattern.search(subject):
            logger.info(
                "Classified as JOB_DIGEST (LinkedIn subject): %s", _log_subject(subject)
            )
            return JOB_DIGEST

    # Recruiter outreach — InMail or message notification
    is_notification_sender = any(s in sender for s in LINKEDIN_NOTIFICATION_SENDERS)
    for pattern in OUTREACH_SUBJECT_PATTERNS:
        if pattern.search(subject):
            logger.info(
                "Classified as RECRUITER_OUTREACH (LinkedIn): %s", _log_subject(subject)
            )
            return RECRUITER_OUTREACH

    # If it's a notification sender, check body for outreach signals
    if is_notification_sender:
        body_preview = _get_body_preview(msg, max_chars=500)
        if body_preview:
            for pattern in OUTREACH_BODY_PATTERNS:
                if pattern.search(body_preview):
                    logger.info(
                        "Classified as RECRUITER_OUTREACH (LinkedIn body match): %s",
                        _log_subject(subject),
                    )
                    return RECRUITER_OUTREACH

    # LinkedIn email we can't confidently classify
    logger.warning(
        "Unclassified LinkedIn email, defaulting to SKIP: from=%s subject='%s'",
        sender[:50],
        _log_subject(subject),
    )
    return SKIP


def _is_corporate_sender(sender: str) -> bool:
    """Check if sender is from a corporate domain (not a bulk/consumer sender)."""
    bulk_domains = [
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "noreply", "no-reply", "donotreply", "mailer-daemon",
    ]
    return not any(d in sender for d in bulk_domains)


def _get_body_preview(msg: Message, max_chars: int = 500) -> str:
    """Extract the first max_chars characters of the email body for classification."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode(charset, errors="replace")[:max_chars]
                elif ctype == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        text = payload.decode(charset, errors="replace")
                        # Strip HTML tags for body scanning
                        clean = re.sub(r"<[^>]+>", " ", text)
                        clean = re.sub(r"\s+", " ", clean).strip()
                        return clean[:max_chars]
        else:
            charset = msg.get_content_charset() or "utf-8"
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text).strip()
                return text[:max_chars]
    except Exception:
        logger.debug("Failed to extract body preview", exc_info=True)
    return ""


def _log_subject(subject: str) -> str:
    """Truncate subject for logging — avoid logging full email content."""
    return subject[:80] if subject else "(no subject)"
