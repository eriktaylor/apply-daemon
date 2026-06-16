"""Pipe-delimited audit log for silent drops.

Used by the mismatch gate (Fix 2a) and the expired-listing gates (Fix 4a
Stage 5, Fix 4b HTTP probe) to leave a stable, greppable trail when a
listing is dropped without reaching Slack. Schema is documented in
``docs/AUDIT.md``.

Security: the schema deliberately excludes raw description text, LLM
prompts/responses, and credentials. The ``reason`` argument is a short
clause supplied by the caller — never a verbatim slice of source content.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger("apply_daemon.audit.mismatch_drops")


def _host(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    return host.lower().removeprefix("www.")


def _safe(value: object) -> str:
    """Strip pipes/newlines from a field to keep the pipe-delimited schema parseable."""
    if value is None:
        return ""
    s = str(value)
    return s.replace("|", " ").replace("\n", " ").replace("\r", " ").strip()


def log_drop(
    *,
    listing_id: str,
    source: str,
    gate: str,
    anchor_company: str,
    observed_company: str = "",
    url: str = "",
    reason: str = "",
) -> None:
    """Emit one pipe-delimited audit row.

    Args:
        listing_id: ``listings.id`` UUID.
        source: Track-A site or Track-B classification (e.g. "linkedin").
        gate: which check fired the drop. One of: stage5, substring, llm, probe.
        anchor_company: company name from the row metadata.
        observed_company: company name detected from body/URL, or "".
        url: ``links[0]`` for host extraction; host is logged, not the full URL.
        reason: short human-readable clause from the calling gate.
    """
    ts = datetime.now(timezone.utc).isoformat()
    fields = [
        ts,
        _safe(listing_id),
        _safe(source),
        _safe(gate),
        _safe(anchor_company),
        _safe(observed_company),
        _host(url),
        _safe(reason),
    ]
    logger.info("audit.mismatch_drops | " + " | ".join(fields))
