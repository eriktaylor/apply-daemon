"""Idempotency and regression tests for the sweeper and tailor modules.

Covers five regressions:

  1. !coverletter / !prep re-fired on every sweep — missing white_check_mark gate
  2. JIT upsert preserved stale pipeline_status, silently blocking ✏️ tailor reactions
  3. _strip_code_fence crashed when the LLM response was truncated (no closing fence)
  4. _handle_pass must explicitly pass attachments=[] to wipe the complex Block Kit card
  5. Sweeper must correctly route ✏️ reactions on channel-level triage cards to _handle_tailor
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.db import Database
from src.models import JobListing
from src.sweeper import _handle_pass, _scan_chatops_commands
from src.tailor import _strip_code_fence

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


def _make_listing(**kwargs) -> JobListing:
    defaults = {
        "source": "linkedin",
        "email_classification": "JOB_DIGEST",
        "title": "Senior Backend Engineer",
        "company": "Acme Corp",
        "location": "Remote",
        "salary": "$150k-$190k",
        "verdict": "YES",
        "reason": "Strong match",
        "model_used": "gemma3:4b",
    }
    defaults.update(kwargs)
    return JobListing(**defaults)


def _make_job_card(
    job_id: str,
    ts: str = "100.000",
    reactions: list | None = None,
    event_type: str = "apply_daemon_listing",
) -> dict:
    """Minimal Slack message payload representing a posted job card.

    The default ``event_type`` is the current wire-format tag. Pass
    ``event_type="apply_pilot_listing"`` to exercise the dual-read path
    against the legacy tag that lives on cards posted before the
    apply-pilot → apply-daemon rename.
    """
    return {
        "ts": ts,
        "metadata": {
            "event_type": event_type,
            "event_payload": {"job_id": job_id},
        },
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "New | *Senior Backend Engineer* — Acme Corp",
                },
            }
        ],
        "reactions": reactions or [],
    }


def _make_reply(ts: str, text: str, *, processed: bool = False) -> dict:
    reactions = (
        [{"name": "white_check_mark", "users": ["UBOT"]}] if processed else []
    )
    return {"ts": ts, "text": text, "reactions": reactions}


# ---------------------------------------------------------------------------
# 1. ChatOps asset idempotency (!coverletter, !prep)
# ---------------------------------------------------------------------------


class TestChatopsAssetIdempotency:
    """!coverletter and !prep must not call the LLM on every sweep pass."""

    def _run_chatops(self, db, mock_app, reply_sequence, asset_type, mocker):
        """Helper: seed DB, configure mock, run two scans, return call counts."""
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        job_card = _make_job_card(listing.id, ts="100.000")
        mock_app.client.conversations_replies.side_effect = [
            {"messages": [job_card, seq]} for seq in reply_sequence
        ]

        mock_asset = mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        count1 = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
        count2 = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])

        return listing, mock_asset, count1, count2

    def test_coverletter_fires_exactly_once_across_two_sweeps(self, db, mocker):
        mock_app = MagicMock()
        unprocessed = _make_reply("200.000", "!coverletter", processed=False)
        processed = _make_reply("200.000", "!coverletter", processed=True)

        listing, mock_asset, count1, count2 = self._run_chatops(
            db, mock_app, [unprocessed, processed], "coverletter", mocker
        )

        assert count1 == 1, "First sweep must process the command"
        assert count2 == 0, "Second sweep must be skipped (idempotency)"
        mock_asset.assert_called_once_with(
            mock_app, "C_TEST", "100.000", listing.id, "coverletter"
        )

    def test_legacy_event_type_card_still_routes_chatops(self, db, mocker):
        # Dual-read regression: a card carrying the legacy
        # apply_pilot_listing tag must still be eligible for ChatOps
        # routing for as long as the legacy entry remains in
        # sweeper._LISTING_EVENT_TYPES.
        mock_app = MagicMock()
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        job_card = _make_job_card(
            listing.id, ts="100.000", event_type="apply_pilot_listing"
        )
        unprocessed = _make_reply("200.000", "!coverletter", processed=False)
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, unprocessed]
        }

        mock_asset = mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
        assert count == 1
        mock_asset.assert_called_once_with(
            mock_app, "C_TEST", "100.000", listing.id, "coverletter"
        )

    def test_prep_fires_exactly_once_across_two_sweeps(self, db, mocker):
        mock_app = MagicMock()
        unprocessed = _make_reply("200.000", "!prep", processed=False)
        processed = _make_reply("200.000", "!prep", processed=True)

        listing, mock_asset, count1, count2 = self._run_chatops(
            db, mock_app, [unprocessed, processed], "prep", mocker
        )

        assert count1 == 1
        assert count2 == 0
        mock_asset.assert_called_once_with(
            mock_app, "C_TEST", "100.000", listing.id, "prep"
        )

    def test_mark_reply_done_stamps_checkmark_on_reply_ts(self, db, mocker):
        """reactions_add(white_check_mark) must target the reply ts, not the card ts."""
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", "!coverletter", processed=False)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }

        mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])

        # Exactly one reactions_add call with white_check_mark on the reply ts
        checkmark_calls = [
            c
            for c in mock_app.client.reactions_add.call_args_list
            if c.kwargs.get("name") == "white_check_mark"
            and c.kwargs.get("timestamp") == "200.000"
        ]
        assert len(checkmark_calls) == 1, (
            "white_check_mark must be added to the reply (200.000), "
            "not the job card, so subsequent sweeps skip it"
        )

    def test_already_processed_reply_does_not_invoke_asset(self, db, mocker):
        """If the reply already carries white_check_mark, zero LLM calls."""
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", "!coverletter", processed=True)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }

        mock_asset = mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])

        assert count == 0
        mock_asset.assert_not_called()


# ---------------------------------------------------------------------------
# 2. JIT upsert must reset pipeline_status to "triaged"
# ---------------------------------------------------------------------------


class TestJitUpsertResetsPipelineStatus:
    """After _handle_triage_jit overwrites a listing, its status must be "triaged".

    Smart Upsert preserves the old pipeline_status. Without an explicit reset,
    a stale "passed" or "tailored" status blocks the sweeper's tailor gate:
        if current_status not in ("triaged", "saved"): skip
    """

    def _run_jit(self, db, old_status: str, mocker) -> None:
        from src.sweeper import _handle_triage_jit

        existing = _make_listing()
        db.insert_listing(existing)
        db.update_pipeline_status(existing.id, old_status)

        # TriageSession returns a listing with the same title+company so that
        # db.upsert_listing finds a fuzzy match and returns was_update=True.
        mock_new = _make_listing()

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.triage_email.return_value = [mock_new]
        mock_session.last_failure_reason = None

        mocker.patch("src.triage.TriageSession", return_value=mock_session)
        mocker.patch(
            "src.profile_loader.load_profile",
            return_value={
                "llm_context": "test context",
                "settings": {"dedup_window_days": 30, "jd_rejection_mode": None},
            },
        )
        mocker.patch("src.sweeper._post_thread_reply")
        mocker.patch("src.sweeper._post_triage_result")

        mock_app = MagicMock()
        _handle_triage_jit(
            mock_app, db, "C_TEST",
            "triage_ts.001", "reply_ts.001",
            "https://example.com/job",
            "Full job description text here for manual triage",
        )

        return existing.id

    def test_passed_status_is_reset_to_triaged(self, db, mocker):
        job_id = self._run_jit(db, "passed", mocker)
        row = db.get_listing_by_id(job_id)
        assert row["pipeline_status"] == "triaged", (
            f"Expected 'triaged' after JIT overwrite of 'passed' record, "
            f"got '{row['pipeline_status']}'"
        )

    def test_tailored_status_is_reset_to_triaged(self, db, mocker):
        job_id = self._run_jit(db, "tailored", mocker)
        row = db.get_listing_by_id(job_id)
        assert row["pipeline_status"] == "triaged"

    def test_reset_status_is_eligible_for_tailor_gate(self, db, mocker):
        """The sweeper's tailor gate is: current_status in ("triaged", "saved").
        This test verifies the JIT reset satisfies that gate."""
        job_id = self._run_jit(db, "passed", mocker)
        row = db.get_listing_by_id(job_id)
        tailor_eligible = {"triaged", "saved"}
        assert row["pipeline_status"] in tailor_eligible, (
            f"'{row['pipeline_status']}' is not in the sweeper's "
            f"tailor-eligible set {tailor_eligible}"
        )


# ---------------------------------------------------------------------------
# 3. _strip_code_fence handles truncated LLM output
# ---------------------------------------------------------------------------


class TestStripCodeFence:
    """_strip_code_fence must not require a closing ``` to work correctly."""

    def test_complete_fenced_json_is_unwrapped(self):
        text = '```json\n{"key": "value"}\n```'
        assert _strip_code_fence(text) == '{"key": "value"}'

    def test_truncated_response_missing_closing_fence(self):
        """Simulates OpenRouter hitting max_tokens and cutting off mid-content."""
        truncated = (
            '```json\n'
            '{"clean_cover_letter_text": "Dear Hiring Team at Acme Corp,\\n\\n'
            'I am writing to express my strong interest in the Senior Backend'
        )
        result = _strip_code_fence(truncated)
        assert not result.startswith("```"), "Opening fence must be stripped"
        assert result.startswith('{"clean_cover_letter_text"'), (
            "Content must start immediately after the stripped fence line"
        )

    def test_plain_json_is_returned_unchanged(self):
        text = '{"match_analysis": "Great fit.", "verdict": "YES"}'
        assert _strip_code_fence(text) == text

    def test_fence_without_language_tag(self):
        text = '```\n{"key": "value"}\n```'
        assert _strip_code_fence(text) == '{"key": "value"}'

    def test_whitespace_only_response_is_returned_as_empty(self):
        assert _strip_code_fence("   ") == ""

    def test_truncated_result_still_fails_json_parse_with_clear_error(self):
        """After stripping the fence, a truncated payload must raise JSONDecodeError
        (not silently return an empty/wrong result). This tests the error path."""
        truncated = '```json\n{"key": "truncated value without closing'
        stripped = _strip_code_fence(truncated)
        with pytest.raises(json.JSONDecodeError):
            json.loads(stripped)

    def test_full_pipeline_parses_fenced_response(self):
        """End-to-end: _parse_single_asset_response handles fenced API output."""
        from src.tailor import _parse_single_asset_response

        fenced = '```json\n{"clean_cover_letter_text": "Dear Team, ..."}\n```'
        result = _parse_single_asset_response(fenced, "clean_cover_letter_text")
        assert result["clean_cover_letter_text"] == "Dear Team, ..."


