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
        assert payload["count"] == 0 and payload["listings"] == []

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
        "date_ingested", "age_days", "distance",
    }
    DETAIL_KEYS = CARD_KEYS | {
        "reason", "job_summary", "matching_skills", "missing_skills",
    }

    def test_next_card_keys(self, db, capsys):
        _seed(db)
        _, payload = _run_json(capsys, "next")
        assert set(payload) == {"verb", "count", "listings", "max_age_days",
                                "hidden_stale"}
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


class TestDeepDive:
    """I-2 — full detail from local cache only; never spends tokens."""

    def _with_assets(self, tmp_path, monkeypatch, job_id, *, research=None,
                     auto=None):
        import src.cli as cli
        out = tmp_path / "output"
        folder = out / f"Acme_Role_{job_id[:8]}"
        folder.mkdir(parents=True)
        if research is not None:
            (folder / "deep_research_context.txt").write_text(research,
                                                              encoding="utf-8")
        if auto is not None:
            (folder / "auto_assets.json").write_text(json.dumps(auto),
                                                     encoding="utf-8")
        monkeypatch.setattr(cli, "OUTPUT_DIR", out)
        return folder

    def test_unknown_id(self, db, capsys):
        code, payload = _run_json(capsys, "deep-dive", "nope")
        assert code == 1 and payload["error"] == "not_found"

    def test_cache_miss_is_not_an_error(self, db, capsys):
        job_id = _seed(db)
        code, payload = _run_json(capsys, "deep-dive", job_id)
        assert code == 0 and payload["ok"] is True
        assert payload["research"] == {"cached": False, "folder": None,
                                       "context": None}
        assert payload["post_research"] is None

    def test_cache_miss_human_output_offers(self, db, capsys):
        job_id = _seed(db)
        _, out = _run(capsys, "deep-dive", job_id)
        assert "No research cached" in out

    def test_returns_cached_dossier(self, db, capsys, tmp_path, monkeypatch):
        job_id = _seed(db)
        self._with_assets(tmp_path, monkeypatch, job_id,
                          research="Acme builds synthetic widgets.")
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert payload["research"]["cached"] is True
        assert "synthetic widgets" in payload["research"]["context"]

    def test_surfaces_post_research_verdict(self, db, capsys, tmp_path,
                                            monkeypatch):
        job_id = _seed(db, confidence=95)
        self._with_assets(tmp_path, monkeypatch, job_id, research="ctx", auto={
            "post_research_verdict": "MAYBE",
            "post_research_confidence": 22,
            "match_analysis": "Weaker than the listing implies.",
            "updated_skills_match": {"matching": ["Python"], "missing": ["Rust"]},
        })
        _, payload = _run_json(capsys, "deep-dive", job_id)
        post = payload["post_research"]
        assert post["verdict"] == "MAYBE"
        assert post["confidence"] == 22
        assert post["matching_skills"] == ["Python"]
        assert post["missing_skills"] == ["Rust"]

    def test_computes_confidence_delta(self, db, capsys, tmp_path, monkeypatch):
        """The disagreement is the point of the verb — don't make the reader
        subtract two numbers in their head."""
        job_id = _seed(db, confidence=95)
        self._with_assets(tmp_path, monkeypatch, job_id, research="ctx", auto={
            "post_research_verdict": "MAYBE", "post_research_confidence": 22,
        })
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert payload["post_research"]["confidence_delta"] == -73

    def test_delta_shown_in_human_output(self, db, capsys, tmp_path, monkeypatch):
        job_id = _seed(db, confidence=90)
        self._with_assets(tmp_path, monkeypatch, job_id, research="ctx", auto={
            "post_research_verdict": "YES", "post_research_confidence": 80,
        })
        _, out = _run(capsys, "deep-dive", job_id)
        assert "-10 vs Stage 5" in out

    def test_skills_as_json_string_shape(self, db, capsys, tmp_path, monkeypatch):
        """updated_skills_match values arrive as a list or a JSON string
        depending on which model wrote them."""
        job_id = _seed(db)
        self._with_assets(tmp_path, monkeypatch, job_id, research="ctx", auto={
            "updated_skills_match": {"matching": json.dumps(["Python", "ML"])},
        })
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert payload["post_research"]["matching_skills"] == ["Python", "ML"]

    def test_malformed_auto_assets_degrades(self, db, capsys, tmp_path,
                                            monkeypatch):
        job_id = _seed(db)
        folder = self._with_assets(tmp_path, monkeypatch, job_id, research="ctx")
        (folder / "auto_assets.json").write_text("{not json", encoding="utf-8")
        code, payload = _run_json(capsys, "deep-dive", job_id)
        assert code == 0
        assert payload["post_research"] is None
        assert payload["research"]["cached"] is True  # dossier still usable

    def test_research_without_autopilot_assets(self, db, capsys, tmp_path,
                                               monkeypatch):
        job_id = _seed(db)
        self._with_assets(tmp_path, monkeypatch, job_id, research="ctx only")
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert payload["research"]["cached"] is True
        assert payload["post_research"] is None

    def test_does_not_mark_presented(self, db, capsys):
        """deep-dive is a detail view, not a page — it must not consume the
        listing from the review queue."""
        job_id = _seed(db)
        _run_json(capsys, "deep-dive", job_id)
        assert db.get_listing_by_id(job_id)["presented_at"] is None

    def test_no_state_change(self, db, capsys, _ledger):
        job_id = _seed(db)
        _run_json(capsys, "deep-dive", job_id)
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "triaged"
        assert not _ledger.exists()  # read-only: no ledger row

    def test_json_envelope_keys(self, db, capsys):
        job_id = _seed(db)
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert set(payload) == {"verb", "ok", "listing", "research",
                                "post_research"}
        assert set(payload["research"]) == {"cached", "folder", "context"}

    def test_post_research_keys(self, db, capsys, tmp_path, monkeypatch):
        job_id = _seed(db)
        self._with_assets(tmp_path, monkeypatch, job_id, research="c", auto={
            "post_research_verdict": "YES", "post_research_confidence": 70,
        })
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert set(payload["post_research"]) == {
            "verdict", "confidence", "confidence_delta", "match_analysis",
            "matching_skills", "missing_skills",
        }

    def test_raw_email_text_never_emitted(self, db, capsys, tmp_path,
                                          monkeypatch):
        job_id = _seed(db, raw_email_text="SECRET body")
        self._with_assets(tmp_path, monkeypatch, job_id, research="ctx")
        _, payload = _run_json(capsys, "deep-dive", job_id)
        assert "SECRET" not in json.dumps(payload)


