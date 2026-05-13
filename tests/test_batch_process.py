"""Tests for the batch processor module."""

from unittest.mock import MagicMock, patch

from src.batch_process import run_batch_process

_MOCK_PROFILE = {
    "name": "Test",
    "llm_context": "test profile",
    "settings": {"batch_process_days": 3},
}


class TestRunBatchProcess:
    @patch("src.batch_process.load_profile", return_value=_MOCK_PROFILE)
    @patch("src.batch_process.submit_batch")
    @patch("src.batch_process.Database")
    def test_no_saved_listings(self, MockDB, mock_submit, mock_profile):
        db_instance = MagicMock()
        db_instance.revert_stuck_batches.return_value = 0
        db_instance.expire_stale_saved.return_value = 0
        db_instance.get_saved_listings.return_value = []
        MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
        MockDB.return_value.__exit__ = MagicMock(return_value=False)

        result = run_batch_process()
        assert result["submitted"] == 0
        mock_submit.assert_not_called()

    @patch("src.batch_process.load_profile", return_value=_MOCK_PROFILE)
    @patch("src.batch_process.submit_batch")
    @patch("src.batch_process.Database")
    def test_submits_saved_listings(self, MockDB, mock_submit, mock_profile):
        db_instance = MagicMock()
        db_instance.revert_stuck_batches.return_value = 0
        db_instance.expire_stale_saved.return_value = 0
        saved_row_1 = {"id": "job_1"}
        saved_row_2 = {"id": "job_2"}
        db_instance.get_saved_listings.return_value = [saved_row_1, saved_row_2]
        MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
        MockDB.return_value.__exit__ = MagicMock(return_value=False)
        mock_submit.return_value = "openrouter-12345"

        result = run_batch_process()
        assert result["submitted"] == 2
        mock_submit.assert_called_once_with(["job_1", "job_2"])

    @patch("src.batch_process.load_profile", return_value=_MOCK_PROFILE)
    @patch("src.batch_process.submit_batch")
    @patch("src.batch_process.Database")
    def test_housekeeping_counts_reported(self, MockDB, mock_submit, mock_profile):
        db_instance = MagicMock()
        db_instance.revert_stuck_batches.return_value = 2
        db_instance.expire_stale_saved.return_value = 3
        db_instance.get_saved_listings.return_value = []
        MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
        MockDB.return_value.__exit__ = MagicMock(return_value=False)

        result = run_batch_process()
        assert result["reverted"] == 2
        assert result["expired"] == 3

    @patch("src.batch_process.load_profile", return_value=_MOCK_PROFILE)
    @patch("src.batch_process.submit_batch")
    @patch("src.batch_process.Database")
    def test_submit_failure_does_not_crash(self, MockDB, mock_submit, mock_profile):
        db_instance = MagicMock()
        db_instance.revert_stuck_batches.return_value = 0
        db_instance.expire_stale_saved.return_value = 0
        db_instance.get_saved_listings.return_value = [{"id": "job_1"}]
        MockDB.return_value.__enter__ = MagicMock(return_value=db_instance)
        MockDB.return_value.__exit__ = MagicMock(return_value=False)
        mock_submit.side_effect = RuntimeError("OpenRouter error")

        result = run_batch_process()
        assert result["submitted"] == 0
