"""User-initiated setup evaluation for apply-daemon.

Walks the README setup checklist (A–H) and reports which components
are configured and reachable. Designed to consume the absolute
minimum of paid credits — the only billable call is a single
1-token OpenRouter completion against the Stage 5 model (well under
$0.0001 with the default ``google/gemini-3.1-flash-lite-preview``).
The Slack ``auth.test`` and Gmail IMAP ``LOGIN`` checks are free;
the IPRoyal check verifies credentials are present without opening
a session (use ``apply-daemon-test-proxy`` for the full smoke test).

Usage:
    python -m src.integration_test
    apply-daemon-eval

Flags:
    --no-llm     skip the live OpenRouter call (no paid traffic)
    --no-network skip every network check (Slack/Gmail/OpenRouter)
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Result statuses
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
WARN = "WARN"

# ANSI colors for terminal-friendly output. Disabled when stdout is not a TTY.
_USE_COLOR = sys.stdout.isatty()
_COLORS = {
    PASS: "\033[32m",  # green
    FAIL: "\033[31m",  # red
    SKIP: "\033[90m",  # grey
    WARN: "\033[33m",  # yellow
}
_RESET = "\033[0m"


def _color(status: str) -> str:
    if not _USE_COLOR:
        return status
    return f"{_COLORS.get(status, '')}{status}{_RESET}"


@dataclass
class CheckResult:
    label: str
    status: str
    detail: str

    def render(self) -> str:
        return f"  {self.label:<32} [{_color(self.status)}]  {self.detail}"


def _check_resume() -> CheckResult:
    """A. base_resume.{docx,md,pdf} present in my_profile/."""
    profile_dir = Path("my_profile")
    if not profile_dir.is_dir():
        return CheckResult(
            "A. Resume",
            SKIP,
            "my_profile/ not yet created — run `cp -r my_profile_example my_profile`",
        )
    for ext in (".docx", ".md", ".pdf"):
        candidate = profile_dir / f"base_resume{ext}"
        if candidate.exists():
            size_kb = candidate.stat().st_size / 1024
            return CheckResult(
                "A. Resume",
                PASS,
                f"{candidate.name} ({size_kb:.1f} KB)",
            )
    return CheckResult(
        "A. Resume",
        FAIL,
        "no base_resume.{docx,md,pdf} in my_profile/ — required for tailoring",
    )


def _check_repo_layout() -> CheckResult:
    """B. my_profile/ + .env present."""
    missing = [p for p in ("my_profile", ".env") if not Path(p).exists()]
    if missing:
        return CheckResult(
            "B. Repository layout",
            FAIL,
            f"missing: {', '.join(missing)}",
        )
    return CheckResult(
        "B. Repository layout",
        PASS,
        "my_profile/ and .env present",
    )


def _check_dependencies() -> CheckResult:
    """C. Required third-party packages importable."""
    required = [
        "rapidfuzz",
        "openai",
        "slack_bolt",
        "jobspy",
        "yaml",
        "geopy",
        "trafilatura",
        "pdfplumber",
        "docx",
    ]
    missing = []
    broken = []
    for name in required:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
        except BaseException as exc:  # tolerate native panics from broken installs
            broken.append(f"{name} ({exc.__class__.__name__})")
    if missing:
        return CheckResult(
            "C. Dependencies",
            FAIL,
            f"not importable: {', '.join(missing)} — run `pip install -e \".[dev]\"`",
        )
    if broken:
        return CheckResult(
            "C. Dependencies",
            WARN,
            f"installed but raised on import: {', '.join(broken)}",
        )
    return CheckResult(
        "C. Dependencies",
        PASS,
        f"{len(required)} required packages importable",
    )


def _check_slack(do_network: bool) -> CheckResult:
    """D. SLACK_BOT_TOKEN + SLACK_CHANNEL_ID present; auth.test reachable."""
    token = os.getenv("SLACK_BOT_TOKEN")
    channel = os.getenv("SLACK_CHANNEL_ID")
    if not token or not channel:
        missing = [name for name, val in
                   (("SLACK_BOT_TOKEN", token), ("SLACK_CHANNEL_ID", channel)) if not val]
        return CheckResult(
            "D. Slack",
            FAIL,
            f"unset in .env: {', '.join(missing)}",
        )
    if not do_network:
        return CheckResult(
            "D. Slack",
            WARN,
            "credentials present; skipped auth.test (--no-network)",
        )
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as exc:
        return CheckResult("D. Slack", FAIL, f"slack_sdk not importable: {exc}")
    client = WebClient(token=token)
    try:
        auth = client.auth_test()
        team = auth.get("team", "?")
        bot = auth.get("user", "?")
    except SlackApiError as exc:
        return CheckResult(
            "D. Slack",
            FAIL,
            f"auth.test failed: {exc.response.get('error', exc)}",
        )
    except Exception as exc:
        return CheckResult("D. Slack", FAIL, f"auth.test error: {exc}")
    # Channel reachability: probe with conversations.history(limit=1), which
    # only needs the channels:history scope the sweeper already requires.
    # Avoids asking users to grant channels:read just to satisfy the eval.
    try:
        client.conversations_history(channel=channel, limit=1)
    except SlackApiError as exc:
        err = exc.response.get("error", str(exc))
        return CheckResult(
            "D. Slack",
            WARN,
            f"bot '{bot}' authed in '{team}', but channel {channel} unreachable: {err}",
        )
    return CheckResult(
        "D. Slack",
        PASS,
        f"bot '{bot}' in '{team}'; channel {channel} reachable",
    )


def _check_openrouter(do_network: bool, do_llm: bool) -> CheckResult:
    """E. OPENROUTER_API_KEY + tiny live completion."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
    if not api_key:
        return CheckResult(
            "E. OpenRouter",
            FAIL,
            "OPENROUTER_API_KEY unset in .env — required for all LLM calls",
        )
    if not (do_network and do_llm):
        return CheckResult(
            "E. OpenRouter",
            WARN,
            f"key present; skipped live completion (model={model})",
        )
    try:
        import openai
    except ImportError as exc:
        return CheckResult("E. OpenRouter", FAIL, f"openai SDK not importable: {exc}")
    try:
        client = openai.OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with just: ok"}],
            max_tokens=1,
            temperature=0,
        )
    except Exception as exc:
        return CheckResult(
            "E. OpenRouter",
            FAIL,
            f"completion failed against {model}: {exc.__class__.__name__}: {exc}",
        )
    usage = getattr(resp, "usage", None)
    in_tokens = getattr(usage, "prompt_tokens", "?")
    out_tokens = getattr(usage, "completion_tokens", "?")
    return CheckResult(
        "E. OpenRouter",
        PASS,
        f"1-token completion via {model} (in={in_tokens}, out={out_tokens})",
    )