class TestTailor:
    """I-4 — in-session tailoring by default, OpenRouter as fallback."""

    def test_unknown_id(self, db, capsys):
        code, payload = _run_json(capsys, "tailor", "nope")
        assert code == 1 and payload["error"] == "not_found"

    def test_default_emits_prompt(self, db, capsys, mocker):
        job_id = _seed(db)
        mocker.patch("src.tailor.build_prompt",
                     return_value=("PROMPT BODY", {"title": "X"}, "ctx"))
        code, payload = _run_json(capsys, "tailor", job_id)
        assert code == 0
        assert payload["route"] == "in_session"
        assert payload["stage"] == "prompt"
        assert payload["prompt"] == "PROMPT BODY"
        assert job_id in payload["apply_with"]

    def test_emitting_prompt_spends_nothing_and_changes_nothing(
            self, db, capsys, mocker, _ledger):
        """Step 1 must be free and inert — the session hasn't answered yet."""
        job_id = _seed(db)
        mocker.patch("src.tailor.build_prompt",
                     return_value=("P", {"title": "X"}, ""))
        call = mocker.patch("src.tailor.generate_immediate")
        _run_json(capsys, "tailor", job_id)
        call.assert_not_called()
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "triaged"
        assert not _ledger.exists()

    def test_apply_writes_assets_and_marks_tailored(self, db, capsys, mocker,
                                                    tmp_path, _ledger):
        job_id = _seed(db)
        mocker.patch("src.tailor.build_prompt",
                     return_value=("P", {"title": "X"}, "ctx"))
        gen = mocker.patch("src.compile.generate_assets",
                           return_value=tmp_path / "out")
        payload_file = tmp_path / "resp.json"
        payload_file.write_text(json.dumps({"match_analysis": "Good fit."}),
                                encoding="utf-8")

        code, payload = _run_json(capsys, "tailor", job_id,
                                  "--apply", str(payload_file))
        assert code == 0
        assert payload["route"] == "in_session"
        assert payload["status"] == "tailored"
        gen.assert_called_once()
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "tailored"
        rec = json.loads(_ledger.read_text().splitlines()[0])
        assert rec["human_reaction"] == "tailor" and rec["surface"] == "cli"

    def test_apply_reads_stdin(self, db, capsys, mocker, tmp_path, monkeypatch):
        import io
        job_id = _seed(db)
        mocker.patch("src.tailor.build_prompt",
                     return_value=("P", {"title": "X"}, ""))
        mocker.patch("src.compile.generate_assets", return_value=tmp_path / "o")
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"match_analysis": "ok"}))
        )
        code, payload = _run_json(capsys, "tailor", job_id, "--apply", "-")
        assert code == 0 and payload["ok"] is True

    def test_apply_rejects_malformed_json(self, db, capsys, mocker, tmp_path):
        job_id = _seed(db)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, payload = _run_json(capsys, "tailor", job_id, "--apply", str(bad))
        assert code == 1 and payload["error"] == "invalid_response"
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "triaged"

    def test_apply_rejects_response_missing_match_analysis(self, db, capsys,
                                                           tmp_path):
        job_id = _seed(db)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"resume_bullet_edits": []}), encoding="utf-8")
        code, payload = _run_json(capsys, "tailor", job_id, "--apply", str(bad))
        assert code == 1 and payload["error"] == "invalid_response"

    def test_apply_rejects_empty_input(self, db, capsys, tmp_path):
        job_id = _seed(db)
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        code, payload = _run_json(capsys, "tailor", job_id, "--apply", str(empty))
        assert code == 1 and payload["error"] == "empty_input"

    def test_via_api_uses_openrouter_path(self, db, capsys, mocker, tmp_path,
                                          _ledger):
        job_id = _seed(db)
        gen = mocker.patch("src.tailor.generate_immediate",
                           return_value=(tmp_path / "out", {"match_analysis": "x"}))
        code, payload = _run_json(capsys, "tailor", job_id, "--via", "api")
        assert code == 0
        assert payload["route"] == "api"
        gen.assert_called_once_with(job_id)
        rec = json.loads(_ledger.read_text().splitlines()[0])
        assert rec["human_reaction"] == "tailor"

    def test_api_failure_is_reported_not_raised(self, db, capsys, mocker):
        job_id = _seed(db)
        mocker.patch("src.tailor.generate_immediate",
                     side_effect=RuntimeError("OPENROUTER_API_KEY not set"))
        code, payload = _run_json(capsys, "tailor", job_id, "--via", "api")
        assert code == 1 and payload["error"] == "api_failed"
        assert db.get_listing_by_id(job_id)["pipeline_status"] == "triaged"

    def test_both_routes_produce_the_same_end_state(self, db, capsys, mocker,
                                                    tmp_path):
        """Downstream must not be able to tell which route ran."""
        mocker.patch("src.tailor.build_prompt",
                     return_value=("P", {"title": "X"}, ""))
        mocker.patch("src.compile.generate_assets", return_value=tmp_path / "o")
        mocker.patch("src.tailor.generate_immediate",
                     return_value=(tmp_path / "o", {"match_analysis": "x"}))

        a = _seed(db, title="A", company="A")
        b = _seed(db, title="B", company="B")
        resp = tmp_path / "r.json"
        resp.write_text(json.dumps({"match_analysis": "x"}), encoding="utf-8")

        _run_json(capsys, "tailor", a, "--apply", str(resp))
        _run_json(capsys, "tailor", b, "--via", "api")
        assert db.get_listing_by_id(a)["pipeline_status"] == "tailored"
        assert db.get_listing_by_id(b)["pipeline_status"] == "tailored"

    def test_json_envelope_keys(self, db, capsys, mocker, tmp_path):
        job_id = _seed(db)
        mocker.patch("src.tailor.build_prompt",
                     return_value=("P", {"title": "X"}, ""))
        mocker.patch("src.compile.generate_assets", return_value=tmp_path / "o")
        resp = tmp_path / "r.json"
        resp.write_text(json.dumps({"match_analysis": "x"}), encoding="utf-8")
        _, payload = _run_json(capsys, "tailor", job_id, "--apply", str(resp))
        assert set(payload) == {"verb", "ok", "id", "route", "folder", "status"}


