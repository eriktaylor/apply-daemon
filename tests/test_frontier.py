"""Unit tests for eval/frontier.py — E-2 Pareto-frontier report."""

from __future__ import annotations

from eval.frontier import (
    Point,
    _to_float,
    build_points,
    load_runs,
    pareto_frontier,
)


def _p(model, acc, cost, others=("s1",)):
    return Point(model=model, accuracy=acc, cost=cost, n_runs=1, other_models=set(others))


class TestToFloat:
    def test_parses(self):
        assert _to_float("0.85") == 0.85

    def test_garbage_is_none(self):
        assert _to_float("") is None
        assert _to_float("abc") is None
        assert _to_float(None) is None


class TestParetoFrontier:
    def test_dominated_point_excluded(self):
        # cheap-and-accurate dominates expensive-and-worse
        good = _p("good", 0.90, 1.0)
        bad = _p("bad", 0.80, 2.0)
        assert pareto_frontier([good, bad]) == {"good"}

    def test_tradeoff_points_both_on_frontier(self):
        cheap = _p("cheap", 0.80, 1.0)
        accurate = _p("accurate", 0.95, 3.0)
        assert pareto_frontier([cheap, accurate]) == {"cheap", "accurate"}

    def test_unpriced_excluded(self):
        priced = _p("priced", 0.90, 1.0)
        unpriced = _p("unpriced", 0.99, None)
        assert pareto_frontier([priced, unpriced]) == {"priced"}

    def test_equal_accuracy_cheaper_wins(self):
        cheap = _p("cheap", 0.90, 1.0)
        pricey = _p("pricey", 0.90, 2.0)
        assert pareto_frontier([cheap, pricey]) == {"cheap"}


class TestBuildPoints:
    def _rows(self):
        from eval.frontier import RunRow
        return [
            RunRow("nano", "flash", 0.8, 0.9, 1000, 0.10, "False", "2026-07-12"),
            RunRow("nano", "flash", 0.9, 0.7, 2000, 0.20, "False", "2026-07-12"),
            RunRow("nano", "gpt4o", 0.85, 0.95, 1500, 0.50, "False", "2026-07-12"),
        ]

    def test_averages_runs_per_model(self):
        points = build_points(
            self._rows(),
            model_of=lambda r: r.stage5_model,
            acc_of=lambda r: r.verdict_accuracy,
            cost_of=lambda r: r.cost_per_1k,
            other_of=lambda r: r.stage1_model,
        )
        flash = next(p for p in points if p.model == "flash")
        # two flash runs: verdict 0.9 & 0.7 → 0.8; cost 0.10 & 0.20 → 0.15
        assert abs(flash.accuracy - 0.8) < 1e-9
        assert abs(flash.cost - 0.15) < 1e-9
        assert flash.n_runs == 2

    def test_tracks_other_stage_slugs(self):
        points = build_points(
            self._rows(),
            model_of=lambda r: r.stage1_model,
            acc_of=lambda r: r.extraction_accuracy,
            cost_of=lambda r: r.avg_tokens,
            other_of=lambda r: r.stage5_model,
        )
        nano = next(p for p in points if p.model == "nano")
        # nano ran against both flash and gpt4o at Stage 5 — confound signal
        assert nano.other_models == {"flash", "gpt4o"}


class TestLoadRuns:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_runs(tmp_path / "nope.csv") == []

    def test_reads_rows(self, tmp_path):
        path = tmp_path / "runs.csv"
        path.write_text(
            "timestamp,dataset,stage1_model,stage5_model,n_emails,runs_per_email,"
            "extraction_accuracy,verdict_accuracy,avg_tokens,cost_per_1k_listings,"
            "pricing_verified,pricing_last_updated\n"
            "2026-07-12T00:00:00,eval.csv,nano,flash,10,1,0.80,0.90,1200,0.15,"
            "False,2026-07-12\n",
            encoding="utf-8",
        )
        rows = load_runs(path)
        assert len(rows) == 1
        assert rows[0].stage1_model == "nano"
        assert rows[0].verdict_accuracy == 0.90
        assert rows[0].cost_per_1k == 0.15
