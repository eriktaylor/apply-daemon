"""Track A: Proactive job polling via JobSpy (python-jobspy).

Reads my_profile/search_config.yaml, runs scrape_jobs() for each configured search ×
site-tier pair with random delays between queries, maps the structured
DataFrame rows to ExtractedListing (Stage 4), optionally lazy-loads full
descriptions for truncated postings (Stage 4b), calls Stage 5 LLM scoring
via TriageSession.evaluate_listing(), and upserts results into SQLite via
Smart Upsert.

This track runs alongside the existing reactive email/Slack pipeline (Track B).
Both tracks share the same database and Smart Upsert dedup logic.

Usage:
    python -m src.jobspy_ingest           # run all configured searches
    apply-daemon-ingest                    # same via CLI entry point
"""

from __future__ import annotations

import logging
import os
import random
import re
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv

load_dotenv()

from src.db import Database
from src.pipeline import setup_logging
from src.profile_loader import load_profile
from src.proxy_manager import get_default_proxy_manager
from src.triage import ExtractedListing, TriageSession, _is_aggregator_url, _scrape_url

logger = logging.getLogger(__name__)

SEARCH_CONFIG_PATH = Path("my_profile/search_config.yaml")
EXAMPLE_SEARCH_CONFIG_PATH = Path("my_profile_example/search_config.yaml")

# Three attempts per query by default (1 initial + 2 IP rotations on confirmed
# blocks). Tunable via IPROYAL_BLOCK_RETRIES; only takes effect when the proxy
# is enabled and a block is actually confirmed (see _Urllib3BlockObserver).
MAX_BLOCK_RETRIES = int(os.getenv("IPROYAL_BLOCK_RETRIES", "2"))


class _Urllib3BlockObserver(logging.Handler):
    """Capture urllib3.connectionpool retry warnings that mention bot-block codes.

    JobSpy's internal urllib3 retries surface block status codes through
    log records like:

        Retrying (Retry(total=2, ...)) after connection broken by
        'OSError('Tunnel connection failed: 403 Forbidden')'

    even when scrape_jobs ultimately swallows the failure and returns an
    empty DataFrame. Listening here gives us a positive block signal so
    we can distinguish a real-zero-results query from a hard block.
    """

    _BLOCK_STATUS_RE = re.compile(r"\b(403|429|999)\b")

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.blocked_codes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        match = self._BLOCK_STATUS_RE.search(msg)
        if match:
            self.blocked_codes.append(match.group(1))


def _scrape_with_block_detection(
    scrape_jobs: Callable[..., Any], **kwargs: Any,
) -> tuple[Any, list[str]]:
    """Run scrape_jobs() while observing urllib3.connectionpool warnings.

    Returns ``(DataFrame_or_None, list_of_block_codes_observed)``. Always
    cleans up the handler even when scrape_jobs raises.
    """
    observer = _Urllib3BlockObserver()
    urllib3_logger = logging.getLogger("urllib3.connectionpool")
    urllib3_logger.addHandler(observer)
    try:
        df = scrape_jobs(**kwargs)
    finally:
        urllib3_logger.removeHandler(observer)
    return df, observer.blocked_codes