class TestTailorNeverSpendsInSession:
    """Regression: build_prompt runs LIVE Deep Research (network + tokens)
    whenever research_context_override is empty (tailor.py:255). The
    in-session route must always pass a non-empty override — cached dossier
    or placeholder — on BOTH steps. Found by audit, hidden by earlier tests
    that mocked build_prompt without asserting its arguments."""

    def _cache(self, tmp_path, job_id, text="CACHED DOSSIER"):
        folder = tmp_path / "output" / f"Acme_Role_{job_id[:8]}"
        folder.mkdir(parents=True)
        (folder / "deep_research_context.txt").write_text(text, encoding="utf-8")

    def test_prompt_step_passes_nonempty_override(self, db, capsys, mocker):
        job_id = _seed(db)  # no cache on disk
        bp = mocker.patch("src.tailor.build_prompt",
                          return_value=("P", {"title": "X"}, ""))
        _, payload = _run_json(capsys, "tailor", job_id)
        override = bp.call_args.kwargs["research_context_override"]
        assert override  # non-empty → tailor skips run_deep_research
        assert payload["research_cached"] is False

    def test_prompt_step_uses_cached_dossier(self, db, capsys, mocker, tmp_path):
        job_id = _seed(db)
        self._cache(tmp_path, job_id)
        bp = mocker.patch("src.tailor.build_prompt",
                          return_value=("P", {"title": "X"}, ""))
        _, payload = _run_json(capsys, "tailor", job_id)
        assert bp.call_args.kwargs["research_context_override"] == "CACHED DOSSIER"
        assert payload["research_cached"] is True

    def test_apply_step_passes_nonempty_override(self, db, capsys, mocker,
                                                 tmp_path):
        job_id = _seed(db)  # no cache
        bp = mocker.patch("src.tailor.build_prompt",
                          return_value=("P", {"title": "X"}, ""))
        gen = mocker.patch("src.compile.generate_assets",
                           return_value=tmp_path / "o")
        resp = tmp_path / "r.json"
        resp.write_text(json.dumps({"match_analysis": "x"}), encoding="utf-8")
        _run_json(capsys, "tailor", job_id, "--apply", str(resp))
        assert bp.call_args.kwargs["research_context_override"]
        # Training dump gets the real cache ("" here), never the placeholder.
        assert gen.call_args.kwargs["research_context"] == ""

    def test_apply_step_dumps_real_cache_to_training_data(self, db, capsys,
                                                          mocker, tmp_path):
        job_id = _seed(db)
        self._cache(tmp_path, job_id, "REAL RESEARCH")
        mocker.patch("src.tailor.build_prompt",
                     return_value=("P", {"title": "X"}, ""))
        gen = mocker.patch("src.compile.generate_assets",
                           return_value=tmp_path / "o")
        resp = tmp_path / "r.json"
        resp.write_text(json.dumps({"match_analysis": "x"}), encoding="utf-8")
        _run_json(capsys, "tailor", job_id, "--apply", str(resp))
        assert gen.call_args.kwargs["research_context"] == "REAL RESEARCH"


