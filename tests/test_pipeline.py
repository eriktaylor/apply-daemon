"""Routing tests for src/pipeline.py — fetch → classify → pool → archive.

Everything external is mocked (IMAP, LLM, HTTP); the Database is real
(tmp_path) so ledger writes and listing upserts are asserted against
actual SQLite state. Synthetic fixtures only.
"""

from __future__ import annotations

import email.message
import email.utils
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.db import Database
from src.email_config import EmailConfig, SenderGroup
from src.email_fetcher import FetchedEmail
from src.models import JobListing
from src.pipeline import _make_duplicate_check, run_pipeline

DIGEST_HTML = """
<html><body>
  <a href="https://www.linkedin.com/comm/jobs/view/4001?trk=x">
    Senior Machine Learning Engineer
  </a><span>Acme Robotics · Remote</span>
  <a href="https://www.linkedin.com/comm/jobs/view/4002?trk=y">
    Staff AI Platform Engineer
  </a><span>Beta Health · SF</span>
</body></html>
"""

NO_LINKS_HTML = (
    "<html><body><p>Weekly digest: many great jobs are waiting for you "
    "on our site, open the app to see them all.</p></body></html>"
)


def _rfc2822(days_ago: float) -> str:
    """RFC-2822 date ``days_ago`` days before now.

    Fixture dates must be clock-relative: ``_config`` sets
    ``lookback_days=14``, so a hardcoded date silently ages past the
    freshness window and flips these tests to ARCHIVED_STALE.
    """
    return email.utils.format_datetime(
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    )


FRESH_DATE = _rfc2822(1)      # inside the 14-day lookback
STALE_DATE = _rfc2822(45)     # well outside it


def _email(
    sender: str,
    subject: str,
    message_id: str,
    html: str | None = DIGEST_HTML,
    date: str = FRESH_DATE,
) -> email.message.EmailMessage:
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = date
    if html is not None:
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content("plain text only")
    return msg


def _fetched(msg, uid: bytes) -> FetchedEmail:
    return FetchedEmail(
        message=msg, uid=uid, gmail_message_id=msg["Message-ID"]
    )


def _config(top_n: int | None) -> EmailConfig:
    return EmailConfig(
        senders=[
            SenderGroup(
                source="linkedin",
                addresses=["jobs-noreply@linkedin.com"],
                tier="friendly",
            )
        ],
        subject_hints=["job alert"],
        top_n=top_n,
        lookback_days=14,
    )


