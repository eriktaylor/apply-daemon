"""Tests for the SQLite database layer."""

from datetime import datetime, timedelta, timezone

import pytest

from src.db import (
    DEFAULT_DB_PATH,
    SEEN_ONLY,
    Database,
    _format_history_timeline,
    is_duplicate_email,
    resolve_db_path,
)
from src.models import JobListing


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
        "reason": "Strong match for backend role",
        "model_used": "gemma3:4b",
    }
    defaults.update(kwargs)
    return JobListing(**defaults)


class TestInsertAndQuery:
    def test_insert_listing(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        rows = db.get_recent_listings(hours=1)
        assert len(rows) == 1
        assert rows[0]["title"] == "Senior Backend Engineer"

    def test_insert_ignores_duplicate_id(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        db.insert_listing(listing)  # Same ID — should not raise
        rows = db.get_recent_listings(hours=1)
        assert len(rows) == 1

    def test_get_listings_by_verdict(self, db):
        db.insert_listing(_make_listing(verdict="YES"))
        db.insert_listing(_make_listing(verdict="NO", title="Junior Dev", company="Other"))
        yes_rows = db.get_listings_by_verdict("YES")
        assert len(yes_rows) == 1
        assert yes_rows[0]["verdict"] == "YES"


class TestListingDedup:
    def test_is_duplicate_listing(self, db):
        db.insert_listing(_make_listing())
        assert db.is_duplicate_listing("Senior Backend Engineer", "Acme Corp")

    def test_is_not_duplicate_different_title(self, db):
        db.insert_listing(_make_listing())
        assert not db.is_duplicate_listing("Staff Engineer", "Acme Corp")

    def test_is_not_duplicate_different_company(self, db):
        db.insert_listing(_make_listing())
        assert not db.is_duplicate_listing("Senior Backend Engineer", "Other Corp")

    def test_fuzzy_duplicate_case_insensitive(self, db):
        db.insert_listing(_make_listing())
        assert db.is_duplicate_listing("senior backend engineer", "acme corp")

    def test_fuzzy_duplicate_minor_variation(self, db):
        db.insert_listing(_make_listing())
        assert db.is_duplicate_listing("Sr. Backend Engineer", "Acme Corp")

    def test_fuzzy_duplicate_reordered_tokens(self, db):
        db.insert_listing(_make_listing())
        assert db.is_duplicate_listing("Backend Engineer, Senior", "Corp Acme")

    def test_fuzzy_not_duplicate_below_threshold(self, db):
        db.insert_listing(_make_listing())
        assert not db.is_duplicate_listing("Junior Frontend Developer", "Acme Corp")

    def test_fuzzy_none_handling(self, db):
        """None values should not crash the fuzzy matcher."""
        db.insert_listing(_make_listing())
        assert not db.is_duplicate_listing(None, None)


class TestEmailDedup:
    def test_duplicate_email_detected(self):
        existing = ["Jobs for you: Senior Engineer at Acme Corp, Remote"]
        assert is_duplicate_email(
            "Jobs for you: Senior Engineer at Acme Corp, Remote",
            existing,
        )

    def test_different_email_not_duplicate(self):
        existing = ["Jobs for you: Senior Engineer at Acme Corp"]
        assert not is_duplicate_email(
            "Your Google Alert: VP of Engineering at RocketScale",
            existing,
        )

    def test_empty_existing_not_duplicate(self):
        assert not is_duplicate_email("Some email text", [])

    def test_threshold_sensitivity(self):
        existing = ["Jobs for you: Senior Engineer at Acme Corp, Remote (US)"]
        # Very similar but not identical
        assert is_duplicate_email(
            "Jobs for you: Senior Engineer at Acme Corp, Remote (US). Apply now!",
            existing,
            threshold=0.8,
        )


class TestProcessedEmails:
    def test_record_and_retrieve(self, db):
        db.record_processed_email("abc123", "Email text preview here")
        texts = db.get_recent_email_texts(days=30)
        assert len(texts) == 1
        assert "Email text preview" in texts[0]

    def test_multiple_records(self, db):
        db.record_processed_email("aaa", "First email")
        db.record_processed_email("bbb", "Second email")
        texts = db.get_recent_email_texts(days=30)
        assert len(texts) == 2


class TestEmailLedger:
    """Message-ID idempotency ledger on processed_emails."""

    def test_unseen_message_id(self, db):
        assert db.is_email_id_seen("<abc@mail.gmail.com>") is False

    def test_seen_after_record(self, db):
        db.record_processed_email(
            "hash1", "preview",
            gmail_message_id="<abc@mail.gmail.com>",
            classification="JOB_DIGEST",
        )
        assert db.is_email_id_seen("<abc@mail.gmail.com>") is True
        assert db.is_email_id_seen("<other@mail.gmail.com>") is False

    def test_empty_message_id_never_seen(self, db):
        assert db.is_email_id_seen("") is False

    def test_backward_compatible_record_without_ledger_fields(self, db):
        db.record_processed_email("hash2", "preview only")
        texts = db.get_recent_email_texts(days=30)
        assert len(texts) == 1

    def test_skip_record_with_empty_preview_excluded_from_text_dedup(self, db):
        db.record_processed_email(
            "idhash", "", gmail_message_id="<skip@mail.gmail.com>", classification="SKIP"
        )
        assert db.is_email_id_seen("<skip@mail.gmail.com>") is True
        # Empty previews must not pollute the text-dedup corpus
        assert db.get_recent_email_texts(days=30) == []

    def test_migration_is_idempotent(self, tmp_path):
        path = tmp_path / "reopen.db"
        first = Database(path)
        first.record_processed_email(
            "h", "p", gmail_message_id="<persist@x>", classification="SKIP"
        )
        first.close()
        # Re-opening re-runs _init_schema; guarded ALTERs must not raise
        second = Database(path)
        assert second.is_email_id_seen("<persist@x>") is True
        second.close()


class TestPipelineStatus:
    def test_update_pipeline_status(self, db):
        db.insert_listing(_make_listing())
        listing = db.get_recent_listings(hours=1)[0]
        assert listing["pipeline_status"] == "triaged"
        assert db.update_pipeline_status(listing["id"], "saved")
        updated = db.get_listing_by_id(listing["id"])
        assert updated["pipeline_status"] == "saved"

    def test_update_invalid_status_rejected(self, db):
        db.insert_listing(_make_listing())
        listing = db.get_recent_listings(hours=1)[0]
        assert not db.update_pipeline_status(listing["id"], "bogus")
        unchanged = db.get_listing_by_id(listing["id"])
        assert unchanged["pipeline_status"] == "triaged"

    def test_update_nonexistent_listing(self, db):
        assert not db.update_pipeline_status("nonexistent-id", "saved")

    def test_all_valid_statuses_accepted(self, db):
        for status in Database.VALID_STATUSES:
            listing = _make_listing(title=f"Role {status}", company=f"Co {status}")
            db.insert_listing(listing)
            assert db.update_pipeline_status(listing.id, status)


class TestGetListingById:
    def test_found(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        row = db.get_listing_by_id(listing.id)
        assert row is not None
        assert row["title"] == "Senior Backend Engineer"

    def test_not_found(self, db):
        assert db.get_listing_by_id("nonexistent") is None


class TestGetDigestListings:
    def test_returns_triaged_and_saved(self, db):
        l1 = _make_listing(title="Role A", company="Co A", confidence=80)
        l2 = _make_listing(title="Role B", company="Co B", confidence=60)
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.update_pipeline_status(l2.id, "saved")
        rows = db.get_digest_listings(days=14)
        assert len(rows) == 2

    def test_excludes_passed_and_tailored(self, db):
        l1 = _make_listing(title="Role A", company="Co A")
        l2 = _make_listing(title="Role B", company="Co B")
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.update_pipeline_status(l1.id, "passed")
        db.update_pipeline_status(l2.id, "tailored")
        rows = db.get_digest_listings(days=14)
        assert len(rows) == 0

    def test_ordered_by_confidence_desc(self, db):
        l1 = _make_listing(title="Low", company="Co A", confidence=30)
        l2 = _make_listing(title="High", company="Co B", confidence=90)
        db.insert_listing(l1)
        db.insert_listing(l2)
        rows = db.get_digest_listings(days=14)
        assert rows[0]["title"] == "High"
        assert rows[1]["title"] == "Low"


class TestBatchProcessing:
    def test_set_batch_id(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        assert db.set_batch_id(listing.id, "batch_abc123")
        row = db.get_listing_by_id(listing.id)
        assert row["batch_id"] == "batch_abc123"
        assert row["pipeline_status"] == "processing_batch"

    def test_set_batch_id_nonexistent(self, db):
        assert not db.set_batch_id("nonexistent", "batch_abc123")

    def test_get_listings_by_batch(self, db):
        l1 = _make_listing(title="Role A", company="Co A")
        l2 = _make_listing(title="Role B", company="Co B")
        l3 = _make_listing(title="Role C", company="Co C")
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.insert_listing(l3)
        db.set_batch_id(l1.id, "batch_1")
        db.set_batch_id(l2.id, "batch_1")
        db.set_batch_id(l3.id, "batch_2")
        rows = db.get_listings_by_batch("batch_1")
        assert len(rows) == 2

    def test_get_processing_batch_ids(self, db):
        l1 = _make_listing(title="Role A", company="Co A")
        l2 = _make_listing(title="Role B", company="Co B")
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.set_batch_id(l1.id, "batch_1")
        db.set_batch_id(l2.id, "batch_2")
        batch_ids = db.get_processing_batch_ids()
        assert set(batch_ids) == {"batch_1", "batch_2"}

    def test_get_processing_batch_ids_excludes_completed(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        db.set_batch_id(listing.id, "batch_1")
        db.update_pipeline_status(listing.id, "tailored")
        assert db.get_processing_batch_ids() == []

    def test_get_saved_listings(self, db):
        l1 = _make_listing(title="Saved Role", company="Co A", confidence=80)
        l2 = _make_listing(title="Triaged Role", company="Co B", confidence=90)
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.update_pipeline_status(l1.id, "saved")
        rows = db.get_saved_listings()
        assert len(rows) == 1
        assert rows[0]["title"] == "Saved Role"

    def test_get_saved_listings_ordered_by_confidence(self, db):
        l1 = _make_listing(title="Low", company="Co A", confidence=30)
        l2 = _make_listing(title="High", company="Co B", confidence=90)
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.update_pipeline_status(l1.id, "saved")
        db.update_pipeline_status(l2.id, "saved")
        rows = db.get_saved_listings()
        assert rows[0]["title"] == "High"


class TestSlackNotified:
    def test_new_listing_not_notified(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        row = db.get_listing_by_id(listing.id)
        assert row["slack_notified"] == 0

    def test_mark_slack_notified(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        assert db.mark_slack_notified(listing.id)
        row = db.get_listing_by_id(listing.id)
        assert row["slack_notified"] == 1

    def test_mark_nonexistent_returns_false(self, db):
        assert not db.mark_slack_notified("nonexistent")

    def test_digest_excludes_notified(self, db):
        l1 = _make_listing(title="Already Posted", company="Co A", confidence=80)
        l2 = _make_listing(title="New Listing", company="Co B", confidence=70)
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.mark_slack_notified(l1.id)
        rows = db.get_digest_listings(days=14)
        assert len(rows) == 1
        assert rows[0]["title"] == "New Listing"

    def test_digest_returns_unnotified_only(self, db):
        l1 = _make_listing(title="Role A", company="Co A")
        l2 = _make_listing(title="Role B", company="Co B")
        db.insert_listing(l1)
        db.insert_listing(l2)
        db.mark_slack_notified(l1.id)
        db.mark_slack_notified(l2.id)
        rows = db.get_digest_listings(days=14)
        assert len(rows) == 0

    def test_digest_excludes_unanimous_no_by_status(self, db):
        # Machine rejections (confidence below threshold) store pipeline_status="rejected".
        # The status gate IN ('triaged', 'saved') excludes them — no separate verdict filter needed.
        good = _make_listing(title="Good Role", company="Co A", verdict="YES", confidence=80)
        machine_rejected = _make_listing(
            title="Bad Role", company="Co B", verdict="NO", confidence=10,
        )
        db.insert_listing(good)
        db.insert_listing(machine_rejected)
        db.update_pipeline_status(machine_rejected.id, "rejected")
        rows = db.get_digest_listings(days=14)
        assert len(rows) == 1
        assert rows[0]["title"] == "Good Role"

    def test_digest_auto_passes_stale_no_in_triaged_status(self, db):
        """Defense in depth: a NO row left in pipeline_status='triaged'
        (e.g. inserted before the auto-pass fix) must NOT surface in the
        digest, and the row should be flipped to 'passed' on the way out
        so any future UI lookup sees the correct status."""
        good = _make_listing(title="Good Role", company="Co A", verdict="YES", confidence=80)
        stale_no = _make_listing(
            title="Sourcing Strategist", company="nan",
            verdict="NO", confidence=95,
        )
        db.insert_listing(good)
        db.insert_listing(stale_no)
        # stale_no defaults to pipeline_status='triaged'

        rows = db.get_digest_listings(days=14)

        titles = [r["title"] for r in rows]
        assert "Sourcing Strategist" not in titles, (
            "A NO verdict must never surface in the digest, even if its "
            "pipeline_status is still 'triaged' from a previous run"
        )
        assert titles == ["Good Role"]

        # Side-effect: the stale NO row should now be 'passed' in the DB.
        flipped = db.get_listing_by_id(stale_no.id)
        assert flipped["pipeline_status"] == "passed"

    def test_digest_auto_passes_stale_no_in_saved_status(self, db):
        """Same guarantee for NO listings somehow saved (status='saved')."""
        stale_no = _make_listing(verdict="NO", confidence=88)
        db.insert_listing(stale_no)
        db.update_pipeline_status(stale_no.id, "saved")

        rows = db.get_digest_listings(days=14)
        assert rows == []

        flipped = db.get_listing_by_id(stale_no.id)
        assert flipped["pipeline_status"] == "passed"

    def test_digest_leaves_non_no_active_rows_untouched(self, db):
        """The auto-pass sweep must only target verdict='NO' rows."""
        maybe = _make_listing(title="Maybe Role", verdict="MAYBE", confidence=65)
        db.insert_listing(maybe)

        rows = db.get_digest_listings(days=14)
        assert len(rows) == 1
        assert rows[0]["title"] == "Maybe Role"

        row = db.get_listing_by_id(maybe.id)
        assert row["pipeline_status"] == "triaged"

    def test_digest_excludes_rejected_status_regardless_of_verdict(self, db):
        # Any listing with pipeline_status='rejected' is excluded, regardless of verdict.
        listing = _make_listing(title="Passed Over", company="Co", verdict="NO")
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "rejected")
        rows = db.get_digest_listings(days=14)
        assert len(rows) == 0


class TestListingHistory:
    def test_no_history_returns_empty(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        result = db.get_listing_history("Senior Backend Engineer", "Acme Corp", listing.id)
        assert result == ""

    def test_single_prior_encounter(self, db):
        old = _make_listing(title="Senior Backend Engineer", company="Acme Corp")
        db.insert_listing(old)
        db.update_pipeline_status(old.id, "passed")

        current = _make_listing(title="Senior Backend Engineer", company="Acme Corp")
        db.insert_listing(current)

        result = db.get_listing_history("Senior Backend Engineer", "Acme Corp", current.id)
        assert "`passed`" in result
        assert "➔" not in result

    def test_multiple_prior_encounters(self, db):
        l1 = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(l1)
        db.update_pipeline_status(l1.id, "passed")

        l2 = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(l2)
        db.update_pipeline_status(l2.id, "saved")

        current = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(current)

        result = db.get_listing_history("Backend Engineer", "Acme Corp", current.id)
        assert "`passed`" in result
        assert "`saved`" in result
        assert "➔" in result

    def test_fuzzy_matches_minor_variation(self, db):
        old = _make_listing(title="Sr. Backend Engineer", company="Acme Corp")
        db.insert_listing(old)
        db.update_pipeline_status(old.id, "tailored")

        current = _make_listing(title="Senior Backend Engineer", company="Acme Corp")
        db.insert_listing(current)

        result = db.get_listing_history("Senior Backend Engineer", "Acme Corp", current.id)
        assert "`tailored`" in result

    def test_triaged_status_maps_to_ignored(self, db):
        old = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(old)
        # Default status is triaged — should display as "ignored"

        current = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(current)

        result = db.get_listing_history("Backend Engineer", "Acme Corp", current.id)
        assert "`ignored`" in result

    def test_excludes_current_job_id(self, db):
        listing = _make_listing()
        db.insert_listing(listing)
        result = db.get_listing_history("Senior Backend Engineer", "Acme Corp", listing.id)
        assert result == ""

    def test_different_company_not_matched(self, db):
        old = _make_listing(title="Backend Engineer", company="Different Corp")
        db.insert_listing(old)

        current = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(current)

        result = db.get_listing_history("Backend Engineer", "Acme Corp", current.id)
        assert result == ""

    def test_chronological_order(self, db):
        # Insert oldest first
        l1 = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(l1)
        db.update_pipeline_status(l1.id, "passed")
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        db.conn.execute(
            "UPDATE listings SET date_ingested = ? WHERE id = ?", (old_date, l1.id)
        )

        l2 = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(l2)
        db.update_pipeline_status(l2.id, "saved")
        mid_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        db.conn.execute(
            "UPDATE listings SET date_ingested = ? WHERE id = ?", (mid_date, l2.id)
        )
        db.conn.commit()

        current = _make_listing(title="Backend Engineer", company="Acme Corp")
        db.insert_listing(current)

        result = db.get_listing_history("Backend Engineer", "Acme Corp", current.id)
        passed_pos = result.index("`passed`")
        saved_pos = result.index("`saved`")
        assert passed_pos < saved_pos


class TestFormatHistoryTimeline:
    def _make_row(self, status, days_ago):
        dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return {
            "pipeline_status": status,
            "date_ingested": dt.isoformat(),
        }

    def test_single_entry(self):
        matches = [self._make_row("passed", 30)]
        result = _format_history_timeline(matches, Database._STATUS_DISPLAY)
        assert "`passed`" in result
        assert "➔" not in result

    def test_four_entries_no_truncation(self):
        matches = [
            self._make_row("passed", 90),
            self._make_row("triaged", 60),
            self._make_row("saved", 30),
            self._make_row("tailored", 10),
        ]
        result = _format_history_timeline(matches, Database._STATUS_DISPLAY)
        assert result.count("➔") == 3
        assert "..." not in result

    def test_five_entries_truncated(self):
        matches = [
            self._make_row("passed", 150),
            self._make_row("triaged", 120),
            self._make_row("saved", 90),
            self._make_row("tailored", 60),
            self._make_row("expired", 30),
        ]
        result = _format_history_timeline(matches, Database._STATUS_DISPLAY)
        assert "..." in result
        # First entry present
        assert "`passed`" in result
        # Last 3 present
        assert "`saved`" in result
        assert "`tailored`" in result
        assert "`expired`" in result
        # Second entry (triaged/ignored) truncated
        parts = result.split(" ➔ ")
        assert parts[0].startswith("`passed`")
        assert parts[1] == "..."

    def test_six_entries_still_shows_first_and_last_three(self):
        matches = [
            self._make_row("passed", 180),
            self._make_row("triaged", 150),
            self._make_row("saved", 120),
            self._make_row("expired", 90),
            self._make_row("tailored", 60),
            self._make_row("applied", 30),
        ]
        result = _format_history_timeline(matches, Database._STATUS_DISPLAY)
        parts = result.split(" ➔ ")
        assert len(parts) == 5  # first + ... + last3
        assert parts[1] == "..."
        assert "`applied`" in parts[-1]


class TestStatusAwareDedup:
    """upsert_listing() blocks pass/expire within pass_window, allows after."""

    def _insert_and_set_status(self, db, status: str) -> JobListing:
        listing = _make_listing(title="ML Engineer", company="Stripe")
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, status)
        return listing

    def test_passed_listing_blocks_within_pass_window(self, db):
        self._insert_and_set_status(db, "passed")
        incoming = _make_listing(title="ML Engineer", company="Stripe")
        was_update, existing_id = db.upsert_listing(
            incoming, window_days=30, pass_window_days=180,
        )
        assert was_update is True
        assert existing_id is not None
        # Data fields should NOT be updated for a blocked pass
        row = db.get_listing_by_id(existing_id)
        assert row["pipeline_status"] == "passed"

    def test_expired_listing_blocks_within_pass_window(self, db):
        self._insert_and_set_status(db, "expired")
        incoming = _make_listing(title="ML Engineer", company="Stripe")
        was_update, existing_id = db.upsert_listing(
            incoming, window_days=30, pass_window_days=180,
        )
        assert was_update is True
        # Status still expired (blocked, not revived)
        row = db.get_listing_by_id(existing_id)
        assert row["pipeline_status"] == "expired"

    def test_passed_listing_allows_reingest_after_pass_window(self, db):
        """After pass_window_days, the same job can re-enter as a fresh row."""
        listing = _make_listing(title="ML Engineer", company="Stripe")
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "passed")
        # Backdate the original row past the pass window
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        db.conn.execute(
            "UPDATE listings SET date_ingested = ? WHERE id = ?",
            (old_date, listing.id),
        )
        db.conn.commit()
        incoming = _make_listing(title="ML Engineer", company="Stripe")
        was_update, existing_id = db.upsert_listing(
            incoming, window_days=30, pass_window_days=180,
        )
        # Should be a fresh INSERT (old row outside both windows)
        assert was_update is False
        assert existing_id is None

    def test_active_listing_preserves_status(self, db):
        """Non-pass/expire status updates data fields and keeps pipeline_status."""
        listing = _make_listing(title="ML Engineer", company="Stripe", reason="Original")
        db.insert_listing(listing)
        db.update_pipeline_status(listing.id, "saved")
        incoming = _make_listing(title="ML Engineer", company="Stripe", reason="Updated")
        was_update, existing_id = db.upsert_listing(
            incoming, window_days=30, pass_window_days=180,
        )
        assert was_update is True
        row = db.get_listing_by_id(existing_id)
        assert row["pipeline_status"] == "saved"
        assert row["reason"] == "Updated"


class TestResolveDbPath:
    """Path resolution precedence: explicit arg → $APPLY_DAEMON_DB → default."""

    def test_explicit_arg_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPLY_DAEMON_DB", str(tmp_path / "env.db"))
        explicit = tmp_path / "explicit.db"
        assert resolve_db_path(explicit) == explicit

    def test_env_var_used_when_no_arg(self, tmp_path, monkeypatch):
        target = tmp_path / "env.db"
        monkeypatch.setenv("APPLY_DAEMON_DB", str(target))
        assert resolve_db_path() == target

    def test_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("APPLY_DAEMON_DB", raising=False)
        assert resolve_db_path() == DEFAULT_DB_PATH

    def test_blank_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("APPLY_DAEMON_DB", "   ")
        assert resolve_db_path() == DEFAULT_DB_PATH

    def test_expands_user_home(self, monkeypatch):
        monkeypatch.setenv("APPLY_DAEMON_DB", "~/apply.db")
        assert "~" not in str(resolve_db_path())

    def test_database_honours_env_var(self, tmp_path, monkeypatch):
        target = tmp_path / "from_env.db"
        monkeypatch.setenv("APPLY_DAEMON_DB", str(target))
        database = Database()
        try:
            assert database.db_path == target
            assert target.exists()
        finally:
            database.close()


class TestBusyTimeout:
    def test_busy_timeout_is_set(self, db):
        (value,) = db.conn.execute("PRAGMA busy_timeout").fetchone()
        assert value == 5000


class TestReviewQueue:
    """D-1 — CLI review queue. presented_at is paging state, not a gate."""

    def _seed(self, db, **kw):
        listing = _make_listing(**kw)
        db.insert_listing(listing)
        return listing.id

    def test_ranks_by_the_post_research_score_where_one_exists(self, db):
        """R-4 — the feed ranks by the re-score, not by the Stage 5 number it
        replaced. Ranking by the stale column left 106 of 172 enriched rows
        tied at exactly 95, which the band tiebreak cannot order."""
        demoted = self._seed(db, title="Looked good", company="A", confidence=95)
        steady = self._seed(db, title="Actually good", company="B", confidence=90)
        db.set_post_research_score(demoted, "MAYBE", 40)
        rows = db.get_review_queue(limit=10)
        assert [r["id"] for r in rows] == [steady, demoted]

    def test_a_post_research_no_leaves_the_queue(self, db):
        job_id = self._seed(db, title="Reads worse up close", company="A",
                            confidence=95)
        db.set_post_research_score(job_id, "NO", 10)
        assert db.get_review_queue(limit=10) == []

    def test_the_confidence_gate_reads_the_effective_score(self, db):
        job_id = self._seed(db, title="Optimistic", company="A", confidence=90)
        db.set_post_research_score(job_id, "MAYBE", 30)
        assert db.get_review_queue(limit=10, min_confidence_pct=50) == []

    def test_stage5_survives_the_write(self, db):
        """The two-value design: `deep-dive` reports the disagreement, so a
        write-back that overwrote Stage 5 would destroy the verb."""
        job_id = self._seed(db, title="Both scores", company="A", confidence=95)
        db.set_post_research_score(job_id, "MAYBE", 40)
        row = db.get_listing_by_id(job_id)
        assert (row["verdict"], row["confidence"]) == ("YES", 95)
        assert (row["post_research_verdict"], row["post_research_confidence"]) == (
            "MAYBE", 40)
        assert row["post_research_at"]

    def test_counts_agree_with_the_queue_they_describe(self, db):
        """status's numbers and the feed must apply the same rule, or the
        surface reports listings it will never show."""
        job_id = self._seed(db, title="Dropped by the re-score", company="A",
                            confidence=95)
        db.set_post_research_score(job_id, "NO", 10)
        assert db.get_queue_stats()["reviewable"] == 0
        assert db.fresh_counts_by_status(30) == {}
        assert db.count_stale_reviewable(1) == 0

    def test_returns_only_review_statuses(self, db):
        keep = self._seed(db, title="Keep", company="A", confidence=80)
        drop = self._seed(db, title="Drop", company="B", confidence=80)
        db.update_pipeline_status(drop, "passed")
        rows = db.get_review_queue(limit=10)
        assert [r["id"] for r in rows] == [keep]

    def test_auto_tier_ranks_before_auto_queued(self, db):
        queued = self._seed(db, title="Queued", company="A", confidence=95)
        auto = self._seed(db, title="Auto", company="B", confidence=60)
        db.update_pipeline_status(queued, "auto_queued")
        db.update_pipeline_status(auto, "auto")
        rows = db.get_review_queue(limit=10)
        # 'auto' wins despite lower confidence — its research is cached.
        assert [r["id"] for r in rows] == [auto, queued]

    def test_orders_by_confidence_within_tier(self, db):
        low = self._seed(db, title="Low", company="A", confidence=60)
        high = self._seed(db, title="High", company="B", confidence=95)
        for job_id in (low, high):
            db.update_pipeline_status(job_id, "auto_queued")
        rows = db.get_review_queue(limit=10)
        assert [r["id"] for r in rows] == [high, low]

    def test_respects_limit(self, db):
        for i in range(5):
            self._seed(db, title=f"Role {i}", company=f"Co {i}", confidence=80)
        assert len(db.get_review_queue(limit=3)) == 3

    def test_filters_by_confidence(self, db):
        self._seed(db, title="Weak", company="A", confidence=30)
        strong = self._seed(db, title="Strong", company="B", confidence=90)
        rows = db.get_review_queue(limit=10, min_confidence_pct=50)
        assert [r["id"] for r in rows] == [strong]

    def test_excludes_no_verdicts(self, db):
        self._seed(db, title="Nope", company="A", verdict="NO", confidence=90)
        assert db.get_review_queue(limit=10) == []

    def test_the_feed_retires_what_it_shows(self, db):
        """A shown listing leaves the feed permanently, not for 120 minutes.

        Re-showing was the defect: confidence is stable, so re-ranking the
        same pool every run returned the same winners forever — measured,
        three rows led the page four runs running while nine fresher
        enrichments sat behind them.
        """
        job_id = self._seed(db, title="Shown", company="A", confidence=80)
        db.mark_presented([job_id])
        assert db.get_review_queue(limit=10) == []

    def test_session_window_pages_forward(self, db):
        first = self._seed(db, title="First", company="A", confidence=90)
        second = self._seed(db, title="Second", company="B", confidence=80)
        db.mark_presented([first])
        rows = db.get_review_queue(limit=10)
        assert [r["id"] for r in rows] == [second]

    def test_retired_rows_are_reachable_as_the_backlog(self, db):
        """What makes retirement safe rather than lossy — the rows stop
        competing for a slot but stay one command away."""
        job_id = self._seed(db, title="Shown", company="A", confidence=80)
        db.mark_presented([job_id])
        rows = db.get_review_queue(limit=10, seen=SEEN_ONLY)
        assert [r["id"] for r in rows] == [job_id]
        assert db.count_seen_undecided() == 1

    def test_backlog_excludes_decided_rows(self, db):
        job_id = self._seed(db, title="Shown", company="A", confidence=80)
        db.mark_presented([job_id])
        db.update_pipeline_status(job_id, "passed")
        assert db.count_seen_undecided() == 0

    def test_age_of_presentation_is_irrelevant(self, db):
        job_id = self._seed(db, title="Stale", company="A", confidence=80)
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        db.conn.execute(
            "UPDATE listings SET presented_at = ? WHERE id = ?", (old, job_id)
        )
        db.conn.commit()
        assert db.get_review_queue(limit=10) == []
        assert len(db.get_review_queue(limit=10, seen=SEEN_ONLY)) == 1

    def test_recency_breaks_ties_within_a_band(self, db):
        """Confidence saturates, so it cannot be the final tiebreak — that
        sorted a growing backlog by an almost-constant key and surfaced the
        oldest rows first."""
        old = self._seed(db, title="Old", company="A", confidence=99)
        new = self._seed(db, title="New", company="B", confidence=96)
        stamp = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        db.conn.execute("UPDATE listings SET date_ingested = ? WHERE id = ?",
                        (stamp, old))
        db.conn.commit()
        rows = db.get_review_queue(limit=10)
        assert [r["id"] for r in rows] == [new, old]

    def test_mark_presented_counts_rows(self, db):
        a = self._seed(db, title="A", company="A")
        b = self._seed(db, title="B", company="B")
        assert db.mark_presented([a, b]) == 2

    def test_mark_presented_empty_is_noop(self, db):
        assert db.mark_presented([]) == 0

    def test_tier_rank_exposed_to_caller(self, db):
        job_id = self._seed(db, title="Auto", company="A", confidence=80)
        db.update_pipeline_status(job_id, "auto")
        # The CLI surfaces the tier so the skill knows a deep-dive is free.
        assert db.get_review_queue(limit=1)[0]["tier_rank"] == 0


class TestTrendSkillSampling:
    """`limit` is per pipeline_status, not global.

    Ingestion outruns decisions by two orders of magnitude in production, so
    a global recency window returns nothing but fresh intake — the decided
    rows carrying the human signal fall outside it, and !trend reported on a
    single cohort while looking like it had covered them all.
    """

    def _seed(self, db, status: str, n: int) -> None:
        for i in range(n):
            listing = _make_listing(
                title=f"{status} role {i}",
                company=f"Co {i}",
                matching_skills='["Python"]',
            )
            db.insert_listing(listing)
            db.update_pipeline_status(listing.id, status)

    def test_lopsided_intake_does_not_starve_decided_rows(self, db):
        self._seed(db, "auto_queued", 40)
        self._seed(db, "tailored", 2)
        self._seed(db, "passed", 3)

        rows = db.get_trend_skills(limit=10)
        by_status = {}
        for row in rows:
            by_status[row["pipeline_status"]] = by_status.get(row["pipeline_status"], 0) + 1

        assert by_status["auto_queued"] == 10   # capped
        assert by_status["tailored"] == 2       # kept whole
        assert by_status["passed"] == 3

    def test_skips_rows_with_no_skills_at_all(self, db):
        listing = _make_listing(matching_skills="", missing_skills="")
        db.insert_listing(listing)
        assert db.get_trend_skills(limit=10) == []
