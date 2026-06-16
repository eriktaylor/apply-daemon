"""Tests for the daily digest module."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from src.digest import (
    _freshness_badge,
    _is_stale,
    build_digest_blocks,
    build_digest_listing_attachment,
    post_digest,
)


class TestBuildDigestBlocks:
    def test_header_present(self):
        blocks = build_digest_blocks([])
        assert any(b.get("type") == "header" for b in blocks)

    def test_counts_correct(self):
        listings = [
            {"verdict": "YES", "pipeline_status": "triaged"},
            {"verdict": "YES", "pipeline_status": "saved"},
            {"verdict": "MAYBE", "pipeline_status": "triaged"},
            {"verdict": "NO", "pipeline_status": "triaged"},
        ]
        blocks = build_digest_blocks(listings)
        text = blocks[1]["text"]["text"]
        assert "*4*" in text  # total
        assert "*2* YES" in text
        assert "*1* MAYBE" in text
        assert "*1* saved" in text
        assert "*3* new" in text  # triaged count


class TestBuildDigestListingAttachment:
    def _listing(self, **overrides):
        base = {
            "id": "abc123",
            "title": "Senior Engineer",
            "company": "Acme Corp",
            "location": "Remote",
            "salary": "$180k",
            "confidence": 85,
            "verdict": "YES",
            "job_summary": "Acme builds widgets. This role owns the backend.",
            "pipeline_status": "triaged",
            "model_scores": "",
            "links": "",
        }
        base.update(overrides)
        return base

    def test_green_color_for_high_confidence_yes(self):
        att = build_digest_listing_attachment(self._listing())
        assert att["color"] == "#2eb67d"

    def test_yellow_color_for_low_confidence_yes(self):
        att = build_digest_listing_attachment(self._listing(confidence=50))
        assert att["color"] == "#ecb22e"

    def test_blue_color_for_maybe(self):
        att = build_digest_listing_attachment(self._listing(verdict="MAYBE"))
        assert att["color"] == "#36c5f0"

    def test_reaction_legend_present(self):
        att = build_digest_listing_attachment(self._listing())
        context_blocks = [b for b in att["blocks"] if b["type"] == "context"]
        all_text = " ".join(
            e["text"] for b in context_blocks for e in b.get("elements", [])
        )
        assert "Save" in all_text
        assert "Pass" in all_text
        assert "Tailor" in all_text
        assert "abc123" in all_text  # job_id embedded

    def test_no_action_buttons(self):
        att = build_digest_listing_attachment(self._listing())
        actions = [b for b in att["blocks"] if b["type"] == "actions"]
        assert len(actions) == 0

    def test_freshness_badge_fresh(self):
        recent = (date.today() - timedelta(days=5)).isoformat()
        att = build_digest_listing_attachment(self._listing(date_posted=recent))
        context_text = " ".join(
            e["text"] for b in att["blocks"]
            if b["type"] == "context" for e in b.get("elements", [])
        )
        assert ":calendar:" in context_text
        assert "5d ago" in context_text

    def test_freshness_badge_stale_warning(self):
        old = (date.today() - timedelta(days=45)).isoformat()
        att = build_digest_listing_attachment(self._listing(date_posted=old))
        context_text = " ".join(
            e["text"] for b in att["blocks"]
            if b["type"] == "context" for e in b.get("elements", [])
        )
        assert ":warning:" in context_text

    def test_freshness_badge_very_stale(self):
        old = (date.today() - timedelta(days=100)).isoformat()
        att = build_digest_listing_attachment(self._listing(date_posted=old))
        context_text = " ".join(
            e["text"] for b in att["blocks"]
            if b["type"] == "context" for e in b.get("elements", [])
        )
        assert "stale" in context_text

    def test_freshness_badge_absent_when_unknown(self):
        att = build_digest_listing_attachment(self._listing(date_posted=""))
        context_text = " ".join(
            e["text"] for b in att["blocks"]
            if b["type"] == "context" for e in b.get("elements", [])
        )
        assert ":calendar:" not in context_text
        assert ":warning:" not in context_text


    def test_job_summary_shown(self):
        att = build_digest_listing_attachment(self._listing())
        texts = [
            b["text"]["text"]
            for b in att["blocks"]
            if b.get("text", {}).get("text", "").startswith(":memo:")
        ]
        assert any("Acme builds widgets" in t for t in texts)

    def test_job_summary_hidden_when_empty(self):
        att = build_digest_listing_attachment(self._listing(job_summary=""))
        texts = [b.get("text", {}).get("text", "") for b in att["blocks"]]
        assert not any(":memo:" in t for t in texts)

    def test_saved_status_icon(self):
        att = build_digest_listing_attachment(self._listing(pipeline_status="saved"))
        context_blocks = [b for b in att["blocks"] if b["type"] == "context"]
        context_text = context_blocks[0]["elements"][0]["text"]
        assert "Saved" in context_text

    def test_skills_not_extracted_shows_na(self):
        att = build_digest_listing_attachment(self._listing(skills_extracted=False))
        texts = [b.get("text", {}).get("text", "") for b in att["blocks"]]
        assert any("N/A (Not specified in listing)" in t for t in texts)

    def test_skills_100_percent_with_names(self):
        att = build_digest_listing_attachment(self._listing(
            skills_extracted=True,
            matching_skills=json.dumps(["Python", "AWS", "Docker", "SQL"]),
            missing_skills="",
        ))
        texts = [b.get("text", {}).get("text", "") for b in att["blocks"]]
        assert any("100% (4/4)" in t for t in texts)
        assert any("Matching:*" in t and "Python" in t for t in texts)

    def test_skills_partial_with_matching_and_missing(self):
        att = build_digest_listing_attachment(self._listing(
            skills_extracted=True,
            matching_skills=json.dumps(["Python", "AWS"]),
            missing_skills=json.dumps(["Kubernetes"]),
        ))
        texts = [b.get("text", {}).get("text", "") for b in att["blocks"]]
        assert any("67% (2/3)" in t for t in texts)
        assert any("Matching:*" in t and "Python" in t for t in texts)
        assert any("Gaps:*" in t and "Kubernetes" in t for t in texts)

    def test_skills_only_missing(self):
        att = build_digest_listing_attachment(self._listing(
            skills_extracted=True,
            matching_skills="",
            missing_skills=json.dumps(["Go", "Rust"]),
        ))
        texts = [b.get("text", {}).get("text", "") for b in att["blocks"]]
        assert any("0% (0/2)" in t for t in texts)
        assert any("Gaps:*" in t and "Go" in t for t in texts)

    def test_skills_db_integer_flag(self):
        """DB stores skills_extracted as INTEGER 0/1."""
        att = build_digest_listing_attachment(self._listing(skills_extracted=0))
        texts = [b.get("text", {}).get("text", "") for b in att["blocks"]]
        assert any("N/A" in t for t in texts)

        att2 = build_digest_listing_attachment(self._listing(
            skills_extracted=1,
            matching_skills=json.dumps(["Go"]),
            missing_skills="",
        ))
        texts2 = [b.get("text", {}).get("text", "") for b in att2["blocks"]]
        assert any("100% (1/1)" in t for t in texts2)

    def test_link_in_header(self):
        att = build_digest_listing_attachment(self._listing(
            links=json.dumps(["https://example.com/job/123"]),
        ))
        header = att["blocks"][0]["text"]["text"]
        assert "<https://example.com/job/123|*Senior Engineer*>" in header

    def test_no_link_plain_header(self):
        att = build_digest_listing_attachment(self._listing(links=""))
        header = att["blocks"][0]["text"]["text"]
        assert "*Senior Engineer*" in header
        assert "<http" not in header

    def test_ensemble_model_scores_displayed(self):
        scores = json.dumps([
            {"model": "gemma3:4b", "verdict": "YES", "confidence": 95},
            {"model": "mistral:latest", "verdict": "YES", "confidence": 90},
            {"model": "qwen3:8b", "verdict": "MAYBE", "confidence": 70},
        ])
        att = build_digest_listing_attachment(self._listing(model_scores=scores))
        context_blocks = [b for b in att["blocks"] if b["type"] == "context"]
        all_text = " ".join(
            e["text"] for b in context_blocks for e in b.get("elements", [])
        )
        assert "Gemma3: YES (95%)" in all_text
        assert "Mistral: YES (90%)" in all_text
        assert "Qwen3: MAYBE (70%)" in all_text

    def test_no_history_omits_context_block(self):
        att = build_digest_listing_attachment(self._listing(), history="")
        all_text = " ".join(
            e.get("text", "")
            for b in att["blocks"] if b["type"] == "context"
            for e in b.get("elements", [])
        )
        assert "History" not in all_text

    def test_single_prior_history_block(self):
        att = build_digest_listing_attachment(
            self._listing(),
            history="`passed` (Oct 12)",
        )
        context_blocks = [b for b in att["blocks"] if b["type"] == "context"]
        all_text = " ".join(
            e.get("text", "") for b in context_blocks for e in b.get("elements", [])
        )
        assert "History (1 prior):" in all_text
        assert "`passed` (Oct 12)" in all_text

    def test_multiple_prior_history_block(self):
        att = build_digest_listing_attachment(
            self._listing(),
            history="`passed` (Oct 12) ➔ `saved` (Nov 15) ➔ `expired` (Dec 01)",
        )
        context_blocks = [b for b in att["blocks"] if b["type"] == "context"]
        all_text = " ".join(
            e.get("text", "") for b in context_blocks for e in b.get("elements", [])
        )
        assert "Seen 3 times" in all_text
        assert "`passed` (Oct 12) ➔ `saved` (Nov 15) ➔ `expired` (Dec 01)" in all_text

    def test_history_block_appears_before_reaction_legend(self):
        att = build_digest_listing_attachment(
            self._listing(),
            history="`passed` (Oct 12)",
        )
        blocks = att["blocks"]
        # History block should be second-to-last context, reaction legend last
        history_idx = None
        legend_idx = None
        for i, b in enumerate(blocks):
            if b["type"] == "context":
                text = " ".join(e.get("text", "") for e in b.get("elements", []))
                if "History" in text:
                    history_idx = i
                if "React:" in text:
                    legend_idx = i
        assert history_idx is not None
        assert legend_idx is not None
        assert history_idx < legend_idx


class TestFreshnessHelpers:
    def test_freshness_badge_unparseable_yields_empty(self):
        assert _freshness_badge("not-a-date") == ""

    def test_freshness_badge_empty_yields_empty(self):
        assert _freshness_badge("") == ""

    def test_freshness_badge_future_date_yields_empty(self):
        future = (date.today() + timedelta(days=3)).isoformat()
        assert _freshness_badge(future) == ""

    def test_is_stale_unknown_passes(self):
        """Unknown date_posted must never be flagged stale."""
        assert _is_stale("", 60) is False
        assert _is_stale("not-a-date", 60) is False

    def test_is_stale_within_window(self):
        recent = (date.today() - timedelta(days=10)).isoformat()
        assert _is_stale(recent, 60) is False

    def test_is_stale_past_window(self):
        old = (date.today() - timedelta(days=100)).isoformat()
        assert _is_stale(old, 60) is True


class TestDigestGeoDistance:
    """Verify geo distance integration in digest listing attachments."""

    def _listing(self, **overrides):
        base = {
            "id": "abc123",
            "title": "Senior Engineer",
            "company": "Acme Corp",
            "location": "San Francisco, CA",
            "salary": "$180k",
            "confidence": 85,
            "verdict": "YES",
            "job_summary": "",
            "pipeline_status": "triaged",
            "model_scores": "",
            "links": "",
        }
        base.update(overrides)
        return base

    @patch("src.digest.get_distance", return_value="12 miles")
    def test_location_with_distance(self, mock_dist):
        att = build_digest_listing_attachment(self._listing())
        header = att["blocks"][0]["text"]["text"]
        assert "San Francisco, CA (12 miles from home)" in header
        assert ":round_pushpin:" in header

    @patch("src.digest.get_distance", return_value="Remote")
    def test_remote_location(self, mock_dist):
        att = build_digest_listing_attachment(self._listing(location="Remote (US)"))
        header = att["blocks"][0]["text"]["text"]
        assert ":round_pushpin: Remote" in header

    @patch("src.digest.get_distance", return_value="Distance unknown")
    def test_unknown_distance(self, mock_dist):
        att = build_digest_listing_attachment(self._listing())
        header = att["blocks"][0]["text"]["text"]
        assert ":round_pushpin: San Francisco, CA" in header
        assert "from home" not in header

    def test_no_location(self):
        att = build_digest_listing_attachment(self._listing(location=""))
        header = att["blocks"][0]["text"]["text"]
        assert ":round_pushpin:" not in header


class TestPostDigestPacing:
    """Verify rate-limit handling and inter-message pacing."""

    @patch("src.digest.time")
    @patch("src.digest.Database")
    @patch("src.digest._import_slack_app")
    @patch("src.digest._get_slack_config", return_value=("xoxb-token", "C123"))
    def test_sleep_called_after_each_listing(self, mock_config, mock_import, MockDB, mock_time):
        app = MagicMock()
        mock_import.return_value = app
        app.client.retry_handlers = []

        db_instance = MagicMock()
        row1 = {
            "id": "job_1", "title": "Eng", "company": "Co",
            "verdict": "YES", "confidence": 80, "pipeline_status": "triaged",
            "location": "", "salary": "", "job_summary": "", "model_scores": "",
            "skills_extracted": 0, "matching_skills": "", "missing_skills": "",
            "links": "",
        }
        row2 = {
            "id": "job_2", "title": "Eng2", "company": "Co2",
            "verdict": "YES", "confidence": 70, "pipeline_status": "triaged",
            "location": "", "salary": "", "job_summary": "", "model_scores": "",
            "skills_extracted": 0, "matching_skills": "", "missing_skills": "",
            "links": "",
        }
        db_instance.get_digest_listings.return_value = [row1, row2]
        db_instance.get_listing_history.return_value = ""
        MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
        MockDB.return_value.__exit__ = MagicMock(return_value=False)

        result = post_digest()
        assert result is True
        # time.sleep(1.5) should be called once per listing
        assert mock_time.sleep.call_count == 2
        mock_time.sleep.assert_called_with(1.5)

    @patch("src.digest.time")
    @patch("src.digest.Database")
    @patch("src.digest._import_slack_app")
    @patch("src.digest._get_slack_config", return_value=("xoxb-token", "C123"))
    def test_rate_limit_handler_attached(self, mock_config, mock_import, MockDB, mock_time):
        app = MagicMock()
        app.client.retry_handlers = []
        mock_import.return_value = app

        db_instance = MagicMock()
        db_instance.get_digest_listings.return_value = []
        MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
        MockDB.return_value.__exit__ = MagicMock(return_value=False)

        post_digest()
        # The rate limit handler should have been appended
        assert len(app.client.retry_handlers) == 1
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        assert isinstance(app.client.retry_handlers[0], RateLimitErrorRetryHandler)


class TestSweeperRateLimitHandler:
    """Verify the sweeper attaches a rate limit handler."""

    @patch("src.sweeper._import_slack_app")
    @patch("src.sweeper._get_slack_config", return_value=("xoxb-token", "C123"))
    def test_rate_limit_handler_attached(self, mock_config, mock_import):
        from src.sweeper import sweep

        app = MagicMock()
        app.client.retry_handlers = []
        app.client.conversations_history.return_value = {"messages": []}
        mock_import.return_value = app

        sweep()
        assert len(app.client.retry_handlers) == 1
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
        assert isinstance(app.client.retry_handlers[0], RateLimitErrorRetryHandler)
