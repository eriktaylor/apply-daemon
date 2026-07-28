"""Tests for src/cli.py (I-1) and its JSON contract (E-2).

The `--json` schema is what the Claude skill parses, so the shape tests here
are a contract: adding keys is fine, renaming or removing one is a breaking
change that must fail loudly. Synthetic fixtures only.
"""

from __future__ import annotations

import json

import pytest

from src.cli import SESSION_WINDOW_MINUTES, main
from src.db import Database
from src.models import JobListing


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real DB the CLI will open via APPLY_DAEMON_DB."""
    path = tmp_path / "cli.db"
    monkeypatch.setenv("APPLY_DAEMON_DB", str(path))
    database = Database(path)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """Redirect the human-label ledger so tests never touch data/."""
    import src.human_labels as hl
    target = tmp_path / "labels.jsonl"
    monkeypatch.setattr(hl, "LABELS_PATH", target)
    return target


@pytest.fixture(autouse=True)
def _no_output_dir(tmp_path, monkeypatch):
    """Point the research-cache probe at an empty dir (no output/ in tests)."""
    import src.cli as cli
    monkeypatch.setattr(cli, "OUTPUT_DIR", tmp_path / "output")


def _seed(db, *, title="ML Engineer", company="Acme", confidence=90,
          status=None, verdict="YES", **kw):
    listing = JobListing(
        source="linkedin", email_classification="JOB_DIGEST",
        title=title, company=company, verdict=verdict,
        confidence=confidence, model_used="test", **kw,
    )
    db.insert_listing(listing)
    if status:
        db.update_pipeline_status(listing.id, status)
    return listing.id


def _run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def _run_json(capsys, *argv):
    code, out = _run(capsys, "--json", *argv)
    return code, json.loads(out)


class TestNextVerb:
    def test_returns_page_of_three_by_default(self, db, capsys):
        for i in range(5):
            _seed(db, title=f"Role {i}", company=f"Co {i}")
        code, payload = _run_json(capsys, "next")
        assert code == 0
        assert payload["count"] == 3
        assert len(payload["listings"]) == 3

    def test_top_flag_overrides(self, db, capsys):
        for i in range(5):
            _seed(db, title=f"Role {i}", company=f"Co {i}")
        _, payload = _run_json(capsys, "next", "--top", "2")
        assert payload["count"] == 2

    def test_pages_forward_on_repeat(self, db, capsys):
        for i in range(6):
            _seed(db, title=f"Role {i}", company=f"Co {i}", confidence=90 - i)
        _, first = _run_json(capsys, "next")
        _, second = _run_json(capsys, "next")
        first_ids = {c["id"] for c in first["listings"]}
        second_ids = {c["id"] for c in second["listings"]}
        assert first_ids and second_ids
        assert first_ids.isdisjoint(second_ids)

    def test_empty_queue_is_not_an_error(self, db, capsys):
        code, payload = _run_json(capsys, "next")
        assert code == 0
        assert payload == {"verb": "next", "count": 0, "listings": []}

    def test_auto_tier_first(self, db, capsys):
        _seed(db, title="Queued", company="A", confidence=99, status="auto_queued")
        auto = _seed(db, title="Auto", company="B", confidence=50, status="auto")
        _, payload = _run_json(capsys, "next")
        assert payload["listings"][0]["id"] == auto
        assert payload["listings"][0]["tier"] == "auto"

    def test_human_output_is_readable(self, db, capsys):
        _seed(db, title="ML Engineer", company="Acme")
        code, out = _run(capsys, "next")
        assert code == 0
        assert "ML Engineer" in out and "Acme" in out


class TestJsonContract:
    """E-2 — the keys the skill depends on."""

    CARD_KEYS = {
        "id", "title", "company", "location", "salary", "verdict",
        "confidence", "status", "tier", "research_cached", "url",
        "date_ingested",
    }
    DETAIL_KEYS = CARD_KEYS | {
        "reason", "job_summary", "matching_skills", "missing_skills",
    }

    def test_next_card_keys(self, db, capsys):
        _seed(db)
        _, payload = _run_json(capsys, "next")
        assert set(payload) == {"verb", "count", "listings"}
        assert set(payload["listings"][0]) == self.CARD_KEYS

    def test_show_card_keys(self, db, capsys):
        job_id = _seed(db)
        _, payload = _run_json(capsys, "show", job_id)
        assert set(payload) == {"verb", "ok", "listing"}
        assert set(payload["listing"]) == self.DETAIL_KEYS

    def test_decision_keys(self, db, capsys):
        job_id = _seed(db)
        _, payload = _run_json(capsys, "save", job_id)
        assert set(payload) == {"verb", "ok", "id", "status", "bulk"}

    def test_bulk_keys(self, db, capsys):
        _seed(db)
        _run_json(capsys, "next")
        _, payload = _run_json(capsys, "pass", "--all")
        assert set(payload) == {"verb", "ok", "ids", "count", "bulk"}

    def test_json_flag_accepted_on_either_side_of_verb(self, db, capsys):
        """`--json next` and `next --json` must both work — a skill will
        reach for whichever reads naturally."""
        _seed(db)
        before = main(["--json", "next"])
        out_before = capsys.readouterr().out
        after = main(["next", "--json"])
        out_after = capsys.readouterr().out
        assert before == after == 0
        assert json.loads(out_before)["verb"] == "next"
        assert json.loads(out_after)["verb"] == "next"

    def test_error_keys(self, db, capsys):
        _, payload = _run_json(capsys, "show", "nope")
        assert set(payload) == {"verb", "ok", "error", "id"}
        assert payload["error"] == "not_found"

    def test_raw_email_text_never_emitted(self, db, capsys):
        """Invariant 3 — CLI output reaches a model context and logs."""
        job_id = _seed(db, raw_email_text="SECRET raw email body")
        _, page = _run_json(capsys, "next")
        _, detail = _run_json(capsys, "show", job_id)
        assert "raw_email_text" not in page["listings"][0]
        assert "raw_email_text" not in detail["listing"]
        assert "SECRET" not in json.dumps(page) + json.dumps(detail)

    def test_skills_parsed_to_lists(self, db, capsys):
        # matching_skills is a JSON *string* column (models.py), unlike links.
        job_id = _seed(db, matching_skills=json.dumps(["Python", "ML"]))
        _, payload = _run_json(capsys, "show", job_id)
        assert payload["listing"]["matching_skills"] == ["Python", "ML"]

    def test_malformed_skills_degrade_to_empty(self, db, capsys):
        job_id = _seed(db, matching_skills="not json at all")
        _, payload = _run_json(capsys, "show", job_id)
        assert payload["listing"]["matching_skills"] == []

    def test_url_taken_from_links(self, db, capsys):
        job_id = _seed(db, links=["https://example.com/job/1"])
        _, payload = _run_json(capsys, "show", job_id)
        assert payload["listing"]["url"] == "https://example.com/job/1"

    def test_url_is_none_without_links(self, db, capsys):
        job_id = _seed(db)
        _, payload = _run_json(capsys, "show", job_id)
        assert payload["listing"]["url"] is None


class TestDecisions:
    def test_save_sets_status(self, db, capsys):
        job_id = _seed(db)
        code, payload = _run_json(capsys, "save", job_id)
        assert code == 0 and payload["ok"] is True
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "saved"

    def test_pass_sets_status(self, db, capsys):
        job_id = _seed(db)
        _run_json(capsys, "pass", job_id)
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "passed"

    def test_unknown_id_exits_nonzero(self, db, capsys):
        code, payload = _run_json(capsys, "save", "missing")
        assert code == 1 and payload["ok"] is False

    def test_repeat_decision_reports_no_transition(self, db, capsys):
        job_id = _seed(db)
        _run_json(capsys, "pass", job_id)
        code, payload = _run_json(capsys, "pass", job_id)
        assert code == 1
        assert payload["error"] == "no_transition"

    def test_missing_id_without_all_is_usage_error(self, db, capsys):
        assert main(["save"]) == 2

    def test_save_cannot_undo_a_pass(self, db, capsys):
        """Pass is terminal (docs/CHATOPS.md); reviving goes via re-triage."""
        job_id = _seed(db)
        _run_json(capsys, "pass", job_id)
        code, payload = _run_json(capsys, "save", job_id)
        assert code == 1 and payload["error"] == "no_transition"
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "passed"

    def test_pass_can_follow_save(self, db, capsys):
        """Changing your mind forward is allowed."""
        job_id = _seed(db)
        _run_json(capsys, "save", job_id)
        code, _ = _run_json(capsys, "pass", job_id)
        assert code == 0
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "passed"

    def test_pass_all_clears_current_page(self, db, capsys):
        for i in range(3):
            _seed(db, title=f"Role {i}", company=f"Co {i}")
        _run_json(capsys, "next")
        code, payload = _run_json(capsys, "pass", "--all")
        assert code == 0 and payload["count"] == 3
        assert _run_json(capsys, "next")[1]["count"] == 0

    def test_pass_all_with_nothing_presented(self, db, capsys):
        code, payload = _run_json(capsys, "pass", "--all")
        assert code == 0 and payload["count"] == 0

    def test_pass_all_targets_only_the_latest_page(self, db, capsys):
        """`pass --all` means the rows on screen, not everything seen this
        session — otherwise paging through three pages and passing the last
        would silently discard nine listings."""
        for i in range(4):
            _seed(db, title=f"Role {i}", company=f"Co {i}", confidence=90 - i)
        _, page1 = _run_json(capsys, "next", "--top", "2")
        _, page2 = _run_json(capsys, "next", "--top", "2")

        _, payload = _run_json(capsys, "pass", "--all")
        assert payload["count"] == 2
        assert set(payload["ids"]) == {c["id"] for c in page2["listings"]}

        # Page 1 is untouched and still undecided.
        for card in page1["listings"]:
            assert db.get_listing_by_id(card["id"])["pipeline_status"] != "passed"


class TestLedger:
    """Invariant 5 — every decision is training data."""

    def test_save_writes_cli_surface_label(self, db, capsys, _ledger):
        job_id = _seed(db)
        _run_json(capsys, "save", job_id)
        (rec,) = [json.loads(line) for line in _ledger.read_text().splitlines()]
        assert rec["job_id"] == job_id
        assert rec["human_reaction"] == "save"
        assert rec["surface"] == "cli"
        assert rec["bulk"] is False

    def test_pass_all_marks_bulk(self, db, capsys, _ledger):
        for i in range(2):
            _seed(db, title=f"Role {i}", company=f"Co {i}")
        _run_json(capsys, "next")
        _run_json(capsys, "pass", "--all")
        recs = [json.loads(line) for line in _ledger.read_text().splitlines()]
        assert len(recs) == 2  # one row per listing, not one for the batch
        assert all(r["bulk"] is True for r in recs)
        assert all(r["human_reaction"] == "pass" for r in recs)

    def test_no_ledger_row_without_transition(self, db, capsys, _ledger):
        job_id = _seed(db)
        _run_json(capsys, "pass", job_id)
        _run_json(capsys, "pass", job_id)  # no-op
        recs = _ledger.read_text().splitlines()
        assert len(recs) == 1

    def test_ledger_actions_score_in_preference_pairs(self, db, capsys, _ledger):
        """CLI verbs must use the vocabulary preference_pairs scores, or the
        decisions land as neutral and never become pairs."""
        from eval.preference_pairs import load_labels

        saved = _seed(db, title="Saved", company="A")
        passed = _seed(db, title="Passed", company="B")
        _run_json(capsys, "save", saved)
        _run_json(capsys, "pass", passed)
        assert load_labels(_ledger) == {saved: "positive", passed: "negative"}


class TestSessionWindow:
    def test_window_constant_matches_plan(self):
        assert SESSION_WINDOW_MINUTES == 120

    def test_undecided_listing_returns_after_window(self, db, capsys):
        from datetime import datetime, timedelta, timezone

        job_id = _seed(db)
        _run_json(capsys, "next")
        assert _run_json(capsys, "next")[1]["count"] == 0  # inside window

        stale = (
            datetime.now(timezone.utc)
            - timedelta(minutes=SESSION_WINDOW_MINUTES + 10)
        ).isoformat()
        db.conn.execute(
            "UPDATE listings SET presented_at = ? WHERE id = ?", (stale, job_id)
        )
        db.conn.commit()
        # Not stranded: an undecided listing comes back around.
        assert _run_json(capsys, "next")[1]["count"] == 1