def _scrape_jobs_with_retries(
    scrape_jobs: Callable[..., Any],
    scrape_kwargs: dict,
    proxy_mgr: Any,
    search_term: str,
    tier_name: str,
) -> tuple[Any, str | None, int]:
    """Run ``scrape_jobs`` with up to MAX_BLOCK_RETRIES IP rotations.

    Mirrors Track B's circuit-breaker contract: one fetch → inspect result
    → rotate the IPRoyal session and retry inline only when a block is
    actually confirmed (urllib3 saw 403/429/999 OR scrape_jobs raised).

    Returns ``(DataFrame_or_None, last_failure_label, attempts_made)``:

    - ``last_failure_label is None``        — success (df has rows)
    - ``"empty_no_block"``                  — legit zero results, no rotation done
    - ``"exception"``                       — every attempt raised
    - ``"block_403"`` / ``"block_403,429"`` — confirmed block on final attempt
    """
    jobs_df = None
    last_failure: str | None = None
    attempt = 0
    for attempt in range(MAX_BLOCK_RETRIES + 1):
        proxy_list = proxy_mgr.proxies_list()  # re-mint each attempt
        if proxy_list:
            scrape_kwargs["proxies"] = proxy_list

        try:
            jobs_df, blocked_codes = _scrape_with_block_detection(
                scrape_jobs, **scrape_kwargs,
            )
        except Exception:
            last_failure = "exception"
            logger.warning(
                "JobSpy raised on attempt %d/%d for '%s' [tier=%s]",
                attempt + 1, MAX_BLOCK_RETRIES + 1, search_term, tier_name,
                exc_info=True,
            )
        else:
            if jobs_df is not None and not jobs_df.empty:
                last_failure = None
                break  # success
            if not blocked_codes:
                # Empty + no urllib3 block signal = genuine zero results.
                # Do NOT burn IP sessions on real zero-result queries.
                last_failure = "empty_no_block"
                break
            last_failure = (
                f"block_{','.join(sorted(set(blocked_codes)))}"
            )

        can_retry = attempt < MAX_BLOCK_RETRIES and proxy_mgr.enabled
        if not can_retry:
            break

        proxy_mgr.force_rotate(
            reason=f"jobspy_{last_failure}_attempt_{attempt+1}"
        )
        logger.warning(
            "Confirmed block (%s) on attempt %d/%d for '%s' [tier=%s] — "
            "rotated IPRoyal session, retrying...",
            last_failure, attempt + 1, MAX_BLOCK_RETRIES + 1,
            search_term, tier_name,
        )

    return jobs_df, last_failure, attempt + 1


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_search_config() -> dict:
    """Load search configuration from my_profile/search_config.yaml."""
    if not SEARCH_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"search_config.yaml not found at {SEARCH_CONFIG_PATH.resolve()}. "
            f"Run 'cp -r my_profile_example my_profile' (or copy "
            f"{EXAMPLE_SEARCH_CONFIG_PATH} into my_profile/) and customize it."
        )
    with SEARCH_CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Stage 4: structured data → ExtractedListing
# ---------------------------------------------------------------------------

def _format_salary(row) -> str:
    """Build a human-readable salary string from flattened JobSpy DataFrame columns."""
    min_amount = row.get("min_amount")
    max_amount = row.get("max_amount")
    interval = row.get("interval") or "yearly"

    try:
        if min_amount and max_amount:
            min_int, max_int = int(min_amount), int(max_amount)
            if str(interval).lower() in ("yearly", "annual"):
                return f"${min_int:,}–${max_int:,} / year"
            if str(interval).lower() == "hourly":
                return f"${min_amount:.2f}–${max_amount:.2f} / hour"
            if str(interval).lower() == "monthly":
                return f"${min_int:,}–${max_int:,} / month"
            return f"${min_int:,}–${max_int:,} ({interval})"
        if min_amount:
            return f"${int(min_amount):,}+ / year"
    except (TypeError, ValueError):
        pass

    return "not listed"


def _row_to_extracted_listing(row) -> ExtractedListing:
    """Stage 4 (Track A): map a JobSpy DataFrame row to ExtractedListing.

    JobSpy returns structured data directly — no LLM extraction needed.
    The DataFrame columns used are:
        title, company, location, is_remote,
        min_amount, max_amount, interval, currency,
        description, job_url
    """
    title = str(row.get("title") or "").strip()
    company = str(row.get("company") or "").strip()
    description = str(row.get("description") or "").strip()
    job_url = str(row.get("job_url") or "").strip()

    # Location: use the string column, optionally append (Remote)
    location = str(row.get("location") or "").strip()
    is_remote = row.get("is_remote")
    if is_remote and location and "remote" not in location.lower():
        location = f"{location} (Remote)"
    elif is_remote and not location:
        location = "Remote"

    salary = _format_salary(row)

    # Use the first 300 chars of description as a quick job_summary (no LLM call).
    # Stage 5's _EVALUATE_PROMPT will see the full description; job_summary is
    # only used for the Slack digest card preview.
    job_summary = description[:300].strip() if description else ""

    return ExtractedListing(
        title=title,
        company=company,
        location=location,
        salary=salary,
        job_summary=job_summary,
        description=description[:2000],  # Stage 5 sees up to 2000 chars
        links=[job_url] if job_url else [],
    )


