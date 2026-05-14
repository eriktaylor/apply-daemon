"""Tests for the reaction-based sweeper module."""

from unittest.mock import MagicMock

import pytest

from src.db import Database
from src.models import JobListing
from src.sweeper import (
    _auto_pass_no_verdict_cards,
    _classify_reaction,
    _classify_trend_cohort,
    _dispatch_reactions,
    _extract_job_id,
    _extract_triage_url,
    _get_user_reactions,
    _post_triage_status,
    _scan_chatops_commands,
)

# ---------------------------------------------------------------------------
# Shared fixtures (mirror test_idempotency.py helpers)
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


def _make_job_card(job_id: str, ts: str = "100.000") -> dict:
    return {
        "ts": ts,
        "metadata": {
            "event_type": "apply_daemon_listing",
            "event_payload": {"job_id": job_id},
        },
        "blocks": [],
        "reactions": [],
    }


def _make_reply(ts: str, text: str, *, processed: bool = False) -> dict:
    reactions = (
        [{"name": "white_check_mark", "users": ["UBOT"]}] if processed else []
    )
    return {"ts": ts, "text": text, "reactions": reactions}


class TestExtractJobId:
    def test_valid_metadata(self):
        msg = {
            "metadata": {
                "event_type": "apply_daemon_listing",
                "event_payload": {"job_id": "abc-123"},
            }
        }
        assert _extract_job_id(msg) == "abc-123"

    def test_legacy_event_type_still_detected(self):
        # Dual-read regression: cards posted before the apply-pilot →
        # apply-daemon rename carry the legacy event_type. They must
        # still be recognized for as long as the legacy tag is in
        # _LISTING_EVENT_TYPES.
        msg = {
            "metadata": {
                "event_type": "apply_pilot_listing",
                "event_payload": {"job_id": "legacy-1"},
            }
        }
        assert _extract_job_id(msg) == "legacy-1"

    def test_wrong_event_type(self):
        msg = {
            "metadata": {
                "event_type": "something_else",
                "event_payload": {"job_id": "abc-123"},
            }
        }
        assert _extract_job_id(msg) is None

    def test_no_metadata(self):
        assert _extract_job_id({"text": "hello"}) is None

    def test_missing_job_id(self):
        msg = {
            "metadata": {
                "event_type": "apply_daemon_listing",
                "event_payload": {},
            }
        }
        assert _extract_job_id(msg) is None


class TestGetUserReactions:
    def test_returns_user_reactions(self):
        msg = {
            "reactions": [
                {"name": "+1", "users": ["U123"]},
                {"name": "fire", "users": ["U456"]},
            ]
        }
        result = _get_user_reactions(msg)
        assert ("+1", "U123") in result
        assert ("fire", "U456") in result

    def test_filters_bot_receipts(self):
        msg = {
            "reactions": [
                {"name": "white_check_mark", "users": ["UBOT"]},
                {"name": "eyes", "users": ["UBOT"]},
                {"name": "+1", "users": ["U123"]},
            ]
        }
        result = _get_user_reactions(msg)
        assert len(result) == 1
        assert result[0][0] == "+1"

    def test_empty_reactions(self):
        assert _get_user_reactions({"reactions": []}) == []

    def test_no_reactions_key(self):
        assert _get_user_reactions({"text": "hello"}) == []


class TestClassifyReaction:
    def test_thumbsdown(self):
        assert _classify_reaction("-1") == "pass"
        assert _classify_reaction("thumbsdown") == "pass"

    def test_thumbsup(self):
        assert _classify_reaction("+1") == "save"
        assert _classify_reaction("thumbsup") == "save"

    def test_pencil(self):
        assert _classify_reaction("pencil2") == "tailor"
        assert _classify_reaction("pencil") == "tailor"

    def test_unknown(self):
        assert _classify_reaction("fire") is None
        assert _classify_reaction("heart") is None


# ---------------------------------------------------------------------------
# Sprint A — !triage URL extraction (_extract_triage_url)
# ---------------------------------------------------------------------------


class TestExtractTriageUrl:
    """Regex URL extraction from Slack !triage payloads.

    Slack wraps URLs in angle brackets (<https://...>). The extractor must
    strip wrapping, ignore trailing text (unfurled previews, copy-paste
    artifacts), and return None on plain-text payloads.
    """

    def test_plain_url(self):
        assert _extract_triage_url("https://jobs.example.com/123") == (
            "https://jobs.example.com/123"
        )

    def test_slack_angle_bracket_wrapping(self):
        assert _extract_triage_url("<https://jobs.example.com/123>") == (
            "https://jobs.example.com/123"
        )

    def test_trailing_text_ignored(self):
        assert _extract_triage_url(
            "<https://jobs.example.com/123> please triage this one"
        ) == "https://jobs.example.com/123"

    def test_http_scheme_supported(self):
        assert _extract_triage_url("http://old-ats.example.com/j/5") == (
            "http://old-ats.example.com/j/5"
        )

    def test_no_url_returns_none(self):
        assert _extract_triage_url("just some plain text") is None

    def test_empty_payload_returns_none(self):
        assert _extract_triage_url("") is None


# ---------------------------------------------------------------------------
# Sprint A + §3.2 #1 — _classify_trend_cohort matrix
# ---------------------------------------------------------------------------


def _row(status: str, verdict: str) -> dict:
    """Minimal dict-like row (supports `row[key]` subscript access)."""
    return {"pipeline_status": status, "verdict": verdict}