class TestStatusVerb:
    """C-1 — the read-only verb that informs the daily run decision."""

    @pytest.fixture(autouse=True)
    def _budget_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUN_LOG_PATH", str(tmp_path / "run_log"))
        monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(tmp_path / "usage.log"))
        for k in ("DAILY_USD_BUDGET", "MIN_RUN_INTERVAL_MINUTES",
                  "RUN_USD_ESTIMATE", "BUDGET_ALLOW_UNPRICED"):
            monkeypatch.delenv(k, raising=False)

    def test_json_envelope_keys(self, db, capsys):
        _, payload = _run_json(capsys, "status")
        assert set(payload) == {"verb", "queue", "budget"}
        assert set(payload["queue"]) == {
            "reviewable", "fresh", "stale_hidden", "max_age_days", "by_tier",
            "total_listings", "last_ingest", "last_ingest_age_hours",
            "last_decision",
        }
        assert set(payload["budget"]) == {
            "can_run", "reason", "spent_usd_today", "spent_tokens_today",
            "budget_usd", "remaining_usd", "minutes_since_run",
        }

    def test_empty_db_is_not_an_error(self, db, capsys):
        code, payload = _run_json(capsys, "status")
        assert code == 0
        assert payload["queue"]["reviewable"] == 0
        assert payload["queue"]["last_ingest"] is None

    def test_counts_by_tier(self, db, capsys):
        _seed(db, title="A", company="A", status="auto")
        _seed(db, title="B", company="B", status="auto_queued")
        _seed(db, title="C", company="C")  # triaged
        _, payload = _run_json(capsys, "status")
        assert payload["queue"]["reviewable"] == 3
        assert payload["queue"]["by_tier"]["auto"] == 1

    def test_decided_listings_excluded_from_queue(self, db, capsys):
        job_id = _seed(db, title="Gone", company="G")
        _run_json(capsys, "save", job_id)
        _, payload = _run_json(capsys, "status")
        assert payload["queue"]["reviewable"] == 0
        assert payload["queue"]["last_decision"] is not None

    def test_reports_budget_block(self, db, capsys, monkeypatch):
        from src.budget import record_run
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        record_run("test")
        _, payload = _run_json(capsys, "status")
        assert payload["budget"]["can_run"] is False
        assert "Cooldown" in payload["budget"]["reason"]

    def test_human_output_readable(self, db, capsys):
        _seed(db, title="A", company="A", status="auto")
        code, out = _run(capsys, "status")
        assert code == 0
        assert "Queue:" in out and "Spend:" in out and "Run:" in out

    def test_suggests_refresh_when_queue_empty(self, db, capsys):
        _, out = _run(capsys, "status")
        assert "refresh" in out.lower()

    def test_suggests_refresh_when_only_stale_remains(self, db, capsys):
        """400 stale rows must not stop status from recommending a refresh
        — the hint keys off FRESH work, not total."""
        from datetime import datetime, timedelta, timezone
        job_id = _seed(db)
        when = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        db.conn.execute("UPDATE listings SET date_ingested = ? WHERE id = ?",
                        (when, job_id))
        db.conn.commit()
        _, payload = _run_json(capsys, "status")
        assert payload["queue"]["fresh"] == 0
        assert payload["queue"]["stale_hidden"] == 1
        _, out = _run(capsys, "status")
        assert "refresh" in out.lower()

    def test_card_shows_age_and_distance(self, db, capsys):
        """Distance silently shapes queue order — an invisible sort key
        reads as a broken sort, so both new keys surface on the card."""
        job_id = _seed(db)
        db.set_distance_bucket(job_id, 1)
        _, payload = _run_json(capsys, "next")
        card = payload["listings"][0]
        assert card["distance"] == "Local"
        assert card["age_days"] == 0
        _, out = _run(capsys, "show", job_id)
        assert "Local" in out

    def test_status_makes_no_state_change(self, db, capsys, _ledger):
        job_id = _seed(db)
        _run_json(capsys, "status")
        assert db.get_listing_by_id(job_id)["presented_at"] is None
        assert not _ledger.exists()


