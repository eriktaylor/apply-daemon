"""Tests for src/email_fetcher.py — touch-nothing fetch + archive move."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.email_config import EmailConfig, SenderGroup
from src.email_fetcher import (
    archive_emails,
    build_gmail_query,
    fetch_inbox,
    sweep_stale_alerts,
)

RAW_EMAIL = (
    b"Message-ID: <alert-1@mail.gmail.com>\r\n"
    b"From: jobs-noreply@linkedin.com\r\n"
    b"Subject: Your job alert\r\n\r\nBody"
)
RAW_HEADER = b"Message-ID: <alert-1@mail.gmail.com>\r\n\r\n"


def _config() -> EmailConfig:
    return EmailConfig(
        senders=[
            SenderGroup(
                source="linkedin",
                addresses=["jobs-noreply@linkedin.com"],
                tier="friendly",
            )
        ],
        subject_hints=["job alert"],
        lookback_days=14,
    )


def _mock_conn(search_uids: bytes = b"1") -> MagicMock:
    conn = MagicMock()

    def uid_dispatch(command, *args):
        if command == "SEARCH":
            return ("OK", [search_uids])
        if command == "FETCH":
            spec = args[1]
            if "HEADER.FIELDS" in spec:
                return ("OK", [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {52}", RAW_HEADER)])
            return ("OK", [(b"1 (BODY[] {96}", RAW_EMAIL)])
        if command in ("STORE", "MOVE"):
            return ("OK", [b""])
        raise AssertionError(f"unexpected uid command: {command}")

    conn.uid.side_effect = uid_dispatch
    return conn


@pytest.fixture
def imap(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
    conn = _mock_conn()
    with patch("src.email_fetcher.imaplib.IMAP4_SSL", return_value=conn):
        yield conn


class TestBuildGmailQuery:
    def test_from_clauses_and_lookback(self):
        query = build_gmail_query(_config())
        assert query.startswith("newer_than:14d (")
        assert "from:jobs-noreply@linkedin.com" in query


class TestFetchInbox:
    def test_returns_fetched_email_with_ids(self, imap):
        result = fetch_inbox(config=_config())
        assert len(result) == 1
        assert result[0].gmail_message_id == "<alert-1@mail.gmail.com>"
        assert result[0].uid == b"1"
        assert result[0].message["Subject"] == "Your job alert"

    def test_never_mutates_flags(self, imap):
        """The touch-nothing contract: no STORE, and all FETCHes use PEEK."""
        fetch_inbox(config=_config())
        for call in imap.uid.call_args_list:
            command = call.args[0]
            assert command != "STORE"
            if command == "FETCH":
                assert "BODY.PEEK" in call.args[2]

    def test_uses_x_gm_raw_query(self, imap, monkeypatch):
        monkeypatch.setenv("GMAIL_USE_X_GM_RAW", "true")
        fetch_inbox(config=_config())
        search_call = imap.uid.call_args_list[0]
        assert search_call.args[1] == "X-GM-RAW"
        assert "newer_than:14d" in search_call.args[2]

    def test_falls_back_to_unseen_when_disabled(self, imap, monkeypatch):
        monkeypatch.setenv("GMAIL_USE_X_GM_RAW", "false")
        fetch_inbox(config=_config())
        search_call = imap.uid.call_args_list[0]
        assert search_call.args[1] == "UNSEEN"

    def test_unseen_when_no_config(self, imap):
        fetch_inbox(config=None)
        search_call = imap.uid.call_args_list[0]
        assert search_call.args[1] == "UNSEEN"

    def test_already_seen_skips_body_download(self, imap):
        result = fetch_inbox(config=_config(), already_seen=lambda mid: True)
        assert result == []
        # Header peek happened, but the full-body fetch never did.
        fetch_specs = [
            c.args[2] for c in imap.uid.call_args_list if c.args[0] == "FETCH"
        ]
        assert all("HEADER.FIELDS" in spec for spec in fetch_specs)

    def test_not_seen_proceeds_to_full_fetch(self, imap):
        result = fetch_inbox(config=_config(), already_seen=lambda mid: False)
        assert len(result) == 1

    def test_empty_search(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
        conn = MagicMock()
        conn.uid.return_value = ("OK", [b""])
        with patch("src.email_fetcher.imaplib.IMAP4_SSL", return_value=conn):
            assert fetch_inbox(config=_config()) == []


class TestArchiveEmails:
    def test_marks_seen_then_moves(self, imap):
        moved = archive_emails([b"1"], "apply-daemon/archive")
        assert moved == 1
        commands = [c.args[0] for c in imap.uid.call_args_list]
        assert commands.index("STORE") < commands.index("MOVE")
        store_call = next(c for c in imap.uid.call_args_list if c.args[0] == "STORE")
        assert store_call.args[2] == "+FLAGS"
        assert "\\Seen" in store_call.args[3]
        imap.create.assert_called_once_with("apply-daemon/archive")

    def test_never_deletes(self, imap):
        """Invariant 2: moves only — no \\Deleted flag, no EXPUNGE."""
        archive_emails([b"1"], "apply-daemon/archive")
        for call in imap.uid.call_args_list:
            assert "\\Deleted" not in str(call)
        imap.expunge.assert_not_called()

    def test_disabled_by_env(self, imap, monkeypatch):
        monkeypatch.setenv("EMAIL_ARCHIVE_ENABLED", "false")
        assert archive_emails([b"1"], "apply-daemon/archive") == 0
        imap.uid.assert_not_called()

    def test_empty_uid_list_no_connection(self):
        # No creds set — would raise if it tried to connect.
        assert archive_emails([], "apply-daemon/archive") == 0

    def test_per_email_failure_is_non_fatal(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
        conn = MagicMock()

        def uid_dispatch(command, *args):
            if command == "MOVE" and args[0] == b"1":
                raise RuntimeError("boom")
            return ("OK", [b""])

        conn.uid.side_effect = uid_dispatch
        with patch("src.email_fetcher.imaplib.IMAP4_SSL", return_value=conn):
            moved = archive_emails([b"1", b"2"], "apply-daemon/archive")
        assert moved == 1


class TestSweepStaleAlerts:
    def test_searches_older_than_and_moves(self, imap):
        seen = []
        moved = sweep_stale_alerts(_config(), ledger=seen.append)
        assert moved == 1
        search_call = imap.uid.call_args_list[0]
        assert search_call.args[1] == "X-GM-RAW"
        assert "older_than:14d" in search_call.args[2]
        assert "from:jobs-noreply@linkedin.com" in search_call.args[2]
        commands = [c.args[0] for c in imap.uid.call_args_list]
        assert commands.index("STORE") < commands.index("MOVE")
        imap.create.assert_called_once_with("apply-daemon/archive")
        # Message-ID from the header peek reaches the ledger callback.
        assert seen == ["<alert-1@mail.gmail.com>"]

    def test_never_deletes(self, imap):
        sweep_stale_alerts(_config())
        for call in imap.uid.call_args_list:
            assert "\\Deleted" not in str(call)
        imap.expunge.assert_not_called()

    def test_disabled_when_archive_off(self, imap, monkeypatch):
        monkeypatch.setenv("EMAIL_ARCHIVE_ENABLED", "false")
        assert sweep_stale_alerts(_config()) == 0
        imap.uid.assert_not_called()

    def test_disabled_without_x_gm_raw(self, imap, monkeypatch):
        """No age filter without Gmail search — sweep must not fall back to
        a query that could match everything."""
        monkeypatch.setenv("GMAIL_USE_X_GM_RAW", "false")
        assert sweep_stale_alerts(_config()) == 0
        imap.uid.assert_not_called()

    def test_per_email_failure_is_non_fatal(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "test@example.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret")
        conn = MagicMock()

        def uid_dispatch(command, *args):
            if command == "SEARCH":
                return ("OK", [b"1 2"])
            if command == "FETCH":
                return ("OK", [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {52}", RAW_HEADER)])
            if command == "MOVE" and args[0] == b"1":
                raise RuntimeError("boom")
            return ("OK", [b""])

        conn.uid.side_effect = uid_dispatch
        with patch("src.email_fetcher.imaplib.IMAP4_SSL", return_value=conn):
            assert sweep_stale_alerts(_config()) == 1