class TestClassifyTrendCohort:
    """Matrix of status × verdict → cohort assignment.

    Cohorts drive the `!trend` report. Any (status, verdict) pair returning
    None is silently dropped from the report.
    """

    @pytest.mark.parametrize(
        "status",
        ["saved", "tailored", "applied", "interviewing"],
    )
    def test_high_intent_by_status(self, status):
        assert _classify_trend_cohort(_row(status, "YES")) == "high_intent"
        assert _classify_trend_cohort(_row(status, "MAYBE")) == "high_intent"
        assert _classify_trend_cohort(_row(status, "NO")) == "high_intent"

    def test_pipeline_triaged_yes_or_maybe(self):
        assert _classify_trend_cohort(_row("triaged", "YES")) == "pipeline"
        assert _classify_trend_cohort(_row("triaged", "MAYBE")) == "pipeline"

    def test_rejected_by_no_verdict(self):
        assert _classify_trend_cohort(_row("triaged", "NO")) == "rejected"

    def test_rejected_by_status(self):
        assert _classify_trend_cohort(_row("passed", "YES")) == "rejected"
        assert _classify_trend_cohort(_row("rejected", "MAYBE")) == "rejected"

    def test_case_insensitive(self):
        assert _classify_trend_cohort(_row("SAVED", "yes")) == "high_intent"
        assert _classify_trend_cohort(_row("Triaged", "Maybe")) == "pipeline"

    def test_empty_row_returns_none(self):
        assert _classify_trend_cohort(_row("", "")) is None
        assert _classify_trend_cohort(_row(None, None)) is None

    def test_unanimous_no_verdict_classifies_as_rejected(self):
        """A NO verdict (below-threshold rejection) classifies as rejected."""
        result = _classify_trend_cohort(_row("triaged", "NO"))
        assert result == "rejected"

    def test_unanimous_no_with_rejected_status_classifies_as_rejected(self):
        """Auto-dismissed listings start with pipeline_status='rejected' → rejected cohort."""
        result = _classify_trend_cohort(_row("rejected", "NO"))
        assert result == "rejected"


# ---------------------------------------------------------------------------
# Sprint C — _CHATOPS_ELIGIBLE_STATUSES gate on !coverletter / !prep
# ---------------------------------------------------------------------------


class TestChatOpsEligibilityGate:
    """`!coverletter` and `!prep` must no-op on ineligible statuses.

    Eligible statuses: {tailored, saved, applied}. Anything else (triaged,
    passed, rejected, interviewing) should skip the LLM call entirely.
    """

    def _run(self, db, status, command, mocker):
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, status)

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", command, processed=False)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }

        mock_asset = mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
        return count, mock_asset

    @pytest.mark.parametrize("status", ["triaged", "passed", "rejected"])
    def test_coverletter_rejected_on_ineligible_status(self, db, mocker, status):
        count, mock_asset = self._run(db, status, "!coverletter", mocker)
        assert count == 0
        mock_asset.assert_not_called()

    @pytest.mark.parametrize("status", ["triaged", "passed", "rejected"])
    def test_prep_rejected_on_ineligible_status(self, db, mocker, status):
        count, mock_asset = self._run(db, status, "!prep", mocker)
        assert count == 0
        mock_asset.assert_not_called()

    @pytest.mark.parametrize("status", ["saved", "tailored", "applied"])
    def test_coverletter_allowed_on_eligible_status(self, db, mocker, status):
        count, mock_asset = self._run(db, status, "!coverletter", mocker)
        assert count == 1
        mock_asset.assert_called_once()


# ---------------------------------------------------------------------------
# Sprint A + §3.1 #3 — !update post-command status contract
# ---------------------------------------------------------------------------


class TestHandleUpdateStatusContract:
    """`!update` merges context but does NOT reset pipeline_status.

    KNOWN GAP (§3.1 #3): if a listing is stuck in a terminal status
    (rejected, passed, auto_rejected), `!update` re-scores via the LLM but
    leaves the old status in place. The `upsert_listing` Smart-Upsert
    preservation contract then keeps that stale status even after a
    successful re-score.

    These tests pin current behavior. When the fix lands, they must be
    updated to assert the new contract (reset to 'triaged' after a
    successful re-score, mirroring `_handle_triage_jit`).
    """

    def test_update_resets_status_to_triaged_after_successful_rescore(self, db, mocker):
        """!update must reset pipeline_status to 'triaged' after a successful re-score.

        Without this reset, a listing stuck in 'rejected' would remain locked
        out of tailor reactions and asset commands even after the user added
        fresh context via !update.
        """

        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "rejected")

        rescored = _make_listing(title=listing.title, company=listing.company)

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.evaluate_listing.return_value = rescored
        mocker.patch("src.triage.TriageSession", return_value=mock_session)
        mocker.patch("src.profile_loader.load_profile", return_value={
            "llm_context": "ctx",
            "settings": {"dedup_window_days": 30},
        })
        mocker.patch("src.sweeper._post_thread_reply")

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", "!update pasted text here", processed=False)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }

        _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "triaged", (
            "!update must reset status to triaged so the listing re-enters the active pipeline"
        )


# ---------------------------------------------------------------------------
# Priority-first reaction dispatch gates
# ---------------------------------------------------------------------------


