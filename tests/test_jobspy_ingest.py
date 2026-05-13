"""Unit tests for src/jobspy_ingest.py — Track A (JobSpy polling) helpers.

Tests cover _format_salary(), _row_to_extracted_listing(), _is_truncated(),
the urllib3 block observer, and the scrape-retry loop. No external calls.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.jobspy_ingest import (
    MAX_BLOCK_RETRIES,
    _format_salary,
    _is_indeed_detail_url,
    _is_truncated,
    _row_to_extracted_listing,
    _scrape_jobs_with_retries,
    _scrape_with_block_detection,
    _Urllib3BlockObserver,
)


def _make_row(**kwargs) -> pd.Series:
    """Build a minimal JobSpy-like DataFrame row (pandas Series)."""
    defaults = {
        "title": "Senior ML Engineer",
        "company": "Acme Corp",
        "location": "San Francisco, CA",
        "is_remote": False,
        "min_amount": None,
        "max_amount": None,
        "interval": None,
        "currency": "USD",
        "description": "We are looking for an ML engineer to join our team.",
        "job_url": "https://jobs.acme.com/ml-engineer",
        "site": "indeed",
    }
    defaults.update(kwargs)
    return pd.Series(defaults)


# ---------------------------------------------------------------------------
# _format_salary()
# ---------------------------------------------------------------------------

class TestFormatSalary:
    def test_yearly_min_and_max(self):
        row = _make_row(min_amount=120000, max_amount=180000, interval="yearly")
        assert _format_salary(row) == "$120,000–$180,000 / year"

    def test_yearly_interval_variants(self):
        row = _make_row(min_amount=100000, max_amount=150000, interval="annual")
        assert _format_salary(row) == "$100,000–$150,000 / year"

    def test_hourly(self):
        row = _make_row(min_amount=50.0, max_amount=75.0, interval="hourly")
        assert _format_salary(row) == "$50.00–$75.00 / hour"

    def test_monthly(self):
        row = _make_row(min_amount=8000, max_amount=12000, interval="monthly")
        assert _format_salary(row) == "$8,000–$12,000 / month"

    def test_min_only(self):
        row = _make_row(min_amount=90000, max_amount=None, interval="yearly")
        assert _format_salary(row) == "$90,000+ / year"

    def test_no_salary_data(self):
        row = _make_row(min_amount=None, max_amount=None, interval=None)
        assert _format_salary(row) == "not listed"

    def test_none_interval_defaults_to_yearly(self):
        row = _make_row(min_amount=130000, max_amount=200000, interval=None)
        result = _format_salary(row)
        assert "130,000" in result
        assert "200,000" in result

    def test_invalid_amount_returns_not_listed(self):
        row = _make_row(min_amount="N/A", max_amount="TBD", interval="yearly")
        assert _format_salary(row) == "not listed"


# ---------------------------------------------------------------------------
# _row_to_extracted_listing()
# ---------------------------------------------------------------------------

class TestRowToExtractedListing:
    def test_basic_mapping(self):
        row = _make_row()
        anchor = _row_to_extracted_listing(row)
        assert anchor.title == "Senior ML Engineer"
        assert anchor.company == "Acme Corp"
        assert anchor.location == "San Francisco, CA"
        assert anchor.links == ["https://jobs.acme.com/ml-engineer"]
        assert anchor.salary == "not listed"

    def test_description_truncated_to_2000(self):
        long_desc = "A" * 5000
        row = _make_row(description=long_desc)
        anchor = _row_to_extracted_listing(row)
        assert len(anchor.description) == 2000

    def test_job_summary_first_300_chars(self):
        desc = "B" * 500
        row = _make_row(description=desc)
        anchor = _row_to_extracted_listing(row)
        assert anchor.job_summary == "B" * 300

    def test_is_remote_appends_remote_to_location(self):
        row = _make_row(location="United States", is_remote=True)
        anchor = _row_to_extracted_listing(row)
        assert "(Remote)" in anchor.location

    def test_is_remote_no_duplicate_if_already_remote(self):
        row = _make_row(location="Remote", is_remote=True)
        anchor = _row_to_extracted_listing(row)
        assert anchor.location.count("Remote") == 1
        assert "(Remote)" not in anchor.location  # no duplicate

    def test_is_remote_true_no_location(self):
        row = _make_row(location=None, is_remote=True)
        anchor = _row_to_extracted_listing(row)
        assert anchor.location == "Remote"

    def test_empty_description(self):
        row = _make_row(description=None)
        anchor = _row_to_extracted_listing(row)
        assert anchor.description == ""
        assert anchor.job_summary == ""

    def test_no_job_url(self):
        row = _make_row(job_url=None)
        anchor = _row_to_extracted_listing(row)
        assert anchor.links == []

    def test_salary_formatted_from_row(self):
        row = _make_row(min_amount=120000, max_amount=160000, interval="yearly")
        anchor = _row_to_extracted_listing(row)
        assert anchor.salary == "$120,000–$160,000 / year"

    def test_whitespace_stripped(self):
        row = _make_row(title="  ML Engineer  ", company="  Stripe  ")
        anchor = _row_to_extracted_listing(row)
        assert anchor.title == "ML Engineer"
        assert anchor.company == "Stripe"


# ---------------------------------------------------------------------------
# _is_truncated()
# ---------------------------------------------------------------------------

class TestIsTruncated:
    def _long_desc(self, word_count: int) -> str:
        """Build a description with the given number of words (no markers)."""
        return " ".join(["word"] * word_count)

    def test_short_description_is_truncated(self):
        desc = self._long_desc(50)  # well under 300-word threshold
        assert _is_truncated(desc) is True

    def test_exactly_299_words_is_truncated(self):
        desc = self._long_desc(299)
        assert _is_truncated(desc) is True

    def test_exactly_300_words_not_truncated(self):
        desc = self._long_desc(300)
        assert _is_truncated(desc) is False

    def test_long_description_not_truncated(self):
        desc = self._long_desc(500)
        assert _is_truncated(desc) is False

    def test_ends_with_ellipsis_is_truncated(self):
        desc = self._long_desc(400) + " ..."
        assert _is_truncated(desc) is True

    def test_ends_with_unicode_ellipsis_is_truncated(self):
        desc = self._long_desc(400) + "…"
        assert _is_truncated(desc) is True

    def test_ends_with_show_more_is_truncated(self):
        desc = self._long_desc(400) + " show more"
        assert _is_truncated(desc) is True

    def test_ends_with_see_more_is_truncated(self):
        desc = self._long_desc(400) + " see more"
        assert _is_truncated(desc) is True

    def test_ends_with_read_more_is_truncated(self):
        desc = self._long_desc(400) + " read more"
        assert _is_truncated(desc) is True

    def test_empty_description_not_truncated(self):
        assert _is_truncated("") is False

    def test_case_insensitive_marker(self):
        desc = self._long_desc(400) + " Show More"
        assert _is_truncated(desc) is True

    def test_indeed_snippet_length_is_truncated(self):
        # Indeed search API commonly returns 100-200 word descriptions.
        # These should always be flagged as truncated under the 300-word threshold.
        desc = self._long_desc(150)
        assert _is_truncated(desc) is True


# ---------------------------------------------------------------------------
# _is_indeed_detail_url()
# ---------------------------------------------------------------------------

class TestIsIndeedDetailUrl:
    def test_standard_indeed_viewjob(self):
        assert _is_indeed_detail_url("https://www.indeed.com/viewjob?jk=abc123") is True

    def test_country_subdomain(self):
        assert _is_indeed_detail_url("https://us.indeed.com/viewjob?jk=xyz789") is True

    def test_indeed_viewjob_trailing_slash(self):
        assert _is_indeed_detail_url("https://indeed.com/viewjob/?jk=abc") is True

    def test_indeed_search_page_is_not_detail(self):
        assert _is_indeed_detail_url("https://indeed.com/jobs?q=engineer") is False

    def test_indeed_viewjob_no_jk_param(self):
        assert _is_indeed_detail_url("https://indeed.com/viewjob?other=abc") is False

    def test_non_indeed_url(self):
        assert _is_indeed_detail_url("https://linkedin.com/jobs/view/12345") is False

    def test_empty_string(self):
        assert _is_indeed_detail_url("") is False


# ---------------------------------------------------------------------------
# urllib3 block observer
# ---------------------------------------------------------------------------

def _emit_urllib3_warning(message: str) -> None:
    """Helper: log a fake warning to the urllib3.connectionpool logger."""
    logging.getLogger("urllib3.connectionpool").warning(message)


class TestUrllib3BlockObserver:
    """Verify the observer flags 403/429/999 status codes seen in urllib3 retry warnings."""

    def _attach(self) -> _Urllib3BlockObserver:
        observer = _Urllib3BlockObserver()
        logging.getLogger("urllib3.connectionpool").addHandler(observer)
        return observer

    def _detach(self, observer: _Urllib3BlockObserver) -> None:
        logging.getLogger("urllib3.connectionpool").removeHandler(observer)

    def test_observer_captures_403(self):
        observer = self._attach()
        try:
            _emit_urllib3_warning(
                "Retrying (Retry(total=2, connect=3, read=None, redirect=None, "
                "status=3)) after connection broken by 'OSError(\"Tunnel "
                "connection failed: 403 Forbidden\")'"
            )
        finally:
            self._detach(observer)
        assert observer.blocked_codes == ["403"]

    def test_observer_captures_429(self):
        observer = self._attach()
        try:
            _emit_urllib3_warning(
                "Retrying after server returned 429 Too Many Requests"
            )
        finally:
            self._detach(observer)
        assert observer.blocked_codes == ["429"]

    def test_observer_captures_999(self):
        observer = self._attach()
        try:
            _emit_urllib3_warning(
                "Retrying after status=999 (LinkedIn auth wall)"
            )
        finally:
            self._detach(observer)
        assert observer.blocked_codes == ["999"]

    def test_observer_ignores_unrelated_warnings(self):
        observer = self._attach()
        try:
            _emit_urllib3_warning("Some unrelated connection warning with no code")
        finally:
            self._detach(observer)
        assert observer.blocked_codes == []

    def test_observer_ignores_other_status_codes(self):
        observer = self._attach()
        try:
            _emit_urllib3_warning("Retrying after 400 Bad Request")
            _emit_urllib3_warning("Retrying after 500 Internal Server Error")
            _emit_urllib3_warning("Retrying after 503 Service Unavailable")
        finally:
            self._detach(observer)
        assert observer.blocked_codes == []

    def test_observer_captures_multiple_codes_in_one_run(self):
        observer = self._attach()
        try:
            _emit_urllib3_warning("Retrying after Tunnel connection failed: 403")
            _emit_urllib3_warning("Retrying after 429 Too Many Requests")
            _emit_urllib3_warning("Retrying after status=999")
        finally:
            self._detach(observer)
        assert observer.blocked_codes == ["403", "429", "999"]


class TestScrapeWithBlockDetection:
    """Verify the wrapper installs/cleans up the handler around scrape_jobs."""

    def test_returns_df_and_empty_codes_on_clean_run(self):
        df_in = pd.DataFrame([{"title": "ML Engineer"}])
        scrape = MagicMock(return_value=df_in)
        df_out, codes = _scrape_with_block_detection(scrape, search_term="x")
        assert df_out is df_in
        assert codes == []
        scrape.assert_called_once_with(search_term="x")

    def test_handler_removed_even_when_scrape_jobs_raises(self):
        urllib3_logger = logging.getLogger("urllib3.connectionpool")
        handlers_before = list(urllib3_logger.handlers)

        def boom(**_kwargs):
            raise RuntimeError("simulated network failure")

        with pytest.raises(RuntimeError):
            _scrape_with_block_detection(boom)

        # Observer must be cleaned up so subsequent calls do not double-count.
        assert urllib3_logger.handlers == handlers_before

    def test_captures_block_codes_emitted_during_call(self):
        def emits_403(**_kwargs):
            _emit_urllib3_warning("Tunnel connection failed: 403 Forbidden")
            return pd.DataFrame()

        df, codes = _scrape_with_block_detection(emits_403)
        assert df.empty
        assert codes == ["403"]


# ---------------------------------------------------------------------------
# Retry loop — _scrape_jobs_with_retries
# ---------------------------------------------------------------------------

def _make_proxy_mgr(*, enabled: bool = True, proxy_url: str = "http://u:p@h:1") -> MagicMock:
    """Build a ProxyManager-shaped mock for retry-loop tests."""
    proxy_mgr = MagicMock()
    proxy_mgr.enabled = enabled
    proxy_mgr.proxies_list.return_value = [proxy_url] if enabled else []
    return proxy_mgr


class TestScrapeRetry:
    """End-to-end behavior of the retry-with-rotation loop."""

    def test_legit_zero_results_no_rotation(self):
        """Empty DataFrame with NO urllib3 block signal must not burn proxy sessions."""
        scrape = MagicMock(return_value=pd.DataFrame())
        proxy_mgr = _make_proxy_mgr(enabled=True)

        df, last_failure, attempts = _scrape_jobs_with_retries(
            scrape, {}, proxy_mgr, "Underwater Basket Weaver", "friendly",
        )

        assert df.empty
        assert last_failure == "empty_no_block"
        assert attempts == 1
        scrape.assert_called_once()
        proxy_mgr.force_rotate.assert_not_called()

    def test_block_then_success_rotates_once(self):
        """First attempt: empty + 403 warning. Second: 1 row. Expect one rotation."""
        good_df = pd.DataFrame([{"title": "ML Engineer"}])

        def fake_scrape(**_kwargs):
            if not fake_scrape.calls:
                fake_scrape.calls.append("first")
                _emit_urllib3_warning("Tunnel connection failed: 403 Forbidden")
                return pd.DataFrame()
            return good_df

        fake_scrape.calls = []
        proxy_mgr = _make_proxy_mgr(enabled=True)

        df, last_failure, attempts = _scrape_jobs_with_retries(
            fake_scrape, {}, proxy_mgr, "ML engineer", "ok",
        )

        assert df is good_df
        assert last_failure is None
        assert attempts == 2
        proxy_mgr.force_rotate.assert_called_once()
        rotate_kwargs = proxy_mgr.force_rotate.call_args.kwargs
        assert rotate_kwargs["reason"] == "jobspy_block_403_attempt_1"

    def test_sustained_block_exhausts_retries(self):
        """All MAX_BLOCK_RETRIES+1 attempts blocked → terminal failure, no raise."""
        def always_blocked(**_kwargs):
            _emit_urllib3_warning("Tunnel connection failed: 403 Forbidden")
            return pd.DataFrame()

        scrape = MagicMock(side_effect=always_blocked)
        proxy_mgr = _make_proxy_mgr(enabled=True)

        df, last_failure, attempts = _scrape_jobs_with_retries(
            scrape, {}, proxy_mgr, "ML engineer", "ok",
        )

        assert df.empty
        assert last_failure.startswith("block_")
        assert attempts == MAX_BLOCK_RETRIES + 1
        assert scrape.call_count == MAX_BLOCK_RETRIES + 1
        # Rotation happens between attempts, so MAX_BLOCK_RETRIES rotations total.
        assert proxy_mgr.force_rotate.call_count == MAX_BLOCK_RETRIES

    def test_proxy_disabled_skips_retry(self):
        """Without a proxy, an empty DataFrame should not trigger any retries."""
        # Even with a 403 signal, no proxy means rotation is impossible.
        def emits_block(**_kwargs):
            _emit_urllib3_warning("Tunnel connection failed: 403 Forbidden")
            return pd.DataFrame()

        scrape = MagicMock(side_effect=emits_block)
        proxy_mgr = _make_proxy_mgr(enabled=False)

        df, last_failure, attempts = _scrape_jobs_with_retries(
            scrape, {}, proxy_mgr, "ML engineer", "ok",
        )

        assert df.empty
        assert attempts == 1
        scrape.assert_called_once()
        proxy_mgr.force_rotate.assert_not_called()

    def test_exception_then_success_retries_inline(self):
        """Exception on attempt 1, success on attempt 2 — query NOT skipped."""
        good_df = pd.DataFrame([{"title": "ML Engineer"}])
        scrape = MagicMock(side_effect=[RuntimeError("connection reset"), good_df])
        proxy_mgr = _make_proxy_mgr(enabled=True)

        df, last_failure, attempts = _scrape_jobs_with_retries(
            scrape, {}, proxy_mgr, "ML engineer", "ok",
        )

        assert df is good_df
        assert last_failure is None
        assert attempts == 2
        assert scrape.call_count == 2
        proxy_mgr.force_rotate.assert_called_once()
        assert proxy_mgr.force_rotate.call_args.kwargs["reason"] == (
            "jobspy_exception_attempt_1"
        )

    def test_proxy_list_re_minted_each_attempt(self):
        """proxies_list() must be called once per attempt so each retry gets a fresh URL."""
        def always_blocked(**_kwargs):
            _emit_urllib3_warning("Tunnel connection failed: 403 Forbidden")
            return pd.DataFrame()

        scrape = MagicMock(side_effect=always_blocked)
        proxy_mgr = _make_proxy_mgr(enabled=True)

        _scrape_jobs_with_retries(scrape, {}, proxy_mgr, "ML engineer", "ok")

        # One proxies_list() call per attempt.
        assert proxy_mgr.proxies_list.call_count == MAX_BLOCK_RETRIES + 1
