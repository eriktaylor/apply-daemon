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
    POST_RESEARCH_FIELDS,
    REQUIRED_FIELDS,
    age_in_days,
    build_card,
    format_skills_line,
    format_verdict_line,
    freshness,
    parse_post_research,
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


class TestTwoScores:
    """Stage 5's score and autopilot's re-score both survive; the card names
    which one a renderer should show (R-4)."""

    def test_no_rescore_falls_back_to_stage5(self):
        card = build_card(_row())
        assert card["confidence_source"] == "stage5"
        assert (card["effective_verdict"], card["effective_confidence"]) == ("YES", 85)
        assert card["post_research_verdict"] is None
        assert card["confidence_delta"] is None

    def test_rescore_from_row_columns_wins(self):
        card = build_card(_row(post_research_verdict="MAYBE",
                               post_research_confidence=58))
        assert card["confidence_source"] == "post_research"
        assert (card["effective_verdict"], card["effective_confidence"]) == ("MAYBE", 58)
        # Stage 5 is kept, not overwritten — the disagreement is the point.
        assert (card["verdict"], card["confidence"]) == ("YES", 85)
        assert card["confidence_delta"] == -27

    def test_rescore_can_be_passed_in_by_a_caller_holding_the_envelope(self):
        """Autopilot renders mid-run from auto_assets.json, not from the row."""
        post = parse_post_research(
            {"post_research_verdict": "maybe", "post_research_confidence": "58"},
            85,
        )
        card = build_card(_row(), post_research=post)
        assert (card["effective_verdict"], card["effective_confidence"]) == ("MAYBE", 58)
        assert card["confidence_delta"] == -27

    def test_a_rescore_without_a_confidence_does_not_blend(self):
        card = build_card(_row(post_research_verdict="MAYBE"))
        assert card["confidence_source"] == "stage5"
        assert card["effective_confidence"] == 85
        assert card["effective_verdict"] == "MAYBE"

    @pytest.mark.parametrize("junk", ["", "not a number", None, [], {}])
    def test_malformed_rescore_degrades_to_stage5(self, junk):
        card = build_card(_row(post_research_verdict=junk,
                               post_research_confidence=junk))
        assert card["confidence_source"] == "stage5"
        assert card["effective_confidence"] == 85

    def test_verdict_line_names_its_source(self):
        assert format_verdict_line(build_card(_row())) == "YES 85% (stage 5)"
        line = format_verdict_line(
            build_card(_row(post_research_verdict="MAYBE",
                            post_research_confidence=58))
        )
        assert line == "MAYBE 58% (post-research · was YES 85%, -27)"

    def test_verdict_line_states_absence_rather_than_raising(self):
        assert format_verdict_line(build_card({})) == "? (stage 5)"


class TestParsePostResearch:
    """One parser for autopilot's envelope, so the CLI and Slack cannot
    disagree about the same file."""

    def test_full_envelope(self):
        post = parse_post_research({
            "post_research_verdict": "yes",
            "post_research_confidence": 80,
            "match_analysis": "Strong fit.",
            "updated_skills_match": {"matching": ["Python"], "missing": ["Rust"]},
        }, 95)
        assert set(post) == set(POST_RESEARCH_FIELDS)
        assert post["verdict"] == "YES"
        assert post["confidence"] == 80
        assert post["confidence_delta"] == -15
        assert post["matching_skills"] == ["Python"]

    def test_non_mapping_is_none(self):
        assert parse_post_research(["not", "a", "dict"]) is None
        assert parse_post_research(None) is None

    def test_empty_envelope_keeps_the_key_set(self):
        post = parse_post_research({}, 95)
        assert set(post) == set(POST_RESEARCH_FIELDS)
        assert post["verdict"] is None and post["confidence"] is None
        assert post["confidence_delta"] is None

    def test_skills_of_the_wrong_shape_never_raise(self):
        post = parse_post_research({"updated_skills_match": "junk"})
        assert post["matching_skills"] == [] and post["missing_skills"] == []

    def test_delta_needs_both_numbers(self):
        assert parse_post_research(
            {"post_research_confidence": 80}, None)["confidence_delta"] is None


