"""Tests for src/cli.py (I-1) and its JSON contract (E-2).

The `--json` schema is what the Claude skill parses, so the shape tests here
are a contract: adding keys is fine, renaming or removing one is a breaking
change that must fail loudly. Synthetic fixtures only.
"""

from __future__ import annotations

import json

import pytest

from src.cli import main
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

    # The shared contract (src/listing_card.REQUIRED_FIELDS) plus the two
    # CLI-only additions. Keeping this literal rather than importing the
    # contract is deliberate: it must fail when the contract changes, so the
    # change is reviewed rather than absorbed silently.
    CARD_KEYS = {
        "id", "title", "company", "verdict", "confidence", "location",
        "distance", "url", "tldr", "skills_pct", "skills_matched",
        "skills_total", "matching_skills", "missing_skills", "age_days",
        "freshness", "tier", "research_cached",
        "status", "date_ingested", "salary",
    }
    DETAIL_KEYS = CARD_KEYS | {"reason"}

    def test_next_card_keys(self, db, capsys):
        _seed(db)
        _, payload = _run_json(capsys, "next")
        assert set(payload) == {"verb", "count", "listings", "max_age_days",
                                "hidden_stale", "awaiting_enrichment", "tiers",
                                "seen", "backlog"}
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


class TestFeedAndBacklog:
    """The feed shows what has never been delivered; the backlog shows what
    was delivered and not acted on. Merging them re-ranked one pool by a
    stable score, so the same rows won every run forever."""

    def test_a_shown_listing_leaves_the_feed(self, db, capsys):
        _seed(db)
        assert _run_json(capsys, "next")[1]["count"] == 1
        assert _run_json(capsys, "next")[1]["count"] == 0

    def test_retired_listings_are_reachable_as_the_backlog(self, db, capsys):
        """Retirement is only safe because this exists."""
        _seed(db)
        _run_json(capsys, "next")
        page = _run_json(capsys, "next", "--seen")[1]
        assert page["count"] == 1 and page["seen"] == "seen"

    def test_the_backlog_is_reported_on_the_feed(self, db, capsys):
        """Counted, so retired rows go quiet rather than invisible."""
        _seed(db)
        _run_json(capsys, "next")
        assert _run_json(capsys, "next")[1]["backlog"] == 1

    def test_deciding_clears_the_backlog(self, db, capsys):
        job_id = _seed(db)
        _run_json(capsys, "next")
        _run_json(capsys, "pass", job_id)
        assert _run_json(capsys, "next")[1]["backlog"] == 0

    def test_revisiting_the_backlog_does_not_re_stamp_it(self, db, capsys):
        """Re-stamping would reorder the backlog by re-presentation rather
        than by quality."""
        _seed(db)
        _run_json(capsys, "next")
        before = db.get_listing_by_id(
            db.get_review_queue(limit=1, seen="seen")[0]["id"])["presented_at"]
        _run_json(capsys, "next", "--seen")
        after = db.get_review_queue(limit=1, seen="seen")[0]["presented_at"]
        assert before == after


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
            "reviewable", "fresh", "ready", "backlog", "awaiting_enrichment",
            "enrichment_cap", "enriched_today", "enrichment_remaining",
            "stale_hidden", "max_age_days", "by_tier", "total_listings",
            "last_ingest", "last_ingest_age_hours", "last_decision",
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