# ---------------------------------------------------------------------------
# 4. _handle_pass wipes the rich Block Kit card
# ---------------------------------------------------------------------------


class TestHandlePassWipesBlocks:
    """_handle_pass must replace the complex job card with a plain receipt."""

    def _call_handle_pass(self, original_blocks: list, original_attachments: list | None = None):
        mock_app = MagicMock()
        mock_db = MagicMock()
        mock_db.update_pipeline_status.return_value = True

        msg = {
            "ts": "100.000",
            "blocks": original_blocks,
            "attachments": original_attachments or [{"fallback": "complex attachment"}],
        }

        _handle_pass(mock_app, mock_db, "C_TEST", "100.000", "job-id-abc", msg)
        return mock_app.client.chat_update.call_args

    def test_chat_update_is_called_exactly_once(self):
        mock_app = MagicMock()
        mock_db = MagicMock()
        msg = {"ts": "100.000", "blocks": [], "attachments": []}

        _handle_pass(mock_app, mock_db, "C_TEST", "100.000", "job-id", msg)

        mock_app.client.chat_update.assert_called_once()

    def test_attachments_are_explicitly_cleared(self):
        """Passing attachments=[] is required; omitting it leaves the old card visible."""
        original = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Senior Dev* — Acme"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Salary: $150k"}]},
        ]
        call_args = self._call_handle_pass(original)
        kwargs = call_args.kwargs
        assert "attachments" in kwargs, "attachments kwarg must be present in chat_update"
        assert kwargs["attachments"] == [], (
            "attachments must be [] to wipe the Block Kit card attachment"
        )

    def test_blocks_replaced_with_single_passed_section(self):
        """The complex multi-block card must be collapsed to a single 'Passed' block."""
        original = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Senior Dev*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Job summary here"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Salary: $150k"}]},
        ]
        call_args = self._call_handle_pass(original)
        kwargs = call_args.kwargs
        blocks = kwargs["blocks"]

        assert len(blocks) == 1, (
            f"Expected exactly 1 replacement block, got {len(blocks)}: {blocks}"
        )
        assert blocks[0]["type"] == "section"
        assert "Passed" in blocks[0]["text"]["text"]

    def test_fallback_text_is_passed(self):
        call_args = self._call_handle_pass([])
        assert call_args.kwargs["text"] == "Passed"

    def test_db_status_is_updated_to_passed(self):
        mock_app = MagicMock()
        mock_db = MagicMock()
        msg = {"ts": "100.000", "blocks": [], "attachments": []}

        _handle_pass(mock_app, mock_db, "C_TEST", "100.000", "job-id-xyz", msg)

        mock_db.update_pipeline_status.assert_called_once_with("job-id-xyz", "passed")


