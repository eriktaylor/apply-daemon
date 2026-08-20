"""Tests for the one-time post-research backfill (R-4) — and for the line it
must not cross.

Two things are being guarded. The obvious one: the backfill copies the right
value onto the right row, and a dry run writes nothing. The load-bearing one:
``eval.listwise_compare.load_gold`` reads ``auto_assets.json``, so the eval
harness was never affected by the missing write-back — and if a backfill ever
made the DB the gold source, every eval number would silently re-baseline
against whatever the pipeline had most recently stored (V-36).
"""

from __future__ import annotations

import json

import pytest

from src.backfill_post_research import (
    _connect_readonly,
    apply,
    plan,
    scan_assets,
)
from src.db import Database


def _seed(db: Database, job_id: str, *, verdict="YES", confidence=95,
          status="auto") -> str:
    now = "2026-08-01T00:00:00+00:00"
    db.conn.execute(
        "INSERT INTO listings (id, source, title, company, verdict, confidence, "
        "date_ingested, pipeline_status, updated_at) "
        "VALUES (?, 'test', 'Applied AI Engineer', 'Acme', ?, ?, ?, ?, ?)",
        (job_id, verdict, confidence, now, status, now),
    )
    db.conn.commit()
    return job_id


def _asset(output_dir, job_id: str, **payload):
    folder = output_dir / f"Acme_Applied_AI_Engineer_{job_id[:8]}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "auto_assets.json").write_text(json.dumps(payload), encoding="utf-8")
    return folder


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "test.db")
    yield db
    db.close()


class TestScan:
    def test_reads_the_rescore(self, tmp_path):
        _asset(tmp_path, "abc12345-0000", post_research_verdict="MAYBE",
               post_research_confidence=58)
        found, unreadable = scan_assets(tmp_path)
        assert unreadable == 0
        assert list(found)[0].endswith("abc12345")
        assert list(found.values())[0]["confidence"] == 58

    def test_malformed_json_is_counted_not_raised(self, tmp_path):
        folder = tmp_path / "Acme_x_abc12345"
        folder.mkdir()
        (folder / "auto_assets.json").write_text("{not json", encoding="utf-8")
        found, unreadable = scan_assets(tmp_path)
        assert found == {} and unreadable == 1

    def test_an_envelope_with_no_rescore_is_not_a_change(self, tmp_path):
        _asset(tmp_path, "abc12345-0000", match_analysis="only prose")
        found, unreadable = scan_assets(tmp_path)
        assert found == {} and unreadable == 1

    def test_missing_output_dir_is_empty_not_fatal(self, tmp_path):
        assert scan_assets(tmp_path / "nope") == ({}, 0)


class TestPlan:
    def test_pairs_a_row_with_its_folder(self, store, tmp_path):
        job_id = _seed(store, "abc12345-0000-0000")
        _asset(tmp_path, job_id, post_research_verdict="MAYBE",
               post_research_confidence=58)
        result = plan(store.conn, tmp_path)
        assert len(result["changes"]) == 1
        change = result["changes"][0]
        assert (change.verdict, change.confidence, change.delta) == ("MAYBE", 58, -37)
        assert (change.stage5_verdict, change.stage5_confidence) == ("YES", 95)

    def test_rows_without_assets_are_counted_not_changed(self, store, tmp_path):
        _seed(store, "abc12345-0000-0000")
        result = plan(store.conn, tmp_path)
        assert result["changes"] == [] and result["rows_without_assets"] == 1

    def test_assets_without_a_row_are_counted(self, store, tmp_path):
        _asset(tmp_path, "deadbeef-0000-0000", post_research_verdict="NO",
               post_research_confidence=10)
        result = plan(store.conn, tmp_path)
        assert result["orphan_assets"] == 1 and result["changes"] == []

    def test_a_row_already_current_is_skipped(self, store, tmp_path):
        job_id = _seed(store, "abc12345-0000-0000")
        store.set_post_research_score(job_id, "MAYBE", 58)
        _asset(tmp_path, job_id, post_research_verdict="MAYBE",
               post_research_confidence=58)
        result = plan(store.conn, tmp_path)
        assert result["already_current"] == 1 and result["changes"] == []

    def test_a_no_verdict_is_flagged_as_leaving_the_feed(self, store, tmp_path):
        job_id = _seed(store, "abc12345-0000-0000")
        _asset(tmp_path, job_id, post_research_verdict="NO",
               post_research_confidence=15)
        change = plan(store.conn, tmp_path)["changes"][0]
        assert change.leaves_the_feed is True


