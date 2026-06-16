"""Unit tests for src/audit_log.py — pipe-delimited mismatch drop log."""

from __future__ import annotations

import logging

from src.audit_log import _host, _safe, log_drop


class TestHostExtraction:
    def test_strips_www_and_lowercases(self):
        assert _host("https://www.Indeed.com/viewjob") == "indeed.com"

    def test_empty_url(self):
        assert _host("") == ""

    def test_bad_url(self):
        # urlparse is permissive; should not raise
        host = _host("not-a-url")
        assert isinstance(host, str)


class TestSafeField:
    def test_strips_pipes(self):
        assert _safe("foo | bar") == "foo   bar"

    def test_strips_newlines(self):
        assert _safe("foo\nbar\rbaz") == "foo bar baz"

    def test_none_yields_empty(self):
        assert _safe(None) == ""


class TestLogDrop:
    def test_emits_pipe_delimited_line(self, caplog):
        with caplog.at_level(logging.INFO, logger="apply_daemon.audit.mismatch_drops"):
            log_drop(
                listing_id="abc",
                source="linkedin",
                gate="llm",
                anchor_company="Handshake",
                observed_company="OpenAI",
                url="https://www.thehomebase.ai/jobs/x",
                reason="anchor not in body",
            )
        assert len(caplog.records) == 1
        msg = caplog.records[0].message
        assert msg.startswith("audit.mismatch_drops | ")
        parts = msg.split(" | ")
        # marker + 8 schema columns
        assert len(parts) == 9
        # Spot-check key columns
        assert parts[2] == "abc"
        assert parts[3] == "linkedin"
        assert parts[4] == "llm"
        assert parts[5] == "Handshake"
        assert parts[6] == "OpenAI"
        assert parts[7] == "thehomebase.ai"
        assert "anchor not in body" in parts[8]

    def test_pipe_in_reason_is_collapsed(self, caplog):
        with caplog.at_level(logging.INFO, logger="apply_daemon.audit.mismatch_drops"):
            log_drop(
                listing_id="x", source="s", gate="g",
                anchor_company="a", reason="bad | reason",
            )
        msg = caplog.records[0].message
        # The schema's marker + 8 schema pipes = 9 segments; no extra pipes from the reason
        assert msg.count(" | ") == 8