class TestFreshnessBound:
    """D-2 — the review surface had no age check while digest.py bounded
    Slack to 14 days. Measured before the fix: 0 reviewable listings under
    15 days old, 91% over 30 days."""

    def _age(self, db, job_id, days):
        from datetime import datetime, timedelta, timezone
        when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        db.conn.execute("UPDATE listings SET date_ingested = ? WHERE id = ?",
                        (when, job_id))
        db.conn.commit()

    def test_stale_listing_hidden_by_default(self, db, capsys):
        job_id = _seed(db)
        self._age(db, job_id, 45)
        _, payload = _run_json(capsys, "next")
        assert payload["count"] == 0
        assert payload["hidden_stale"] == 1

    def test_fresh_listing_shown(self, db, capsys):
        _seed(db)
        _, payload = _run_json(capsys, "next")
        assert payload["count"] == 1
        assert payload["hidden_stale"] == 0

    def test_max_age_zero_disables_bound(self, db, capsys):
        job_id = _seed(db)
        self._age(db, job_id, 400)
        _, payload = _run_json(capsys, "next", "--max-age", "0")
        assert payload["count"] == 1

    def test_explicit_max_age_override(self, db, capsys):
        job_id = _seed(db)
        self._age(db, job_id, 45)
        assert _run_json(capsys, "next", "--max-age", "60")[1]["count"] == 1

    def test_env_default(self, db, capsys, monkeypatch):
        monkeypatch.setenv("REVIEW_MAX_AGE_DAYS", "10")
        job_id = _seed(db)
        self._age(db, job_id, 20)
        assert _run_json(capsys, "next")[1]["count"] == 0

    def test_empty_page_explains_staleness(self, db, capsys):
        """An empty queue with no explanation reads as a broken tool."""
        job_id = _seed(db)
        self._age(db, job_id, 45)
        _, out = _run(capsys, "next")
        assert "older than 30 days" in out
        assert "--max-age 0" in out

    def test_visible_page_notes_hidden_count(self, db, capsys):
        fresh = _seed(db, title="Fresh", company="F")
        stale = _seed(db, title="Stale", company="S")
        self._age(db, stale, 45)
        _, out = _run(capsys, "next")
        assert "Fresh" in out and "1 older than 30d hidden" in out
        assert fresh  # fresh row is the one shown


