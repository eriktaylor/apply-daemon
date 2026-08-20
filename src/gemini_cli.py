"""Subscription-billed model calls through the Antigravity CLI (`agy`).

Sibling of :mod:`src.claude_cli`, and deliberately the same shape: a
subprocess that starts its own headless session, so unattended code can reach
a model without spending metered OpenRouter credit. Where `claude -p` draws on
the Claude subscription, `agy` draws on Google AI Pro — one pool shared across
*all* its slugs (Gemini, Claude, GPT-OSS alike), which is why routing two
stages here can make one throttle the other. See `docs/MODELS.md`.

**Spend.** Subscription-billed, so — exactly as in ``claude_cli`` — usage is
NOT written to ``logs/model_usage.log``. That file is the basis for the spend
ceiling in ``src/budget.py``; counting membership work there would make the
daily cap refuse runs over money nobody was charged.

**Measured 2026-08-19** (`agy` v1.1.15), against `claude -p`'s ~23k:

- Per-call overhead is **~14k input tokens**, and `cache_read_tokens` shows
  the preamble caching across calls within a session.
- A trivial prompt costs ~8s of model time, ~11s wall.
- ``--json-schema`` enforces structured output and returns it pre-parsed in
  ``structured_output``. This is the one capability the OpenRouter slots lack,
  and it removes the fence-stripping/``JSONDecodeError`` failure class that
  silently truncated ``rank_stage5``.

**Two invocation traps**, both found the hard way:

1. ``-p``/``--print`` takes the prompt **as its value**, unlike ``claude -p``
   which is a boolean flag. ``agy -p --output-format json`` therefore sends
   the literal string ``--output-format`` as the prompt.
2. There is **no stdin path** — piping fails with an argument error, so the
   prompt goes on argv. Fine at listwise sizes (~40KB against a ~2MB ARG_MAX),
   but it is a ceiling that ``claude_cli`` does not have.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from src.claude_cli import ClaudeResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300
BINARY = "agy"

# Overhead measured 2026-08-19; used only for reporting, never for billing.
MEASURED_OVERHEAD_TOKENS = 14_174


def is_available() -> bool:
    """True when the `agy` binary is on PATH.

    Callers must check: like `claude`, it is present in a developer shell and
    absent from a bare cron environment, and a missing binary should fall back
    to the metered route rather than fail the run.
    """
    return shutil.which(BINARY) is not None


def session_model(default: str = "gemini-3.7-flash-medium") -> str:
    """Which model the agy route uses (``AGY_CLI_MODEL``)."""
    return os.getenv("AGY_CLI_MODEL", "").strip() or default


def run(
    prompt: str,
    *,
    model: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    stage: str = "",
    json_schema: dict | None = None,
) -> ClaudeResult:
    """Run *prompt* through the Antigravity CLI. Never raises — returns ok=False.

    Failure is always recoverable by design: every caller has a metered
    fallback, so a missing binary, a non-zero exit, a timeout, or an
    unparseable envelope must degrade rather than abort.

    When *json_schema* is given, ``text`` carries the CLI's own parsed
    ``structured_output`` re-serialized, not the raw model response — the
    schema is enforced upstream, so callers need no fence handling.
    """
    model = model or session_model()
    if not is_available():
        return ClaudeResult(ok=False, error=f"{BINARY} CLI not on PATH")

    # Prompt is an argv value, not stdin: `agy --print` *takes* the prompt, and
    # piping is rejected outright (see module docstring).
    cmd = [BINARY, "--print", prompt, "--output-format", "json",
           "--model", model]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult(ok=False, error=f"timed out after {timeout_s}s")
    except OSError as exc:
        return ClaudeResult(ok=False, error=f"could not run {BINARY}: {exc}")

    if proc.returncode != 0:
        return ClaudeResult(
            ok=False,
            error=f"rc={proc.returncode}: {(proc.stderr or '')[-300:]}",
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ClaudeResult(ok=False, error=f"non-JSON envelope from {BINARY}")

    if str(envelope.get("status", "")).upper() != "SUCCESS":
        return ClaudeResult(
            ok=False,
            error=f"status={envelope.get('status')!r}",
        )

    # Prefer the CLI's own schema-validated parse when we asked for one.
    structured = envelope.get("structured_output")
    if json_schema is not None and isinstance(structured, dict):
        text = json.dumps(structured)
    else:
        text = str(envelope.get("response", "") or "").strip()

    usage = envelope.get("usage") or {}
    # Cache reads are real input the model processed; counting only
    # `input_tokens` understates a cached call.
    in_tok = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_tokens", 0) or 0)
    )
    result = ClaudeResult(
        text=text,
        input_tokens=in_tok,
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        # Subscription-billed: there is no per-call charge to report.
        cost_usd=0.0,
        ok=True,
    )
    logger.info(
        "Antigravity route (%s): model=%s in=%d out=%d model_time=%.1fs "
        "(subscription, not metered)",
        stage or "agy-cli", model, result.input_tokens, result.output_tokens,
        float(envelope.get("duration_seconds", 0.0) or 0.0),
    )
    return result
