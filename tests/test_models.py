"""Tests for src/models.py — the stored-listing field contract.

``job_description_text`` decides which stored field every downstream model
consumer treats as "the job". Getting it wrong is invisible: the prompt still
renders, the model still answers, and the answer is about a summary rather
than a posting.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.models import JOB_DESCRIPTION_CHARS, job_description_text


def _row(**overrides) -> dict:
    row = {
        "raw_email_text": "Full posting. Requires Rust, CUDA, and 5 years of "
                          "distributed systems experience.",
        "job_summary": "A two-sentence TL;DR of the company and the role.",
        "reason": "Strong match on agentic AI experience.",
    }
    row.update(overrides)
    return row


class TestJobDescriptionText:
    def test_prefers_the_full_description(self):
        assert job_description_text(_row()).startswith("Full posting.")

    def test_falls_back_to_summary_when_description_missing(self):
        assert job_description_text(_row(raw_email_text="")) == (
            "A two-sentence TL;DR of the company and the role."
        )

    def test_never_returns_the_models_own_reasoning(self):
        """`reason` is the Stage 5 verdict's justification. A re-scorer that
        reads it grades the incumbent, not the job — the contamination that
        made autopilot's post-research verdict unusable as a gold standard."""
        out = job_description_text(_row(raw_email_text="", job_summary=""))
        assert out == ""
        assert "agentic AI" not in out

    def test_whitespace_only_fields_are_empty(self):
        assert job_description_text(
            _row(raw_email_text="   \n  ", job_summary="")
        ) == ""

    def test_truncates_to_the_ingestion_width(self):
        out = job_description_text(_row(raw_email_text="x" * 9000))
        assert len(out) == JOB_DESCRIPTION_CHARS

    def test_limit_is_overridable(self):
        assert len(job_description_text(_row(raw_email_text="x" * 900), limit=50)) == 50

    def test_missing_keys_do_not_raise(self):
        assert job_description_text({}) == ""
        assert job_description_text({"title": "SWE"}) == ""

    def test_accepts_a_sqlite_row(self):
        """Callers pass both dicts (process_queue) and Rows (triage)."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (raw_email_text TEXT, job_summary TEXT)")
        conn.execute("INSERT INTO t VALUES ('the posting', 'the summary')")
        row = conn.execute("SELECT * FROM t").fetchone()
        assert job_description_text(row) == "the posting"

    def test_sqlite_row_missing_column_does_not_raise(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (title TEXT)")
        conn.execute("INSERT INTO t VALUES ('SWE')")
        row = conn.execute("SELECT * FROM t").fetchone()
        assert job_description_text(row) == ""

    @pytest.mark.parametrize("value", [None, 0])
    def test_null_values_fall_through(self, value):
        assert job_description_text(
            _row(raw_email_text=value)
        ) == "A two-sentence TL;DR of the company and the role."

    def test_accepts_a_joblisting_dataclass(self):
        """Attribute-style access — a dataclass would otherwise silently
        return '' from every field and no caller would notice."""
        from src.models import JobListing
        listing = JobListing(raw_email_text="the posting body")
        assert job_description_text(listing) == "the posting body"
