"""Loader for my_profile/email_config.yaml — Track B sender allowlist + knobs.

The config file is the single human-readable source of truth for which
senders Track B may treat as job alerts (and therefore mark read / archive
in later pipeline stages) and for the Track B runtime knobs (top_n,
lookback_days, archive_folder).

Load semantics:
  - File missing → built-in defaults (mirroring the template in
    my_profile_example/), with a log line pointing at the template copy.
    Setups that predate the config file keep working unchanged.
  - File present but malformed, or listing no senders → EmailConfigError.
    A broken config must fail loud — it can never silently widen the
    allowlist to fetch-everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

EMAIL_CONFIG_PATH = Path("my_profile/email_config.yaml")
EXAMPLE_EMAIL_CONFIG_PATH = Path("my_profile_example/email_config.yaml")

VALID_TIERS = ("friendly", "ok", "hostile")
DEFAULT_TIER = "ok"
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_ARCHIVE_FOLDER = "apply-daemon/archive"

# Built-in allowlist used when my_profile/email_config.yaml doesn't exist.
# Mirrors my_profile_example/email_config.yaml and the sender constants in
# email_classifier.py — test_email_config.py asserts they stay in sync.
_DEFAULT_ALERT_SENDERS: dict[str, dict] = {
    "linkedin": {
        "tier": "friendly",
        "addresses": [
            "jobs-noreply@linkedin.com",
            "jobs-listings@linkedin.com",
            "jobalerts-noreply@linkedin.com",
            "jobalerts@linkedin.com",
        ],
    },
    "indeed": {
        "tier": "friendly",
        "addresses": [
            "alert@indeed.com",
            "jobalert@indeed.com",
            "donotreply@jobalert.indeed.com",
        ],
    },
    "glassdoor": {
        "tier": "ok",
        "addresses": ["noreply@glassdoor.com"],
    },
    "google_alerts": {
        "tier": "ok",
        "addresses": [
            "googlealerts-noreply@google.com",
            "notify-noreply@google.com",
        ],
    },
}

_DEFAULT_SUBJECT_HINTS = [
    "job alert",
    "job picks",
    "new jobs for you",
    "jobs that match",
    "matching jobs",
    "jobs you might",
    "recommended jobs",
    "jobs in your area",
]


class EmailConfigError(ValueError):
    """Raised when email_config.yaml exists but cannot be used safely."""


@dataclass
class SenderGroup:
    """One source's allowlisted sender addresses and its ranking tier."""

    source: str
    addresses: list[str]
    tier: str = DEFAULT_TIER


@dataclass
class EmailConfig:
    """Parsed Track B email configuration."""

    senders: list[SenderGroup] = field(default_factory=list)
    subject_hints: list[str] = field(default_factory=list)
    top_n: int | None = None
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    archive_folder: str = DEFAULT_ARCHIVE_FOLDER
    origin: str = "file"  # "file" or "defaults"

    def all_addresses(self) -> list[str]:
        """Every allowlisted sender address, lowercased."""
        return [a.lower() for g in self.senders for a in g.addresses]

    def source_for(self, sender: str) -> str | None:
        """Map a From header to its source name, or None if not allowlisted."""
        sender_lower = sender.lower()
        for group in self.senders:
            if any(a.lower() in sender_lower for a in group.addresses):
                return group.source
        return None

    def tier_for(self, sender: str) -> str | None:
        """Ranking tier for a From header, or None if not allowlisted."""
        sender_lower = sender.lower()
        for group in self.senders:
            if any(a.lower() in sender_lower for a in group.addresses):
                return group.tier
        return None


def load_email_config(path: Path | None = None) -> EmailConfig:
    """Load and validate the Track B email config.

    Missing file → built-in defaults. Present-but-broken file → EmailConfigError.
    """
    cfg_path = path or EMAIL_CONFIG_PATH
    if not cfg_path.exists():
        logger.info(
            "email_config.yaml not found at %s — using built-in defaults. "
            "Copy %s into my_profile/ to customize.",
            cfg_path,
            EXAMPLE_EMAIL_CONFIG_PATH,
        )
        return _default_config()

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EmailConfigError(f"email_config.yaml is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise EmailConfigError("email_config.yaml top level must be a mapping")

    return _parse_config(raw)


def _default_config() -> EmailConfig:
    config = _parse_config(
        {
            "alert_senders": _DEFAULT_ALERT_SENDERS,
            "subject_hints": _DEFAULT_SUBJECT_HINTS,
        }
    )
    config.origin = "defaults"
    return config


def _parse_config(raw: dict) -> EmailConfig:
    senders = _parse_senders(raw.get("alert_senders"))
    subject_hints = _parse_subject_hints(raw.get("subject_hints"))
    settings = raw.get("settings") or {}
    if not isinstance(settings, dict):
        raise EmailConfigError("settings must be a mapping")

    return EmailConfig(
        senders=senders,
        subject_hints=subject_hints,
        top_n=_parse_top_n(settings.get("top_n")),
        lookback_days=_parse_positive_int(
            settings.get("lookback_days"), "lookback_days", DEFAULT_LOOKBACK_DAYS
        ),
        archive_folder=_parse_archive_folder(settings.get("archive_folder")),
    )


def _parse_senders(raw_senders) -> list[SenderGroup]:
    if not isinstance(raw_senders, dict) or not raw_senders:
        raise EmailConfigError(
            "alert_senders must be a non-empty mapping of source → {tier, addresses}"
        )
    groups: list[SenderGroup] = []
    for source, entry in raw_senders.items():
        if not isinstance(entry, dict):
            raise EmailConfigError(f"alert_senders.{source} must be a mapping")
        tier = entry.get("tier", DEFAULT_TIER)
        if tier not in VALID_TIERS:
            raise EmailConfigError(
                f"alert_senders.{source}.tier must be one of {VALID_TIERS}, got {tier!r}"
            )
        addresses = entry.get("addresses")
        if (
            not isinstance(addresses, list)
            or not addresses
            or not all(isinstance(a, str) and a.strip() for a in addresses)
        ):
            raise EmailConfigError(
                f"alert_senders.{source}.addresses must be a non-empty list of strings"
            )
        groups.append(
            SenderGroup(
                source=str(source),
                addresses=[a.strip() for a in addresses],
                tier=tier,
            )
        )
    return groups


def _parse_subject_hints(raw_hints) -> list[str]:
    if raw_hints is None:
        return list(_DEFAULT_SUBJECT_HINTS)
    if not isinstance(raw_hints, list) or not all(
        isinstance(h, str) and h.strip() for h in raw_hints
    ):
        raise EmailConfigError("subject_hints must be a list of non-empty strings")
    return [h.strip() for h in raw_hints]


def _parse_top_n(value) -> int | None:
    if value is None or value == "":
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EmailConfigError(f"settings.top_n must be a positive integer, got {value!r}")
    return value


def _parse_positive_int(value, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EmailConfigError(f"settings.{name} must be a positive integer, got {value!r}")
    return value


def _parse_archive_folder(value) -> str:
    if value is None or value == "":
        return DEFAULT_ARCHIVE_FOLDER
    if not isinstance(value, str) or not value.strip():
        raise EmailConfigError("settings.archive_folder must be a non-empty string")
    return value.strip()
