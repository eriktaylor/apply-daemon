"""Spend ceilings for agent-triggered pipeline runs (C-3).

Guards the failure mode that matters: **continuous or heavy spend**, not any
single small decision. The agent may spend freely under the ceiling; what it
cannot do is exceed a daily cap or fire runs back-to-back.

Three guards, in order of how exactly they can be enforced:

1. **Cooldown** (`MIN_RUN_INTERVAL_MINUTES`) — exact. Reads the run log.
   This is the guard that stops a looping agent, and it is the only one that
   catches a run which spends nothing per attempt but still scrapes, hits
   IMAP, and burns proxy bandwidth.
2. **Daily ceiling** (`DAILY_USD_BUDGET`) — exact for what has already been
   spent. Reads `spend_today()`.
3. **Projection** (`RUN_USD_ESTIMATE`) — approximate by construction: the
   cost of a run cannot be known before it runs, so a historical average is
   used to ask "would starting this push me over?". Labelled as an estimate
   everywhere it surfaces.

Refuse-and-report, never truncate: a half-finished batch with no explanation
reads as a broken pipeline, which is worse than a run that didn't start.

The ceiling is denominated in **USD**, which is what the user actually cares
about, and is only meaningful because `eval/model_pricing.py` is verified.
When today's spend contains an unpriced model the cost is *unknown*, not
zero — and an unknown blocks by default, because a budget check must never
pass on spend it cannot see. Set ``BUDGET_ALLOW_UNPRICED=true`` to override.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.model_usage import spend_today

logger = logging.getLogger(__name__)

_DEFAULT_RUN_LOG = "logs/run_log"

# Defaults sized from a measured batch at AUTOPILOT_TOP_N=3 (~$0.48/run):
# a few runs a day is fine, a loop is not.
_DEFAULT_DAILY_USD = 3.0
_DEFAULT_INTERVAL_MINUTES = 60


def _run_log_path() -> Path:
    return Path(os.getenv("RUN_LOG_PATH", _DEFAULT_RUN_LOG))


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def daily_budget_usd() -> float:
    """Daily ceiling in USD. 0 or negative disables the check."""
    return _float_env("DAILY_USD_BUDGET", _DEFAULT_DAILY_USD)


def min_run_interval_minutes() -> int:
    """Cooldown between runs. 0 or negative disables the check."""
    return _int_env("MIN_RUN_INTERVAL_MINUTES", _DEFAULT_INTERVAL_MINUTES)


def record_run(trigger: str = "cli") -> None:
    """Append a run marker. Called by whatever fires the pipeline (C-2).

    Deliberately separate from `model_usage.log`: that records *calls*, and a
    run which scrapes but triages nothing makes no calls while still doing
    real work. Deriving the cooldown from call timestamps would let exactly
    that run be repeated without limit.
    """
    path = _run_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp}|{trigger}\n")
    except OSError:
        # Never let bookkeeping break a run that is otherwise allowed.
        logger.warning("Could not append to run log at %s", path, exc_info=True)


def last_run_at() -> datetime | None:
    """Timestamp of the most recent recorded run, or None."""
    path = _run_log_path()
    if not path.exists():
        return None
    last = None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stamp = line.strip().split("|")[0]
            if not stamp:
                continue
            try:
                last = datetime.fromisoformat(stamp)
            except ValueError:
                continue
    except OSError:
        return None
    return last


def run_estimate_usd() -> float | None:
    """Expected USD cost of one run.

    ``RUN_USD_ESTIMATE`` when set, else None. Intentionally not inferred from
    history here: run boundaries are only meaningful once C-2 records them,
    and a wrong auto-estimate silently mis-gates runs. None means "cannot
    project", which the caller reports rather than guesses around.
    """
    raw = os.getenv("RUN_USD_ESTIMATE", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("RUN_USD_ESTIMATE=%r is not a number; ignoring", raw)
        return None


@dataclass
class BudgetDecision:
    """Outcome of a pre-run budget check."""

    allowed: bool
    reason: str = ""
    spent_usd: float | None = None
    spent_tokens: int = 0
    budget_usd: float = 0.0
    minutes_since_run: float | None = None
    projected_usd: float | None = None

    @property
    def remaining_usd(self) -> float | None:
        if self.spent_usd is None or self.budget_usd <= 0:
            return None
        return max(0.0, self.budget_usd - self.spent_usd)


def check_run_allowed(*, now: datetime | None = None) -> BudgetDecision:
    """Decide whether a pipeline run may start.

    Checks run cheapest-and-most-certain first, so the reported reason is the
    most actionable one rather than whichever fired first by accident.
    """
    now = now or datetime.now(timezone.utc)
    tokens, usd = spend_today()
    budget = daily_budget_usd()
    interval = min_run_interval_minutes()

    minutes_since: float | None = None
    last = last_run_at()
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        minutes_since = (now - last).total_seconds() / 60.0

    base = BudgetDecision(
        allowed=True, spent_usd=usd, spent_tokens=tokens, budget_usd=budget,
        minutes_since_run=minutes_since,
    )

    # 1. Cooldown — the guard against a looping agent.
    if interval > 0 and minutes_since is not None and minutes_since < interval:
        wait = interval - minutes_since
        base.allowed = False
        base.reason = (
            f"Cooldown: last run was {minutes_since:.0f} min ago; "
            f"{wait:.0f} min left of the {interval}-min minimum interval."
        )
        return base

    if budget <= 0:
        base.reason = "No daily budget configured (DAILY_USD_BUDGET=0) — unlimited."
        return base

    # 2. Unknown spend blocks — never pass a check on cost we cannot see.
    if usd is None and tokens > 0:
        if _bool_env("BUDGET_ALLOW_UNPRICED"):
            base.reason = (
                f"{tokens:,} tokens spent today are unpriced; allowed by "
                "BUDGET_ALLOW_UNPRICED."
            )
            return base
        base.allowed = False
        base.reason = (
            f"{tokens:,} tokens spent today could not be priced — add the "
            "model to eval/model_pricing.py, or set BUDGET_ALLOW_UNPRICED=true."
        )
        return base

    spent = usd or 0.0

    # 3. Already over.
    if spent >= budget:
        base.allowed = False
        base.reason = (
            f"Daily budget reached: ${spent:.2f} of ${budget:.2f} spent today."
        )
        return base

    # 4. Projection — approximate, and labelled as such.
    estimate = run_estimate_usd()
    base.projected_usd = estimate
    if estimate is not None and spent + estimate > budget:
        base.allowed = False
        base.reason = (
            f"Projected over budget: ${spent:.2f} spent + ~${estimate:.2f} "
            f"estimated for this run exceeds ${budget:.2f}."
        )
        return base

    base.reason = f"${spent:.2f} of ${budget:.2f} used today."
    return base
