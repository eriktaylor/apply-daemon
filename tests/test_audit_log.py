"""Unit tests for src/audit_log.py — pipe-delimited mismatch drop log."""

from __future__ import annotations

import inspect
import logging

import pytest

import src.audit_log as audit_log
from src.audit_log import _LOGGER_NAME, _host, _safe, log_dedup_drop, log_drop


def _reset_channel():
    """Detach any FileHandler so the next _get_logger() call reattaches.

    Mirrors tests/test_model_usage.py::_reset_channel — the same shared
    src/file_logger.py mechanics, so the same reset shape applies.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def _enable(monkeypatch, path):
    monkeypatch.setenv("AUDIT_LOG_ENABLED", "true")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(path))
    _reset_channel()


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


class TestFileSink:
    """The durable sink (V-38 / A-8 prerequisite): logs/audit.log by default,
    a FileHandler attached exactly once via src/file_logger.py, path
    overridable by AUDIT_LOG_PATH, gated by AUDIT_LOG_ENABLED (default true
    in production; the test suite defaults it false — tests/conftest.py —
    same shape as MODEL_USAGE_LOG_ENABLED)."""

    def test_log_drop_writes_one_line_to_file(self, tmp_path, monkeypatch):
        log_path = tmp_path / "logs" / "audit.log"
        _enable(monkeypatch, log_path)

        log_drop(
            listing_id="abc", source="linkedin", gate="llm",
            anchor_company="Handshake", reason="anchor not in body",
        )

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("audit.mismatch_drops | ")
        _reset_channel()

    def test_default_path_is_logs_audit_log(self):
        assert audit_log._DEFAULT_LOG_PATH == "logs/audit.log"

    def test_repeated_calls_do_not_attach_duplicate_handlers(self, tmp_path, monkeypatch):
        log_path = tmp_path / "audit.log"
        _enable(monkeypatch, log_path)

        log_drop(listing_id="a", source="s", gate="g", anchor_company="c1")
        log_drop(listing_id="b", source="s", gate="g", anchor_company="c2")

        logger = logging.getLogger(_LOGGER_NAME)
        assert len(logger.handlers) == 1

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        # Two calls, two lines — not four, which is what a doubled handler
        # would produce.
        assert len(lines) == 2
        _reset_channel()

    def test_env_override_is_honoured(self, tmp_path, monkeypatch):
        custom_path = tmp_path / "somewhere_else" / "drops.log"
        _enable(monkeypatch, custom_path)

        log_drop(listing_id="x", source="s", gate="g", anchor_company="c")

        assert custom_path.exists()
        assert "audit.mismatch_drops" in custom_path.read_text(encoding="utf-8")
        _reset_channel()

    def test_disabled_by_default_writes_no_file(self, tmp_path, monkeypatch):
        # AUDIT_LOG_ENABLED is unset here (conftest.py's suite-wide default
        # of "false" applies), mirroring MODEL_USAGE_LOG_ENABLED's test-time
        # default so the suite never writes a stray logs/ directory.
        log_path = tmp_path / "audit.log"
        monkeypatch.setenv("AUDIT_LOG_PATH", str(log_path))
        _reset_channel()

        log_drop(listing_id="x", source="s", gate="g", anchor_company="c")

        assert not log_path.exists()
        _reset_channel()

    def test_propagation_stays_on_so_cron_redirection_still_works(
        self, tmp_path, monkeypatch, caplog
    ):
        # Unlike model_usage's channel (propagate=False, a pure data sink),
        # the audit file must be additive: a cron setup that redirects
        # stderr must still see these lines even after the file sink lands.
        log_path = tmp_path / "audit.log"
        _enable(monkeypatch, log_path)

        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            log_drop(listing_id="x", source="s", gate="g", anchor_company="c")

        assert len(caplog.records) == 1
        assert log_path.exists()
        _reset_channel()


class TestDedupDrop:
    """gate=dedup — the pre-Stage-5 skip both ingestion tracks make (A-8)."""

    def test_row_names_the_matched_id_and_nothing_else(self, tmp_path, monkeypatch):
        log_path = tmp_path / "audit.log"
        _enable(monkeypatch, log_path)

        log_dedup_drop(
            source="linkedin",
            anchor_company="Acme Corp",
            matched_id="9ad4143b-3617-4c1e-9a2b-000000000000",
            url="https://www.indeed.com/viewjob?jk=1",
        )

        line = log_path.read_text(encoding="utf-8").strip()
        fields = [f.strip() for f in line.split("|")]
        # audit.mismatch_drops | ts | listing_id | source | gate | ...
        assert fields[2] == ""  # never inserted, so there is no listing id
        assert fields[3] == "linkedin"
        assert fields[4] == "dedup"
        assert fields[5] == "Acme Corp"
        assert fields[7] == "indeed.com"
        assert fields[8] == "dedup: matches 9ad4143b"
        _reset_channel()

    def test_short_or_empty_matched_id_does_not_raise(self, caplog):
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            log_dedup_drop(source="s", anchor_company="c", matched_id="")
        assert "dedup: matches" in caplog.text

    def test_gate_vocabulary_matches_the_doc(self):
        """docs/AUDIT.md owns the vocabulary; log_drop's docstring must agree.

        Two copies of a list is the drift shape CLAUDE.md names — this is the
        cheap guard that they stay one vocabulary.
        """
        import re
        from pathlib import Path

        doc = Path("docs/AUDIT.md").read_text(encoding="utf-8")
        table = doc.split("## Gate values", 1)[1].split("##", 1)[0]
        documented = set(re.findall(r"^\| `([a-z0-9]+)` \|", table, re.MULTILINE))

        listed = log_drop.__doc__.split("One of:", 1)[1].split(".", 1)[0]
        declared = {g.strip() for g in listed.replace("\n", " ").split(",") if g.strip()}

        assert declared == documented, (
            f"gate vocabulary drift: docstring={declared}, docs/AUDIT.md={documented}"
        )


class TestNoRawContent:
    """SECURITY.md: audit rows carry IDs, host, gate, reason — never raw
    listing/email content."""

    def test_signature_has_no_raw_content_fields(self):
        banned = {
            "body", "description", "raw_email_text", "job_summary", "html",
            "text", "email_body",
        }
        params = set(inspect.signature(log_drop).parameters)
        assert not (params & banned), (
            f"log_drop's signature admits a raw-content-shaped field: {params & banned}"
        )

    def test_log_drop_rejects_unlisted_fields(self):
        """Structural guarantee: log_drop takes no **kwargs, so a raw-content
        field (e.g. a full email/listing body) can't be smuggled through an
        unlisted keyword — the parameter list is closed to exactly the
        schema's columns."""
        with pytest.raises(TypeError):
            log_drop(
                listing_id="x", source="s", gate="g", anchor_company="c",
                raw_email_body="the full email text would go here",
            )

    def test_emitted_line_carries_ids_host_and_reason(self, tmp_path, monkeypatch):
        log_path = tmp_path / "audit.log"
        _enable(monkeypatch, log_path)

        log_drop(
            listing_id="abc123", source="linkedin", gate="llm",
            anchor_company="Handshake", url="https://www.thehomebase.ai/x",
            reason="anchor not in body",
        )

        line = log_path.read_text(encoding="utf-8").strip()
        assert "abc123" in line
        assert "linkedin" in line
        assert "thehomebase.ai" in line
        assert "anchor not in body" in line
        _reset_channel()
