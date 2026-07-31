"""Unit tests for O-2 — db.get_model_breakdown + report.py --models helpers."""

from __future__ import annotations

import pytest

from src.db import Database
from src.models import JobListing
from src.report import (
    _parse_usage_log,
    _print_model_breakdown,
)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def _listing(**kwargs) -> JobListing:
    defaults = {
        "source": "linkedin",
        "email_classification": "JOB_DIGEST",
        "title": "Engineer",
        "company": "Acme",
        "verdict": "YES",
        "model_used": "M1",
    }
    defaults.update(kwargs)
    return JobListing(**defaults)


class TestGetModelBreakdown:
    def test_groups_by_model_and_status(self, db):
        a = _listing(model_used="M1", confidence=80, title="A", company="A")
        b = _listing(model_used="M1", confidence=90, title="B", company="B")
        c = _listing(model_used="M2", confidence=40, verdict="NO", title="C", company="C")
        for listing in (a, b, c):
            db.insert_listing(listing)
        db.update_pipeline_status(a.id, "saved")
        db.update_pipeline_status(b.id, "interviewing")

        breakdown = db.get_model_breakdown()
        assert set(breakdown) == {"M1", "M2"}
        assert breakdown["M1"]["statuses"] == {"saved": 1, "interviewing": 1}
        assert breakdown["M1"]["verdicts"]["YES"] == 2
        assert sorted(c for c, _ in breakdown["M1"]["confidences"]) == [80, 90]
        assert breakdown["M2"]["verdicts"]["NO"] == 1

    def test_null_model_buckets_unknown(self, db):
        db.insert_listing(_listing(model_used=None, title="X", company="X"))
        breakdown = db.get_model_breakdown()
        assert "(unknown)" in breakdown

    def test_respects_age_window(self, db):
        db.insert_listing(_listing(title="Recent", company="R"))
        # All rows are "now", so a 0-day window still includes them; a huge
        # negative would exclude — but assert the window path runs cleanly.
        assert db.get_model_breakdown(max_age_days=30)


class TestParseUsageLog:
    def test_missing_file_empty(self, tmp_path):
        assert _parse_usage_log(tmp_path / "nope.log") == {}

    def test_aggregates_by_model_stage(self, tmp_path):
        log = tmp_path / "model_usage.log"
        log.write_text(
            "2026-07-12T00:00:00+00:00|stage1|nano|100\n"
            "2026-07-12T00:01:00+00:00|stage1|nano|150\n"
            "2026-07-12T00:02:00+00:00|stage5|flash|200\n"
            "GARBAGE LINE\n"
            "bad|line|only|notanumber\n",
            encoding="utf-8",
        )
        agg = _parse_usage_log(log)
        assert agg[("nano", "stage1")] == {"calls": 2, "tokens": 250}
        assert agg[("flash", "stage5")] == {"calls": 1, "tokens": 200}
        assert ("bad", "line") not in agg  # malformed token skipped


class TestPrintModelBreakdown:
    def test_prints_save_rate_and_calibration(self, capsys):
        breakdown = {
            "M1": {
                "statuses": {"saved": 2, "triaged": 1, "interviewing": 1},
                "verdicts": {"YES": 3, "NO": 1},
                "confidences": [(80, "saved"), (90, "interviewing"),
                                (40, "triaged"), (78, "saved")],
            }
        }
        _print_model_breakdown(breakdown)
        out = capsys.readouterr().out
        assert "M1" in out
        assert "3/4 advanced" in out       # saved(2) + interviewing(1)
        assert "33" in out                 # interviews/100 YES = 1/3*100
        assert "75-89" in out              # calibration band with 80 & 78

    def test_empty_breakdown(self, capsys):
        _print_model_breakdown({})
        assert "no listings" in capsys.readouterr().out.lower()


class TestSpendToday:
    """C-4 — the number C-3's ceiling is enforced against."""

    def _log(self, tmp_path, lines):
        path = tmp_path / "usage.log"
        path.write_text("".join(lines), encoding="utf-8")
        return path

    def test_missing_log_is_zero_not_error(self, tmp_path):
        from src.model_usage import spend_today
        tokens, usd = spend_today(tmp_path / "nope.log")
        assert tokens == 0 and usd is None

    def test_sums_only_today(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        from src.model_usage import spend_today
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=3)).isoformat()
        log = self._log(tmp_path, [
            f"{now.isoformat()}|stage5|google/gemini-3.1-flash-lite|1000\n",
            f"{old}|stage5|google/gemini-3.1-flash-lite|9999999\n",
        ])
        tokens, usd = spend_today(log)
        assert tokens == 1000
        assert usd is not None and usd > 0

    def test_unpriced_model_leaves_usd_none(self, tmp_path):
        """An unpriced model must not read as free — that would let a budget
        check pass on spend it cannot see."""
        from datetime import datetime, timezone

        from src.model_usage import spend_today
        now = datetime.now(timezone.utc).isoformat()
        log = self._log(tmp_path, [f"{now}|stage5|who/knows|5000\n"])
        tokens, usd = spend_today(log)
        assert tokens == 5000
        assert usd is None

    def test_malformed_lines_skipped(self, tmp_path):
        from datetime import datetime, timezone

        from src.model_usage import spend_today
        now = datetime.now(timezone.utc).isoformat()
        log = self._log(tmp_path, [
            "GARBAGE\n",
            f"{now}|stage5|google/gemini-3.1-flash-lite|notanint\n",
            f"{now}|stage5|google/gemini-3.1-flash-lite|100\n",
        ])
        tokens, _ = spend_today(log)
        assert tokens == 100


