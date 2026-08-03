"""Tests for autopilot top-N selection (confidence bands + composite + lazy geo)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.process_queue import (
    _band,
    _build_prompt,
    _composite_score,
    _compute_distance_bucket,
    _resolve_bucket,
    _select_top_n,
    _skill_score,
)


def _row(
    listing_id: str = "abc",
    confidence: int = 90,
    verdict: str = "YES",
    matching: list[str] | None = None,
    missing: list[str] | None = None,
    location: str = "San Francisco, CA",
    date_ingested: str = "2026-05-27T00:00:00+00:00",
    distance_bucket: int | None = None,
) -> dict:
    return {
        "id": listing_id,
        "confidence": confidence,
        "verdict": verdict,
        "matching_skills": json.dumps(matching) if matching is not None else "",
        "missing_skills": json.dumps(missing) if missing is not None else "",
        "location": location,
        "date_ingested": date_ingested,
        "distance_bucket": distance_bucket,
    }


def test_band_5_point_bucketing():
    assert _band(100) == 20
    assert _band(95) == 19
    assert _band(94) == 18
    assert _band(90) == 18
    assert _band(89) == 17
    assert _band(85) == 17
    assert _band(0) == 0


def test_skill_score_handles_empty_and_malformed():
    assert _skill_score(_row(matching=["a", "b", "c"], missing=["x"])) == 2
    assert _skill_score(_row(matching=[], missing=[])) == 0
    assert _skill_score({"matching_skills": "not-json", "missing_skills": ""}) == 0
    assert _skill_score({"matching_skills": "", "missing_skills": ""}) == 0


@pytest.mark.parametrize(
    "get_distance_return,expected",
    [
        ("Remote", 0),
        ("12 miles", 1),
        ("30 miles", 1),
        ("31 miles", 2),
        ("60 miles", 2),
        ("61 miles", 3),
        ("Distance unknown", 3),
        ("garbage", 3),
    ],
)
def test_compute_distance_bucket_thresholds(get_distance_return, expected):
    with patch("src.process_queue.get_distance", return_value=get_distance_return):
        assert _compute_distance_bucket("Anywhere, CA") == expected


def test_compute_distance_bucket_empty_location():
    assert _compute_distance_bucket("") == 3


def test_composite_yes_with_remote_and_skills_beats_maybe_unknown():
    yes_remote = _composite_score(
        _row(verdict="YES", matching=["a", "b", "c"], missing=[]), bucket=0
    )
    maybe_unknown = _composite_score(
        _row(verdict="MAYBE", matching=[], missing=["x", "y"]), bucket=3
    )
    assert yes_remote > maybe_unknown


def test_composite_strong_maybe_can_outrank_weak_yes():
    # MAYBE + 3 matching + Remote should beat YES + 0 matching + Unknown.
    strong_maybe = _composite_score(
        _row(verdict="MAYBE", matching=["a", "b", "c"], missing=[]), bucket=0
    )
    weak_yes = _composite_score(_row(verdict="YES", matching=[], missing=[]), bucket=3)
    assert strong_maybe > weak_yes


def test_resolve_bucket_uses_cached_value_without_calling_geo():
    db = MagicMock()
    row = _row(distance_bucket=2)
    with patch("src.process_queue.get_distance") as mock_geo:
        assert _resolve_bucket(row, db) == 2
        mock_geo.assert_not_called()
    db.set_distance_bucket.assert_not_called()


def test_resolve_bucket_computes_and_persists_on_miss():
    db = MagicMock()
    row = _row(distance_bucket=None, location="Oakland, CA")
    with patch("src.process_queue.get_distance", return_value="8 miles"):
        bucket = _resolve_bucket(row, db)
    assert bucket == 1
    assert row["distance_bucket"] == 1
    db.set_distance_bucket.assert_called_once_with("abc", 1)


def test_select_top_n_walks_bands_descending_and_stops_early():
    db = MagicMock()
    rows = [
        _row("a", confidence=100, distance_bucket=0),                      # band 20
        _row("b", confidence=95, distance_bucket=0),                       # band 19
        _row("c", confidence=95, distance_bucket=3),                       # band 19
        # band 18 (should NOT be considered):
        _row("d", confidence=90, distance_bucket=0),
    ]
    selected = _select_top_n(rows, top_n=2, db=db)
    ids = [r["id"] for r in selected]
    # Top band (100) wins; second slot goes to band 19's best composite.
    assert ids[0] == "a"
    assert ids[1] == "b"  # Remote outranks Unknown within band 19
    # Band 18 should have been completely skipped (no geo lookup needed).
    # All rows here had cached buckets, so geo is never called regardless.
    db.set_distance_bucket.assert_not_called()


def test_select_top_n_lazy_geo_only_for_considered_bands():
    db = MagicMock()
    # All bucket fields are None — geo must be computed lazily.
    rows = [
        _row("hi-1", confidence=95, distance_bucket=None, location="Oakland, CA"),
        _row("hi-2", confidence=95, distance_bucket=None, location="Remote"),
        _row("lo-1", confidence=80, distance_bucket=None, location="NYC, NY"),
    ]
    with patch("src.process_queue.get_distance") as mock_geo:
        mock_geo.side_effect = lambda loc: {
            "Oakland, CA": "8 miles",
            "Remote": "Remote",
            "NYC, NY": "2500 miles",
        }[loc]
        _select_top_n(rows, top_n=2, db=db)
    # Only the top band (95) should have triggered geocoding.
    geocoded_locations = {call.args[0] for call in mock_geo.call_args_list}
    assert geocoded_locations == {"Oakland, CA", "Remote"}
    assert "NYC, NY" not in geocoded_locations


def test_select_top_n_within_band_tiebreak_uses_skills_then_date():
    db = MagicMock()
    rows = [
        # Same band (90 → 18), same verdict, same geo → composite decided by skills.
        _row("older-good-skills", confidence=90, distance_bucket=1,
             matching=["a", "b", "c"], missing=[],
             date_ingested="2026-05-25T00:00:00+00:00"),
        _row("newer-bad-skills", confidence=90, distance_bucket=1,
             matching=[], missing=["x", "y"],
             date_ingested="2026-05-27T00:00:00+00:00"),
        # Same skills + geo as older-good-skills, but newer → wins date tiebreak.
        _row("newer-good-skills", confidence=90, distance_bucket=1,
             matching=["a", "b", "c"], missing=[],
             date_ingested="2026-05-27T00:00:00+00:00"),
    ]
    selected = _select_top_n(rows, top_n=2, db=db)
    ids = [r["id"] for r in selected]
    assert ids == ["newer-good-skills", "older-good-skills"]


def test_select_top_n_empty_inputs():
    db = MagicMock()
    assert _select_top_n([], top_n=10, db=db) == []
    assert _select_top_n([_row()], top_n=0, db=db) == []


# ---------------------------------------------------------------------------
# What the post-research re-score is allowed to see
# ---------------------------------------------------------------------------


def _queued_listing(**overrides) -> dict:
    listing = {
        "title": "Applied AI Engineer",
        "company": "Acme",
        "location": "Oakland, CA",
        "salary": "not listed",
        "raw_email_text": "Owns evaluation harnesses. Requires Rust and CUDA.",
        "job_summary": "Acme is a Series B lab. The role owns evaluation.",
        "reason": "Strong match on agentic AI experience.",
    }
    listing.update(overrides)
    return listing


class TestAutoPromptGrounding:
    """The re-score exists to second-guess Stage 5. It cannot do that while
    reading Stage 5's own reasoning, and it cannot do it at all from a
    ~290-char summary of a posting it never sees."""

    def test_sends_the_job_description(self):
        prompt = _build_prompt(_queued_listing(), "research", "profile", "resume")
        assert "Requires Rust and CUDA." in prompt

    def test_never_sends_the_incumbent_models_reasoning(self):
        prompt = _build_prompt(_queued_listing(), "research", "profile", "resume")
        assert "Strong match on agentic AI experience." not in prompt
        assert "Initial Reasoning" not in prompt

    def test_falls_back_to_summary_without_a_description(self):
        prompt = _build_prompt(
            _queued_listing(raw_email_text=""), "research", "profile", "resume",
        )
        assert "Acme is a Series B lab." in prompt
        assert "Strong match on agentic AI experience." not in prompt

    def test_missing_body_is_a_stated_absence(self):
        prompt = _build_prompt(
            _queued_listing(raw_email_text="", job_summary=""),
            "research", "profile", "resume",
        )
        assert "(No job description was stored.)" in prompt


# ---------------------------------------------------------------------------
# The autopilot Slack card
# ---------------------------------------------------------------------------


class TestBuildSlackBlocks:
    """Regression: `card_blocks.append(dict, dict)` shipped and stayed broken
    for weeks because nothing ever rendered this card. It raised TypeError
    *outside* _post_results_to_slack's try, so it crashed the whole autopilot
    task and stranded enrichments that had already been researched and paid
    for. Every branch of the builder needs to be exercised, not just imported.
    """

    def _listing(self, **kw):
        row = {
            "id": "abcdef12-0000-0000-0000-000000000000",
            "title": "Applied AI Engineer",
            "company": "Acme",
            "location": "Oakland, CA",
            "salary": "$200k",
            "job_summary": "Acme builds evaluation tooling. You own the harness.",
            "verdict": "YES",
            "confidence": 90,
            "matching_skills": json.dumps(["Python", "Evals"]),
            "missing_skills": json.dumps(["Rust"]),
        }
        row.update(kw)
        return row

    def _auto(self, **kw):
        d = {
            "post_research_verdict": "YES",
            "post_research_confidence": 88,
            "match_analysis": "Strong fit.",
            "updated_skills_match": {"matching": ["Python"], "missing": ["Rust"]},
        }
        d.update(kw)
        return d

    def _render(self, listing=None, auto=None):
        from src.process_queue import _build_slack_blocks
        return _build_slack_blocks(
            listing or self._listing(), auto or self._auto(), Path("output/x"))

    def test_renders_with_a_job_summary(self):
        card, thread = self._render()
        assert all(isinstance(b, dict) for b in card)
        assert any("TL;DR" in json.dumps(b) for b in card)

    def test_renders_without_a_job_summary(self):
        card, _ = self._render(self._listing(job_summary=""))
        assert all(isinstance(b, dict) for b in card)

    def test_card_carries_the_skills_line(self):
        """The reason this branch exists — the card was the third renderer
        that silently lacked skills."""
        card, _ = self._render()
        assert any("Python" in json.dumps(b) for b in card)

    def test_no_section_exceeds_slacks_limit(self):
        """Slack rejects the entire message when one section's text is over
        3000 chars, so an over-long field drops the post rather than clipping
        it. Model output length is unbounded."""
        from src.notifications import SLACK_SECTION_TEXT_MAX
        card, thread = self._render(auto=self._auto(match_analysis="x" * 9000))
        for block in card + thread["thread_blocks"]:
            text = (block.get("text") or {}).get("text", "")
            assert len(text) <= SLACK_SECTION_TEXT_MAX

    @pytest.mark.parametrize("verdict", ["YES", "MAYBE", "NO"])
    def test_every_verdict_renders(self, verdict):
        card, _ = self._render(auto=self._auto(post_research_verdict=verdict))
        assert card


class TestPostResultsIsNonFatal:
    """_post_results_to_slack promises (success, ts). A card-building bug must
    surface as False, never as an exception that kills the task."""

    def test_card_builder_failure_returns_false(self):
        from src.process_queue import _post_results_to_slack
        with patch("src.process_queue._build_slack_blocks",
                   side_effect=TypeError("boom")):
            posted, ts = _post_results_to_slack(
                MagicMock(), "C1", {"id": "x"}, {}, Path("output/x"), None)
        assert posted is False and ts is None


class TestRankShortlist:
    """Regression: _rank_select fed all 536 eligible rows to the ranker. The
    response needed ~10k output tokens for the ids alone, truncated, failed to
    parse, and ranking silently fell open to input order on every production
    run — while still billing a Sonnet call each time."""

    def test_ranker_sees_only_the_head(self):
        from src.process_queue import _RANK_SHORTLIST, _rank_select
        rows = [_row(f"id-{i:03}", confidence=99 - i // 10) for i in range(60)]
        with patch("src.process_queue.ranking_mode", return_value="listwise"), \
             patch("src.process_queue.rank_candidates") as rank:
            # Reverse so the order differs from input (else fail-open logic).
            rank.side_effect = lambda **kw: list(reversed(kw["candidates"]))
            picked = _rank_select(rows, top_n=10, client=MagicMock())
        sent = rank.call_args.kwargs["candidates"]
        assert len(sent) == _RANK_SHORTLIST
        assert [c.id for c in sent] == [f"id-{i:03}" for i in range(_RANK_SHORTLIST)]
        assert picked is not None and len(picked) == 10
