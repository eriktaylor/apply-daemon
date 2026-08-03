"""Subscription-billed model calls through the Claude Code CLI.

`claude -p --model X --output-format json` starts its own headless session,
so unattended code — autopilot inside `script.sh`, the eval harness — can
reach a Claude model without spending metered OpenRouter credit. This is the
only membership route available to code that has no calling session to hand
a prompt back to (which is what `cli tailor`'s emit/apply handshake does).

**Cost model.** The CLI reports real usage including cache hits, which list
pricing cannot show. That spend is subscription-billed, so it is deliberately
NOT written to ``logs/model_usage.log``: that file is the basis for the
spend ceiling in ``src/budget.py``, and counting membership work there would
make the daily cap refuse runs over money nobody was charged. Reported cost
is logged for visibility instead.

**Overhead.** Each invocation carries the harness's own system prompt
(~23k tokens, measured 2026-07-30). Irrelevant for a handful of large calls,
decisive against many small ones — which is why Stage 5's 100+ per-listing
calls stay on OpenRouter while the re-score does not.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300

# Route vocabulary shared with `cli tailor --via` so the surfaces agree on
# what "session" and "api" mean.
ROUTE_SESSION = "session"
ROUTE_API = "api"


@dataclass(frozen=True)
class ClaudeResult:
    """One CLI call. ``ok`` false means the caller should fall back."""

    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    ok: bool = False
    error: str = ""


def is_available() -> bool:
    """True when the `claude` binary is on PATH.

    Callers must check: the CLI is present in a developer shell and absent
    from a bare cron environment, and a missing binary should fall back to
    the metered route rather than fail the run.
    """
    return shutil.which("claude") is not None


def session_model(default: str = "sonnet") -> str:
    """Which model the session route uses (``CLAUDE_CLI_MODEL``)."""
    return os.getenv("CLAUDE_CLI_MODEL", "").strip() or default


def strip_fence(text: str) -> str:
    """Return the JSON object inside a markdown fence, if there is one."""
    text = (text or "").strip()
    if not text.startswith("```"):
        return text
    for part in text.split("```"):
        cleaned = part.removeprefix("json").strip()
        if cleaned.startswith("{"):
            return cleaned
    return text


def run(
    prompt: str,
    *,
    model: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    stage: str = "",
) -> ClaudeResult:
    """Run *prompt* through the Claude CLI. Never raises — returns ok=False.

    Failure is always recoverable by design: every caller has a metered
    fallback, so a missing binary, a non-zero exit, a timeout, or an
    unparseable envelope must degrade rather than abort.
    """
    model = model or session_model()
    if not is_available():
        return ClaudeResult(ok=False, error="claude CLI not on PATH")

    # Prompt goes on stdin, not argv: passing it as an argument fails without
    # a TTY (backgrounded runs error with "Input must be provided either
    # through stdin or as a prompt argument"), and these prompts are long
    # enough to risk ARG_MAX.
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json"],
            input=prompt, capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult(ok=False, error=f"timed out after {timeout_s}s")
    except OSError as exc:
        return ClaudeResult(ok=False, error=f"could not run claude: {exc}")

    if proc.returncode != 0:
        return ClaudeResult(
            ok=False,
            error=f"rc={proc.returncode}: {(proc.stderr or '')[-300:]}",
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ClaudeResult(ok=False, error="non-JSON envelope from claude CLI")

    usage = envelope.get("usage") or {}
    # Cache reads/creations are real input the model processed; counting only
    # `input_tokens` understates a cached call by orders of magnitude.
    in_tok = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
    )
    result = ClaudeResult(
        text=strip_fence(envelope.get("result", "") or ""),
        input_tokens=in_tok,
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cost_usd=float(envelope.get("total_cost_usd", 0.0) or 0.0),
        ok=True,
    )
    logger.info(
        "Session route (%s): model=%s in=%d out=%d subscription_cost=$%.4f "
        "(not metered)",
        stage or "claude-cli", model, result.input_tokens,
        result.output_tokens, result.cost_usd,
    )
    return result
