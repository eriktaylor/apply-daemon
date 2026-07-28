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