def _listing(**kwargs) -> JobListing:
    defaults = {
        "source": "linkedin",
        "email_classification": "JOB_DIGEST",
        "title": "Senior Machine Learning Engineer",
        "company": "Acme Robotics",
        "verdict": "YES",
        "confidence": 90,
        "reason": "match",
        "model_used": "test",
    }
    defaults.update(kwargs)
    return JobListing(**defaults)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Patch every external boundary; yield the knobs tests turn."""
    monkeypatch.setenv("EXPIRED_PROBE_ENABLED", "false")  # no HTTP in select
    monkeypatch.setenv("AUTOPILOT_ENABLED", "false")

    db = Database(tmp_path / "pipeline.db")

    session = MagicMock()
    session.triage_email.return_value = [_listing()]
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False

    profile = {"name": "Test", "llm_context": "ctx", "settings": {}}

    patches = {
        "load_profile": patch("src.pipeline.load_profile", return_value=profile),
        "load_email_config": patch(
            "src.pipeline.load_email_config", return_value=_config(top_n=2)
        ),
        "fetch_inbox": patch("src.pipeline.fetch_inbox", return_value=[]),
        "archive_emails": patch("src.pipeline.archive_emails", return_value=0),
        "sweep_stale_alerts": patch("src.pipeline.sweep_stale_alerts", return_value=0),
        "TriageSession": patch("src.pipeline.TriageSession", return_value=session_cm),
        "Database": patch("src.pipeline.Database", return_value=db),
        "_scrape_url": patch("src.pipeline._scrape_url", return_value="Full JD text " * 50),
        "get_confidence_threshold": patch(
            "src.pipeline.get_confidence_threshold", return_value=0.75
        ),
    }
    mocks = {name: p.start() for name, p in patches.items()}
    mocks["session"] = session
    mocks["db"] = db
    # Database.__exit__ closes the connection; keep it open for assertions.
    db.close = lambda: None
    yield mocks
    for p in patches.values():
        p.stop()


def _classification_of(db: Database, message_id: str) -> str | None:
    row = db.conn.execute(
        "SELECT classification FROM processed_emails WHERE gmail_message_id = ?",
        (message_id,),
    ).fetchone()
    return row["classification"] if row else None


class TestSkipRouting:
    def test_recruiter_mail_ledgered_and_never_archived(self, env):
        msg = _email(
            "recruiter@techstartup.com", "Senior Engineer role at TechStartup",
            "<recruiter-1@x>", html=None,
        )
        env["fetch_inbox"].return_value = [_fetched(msg, b"1")]
        run_pipeline()
        assert _classification_of(env["db"], "<recruiter-1@x>") == "SKIP_RECRUITER"
        env["archive_emails"].assert_not_called()
        env["session"].triage_email.assert_not_called()

    def test_junk_mail_ledgered_untouched(self, env):
        msg = _email(
            "noreply@google.com", "Security alert", "<junk-1@x>", html=None
        )
        env["fetch_inbox"].return_value = [_fetched(msg, b"2")]
        run_pipeline()
        assert _classification_of(env["db"], "<junk-1@x>") == "SKIP_JUNK"
        env["archive_emails"].assert_not_called()

    def test_ledgered_email_short_circuits(self, env):
        env["db"].record_processed_email(
            "h", "", gmail_message_id="<seen-1@x>", classification="JOB_DIGEST"
        )
        msg = _email("jobs-noreply@linkedin.com", "job alert", "<seen-1@x>")
        env["fetch_inbox"].return_value = [_fetched(msg, b"3")]
        run_pipeline()
        env["session"].triage_email.assert_not_called()


class TestStaleAlerts:
    def test_stale_sweep_runs_every_pipeline_run(self, env):
        run_pipeline()
        env["sweep_stale_alerts"].assert_called_once()
        # The ledger callback records unseen Message-IDs as ARCHIVED_STALE.
        ledger = env["sweep_stale_alerts"].call_args.kwargs["ledger"]
        ledger("<swept-1@x>")
        assert _classification_of(env["db"], "<swept-1@x>") == "ARCHIVED_STALE"
        ledger("<swept-1@x>")  # already seen → no duplicate row
        rows = env["db"].conn.execute(
            "SELECT COUNT(*) c FROM processed_emails WHERE gmail_message_id = ?",
            ("<swept-1@x>",),
        ).fetchone()
        assert rows["c"] == 1

    def test_stale_alert_archived_without_triage(self, env):
        msg = _email(
            "jobs-noreply@linkedin.com", "your job alert", "<stale-1@x>",
            date=STALE_DATE,  # outside the 14-day lookback
        )
        env["fetch_inbox"].return_value = [_fetched(msg, b"4")]
        run_pipeline()
        assert _classification_of(env["db"], "<stale-1@x>") == "ARCHIVED_STALE"
        env["session"].triage_email.assert_not_called()
        env["archive_emails"].assert_called_once()
        assert b"4" in env["archive_emails"].call_args.args[0]


class TestAggregationPath:
    def test_alert_pooled_selected_stored_archived(self, env):
        msg = _email("jobs-noreply@linkedin.com", "your job alert", "<alert-1@x>")
        env["fetch_inbox"].return_value = [_fetched(msg, b"5")]
        run_pipeline()

        # Two candidates parsed, top_n=2 → both triaged from scraped text.
        assert env["session"].triage_email.call_count == 2
        listings = env["db"].get_recent_listings(hours=1)
        assert len(listings) == 1  # same listing returned → upsert dedupes

        # Email ledgered with its classification and archived.
        assert _classification_of(env["db"], "<alert-1@x>") == "JOB_DIGEST"
        assert b"5" in env["archive_emails"].call_args.args[0]

    def test_top_n_caps_triage_calls(self, env):
        env["load_email_config"].return_value = _config(top_n=1)
        msg = _email("jobs-noreply@linkedin.com", "your job alert", "<alert-2@x>")
        env["fetch_inbox"].return_value = [_fetched(msg, b"6")]
        run_pipeline()
        assert env["session"].triage_email.call_count == 1

    def test_unparseable_alert_falls_back_to_email_level(self, env):
        msg = _email(
            "jobs-noreply@linkedin.com", "your job alert", "<fallback-1@x>",
            html=NO_LINKS_HTML,
        )
        env["fetch_inbox"].return_value = [_fetched(msg, b"7")]
        run_pipeline()
        # Email-level triage ran once on the email text (not per candidate).
        assert env["session"].triage_email.call_count == 1
        assert _classification_of(env["db"], "<fallback-1@x>") == "PARSE_FALLBACK"
        assert b"7" in env["archive_emails"].call_args.args[0]

    def test_triage_failure_not_ledgered_for_retry(self, env):
        env["session"].triage_email.side_effect = RuntimeError("LLM down")
        msg = _email(
            "jobs-noreply@linkedin.com", "your job alert", "<retry-1@x>",
            html=NO_LINKS_HTML,  # email-level path exercises the except branch
        )
        env["fetch_inbox"].return_value = [_fetched(msg, b"8")]
        run_pipeline()
        # Transient failure: no ledger entry, so next run retries it.
        assert _classification_of(env["db"], "<retry-1@x>") is None
        env["archive_emails"].assert_not_called()


class TestEmailLevelPath:
    def test_top_n_blank_uses_email_level_triage(self, env):
        env["load_email_config"].return_value = _config(top_n=None)
        msg = _email("jobs-noreply@linkedin.com", "your job alert", "<plain-1@x>")
        env["fetch_inbox"].return_value = [_fetched(msg, b"9")]
        run_pipeline()
        # One call for the whole email — candidates never pooled.
        assert env["session"].triage_email.call_count == 1
        call = env["session"].triage_email.call_args
        assert call.args[2] == "JOB_DIGEST"  # classification, not candidate
        assert _classification_of(env["db"], "<plain-1@x>") == "JOB_DIGEST"
        assert b"9" in env["archive_emails"].call_args.args[0]

    def test_duplicate_email_archived(self, env):
        env["load_email_config"].return_value = _config(top_n=None)
        msg1 = _email("jobs-noreply@linkedin.com", "your job alert", "<dup-1@x>")
        env["fetch_inbox"].return_value = [_fetched(msg1, b"10")]
        run_pipeline()
        # Same body, different message-id → email-level text dedup fires.
        msg2 = _email("jobs-noreply@linkedin.com", "your job alert", "<dup-2@x>")
        env["fetch_inbox"].return_value = [_fetched(msg2, b"11")]
        run_pipeline()
        assert _classification_of(env["db"], "<dup-2@x>") == "DUPLICATE"
        assert b"11" in env["archive_emails"].call_args.args[0]


class TestDedupAudit:
    """A-8: the pre-Stage-5 dedup skip must leave a row in logs/audit.log.

    Track B ingested nothing for two months and the log could not say why,
    because this skip was silent. The drop is what gets instrumented — not
    the check.
    """

    def _db_with(self, tmp_path, listing):
        db = Database(tmp_path / "dedup.db")
        db.insert_listing(listing)
        return db

    def test_duplicate_logs_exactly_one_dedup_row(self, tmp_path):
        existing = _listing(title="Senior ML Engineer", company="Acme Robotics")
        db = self._db_with(tmp_path, existing)
        check = _make_duplicate_check(db, source="linkedin", window_days=30)

        with patch("src.audit_log.log_drop") as log_drop:
            assert check("Senior ML Engineer", "Acme Robotics") is True

        assert log_drop.call_count == 1
        kwargs = log_drop.call_args.kwargs
        assert kwargs["gate"] == "dedup"
        assert kwargs["listing_id"] == ""  # nothing was inserted
        assert kwargs["source"] == "linkedin"
        assert kwargs["anchor_company"] == "Acme Robotics"
        assert existing.id[:8] in kwargs["reason"]
        db.close()

    def test_non_duplicate_logs_nothing(self, tmp_path):
        db = self._db_with(tmp_path, _listing(title="Senior ML Engineer",
                                              company="Acme Robotics"))
        check = _make_duplicate_check(db, source="linkedin", window_days=30)

        with patch("src.audit_log.log_drop") as log_drop:
            assert check("Warehouse Associate", "Other Corp") is False

        log_drop.assert_not_called()
        db.close()

    def test_row_carries_no_listing_content(self, tmp_path):
        """SECURITY.md: IDs and decisions, never titles or body text."""
        existing = _listing(
            title="Senior ML Engineer",
            company="Acme Robotics",
            job_summary="Confidential body text from the alert email",
        )
        db = self._db_with(tmp_path, existing)
        check = _make_duplicate_check(
            db, source="linkedin", window_days=30,
            url="https://www.linkedin.com/jobs/view/4001",
        )

        with patch("src.audit_log.log_drop") as log_drop:
            check("Senior ML Engineer", "Acme Robotics")

        emitted = " ".join(str(v) for v in log_drop.call_args.kwargs.values())
        assert "Senior ML Engineer" not in emitted
        assert "Confidential" not in emitted
        db.close()