def _run_dispatch(db, mocker, job_id, status, reactions):
    """Helper: insert a listing at *status*, attach *reactions* to its card, run dispatch."""
    listing = _make_listing()
    # Override the auto-generated id so callers can pass a known job_id
    listing = listing._replace(id=job_id) if hasattr(listing, "_replace") else listing
    # Use the actual listing id (JobListing may be a dataclass)
    db.insert_listing(listing)
    db.update_pipeline_status(listing.id, status)

    # Build the card with the requested reactions
    job_card = _make_job_card(listing.id)
    job_card["reactions"] = [
        {"name": r, "users": ["U_HUMAN"]}
        for r in reactions
    ]

    mock_app = MagicMock()
    counts = {"passed": 0, "saved": 0, "tailored": 0, "questions": 0, "skipped": 0}

    mock_save = mocker.patch("src.sweeper._handle_save")
    mock_tailor = mocker.patch("src.sweeper._handle_tailor")
    mock_pass = mocker.patch("src.sweeper._handle_pass")
    mock_router = mocker.patch("src.sweeper._handle_smart_router", return_value="questions")
    mocker.patch("src.sweeper._append_human_label")

    _dispatch_reactions(mock_app, db, "C_TEST", [job_card], counts)

    return counts, mock_save, mock_tailor, mock_pass, mock_router, listing.id


class TestReactionDispatchGates:
    """Priority model: pass > tailor > save.  Lower-priority co-reactions are no-ops."""

    def test_save_from_triaged(self, db, mocker):
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-save-triaged", "triaged", ["+1"]
        )
        mock_save.assert_called_once()
        mock_tailor.assert_not_called()
        mock_pass.assert_not_called()
        assert counts["saved"] == 1

    def test_save_skipped_when_already_saved(self, db, mocker):
        counts, mock_save, *_ = _run_dispatch(
            db, mocker, "jid-save-saved", "saved", ["+1"]
        )
        mock_save.assert_not_called()
        assert counts["skipped"] == 1

    def test_save_skipped_when_tailored(self, db, mocker):
        """Scenario 1 regression: 👍 on a tailored listing must NOT re-invoke save."""
        counts, mock_save, *_ = _run_dispatch(
            db, mocker, "jid-save-tailored", "tailored", ["+1"]
        )
        mock_save.assert_not_called()
        assert counts["skipped"] == 1

    def test_save_skipped_when_passed(self, db, mocker):
        counts, mock_save, *_ = _run_dispatch(
            db, mocker, "jid-save-passed", "passed", ["+1"]
        )
        mock_save.assert_not_called()
        assert counts["skipped"] == 1

    def test_tailor_from_triaged(self, db, mocker):
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-tailor-triaged", "triaged", ["pencil"]
        )
        mock_tailor.assert_called_once()
        mock_save.assert_not_called()
        mock_pass.assert_not_called()
        assert counts["tailored"] == 1

    def test_tailor_from_saved(self, db, mocker):
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-tailor-saved", "saved", ["pencil"]
        )
        mock_tailor.assert_called_once()
        mock_pass.assert_not_called()
        assert counts["tailored"] == 1

    def test_tailor_skipped_when_already_tailored(self, db, mocker, tmp_path):
        """status=tailored + both checkpoint files present → skip (no regen)."""
        # Provide a "complete" output folder so checkpoint logic reaches "complete"
        job_dir = tmp_path / "acme_corp_jid-tail8"
        job_dir.mkdir()
        (job_dir / "deep_research_context.txt").write_text("research")
        (job_dir / "assets.json").write_text("{}")
        mocker.patch("src.tailor._find_existing_output", return_value=job_dir)

        counts, mock_save, mock_tailor, *_ = _run_dispatch(
            db, mocker, "jid-tailor-tailored", "tailored", ["pencil"]
        )
        mock_tailor.assert_not_called()
        assert counts["skipped"] == 1

    def test_pass_from_tailored(self, db, mocker):
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-pass-tailored", "tailored", ["-1"]
        )
        mock_pass.assert_called_once()
        mock_save.assert_not_called()
        mock_tailor.assert_not_called()
        assert counts["passed"] == 1

    def test_pass_skipped_when_already_passed(self, db, mocker):
        counts, _, __, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-pass-passed", "passed", ["-1"]
        )
        mock_pass.assert_not_called()
        assert counts["skipped"] == 1

    def test_tailor_wins_over_save(self, db, mocker):
        """✏️ + 👍 together: only tailor fires, save is a no-op."""
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-tailor-over-save", "triaged", ["pencil", "+1"]
        )
        mock_tailor.assert_called_once()
        mock_save.assert_not_called()
        mock_pass.assert_not_called()
        assert counts["tailored"] == 1

    def test_pass_wins_over_save(self, db, mocker):
        """👎 + 👍 together: only pass fires."""
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-pass-over-save", "triaged", ["-1", "+1"]
        )
        mock_pass.assert_called_once()
        mock_save.assert_not_called()
        mock_tailor.assert_not_called()
        assert counts["passed"] == 1

    def test_pass_wins_over_tailor(self, db, mocker):
        """Scenario 2 regression: ✏️ + 👎 together: only pass fires, tailor does NOT run."""
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-pass-over-tailor", "saved", ["-1", "pencil"]
        )
        mock_pass.assert_called_once()
        mock_tailor.assert_not_called()
        mock_save.assert_not_called()
        assert counts["passed"] == 1

    def test_pass_wins_over_all(self, db, mocker):
        """👍 + ✏️ + 👎: only pass fires."""
        counts, mock_save, mock_tailor, mock_pass, *_ = _run_dispatch(
            db, mocker, "jid-pass-all", "triaged", ["+1", "pencil", "-1"]
        )
        mock_pass.assert_called_once()
        mock_save.assert_not_called()
        mock_tailor.assert_not_called()
        assert counts["passed"] == 1


