"""Shared "dedicated logger, file sink attached once" pattern.

Two callers need the same small mechanism — a logger that writes to its own
file, with the handler created exactly once no matter how many times the
call site runs, and the path overridable by env: ``src/model_usage.py``
(metered-spend telemetry) and ``src/audit_log.py`` (silent-drop rows). A
second hand-rolled copy of "attach a FileHandler, guard against doing it
twice" is exactly the duplication CLAUDE.md's anti-drift rule names as the
trap — this module is the one implementation site. Register any future
instance in ``tests/test_no_duplication.py`` rather than writing a second
copy; see that file's docstring for how the registry works.

Data-safety is a caller concern, not this module's: each caller decides
what fields land in a line and whether the record also propagates to the
root logger. This module only owns the sink mechanics.
"""

from __future__ import annotations

import logging
from pathlib import Path


def get_file_logger(
    name: str,
    path: Path,
    *,
    propagate: bool = False,
) -> logging.Logger | None:
    """Return the named logger with a ``FileHandler`` attached exactly once.

    Idempotent per logger ``name``: a second and later call returns the same
    logger without adding a second handler, which would double every line
    written through it. Idempotency is keyed off ``logger.handlers`` rather
    than a private module flag — ``logging.getLogger(name)`` already hands
    back the same singleton object for a given name, so a caller's test can
    force re-attachment (e.g. after swapping the log path) just by removing
    the logger's handlers directly; see
    ``tests/test_model_usage.py::_reset_channel`` for the pattern.

    Returns ``None`` if the sink can't be opened (e.g. an unwritable
    directory) — losing a log sink must never break the call site it
    instruments. The named logger itself is untouched on failure, so a
    caller that still wants records to propagate to the root logger (rather
    than being silently dropped) can go on using it directly.

    ``propagate`` controls whether records also flow to the root logger
    once this handler is attached. Pass ``False`` for a pure data sink
    (e.g. usage telemetry, which must not double up in application logs);
    pass ``True`` when the file is meant to be *additive* to whatever
    already captures the logger's output (e.g. cron's stdout/stderr
    redirect) rather than a replacement for it.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = propagate
    except OSError:
        return None
    return logger
