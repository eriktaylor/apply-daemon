"""Tests for the shared human-feedback ledger (E-1).

The ledger is the sole input to eval/preference_pairs.py, so its wire format
is a contract, not an implementation detail. Synthetic fixtures only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.human_labels import (
    SURFACE_CLI,
    SURFACE_SLACK,
    append_human_label,
)


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestRecordSchema:
    def test_writes_expected_keys(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "save", {"title": "Engineer"}, path=target)
        (rec,) = _rows(target)
        assert set(rec) == {
            "job_id", "timestamp", "human_reaction", "surface", "bulk", "listing"
        }
        assert rec["job_id"] == "job-1"
        assert rec["human_reaction"] == "save"
        assert rec["listing"] == {"title": "Engineer"}

    def test_defaults_to_slack_surface(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "save", {}, path=target)
        assert _rows(target)[0]["surface"] == SURFACE_SLACK

    def test_bulk_defaults_false_and_is_always_emitted(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "pass", {}, path=target)
        rec = _rows(target)[0]
        assert rec["bulk"] is False  # present, not absent — no key ambiguity

    def test_bulk_flag_recorded(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "pass", {}, surface=SURFACE_CLI, bulk=True,
                           path=target)
        assert _rows(target)[0]["bulk"] is True

    def test_appends_rather_than_truncates(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "save", {}, path=target)
        append_human_label("job-2", "pass", {}, path=target)
        assert [r["job_id"] for r in _rows(target)] == ["job-1", "job-2"]

    def test_creates_parent_directory(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "labels.jsonl"
        append_human_label("job-1", "save", {}, path=target)
        assert target.exists()

    def test_timestamp_is_utc_isoformat(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "save", {}, path=target)
        parsed = datetime.fromisoformat(_rows(target)[0]["timestamp"])
        assert parsed.tzinfo is not None

    def test_serializes_datetime_in_listing(self, tmp_path):
        target = tmp_path / "labels.jsonl"
        stamp = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        append_human_label("job-1", "save", {"date_ingested": stamp}, path=target)
        assert _rows(target)[0]["listing"]["date_ingested"] == stamp.isoformat()

    def test_unknown_surface_still_records(self, tmp_path, caplog):
        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "save", {}, surface="carrier-pigeon",
                           path=target)
        # Warn, but never drop a human decision (invariant 5).
        assert _rows(target)[0]["surface"] == "carrier-pigeon"


class TestPathResolution:
    """The override exists so throwaway runs can't append phantom decisions
    to the real ledger — they are indistinguishable from genuine ones."""

    def test_defaults_to_data_dir(self, monkeypatch):
        from src.human_labels import LABELS_PATH, resolve_labels_path
        monkeypatch.delenv("HUMAN_LABELS_PATH", raising=False)
        assert resolve_labels_path() == LABELS_PATH

    def test_env_override(self, tmp_path, monkeypatch):
        from src.human_labels import resolve_labels_path
        target = tmp_path / "elsewhere.jsonl"
        monkeypatch.setenv("HUMAN_LABELS_PATH", str(target))
        assert resolve_labels_path() == target

    def test_blank_override_ignored(self, monkeypatch):
        from src.human_labels import LABELS_PATH, resolve_labels_path
        monkeypatch.setenv("HUMAN_LABELS_PATH", "  ")
        assert resolve_labels_path() == LABELS_PATH

    def test_append_honours_override(self, tmp_path, monkeypatch):
        target = tmp_path / "override.jsonl"
        monkeypatch.setenv("HUMAN_LABELS_PATH", str(target))
        append_human_label("job-1", "save", {})
        assert target.exists()
        assert _rows(target)[0]["job_id"] == "job-1"


class TestSurfaceParity:
    """A CLI decision and the equivalent Slack reaction must differ only in
    `surface` — otherwise S-2's per-surface analysis compares apples to
    oranges, and E-3's pair extraction sees two schemas."""

    def test_rows_differ_only_in_surface(self, tmp_path):
        listing = {"title": "ML Engineer", "company": "Acme"}
        slack_path = tmp_path / "slack.jsonl"
        cli_path = tmp_path / "cli.jsonl"

        append_human_label("job-1", "pass", listing, surface=SURFACE_SLACK,
                           path=slack_path)
        append_human_label("job-1", "pass", listing, surface=SURFACE_CLI,
                           path=cli_path)

        slack_rec = _rows(slack_path)[0]
        cli_rec = _rows(cli_path)[0]
        assert slack_rec.keys() == cli_rec.keys()

        differing = {
            k for k in slack_rec
            if slack_rec[k] != cli_rec[k] and k != "timestamp"
        }
        assert differing == {"surface"}


class TestSweeperDelegation:
    """sweeper._append_human_label is a seam that pins surface='slack'."""

    def test_sweeper_wrapper_pins_slack_surface(self, tmp_path, monkeypatch):
        import src.human_labels as hl
        import src.sweeper as sweeper

        target = tmp_path / "labels.jsonl"
        monkeypatch.setattr(hl, "LABELS_PATH", target)
        sweeper._append_human_label("job-1", "tailor", {"title": "X"})
        assert _rows(target)[0]["surface"] == SURFACE_SLACK


class TestBackwardCompatibility:
    def test_preference_pair_reader_tolerates_new_fields(self, tmp_path):
        """eval/preference_pairs.py reads via rec.get() — new keys are inert."""
        from eval.preference_pairs import load_labels

        target = tmp_path / "labels.jsonl"
        append_human_label("job-1", "save", {}, surface=SURFACE_CLI, path=target)
        append_human_label("job-2", "pass", {}, surface=SURFACE_SLACK, path=target)
        polarity = load_labels(target)
        assert polarity == {"job-1": "positive", "job-2": "negative"}

    def test_legacy_rows_without_surface_still_parse(self, tmp_path):
        """Rows written before E-1 have no `surface` key — implicitly slack."""
        from eval.preference_pairs import load_labels

        target = tmp_path / "labels.jsonl"
        target.write_text(
            json.dumps({"job_id": "old-1", "human_reaction": "save",
                        "listing": {}}) + "\n",
            encoding="utf-8",
        )
        assert load_labels(target) == {"old-1": "positive"}