class TestSaveReceiptIdempotency:
    """already_reacted from Slack should log at DEBUG, not ERROR."""

    def test_already_reacted_logs_debug_not_error(self, db, mocker, caplog):
        import logging

        from slack_sdk.errors import SlackApiError

        listing = _make_listing()
        db.insert_listing(listing)

        mock_app = MagicMock()
        already_reacted = SlackApiError(
            message="already_reacted",
            response={"error": "already_reacted", "ok": False},
        )
        mock_app.client.reactions_add.side_effect = already_reacted

        job_card = _make_job_card(listing.id)
        job_card["reactions"] = [{"name": "+1", "users": ["U_HUMAN"]}]

        mocker.patch("src.sweeper._append_human_label")

        counts = {"passed": 0, "saved": 0, "tailored": 0, "questions": 0, "skipped": 0}
        with caplog.at_level(logging.DEBUG, logger="src.sweeper"):
            _dispatch_reactions(mock_app, db, "C_TEST", [job_card], counts)

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, f"Unexpected ERROR log: {error_records}"
        debug_records = [r for r in caplog.records if "already present" in r.message]
        assert debug_records, "Expected a DEBUG log for already_reacted"


# ---------------------------------------------------------------------------
# !triage revival: bring back a passed listing
# ---------------------------------------------------------------------------


class TestPostTriageStatus:
    """Manual !triage / !update paths bypass the confidence-threshold rejection
    so a NO can still surface. The sweeper must auto-dismiss those NOs as
    `passed` so they land in the rejected lane instead of as an active card."""

    def test_no_verdict_returns_passed(self):
        assert _post_triage_status("NO") == "passed"

    def test_yes_verdict_returns_triaged(self):
        assert _post_triage_status("YES") == "triaged"

    def test_maybe_verdict_returns_triaged(self):
        assert _post_triage_status("MAYBE") == "triaged"

    def test_case_insensitive(self):
        assert _post_triage_status("no") == "passed"
        assert _post_triage_status("No") == "passed"

    def test_empty_or_none_defaults_to_triaged(self):
        assert _post_triage_status("") == "triaged"
        assert _post_triage_status(None) == "triaged"


class TestTriageNoAutoPassed:
    """End-to-end: a manual !triage that returns a NO verdict must end up with
    pipeline_status='passed' in the DB, both for brand-new inserts and updates."""

    def _run_triage(self, db, mocker, rescored, existing=None):
        from src.sweeper import _handle_triage

        if existing is not None:
            db.insert_listing(existing)

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.triage_email.return_value = [rescored]
        mock_session.last_failure_reason = None
        mocker.patch("src.triage.TriageSession", return_value=mock_session)
        mocker.patch("src.profile_loader.load_profile", return_value={
            "llm_context": "ctx",
            "settings": {"dedup_window_days": 30},
        })
        mocker.patch("src.sweeper._scrape_for_triage", return_value="job text")
        mocker.patch("src.sweeper._post_thread_reply")
        mocker.patch("src.sweeper._post_triage_result")

        mock_app = MagicMock()
        _handle_triage(
            mock_app, db, "C_TEST", "100.000",
            "https://careers.example.com/role/123",
        )

    def test_no_on_new_insert_lands_as_passed(self, db, mocker):
        rescored = _make_listing(verdict="NO", confidence=92)
        self._run_triage(db, mocker, rescored)

        row = db.get_listing_by_id(rescored.id)
        assert row is not None
        assert row["pipeline_status"] == "passed", (
            "A NO verdict surfaced via manual !triage must be auto-dismissed "
            "to 'passed' even on a brand-new insert"
        )

    def test_no_on_update_overrides_revive_to_passed(self, db, mocker):
        existing = _make_listing()
        rescored = _make_listing(
            title=existing.title, company=existing.company,
            verdict="NO", confidence=90,
        )
        self._run_triage(db, mocker, rescored, existing=existing)

        row = db.get_listing_by_id(existing.id)
        assert row["pipeline_status"] == "passed", (
            "A NO verdict re-scored via !triage must override the usual "
            "'triaged' revive and land in 'passed'"
        )

    def test_yes_still_lands_as_triaged(self, db, mocker):
        rescored = _make_listing(verdict="YES", confidence=88)
        self._run_triage(db, mocker, rescored)

        row = db.get_listing_by_id(rescored.id)
        assert row["pipeline_status"] == "triaged"


