"""Unit tests for src/ranking.py — M-1 shared ranking utility."""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

from src.ranking import (
    RankCandidate,
    _parse_first_json_object,
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


class TestListwiseResponseParsing:
    """The ranking call gets prose it did not ask for (I-13, 2026-08-21).

    ``response_format={"type": "json_object"}`` is a request, not a guarantee:
    the model returned a valid object followed by a sentence of commentary,
    ``json.loads`` raised ``Extra data``, and that run's whole top-10 fell
    back to input order with nothing but a Traceback to say so.
    """

    @staticmethod
    def _client(content):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = content
        return client

    def test_trailing_prose_after_object_still_ranks(self):
        client = self._client(
            json.dumps({"order": ["b", "a"]})
            + "\n\nI ranked b first because it is the closer seniority match."
        )
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["b", "a"]

    def test_fenced_json_still_ranks(self):
        client = self._client("```json\n" + json.dumps({"order": ["b", "a"]}) + "\n```")
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["b", "a"]

    def test_preamble_before_object_still_ranks(self):
        client = self._client("Here is the ranking:\n" + json.dumps({"order": ["b", "a"]}))
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["b", "a"]

    def test_happy_path_unchanged(self):
        client = self._client(json.dumps({"order": ["b", "a"]}))
        out = rank_listwise(client, "m", _cands("a", "b"))
        assert [c.id for c in out] == ["b", "a"]

    def test_truncated_json_warns_once_and_keeps_input_order(self, caplog):
        caplog.set_level(logging.WARNING, logger="src.ranking")
        client = self._client('{"order": ["synthetic-listing-b", "synthe')
        out = rank_listwise(client, "m", _cands("a", "b"), surface="STAGE5")
        assert [c.id for c in out] == ["a", "b"]

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        rec = warnings[0]
        # A traceback at WARNING is what made the last two failures unreadable.
        assert rec.exc_info is None
        message = rec.getMessage()
        assert "rank_stage5" in message          # which stage
        assert "2 candidate(s)" in message       # how many listings lost their ranking
        assert "JSONDecodeError" in message      # why
        # Never the response body: it is model output and may quote listings.
        assert "synthetic-listing-b" not in message

    def test_missing_order_key_warns_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="src.ranking")
        client = self._client(json.dumps({"ranking": ["b", "a"]}))
        out = rank_listwise(client, "m", _cands("a", "b"), surface="TRACK_B")
        assert [c.id for c in out] == ["a", "b"]

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "no 'order' list" in warnings[0].getMessage()
        assert warnings[0].exc_info is None

    def test_traceback_still_reachable_at_debug(self, caplog):
        caplog.set_level(logging.DEBUG, logger="src.ranking")
        rank_listwise(self._client("not json at all"), "m", _cands("a", "b"))
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(r.exc_info for r in debugs)


class TestParseFirstJsonObject:
    def test_ignores_everything_after_the_first_object(self):
        assert _parse_first_json_object('{"a": {"b": 1}} trailing text') == {"a": {"b": 1}}

    def test_rejects_a_non_object_payload(self):
        with pytest.raises(ValueError):
            _parse_first_json_object("[1, 2, 3]")

    def test_rejects_a_response_with_no_object(self):
        with pytest.raises(ValueError):
            _parse_first_json_object("sorry, I can't rank these")
