"""Unit tests for src/model_usage.py — O-1 usage telemetry channel."""

from __future__ import annotations

import logging

import src.model_usage as mu
from src.model_usage import _LOGGER_NAME, log_model_usage


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
    # timestamp|stage|model|tokens
    assert len(fields) == 4
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