# ---------------------------------------------------------------------------
# Stage 4b: lazy-load full description for truncated postings
# ---------------------------------------------------------------------------

_TRUNCATION_MARKERS = frozenset({"...", "…", "show more", "see more", "read more"})
# Indeed/LinkedIn search-result descriptions are often 100-200-word snippets.
# Raise the bar to 300 so those truncated previews always trigger a lazy fetch.
_MIN_WORDS_FULL = 300


def _is_truncated(description: str) -> bool:
    """Return True if the description looks like it was cut off by the job board.

    Heuristics:
    - Fewer than _MIN_WORDS_FULL words (Indeed/LinkedIn search APIs truncate previews).
    - Ends with a known truncation marker after stripping whitespace.
    """
    if not description:
        return False
    stripped = description.strip()
    word_count = len(stripped.split())
    if word_count < _MIN_WORDS_FULL:
        logger.debug("Description has only %d words — flagged as truncated", word_count)
        return True
    lower = stripped.lower()
    return any(lower.endswith(marker) for marker in _TRUNCATION_MARKERS)


def _is_indeed_detail_url(url: str) -> bool:
    """Return True if the URL is an Indeed job-detail page (viewjob?jk=...).

    Indeed's viewjob pages contain the full posting and are safe to scrape,
    even though ``indeed.com`` is listed in _AGGREGATOR_DOMAINS (which blocks
    tracker/redirect URLs from email digests).
    """
    try:
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        return (
            (host == "indeed.com" or host.endswith(".indeed.com"))
            and parsed.path.rstrip("/") == "/viewjob"
            and "jk" in parse_qs(parsed.query)
        )
    except Exception:
        return False


def _get_full_description(anchor: ExtractedListing, row) -> str | None:
    """Attempt to fetch the full job description by scraping direct URLs.

    Priority order:
    1. ``job_url_direct`` — ATS link (Workday, Greenhouse, etc.), never aggregator
    2. ``job_url`` — job-board detail page; allowed when it's an Indeed viewjob URL
       or any other non-aggregator URL

    Returns the scraped text on success, or None if all attempts fail.
    """
    candidates: list[str] = []

    direct = str(row.get("job_url_direct") or "").strip()
    if direct and not _is_aggregator_url(direct):
        candidates.append(direct)

    job_url = str(row.get("job_url") or "").strip()
    if job_url and job_url not in candidates:
        # Allow Indeed viewjob detail pages even though indeed.com is in _AGGREGATOR_DOMAINS
        if not _is_aggregator_url(job_url) or _is_indeed_detail_url(job_url):
            candidates.append(job_url)

    for url in candidates:
        try:
            text = _scrape_url(url)
            if text and len(text.split()) >= _MIN_WORDS_FULL:
                logger.debug(
                    "Lazy-loaded full description for '%s at %s' from %s",
                    anchor.title, anchor.company, url,
                )
                return text
        except Exception:
            logger.debug(
                "Failed to lazy-load description from %s for '%s at %s'",
                url, anchor.title, anchor.company, exc_info=True,
            )

    return None


# ---------------------------------------------------------------------------
# Main ingest loop
# ---------------------------------------------------------------------------

