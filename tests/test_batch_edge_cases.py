"""Tests for batch processing edge-case protections."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db import Database
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


# ---------------------------------------------------------------------------
# Test Case 1: Queue Rot — 7-Day TTL Garbage Collection
# ---------------------------------------------------------------------------

class TestQueueRotTTL:
    """Saved listings older than 7 days should be expired before batch submission."""

    def test_stale_saved_listing_expired_fresh_one_batched(self, db):
        # Create a fresh saved listing (2 days ago)
        fresh = _make_listing(title="Fresh Role", company="Fresh Co")
        db.insert_listing(fresh)
        db.update_pipeline_status(fresh.id, "saved")
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        db.conn.execute(
            "UPDATE listings SET date_ingested = ?, updated_at = ? WHERE id = ?",
            (two_days_ago, two_days_ago, fresh.id),
        )
        db.conn.commit()

        # Create a stale saved listing (8 days ago)
        stale = _make_listing(title="Stale Role", company="Stale Co")
        db.insert_listing(stale)
        db.update_pipeline_status(stale.id, "saved")
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        db.conn.execute(
            "UPDATE listings SET date_ingested = ?, updated_at = ? WHERE id = ?",
            (eight_days_ago, eight_days_ago, stale.id),
        )
        db.conn.commit()

        # Run garbage collection
        expired_count = db.expire_stale_saved(max_age_days=7)

        # Stale listing should be expired
        assert expired_count == 1
        stale_row = db.get_listing_by_id(stale.id)
        assert stale_row["pipeline_status"] == "expired"

        # Fresh listing should still be saved
        fresh_row = db.get_listing_by_id(fresh.id)
        assert fresh_row["pipeline_status"] == "saved"

        # Only the fresh listing should appear in get_saved_listings
        saved = db.get_saved_listings()
        assert len(saved) == 1
        assert saved[0]["id"] == fresh.id

    def test_batch_process_expires_before_submitting(self, db):
        """Integration: run_batch_process expires stale jobs before Phase B submit."""
        stale = _make_listing(title="Old Role", company="Old Co")
        db.insert_listing(stale)
        db.update_pipeline_status(stale.id, "saved")
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        db.conn.execute(
            "UPDATE listings SET date_ingested = ?, updated_at = ? WHERE id = ?",
            (eight_days_ago, eight_days_ago, stale.id),
        )
        db.conn.commit()

        # Patch Database to return our real db instance
        mock_profile = {"name": "Test", "llm_context": "", "settings": {}}
        with patch("src.batch_process.Database") as MockDB, \
             patch("src.batch_process.submit_batch") as mock_submit, \
             patch("src.batch_process.load_profile", return_value=mock_profile):
            MockDB.return_value.__enter__ = MagicMock(return_value=db)
            MockDB.return_value.__exit__ = MagicMock(return_value=False)

            from src.batch_process import run_batch_process
            result = run_batch_process()

        # Stale listing was expired, nothing to submit
        assert result["expired"] >= 1
        assert result["submitted"] == 0
        mock_submit.assert_not_called()

        row = db.get_listing_by_id(stale.id)
        assert row["pipeline_status"] == "expired"


# ---------------------------------------------------------------------------
# Test Case 2: retrieve_batch stub
# ---------------------------------------------------------------------------

class TestRetrieveBatchStub:
    """retrieve_batch is a no-op stub — OpenRouter tailor is synchronous."""

    def test_retrieve_batch_always_returns_true(self):
        from src.tailor import retrieve_batch
        assert retrieve_batch("openrouter-12345") is True
        assert retrieve_batch("any-batch-id") is True


# ---------------------------------------------------------------------------
# Test Case 3: Async tailor failure isolation
# ---------------------------------------------------------------------------

class TestAsyncTailorFailureIsolation:
    """A generate_assets crash for one job must not halt the concurrent batch."""

    def test_compilation_failure_isolated(self):
        import asyncio

        from src.tailor import _tailor_one_async

        valid_json = json.dumps({
            "match_analysis": "Good match.",
            "resume_bullet_edits": [],
        })

        mock_client = MagicMock()
        completion = MagicMock()
        completion.choices[0].message.content = valid_json
        mock_client.chat.completions.create = AsyncMock(return_value=completion)

        db_instance = MagicMock()

        call_log: list[tuple[str, str]] = []

        def mock_generate(job_id, assets, listing, **kw):
            if job_id == "job_a":
                raise Exception("Corrupted docx template")
            return "/output/test"

        def mock_update(job_id, status):
            call_log.append((job_id, status))

        db_instance.update_pipeline_status.side_effect = mock_update

        with patch("src.tailor.Database") as MockDB, \
             patch("src.tailor.generate_assets", side_effect=mock_generate):
            MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
            MockDB.return_value.__exit__ = MagicMock(return_value=False)

            async def run():
                await _tailor_one_async(mock_client, "job_a", "prompt", {}, "openai/gpt-4o-mini")
                await _tailor_one_async(mock_client, "job_b", "prompt", {}, "openai/gpt-4o-mini")

            asyncio.run(run())

        statuses = dict(call_log)
        assert statuses.get("job_a") == "failed_compilation"
        assert statuses.get("job_b") == "tailored"


# ---------------------------------------------------------------------------
# Test Case 4: "Stuck" Batch Reversion
# ---------------------------------------------------------------------------

class TestStuckBatchReversion:
    """processing_batch listings older than 48h should revert to saved."""

    def test_stuck_batch_reverted_to_saved(self, db):
        listing = _make_listing(title="Stuck Role", company="Stuck Co")
        db.insert_listing(listing)
        db.set_batch_id(listing.id, "batch_old")

        # Backdate updated_at to 49 hours ago
        forty_nine_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=49)
        ).isoformat()
        db.conn.execute(
            "UPDATE listings SET updated_at = ? WHERE id = ?",
            (forty_nine_hours_ago, listing.id),
        )
        db.conn.commit()

        reverted = db.revert_stuck_batches(max_age_hours=48)
        assert reverted == 1

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "saved"
        assert row["batch_id"] is None

    def test_recent_batch_not_reverted(self, db):
        listing = _make_listing(title="Active Role", company="Active Co")
        db.insert_listing(listing)
        db.set_batch_id(listing.id, "batch_new")
        # updated_at is now — well within 48h

        reverted = db.revert_stuck_batches(max_age_hours=48)
        assert reverted == 0

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "processing_batch"
        assert row["batch_id"] == "batch_new"

    def test_batch_process_reverts_stuck_before_retrieve(self, db):
        """Integration: run_batch_process reverts stuck jobs at startup."""
        listing = _make_listing(title="Stuck Role", company="Stuck Co")
        db.insert_listing(listing)
        db.set_batch_id(listing.id, "batch_stuck")
        forty_nine_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=49)
        ).isoformat()
        db.conn.execute(
            "UPDATE listings SET updated_at = ? WHERE id = ?",
            (forty_nine_hours_ago, listing.id),
        )
        db.conn.commit()

        mock_profile = {"name": "Test", "llm_context": "", "settings": {}}
        with patch("src.batch_process.Database") as MockDB, \
             patch("src.batch_process.submit_batch"), \
             patch("src.batch_process.load_profile", return_value=mock_profile):
            MockDB.return_value.__enter__ = MagicMock(return_value=db)
            MockDB.return_value.__exit__ = MagicMock(return_value=False)

            from src.batch_process import run_batch_process
            result = run_batch_process()

        assert result["reverted"] >= 1

        row = db.get_listing_by_id(listing.id)
        assert row["pipeline_status"] == "saved"
        assert row["batch_id"] is None
