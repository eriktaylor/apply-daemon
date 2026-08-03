"""Unit tests for src/ranking.py — M-1 shared ranking utility."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.ranking import (
    RankCandidate,
    _reorder_by_ids,
    rank_candidates,
    rank_listwise,
    ranking_mode,
)


def _cands(*ids):
    return [RankCandidate(id=i, title=f"Job {i}") for i in ids]


class TestRankingMode:
    def test_defaults_off(self, monkeypatch):
        monkeypatch.delenv("RANKING_MODE", raising=False)
        monkeypatch.delenv("RANKING_MODE_TRACK_B", raising=False)
        assert ranking_mode("TRACK_B") == "off"

    def test_global_default_applies(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE", "listwise")
        monkeypatch.delenv("RANKING_MODE_TRACK_B", raising=False)
        assert ranking_mode("TRACK_B") == "listwise"

    def test_surface_override_wins(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE", "listwise")
        monkeypatch.setenv("RANKING_MODE_STAGE5", "off")
        # Global says listwise, but Stage 5 is explicitly held off.
        assert ranking_mode("STAGE5") == "off"
        assert ranking_mode("TRACK_B") == "listwise"

    def test_invalid_mode_falls_back_off(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE_TRACK_B", "bogus")
        assert ranking_mode("TRACK_B") == "off"

    def test_blank_override_falls_through_to_global(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE", "listwise")
        monkeypatch.setenv("RANKING_MODE_TRACK_B", "")
        assert ranking_mode("TRACK_B") == "listwise"


class TestReorderByIds:
    def test_reorders(self):
        cands = _cands("a", "b", "c")
        out = _reorder_by_ids(cands, ["c", "a", "b"])
        assert [c.id for c in out] == ["c", "a", "b"]

    def test_omitted_id_appended_in_original_order(self):
        cands = _cands("a", "b", "c")
        out = _reorder_by_ids(cands, ["c"])  # model dropped a, b
        assert [c.id for c in out] == ["c", "a", "b"]

    def test_duplicate_and_unknown_ids_ignored(self):
        cands = _cands("a", "b")
        out = _reorder_by_ids(cands, ["b", "b", "zzz", "a"])
        assert [c.id for c in out] == ["b", "a"]

    def test_output_is_always_a_permutation(self):
        cands = _cands("a", "b", "c")
        out = _reorder_by_ids(cands, [])
        assert sorted(c.id for c in out) == ["a", "b", "c"]


class TestRankListwise:
    def test_single_candidate_short_circuits(self):
        client = MagicMock()
        out = rank_listwise(client, "m", _cands("a"))
        assert [c.id for c in out] == ["a"]
        client.chat.completions.create.assert_not_called()

    def test_none_client_returns_input(self):
        out = rank_listwise(None, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["a", "b"]

    def test_parses_order_from_response(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = (
            json.dumps({"order": ["b", "a"]})
        )
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["b", "a"]

    def test_fails_open_on_bad_json(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = "not json"
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["a", "b"]

    def test_fails_open_on_exception(self):
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["a", "b"]

    def test_uses_rank_model_slot_override(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_RANK_MODEL", "special/ranker")
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = (
            json.dumps({"order": ["a", "b"]})
        )
        rank_listwise(client, "fallback/model", _cands("a", "b"))
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "special/ranker"


class TestRankCandidates:
    def test_off_returns_input_unchanged(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE_TRACK_B", "off")
        client = MagicMock()
        cands = _cands("a", "b")
        out = rank_candidates(
            client=client, model="m", surface="TRACK_B", candidates=cands,
        )
        assert out is cands
        client.chat.completions.create.assert_not_called()

    def test_listwise_dispatches(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE_TRACK_B", "listwise")
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = (
            json.dumps({"order": ["b", "a"]})
        )
        out = rank_candidates(
            client=client, model="m", surface="TRACK_B", candidates=_cands("a", "b"),
        )
        assert [c.id for c in out] == ["b", "a"]

    def test_swiss_not_implemented_keeps_input(self, monkeypatch):
        monkeypatch.setenv("RANKING_MODE_STAGE5", "swiss")
        client = MagicMock()
        out = rank_candidates(
            client=client, model="m", surface="STAGE5", candidates=_cands("a", "b"),
        )
        assert [c.id for c in out] == ["a", "b"]
        client.chat.completions.create.assert_not_called()


def test_output_budget_scales_with_candidates():
    """Regression: a fixed max_tokens=500 truncated the response on large
    pools, and a truncated JSON object doesn't degrade — it fails to parse
    and the entire ranking is discarded while the call still bills."""
    from src.ranking import RankCandidate, rank_listwise

    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"order": []}'))],
    )
    n = 100
    cands = [RankCandidate(id=f"c{i}", title="t", company="c") for i in range(n)]
    rank_listwise(client, "m", cands)
    budget = client.chat.completions.create.call_args.kwargs["max_tokens"]
    assert budget >= 24 * n  # room for one id per candidate
