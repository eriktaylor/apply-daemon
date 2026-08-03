"""Tests for the subscription-billed transport (src/claude_cli.py).

Every caller has a metered fallback, so the contract that matters is: this
module never raises. A missing binary, a crash, a timeout, or a malformed
envelope must all degrade to ok=False so the caller can pay for the work
instead of losing the listing.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from src import claude_cli


def _envelope(result: str, **usage) -> str:
    u = {"input_tokens": 100, "output_tokens": 50}
    u.update(usage)
    return json.dumps({"result": result, "usage": u, "total_cost_usd": 0.012})


def _proc(stdout: str = "", rc: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=rc, stdout=stdout, stderr=stderr)


class TestRun:
    def test_parses_envelope_and_result(self):
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope('{"a": 1}'))):
            r = claude_cli.run("hi")
        assert r.ok and r.text == '{"a": 1}'
        assert r.input_tokens == 100 and r.output_tokens == 50
        assert r.cost_usd == pytest.approx(0.012)

    def test_counts_cache_tokens_as_input(self):
        """Counting only input_tokens understates a cached call by orders of
        magnitude — cache reads are real input the model processed."""
        env = _envelope("{}", input_tokens=10,
                        cache_read_input_tokens=9000,
                        cache_creation_input_tokens=500)
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(env)):
            r = claude_cli.run("hi")
        assert r.input_tokens == 9510

    def test_strips_a_code_fence(self):
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run",
                   return_value=_proc(_envelope('```json\n{"a": 1}\n```'))):
            assert claude_cli.run("hi").text == '{"a": 1}'

    def test_prompt_goes_on_stdin_not_argv(self):
        """Passing the prompt as an argument fails without a TTY, and these
        prompts are long enough to risk ARG_MAX."""
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope("{}"))) as sp:
            claude_cli.run("THE PROMPT", model="haiku")
        argv = sp.call_args.args[0]
        assert "THE PROMPT" not in argv
        assert sp.call_args.kwargs["input"] == "THE PROMPT"
        assert argv[:2] == ["claude", "-p"] and "haiku" in argv


class TestNeverRaises:
    @pytest.mark.parametrize("failure,marker", [
        ({"return_value": _proc(rc=1, stderr="boom")}, "rc=1"),
        ({"return_value": _proc("not json")}, "non-JSON"),
        ({"side_effect": subprocess.TimeoutExpired("claude", 300)}, "timed out"),
        ({"side_effect": OSError("no such file")}, "could not run"),
    ])
    def test_failures_degrade_to_not_ok(self, failure, marker):
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", **failure):
            r = claude_cli.run("hi")
        assert r.ok is False and marker in r.error

    def test_missing_binary_is_reported_not_raised(self):
        with patch("src.claude_cli.is_available", return_value=False), \
             patch("subprocess.run") as sp:
            r = claude_cli.run("hi")
        assert r.ok is False and "PATH" in r.error
        sp.assert_not_called()