class TestAutoPassNoVerdictCards:
    """The sweep loop must convert any NO-verdict card surfaced in Slack to
    'Passed' — including stragglers from previous runs that landed before
    the Stage 5 gate. Must be idempotent across sweeps."""

    def _counts(self):
        return {
            "passed": 0, "saved": 0, "tailored": 0, "questions": 0,
            "chatops": 0, "triage": 0, "trend": 0, "skipped": 0, "regenerate": 0,
        }

    def test_no_card_flipped_to_passed_and_ui_updated(self, db, mocker):
        listing = _make_listing(verdict="NO", confidence=95)
        db.insert_listing(listing)
        # Status defaults to 'triaged' on insert.

        msg = _make_job_card(listing.id)
        mock_app = MagicMock()
        # Speed up the rate-limit valve in _handle_pass.
        mocker.patch("src.sweeper.time.sleep")
        counts = self._counts()

        _auto_pass_no_verdict_cards(mock_app, db, "C_TEST", [msg], counts)

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "passed"
        assert counts["passed"] == 1
        mock_app.client.chat_update.assert_called_once()
        update_kwargs = mock_app.client.chat_update.call_args.kwargs
        assert update_kwargs["channel"] == "C_TEST"
        assert update_kwargs["ts"] == "100.000"
        assert update_kwargs["attachments"] == []

    def test_yes_card_left_alone(self, db, mocker):
        listing = _make_listing(verdict="YES", confidence=88)
        db.insert_listing(listing)

        msg = _make_job_card(listing.id)
        mock_app = MagicMock()
        mocker.patch("src.sweeper.time.sleep")
        counts = self._counts()

        _auto_pass_no_verdict_cards(mock_app, db, "C_TEST", [msg], counts)

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "triaged"
        assert counts["passed"] == 0
        mock_app.client.chat_update.assert_not_called()

    def test_idempotent_when_already_passed(self, db, mocker):
        listing = _make_listing(verdict="NO", confidence=80)
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "passed")

        msg = _make_job_card(listing.id)
        mock_app = MagicMock()
        mocker.patch("src.sweeper.time.sleep")
        counts = self._counts()

        _auto_pass_no_verdict_cards(mock_app, db, "C_TEST", [msg], counts)

        assert counts["passed"] == 0
        mock_app.client.chat_update.assert_not_called()

    def test_skips_messages_without_job_id_metadata(self, db, mocker):
        # A bot message with no apply_daemon_listing metadata — must be a no-op.
        msg = {"ts": "999.000", "blocks": [], "reactions": []}
        mock_app = MagicMock()
        mocker.patch("src.sweeper.time.sleep")
        counts = self._counts()

        _auto_pass_no_verdict_cards(mock_app, db, "C_TEST", [msg], counts)

        assert counts["passed"] == 0
        mock_app.client.chat_update.assert_not_called()

    def test_skips_when_db_row_missing(self, db, mocker):
        # Card present in Slack but DB row purged — must not raise.
        msg = _make_job_card("missing-job-id")
        mock_app = MagicMock()
        mocker.patch("src.sweeper.time.sleep")
        counts = self._counts()

        _auto_pass_no_verdict_cards(mock_app, db, "C_TEST", [msg], counts)

        assert counts["passed"] == 0
        mock_app.client.chat_update.assert_not_called()

    def test_flips_no_in_saved_status(self, db, mocker):
        """A NO somehow saved by the user still gets auto-passed."""
        listing = _make_listing(verdict="NO", confidence=72)
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        msg = _make_job_card(listing.id)
        mock_app = MagicMock()
        mocker.patch("src.sweeper.time.sleep")
        counts = self._counts()

        _auto_pass_no_verdict_cards(mock_app, db, "C_TEST", [msg], counts)

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "passed"
        assert counts["passed"] == 1


class TestTriageRevivesPassedListing:
    """`!triage <url>` on an existing passed/rejected listing must reset
    pipeline_status to 'triaged' so reactions on the new card are functional."""

    def test_triage_resets_status_to_triaged_on_update(self, db, mocker):
        from src.sweeper import _handle_triage

        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "passed")

        rescored = _make_listing(title=listing.title, company=listing.company)

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.triage_email.return_value = [rescored]
        mock_session.last_failure_reason = None
        mocker.patch("src.triage.TriageSession", return_value=mock_session)
        mocker.patch("src.profile_loader.load_profile", return_value={
            "llm_context": "ctx",
            "settings": {"dedup_window_days": 30},
        })
        mocker.patch("src.sweeper._scrape_for_triage", return_value="job text")
        mocker.patch("src.sweeper._post_thread_reply")
        mocker.patch("src.sweeper._post_triage_result")

        mock_app = MagicMock()
        _handle_triage(
            mock_app, db, "C_TEST", "100.000",
            "https://careers.example.com/role/123",
        )

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "triaged", (
            "!triage on a passed listing must reset status to 'triaged' so the "
            "user can save/tailor/pass on the revived card"
        )


# ---------------------------------------------------------------------------
# Tailor checkpoint fallback — disk cross-check when status == "tailored"
# ---------------------------------------------------------------------------


class TestTailorCheckpointFallback:
    """When pencil wins priority and status == 'tailored', the sweeper checks
    the output folder against three checkpoints before deciding to skip."""

    def test_checkpoint1_no_folder_triggers_regen(self, db, mocker):
        """Checkpoint 1: output folder missing → regenerate (stale status or user deleted)."""
        mocker.patch("src.tailor._find_existing_output", return_value=None)

        counts, _, mock_tailor, *_ = _run_dispatch(
            db, mocker, "jid-chk1-regen", "tailored", ["pencil"]
        )
        mock_tailor.assert_called_once()
        assert counts["tailored"] == 1

    def test_checkpoint2_no_research_file_posts_error(self, db, mocker, tmp_path):
        """Checkpoint 2: folder present but deep_research_context.txt missing →
        post Slack error, do NOT regenerate."""
        job_dir = tmp_path / "co_role_jid-chk2"
        job_dir.mkdir()
        # No deep_research_context.txt written
        mocker.patch("src.tailor._find_existing_output", return_value=job_dir)
        mock_post = mocker.patch("src.sweeper._post_thread_reply")

        counts, _, mock_tailor, *_ = _run_dispatch(
            db, mocker, "jid-chk2-error", "tailored", ["pencil"]
        )
        mock_tailor.assert_not_called()
        mock_post.assert_called_once()
        # _post_thread_reply(app, channel, ts, text) — text is the 4th arg
        assert "!regenerate" in mock_post.call_args[0][3]
        assert counts["skipped"] == 1

    def test_checkpoint3_no_assets_json_resumes_from_research(self, db, mocker, tmp_path):
        """Checkpoint 3: research file present, assets.json absent →
        _handle_tailor is called with the cached research text."""
        cached_text = "Company research text for checkpoint resume"
        job_dir = tmp_path / "co_role_jid-chk3"
        job_dir.mkdir()
        (job_dir / "deep_research_context.txt").write_text(cached_text)
        # No assets.json
        mocker.patch("src.tailor._find_existing_output", return_value=job_dir)

        counts, _, mock_tailor, *_ = _run_dispatch(
            db, mocker, "jid-chk3-resume", "tailored", ["pencil"]
        )
        mock_tailor.assert_called_once()
        # Verify the cached research was passed as keyword arg
        _, kwargs = mock_tailor.call_args
        assert kwargs.get("research_context_cache") == cached_text
        assert counts["tailored"] == 1

    def test_checkpoint_complete_skips(self, db, mocker, tmp_path):
        """Both checkpoint files present → skip, _handle_tailor not called."""
        job_dir = tmp_path / "co_role_jid-chkdone"
        job_dir.mkdir()
        (job_dir / "deep_research_context.txt").write_text("research")
        (job_dir / "assets.json").write_text("{}")
        mocker.patch("src.tailor._find_existing_output", return_value=job_dir)

        counts, _, mock_tailor, *_ = _run_dispatch(
            db, mocker, "jid-chk-done", "tailored", ["pencil"]
        )
        mock_tailor.assert_not_called()
        assert counts["skipped"] == 1