def _check_profile() -> CheckResult:
    """F1. my_profile/profile.md parses."""
    try:
        from src.profile_loader import load_profile
        profile = load_profile()
    except FileNotFoundError as exc:
        return CheckResult("F1. profile.md", FAIL, str(exc))
    except Exception as exc:
        return CheckResult("F1. profile.md", FAIL, f"parse error: {exc}")
    name = profile.get("name") or "<unset>"
    settings = profile.get("settings") or {}
    home = settings.get("home_location", "<unset>")
    return CheckResult(
        "F1. profile.md",
        PASS,
        f"name={name!r}, home_location={home!r}, {len(settings)} settings parsed",
    )


def _check_search_config() -> CheckResult:
    """F2. Track A — my_profile/search_config.yaml parses."""
    try:
        from src.jobspy_ingest import load_search_config
        config = load_search_config()
    except FileNotFoundError:
        return CheckResult(
            "F2. Track A (search yaml)",
            SKIP,
            "my_profile/search_config.yaml absent — Track A disabled",
        )
    except Exception as exc:
        return CheckResult(
            "F2. Track A (search yaml)",
            FAIL,
            f"parse error: {exc}",
        )
    searches = config.get("searches") or []
    tiers = config.get("site_tiers") or []
    # site_tiers is a list of {name, sites, results_wanted}; tolerate
    # the legacy dict shape (name → cfg) too.
    if isinstance(tiers, dict):
        tier_iter = tiers.values()
    else:
        tier_iter = tiers
    active_tiers = [
        cfg for cfg in tier_iter
        if isinstance(cfg, dict) and cfg.get("results_wanted", 0) > 0
    ]
    if not searches or not active_tiers:
        return CheckResult(
            "F2. Track A (search yaml)",
            WARN,
            f"{len(searches)} searches, {len(active_tiers)} active tiers — nothing will run",
        )
    return CheckResult(
        "F2. Track A (search yaml)",
        PASS,
        f"{len(searches)} searches × {len(active_tiers)} active tiers",
    )


