"""Pipe-delimited audit log for silent drops.

Used by the mismatch gate (Fix 2a) and the expired-listing gates (Fix 4a
Stage 5, Fix 4b HTTP probe) to leave a stable, greppable trail when a
listing is dropped without reaching Slack. Schema is documented in
``docs/AUDIT.md``.

Writes to its own file (``logs/audit.log`` by default) via
``src/file_logger.py`` — the same "dedicated logger, sink attached once"
helper ``src/model_usage.py`` uses, so this is the second caller rather than
a second implementation (CLAUDE.md → Anti-drift in code). Unlike that
channel this one keeps ``propagate=True``: the file is additive, not a
replacement for whatever already captures the process's logging output
(e.g. cron's stdout/stderr redirect per ``docs/AUDIT.md``), so a cron setup
loses nothing.

Security: the schema deliberately excludes raw description text, LLM
prompts/responses, and credentials. The ``reason`` argument is a short
clause supplied by the caller — never a verbatim slice of source content.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.file_logger import get_file_logger

_LOGGER_NAME = "apply_daemon.audit.mismatch_drops"
_DEFAULT_LOG_PATH = "logs/audit.log"

logger = logging.getLogger(_LOGGER_NAME)


def _enabled() -> bool:
    return os.getenv("AUDIT_LOG_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _log_path() -> Path:
    return Path(os.getenv("AUDIT_LOG_PATH", _DEFAULT_LOG_PATH))


def _get_logger() -> logging.Logger | None:
    """Best-effort attach of the audit log's file sink.

    Returns None (and attaches nothing) when disabled or when the sink
    can't be opened — ``log_drop`` always emits via the module logger
    regardless of this outcome, so propagation to the root logger (cron's
    stderr redirect) never depends on whether the file sink attached.
    """
    if not _enabled():
        return None
    return get_file_logger(_LOGGER_NAME, _log_path(), propagate=True)


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
    _get_logger()  # best-effort file-sink attach; log line fires regardless
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
