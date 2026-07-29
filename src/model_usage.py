"""Model-usage telemetry channel (ranking_upgrade.md item O-1).

An append-only log of every OpenRouter call's cost signals — model slug,
pipeline stage, and token count — so the live model report (O-2) has a
durable source across interactive runs and cron runs alike.

Deliberately independent of ``src/audit_log.py``: it borrows that module's
pipe-delimited, no-raw-content schema style but writes to its own sink
(``logs/model_usage.log``). The audit logger has no sink of its own — its
lines land in whatever captures ``script.sh``'s stdout (per ``docs/AUDIT.md``),
which evaporates on interactive runs and is useless as a source O-2 must
aggregate across weeks.

Data-safety (SECURITY.md invariant 2): only the model slug, stage, and token
counts are ever written — never raw email, prompt, or response content.

Reversibility: set ``MODEL_USAGE_LOG_ENABLED=false`` to silence the channel
entirely (pure observability loss, no behavior change).

Record schema (pipe-delimited, one line per OpenRouter call):

    <iso8601-utc>|<stage>|<model-slug>|<total-tokens>

``stage`` is a short caller-supplied label (``stage1``, ``stage5``,
``stage3_validate``, ``tailor``, …). ``total-tokens`` is ``resp.usage``'s
total; cost is derived downstream by O-2 joining ``eval/model_pricing.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

_LOGGER_NAME = "apply_daemon.audit.model_usage"
_DEFAULT_LOG_PATH = "logs/model_usage.log"

_configured = False


def _enabled() -> bool:
    return os.getenv("MODEL_USAGE_LOG_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _log_path() -> Path:
    return Path(os.getenv("MODEL_USAGE_LOG_PATH", _DEFAULT_LOG_PATH))


def _get_logger() -> logging.Logger | None:
    """Return the dedicated usage logger, attaching its FileHandler once.

    Returns None if the sink can't be opened — telemetry loss must never
    break a production LLM call.
    """
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Keep usage lines out of the root/app logs — this is a data sink,
        # not diagnostic output.
        logger.propagate = False
        _configured = True
    except OSError:
        return None
    return logger


def iter_usage(path: Path | None = None) -> Iterator[tuple[str, str, str, int]]:
    """Yield ``(day, stage, model, tokens)`` from the usage log.

    ``day`` is the ISO date prefix of the timestamp. Malformed lines are
    skipped rather than raised on: the log is append-only telemetry written
    by many processes, and a torn line must never break a report.
    """
    target = path or _log_path()
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        ts, stage, model, tokens = parts
        try:
            tok = int(tokens)
        except ValueError:
            continue
        yield ts[:10], stage, model, tok


def spend_today(path: Path | None = None) -> tuple[int, float | None]:
    """Return ``(tokens, usd)`` metered so far today (UTC).

    ``usd`` is None when no row could be priced — the caller must treat that
    as "unknown", never as zero, or an unpriced model would silently look
    free to a budget check (`cli_skill_interface.md` C-3).
    """
    today = datetime.now(timezone.utc).date().isoformat()
    tokens = 0
    usd: float | None = None
    try:
        from eval.model_pricing import cost_for_tokens
    except ImportError:
        cost_for_tokens = None

    for day, _stage, model, tok in iter_usage(path):
        if day != today:
            continue
        tokens += tok
        if cost_for_tokens is not None:
            priced = cost_for_tokens(model, tok)
            if priced is not None:
                usd = (usd or 0.0) + priced
    return tokens, usd


def log_response_usage(resp: object, model: str, stage: str) -> int:
    """Log token usage straight from an OpenAI-SDK response object.

    Every metered call site should use this rather than reaching for
    ``resp.usage`` itself — the extraction is defensive (``usage`` is absent
    on some providers and on malformed responses) and the ledger must never
    be the reason a pipeline stage crashes.

    Returns the token count logged (0 when unavailable), so callers that
    already track tokens can keep doing so without a second read.
    """
    tokens = 0
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            tokens = int(getattr(usage, "total_tokens", 0) or 0)
    except (TypeError, ValueError):
        tokens = 0
    log_model_usage(model, stage, tokens)
    return tokens


def log_model_usage(model: str, stage: str, tokens: int) -> None:
    """Append one pipe-delimited usage record. Never raises."""
    if not _enabled():
        return
    logger = _get_logger()
    if logger is None:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat()
        logger.info("%s|%s|%s|%d", ts, stage, model, int(tokens or 0))
    except Exception:  # noqa: BLE001 — telemetry must never break a call
        return