def run_jobspy_ingest() -> None:
    """Execute all configured JobSpy searches and upsert results into SQLite."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        logger.error(
            "python-jobspy is not installed. Run: pip install python-jobspy"
        )
        sys.exit(1)

    config = load_search_config()
    profile = load_profile()
    settings = profile["settings"]
    dedup_window = settings.get("dedup_window_days", 30)
    pass_window = settings.get("pass_window_days", 180)
    delays = config.get("delays", {})
    delay_min = float(delays.get("between_queries_min", 5))
    delay_max = float(delays.get("between_queries_max", 15))

    site_tiers = config.get("site_tiers", [])
    searches = config.get("searches", [])
    if not searches:
        logger.warning("No searches configured in my_profile/search_config.yaml — nothing to do")
        return
    if not site_tiers:
        logger.warning("No site_tiers configured in my_profile/search_config.yaml — nothing to do")
        return

    # Expand enabled tiers (results_wanted > 0)
    active_tiers = [t for t in site_tiers if t.get("results_wanted", 0) > 0]
    if not active_tiers:
        logger.warning("All site_tiers have results_wanted=0 — nothing to do")
        return

    total_queries = len(searches) * len(active_tiers)
    proxy_mgr = get_default_proxy_manager()
    if proxy_mgr.enabled:
        logger.info(
            "JobSpy ingest: routing through IPRoyal sticky residential proxy "
            "(lifetime=%dm)", proxy_mgr._lifetime_minutes,
        )
    logger.info(
        "JobSpy ingest starting — %d search(es) × %d active tier(s) = %d queries for %s",
        len(searches), len(active_tiers), total_queries, profile["name"],
    )

    stats = {
        "total": 0, "new": 0, "updated": 0, "skipped": 0, "deduped": 0,
        "yes": 0, "maybe": 0, "no": 0,
    }

    query_count = 0

    with Database() as db, TriageSession(profile["llm_context"]) as session:
        for search in searches:
            for tier in active_tiers:
                if query_count > 0:
                    delay = random.uniform(delay_min, delay_max)
                    logger.info("Sleeping %.1fs between queries...", delay)
                    time.sleep(delay)

                query_count += 1
                search_term = search.get("search_term", "")
                location = search.get("location", "")
                tier_name = tier.get("name", "unknown")
                tier_sites = tier.get("sites", [])
                results_wanted = tier.get("results_wanted", 10)

                logger.info(
                    "Query %d/%d: '%s' in '%s' [tier=%s, sites=%s, results=%d]",
                    query_count, total_queries,
                    search_term, location, tier_name, tier_sites, results_wanted,
                )

                # Build scrape_jobs kwargs — only pass keys the function accepts.
                scrape_kwargs: dict = {
                    "site_name": tier_sites,
                    "search_term": search_term,
                    "location": location,
                    "hours_old": search.get("hours_old", 24),
                    "results_wanted": results_wanted,
                    "country_indeed": search.get("country_indeed", "USA"),
                    "is_remote": search.get("is_remote", False),
                    "verbose": 0,
                }

                # LinkedIn returns truncated search-result descriptions by default.
                # linkedin_fetch_description=True fetches each job's full detail page
                # at scrape time, eliminating the need for lazy loading on LinkedIn.
                if any("linkedin" in s.lower() for s in tier_sites):
                    scrape_kwargs["linkedin_fetch_description"] = True

                jobs_df, last_failure, attempts_made = _scrape_jobs_with_retries(
                    scrape_jobs, scrape_kwargs, proxy_mgr, search_term, tier_name,
                )

                if jobs_df is None or jobs_df.empty:
                    if last_failure == "empty_no_block":
                        logger.info(
                            "No results for '%s' in '%s' [tier=%s]",
                            search_term, location, tier_name,
                        )
                    elif last_failure == "exception":
                        logger.error(
                            "JobSpy failed after %d attempt(s) for '%s' [tier=%s] — skipping.",
                            attempts_made, search_term, tier_name,
                        )
                    else:
                        logger.error(
                            "JobSpy returned empty results after %d IP rotation(s) for "
                            "'%s' [tier=%s, sites=%s, last_signal=%s] — site likely "
                            "hard-blocking us.",
                            attempts_made - 1, search_term, tier_name, tier_sites, last_failure,
                        )
                    continue

                logger.info(
                    "Found %d job(s) for '%s' in '%s' [tier=%s]",
                    len(jobs_df), search_term, location, tier_name,
                )

                for _, row in jobs_df.iterrows():
                    stats["total"] += 1

                    # Stage 4: map structured row → ExtractedListing
                    anchor = _row_to_extracted_listing(row)

                    if not anchor.title or not anchor.company:
                        logger.debug("Skipping row with missing title/company")
                        stats["skipped"] += 1
                        continue

                    # Pre-Stage-5 dedup: skip jobs already in the database to
                    # avoid wasting OpenRouter API credits on known listings.
                    if db.is_duplicate_listing(
                        anchor.title, anchor.company, window_days=dedup_window
                    ):
                        logger.info(
                            "Dedup (pre-Stage5): skipping '%s at %s' — already in DB",
                            anchor.title, anchor.company,
                        )
                        stats["deduped"] += 1
                        continue

                    # Stage 4b: lazy-load full description if truncated.
                    # Job boards (especially LinkedIn) often return <100-word previews.
                    # Attempt to fetch the full text from job_url_direct before scoring.
                    if _is_truncated(anchor.description):
                        logger.debug(
                            "Description truncated for '%s at %s' — attempting lazy load",
                            anchor.title, anchor.company,
                        )
                        full_text = _get_full_description(anchor, row)
                        if full_text:
                            anchor = replace(
                                anchor,
                                description=full_text[:2000],
                                job_summary=full_text[:300].strip(),
                            )
                        else:
                            logger.debug(
                                "Lazy load failed for '%s at %s' — using truncated description",
                                anchor.title, anchor.company,
                            )

                    # Stage 5: LLM scoring against candidate profile
                    source_site = str(row.get("site") or "jobspy")
                    try:
                        listing = session.evaluate_listing(
                            anchor=anchor,
                            job_text=anchor.description,
                            job_links=anchor.links,
                            classification="JOB_DIGEST",
                            source=source_site,
                        )
                    except Exception:
                        logger.error(
                            "Stage 5 evaluation failed for '%s at %s'",
                            anchor.title, anchor.company, exc_info=True,
                        )
                        stats["skipped"] += 1
                        continue

                    if listing is None:
                        stats["skipped"] += 1
                        continue

                    # Smart Upsert: UPDATE if fuzzy-matched existing, INSERT if new
                    try:
                        was_update, existing_id = db.upsert_listing(
                            listing, window_days=dedup_window, pass_window_days=pass_window
                        )
                    except Exception:
                        logger.error(
                            "DB upsert failed for '%s at %s'",
                            anchor.title, anchor.company, exc_info=True,
                        )
                        stats["skipped"] += 1
                        continue

                    if was_update:
                        logger.info(
                            "Updated: '%s' at '%s' (id=%s)",
                            listing.title, listing.company, existing_id,
                        )
                        stats["updated"] += 1
                    else:
                        logger.info(
                            "New: %s '%s' at '%s' (%d%%)",
                            listing.verdict, listing.title, listing.company,
                            listing.confidence,
                        )
                        stats["new"] += 1
                        verdict_key = listing.verdict.lower()
                        if verdict_key in stats:
                            stats[verdict_key] += 1

    logger.info("JobSpy ingest complete:")
    logger.info("  Total rows processed: %d", stats["total"])
    logger.info("  Deduped (pre-LLM):    %d", stats["deduped"])
    logger.info("  New listings:         %d", stats["new"])
    logger.info("  Updated listings:     %d", stats["updated"])
    logger.info("  Skipped:              %d", stats["skipped"])
    logger.info(
        "  Verdicts:             YES=%d, MAYBE=%d, NO=%d",
        stats["yes"], stats["maybe"], stats["no"],
    )


def main() -> None:
    setup_logging()
    run_jobspy_ingest()


if __name__ == "__main__":
    main()
