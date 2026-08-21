"""Tests for the subscription-billed transport (src/claude_cli.py).

Every caller has a metered fallback, so the contract that matters is: this
module never raises. A missing binary, a crash, a timeout, or a malformed
envelope must all degrade to ok=False so the caller can pay for the work
instead of losing the listing.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
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

    def test_tools_are_disabled_so_the_call_is_one_completion(self):
        """A scorer that can call a tool can take a second turn — twice the
        tokens for one verdict, and the judge reads whatever is around it."""
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope("{}"))) as sp:
            claude_cli.run("hi")
        argv = sp.call_args.args[0]
        assert argv[argv.index("--tools") + 1] == ""
        assert "--no-session-persistence" in argv

    def test_runs_outside_the_repo(self):
        """The subprocess inherits its cwd, and inside the checkout that loads
        this repo's CLAUDE.md into a judgement about job listings."""
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope("{}"))) as sp:
            claude_cli.run("hi")
        cwd = sp.call_args.kwargs.get("cwd")
        assert cwd
        repo = Path(__file__).resolve().parent.parent
        assert Path(cwd).resolve() != repo
        assert repo not in Path(cwd).resolve().parents

    def test_parses_num_turns(self):
        env = json.dumps({"result": "{}", "usage": {}, "num_turns": 3})
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(env)):
            assert claude_cli.run("hi").turns == 3

    def test_defaults_to_one_turn_when_the_envelope_omits_it(self):
        with patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope("{}"))):
            assert claude_cli.run("hi").turns == 1

    def test_warns_when_the_call_took_more_than_one_turn(self, caplog):
        env = json.dumps({"result": "{}", "usage": {}, "num_turns": 2})
        with caplog.at_level(logging.WARNING, logger="src.claude_cli"), \
             patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(env)):
            claude_cli.run("hi")
        assert [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_single_turn_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.claude_cli"), \
             patch("src.claude_cli.is_available", return_value=True), \
             patch("subprocess.run", return_value=_proc(_envelope("{}"))):
            claude_cli.run("hi")
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


class TestNeverRaises:
    @pytest.mark.parametrize("failure,marker", [
        ({"return_value": _proc(rc=1, stderr="boom")}, "rc=1"),
        ({"return_value": _proc("not json")}, "non-JSON"),
        ({"side_effect": subprocess.TimeoutExpired("claude", 300)}, "timed out"),
        ({"side_effect": OSError("no such file")}, "could not run"),
        # A refused invocation exits 0 with a well-formed envelope saying so
        # (`--bare` does this). Untreated it parses as an empty success.
        ({"return_value": _proc(json.dumps(
            {"is_error": True, "result": "Error: refused", "usage": {}}))},
         "is_error"),
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
