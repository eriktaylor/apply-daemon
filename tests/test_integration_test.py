"""Tests for the user-initiated setup evaluation (src/integration_test.py).

The smoke checks are mostly thin wrappers over filesystem and env-var
state; these tests pin the offline behaviour (no real Slack /
OpenRouter / Gmail traffic) and the exit-code policy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src import integration_test as it


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Run each test in a clean cwd with a scrubbed env.

    Strips every variable the integration test reads so the tests do
    not pick up the developer's real .env values.
    """
    monkeypatch.chdir(tmp_path)
    for var in (
        "SLACK_BOT_TOKEN",
        "SLACK_CHANNEL_ID",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "GMAIL_ADDRESS",
        "GMAIL_APP_PASSWORD",
        "IPROYAL_USERNAME",
        "IPROYAL_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


class TestResumeCheck:
    def test_skips_when_my_profile_missing(self):
        result = it._check_resume()
        assert result.status == it.SKIP
        assert "my_profile/" in result.detail

    def test_fails_when_my_profile_present_but_no_resume(self):
        Path("my_profile").mkdir()
        result = it._check_resume()
        assert result.status == it.FAIL

    def test_passes_when_resume_exists(self):
        Path("my_profile").mkdir()
        (Path("my_profile") / "base_resume.md").write_text("hi")
        result = it._check_resume()
        assert result.status == it.PASS
        assert "base_resume.md" in result.detail


class TestRepoLayout:
    def test_fails_when_both_missing(self):
        result = it._check_repo_layout()
        assert result.status == it.FAIL
        assert "my_profile" in result.detail
        assert ".env" in result.detail

    def test_passes_when_both_present(self):
        Path("my_profile").mkdir()
        Path(".env").write_text("")
        result = it._check_repo_layout()
        assert result.status == it.PASS


class TestSlackCheck:
    def test_fails_without_credentials(self):
        result = it._check_slack(do_network=False)
        assert result.status == it.FAIL

    def test_warns_with_credentials_offline(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
        result = it._check_slack(do_network=False)
        assert result.status == it.WARN
        assert "auth.test" in result.detail


class TestOpenRouterCheck:
    def test_fails_without_key(self):
        result = it._check_openrouter(do_network=False, do_llm=False)
        assert result.status == it.FAIL

    def test_warns_with_key_when_llm_disabled(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        result = it._check_openrouter(do_network=True, do_llm=False)
        assert result.status == it.WARN


class TestProxyCheck:
    def test_skips_without_credentials(self):
        result = it._check_proxy()
        assert result.status == it.SKIP

    def test_passes_with_credentials(self, monkeypatch):
        monkeypatch.setenv("IPROYAL_USERNAME", "u")
        monkeypatch.setenv("IPROYAL_PASSWORD", "p")
        result = it._check_proxy()
        assert result.status == it.PASS
        assert "ProxyManager" in result.detail


class TestGmailCheck:
    def test_skips_without_credentials(self):
        result = it._check_gmail(do_network=False)
        assert result.status == it.SKIP

    def test_warns_with_credentials_offline(self, monkeypatch):
        monkeypatch.setenv("GMAIL_ADDRESS", "x@example.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
        result = it._check_gmail(do_network=False)
        assert result.status == it.WARN


class TestSearchConfigCheck:
    def test_skips_when_yaml_absent(self):
        result = it._check_search_config()
        assert result.status == it.SKIP

    def test_warns_when_no_active_tiers(self):
        Path("my_profile").mkdir()
        (Path("my_profile") / "search_config.yaml").write_text(
            "site_tiers:\n"
            "  - name: friendly\n"
            "    sites: [indeed]\n"
            "    results_wanted: 0\n"
            "searches:\n"
            "  - search_term: data\n"
        )
        result = it._check_search_config()
        assert result.status == it.WARN

    def test_passes_with_active_tiers(self):
        Path("my_profile").mkdir()
        (Path("my_profile") / "search_config.yaml").write_text(
            "site_tiers:\n"
            "  - name: friendly\n"
            "    sites: [indeed]\n"
            "    results_wanted: 5\n"
            "searches:\n"
            "  - search_term: data\n"
        )
        result = it._check_search_config()
        assert result.status == it.PASS
        assert "1 searches" in result.detail


class TestTrackStatus:
    def _result(self, label, status):
        return it.CheckResult(label=label, status=status, detail="")

    def _baseline_ready(self):
        # Every shared-required component PASS, both tracks SKIP.
        return [
            self._result("A. Resume", it.PASS),
            self._result("B. Repository layout", it.PASS),
            self._result("C. Dependencies", it.PASS),
            self._result("D. Slack", it.PASS),
            self._result("E. OpenRouter", it.PASS),
            self._result("F1. profile.md", it.PASS),
            self._result("F2. Track A (search yaml)", it.SKIP),
            self._result("F3. Track B (Gmail IMAP)", it.SKIP),
        ]

    def test_no_go_when_shared_required_fails(self):
        results = self._baseline_ready()
        # Knock out OpenRouter
        for r in results:
            if r.label == "E. OpenRouter":
                r.status = it.FAIL
        status, _ = it._track_status("F2. Track A (search yaml)", results)
        assert status == "NO-GO"

    def test_no_go_when_track_skipped(self):
        results = self._baseline_ready()
        status, _ = it._track_status("F2. Track A (search yaml)", results)
        assert status == "NO-GO"

    def test_go_when_track_pass(self):
        results = self._baseline_ready()
        for r in results:
            if r.label == "F2. Track A (search yaml)":
                r.status = it.PASS
        status, _ = it._track_status("F2. Track A (search yaml)", results)
        assert status == "GO"

    def test_warn_counts_as_configured(self):
        # WARN means "credentials present, live check skipped" — track ready.
        results = self._baseline_ready()
        for r in results:
            if r.label == "D. Slack":
                r.status = it.WARN
            if r.label == "F3. Track B (Gmail IMAP)":
                r.status = it.WARN
        status, _ = it._track_status("F3. Track B (Gmail IMAP)", results)
        assert status == "GO"


class TestMain:
    def _run(self, monkeypatch, *args):
        monkeypatch.setattr(sys, "argv", ["integration_test", *args])
        return it.main()

    def test_exit_1_when_no_track_ready(self, monkeypatch, capsys):
        # Bare environment — both tracks NO-GO.
        rc = self._run(monkeypatch, "--no-network")
        captured = capsys.readouterr()
        assert rc == 1
        assert "Track A" in captured.out
        assert "Track B" in captured.out
        assert "No track is ready" in captured.out

    def test_exit_1_when_neither_track_configured(self, monkeypatch, capsys):
        # Satisfy shared-required components; leave Track A + B unconfigured.
        Path("my_profile").mkdir()
        (Path("my_profile") / "base_resume.md").write_text("hi")
        (Path("my_profile") / "profile.md").write_text(
            "# Test\n## Who I am\nJane Doe is an engineer.\n"
        )
        Path(".env").write_text("")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        rc = self._run(monkeypatch, "--no-network")
        captured = capsys.readouterr()
        assert rc == 1
        assert "🔴 NO-GO" in captured.out
        assert "No track is ready" in captured.out

    def test_exit_0_when_track_a_configured(self, monkeypatch, capsys):
        Path("my_profile").mkdir()
        (Path("my_profile") / "base_resume.md").write_text("hi")
        (Path("my_profile") / "profile.md").write_text(
            "# Test\n## Who I am\nJane Doe is an engineer.\n"
        )
        (Path("my_profile") / "search_config.yaml").write_text(
            "site_tiers:\n  - name: friendly\n    sites: [indeed]\n    results_wanted: 5\n"
            "searches:\n  - search_term: data\n"
        )
        Path(".env").write_text("")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        rc = self._run(monkeypatch, "--no-network")
        captured = capsys.readouterr()
        assert rc == 0
        assert "🟢 GO" in captured.out
        assert "python -m src.jobspy_ingest" in captured.out
        assert "Setup looks good" in captured.out