class TestLocationOrdering:
    """D-3 — location joins the sort inside a confidence band."""

    def _bucket(self, db, job_id, bucket):
        db.set_distance_bucket(job_id, bucket)

    def test_nearer_wins_within_band(self, db, capsys):
        far = _seed(db, title="Far", company="F", confidence=92)
        near = _seed(db, title="Near", company="N", confidence=90)
        self._bucket(db, far, 3)    # relocation
        self._bucket(db, near, 1)   # local
        _, payload = _run_json(capsys, "next")
        # 90 and 92 share a 5-point band, so distance decides.
        assert payload["listings"][0]["id"] == near

    def test_big_quality_gap_still_wins(self, db, capsys):
        far = _seed(db, title="Far", company="F", confidence=98)
        near = _seed(db, title="Near", company="N", confidence=60)
        self._bucket(db, far, 3)
        self._bucket(db, near, 1)
        _, payload = _run_json(capsys, "next")
        # Different bands — location must not override a real quality gap.
        assert payload["listings"][0]["id"] == far

    def test_unknown_distance_sorts_last_in_band(self, db, capsys):
        known = _seed(db, title="Known", company="K", confidence=90)
        _seed(db, title="Unknown", company="U", confidence=92)  # no bucket
        self._bucket(db, known, 2)
        _, payload = _run_json(capsys, "next")
        # An unknown commute must not masquerade as a short one.
        assert payload["listings"][0]["id"] == known

    def test_tier_still_outranks_location(self, db, capsys):
        auto = _seed(db, title="Auto", company="A", confidence=60,
                     status="auto")
        queued = _seed(db, title="Queued", company="Q", confidence=99,
                       status="auto_queued")
        self._bucket(db, auto, 3)
        self._bucket(db, queued, 0)
        _, payload = _run_json(capsys, "next")
        assert payload["listings"][0]["id"] == auto
