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

**Overhead.** Each invocation carries the harness's own system prompt, and
what else it carries depends on how it is invoked — measured table in
``docs/MODELS.md`` under "Per-call overhead". Two of the three things that
number is made of are the caller's to suppress, and ``run`` suppresses both:

- ``--tools ""`` drops the built-in tool definitions. It also makes the call
  a **pure completion by construction** — a scoring prompt that can call a
  tool is a scorer that may take a second turn, read whatever is around it,
  and bill twice for a verdict. ``turns`` is parsed back out so a regression
  is visible rather than merely expensive.
- ``cwd`` is a neutral directory, not the repo. The subprocess inherits its
  working directory, and inside a checkout that means the *coding agent's*
  instructions (CLAUDE.md, the bundled skill, memory) are loaded into a
  judgement about job listings.

(``--no-session-persistence`` rides along for hygiene: a one-shot completion
has nothing worth resuming, so it should not leave a session file behind.)

``--bare`` looks like it belongs on that list and does not: it returns
``is_error: true`` with no completion.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
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
    # Anything but 1 means the model did not answer in one pass — it called a
    # tool. Defaulted so `gemini_cli`, which shares this dataclass and has no
    # turn concept, constructs it unchanged.
    turns: int = 1


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
    fallback, so a missing binary, a non-zero exit, a timeout, an envelope
    reporting its own error, or an unparseable one must degrade rather than
    abort.
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
            ["claude", "-p", "--model", model, "--output-format", "json",
             # One completion, no tools, no session file. See module docstring.
             "--tools", "", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, timeout=timeout_s,
            # Never the repo: the subprocess would inherit its CLAUDE.md.
            cwd=tempfile.gettempdir(),
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

    # A refused invocation exits 0 and returns a well-formed envelope that says
    # so (`--bare` does exactly this). Without the check it parses as a
    # successful call with an empty completion — silent, and indistinguishable
    # from a model that answered nothing.
    if envelope.get("is_error"):
        detail = str(envelope.get("result") or envelope.get("subtype") or "")
        return ClaudeResult(
            ok=False, error=f"is_error from claude CLI: {detail[:300]}",
        )

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
        turns=int(envelope.get("num_turns", 1) or 1),
    )
    logger.info(
        "Session route (%s): model=%s turns=%d in=%d out=%d "
        "subscription_cost=$%.4f (not metered)",
        stage or "claude-cli", model, result.turns, result.input_tokens,
        result.output_tokens, result.cost_usd,
    )
    if result.turns != 1:
        logger.warning(
            "Session route (%s): %d turns — the call was not a pure "
            "completion, so something ran a tool despite --tools \"\"",
            stage or "claude-cli", result.turns,
        )
    return result
