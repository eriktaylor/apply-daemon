"""Model-usage telemetry channel (ranking_upgrade.md item O-1).

An append-only log of every OpenRouter call's cost signals — model slug,
pipeline stage, and token count — so the live model report (O-2) has a
durable source across interactive runs and cron runs alike.

Shares its "dedicated logger, file sink attached once" mechanics with
``src/audit_log.py`` via ``src/file_logger.py`` (see that module — the one
implementation site, guarded by ``tests/test_no_duplication.py``). The two
channels stay logically independent sinks with independent files: this one
is metered-spend telemetry (``logs/model_usage.log``) consumed by
``budget.py``/``report.py``; audit_log is drop reasons (``logs/audit.log``)
consumed by grep — see ``docs/AUDIT.md``.

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

from src.file_logger import get_file_logger

_LOGGER_NAME = "apply_daemon.audit.model_usage"
_DEFAULT_LOG_PATH = "logs/model_usage.log"

# Legacy fast-path flag, kept for tests that reset the channel by setting it
# False (tests/test_model_usage.py::_reset_channel). The actual attach-once
# guard now lives in src/file_logger.py, keyed off logger.handlers, so this
# flag no longer gates anything itself — it just mirrors whether the last
# attach attempt succeeded.
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
    break a production LLM call. Handler-attach mechanics live in
    src/file_logger.py (shared with src/audit_log.py); this wrapper only
    adds the module's env-driven path and the "data sink, not diagnostic
    output" propagation choice.
    """
    global _configured
    logger = get_file_logger(_LOGGER_NAME, _log_path(), propagate=False)
    _configured = logger is not None
    return logger


def iter_usage(
    path: Path | None = None,
) -> Iterator[tuple[str, str, str, int, int, int]]:
    """Yield ``(day, stage, model, total, prompt, completion)`` from the log.

    Accepts both schemas: four fields (pre-split, prompt/completion reported
    as 0) and six. ``day`` is the ISO date prefix. Malformed lines are skipped
    rather than raised on — the log is append-only telemetry written by many
    processes, and a torn line must never break a report.
    """
    target = path or _log_path()
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split("|")
        if len(parts) not in (4, 6):
            continue
        try:
            total = int(parts[3])
            prompt = int(parts[4]) if len(parts) == 6 else 0
            completion = int(parts[5]) if len(parts) == 6 else 0
        except ValueError:
            continue
        yield parts[0][:10], parts[1], parts[2], total, prompt, completion


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

    try:
        from eval.model_pricing import cost_for_usage
    except ImportError:
        cost_for_usage = None

    for day, _stage, model, tok, prompt, completion in iter_usage(path):
        if day != today:
            continue
        tokens += tok
        priced = None
        if cost_for_usage is not None and (prompt or completion):
            priced = cost_for_usage(model, prompt, completion)
        elif cost_for_tokens is not None:
            priced = cost_for_tokens(model, tok)
        if priced is not None:
            usd = (usd or 0.0) + priced
    return tokens, usd


def _usage_split(resp: object) -> tuple[int, int, int]:
    """Extract ``(total, prompt, completion)`` tokens from an SDK response.

    Direction matters for pricing: input and output rates differ by 5-6x, and
    this workload is overwhelmingly input (a large profile + resume preamble
    against a small JSON reply). Blending them with a fixed ratio overstated
    real spend by ~2.2x — see eval/model_pricing.
    """
    total = prompt = completion = 0
    try:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            total = int(getattr(usage, "total_tokens", 0) or 0)
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0, 0
    if not total:
        total = prompt + completion
    return total, prompt, completion


def log_response_usage(resp: object, model: str, stage: str) -> int:
    """Log token usage straight from an OpenAI-SDK response object.

    Every metered call site should use this rather than reaching for
    ``resp.usage`` itself — the extraction is defensive (``usage`` is absent
    on some providers and on malformed responses) and the ledger must never
    be the reason a pipeline stage crashes.

    Returns the token count logged (0 when unavailable), so callers that
    already track tokens can keep doing so without a second read.
    """
    total, prompt, completion = _usage_split(resp)
    log_model_usage(model, stage, total, prompt=prompt, completion=completion)
    return total


def log_model_usage(model: str, stage: str, tokens: int, *,
                    prompt: int = 0, completion: int = 0) -> None:
    """Append one pipe-delimited usage record. Never raises."""
    if not _enabled():
        return
    logger = _get_logger()
    if logger is None:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat()
        # Schema: timestamp|stage|model|total|prompt|completion
        # Lines written before the split have four fields; iter_usage reads
        # both, so old logs stay priceable at the blended rate.
        logger.info(
            "%s|%s|%s|%d|%d|%d", ts, stage, model,
            int(tokens or 0), int(prompt or 0), int(completion or 0),
        )
    except Exception:  # noqa: BLE001 — telemetry must never break a call
        return
