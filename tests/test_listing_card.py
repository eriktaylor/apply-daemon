"""Conformance tests for the listing-card contract.

LLM output is probabilistic, so a field will eventually be absent, empty, or
malformed. These tests assert the card contract holds anyway: every required
field always present, every derivable value computed rather than trusted, and
nothing raising on bad input.

The failure this guards against is real and has happened: the Slack card lost
its skills block, and nothing failed — a listing simply looked like it had no
skills. A contract nothing checks is a convention.
"""

from __future__ import annotations

import json

import pytest

from src.listing_card import (
    REQUIRED_FIELDS,
    age_in_days,
    build_card,
    format_skills_line,
    freshness,
    parse_skill_list,
    skills_match,
)


def _row(**kw) -> dict:
    base = {
        "id": "abc12345-0000",
        "title": "Applied AI ML Researcher Director",
        "company": "Acme Bank",
        "verdict": "YES",
        "confidence": 85,
        "location": "Palo Alto, CA, US",
        "distance_bucket": 1,
        "links": json.dumps(["https://example.com/job/1"]),
        "job_summary": "Architect autonomous agentic AI solutions.",
        "matching_skills": json.dumps(
            ["Agentic AI systems", "Applied AI research", "Cross-functional leadership"]
        ),
        "missing_skills": json.dumps(["Financial domain expertise"]),
        "date_ingested": "2026-07-29T00:00:00+00:00",
        "pipeline_status": "auto",
    }
    base.update(kw)
    return base


class TestContractAlwaysComplete:
    """Every required field present for every input, however degraded."""

    def test_full_row(self):
        card = build_card(_row())
        assert set(REQUIRED_FIELDS) <= set(card)

    def test_empty_row(self):
        card = build_card({})
        assert set(REQUIRED_FIELDS) <= set(card), "fields vanish on an empty row"

    @pytest.mark.parametrize("field", [
        "matching_skills", "missing_skills", "job_summary", "location",
        "links", "confidence", "verdict", "distance_bucket", "date_ingested",
    ])
    def test_any_single_field_missing(self, field):
        row = _row()
        del row[field]
        card = build_card(row)
        assert set(REQUIRED_FIELDS) <= set(card)

    @pytest.mark.parametrize("junk", ["", "null", "not json", "{}", "[[", None, 42])
    def test_malformed_skills_never_raise(self, junk):
        card = build_card(_row(matching_skills=junk, missing_skills=junk))
        assert card["matching_skills"] == []
        assert card["skills_pct"] is None

    def test_raw_email_text_never_emitted(self):
        card = build_card(_row(raw_email_text="SECRET body"))
        assert "SECRET" not in json.dumps(card)
        assert "raw_email_text" not in card


class TestHeuristicsNotLLM:
    """Derivable values are computed, never taken from the model."""

    def test_skills_pct_is_computed_from_the_lists(self):
        card = build_card(_row())
        # 3 matching, 1 missing → 75% (3/4), the reference card's numbers.
        assert (card["skills_pct"], card["skills_matched"], card["skills_total"]) == (
            75, 3, 4,
        )

    def test_a_model_supplied_percentage_is_ignored(self):
        """If a row ever carries its own percentage, ours still wins."""
        card = build_card(_row(skills_pct=99, skills_match_pct=99))
        assert card["skills_pct"] == 75

    def test_no_skills_is_none_not_zero_or_hundred(self):
        """0% and 100% are both misleading when nothing was extracted."""
        card = build_card(_row(matching_skills="[]", missing_skills="[]"))
        assert card["skills_pct"] is None

    def test_all_matching_is_100(self):
        assert skills_match(["a", "b"], [])[0] == 100

    def test_all_missing_is_0(self):
        assert skills_match([], ["a"])[0] == 0

    def test_distance_label_from_bucket(self):
        assert build_card(_row(distance_bucket=0))["distance"] == "Remote"
        assert build_card(_row(distance_bucket=3))["distance"] == "Far"
        assert build_card(_row(distance_bucket=None))["distance"] is None


class TestFreshness:
    def test_bands(self):
        assert freshness(0) == "new"
        assert freshness(7) == "new"
        assert freshness(20) == "recent"
        assert freshness(90) == "stale"
        assert freshness(None) is None

    def test_posting_date_preferred_over_ingest(self):
        from datetime import datetime, timedelta, timezone
        posted = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
        card = build_card(_row(date_posted=posted,
                               date_ingested="2026-01-01T00:00:00+00:00"))
        assert card["age_days"] == 2

    def test_falls_back_to_ingest_when_unposted(self):
        card = build_card(_row(date_posted=None))
        assert card["age_days"] is not None

    def test_unparseable_dates_are_none(self):
        assert age_in_days("not a date") is None
        assert age_in_days("") is None


class TestParseSkillList:
    def test_json_string(self):
        assert parse_skill_list('["a", "b"]') == ["a", "b"]

    def test_real_list(self):
        assert parse_skill_list(["a", "b"]) == ["a", "b"]

    def test_drops_blanks(self):
        assert parse_skill_list('["a", "", "  "]') == ["a"]

    def test_non_list_json(self):
        assert parse_skill_list('{"a": 1}') == []


class TestRenderedLine:
    def test_carries_every_decision_field(self):
        """The rendered line must not silently drop skills — the exact
        regression that made a listing look like it had none."""
        line = format_skills_line(build_card(_row()))
        assert "75%" in line and "(3/4)" in line
        assert "Agentic AI systems" in line
        assert "Financial domain expertise" in line

    def test_states_absence_rather_than_omitting(self):
        line = format_skills_line(build_card(_row(matching_skills="[]",
                                                  missing_skills="[]")))
        assert "not specified" in line


class TestSurfacesShareTheContract:
    """Both renderers must consume the shared builder, not assemble their own
    field set — two assemblers is how the contract drifted the first time."""

    def test_cli_uses_the_builder(self):
        import inspect

        import src.cli as cli
        assert "build_card" in inspect.getsource(cli._card)

    def test_digest_uses_the_builder(self):
        import inspect

        import src.digest as digest
        src = inspect.getsource(digest)
        assert "listing_card" in src, (
            "digest.py no longer imports the card contract — it is assembling "
            "its own field set again."
        )
