"""Email classifier — categorizes inbox emails before parsing.

Classifies each email into JOB_DIGEST, RECRUITER_OUTREACH, GOOGLE_ALERT,
SKIP_JUNK, or SKIP_UNCLASSIFIED using only Subject, From, and a quick body
scan. No LLM calls — pure regex/heuristic.

When an EmailConfig is passed, only allowlisted senders may classify as
JOB_DIGEST / GOOGLE_ALERT — digest-shaped mail from unknown senders lands
in SKIP_UNCLASSIFIED so the allowlist-mining report can surface it as a
candidate addition instead of the pipeline silently processing it.

All patterns are defined as constants at the top of this module for easy tuning.
"""

from __future__ import annotations

import logging
import re
from email.message import Message
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.email_config import EmailConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification buckets
# ---------------------------------------------------------------------------

JOB_DIGEST = "JOB_DIGEST"
RECRUITER_OUTREACH = "RECRUITER_OUTREACH"
GOOGLE_ALERT = "GOOGLE_ALERT"
# Confident junk (known noise senders, social notifications) — never revisited.
SKIP_JUNK = "SKIP_JUNK"
# Couldn't positively classify — eligible for re-classification after
# ruleset/allowlist improvements, and mined by the E-1 report.
SKIP_UNCLASSIFIED = "SKIP_UNCLASSIFIED"
# Legacy alias retained for imports; classify_email no longer returns it.
SKIP = "SKIP"

SKIP_CLASSIFICATIONS = frozenset({SKIP, SKIP_JUNK, SKIP_UNCLASSIFIED})

# ---------------------------------------------------------------------------
# Sender patterns (lowercased for matching)
# ---------------------------------------------------------------------------

# Job board digest senders
JOB_DIGEST_SENDERS = [
    "jobs-noreply@linkedin.com",
    "jobs-listings@linkedin.com",
    "jobalerts-noreply@linkedin.com",
    "jobalerts@linkedin.com",
    "alert@indeed.com",
    "jobalert@indeed.com",
    "donotreply@jobalert.indeed.com",
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

def classify_email(msg: Message, config: "EmailConfig | None" = None) -> str:
    """Classify an email using Subject, From, and a quick body scan.

    Returns JOB_DIGEST, GOOGLE_ALERT, RECRUITER_OUTREACH, SKIP_JUNK, or
    SKIP_UNCLASSIFIED. With ``config``, alert classifications are restricted
    to allowlisted senders; without it, the built-in constants apply.
    """
    sender = (msg.get("From", "") or "").lower()
    subject = msg.get("Subject", "") or ""

    google_senders, digest_senders = _effective_senders(config)
    subject_hints = config.subject_hints if config is not None else []
    allowlisted = config is None or config.source_for(sender) is not None

    # --- Google Alerts: specific sender check first ---
    if any(s in sender for s in google_senders):
        logger.info("Classified as GOOGLE_ALERT: %s", _log_subject(subject))
        return GOOGLE_ALERT

    # --- Always-junk senders ---
    for skip_sender in SKIP_SENDERS:
        if skip_sender in sender:
            logger.debug(
                "Classified as SKIP_JUNK (known skip sender): %s", _log_subject(subject)
            )
            return SKIP_JUNK

    # --- LinkedIn emails need careful disambiguation ---
    if "linkedin.com" in sender:
        return _classify_linkedin(
            msg, sender, subject, digest_senders, subject_hints, allowlisted
        )

    # --- Job board digest senders ---
    for digest_sender in digest_senders:
        if digest_sender in sender:
            logger.info("Classified as JOB_DIGEST: %s", _log_subject(subject))
            return JOB_DIGEST

    # --- Subject-based digest detection (non-LinkedIn senders) ---
    if _digest_shaped_subject(subject, subject_hints):
        if allowlisted:
            logger.info(
                "Classified as JOB_DIGEST (subject match): %s", _log_subject(subject)
            )
            return JOB_DIGEST
        # Digest-shaped mail from a non-allowlisted sender: surface as
        # an allowlist candidate instead of processing it.
        logger.info(
            "Digest-shaped subject from non-allowlisted sender %s — "
            "SKIP_UNCLASSIFIED (allowlist candidate): %s",
            sender[:50], _log_subject(subject),
        )
        return SKIP_UNCLASSIFIED

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

    # --- Unclassified ---
    logger.warning(
        "Unclassified email — SKIP_UNCLASSIFIED: from=%s subject='%s'",
        sender[:50],
        _log_subject(subject),
    )
    return SKIP_UNCLASSIFIED


def _digest_shaped_subject(subject: str, subject_hints: list[str]) -> bool:
    """True when the subject looks like a job-alert digest.

    Built-in regex heuristics plus the human-editable plain-substring
    subject_hints from email_config.yaml — new alert formats can be
    covered by editing the yaml, no code change needed.
    """
    if any(pattern.search(subject) for pattern in JOB_DIGEST_SUBJECT_PATTERNS):
        return True
    subject_lower = subject.lower()
    return any(hint.lower() in subject_lower for hint in subject_hints)


def _effective_senders(config: "EmailConfig | None") -> tuple[list[str], list[str]]:
    """(google_alert_senders, digest_senders) from config, or the constants."""
    if config is None:
        return GOOGLE_ALERTS_SENDERS, JOB_DIGEST_SENDERS
    google: list[str] = []
    digest: list[str] = []
    for group in config.senders:
        target = google if group.source == "google_alerts" else digest
        target.extend(a.lower() for a in group.addresses)
    return google, digest


def _classify_linkedin(
    msg: Message,
    sender: str,
    subject: str,
    digest_senders: list[str],
    subject_hints: list[str],
    allowlisted: bool,
) -> str:
    """Disambiguate LinkedIn email types.

    LinkedIn sends many email types from similar addresses. Subject line patterns
    are the most reliable differentiator.
    """
    # Check junk patterns first — social/engagement notifications
    for pattern in LINKEDIN_SKIP_SUBJECT_PATTERNS:
        if pattern.search(subject):
            logger.debug(
                "Classified as SKIP_JUNK (LinkedIn social): %s", _log_subject(subject)
            )
            return SKIP_JUNK

    # Job alert digest senders
    for digest_sender in digest_senders:
        if digest_sender in sender:
            logger.info("Classified as JOB_DIGEST (LinkedIn jobs): %s", _log_subject(subject))
            return JOB_DIGEST

    # Job digest by subject shape — allowlist-gated when a config is active
    if _digest_shaped_subject(subject, subject_hints):
        if allowlisted:
            logger.info(
                "Classified as JOB_DIGEST (LinkedIn subject): %s", _log_subject(subject)
            )
            return JOB_DIGEST
        logger.info(
            "Digest-shaped LinkedIn mail from non-allowlisted address %s — "
            "SKIP_UNCLASSIFIED: %s",
            sender[:50], _log_subject(subject),
        )
        return SKIP_UNCLASSIFIED

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
        "Unclassified LinkedIn email — SKIP_UNCLASSIFIED: from=%s subject='%s'",
        sender[:50],
        _log_subject(subject),
    )
    return SKIP_UNCLASSIFIED


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