class TestApplyAndReadOnly:
    def test_apply_writes_both_columns_and_leaves_stage5_alone(self, store, tmp_path):
        job_id = _seed(store, "abc12345-0000-0000")
        _asset(tmp_path, job_id, post_research_verdict="MAYBE",
               post_research_confidence=58)
        written = apply(store, plan(store.conn, tmp_path)["changes"])
        assert written == 1
        row = store.get_listing_by_id(job_id)
        assert (row["post_research_verdict"], row["post_research_confidence"]) == (
            "MAYBE", 58)
        assert (row["verdict"], row["confidence"]) == ("YES", 95)
        assert row["post_research_at"]

    def test_the_backfilled_row_reorders_and_relabels_the_feed(self, store, tmp_path):
        """The point of the write: the feed ranks and labels by the re-score."""
        from src.listing_card import build_card, format_verdict_line

        high = _seed(store, "aaaa1111-0000-0000", confidence=95)
        _seed(store, "bbbb2222-0000-0000", confidence=90)
        _asset(tmp_path, high, post_research_verdict="MAYBE",
               post_research_confidence=40)
        apply(store, plan(store.conn, tmp_path)["changes"])

        rows = store.get_review_queue(limit=5)
        assert [r["id"] for r in rows] == ["bbbb2222-0000-0000", high]
        card = build_card(next(r for r in rows if r["id"] == high))
        assert format_verdict_line(card) == "MAYBE 40% (post-research · was YES 95%, -55)"

    def test_the_dry_run_connection_cannot_write(self, store, tmp_path):
        import sqlite3
        job_id = _seed(store, "abc12345-0000-0000")
        conn = _connect_readonly(store.db_path)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE listings SET confidence = 1 WHERE id = ?", (job_id,))
        finally:
            conn.close()

    def test_plan_works_before_the_migration_has_run(self, store, tmp_path):
        """The dry run opens read-only, so the columns may not exist yet."""
        store.conn.execute("ALTER TABLE listings DROP COLUMN post_research_verdict")
        store.conn.execute("ALTER TABLE listings DROP COLUMN post_research_confidence")
        store.conn.commit()
        job_id = _seed(store, "abc12345-0000-0000")
        _asset(tmp_path, job_id, post_research_verdict="MAYBE",
               post_research_confidence=58)
        result = plan(store.conn, tmp_path)
        assert result["has_columns"] is False and len(result["changes"]) == 1


class TestGoldStandardStaysInTheJson:
    """V-36 — the eval harness reads ``auto_assets.json``, not the DB.

    ``load_gold`` was unaffected by the missing write-back precisely because
    it never looked at the row. If a backfill turned the DB into the gold
    source, every eval percentage would re-baseline against pipeline state
    and stop being comparable to any number recorded before it.
    """

    def test_load_gold_reads_the_json(self, tmp_path, monkeypatch):
        from eval.listwise_compare import load_gold

        _asset(tmp_path / "output", "abc12345-0000-0000",
               post_research_verdict="maybe", post_research_confidence=58)
        monkeypatch.chdir(tmp_path)
        assert load_gold() == {"abc12345": "MAYBE"}

    def test_load_gold_never_touches_the_database(self):
        import inspect

        from eval.listwise_compare import load_gold

        source = inspect.getsource(load_gold)
        assert "auto_assets.json" in source
        for forbidden in ("Database", "listings", "post_research_confidence"):
            assert forbidden not in source, (
                f"load_gold() now references {forbidden!r} — the gold standard "
                "must come from the archived JSON, never from the row a "
                "backfill wrote."
            )
