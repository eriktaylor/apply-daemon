"""Listing-level aggregation for Track B alert emails.

Parses individual listing candidates out of job-alert emails (generic
link-block pass, no platform-specific parsers), pools them across the run,
and selects the top-N by staged scoring:

  1. Cheap signals on the whole pool — sender tier, email freshness, and a
     novelty boost for titles that don't fuzzy-match recent DB listings
     (the "diversity over Track A" lever).
  2. HTTP reachability probe (reusing expired_probe, fail-open) on the
     shortlist only — the top 2×top_n by cheap score.

Selected candidates are handed back to the pipeline, which scrapes the
full posting and runs the existing Stage 1 → 5 triage on each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from rapidfuzz.fuzz import token_set_ratio

from src.expired_probe import probe

logger = logging.getLogger(__name__)

# Links that are navigation/housekeeping, never job postings.
_SKIP_HREF_PATTERNS = [
    "unsubscribe", "optout", "opt-out", "preferences", "tracking", "beacon",
    ".gif", "settings", "privacy", "help.", "/help", "support.", "feedback",
    "login", "signin", "sign-in", "account", "notifications/",
]

# Anchor texts that are UI chrome, not job titles.
_SKIP_TEXT_PHRASES = [
    "see all", "view all", "see more", "view more", "browse", "unsubscribe",
    "manage", "sign in", "log in", "open app", "download", "get the app",
    "view job", "apply now", "see jobs", "help center", "flag as",
]

_NOVELTY_FUZZ_THRESHOLD = 90  # token_set_ratio at/above this = already known
_TIER_WEIGHTS = {"friendly": 2.0, "ok": 1.0, "hostile": 0.0}


@dataclass
class ListingCandidate:
    """One parsed listing link from an alert email, plus scoring context."""

    title: str
    url: str
    snippet: str = ""
    source: str = ""
    tier: str = "ok"
    email_uid: bytes = b""
    gmail_message_id: str = ""
    email_age_days: float = 0.0
    score: float = field(default=0.0, compare=False)


def _unwrap_google_redirect(href: str) -> str:
    """Resolve google.com/url?...&url=<real> redirect wrappers to the target."""
    parsed = urlparse(href)
    if "google." in parsed.netloc and parsed.path == "/url":
        params = parse_qs(parsed.query)
        for key in ("url", "q"):
            if params.get(key):
                return params[key][0]
    return href


def _looks_like_job_link(href: str, text: str) -> bool:
    # Match skip patterns against host+path only — job URLs legitimately
    # carry query params like ?trackingId=... that must not disqualify them.
    parsed = urlparse(href.lower())
    host_and_path = f"{parsed.netloc}{parsed.path}"
    if any(p in host_and_path for p in _SKIP_HREF_PATTERNS):
        return False
    text_lower = text.lower()
    if any(p in text_lower for p in _SKIP_TEXT_PHRASES):
        return False
    # A job title is at least two words and a real phrase, not an icon/emoji
    # placeholder or a bare company name like "LinkedIn".
    return len(text) >= 10 and len(text.split()) >= 2


def parse_candidates(
    html: str,
    source: str,
    tier: str,
    email_uid: bytes,
    gmail_message_id: str,
    email_age_days: float,
) -> list[ListingCandidate]:
    """Extract listing candidates from an alert email's HTML.

    Generic link-block pass: anchor text is the title candidate, the parent
    block's text is the snippet (usually company · location · salary in
    digest layouts). Returns [] when nothing parseable is found — the
    pipeline then falls back to email-level Stage 1 for this email.
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[ListingCandidate] = []
    seen_keys: set[tuple[str, str]] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        text = a.get_text(separator=" ", strip=True)
        if not _looks_like_job_link(href, text):
            continue

        url = _unwrap_google_redirect(href)
        host = urlparse(url).netloc.lower()
        key = (text.lower(), host)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        snippet = ""
        if a.parent is not None:
            snippet = a.parent.get_text(separator=" ", strip=True)[:300]

        candidates.append(
            ListingCandidate(
                title=text[:200],
                url=url,
                snippet=snippet,
                source=source,
                tier=tier,
                email_uid=email_uid,
                gmail_message_id=gmail_message_id,
                email_age_days=email_age_days,
            )
        )

    logger.info(
        "Parsed %d candidate(s) from %s email (age %.1fd)",
        len(candidates), source, email_age_days,
    )
    return candidates


def score_candidates(
    candidates: list[ListingCandidate], recent_titles: list[str]
) -> None:
    """Assign the cheap-signal score in place: tier + freshness + novelty."""
    for cand in candidates:
        tier_score = _TIER_WEIGHTS.get(cand.tier, 1.0)
        freshness = max(0.0, 1.5 - 0.1 * cand.email_age_days)
        novelty = 2.0
        for known_title in recent_titles:
            if token_set_ratio(cand.title, known_title) >= _NOVELTY_FUZZ_THRESHOLD:
                novelty = 0.0
                break
        cand.score = tier_score + freshness + novelty


def select_top_n(
    candidates: list[ListingCandidate],
    top_n: int | None,
    probe_fn=probe,
) -> tuple[list[ListingCandidate], int]:
    """Pick the top-N candidates by score, probing the shortlist for life.

    Probe budget is 2×top_n: candidates beyond the budget are not selected
    unprobed. Probe failures fail open (candidate kept) inside probe_fn, so
    a flaky probe never starves the selection. Returns (selected,
    expired_dropped_count). top_n=None returns everything unprobed —
    today's process-everything behavior.
    """
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    if top_n is None:
        return ranked, 0

    selected: list[ListingCandidate] = []
    dropped = 0
    probe_budget = 2 * top_n

    for cand in ranked:
        if len(selected) >= top_n or probe_budget <= 0:
            break
        probe_budget -= 1
        is_expired, reason = probe_fn(cand.url)
        if is_expired:
            dropped += 1
            logger.info(
                "Probe dropped candidate '%s' (%s): %s",
                cand.title[:60], urlparse(cand.url).netloc, reason,
            )
            continue
        selected.append(cand)

    logger.info(
        "Selected %d/%d candidate(s) (top_n=%d, probe-dropped %d)",
        len(selected), len(candidates), top_n, dropped,
    )
    return selected, dropped
