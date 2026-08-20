"""Tests for the Antigravity transport (src/gemini_cli.py).

Same contract as `claude_cli`: every caller has a metered fallback, so this
module never raises — a missing binary, a crash, a timeout, or a malformed
envelope must all degrade to ok=False.

Two of these tests exist because the invocation was got wrong first:
`agy`'s `-p` takes the prompt as its *value* (unlike `claude -p`, a boolean
flag) and there is no stdin path. A malformed call does not error — it sends
the wrong string as the prompt and returns a confident answer to a question
nobody asked, which is why it is pinned here rather than left to review.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src import gemini_cli


def _envelope(response: str = "{}", status: str = "SUCCESS", **kw) -> str:
    env = {
        "conversation_id": "abc", "status": status, "response": response,
        "duration_seconds": 8.0, "num_turns": 1,
        "usage": {"input_tokens": 100, "output_tokens": 50,
                  "thinking_tokens": 10, "cache_read_tokens": 0},
    }
    env.update(kw)
    return json.dumps(env)


def _proc(stdout: str = "", rc: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=rc, stdout=stdout, stderr=stderr)


class TestRun:
    def test_parses_envelope_and_response(self):
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope('{"a": 1}'))):
            r = gemini_cli.run("hi")
        assert r.ok and r.text == '{"a": 1}'
        assert r.input_tokens == 100 and r.output_tokens == 50

    def test_cost_is_always_zero_because_subscription_billed(self):
        """There is no per-call charge to report, and reporting one would
        invite a caller to add it to the metered log."""
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope())):
            assert gemini_cli.run("hi").cost_usd == 0.0

    def test_counts_cache_reads_as_input(self):
        env = _envelope(usage={"input_tokens": 10, "output_tokens": 5,
                               "cache_read_tokens": 8093})
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(env)):
            assert gemini_cli.run("hi").input_tokens == 8103

    def test_non_success_status_is_a_failure(self):
        """Observed live: concurrent calls return status=CANCELED / ERROR with
        rc=0. Trusting the return code alone would treat a dropped batch as an
        empty-but-valid result — the silent-coverage-loss failure of V-29."""
        for status in ("CANCELED", "ERROR"):
            with patch("src.gemini_cli.is_available", return_value=True), \
                 patch("subprocess.run", return_value=_proc(_envelope(status=status))):
                r = gemini_cli.run("hi")
            assert r.ok is False and status in r.error


class TestInvocation:
    def test_prompt_is_the_value_of_print_not_stdin(self):
        """`agy --print <prompt>`: the flag *takes* the prompt. Passing it the
        way `claude_cli` does sends the next flag name as the prompt instead,
        and piping is rejected outright — there is no stdin path.
        """
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope())) as sp:
            gemini_cli.run("THE PROMPT", model="gemini-3.7-flash-low")
        argv = sp.call_args.args[0]
        assert argv[0] == "agy"
        assert argv[argv.index("--print") + 1] == "THE PROMPT"
        assert "input" not in sp.call_args.kwargs
        assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-low"

    def test_json_schema_is_passed_and_its_parse_is_preferred(self):
        """The CLI validates against the schema and hands back
        `structured_output` already parsed. Preferring it removes the
        fence-stripping / JSONDecodeError class that truncated rank_stage5 —
        note the raw `response` here carries extra keys the schema excludes.
        """
        schema = {"type": "object",
                  "properties": {"verdict": {"type": "string"}},
                  "required": ["verdict"]}
        env = _envelope('{"verdict":"YES","toolSummary":"Task finished"}',
                        structured_output={"verdict": "YES"})
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(env)) as sp:
            r = gemini_cli.run("hi", json_schema=schema)
        argv = sp.call_args.args[0]
        assert json.loads(argv[argv.index("--json-schema") + 1]) == schema
        assert json.loads(r.text) == {"verdict": "YES"}

    def test_without_a_schema_the_raw_response_is_used(self):
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope("plain text"))) as sp:
            r = gemini_cli.run("hi")
        assert "--json-schema" not in sp.call_args.args[0]
        assert r.text == "plain text"

    def test_session_model_default_and_override(self, monkeypatch):
        monkeypatch.delenv("AGY_CLI_MODEL", raising=False)
        assert gemini_cli.session_model() == "gemini-3.7-flash-medium"
        monkeypatch.setenv("AGY_CLI_MODEL", "gemini-3.1-pro-high")
        assert gemini_cli.session_model() == "gemini-3.1-pro-high"


class TestNeverRaises:
    @pytest.mark.parametrize("failure,marker", [
        ({"return_value": _proc(rc=1, stderr="boom")}, "rc=1"),
        ({"return_value": _proc("not json")}, "non-JSON"),
        ({"side_effect": subprocess.TimeoutExpired("agy", 300)}, "timed out"),
        ({"side_effect": OSError("no such file")}, "could not run"),
    ])
    def test_failures_degrade_to_not_ok(self, failure, marker):
        with patch("src.gemini_cli.is_available", return_value=True), \
             patch("subprocess.run", **failure):
            r = gemini_cli.run("hi")
        assert r.ok is False and marker in r.error

    def test_missing_binary_is_reported_not_raised(self):
        with patch("src.gemini_cli.is_available", return_value=False), \
             patch("subprocess.run") as sp:
            r = gemini_cli.run("hi")
        assert r.ok is False and "PATH" in r.error
        sp.assert_not_called()