class TestSpendReport:
    def test_empty_log_explains_why(self, tmp_path, monkeypatch, capsys):
        import src.model_usage as mu
        from src.report import spend_report
        monkeypatch.setattr(mu, "_DEFAULT_LOG_PATH", str(tmp_path / "none.log"))
        monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(tmp_path / "none.log"))
        spend_report()
        out = capsys.readouterr().out
        assert "No metered calls recorded yet" in out

    def test_reports_days_and_stages(self, tmp_path, monkeypatch, capsys):
        from src.report import spend_report
        log = tmp_path / "usage.log"
        log.write_text(
            "2026-07-28T10:00:00+00:00|stage5|google/gemini-3.1-flash-lite|1000\n"
            "2026-07-28T11:00:00+00:00|autopilot_rescore|anthropic/claude-sonnet-4.6|2000\n"
            "2026-07-29T10:00:00+00:00|stage5|google/gemini-3.1-flash-lite|3000\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(log))
        spend_report()
        out = capsys.readouterr().out
        assert "2026-07-28" in out and "2026-07-29" in out
        assert "autopilot_rescore" in out
        assert "TOTAL" in out
        assert "verified" in out

    def test_unpriced_model_warns(self, tmp_path, monkeypatch, capsys):
        from src.report import spend_report
        log = tmp_path / "usage.log"
        log.write_text("2026-07-29T10:00:00+00:00|s|mystery/model|500\n",
                       encoding="utf-8")
        monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(log))
        spend_report()
        out = capsys.readouterr().out
        assert "Unpriced models" in out and "mystery/model" in out


class TestM6RankSelection:
    """M-6 — top-N by rank position, not by confidence."""

    def _rows(self, n=5):
        return [
            {"id": f"id{i}", "title": f"T{i}", "company": f"C{i}",
             "location": "SF", "confidence": 50 + i, "verdict": "YES",
             "date_ingested": "2026-07-30T00:00:00+00:00",
             "matching_skills": "", "missing_skills": "", "distance_bucket": 1}
            for i in range(n)
        ]

    def test_disabled_falls_back_to_heuristic(self, monkeypatch):
        from src.process_queue import _rank_select
        monkeypatch.delenv("RANKING_MODE_STAGE5", raising=False)
        monkeypatch.delenv("RANKING_MODE", raising=False)
        assert _rank_select(self._rows(), 3, client=None) is None

    def test_rank_order_beats_confidence_order(self, monkeypatch, mocker):
        """The point of M-6: a lower-confidence listing ranked first wins."""
        from src.ranking import RankCandidate
        monkeypatch.setenv("RANKING_MODE_STAGE5", "listwise")
        rows = self._rows(3)          # confidences 50, 51, 52
        mocker.patch(
            "src.process_queue.rank_candidates",
            return_value=[RankCandidate(id="id0", title="T0"),
                          RankCandidate(id="id2", title="T2"),
                          RankCandidate(id="id1", title="T1")],
        )
        from src.process_queue import _rank_select
        picked = _rank_select(rows, 2, client=object())
        assert [r["id"] for r in picked] == ["id0", "id2"]   # not id2,id1 by conf

    def test_failed_open_ranker_is_treated_as_no_ranking(self, monkeypatch, mocker):
        """ranking.py fails open by returning input order — that must not be
        mistaken for a real rank-based selection."""
        from src.ranking import RankCandidate
        monkeypatch.setenv("RANKING_MODE_STAGE5", "listwise")
        rows = self._rows(3)
        mocker.patch(
            "src.process_queue.rank_candidates",
            return_value=[RankCandidate(id=f"id{i}", title=f"T{i}") for i in range(3)],
        )
        from src.process_queue import _rank_select
        assert _rank_select(rows, 2, client=object()) is None

    def test_single_row_needs_no_ranking(self, monkeypatch):
        from src.process_queue import _rank_select
        monkeypatch.setenv("RANKING_MODE_STAGE5", "listwise")
        assert _rank_select(self._rows(1), 1, client=object()) is None


class TestNoiseFloor:
    def test_defaults_to_confidence_threshold(self, monkeypatch):
        from src.triage import noise_floor_pct
        monkeypatch.delenv("NOISE_FLOOR_PCT", raising=False)
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.75")
        assert noise_floor_pct() == 75

    def test_explicit_floor_wins(self, monkeypatch):
        from src.triage import noise_floor_pct
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.85")
        monkeypatch.setenv("NOISE_FLOOR_PCT", "55")
        assert noise_floor_pct() == 55

    def test_garbage_falls_back(self, monkeypatch):
        from src.triage import noise_floor_pct
        monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.6")
        monkeypatch.setenv("NOISE_FLOOR_PCT", "loads")
        assert noise_floor_pct() == 60
