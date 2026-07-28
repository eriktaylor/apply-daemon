"""Unit tests for eval/preference_pairs.py — E-3 preference-pair extraction."""

from __future__ import annotations

import json

from eval.preference_pairs import (
    _polarity,
    build_pairs,
    load_labels,
    summarize,
)


def _sig(job_id, verdict, confidence, day):
    return {
        "id": job_id,
        "verdict": verdict,
        "confidence": confidence,
        "date_ingested": f"{day}T12:00:00+00:00",
    }


class TestPolarity:
    def test_positive_actions(self):
        assert _polarity({"save"}) == "positive"
        assert _polarity({"tailor"}) == "positive"
        assert _polarity({"coverletter"}) == "positive"

    def test_negative_actions(self):
        assert _polarity({"pass"}) == "negative"
        assert _polarity({"rejected"}) == "negative"

    def test_positive_wins_tie(self):
        assert _polarity({"pass", "tailor"}) == "positive"

    def test_neutral_only_is_none(self):
        assert _polarity({"questions", "answer"}) is None


class TestLoadLabels:
    def test_reads_polarity_from_ledger(self, tmp_path):
        path = tmp_path / "human_labels.jsonl"
        lines = [
            {"job_id": "a", "human_reaction": "save", "listing": {}},
            {"job_id": "b", "human_reaction": "pass", "listing": {}},
            {"job_id": "c", "human_reaction": "questions", "listing": {}},
        ]
        path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        polarity = load_labels(path)
        assert polarity == {"a": "positive", "b": "negative"}

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_labels(tmp_path / "nope.jsonl") == {}

    def test_tolerates_bad_lines(self, tmp_path):
        path = tmp_path / "labels.jsonl"
        path.write_text('{"job_id": "a", "human_reaction": "save"}\nGARBAGE\n', encoding="utf-8")
        assert load_labels(path) == {"a": "positive"}


class TestBuildPairs:
    def test_strong_pair_positive_vs_negative(self):
        polarity = {"win": "positive", "lose": "negative"}
        signals = [
            _sig("win", "YES", 80, "2026-07-01"),
            _sig("lose", "YES", 60, "2026-07-01"),
        ]
        pairs = build_pairs(polarity, signals)
        assert len(pairs) == 1
        assert pairs[0].strength == "strong"
        assert pairs[0].preferred_id == "win"
        assert pairs[0].other_id == "lose"

    def test_weak_pair_positive_vs_unreacted(self):
        polarity = {"win": "positive"}
        signals = [
            _sig("win", "MAYBE", 70, "2026-07-01"),
            _sig("ignored", "MAYBE", 65, "2026-07-01"),
        ]
        pairs = build_pairs(polarity, signals)
        assert len(pairs) == 1
        assert pairs[0].strength == "weak"

    def test_no_pair_across_verdict_tiers(self):
        polarity = {"win": "positive", "lose": "negative"}
        signals = [
            _sig("win", "YES", 80, "2026-07-01"),
            _sig("lose", "MAYBE", 60, "2026-07-01"),  # different tier
        ]
        assert build_pairs(polarity, signals) == []

    def test_no_pair_across_batches(self):
        polarity = {"win": "positive", "lose": "negative"}
        signals = [
            _sig("win", "YES", 80, "2026-07-01"),
            _sig("lose", "YES", 60, "2026-07-02"),  # different day
        ]
        assert build_pairs(polarity, signals) == []

    def test_two_positives_make_no_pair(self):
        polarity = {"a": "positive", "b": "positive"}
        signals = [
            _sig("a", "YES", 80, "2026-07-01"),
            _sig("b", "YES", 70, "2026-07-01"),
        ]
        assert build_pairs(polarity, signals) == []

    def test_untiered_listing_skipped(self):
        polarity = {"win": "positive", "lose": "negative"}
        signals = [
            _sig("win", "YES", 80, "2026-07-01"),
            _sig("lose", "", 60, "2026-07-01"),  # no verdict
        ]
        assert build_pairs(polarity, signals) == []


class TestSummarize:
    def test_counts_and_breakdown(self):
        polarity = {"win": "positive", "lose": "negative", "win2": "positive"}
        signals = [
            _sig("win", "YES", 80, "2026-07-01"),
            _sig("lose", "YES", 60, "2026-07-01"),
            _sig("win2", "MAYBE", 70, "2026-07-01"),
            _sig("cold", "MAYBE", 50, "2026-07-01"),  # un-reacted
        ]
        summary = summarize(build_pairs(polarity, signals))
        assert summary["strong"] == 1  # win > lose (YES)
        assert summary["weak"] == 1    # win2 > cold (MAYBE)
        assert summary["by_verdict"]["YES"]["strong"] == 1
        assert summary["by_verdict"]["MAYBE"]["weak"] == 1