# ---------------------------------------------------------------------------
# !regenerate ChatOps command
# ---------------------------------------------------------------------------


def _run_chatops(db, mocker, job_id, status, card_reactions, reply_text, *,
                 reply_processed=False):
    """Helper: set up a listing + card + thread reply, run _scan_chatops_commands.

    Returns (count, mock_tailor, mock_app, actual_job_id).  actual_job_id is
    the DB-assigned listing id (may differ from the job_id hint if JobListing
    is a dataclass without _replace support).
    """
    listing = _make_listing()
    listing = listing._replace(id=job_id) if hasattr(listing, "_replace") else listing
    db.insert_listing(listing)
    actual_id = listing.id
    db.update_pipeline_status(actual_id, status)

    job_card = _make_job_card(actual_id, ts="100.000")
    job_card["reactions"] = [
        {"name": r, "users": ["U_HUMAN"]} for r in card_reactions
    ]
    reply = _make_reply("200.000", reply_text, processed=reply_processed)

    mock_app = MagicMock()
    mock_app.client.conversations_replies.return_value = {
        "messages": [job_card, reply],
    }

    mock_tailor = mocker.patch("src.sweeper._handle_tailor")
    mocker.patch("src.sweeper._append_human_label")
    mocker.patch("src.sweeper._mark_regenerate_done")
    mocker.patch("src.sweeper._post_thread_reply")

    count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
    return count, mock_tailor, mock_app, actual_id


class TestRegenerateCommand:
    """!regenerate: delete folder + full regen when pencil is present."""

    def test_regenerate_fires_tailor_and_deletes_folder(
        self, db, mocker, tmp_path
    ):
        """!regenerate with pencil: folder removed, status reset, _handle_tailor called."""
        job_dir = tmp_path / "co_role_jid-regen1"
        job_dir.mkdir()
        (job_dir / "deep_research_context.txt").write_text("old research")
        (job_dir / "assets.json").write_text("{}")

        mocker.patch("src.tailor._find_existing_output", return_value=job_dir)
        mock_rmtree = mocker.patch("shutil.rmtree")

        count, mock_tailor, _, actual_id = _run_chatops(
            db, mocker, "jid-regen1", "tailored",
            card_reactions=["pencil"],
            reply_text="!regenerate",
        )
        mock_rmtree.assert_called_once_with(job_dir)
        mock_tailor.assert_called_once()
        assert count == 1

        # DB status must have been reset to triaged before tailor fires
        row = db.get_listing_by_id(actual_id)
        assert row["pipeline_status"] == "triaged"

    def test_regenerate_requires_pencil_emoji(self, db, mocker, tmp_path):
        """!regenerate without pencil on card → error reply, tailor NOT called."""
        mocker.patch("src.tailor._find_existing_output", return_value=None)

        count, mock_tailor, _, _actual_id = _run_chatops(
            db, mocker, "jid-regen-nopencil", "tailored",
            card_reactions=["+1"],  # no pencil
            reply_text="!regenerate",
        )
        mock_tailor.assert_not_called()
        assert count == 1  # command was processed (error path still counts)

    def test_regenerate_blocked_on_passed_listing(self, db, mocker, tmp_path):
        """!regenerate on a passed listing → error reply, tailor NOT called."""
        mocker.patch("src.tailor._find_existing_output", return_value=None)

        count, mock_tailor, _, _actual_id = _run_chatops(
            db, mocker, "jid-regen-passed", "passed",
            card_reactions=["pencil"],
            reply_text="!regenerate",
        )
        mock_tailor.assert_not_called()
        assert count == 1

    def test_regenerate_idempotent_on_second_sweep(self, db, mocker, tmp_path):
        """Reply already has arrows_counterclockwise receipt → skip entirely."""
        mocker.patch("src.tailor._find_existing_output", return_value=None)

        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "tailored")

        job_card = _make_job_card(listing.id, ts="100.000")
        job_card["reactions"] = [{"name": "pencil", "users": ["U_HUMAN"]}]
        # Reply already processed with arrows_counterclockwise
        reply = {
            "ts": "200.000",
            "text": "!regenerate",
            "reactions": [{"name": "arrows_counterclockwise", "users": ["UBOT"]}],
        }
        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply],
        }
        mock_tailor = mocker.patch("src.sweeper._handle_tailor")
        mocker.patch("src.sweeper._append_human_label")

        count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
        mock_tailor.assert_not_called()
        assert count == 0