def _check_gmail(do_network: bool) -> CheckResult:
    """F3. Track B — Gmail IMAP credentials + login."""
    address = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not address or not password:
        return CheckResult(
            "F3. Track B (Gmail IMAP)",
            SKIP,
            "GMAIL_ADDRESS / GMAIL_APP_PASSWORD unset — Track B disabled",
        )
    if not do_network:
        return CheckResult(
            "F3. Track B (Gmail IMAP)",
            WARN,
            f"credentials present for {address}; skipped login (--no-network)",
        )
    import imaplib
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(address, password)
        conn.logout()
    except Exception as exc:
        return CheckResult(
            "F3. Track B (Gmail IMAP)",
            FAIL,
            f"IMAP login failed for {address}: {exc}",
        )
    return CheckResult(
        "F3. Track B (Gmail IMAP)",
        PASS,
        f"IMAP login succeeded for {address}",
    )


def _check_proxy() -> CheckResult:
    """G. IPRoyal credentials present (full smoke test = apply-daemon-test-proxy)."""
    try:
        from src.proxy_manager import ProxyManager
    except ImportError as exc:
        return CheckResult("G. IPRoyal proxy", FAIL, f"proxy_manager import failed: {exc}")
    mgr = ProxyManager()
    if not mgr.enabled:
        return CheckResult(
            "G. IPRoyal proxy",
            SKIP,
            "IPROYAL_USERNAME / IPROYAL_PASSWORD unset (optional)",
        )
    return CheckResult(
        "G. IPRoyal proxy",
        PASS,
        f"{mgr.describe()} — run `apply-daemon-test-proxy` for live IP smoke test",
    )


def run_all(do_network: bool, do_llm: bool) -> list[CheckResult]:
    return [
        _check_resume(),
        _check_repo_layout(),
        _check_dependencies(),
        _check_slack(do_network),
        _check_openrouter(do_network, do_llm),
        _check_profile(),
        _check_search_config(),
        _check_gmail(do_network),
        _check_proxy(),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "User-initiated setup evaluation. Walks the README A–H checklist "
            "and reports which components are configured and reachable."
        ),
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="Skip every network check (Slack auth.test, Gmail IMAP login, OpenRouter call).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip only the live OpenRouter completion (no paid traffic).",
    )
    args = parser.parse_args()

    do_network = not args.no_network
    do_llm = not args.no_llm

    print()
    print("apply-daemon — integration evaluation")
    print("=" * 60)
    results = run_all(do_network=do_network, do_llm=do_llm)
    for result in results:
        print(result.render())
    print()

    counts = {PASS: 0, FAIL: 0, SKIP: 0, WARN: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = (
        f"Summary: {counts[PASS]} passed, {counts[FAIL]} failed, "
        f"{counts[WARN]} warned, {counts[SKIP]} skipped"
    )
    print(summary)
    print()

    required_labels = {
        "B. Repository layout",
        "C. Dependencies",
        "D. Slack",
        "E. OpenRouter",
        "F1. profile.md",
    }
    required_failures = [r for r in results if r.status == FAIL and r.label in required_labels]
    if required_failures:
        print("Required components failing — fix the items above before running the pipeline.")
        return 1

    track_a = next((r for r in results if r.label == "F2. Track A (search yaml)"), None)
    track_b = next((r for r in results if r.label == "F3. Track B (Gmail IMAP)"), None)
    if (track_a and track_a.status != PASS) and (track_b and track_b.status != PASS):
        print("Configure at least one of Track A (search_config.yaml) or Track B (Gmail) "
              "before running the pipeline.")
        return 1

    print("Setup looks good — run `apply-daemon-ingest` (Track A) or `apply-daemon` (Track B).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
