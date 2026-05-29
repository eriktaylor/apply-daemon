"""Tests for autopilot top-N selection (confidence bands + composite + lazy geo)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.process_queue import (
    _band,
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