class TestRefreshVerb:
    """C-2 — the only money-spending verb. Every test mocks subprocess; none
    of these may actually launch a pipeline stage."""

    @pytest.fixture(autouse=True)
    def _budget_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUN_LOG_PATH", str(tmp_path / "run_log"))
        monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(tmp_path / "usage.log"))
        for k in ("DAILY_USD_BUDGET", "MIN_RUN_INTERVAL_MINUTES",
                  "RUN_USD_ESTIMATE", "BUDGET_ALLOW_UNPRICED"):
            monkeypatch.delenv(k, raising=False)

    def _ok(self, mocker):
        import subprocess
        return mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""))

    def test_runs_every_stage_in_order(self, db, capsys, mocker):
        from src.cli import REFRESH_STAGES
        run = self._ok(mocker)
        code, payload = _run_json(capsys, "refresh")
        assert code == 0 and payload["ok"] is True
        assert [s["module"] for s in payload["stages"]] == [m for _, m in REFRESH_STAGES]
        assert run.call_count == len(REFRESH_STAGES)

    def test_records_the_run_before_stages(self, db, capsys, mocker):
        """Cooldown must apply to an attempt, not only a success — otherwise a
        crashing run can be retried without limit."""
        from src.budget import last_run_at
        self._ok(mocker)
        assert last_run_at() is None
        _run_json(capsys, "refresh")
        assert last_run_at() is not None

    def test_blocked_by_cooldown(self, db, capsys, mocker, monkeypatch):
        from src.budget import record_run
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        record_run("prior")
        run = self._ok(mocker)
        code, payload = _run_json(capsys, "refresh")
        assert code == 1 and payload["error"] == "budget_blocked"
        assert "Cooldown" in payload["reason"]
        run.assert_not_called()          # nothing spent

    def test_force_overrides_the_block(self, db, capsys, mocker, monkeypatch):
        from src.budget import record_run
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        record_run("prior")
        run = self._ok(mocker)
        code, payload = _run_json(capsys, "refresh", "--force")
        assert code == 0 and payload["ok"] is True
        assert run.call_count > 0

    def test_dry_run_spends_nothing_and_records_nothing(self, db, capsys, mocker):
        from src.budget import last_run_at
        run = self._ok(mocker)
        code, payload = _run_json(capsys, "refresh", "--dry-run")
        assert code == 0 and payload["dry_run"] is True
        assert payload["would_run"]
        run.assert_not_called()
        assert last_run_at() is None

    def test_dry_run_reports_a_block_without_failing(self, db, capsys, mocker,
                                                     monkeypatch):
        from src.budget import record_run
        monkeypatch.setenv("MIN_RUN_INTERVAL_MINUTES", "60")
        record_run("prior")
        self._ok(mocker)
        code, payload = _run_json(capsys, "refresh", "--dry-run")
        assert code == 0
        assert payload["allowed"] is False

    def test_stops_at_first_failing_stage(self, db, capsys, mocker):
        import subprocess
        outcomes = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
        ]
        run = mocker.patch("subprocess.run", side_effect=outcomes)
        code, payload = _run_json(capsys, "refresh")
        assert code == 1 and payload["ok"] is False
        assert payload["failed_stage"] == payload["stages"][-1]["stage"]
        assert run.call_count == 2       # later stages skipped

    def test_top_n_overrides_env_for_this_run_only(self, db, capsys, mocker,
                                                   monkeypatch):
        import os
        monkeypatch.setenv("AUTOPILOT_TOP_N", "10")
        run = self._ok(mocker)
        _run_json(capsys, "refresh", "--top-n", "3")
        assert run.call_args.kwargs["env"]["AUTOPILOT_TOP_N"] == "3"
        assert os.environ["AUTOPILOT_TOP_N"] == "10"   # process env untouched

    def test_reports_incremental_cost(self, db, capsys, mocker, tmp_path):
        """The run's own cost, not the day's total."""
        import subprocess
        log = tmp_path / "usage.log"
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        log.write_text(f"{now}|stage5|google/gemini-3.1-flash-lite|1000\n",
                       encoding="utf-8")

        def _spend(*a, **k):
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{now}|stage5|google/gemini-3.1-flash-lite|1000\n")
            return subprocess.CompletedProcess(args=[], returncode=0,
                                               stdout="", stderr="")
        mocker.patch("subprocess.run", side_effect=_spend)
        _, payload = _run_json(capsys, "refresh")
        assert payload["spent_usd_this_run"] > 0
        assert payload["spent_usd_today"] > payload["spent_usd_this_run"]

    def test_json_envelope_keys(self, db, capsys, mocker):
        self._ok(mocker)
        _, payload = _run_json(capsys, "refresh")
        assert set(payload) == {"verb", "ok", "stages", "failed_stage",
                                "spent_usd_this_run", "spent_usd_today", "page"}

    def test_chains_into_the_first_page(self, db, capsys, mocker):
        """C-5: the batch exists for the listings it produced — making the
        user issue a second command was the automation regression."""
        self._ok(mocker)
        job_id = _seed(db, title="Fresh Match", company="Acme", status="auto")
        _, payload = _run_json(capsys, "refresh")
        assert payload["page"] is not None
        assert [c["id"] for c in payload["page"]["listings"]] == [job_id]

    def test_chain_renders_cards_in_human_output(self, db, capsys, mocker):
        self._ok(mocker)
        _seed(db, title="Fresh Match", company="Acme", status="auto")
        _, out = _run(capsys, "refresh")
        assert "Fresh Match" in out and "Deep-dive" in out

    def test_no_next_suppresses_the_chain(self, db, capsys, mocker):
        self._ok(mocker)
        _seed(db, title="Fresh Match", company="Acme", status="auto")
        _, payload = _run_json(capsys, "refresh", "--no-next")
        assert payload["page"] is None

    def test_no_chain_when_a_stage_failed(self, db, capsys, mocker):
        """A half-run batch's "top 3" would be misleading."""
        import subprocess
        mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom"))
        _seed(db, title="Fresh Match", company="Acme", status="auto")
        _, payload = _run_json(capsys, "refresh")
        assert payload["ok"] is False and payload["page"] is None