# ---------------------------------------------------------------------------
# !polish command
# ---------------------------------------------------------------------------


class TestPolishCommand:
    """`!polish` on-demand polished resume generation."""

    def _run(self, db, status, mocker):
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, status)

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", "!polish", processed=False)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }

        mock_asset = mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
        return count, mock_asset, mock_app

    def test_polish_allowed_when_tailored(self, db, mocker):
        count, mock_asset, _ = self._run(db, "tailored", mocker)
        assert count == 1
        mock_asset.assert_called_once()
        _, call_kwargs = mock_asset.call_args[0], mock_asset.call_args
        assert call_kwargs[0][4] == "polish"

    @pytest.mark.parametrize("status", ["triaged", "saved", "applied", "passed"])
    def test_polish_blocked_on_non_tailored_status(self, db, mocker, status):
        count, mock_asset, mock_app = self._run(db, status, mocker)
        # On ineligible status (triaged/passed) the status gate blocks before !polish is reached;
        # on eligible-but-not-tailored (saved/applied) the !polish block posts an error reply.
        mock_asset.assert_not_called()

    def test_polish_idempotent_when_already_processed(self, db, mocker):
        """Reply with white_check_mark already → skip entirely."""
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "tailored")

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", "!polish", processed=True)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }
        mock_asset = mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")

        count = _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])
        mock_asset.assert_not_called()
        assert count == 0

    def test_polish_posts_error_when_not_tailored(self, db, mocker):
        """When status is 'saved', !polish should post a warning reply."""
        listing = _make_listing()
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")

        job_card = _make_job_card(listing.id, ts="100.000")
        reply = _make_reply("200.000", "!polish", processed=False)

        mock_app = MagicMock()
        mock_app.client.conversations_replies.return_value = {
            "messages": [job_card, reply]
        }
        mocker.patch("src.sweeper._handle_ondemand_asset")
        mocker.patch("src.sweeper._append_human_label")
        mock_post = mocker.patch("src.sweeper._post_thread_reply")

        _scan_chatops_commands(mock_app, db, "C_TEST", [job_card])

        mock_post.assert_called_once()
        call_text = mock_post.call_args[0][3]
        assert "tailor" in call_text.lower() or "pencil" in call_text.lower()


# ---------------------------------------------------------------------------
# _next_version_path unit tests
# ---------------------------------------------------------------------------


class TestNextVersionPath:
    """Versioned output path helper."""

    def test_first_call_returns_base_name(self, tmp_path):
        from src.tailor import _next_version_path
        result = _next_version_path(tmp_path, "Cover_Letter_Acme", ".docx")
        assert result == tmp_path / "Cover_Letter_Acme.docx"

    def test_second_call_returns_v2(self, tmp_path):
        from src.tailor import _next_version_path
        (tmp_path / "Cover_Letter_Acme.docx").touch()
        result = _next_version_path(tmp_path, "Cover_Letter_Acme", ".docx")
        assert result == tmp_path / "Cover_Letter_Acme_v2.docx"

    def test_third_call_returns_v3(self, tmp_path):
        from src.tailor import _next_version_path
        (tmp_path / "Cover_Letter_Acme.docx").touch()
        (tmp_path / "Cover_Letter_Acme_v2.docx").touch()
        result = _next_version_path(tmp_path, "Cover_Letter_Acme", ".docx")
        assert result == tmp_path / "Cover_Letter_Acme_v3.docx"

    def test_works_with_md_extension(self, tmp_path):
        from src.tailor import _next_version_path
        (tmp_path / "Interview_Prep_Acme.md").touch()
        result = _next_version_path(tmp_path, "Interview_Prep_Acme", ".md")
        assert result == tmp_path / "Interview_Prep_Acme_v2.md"


class TestExpireAlias:
    """!expired is a registered alias for !expire."""

    def test_expired_alias_in_state_commands(self):
        from src.sweeper import _CHATOPS_STATE_COMMANDS
        assert "!expired" in _CHATOPS_STATE_COMMANDS
        assert _CHATOPS_STATE_COMMANDS["!expired"] == "expired"

    def test_expire_in_state_commands(self):
        from src.sweeper import _CHATOPS_STATE_COMMANDS
        assert "!expire" in _CHATOPS_STATE_COMMANDS
        assert _CHATOPS_STATE_COMMANDS["!expire"] == "expired"

    def test_both_spellings_in_prefix_gate(self):
        from src.sweeper import _ALL_CHATOPS_PREFIXES
        assert "!expire" in _ALL_CHATOPS_PREFIXES
        assert "!expired" in _ALL_CHATOPS_PREFIXES

    def test_expired_badge_uses_no_entry_sign(self):
        from src.sweeper import _STATUS_BADGES
        assert ":no_entry_sign:" in _STATUS_BADGES["expired"]


# ---------------------------------------------------------------------------
# !trend --deep argument parsing + skill stoplist
# ---------------------------------------------------------------------------


