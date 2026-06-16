"""Unit tests for src/mismatch_gate.py — Fix 2a hybrid title↔body gate."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.mismatch_gate import (
    _normalize_tokens,
    _substring_hit,
    _url_host_blob,
    check_mismatch,
)


class TestNormalizeTokens:
    def test_drops_short_tokens(self):
        assert _normalize_tokens("AI Co") == set()

    def test_drops_stopwords(self):
        assert _normalize_tokens("OpenAI Inc") == {"openai"}

    def test_strips_punctuation(self):
        assert _normalize_tokens("M&M's, Inc.") == set()  # all short / stop

    def test_keeps_multiword_brands(self):
        tokens = _normalize_tokens("Intuitive Surgical Inc")
        assert tokens == {"intuitive", "surgical"}

    def test_empty_input(self):
        assert _normalize_tokens("") == set()


class TestUrlHostBlob:
    def test_strips_www_and_tld(self):
        assert _url_host_blob("https://www.protege.health/jobs/1") == "protege"

    def test_keeps_subdomain(self):
        assert _url_host_blob("https://careers.openai.com/x") == "careers.openai"

    def test_handles_bad_url(self):
        assert _url_host_blob("not-a-url") == ""

    def test_handles_empty(self):
        assert _url_host_blob("") == ""


class TestSubstringHit:
    def test_hit_in_summary(self):
        assert _substring_hit(
            "Protege Inc", "Protege is a high-growth healthcare AI company.", "",
        ) is True

    def test_hit_in_host(self):
        assert _substring_hit(
            "Protege Inc", "Our company does AI.", "https://protege.com/jobs/1",
        ) is True

    def test_miss_in_both(self):
        # Handshake body talks only about OpenAI — body of evidence B
        assert _substring_hit(
            "Handshake",
            "This role sits within OpenAI's Forward Deployed Engineering team.",
            "https://thehomebase.ai/jobs/x",
        ) is False

    def test_empty_company_falls_open(self):
        """No significant tokens → permissive (substring hit returns True)."""
        assert _substring_hit("AI", "anything", "") is True


class TestCheckMismatchModes:
    def _client(self, matches: bool, observed: str = ""):
        client = MagicMock()
        msg = MagicMock()
        msg.message.content = json.dumps({"matches": matches, "actual_company": observed})
        resp = MagicMock()
        resp.choices = [msg]
        client.chat.completions.create.return_value = resp
        return client

    def test_hybrid_substring_pass(self, monkeypatch):
        monkeypatch.delenv("MISMATCH_GATE_MODE", raising=False)
        drop, gate, observed = check_mismatch(
            client=None, model="x", listing_id="i", source="s",
            anchor_company="Protege Inc",
            job_summary="Protege builds healthcare AI.",
            url="",
        )
        assert drop is False
        assert gate == ""

    def test_hybrid_substring_miss_then_llm_drop(self, monkeypatch):
        monkeypatch.delenv("MISMATCH_GATE_MODE", raising=False)
        client = self._client(matches=False, observed="OpenAI")
        drop, gate, observed = check_mismatch(
            client=client, model="x", listing_id="i", source="s",
            anchor_company="Handshake",
            job_summary="This role sits within OpenAI's FDE team.",
            url="https://thehomebase.ai/jobs/x",
        )
        assert drop is True
        assert gate == "llm"
        assert observed == "OpenAI"

    def test_hybrid_substring_miss_then_llm_keep(self, monkeypatch):
        monkeypatch.delenv("MISMATCH_GATE_MODE", raising=False)
        client = self._client(matches=True)
        drop, gate, observed = check_mismatch(
            client=client, model="x", listing_id="i", source="s",
            anchor_company="StealthCo",
            job_summary="We are a high-growth fintech you've never heard of.",
            url="https://careers.unrelated.io/x",
        )
        assert drop is False
        assert gate == ""

    def test_substring_only_mode_drops_without_llm(self, monkeypatch):
        monkeypatch.setenv("MISMATCH_GATE_MODE", "substring_only")
        client = self._client(matches=False)  # would fail if called
        drop, gate, observed = check_mismatch(
            client=client, model="x", listing_id="i", source="s",
            anchor_company="Handshake",
            job_summary="OpenAI FDE role.", url="https://thehomebase.ai/x",
        )
        assert drop is True
        assert gate == "substring"
        # LLM must NOT have been called in substring_only mode
        client.chat.completions.create.assert_not_called()

    def test_llm_only_mode_skips_substring(self, monkeypatch):
        monkeypatch.setenv("MISMATCH_GATE_MODE", "llm_only")
        client = self._client(matches=False, observed="OpenAI")
        drop, gate, observed = check_mismatch(
            client=client, model="x", listing_id="i", source="s",
            anchor_company="Protege Inc",
            job_summary="Protege healthcare AI.", url="https://protege.com/",
        )
        assert drop is True
        assert gate == "llm"
        client.chat.completions.create.assert_called_once()

    def test_off_mode_never_drops(self, monkeypatch):
        monkeypatch.setenv("MISMATCH_GATE_MODE", "off")
        client = self._client(matches=False)
        drop, gate, observed = check_mismatch(
            client=client, model="x", listing_id="i", source="s",
            anchor_company="anything",
            job_summary="anything else", url="https://wrong.com/",
        )
        assert drop is False
        assert gate == ""
        client.chat.completions.create.assert_not_called()


class TestCheckMismatchFailsOpen:
    def test_llm_exception_fails_open(self, monkeypatch):
        monkeypatch.delenv("MISMATCH_GATE_MODE", raising=False)
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("boom")
        drop, gate, observed = check_mismatch(
            client=client, model="x", listing_id="i", source="s",
            anchor_company="Handshake",
            job_summary="OpenAI FDE role.", url="https://thehomebase.ai/x",
        )
        assert drop is False  # fails open on LLM error