class TestCrossSurfaceParity:
    """V-35 — Slack and the CLI must show the same verdict and confidence for
    the same listing.

    This failed on 212 of 213 rows: autopilot's Slack card was built from
    ``auto_assets.json`` while the CLI built one from the DB row, and only the
    JSON carried the re-score. The assertion is over the shared contract, not
    two renderers eyeballed side by side — a contract test cannot catch drift
    while only one surface imports it.
    """

    RESCORE = {
        "post_research_verdict": "MAYBE",
        "post_research_confidence": 58,
        "match_analysis": "Weaker than the listing implies.",
        "updated_skills_match": {"matching": ["Python"], "missing": ["Rust"]},
    }
    EXPECTED = "MAYBE 58% (post-research · was YES 95%, -37)"

    def _db_row(self):
        """What the CLI feed reads: the row, after autopilot's write-back."""
        return _row(confidence=95,
                    post_research_verdict=self.RESCORE["post_research_verdict"],
                    post_research_confidence=self.RESCORE["post_research_confidence"])

    def _listing(self):
        """What autopilot holds mid-run: the row *without* the re-score."""
        row = _row(confidence=95)
        row["salary"] = "$200k"
        return row

    def test_slack_and_cli_render_the_same_score(self, tmp_path, monkeypatch):
        import src.cli as cli
        from src.process_queue import _build_slack_blocks

        monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)
        cli_card = cli._card(self._db_row())
        blocks, _ = _build_slack_blocks(
            self._listing(), self.RESCORE, tmp_path / "Acme_x_abc12345")
        slack_card = build_card(
            self._listing(),
            post_research=parse_post_research(self.RESCORE, 95),
        )

        assert format_verdict_line(cli_card) == self.EXPECTED
        assert format_verdict_line(slack_card) == self.EXPECTED
        # ...and both renderers actually put that string in front of a human.
        assert self.EXPECTED in cli._fmt_card(cli_card)
        assert self.EXPECTED in json.dumps(blocks, ensure_ascii=False)

    def test_the_two_sources_agree_field_by_field(self, tmp_path, monkeypatch):
        import src.cli as cli

        monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path)
        from_db = cli._card(self._db_row())
        from_json = build_card(
            self._listing(),
            post_research=parse_post_research(self.RESCORE, 95),
        )
        for field in ("verdict", "confidence", "post_research_verdict",
                      "post_research_confidence", "confidence_delta",
                      "confidence_source", "effective_verdict",
                      "effective_confidence"):
            assert from_db[field] == from_json[field], field


class TestAutopilotUsesTheBuilder:
    """The third renderer. It assembled its own field set until R-4 — which is
    the drift `TestSurfacesShareTheContract` was written to prevent and could
    not see, because process_queue imported only the helpers."""

    def test_autopilot_card_uses_the_builder(self):
        import inspect

        import src.process_queue as pq
        assert "build_card" in inspect.getsource(pq._autopilot_card)

    def test_autopilot_card_carries_every_decision_field(self, tmp_path):
        from src.process_queue import _build_slack_blocks

        listing = _row(salary="$200k")
        blocks, thread = _build_slack_blocks(
            listing,
            {"post_research_verdict": "YES", "post_research_confidence": 88,
             "match_analysis": "Strong fit."},
            tmp_path,
        )
        rendered = json.dumps(blocks)
        for expected in ("Applied AI ML Researcher Director", "Acme Bank",
                         "Palo Alto", "$200k", "75%", "Agentic AI systems",
                         "Financial domain expertise", "abc12345-0000",
                         "https://example.com/job/1"):
            assert expected in rendered, expected

    def test_skills_survive_a_listing_with_no_summary(self, tmp_path):
        """They used to hang off the TL;DR branch, so a summary-less listing
        lost the line the card exists to carry."""
        from src.process_queue import _build_slack_blocks

        blocks, _ = _build_slack_blocks(
            _row(job_summary=""),
            {"post_research_verdict": "YES", "post_research_confidence": 88},
            tmp_path,
        )
        assert "Agentic AI systems" in json.dumps(blocks)
