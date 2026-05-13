"""Tests for the notifications module (Slack error handling and Block Kit rendering)."""

import json
from unittest.mock import MagicMock, patch

from src.notifications import (
    PipelineSummary,
    _build_header_blocks,
    _build_listing_attachment,
    post_pipeline_summary,
)


class TestBuildListingAttachment:
    def test_auto_match_color(self):
        listing = {"id": "abc", "title": "Eng", "company": "Co", "confidence": 90}
        att = _build_listing_attachment(listing, status="auto_match")
        assert att["color"] == "#2eb67d"

    def test_escalate_color(self):
        listing = {"id": "abc", "title": "Eng", "company": "Co", "confidence": 50}
        att = _build_listing_attachment(listing, status="escalate")
        assert att["color"] == "#ecb22e"

    def test_model_scores_rendered(self):
        scores = [
            {"model": "gemma3:4b", "verdict": "YES", "confidence": 85, "reasoning": "Good"},
            {"model": "mistral", "verdict": "NO", "confidence": 40, "reasoning": "Bad"},
        ]
        listing = {
            "id": "abc", "title": "Eng", "company": "Co",
            "confidence": 62, "model_scores": json.dumps(scores),
        }
        att = _build_listing_attachment(listing, status="escalate")
        # Find the context block with model scores
        context_blocks = [b for b in att["blocks"] if b["type"] == "context"]
        assert context_blocks
        all_text = " ".join(
            e["text"] for b in context_blocks for e in b.get("elements", [])
        )
        assert "Gemma3" in all_text
        assert "Mistral" in all_text
        assert "85%" in all_text
        assert "40%" in all_text

    def test_job_summary_displayed(self):
        listing = {
            "id": "abc", "title": "Eng", "company": "Co",
            "confidence": 80,
            "job_summary": "Acme builds rockets. This role leads propulsion engineering.",
        }
        att = _build_listing_attachment(listing, status="yes")
        all_text = " ".join(
            b["text"]["text"] for b in att["blocks"]
            if b["type"] == "section" and "text" in b
        )
        assert "Acme builds rockets" in all_text

    def test_job_summary_not_shown_when_empty(self):
        listing = {"id": "abc", "title": "Eng", "company": "Co", "confidence": 80}
        att = _build_listing_attachment(listing, status="yes")
        all_text = " ".join(
            b["text"]["text"] for b in att["blocks"]
            if b["type"] == "section" and "text" in b
        )
        assert "TL;DR" not in all_text

    def test_action_buttons_present(self):
        listing = {"id": "abc123", "title": "Eng", "company": "Co", "confidence": 80}
        att = _build_listing_attachment(listing, status="yes")
        action_blocks = [b for b in att["blocks"] if b["type"] == "actions"]
        assert len(action_blocks) == 1
        buttons = action_blocks[0]["elements"]
        button_texts = [b["text"]["text"] for b in buttons]
        assert "Save" in button_texts
        assert "Pass" in button_texts
        assert "Escalate to Cloud LLM" in button_texts


class TestBuildHeaderBlocks:
    def test_header_includes_stats(self):
        summary = PipelineSummary(
            emails_fetched=10, emails_processed=8, listings_stored=5,
            verdict_counts={"yes": 2, "maybe": 2, "no": 1},
        )
        blocks = _build_header_blocks(summary)
        assert blocks[0]["type"] == "header"
        section_text = blocks[1]["text"]["text"]
        assert "10" in section_text
        assert "8" in section_text

    def test_header_shows_auto_match_escalate_counts(self):
        summary = PipelineSummary(
            emails_fetched=10, emails_processed=8, listings_stored=5,
            verdict_counts={"yes": 2, "maybe": 2, "no": 1},
            auto_match_listings=[{"id": "a"}],
            escalate_listings=[{"id": "b"}, {"id": "c"}],
        )
        blocks = _build_header_blocks(summary)
        all_text = " ".join(
            b["text"]["text"] for b in blocks if b.get("text", {}).get("type") == "mrkdwn"
        )
        assert "AUTO_MATCH" in all_text
        assert "ESCALATE" in all_text


class TestSlackErrorHandling:
    def _make_mock_app(self, error_message):
        """Create a mock App class whose client raises on chat_postMessage."""
        mock_app = MagicMock()
        mock_app.client.chat_postMessage.side_effect = Exception(error_message)
        return mock_app

    @patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C123"})
    def test_not_in_channel_warning(self, caplog, monkeypatch):
        mock_app = self._make_mock_app("not_in_channel")
        mock_app_cls = MagicMock(return_value=mock_app)
        import src.notifications as mod
        monkeypatch.setattr(mod, "_import_slack_app", lambda token: mock_app_cls(token=token))

        summary = PipelineSummary(
            emails_fetched=1, verdict_counts={"yes": 0, "maybe": 0, "no": 0},
        )
        result = post_pipeline_summary(summary)
        assert result is False
        assert "Bot is not in the channel" in caplog.text

    @patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C123"})
    def test_channel_not_found_warning(self, caplog, monkeypatch):
        import src.notifications as mod
        monkeypatch.setattr(mod, "_import_slack_app", lambda token: MagicMock(
            client=MagicMock(chat_postMessage=MagicMock(side_effect=Exception("channel_not_found")))
        ))

        summary = PipelineSummary(
            emails_fetched=1, verdict_counts={"yes": 0, "maybe": 0, "no": 0},
        )
        result = post_pipeline_summary(summary)
        assert result is False
        assert "Channel not found" in caplog.text

    @patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL_ID": "C123"})
    def test_invalid_auth_warning(self, caplog, monkeypatch):
        import src.notifications as mod
        monkeypatch.setattr(mod, "_import_slack_app", lambda token: MagicMock(
            client=MagicMock(chat_postMessage=MagicMock(side_effect=Exception("invalid_auth")))
        ))

        summary = PipelineSummary(
            emails_fetched=1, verdict_counts={"yes": 0, "maybe": 0, "no": 0},
        )
        result = post_pipeline_summary(summary)
        assert result is False
        assert "Invalid or revoked bot token" in caplog.text