class TestTrendArgs:
    """Verify !trend --deep N parsing and clamping."""

    def test_no_flag_returns_default(self):
        from src.sweeper import _TREND_DEFAULT_LIMIT, _parse_trend_args
        assert _parse_trend_args("!trend") == _TREND_DEFAULT_LIMIT
        assert _parse_trend_args("!trend ") == _TREND_DEFAULT_LIMIT

    def test_deep_flag_parses(self):
        from src.sweeper import _parse_trend_args
        assert _parse_trend_args("!trend --deep 250") == 250
        assert _parse_trend_args("!trend --deep 100") == 100

    def test_clamps_below_min(self):
        from src.sweeper import _TREND_MIN_LIMIT, _parse_trend_args
        assert _parse_trend_args("!trend --deep 1") == _TREND_MIN_LIMIT
        assert _parse_trend_args("!trend --deep 0") == _TREND_MIN_LIMIT

    def test_clamps_above_max(self):
        from src.sweeper import _TREND_MAX_LIMIT, _parse_trend_args
        assert _parse_trend_args("!trend --deep 9999") == _TREND_MAX_LIMIT

    def test_case_and_whitespace_insensitive(self):
        from src.sweeper import _parse_trend_args
        assert _parse_trend_args("!trend  --DEEP   200  ") == 200
        assert _parse_trend_args("!Trend --Deep\t150") == 150


class TestSkillStoplist:
    """Verify _parse_skills_csv filters extractor sentinel placeholders."""

    def test_filters_none_explicitly_stated(self):
        from src.sweeper import _parse_skills_csv
        assert _parse_skills_csv("Python, None explicitly stated, AWS") == [
            "Python", "AWS",
        ]

    def test_filters_common_sentinels(self):
        from src.sweeper import _parse_skills_csv
        assert _parse_skills_csv("Python, n/a, NA, Unknown, None, AWS") == [
            "Python", "AWS",
        ]

    def test_preserves_real_skills_with_punctuation(self):
        from src.sweeper import _parse_skills_csv
        assert _parse_skills_csv("Python 3.11, A/B Testing, k8s") == [
            "Python 3.11", "A/B Testing", "k8s",
        ]

    def test_handles_empty_and_none(self):
        from src.sweeper import _parse_skills_csv
        assert _parse_skills_csv("") == []
        assert _parse_skills_csv(None) == []


class TestTrendFormatting:
    """Verify share-% column and ellipsis truncation in trend report."""

    def test_share_percent_appears(self):
        from src.sweeper import _format_trend_report
        report = _format_trend_report(
            ({"Python": 5}, {}), 10,
            ({}, {}), 0,
            ({}, {}), 0,
            10,
        )
        # 5/10 = 50%
        assert "50%" in report

    def test_long_skill_uses_ellipsis_not_midword_cut(self):
        from src.sweeper import _format_trend_report
        report = _format_trend_report(
            ({"Cross-functional Leadership Across Teams": 3}, {}), 5,
            ({}, {}), 0,
            ({}, {}), 0,
            5,
        )
        # Old behavior would truncate to "Cross-functional Leade" (mid-word, no
        # marker). New behavior must use a horizontal ellipsis.
        assert "…" in report
        assert "Cross-functional Leade  " not in report

    def test_header_reflects_total(self):
        from src.sweeper import _format_trend_report
        report = _format_trend_report(
            ({}, {}), 0, ({}, {}), 0, ({}, {}), 0, 250,
        )
        assert "Last 250 Jobs" in report


class TestSentinelDropFromCanonical:
    """Verify _drop_sentinels scrubs LLM-emitted placeholder groups."""

    def test_drops_none_explicitly_stated(self):
        from src.sweeper import _drop_sentinels
        out = _drop_sentinels({"Python": 5, "None explicitly stated": 10, "AWS": 3})
        assert out == {"Python": 5, "AWS": 3}

    def test_drops_invented_phrasings(self):
        from src.sweeper import _drop_sentinels
        out = _drop_sentinels({
            "Python": 5,
            "None explicitly mentioned": 4,
            "Not specified": 3,
            "Not applicable": 2,
            "AWS": 1,
        })
        assert out == {"Python": 5, "AWS": 1}

    def test_keeps_real_skills_starting_with_n(self):
        from src.sweeper import _drop_sentinels
        # "Node.js", "NumPy", "NLP" should not be filtered.
        out = _drop_sentinels({"Node.js": 3, "NumPy": 2, "NLP": 1})
        assert out == {"Node.js": 3, "NumPy": 2, "NLP": 1}


class TestTrendChunks:
    """Verify the report splits into per-cohort chunks under Slack's section limit."""

    def test_returns_four_chunks(self):
        from src.sweeper import _format_trend_chunks
        chunks = _format_trend_chunks(
            ({"Python": 5}, {"k8s": 2}), 10,
            ({"SQL": 3}, {}), 6,
            ({"Java": 8}, {"Go": 4}), 20,
            36,
        )
        # header + 3 cohorts
        assert len(chunks) == 4
        assert "SKILL TRENDS — Last 36 Jobs" in chunks[0]
        assert "HIGH INTENT" in chunks[1]
        assert "PIPELINE" in chunks[2]
        assert "REJECTED" in chunks[3]

    def test_each_chunk_under_slack_section_limit(self):
        """At max --deep with maxed-out top-10 entries, no chunk should exceed 2900 chars."""
        from src.sweeper import _SLACK_SECTION_TEXT_MAX, _format_trend_chunks
        # Worst case: 10 long-name entries on each side per cohort.
        big_matched = {f"Skill Name Number {i:03d}": 99 for i in range(10)}
        big_missing = {f"Gap Skill Name {i:03d}": 88 for i in range(10)}
        chunks = _format_trend_chunks(
            (big_matched, big_missing), 500,
            (big_matched, big_missing), 500,
            (big_matched, big_missing), 500,
            500,
        )
        for chunk in chunks:
            wrapped = f"```\n{chunk}\n```"
            assert len(wrapped) <= _SLACK_SECTION_TEXT_MAX, (
                f"chunk exceeds Slack section limit: {len(wrapped)} chars"
            )
