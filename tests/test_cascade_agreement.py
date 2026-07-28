"""Unit tests for eval/cascade_agreement.py — O-3 cascade agreement."""

from __future__ import annotations

import json

from eval.cascade_agreement import (
    _fuzzy_overlap,
    _parse_skill_list,
    analyze_pair,
    summarize,
    walk,
)


class TestFuzzyOverlap:
    def test_none_when_both_empty(self):
        assert _fuzzy_overlap(set(), set()) is None

    def test_zero_when_one_side_empty(self):
        assert _fuzzy_overlap({"python"}, set()) == 0.0

    def test_tag_matches_descriptive_phrase(self):
        # The real-data case: terse tag vs. verbose phrase for the same skill.
        terse = {"ai evaluation"}
        verbose = {"ai evaluation and model evaluation (if role description is accurate)"}
        assert _fuzzy_overlap(terse, verbose) == 1.0

    def test_unrelated_skills_score_zero(self):
        assert _fuzzy_overlap({"welding"}, {"tax law"}) == 0.0


class TestParseSkillList:
    def test_parses_json_string_shape(self):
        # original_triage stores skills as a JSON-list *string*
        assert _parse_skill_list('["Python", "ML"]') == {"python", "ml"}

    def test_parses_already_list_shape(self):
        # auto_assets stores them as an actual list
        assert _parse_skill_list(["Python", "SQL"]) == {"python", "sql"}

    def test_handles_garbage(self):
        assert _parse_skill_list("not json") == set()
        assert _parse_skill_list(None) == set()


class TestAnalyzePair:
    def test_verdict_agreement_and_confidence_gap(self):
        orig = {
            "verdict": "MAYBE", "confidence": 70,
            "matching_skills": '["python"]', "missing_skills": '["go"]',
        }
        auto = {
            "post_research_verdict": "YES", "post_research_confidence": "85",
            "updated_skills_match": {"matching": ["python", "sql"], "missing": []},
        }
        rec = analyze_pair(orig, auto)
        assert rec["verdict_agree"] is False
        assert rec["confidence_gap"] == 15  # 85 - 70
        # orig {python} vs auto {python, sql}: python covered both ways, sql
        # unmatched → (1 + 1) / (1 + 2) ≈ 0.667 under fuzzy overlap.
        assert abs(rec["matching_overlap"] - 2 / 3) < 1e-9

    def test_none_when_verdict_missing(self):
        assert analyze_pair({"confidence": 70}, {"post_research_confidence": 80}) is None

    def test_confidence_gap_none_when_unparseable(self):
        orig = {"verdict": "YES", "confidence": "?"}
        auto = {"post_research_verdict": "YES", "post_research_confidence": 80}
        rec = analyze_pair(orig, auto)
        assert rec["verdict_agree"] is True
        assert rec["confidence_gap"] is None


class TestSummarize:
    def test_empty(self):
        assert summarize([]) == {"n": 0}

    def test_aggregates(self):
        records = [
            {"verdict_agree": True, "confidence_gap": 10,
             "matching_overlap": 1.0, "missing_overlap": None},
            {"verdict_agree": False, "confidence_gap": -4,
             "matching_overlap": 0.5, "missing_overlap": 0.0},
        ]
        summary = summarize(records)
        assert summary["n"] == 2
        assert summary["verdict_agreement_rate"] == 0.5
        assert summary["mean_confidence_gap"] == 3.0  # (10 + -4)/2
        assert summary["confidence_raised"] == 1
        assert summary["confidence_lowered"] == 1
        assert summary["mean_matching_overlap"] == 0.75


class TestWalk:
    def test_reads_folder_pairs(self, tmp_path):
        folder = tmp_path / "Acme_Engineer_abc12345"
        folder.mkdir()
        (folder / "original_triage.json").write_text(json.dumps({
            "verdict": "YES", "confidence": 80,
            "matching_skills": '["python"]', "missing_skills": "[]",
        }), encoding="utf-8")
        (folder / "auto_assets.json").write_text(json.dumps({
            "post_research_verdict": "YES", "post_research_confidence": 90,
            "updated_skills_match": {"matching": ["python"], "missing": []},
        }), encoding="utf-8")
        records = walk(tmp_path)
        assert len(records) == 1
        assert records[0]["folder"] == "Acme_Engineer_abc12345"
        assert records[0]["verdict_agree"] is True

    def test_skips_folders_missing_a_file(self, tmp_path):
        folder = tmp_path / "OnlyTriage_x"
        folder.mkdir()
        (folder / "original_triage.json").write_text("{}", encoding="utf-8")
        assert walk(tmp_path) == []

    def test_missing_root_returns_empty(self, tmp_path):
        assert walk(tmp_path / "does-not-exist") == []
