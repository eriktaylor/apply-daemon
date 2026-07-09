"""Tests for src/email_config.py — Track B sender allowlist + knobs loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.email_classifier import GOOGLE_ALERTS_SENDERS, JOB_DIGEST_SENDERS
from src.email_config import (
    DEFAULT_ARCHIVE_FOLDER,
    DEFAULT_LOOKBACK_DAYS,
    EXAMPLE_EMAIL_CONFIG_PATH,
    EmailConfigError,
    load_email_config,
)

VALID_YAML = """
alert_senders:
  linkedin:
    tier: friendly
    addresses:
      - jobs-noreply@linkedin.com
  indeed:
    addresses:
      - alert@indeed.com
subject_hints:
  - "job alert"
settings:
  top_n: 10
  lookback_days: 7
  archive_folder: "custom/archive"
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "email_config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadValidFile:
    def test_parses_senders_and_settings(self, tmp_path):
        config = load_email_config(_write(tmp_path, VALID_YAML))
        assert config.origin == "file"
        assert config.top_n == 10
        assert config.lookback_days == 7
        assert config.archive_folder == "custom/archive"
        assert "jobs-noreply@linkedin.com" in config.all_addresses()
        assert "alert@indeed.com" in config.all_addresses()

    def test_tier_defaults_to_ok(self, tmp_path):
        config = load_email_config(_write(tmp_path, VALID_YAML))
        assert config.tier_for("Indeed <alert@indeed.com>") == "ok"
        assert config.tier_for("LinkedIn <jobs-noreply@linkedin.com>") == "friendly"

    def test_source_for_full_from_header(self, tmp_path):
        config = load_email_config(_write(tmp_path, VALID_YAML))
        assert config.source_for("LinkedIn Jobs <JOBS-NOREPLY@linkedin.com>") == "linkedin"
        assert config.source_for("someone@example.com") is None
        assert config.tier_for("someone@example.com") is None

    def test_blank_top_n_means_no_cap(self, tmp_path):
        yaml_text = VALID_YAML.replace("top_n: 10", "top_n:")
        config = load_email_config(_write(tmp_path, yaml_text))
        assert config.top_n is None

    def test_missing_settings_section_uses_defaults(self, tmp_path):
        yaml_text = """
alert_senders:
  linkedin:
    addresses: [jobs-noreply@linkedin.com]
"""
        config = load_email_config(_write(tmp_path, yaml_text))
        assert config.top_n is None
        assert config.lookback_days == DEFAULT_LOOKBACK_DAYS
        assert config.archive_folder == DEFAULT_ARCHIVE_FOLDER
        # subject_hints falls back to the built-in list
        assert "job alert" in config.subject_hints


class TestMissingFile:
    def test_returns_builtin_defaults(self, tmp_path):
        config = load_email_config(tmp_path / "does_not_exist.yaml")
        assert config.origin == "defaults"
        assert config.all_addresses()  # non-empty
        assert config.source_for("jobs-noreply@linkedin.com") == "linkedin"
        assert config.lookback_days == DEFAULT_LOOKBACK_DAYS


class TestMalformedFile:
    def test_invalid_yaml_raises(self, tmp_path):
        with pytest.raises(EmailConfigError, match="not valid YAML"):
            load_email_config(_write(tmp_path, "alert_senders: [unclosed"))

    def test_non_mapping_top_level_raises(self, tmp_path):
        with pytest.raises(EmailConfigError, match="must be a mapping"):
            load_email_config(_write(tmp_path, "- just\n- a\n- list\n"))

    def test_missing_alert_senders_raises(self, tmp_path):
        with pytest.raises(EmailConfigError, match="alert_senders"):
            load_email_config(_write(tmp_path, "subject_hints: ['job alert']\n"))

    def test_empty_alert_senders_raises(self, tmp_path):
        with pytest.raises(EmailConfigError, match="alert_senders"):
            load_email_config(_write(tmp_path, "alert_senders: {}\n"))

    def test_empty_addresses_raises(self, tmp_path):
        yaml_text = """
alert_senders:
  linkedin:
    addresses: []
"""
        with pytest.raises(EmailConfigError, match="addresses"):
            load_email_config(_write(tmp_path, yaml_text))

    def test_invalid_tier_raises(self, tmp_path):
        yaml_text = """
alert_senders:
  linkedin:
    tier: bogus
    addresses: [jobs-noreply@linkedin.com]
"""
        with pytest.raises(EmailConfigError, match="tier"):
            load_email_config(_write(tmp_path, yaml_text))

    def test_negative_top_n_raises(self, tmp_path):
        yaml_text = VALID_YAML.replace("top_n: 10", "top_n: -1")
        with pytest.raises(EmailConfigError, match="top_n"):
            load_email_config(_write(tmp_path, yaml_text))

    def test_non_int_lookback_raises(self, tmp_path):
        yaml_text = VALID_YAML.replace("lookback_days: 7", "lookback_days: soon")
        with pytest.raises(EmailConfigError, match="lookback_days"):
            load_email_config(_write(tmp_path, yaml_text))


class TestTemplateFile:
    """The committed template must parse and stay in sync with the classifier."""

    def test_template_parses(self):
        config = load_email_config(EXAMPLE_EMAIL_CONFIG_PATH)
        assert config.origin == "file"
        assert config.top_n is None
        assert config.lookback_days == 14
        assert config.archive_folder == "apply-daemon/archive"

    def test_template_addresses_known_to_classifier(self):
        """Every template sender must be recognized by email_classifier's
        constants, so config-driven and heuristic classification agree."""
        config = load_email_config(EXAMPLE_EMAIL_CONFIG_PATH)
        known = set(JOB_DIGEST_SENDERS) | set(GOOGLE_ALERTS_SENDERS)
        for address in config.all_addresses():
            assert address in known, f"{address} not in email_classifier constants"

    def test_defaults_match_template(self):
        template = load_email_config(EXAMPLE_EMAIL_CONFIG_PATH)
        defaults = load_email_config(Path("nonexistent/email_config.yaml"))
        assert set(defaults.all_addresses()) == set(template.all_addresses())
        assert defaults.subject_hints == template.subject_hints
