"""Tests for src/budget.py — C-3 spend ceilings.

The guard being tested is against *continuous or heavy* spend. The cooldown
tests matter most: a looping agent is the realistic runaway path, and no
per-run ceiling catches it. Synthetic fixtures only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.budget import (
    BudgetDecision,
    check_run_allowed,
    daily_budget_usd,
    last_run_at,
    min_run_interval_minutes,
    record_run,
    run_estimate_usd,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point both ledgers at tmp so no test touches logs/."""
    monkeypatch.setenv("RUN_LOG_PATH", str(tmp_path / "run_log"))
    monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(tmp_path / "usage.log"))
    for key in ("DAILY_USD_BUDGET", "MIN_RUN_INTERVAL_MINUTES",
                "RUN_USD_ESTIMATE", "BUDGET_ALLOW_UNPRICED"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _usage(tmp_path, model="google/gemini-3.1-flash-lite", tokens=1000,
           when=None):
    when = when or datetime.now(timezone.utc)
    path = tmp_path / "usage.log"
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{when.isoformat()}|stage5|{model}|{tokens}\n")


class TestConfig:
    def test_defaults(self, monkeypatch):
        assert daily_budget_usd() == 3.0
        assert min_run_interval_minutes() == 60
        assert run_estimate_usd() is None

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("DAILY_USD_BUDGET", "12.5")
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "5")
        monkeypatch.setenv("RUN_USD_ESTIMATE", "0.75")
        assert daily_budget_usd() == 12.5
        assert min_run_interval_minutes() == 5
        assert run_estimate_usd() == 0.75

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DAILY_USD_BUDGET", "lots")
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "soon")
        monkeypatch.setenv("RUN_USD_ESTIMATE", "cheap")
        assert daily_budget_usd() == 3.0
        assert min_run_interval_minutes() == 60
        assert run_estimate_usd() is None


class TestRunLog:
    def test_no_runs_yet(self):
        assert last_run_at() is None

    def test_record_and_read_back(self):
        record_run("test")
        stamp = last_run_at()
        assert stamp is not None
        assert (datetime.now(timezone.utc) - stamp).total_seconds() < 60

    def test_returns_most_recent(self, _isolate):
        path = _isolate / "run_log"
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        path.write_text(f"{old}|old\n", encoding="utf-8")
        record_run("new")
        assert last_run_at().date() == datetime.now(timezone.utc).date()

    def test_malformed_lines_ignored(self, _isolate):
        path = _isolate / "run_log"
        good = datetime.now(timezone.utc).isoformat()
        path.write_text(f"GARBAGE\n\n{good}|cli\nnot-a-date|cli\n",
                        encoding="utf-8")
        assert last_run_at() is not None

    def test_unwritable_path_does_not_raise(self, monkeypatch, tmp_path):
        # Bookkeeping must never break an otherwise-allowed run.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        monkeypatch.setenv("RUN_LOG_PATH", str(blocker / "sub" / "run_log"))
        record_run("test")  # no exception


class TestCooldown:
    """The guard that actually stops a looping agent."""

    def test_blocks_inside_interval(self, monkeypatch):
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        record_run()
        decision = check_run_allowed()
        assert decision.allowed is False
        assert "Cooldown" in decision.reason

    def test_allows_after_interval(self, monkeypatch, _isolate):
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        (_isolate / "run_log").write_text(f"{old}|cli\n", encoding="utf-8")
        assert check_run_allowed().allowed is True

    def test_zero_interval_disables(self, monkeypatch):
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "0")
        record_run()
        assert check_run_allowed().allowed is True

    def test_first_ever_run_allowed(self):
        assert check_run_allowed().allowed is True

    def test_cooldown_beats_budget_in_reporting(self, monkeypatch, _isolate):
        """Both blocked → report the cooldown, the more actionable one."""
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        monkeypatch.setenv("DAILY_USD_BUDGET", "0.0001")
        _usage(_isolate, tokens=999999)
        record_run()
        assert "Cooldown" in check_run_allowed().reason


class TestDailyCeiling:
    def test_allows_under_budget(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "10")
        _usage(_isolate, tokens=1000)
        decision = check_run_allowed()
        assert decision.allowed is True
        assert decision.remaining_usd is not None

    def test_blocks_at_budget(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "0.01")
        _usage(_isolate, tokens=500_000)
        decision = check_run_allowed()
        assert decision.allowed is False
        assert "Daily budget reached" in decision.reason

    def test_yesterday_does_not_count(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "0.01")
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        _usage(_isolate, tokens=500_000, when=yesterday)
        assert check_run_allowed().allowed is True

    def test_zero_budget_disables(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "0")
        _usage(_isolate, tokens=500_000)
        decision = check_run_allowed()
        assert decision.allowed is True
        assert "unlimited" in decision.reason


class TestUnpricedSpend:
    """A budget check must never pass on spend it cannot see."""

    def test_unpriced_model_blocks(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "10")
        _usage(_isolate, model="who/knows", tokens=5000)
        decision = check_run_allowed()
        assert decision.allowed is False
        assert "could not be priced" in decision.reason

    def test_override_allows(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "10")
        monkeypatch.setenv("BUDGET_ALLOW_UNPRICED", "true")
        _usage(_isolate, model="who/knows", tokens=5000)
        assert check_run_allowed().allowed is True

    def test_no_spend_at_all_is_not_unknown(self, monkeypatch):
        monkeypatch.setenv("DAILY_USD_BUDGET", "10")
        assert check_run_allowed().allowed is True


class TestProjection:
    def test_blocks_when_estimate_would_exceed(self, monkeypatch, _isolate):
        # Rate-independent: any non-zero spend plus a 0.99 estimate breaches a
        # 1.00 ceiling. Pinning it to a specific token cost made this fail when
        # the output-fraction assumption was corrected.
        monkeypatch.setenv("DAILY_USD_BUDGET", "1.00")
        monkeypatch.setenv("RUN_USD_ESTIMATE", "0.99")
        _usage(_isolate, tokens=300_000)
        decision = check_run_allowed()
        assert decision.allowed is False
        assert "Projected over budget" in decision.reason

    def test_allows_when_estimate_fits(self, monkeypatch, _isolate):
        monkeypatch.setenv("DAILY_USD_BUDGET", "10")
        monkeypatch.setenv("RUN_USD_ESTIMATE", "0.50")
        _usage(_isolate, tokens=1000)
        assert check_run_allowed().allowed is True

    def test_absent_estimate_does_not_block(self, monkeypatch, _isolate):
        """No estimate means 'cannot project' — not 'assume the worst'."""
        monkeypatch.setenv("DAILY_USD_BUDGET", "0.20")
        _usage(_isolate, tokens=1000)
        decision = check_run_allowed()
        assert decision.allowed is True
        assert decision.projected_usd is None


class TestDecisionShape:
    def test_remaining_none_without_budget(self):
        d = BudgetDecision(allowed=True, spent_usd=1.0, budget_usd=0)
        assert d.remaining_usd is None

    def test_remaining_never_negative(self):
        d = BudgetDecision(allowed=False, spent_usd=5.0, budget_usd=3.0)
        assert d.remaining_usd == 0.0