class TestScriptShIsAWrapper:
    """R-1 applied to C-2: the stage sequence must exist in exactly one
    place, so script.sh delegates rather than repeating the chain."""

    def _script(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "script.sh").read_text()

    def test_delegates_to_the_refresh_verb(self):
        assert "src.cli refresh" in self._script()

    def test_does_not_repeat_the_stage_chain(self):
        body = self._script()
        for module in ("src.jobspy_ingest", "src.pipeline", "src.process_queue"):
            assert module not in body, (
                f"script.sh invokes {module} directly — the sequence belongs to "
                "cli.REFRESH_STAGES only."
            )

    def test_forwards_arguments(self):
        assert '"$@"' in self._script()


class TestHighSignalDefault:
    """`auto_queued` is backend state: raw Stage 5 output with no research and
    no large-model re-score. The CLI honors AUTOPILOT_POST_STAGE_5, the knob
    that has always governed this for the Slack digest."""

    def test_high_signal_hides_unenriched(self, db, capsys, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_POST_STAGE_5", "false")
        _seed(db, title="Raw", company="R", status="auto_queued")
        enriched = _seed(db, title="Enriched", company="E", status="auto")
        _, payload = _run_json(capsys, "next")
        assert [c["id"] for c in payload["listings"]] == [enriched]
        assert payload["tiers"] == ["auto"]

    def test_all_tiers_opts_back_in(self, db, capsys, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_POST_STAGE_5", "false")
        _seed(db, title="Raw", company="R", status="auto_queued")
        _seed(db, title="Enriched", company="E", status="auto")
        _, payload = _run_json(capsys, "next", "--all-tiers")
        assert payload["count"] == 2

    def test_funnel_mode_shows_everything(self, db, capsys, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_POST_STAGE_5", "true")
        _seed(db, title="Raw", company="R", status="auto_queued")
        _seed(db, title="Enriched", company="E", status="auto")
        assert _run_json(capsys, "next")[1]["count"] == 2


class TestCardCarriesFullContract:
    """The key user interface: every decision field on every card."""

    def test_next_card_has_tldr_and_skills(self, db, capsys):
        import json as _json
        _seed(db, title="ML Eng", company="Acme",
              job_summary="Build agentic AI systems for ops automation.",
              matching_skills=_json.dumps(["Agentic AI", "Python", "Eval"]),
              missing_skills=_json.dumps(["Finance domain"]))
        _, payload = _run_json(capsys, "next")
        card = payload["listings"][0]
        assert card["tldr"].startswith("Build agentic AI")
        assert card["skills_pct"] == 75 and card["skills_total"] == 4
        assert "Agentic AI" in card["matching_skills"]
        assert "Finance domain" in card["missing_skills"]

    def test_human_render_shows_every_field(self, db, capsys):
        import json as _json
        job_id = _seed(db, title="ML Eng", company="Acme", confidence=85,
                       location="Palo Alto, CA",
                       job_summary="Architect agentic AI solutions.",
                       matching_skills=_json.dumps(["Agentic AI"]),
                       missing_skills=_json.dumps(["Finance"]))
        db.set_distance_bucket(job_id, 1)
        _, out = _run(capsys, "next")
        for expected in ("YES:", "ML Eng", "Acme", "Palo Alto", "Local",
                         "85%", "TL;DR", "Skills: 50%", "Agentic AI", "Finance"):
            assert expected in out, f"card is missing {expected!r}"


class TestSurfacesTellOneStory:
    """status's numbers and next's behavior must agree — 'status says 43
    fresh, next shows 8 then a wall' was the audited failure."""

    @pytest.fixture(autouse=True)
    def _high_signal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTOPILOT_POST_STAGE_5", "false")
        monkeypatch.setenv("RUN_LOG_PATH", str(tmp_path / "rl"))
        monkeypatch.setenv("MODEL_USAGE_LOG_PATH", str(tmp_path / "ml"))

    def _mixed_queue(self, db):
        enriched = _seed(db, title="Ready", company="R", status="auto")
        for i in range(3):
            _seed(db, title=f"Raw {i}", company=f"C{i}", status="auto_queued")
        return enriched

    def test_status_splits_ready_from_awaiting(self, db, capsys):
        self._mixed_queue(db)
        _, payload = _run_json(capsys, "status")
        q = payload["queue"]
        assert q["ready"] == 1
        assert q["awaiting_enrichment"] == 3
        assert q["fresh"] == 4

    def test_status_human_line_shows_the_split(self, db, capsys):
        self._mixed_queue(db)
        _, out = _run(capsys, "status")
        assert "1 new" in out and "3 awaiting enrichment" in out
        assert "undecided from earlier" in out

    def test_status_steers_to_enrichment_not_max_age(self, db, capsys):
        for i in range(3):
            _seed(db, title=f"Raw {i}", company=f"C{i}", status="auto_queued")
        _, out = _run(capsys, "status")
        assert "enrich" in out.lower()

    def test_empty_page_names_the_enrichment_backlog(self, db, capsys):
        for i in range(3):
            _seed(db, title=f"Raw {i}", company=f"C{i}", status="auto_queued")
        _, payload = _run_json(capsys, "next")
        assert payload["count"] == 0
        assert payload["awaiting_enrichment"] == 3
        _, out = _run(capsys, "next")
        assert "awaiting autopilot enrichment" in out
        assert "--all-tiers" in out

    def test_hidden_stale_counts_only_the_shown_tiers(self, db, capsys):
        """A stale RAW row must not inflate the stale count of the enriched
        view — that mislabels an enrichment shortfall as staleness."""
        from datetime import datetime, timedelta, timezone
        raw = _seed(db, title="Old Raw", company="OR", status="auto_queued")
        when = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        db.conn.execute("UPDATE listings SET date_ingested=? WHERE id=?",
                        (when, raw))
        db.conn.commit()
        _, payload = _run_json(capsys, "next")
        assert payload["hidden_stale"] == 0


class TestEnrichmentCapacityIsVisible:
    """Autopilot is what puts cards in Slack and rows in the enriched feed.
    When its daily cap is spent, a refresh still costs money and still
    ingests, but produces no new cards anywhere — which reads as a broken
    Slack integration unless the surface says so. It did, once, for real."""

    def test_status_reports_capacity(self, db, capsys, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_TOP_N", "10")
        _, payload = _run_json(capsys, "status")
        q = payload["queue"]
        assert q["enrichment_cap"] == 10
        assert q["enriched_today"] == 0
        assert q["enrichment_remaining"] == 10

    def test_human_line_warns_when_capped(self, db, capsys, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_TOP_N", "0")
        _, out = _run(capsys, "status")
        assert "cap reached" in out
        assert "no new cards" in out

    def test_human_line_shows_headroom_when_not_capped(self, db, capsys, monkeypatch):
        monkeypatch.setenv("AUTOPILOT_TOP_N", "10")
        _, out = _run(capsys, "status")
        assert "0/10 used today" in out and "10 left" in out


class TestSweepVerb:
    """`cli sweep` is sweeper's dispatch with ✏️ deferred — the JSON contract
    the skill reads, and the promise that a failed sweep is reported, not
    raised."""

    def _counts(self, **kw):
        base = {"passed": 1, "saved": 2, "skipped": 3, "tailored": 0,
                "deferred_tailors": []}
        base.update(kw)
        return base

    def test_json_contract(self, db, capsys, mocker):
        mocker.patch("src.sweeper.sweep", return_value=self._counts())
        _, payload = _run_json(capsys, "sweep")
        assert set(payload) == {"verb", "ok", "passed", "saved", "skipped",
                                "pending_tailors"}
        assert payload["ok"] and payload["passed"] == 1

    def test_defers_and_names_the_free_route(self, db, capsys, mocker):
        mocker.patch("src.sweeper.sweep",
                     return_value=self._counts(deferred_tailors=["abc123"]))
        _, out = _run(capsys, "sweep")
        assert "tailor abc123" in out
        assert "no API cost" in out

    def test_failure_is_reported_not_raised(self, db, capsys, mocker):
        mocker.patch("src.sweeper.sweep", side_effect=RuntimeError("slack down"))
        code, payload = _run_json(capsys, "sweep")
        assert code == 1
        assert payload["error"] == "sweep_failed" and "slack down" in payload["detail"]
