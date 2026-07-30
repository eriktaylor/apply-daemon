"""Unit tests for src/model_usage.py — O-1 usage telemetry channel."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import src.model_usage as mu
from src.model_usage import _LOGGER_NAME, log_model_usage, log_response_usage


def _reset_channel():
    """Detach any FileHandler + clear the one-time configured flag."""
    logger = logging.getLogger(_LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    mu._configured = False


def _enable(monkeypatch, path):
    monkeypatch.setenv("MODEL_USAGE_LOG_ENABLED", "true")
    monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(path))
    _reset_channel()


def test_writes_pipe_delimited_record(tmp_path, monkeypatch):
    log_path = tmp_path / "logs" / "model_usage.log"
    _enable(monkeypatch, log_path)

    log_model_usage("openai/gpt-5.4-nano", "stage1", 1234)

    contents = log_path.read_text(encoding="utf-8").strip()
    fields = contents.split("|")
    # timestamp|stage|model|total|prompt|completion
    assert len(fields) == 6
    assert fields[1] == "stage1"
    assert fields[2] == "openai/gpt-5.4-nano"
    assert fields[3] == "1234"
    _reset_channel()


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    log_path = tmp_path / "logs" / "model_usage.log"
    monkeypatch.setenv("MODEL_USAGE_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(log_path))
    _reset_channel()

    log_model_usage("some/model", "stage5", 10)

    assert not log_path.exists()
    _reset_channel()


def test_never_raises_on_bad_token(tmp_path, monkeypatch):
    log_path = tmp_path / "logs" / "model_usage.log"
    _enable(monkeypatch, log_path)

    # A non-numeric token (e.g. a MagicMock leaking from a test) must not blow up.
    log_model_usage("m", "stage1", object())  # type: ignore[arg-type]
    _reset_channel()


def test_appends_multiple_records(tmp_path, monkeypatch):
    log_path = tmp_path / "model_usage.log"
    _enable(monkeypatch, log_path)

    log_model_usage("m1", "stage1", 1)
    log_model_usage("m2", "stage5", 2)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].split("|")[2] == "m1"
    assert lines[1].split("|")[2] == "m2"
    _reset_channel()


class TestResponseUsageHelper:
    """log_response_usage — defensive extraction shared by every call site."""

    def _resp(self, total):
        return SimpleNamespace(usage=SimpleNamespace(total_tokens=total))

    def test_logs_and_returns_tokens(self, tmp_path, monkeypatch):
        log_path = tmp_path / "usage.log"
        _enable(monkeypatch, log_path)
        assert log_response_usage(self._resp(1234), "m", "s") == 1234
        assert "|s|m|1234" in log_path.read_text(encoding="utf-8")
        _reset_channel()

    def test_missing_usage_logs_zero(self, tmp_path, monkeypatch):
        log_path = tmp_path / "usage.log"
        _enable(monkeypatch, log_path)
        assert log_response_usage(SimpleNamespace(usage=None), "m", "s") == 0
        assert "|s|m|0" in log_path.read_text(encoding="utf-8")
        _reset_channel()

    def test_absent_attribute_does_not_raise(self, tmp_path, monkeypatch):
        _enable(monkeypatch, tmp_path / "usage.log")
        assert log_response_usage(object(), "m", "s") == 0
        _reset_channel()

    def test_garbage_tokens_do_not_raise(self, tmp_path, monkeypatch):
        _enable(monkeypatch, tmp_path / "usage.log")
        resp = SimpleNamespace(usage=SimpleNamespace(total_tokens="not-a-number"))
        assert log_response_usage(resp, "m", "s") == 0
        _reset_channel()


class TestMeteringCoverage:
    """C-6 guard: every OpenRouter call site must log its usage.

    Source-level rather than per-site mocks — a NEW call site added later is
    exactly the regression this must catch, and a per-site test cannot see
    code that doesn't exist yet. Invariant 7: a spending path missing from
    the log makes the budget unenforceable.
    """

    def test_every_completion_call_site_logs_usage(self):
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src"
        offenders = []
        for path in sorted(src.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            n_calls = len(re.findall(r"chat\.completions\.create", text))
            if not n_calls:
                continue
            n_logs = len(re.findall(r"log_(?:response|model)_usage\(", text))
            if n_logs < n_calls:
                offenders.append(f"{path.name}: {n_calls} call(s), {n_logs} log(s)")
        assert not offenders, (
            "OpenRouter call sites without usage logging (invariant 7): "
            + "; ".join(offenders)
        )