# ---------------------------------------------------------------------------
# 5. Sweeper routes ✏️ reactions on channel-level triage cards to _handle_tailor
# ---------------------------------------------------------------------------


class TestSweeperRoutesTriageCardReactions:
    """The sweeper must detect ✏️ on a channel-level job card and call _handle_tailor.

    The bug: if the job card was posted as a thread reply, conversations_history
    would never return it and the reaction would be invisible. The fix posts cards
    at channel level. This test verifies that a card with apply_daemon_listing
    metadata at the channel level is correctly picked up and routed.
    """

    def _build_sweep_mocks(self, db, job_card: dict, mocker):
        mock_app = MagicMock()
        mock_app.client.conversations_history.return_value = {"messages": [job_card]}
        mock_app.client.conversations_replies.return_value = {"messages": []}

        # Wire Database() context manager to the real test db
        mock_db_cls = mocker.patch("src.sweeper.Database")
        mock_db_cls.return_value.__enter__ = MagicMock(return_value=db)
        mock_db_cls.return_value.__exit__ = MagicMock(return_value=False)

        mocker.patch("src.sweeper._get_slack_config", return_value=("tok_test", "C_TEST"))
        mocker.patch("src.sweeper._import_slack_app", return_value=mock_app)

        # Suppress side-effectful scan passes unrelated to this test
        mocker.patch("src.sweeper._scan_chatops_commands", return_value=0)
        mocker.patch("src.sweeper._scan_triage_commands", return_value=0)
        mocker.patch("src.sweeper._scan_triage_fallback_commands", return_value=0)
        mocker.patch("src.sweeper._scan_trend_commands", return_value=0)
        mocker.patch("src.sweeper._append_human_label")

        mock_tailor = mocker.patch("src.sweeper._handle_tailor")
        return mock_tailor

    def test_pencil_reaction_on_saved_card_calls_handle_tailor(self, db, mocker):
        from src.sweeper import sweep

        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        job_card = _make_job_card(
            listing.id,
            ts="100.000",
            reactions=[{"name": "pencil2", "users": ["U_ETAYLOR"]}],
        )
        mock_tailor = self._build_sweep_mocks(db, job_card, mocker)

        counts = sweep()

        mock_tailor.assert_called_once()
        assert counts["tailored"] == 1
        assert counts["skipped"] == 0

    def test_pencil_reaction_on_triaged_card_calls_handle_tailor(self, db, mocker):
        from src.sweeper import sweep

        listing = _make_listing()
        db.insert_listing(listing)
        # Default status after insert is "triaged"

        job_card = _make_job_card(
            listing.id,
            ts="100.000",
            reactions=[{"name": "pencil2", "users": ["U_ETAYLOR"]}],
        )
        mock_tailor = self._build_sweep_mocks(db, job_card, mocker)

        counts = sweep()

        mock_tailor.assert_called_once()
        assert counts["tailored"] == 1

    def test_pencil_reaction_on_already_tailored_card_is_skipped(self, db, mocker, tmp_path):
        """A 'tailored' card with complete output assets must not re-trigger the tailor engine."""
        from src.sweeper import sweep

        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "tailored")

        # Provide a "complete" output folder so the checkpoint scan reaches "complete" and skips
        job_dir = tmp_path / f"acme_corp_{listing.id[:8]}"
        job_dir.mkdir()
        (job_dir / "deep_research_context.txt").write_text("research")
        (job_dir / "assets.json").write_text("{}")
        mocker.patch("src.tailor._find_existing_output", return_value=job_dir)

        job_card = _make_job_card(
            listing.id,
            ts="100.000",
            reactions=[{"name": "pencil2", "users": ["U_ETAYLOR"]}],
        )
        mock_tailor = self._build_sweep_mocks(db, job_card, mocker)

        counts = sweep()

        mock_tailor.assert_not_called()
        assert counts["skipped"] == 1
        assert counts["tailored"] == 0

    def test_pencil_reaction_on_passed_card_is_skipped(self, db, mocker):
        """A 'passed' job must not be tailored even if the user adds ✏️."""
        from src.sweeper import sweep

        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "passed")

        job_card = _make_job_card(
            listing.id,
            ts="100.000",
            reactions=[{"name": "pencil2", "users": ["U_ETAYLOR"]}],
        )
        mock_tailor = self._build_sweep_mocks(db, job_card, mocker)

        counts = sweep()

        mock_tailor.assert_not_called()
        assert counts["skipped"] == 1
