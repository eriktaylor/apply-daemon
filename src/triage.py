"""Confidence-thresholded scoring engine for job listing triage.

Two-step process:
  Step A (Extract): First model extracts structured listings from email text.
  Step B (Score):   The configured model evaluates each listing, returning
                    verdict + confidence + reasoning as JSON. Listings with
                    confidence below CONFIDENCE_THRESHOLD are auto-rejected.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import openai
from dotenv import load_dotenv

from src.model_usage import log_model_usage
from src.models import (
    FULL_JOB_DESCRIPTION_CHARS,
    JOB_DESCRIPTION_CHARS,
    JobListing,
    job_description_text,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """\
You are a recruiting assistant. Below is the raw text extracted from a job-related email.

## Email text
{extracted_email_text}

## Instructions
1. Identify each distinct job listing mentioned in the email.
   - For digest emails, there may be multiple listings (typically 3-8).
   - For recruiter outreach, the entire email is about one role.
   - If the email contains no identifiable job listings, respond with: NO_LISTINGS_FOUND

2. If the email text appears to be a direct email from a recruiter, IGNORE the location listed in
   their email signature. Only extract a location if it is explicitly stated as the required location
   for the role itself. If the role's location is ambiguous or not explicitly stated, output "Unknown"
   for the location field.

3. For each listing, extract whatever information is available and respond in
   this exact format, one block per listing:

LISTING:
title: [job title]
company: [company name]
location: [location or "not specified"]
salary: [salary range or "not listed"]
job_summary: [2 punchy sentences. (1) For startups or lesser-known companies: what they do and their stage (e.g. seed-stage, Series B, public); for household names (Google, Meta, Stripe, etc.): name the specific team, product area, or initiative. (2) Core responsibility of this role.]
description: [2-3 sentence summary of the role, responsibilities, and notable details]
source_board: [domain of the job board if the listing includes a "via [Board]" attribution, e.g. "talent.com" from "via Talent.com"; otherwise "none"]
links: [any relevant job URLs from the email, comma-separated, or "none"]
---

If the email is a recruiter outreach, also include:
recruiter_name: [name if identifiable]
recruiter_title: [title if identifiable]

Do not include any text outside of this format.\
"""

# M-4: batch scoring. Same task as _EVALUATE_PROMPT, N listings per call, so
# the candidate profile is sent once instead of once per listing. Measured at
# ~9x fewer input tokens with 98% verdict agreement against the pointwise path
# (eval/listwise_compare.py). Gated by STAGE5_LISTWISE_BATCH; pointwise stays
# the default and the fallback.
_LISTWISE_EVAL_PROMPT = """\
You are a recruiting assistant evaluating job listings against a candidate's profile.

## Candidate profile
{profile_llm_context}

## Job listings
{listings_block}

## Instructions
Evaluate EVERY listing above against the candidate profile. Judge each on its own merits;
use the others only to keep your confidence scale consistent across the batch.

For each listing return an object with:
- `id`: the listing id exactly as given.
- `verdict`: YES, MAYBE, or NO.
- `confidence`: integer 0-100.
- `reasoning`: one or two sentences on the match.
- `skills_extracted`: true unless the listing states no requirements at all.
- `matching_skills`: up to 3 requirements the candidate clearly has.
- `missing_skills`: up to 3 requirements stated in the listing that the candidate lacks.
- `job_summary`: 2 punchy sentences describing the company and the role.

Return ONLY a JSON object: {{"listings": [ ... ]}}
Every id given must appear exactly once.
"""


_EVALUATE_PROMPT = """\
You are a recruiting assistant evaluating a job listing against a candidate's profile.

## Candidate profile
{profile_llm_context}

## Job listing
Title: {title}
Company: {company}
Location: {location}
Salary: {salary}
Description: {description}

## Instructions
Evaluate how well this listing matches the candidate profile.

Expired-listing check (do this FIRST): if the description contains language indicating the listing is closed, expired, or no longer accepting applications — e.g. "No longer accepting applications", "This position has been filled", "Job posting has expired", "This role has been closed" — set verdict=NO and start the `reasoning` field with the literal prefix "listing expired:". The pipeline routes those into pipeline_status='expired' instead of 'passed' so they don't resurface. This applies ONLY to language about the application or posting itself; statements about company milestones like "we recently closed our seed round" or "we just hit Series B" are NOT expiration signals.

Scan the job description for required skills, domain expertise, or professional competencies.
Set `skills_extracted` to false ONLY when the listing provides no specific requirements at all
(e.g. a short recruiter ping with no job description). If the listing states ANY explicit
requirements — technical tools, domain knowledge, frameworks, certifications, or professional
skills — set `skills_extracted` to true and populate the lists:
- Start from what the JOB EXPLICITLY REQUIRES (not the candidate's profile).
- `matching_skills`: Top 3 requirements the candidate clearly has. Use concise labels that reflect
  the actual requirement type — technical (e.g. "Python", "SQL", "Kubernetes"), domain (e.g.
  "AI Safety", "Trust & Safety", "Healthcare Compliance"), or professional (e.g. "Product Strategy",
  "Data Science", "Cross-functional Leadership").
- `missing_skills`: Top 2-3 requirements explicitly stated in the JD that the candidate lacks.
  Do not invent requirements. Same label style as above.
- `job_summary`: 2 punchy sentences. (1) For startups or lesser-known companies: what they do
  and their stage (e.g. seed-stage, Series B, public enterprise); for household names (Google,
  Meta, Stripe, etc.): skip the generic description and name the specific team, product area,
  or initiative this role sits within. (2) The core responsibility of this role.

Respond with ONLY a valid JSON object (no markdown, no extra text):
{{"verdict": "YES or NO or MAYBE", "confidence": <integer 0-100>, "reasoning": "<one sentence>", "job_summary": "<2 sentences>", "skills_extracted": true/false, "matching_skills": ["skill1", "skill2", "skill3"], "missing_skills": ["skill4"]}}\
"""

_SINGLE_PROMPT = """\
You are a recruiting assistant. Below is the raw text extracted from a job-related email
and a candidate's profile.

## Candidate profile
{profile_llm_context}

## Email text
{extracted_email_text}

## Instructions
1. Identify each distinct job listing mentioned in the email.
   - For digest emails, there may be multiple listings (typically 3-8).
   - For recruiter outreach, the entire email is about one role.
   - If the email contains no identifiable job listings, respond with: NO_LISTINGS_FOUND

2. For each listing, extract whatever information is available:
   - Job title
   - Company name
   - Location
   - Salary (if mentioned)
   - Any notable details (company stage, team size, specific technologies)

3. Evaluate each listing against the candidate profile:
   - YES — strong match, candidate should apply
   - NO — poor match, do not surface
   - MAYBE — partial match, worth reviewing

4. Scan the job description for required skills, domain expertise, or professional competencies.
   Set skills_extracted to false ONLY when the listing provides no specific requirements at all
   (e.g. a short recruiter ping with no job description). If the listing states ANY explicit
   requirements — technical tools, domain knowledge, frameworks, or professional skills — set
   skills_extracted to true and populate the lists:
   - matching_skills: Top 3 job-required skills, domain expertise, or competencies the candidate has.
     Use concise labels: technical (e.g. "Python", "SQL"), domain (e.g. "AI Safety", "Trust & Safety",
     "Healthcare"), or professional (e.g. "Product Strategy", "Data Science", "Stakeholder Management").
   - missing_skills: Top 2-3 requirements explicitly stated in the JD that the candidate lacks.
     Do not invent requirements. Same label style as matching_skills.

5. Respond in this exact format, one block per listing:

LISTING:
title: [job title]
company: [company name]
location: [location or "not specified"]
salary: [salary range or "not listed"]
job_summary: [2 punchy sentences. (1) For startups or lesser-known companies: what they do and their stage (e.g. seed-stage, Series B, public); for household names (Google, Meta, Stripe, etc.): name the specific team, product area, or initiative. (2) Core responsibility of this role.]
verdict: [YES/NO/MAYBE]
confidence: [integer 0-100 indicating how confident you are in the verdict]
reason: [one sentence explaining the match decision]
skills_extracted: [true/false — true if the listing mentions any explicit requirements; false only for bare recruiter pings]
matching_skills: [comma-separated top 3 job-required skills the candidate also has, or "none"]
missing_skills: [comma-separated top 2-3 job-required skills the candidate lacks, or "none"]
links: [any relevant job URLs from the email, comma-separated, or "none"]
---

If the email is a recruiter outreach, also include:
recruiter_name: [name if identifiable]
recruiter_title: [title if identifiable]

Do not include any text outside of this format.\
"""

# ---------------------------------------------------------------------------
# Speculative JD synthesis prompt
# ---------------------------------------------------------------------------

_SPECULATIVE_JD_PROMPT = """\
You are a recruiting assistant. Using ONLY the context below from public job-board
search snippets, synthesise a concise job description for this role.
Only include details explicitly mentioned or strongly implied by the snippets.
Do not invent technologies, salary figures, or requirements not hinted at in the text.

Role: {title} at {company} ({location})

Search snippets:
{snippets}

Write 3-5 sentences covering: what the company does, the core responsibilities,
and any technical requirements mentioned in the snippets.
Output only the description text — no headers, no markdown, no preamble.\
"""

_SPECULATIVE_JD_PREFIX = (
    "[SPECULATIVE JD — synthesised from web context, not the original posting]\n\n"
)

_SPECULATIVE_JD_PROMPT_NO_CONTEXT = """\
You are a recruiting assistant. No public search results are available for this role.
Based solely on the company name, job title, and location below, write a brief,
cautious job description. Clearly frame any details as typical/inferred.

Role: {title} at {company} ({location})

Write 3-5 sentences covering: what the company likely does, probable core
responsibilities for this title, and typical requirements for this kind of role.
Output only the description text — no headers, no markdown, no preamble.\
"""

# ---------------------------------------------------------------------------
# Scrape validity judge prompt
# ---------------------------------------------------------------------------

_JUDGE_PROMPT = """\
You are a content-quality judge. Examine the following scraped web text and \
determine whether it contains an actual, substantive job description.

A VALID job description includes most of: job title, responsibilities, \
qualifications, company name, and application details.

INVALID content includes: CAPTCHA / challenge pages, cookie/privacy notices, \
"click here to view" redirect wrappers, login walls, generic site navigation, \
share-link metadata, or error pages.

Text to evaluate:
---
{scraped_text}
---

Respond with ONLY a JSON object, no markdown or other text:
{{"is_valid": true/false, "company_name": "extracted or empty string", \
"job_title": "extracted or empty string", \
"reason": "one-sentence explanation"}}\
"""

# ---------------------------------------------------------------------------
# Batch scrape validity judge prompt
# ---------------------------------------------------------------------------

_BATCH_JUDGE_PROMPT = """\
You are a content-quality judge. Below are multiple scraped web pages found via a job search.
Evaluate each option and identify the single best, most complete actual job description.

A VALID job description includes most of: job title, responsibilities, qualifications, \
company name, and application details.

INVALID content includes: CAPTCHA/challenge pages, cookie/privacy notices, \
"click here to view" redirect wrappers, login walls, generic site navigation, \
share-link metadata, or error pages.

{batch_options}

Respond with ONLY a JSON object, no markdown:
{{"winner_url": "<exact URL string of the best valid option, or null if all are invalid>", \
"reason": "one-sentence explanation"}}\
"""

# Aggregator domains to skip when searching for direct ATS links
_AGGREGATOR_DOMAINS = frozenset({
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
    "careerbuilder.com", "simplyhired.com", "jooble.org", "talent.com",
    "linkedin.com", "builtin.com", "adzuna.com",
    # Added after observing the URL healer (Stage 3) accept these domains as
    # canonical and surface unrelated postings in the digest. See
    # plans/autopilot_updates.md Fix 2b for evidence.
    "thehomebase.ai", "goremotejob.com", "careers4graduates.com",
    "choppingblock.ai",
})

# Tracking/redirect domains that serve dead links — discard immediately, never save to DB
_TRACKING_DOMAINS = frozenset({
    "googleapis.com",   # covers notifications.googleapis.com and all other subdomains
})

# Location values that mean "we don't know" — treated as null in Stage 2 validation
_NULL_LOCATION_VALUES = frozenset({
    "not specified", "not stated", "not listed", "unknown",
    "n/a", "none", "unspecified", "",
})


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

# Consecutive anchor rejections before the anchor is switched off for the rest
# of a run. A reshuffle only recovers composition-dependent drift; past this,
# the run is paying listwise + retry + pointwise per listing to learn nothing.
_MAX_CONSECUTIVE_REJECTIONS = 2


def _ordering_score(item: dict) -> int:
    """Collapse (verdict, confidence) into one signed desirability scale.

    `confidence` alone cannot be ordered across verdicts: it measures
    certainty in the verdict given, so a confident NO and a confident YES both
    score ~95 while sitting at opposite ends of desirability. NO maps negative,
    MAYBE to the middle, YES positive — the ordering the ranking surfaces
    actually care about.
    """
    try:
        conf = int(item.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0
    verdict = str(item.get("verdict", "") or "").strip().upper()
    if verdict == "NO":
        return -conf
    if verdict == "YES":
        return conf
    return 0  # MAYBE, or anything unparseable: neither endorsed nor rejected


def get_openrouter_config() -> tuple[str, str]:
    """Load OpenRouter configuration from environment.

    Returns (api_key, model).
    """
    load_dotenv()
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    model = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite")
    return api_key, model


def listwise_batch_size() -> int:
    """Listings per Stage 5 call. 0 or 1 keeps the pointwise path (default).

    Batching trades blast radius for cost: one malformed response affects a
    whole batch, so `prescore_batch` parses per item and anything missing
    falls back to a pointwise call rather than being lost.
    """
    raw = os.getenv("STAGE5_LISTWISE_BATCH", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("STAGE5_LISTWISE_BATCH=%r is not an integer; ignoring", raw)
        return 0


def noise_floor_pct() -> int:
    """Confidence below which a Stage 5 survivor is discarded as noise (M-6).

    `CONFIDENCE_THRESHOLD` was always a blunt noise filter, not a calibrated
    probability — raising it was how a user compensated for pointwise scoring
    being over-optimistic. Under rank-based selection the *ranking* does the
    quality work, so this only needs to strip obvious junk; a high value here
    starves the pool the ranker chooses from.

    Reads `NOISE_FLOOR_PCT`, else falls back to `CONFIDENCE_THRESHOLD` so
    existing configs keep working unchanged.
    """
    raw = os.getenv("NOISE_FLOOR_PCT", "").strip()
    if raw:
        try:
            return max(0, min(100, int(raw)))
        except ValueError:
            logger.warning("NOISE_FLOOR_PCT=%r is not an integer; ignoring", raw)
    return int(round(get_confidence_threshold() * 100))


def anchor_enabled() -> bool:
    """Whether each listwise batch carries a calibration anchor (M-5)."""
    return os.getenv("STAGE5_ANCHOR", "true").strip().lower() in ("1", "true", "yes")


def get_confidence_threshold() -> float:
    """Read CONFIDENCE_THRESHOLD from env, clamped to [0.0, 1.0].

    Returns 0.5 if unset or unparseable.
    """
    raw = os.getenv("CONFIDENCE_THRESHOLD", "0.5").strip()
    try:
        val = float(raw)
    except ValueError:
        logger.warning("CONFIDENCE_THRESHOLD=%r is not a number; using 0.5", raw)
        return 0.5
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


_DEPRECATED_ENSEMBLE_WARNED = False


def _warn_deprecated_ensemble_env() -> None:
    """Log a one-time warning if legacy ensemble env vars are set."""
    global _DEPRECATED_ENSEMBLE_WARNED
    if _DEPRECATED_ENSEMBLE_WARNED:
        return
    legacy = [k for k in ("OPENROUTER_ENSEMBLE_MODELS", "JD_REJECTION_MODE") if os.getenv(k)]
    if legacy:
        logger.warning(
            "Deprecated env vars set: %s. Ensemble scoring has been removed; "
            "use CONFIDENCE_THRESHOLD instead (see .env.example and docs/MODELS.md).",
            ", ".join(legacy),
        )
        _DEPRECATED_ENSEMBLE_WARNED = True


# ---------------------------------------------------------------------------
# Scrape validity judge
# ---------------------------------------------------------------------------

def evaluate_scrape_validity(
    text: str,
    client: openai.OpenAI,
    model: str,
) -> dict:
    """Ask the LLM whether *text* is a real job description.

    Returns dict with keys:
        is_valid (bool), company_name (str), job_title (str), reason (str)
    """
    # Truncate to ~3000 chars — enough for the judge, saves tokens
    snippet = text[:3000]
    prompt = _JUDGE_PROMPT.format(scraped_text=snippet)
    response = _call_openrouter(client, model, prompt, json_mode=True, stage="stage3_validate")
    raw = response["text"]
    try:
        # Attempt to extract JSON from the response
        match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            data = json.loads(raw)
        return {
            "is_valid": bool(data.get("is_valid", False)),
            "company_name": str(data.get("company_name", "")),
            "job_title": str(data.get("job_title", "")),
            "reason": str(data.get("reason", "")),
        }
    except (json.JSONDecodeError, ValueError):
        logger.warning("Judge returned unparseable response: %s", raw[:200])
        return {"is_valid": False, "company_name": "", "job_title": "", "reason": "unparseable judge response"}


def _is_aggregator_url(url: str) -> bool:
    """Return True if the URL belongs to a known job aggregator."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        host = host.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in _AGGREGATOR_DOMAINS)
    except Exception:
        return False


def _is_tracking_url(url: str) -> bool:
    """Return True if the URL is a dead tracking/redirect link that should be discarded.

    These URLs (e.g. notifications.googleapis.com, google.com/url?q=...) are
    unscrapable even by humans and must never be saved to the database.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        # Known tracking subdomains (e.g. notifications.googleapis.com)
        if any(host == d or host.endswith("." + d) for d in _TRACKING_DOMAINS):
            return True
        # Google URL redirect service: google.com/url?q=...
        if host == "google.com" and parsed.path.rstrip("/").startswith("/url"):
            return True
        return False
    except Exception:
        return False


def _build_ddg_search_url(anchor) -> str:
    """Build a DuckDuckGo safe-search URL for a job listing.

    Used as a last-resort fallback when DDGS returns no canonical URL.
    The URL is human-readable and useful for manual lookup.
    """
    from urllib.parse import urlencode
    parts = [p for p in [anchor.company, anchor.title, anchor.location] if p]
    query = " ".join(parts)
    return "https://duckduckgo.com/?" + urlencode({"q": query})


def _search_duckduckgo_for_listing(query: str, max_results: int = 5) -> list[str]:
    """Search DuckDuckGo and return non-aggregator URLs."""
    return [r["href"] for r in _search_duckduckgo_results(query, max_results)]


def _search_duckduckgo_results(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo and return full result dicts (href, title, body).

    Results from known aggregator domains are filtered out.  Returns [] on
    any error so callers can treat an empty list as a total search failure.
    """
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [r for r in results if r.get("href") and not _is_aggregator_url(r["href"])]
    except Exception:
        logger.warning("DuckDuckGo search failed", exc_info=True)
        return []


def _clean_source_board(raw: str) -> str:
    """Normalise an LLM-extracted source_board value into a bare, space-free domain.

    The LLM is instructed to output e.g. "talent.com" but sometimes includes
    the literal "via " prefix or omits the TLD.  Examples:

        "talent.com"    → "talent.com"
        "via Talent.com"→ "talent.com"
        "jobleads"      → "jobleads.com"
        "via JobLeads"  → "jobleads.com"
        "job leads"     → "jobleads.com"
        "none" / ""     → ""
    """
    raw = raw.strip().lower()
    if not raw or raw == "none":
        return ""
    raw = re.sub(r"^via\s+", "", raw).strip()   # strip "via " prefix
    raw = raw.replace(" ", "")                   # collapse any remaining spaces
    if not raw:
        return ""
    if "." not in raw:
        raw += ".com"                             # add TLD if missing
    return raw


def _scrape_url(url: str) -> str | None:
    """Scrape a URL with trafilatura, fail-fast (5s timeout, no retries).

    Returns extracted text (≥100 chars) or None on any failure, including
    429/503 anti-bot responses, timeouts, or unscrapable content.

    When ``IPROYAL_USERNAME`` / ``IPROYAL_PASSWORD`` are configured, the
    request is routed through a sticky residential proxy via
    ``src.proxy_manager.get_default_proxy_manager``. A 403/429/999 response
    triggers an immediate session rotation so the next caller gets a fresh
    exit IP.
    """
    try:
        import requests
        import trafilatura
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        from src.proxy_manager import get_default_proxy_manager

        proxy_mgr = get_default_proxy_manager()
        proxies = proxy_mgr.proxies_dict()

        session = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(total=0))
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        try:
            response = session.get(
                url, timeout=5, verify=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; apply-daemon/1.0)"},
                proxies=proxies or None,
            )
        except Exception:
            logger.debug("HTTP fetch failed for %s", url)
            return None

        proxy_mgr.report_status(response.status_code)

        if response.status_code in (429, 503):
            logger.debug("Anti-bot response %d for %s — skipping", response.status_code, url)
            return None

        html = response.text
        if not html:
            return None
        text = trafilatura.extract(html) or ""
        return text if len(text.strip()) >= 100 else None
    except Exception:
        logger.debug("Scrape failed for %s", url, exc_info=True)
        return None


def _anchor_is_valid(anchor: ExtractedListing) -> bool:
    """Stage 2: return True only if all required anchor fields are present and non-null.

    Hard-stops on any anchor missing company, job title, or a meaningful location.
    """
    if not anchor.title or anchor.title.lower().strip() in {"unknown", ""}:
        return False
    if not anchor.company or anchor.company.lower().strip() in {"unknown", ""}:
        return False
    if not anchor.location or anchor.location.lower().strip() in _NULL_LOCATION_VALUES:
        return False
    return True


def _post_escalation(company: str, title: str, original_url: str) -> None:
    """Post a Slack escalation message when agentic healing fails."""
    from src.notifications import _get_slack_config, _import_slack_app

    token, channel = _get_slack_config()
    if not token or not channel:
        logger.warning("Slack not configured — cannot escalate blocked triage")
        return

    url_line = f"\n:link: Original URL: {original_url}" if original_url else ""
    text = (
        f":rotating_light: *Triage Blocked:* Could not resolve actual job listing "
        f"for `{company} — {title}`. Automated web search failed.{url_line}\n"
        f":point_right: Please provide manual triage using: "
        f"`!triage [raw job text or direct ATS URL]`"
    )
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f":mag: Company: *{company}*  |  Title: *{title}*"},
            ],
        },
    ]

    try:
        app = _import_slack_app(token)
        app.client.chat_postMessage(
            channel=channel,
            text=f"Triage Blocked: {company} — {title}",
            blocks=blocks,
        )
        logger.info("Posted triage escalation for %s — %s", company, title)
    except Exception:
        logger.error("Failed to post escalation to Slack", exc_info=True)


# ---------------------------------------------------------------------------
# Verdict / confidence scoring
# ---------------------------------------------------------------------------

def auto_match_cutoff(confidence_threshold: float) -> int:
    """Confidence percentage at or above which a YES verdict is AUTO_MATCH.

    AUTO_MATCH triggers at max(threshold, 0.8). Threshold is a fraction (0.0–1.0).
    """
    return int(round(max(confidence_threshold, 0.8) * 100))


def _consensus_label(
    verdict: str,
    confidence: int,
    confidence_threshold: float,
) -> str:
    """Display label for the digest.

    - YES with confidence >= auto_match_cutoff → AUTO_MATCH
    - YES below that cutoff                    → NEEDS_REVIEW
    - Everything else                          → STANDARD
    """
    if verdict == "YES" and confidence >= auto_match_cutoff(confidence_threshold):
        return "AUTO_MATCH"
    if verdict == "YES":
        return "NEEDS_REVIEW"
    return "STANDARD"


# ---------------------------------------------------------------------------
# Extracted listing (intermediate representation before scoring)
# ---------------------------------------------------------------------------

@dataclass
class ExtractedListing:
    """A listing extracted from email text before evaluation scoring."""

    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    job_summary: str = ""
    description: str = ""
    source_board: str = ""  # job board domain from "via [Board]" in Google Jobs emails
    links: list[str] = field(default_factory=list)
    recruiter_name: str | None = None
    recruiter_title: str | None = None
    date_posted: str = ""  # ISO date when the role was posted, when known


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class TriageSession:
    """Reusable session for batching triage requests.

    Keeps a single OpenAI client open across the full batch for connection reuse.
    """

    def __init__(
        self,
        profile_llm_context: str,
        model: str | None = None,
        confidence_threshold: float | None = None,
        bypass_rejection: bool = False,
        stage1_model: str | None = None,
    ):
        _warn_deprecated_ensemble_env()
        env_api_key, env_model = get_openrouter_config()
        self.api_key = env_api_key
        self.model = model or env_model
        # Stage 1 extraction model — separate fast/cheap model for extraction.
        # Explicit override (e.g. eval's --stage1-model) wins over the env slot
        # so a "nano vs. flash-lite for Stage 1" comparison is possible.
        self.stage1_model = stage1_model or os.getenv(
            "OPENROUTER_STAGE1_MODEL", "openai/gpt-5.4-nano"
        )
        # Max tokens for Stage 5 evaluation responses
        self.max_tokens = int(os.getenv("OPENROUTER_NUM_PREDICT", "1000"))
        self.profile_llm_context = profile_llm_context
        self._client: openai.OpenAI | None = None
        self.confidence_threshold = (
            confidence_threshold if confidence_threshold is not None
            else get_confidence_threshold()
        )
        self.last_failure_reason: str | None = None  # set when triage_email returns []
        # When True, the confidence-threshold rejection is bypassed so the caller
        # always receives the listing regardless of confidence. Used for explicit
        # user-initiated !triage commands so the user sees the verdict + reasoning
        # instead of a silent drop.
        self.bypass_rejection = bypass_rejection
        # M-4: verdicts pre-computed by prescore_batch, keyed by listing
        # identity. A miss simply means a pointwise call, so an empty or
        # partial cache is always safe.
        self._listwise_scores: dict[str, dict] = {}

    def __enter__(self):
        self._client = openai.OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.api_key,
        )
        return self

    def __exit__(self, *args):
        self._client = None

    def _stage3_scrape_and_heal(
        self,
        anchor: ExtractedListing,
        source_text: str,
        source_links: list[str],
        source_is_already_scraped: bool = False,
        classification: str = "",
    ) -> tuple[str, list[str], bool]:
        """Stage 3: obtain the best available job description for *anchor*.

        Uses anchored company/title/location for any DuckDuckGo search —
        never relies on the judge's guesses from invalid content.

        Args:
            anchor: Stage 1 extraction result (company/title/location are known).
            source_text: original source (email body or already-scraped page).
            source_links: links from the source for this anchor.
            source_is_already_scraped: when True, *source_text* is the scraped
                content of the URL the user provided (e.g. via !triage URL).
                Judge it directly instead of re-scraping.
            classification: email classification (RECRUITER_OUTREACH, JOB_DIGEST, etc.).
                Used to determine whether to use source text or DDGS heal when no
                job-specific URL is available.

        Returns:
            (job_desc_text, effective_links, ok)
            ok=False means all attempts failed; the caller should skip this anchor.
        """
        original_url = anchor.links[0] if anchor.links else (source_links[0] if source_links else "")

        # ----------------------------------------------------------------
        # Path A: source_text IS already the scraped content (!triage URL)
        # ----------------------------------------------------------------
        if source_is_already_scraped:
            verdict = evaluate_scrape_validity(source_text, self._client, self.model)
            if verdict["is_valid"]:
                logger.info("Stage 3: scraped content is valid — %s", verdict["reason"])
                return source_text, source_links, True

            logger.warning(
                "Stage 3: scraped content INVALID — %s; healing with anchored query",
                verdict["reason"],
            )
            return self._ddgs_heal(anchor, source_text, source_links, original_url)

        # ----------------------------------------------------------------
        # Path B: pipeline email — try scraping anchor's URL(s)
        # ----------------------------------------------------------------
        non_aggregator_links = [
            u for u in anchor.links
            if not _is_aggregator_url(u) and not _is_tracking_url(u)
        ]
        if not non_aggregator_links:
            if classification == "RECRUITER_OUTREACH":
                # Email body IS the job description for recruiter outreach.
                logger.info(
                    "Stage 3: no URL for '%s at %s' (recruiter outreach) — using source text",
                    anchor.title, anchor.company,
                )
                return source_text, source_links, True
            # Digest/manual: no valid job URL found — attempt DDGS heal.
            # Hallucinated anchors (no real URL) will fail DDGS and be dropped (ok=False).
            logger.info(
                "Stage 3: no scrapable URL for '%s at %s' — attempting DDGS heal",
                anchor.title, anchor.company,
            )
            return self._ddgs_heal(anchor, source_text, source_links, original_url=None)

        # Try the first non-aggregator URL
        url = non_aggregator_links[0]
        logger.info("Stage 3: scraping URL for '%s at %s': %s", anchor.title, anchor.company, url)
        scraped = _scrape_url(url)
        if scraped:
            verdict = evaluate_scrape_validity(scraped, self._client, self.model)
            if verdict["is_valid"]:
                logger.info("Stage 3: direct scrape valid — %s", verdict["reason"])
                return scraped, [url], True

            logger.warning(
                "Stage 3: direct scrape INVALID (%s) — healing with anchored query",
                verdict["reason"],
            )
        else:
            logger.warning("Stage 3: scrape returned no content for %s — healing", url)

        return self._ddgs_heal(anchor, source_text, source_links, original_url)

    def _synthesize_speculative_jd(self, anchor: ExtractedListing) -> str:
        """Gather DDGS context in three escalating passes, then synthesise a JD.

        This method is infallible — it always returns a non-empty string.

        Pass 1: company name only          → company overview snippets
        Pass 2: company + title            → role-specific snippets
        Pass 3: company + title + location → geo-targeted snippets

        Results are deduplicated by href.  If DDGS returns nothing or snippets
        have no body text, the LLM is prompted to infer from the anchor fields
        alone.  If the LLM call itself fails, a minimal placeholder is returned.
        """
        seen_hrefs: set[str] = set()
        all_results: list[dict] = []

        queries = [
            anchor.company,
            f"{anchor.company} {anchor.title}",
        ]
        if anchor.location:
            queries.append(f"{anchor.company} {anchor.title} {anchor.location}")

        for q in queries:
            for r in _search_duckduckgo_results(q.strip(), max_results=3):
                href = r.get("href", "")
                if href and href not in seen_hrefs:
                    seen_hrefs.add(href)
                    all_results.append(r)

        snippets = "\n\n".join(
            f"[{i+1}] {r.get('title', '')}\n{r.get('body', '')}"
            for i, r in enumerate(all_results[:9])
            if r.get("body")
        )

        if snippets.strip():
            prompt = _SPECULATIVE_JD_PROMPT.format(
                title=anchor.title,
                company=anchor.company,
                location=anchor.location or "not specified",
                snippets=snippets,
            )
            logger.debug(
                "Stage 3: synthesising speculative JD for '%s at %s' (%d snippets)",
                anchor.title, anchor.company, len(all_results),
            )
        else:
            prompt = _SPECULATIVE_JD_PROMPT_NO_CONTEXT.format(
                title=anchor.title,
                company=anchor.company,
                location=anchor.location or "not specified",
            )
            logger.debug(
                "Stage 3: no DDGS snippets for '%s at %s' — synthesising from anchor alone",
                anchor.title, anchor.company,
            )

        try:
            resp = _call_openrouter(
                self._client, self.model, prompt, max_tokens=500, stage="stage3_synth",
            )
            text = resp.get("text", "").strip()
            if text and len(text.split()) >= 10:
                logger.info(
                    "Stage 3: speculative JD synthesised for '%s at %s' (%d words)",
                    anchor.title, anchor.company, len(text.split()),
                )
                return _SPECULATIVE_JD_PREFIX + text
            logger.warning(
                "Speculative JD LLM response too short for '%s at %s' — using placeholder",
                anchor.title, anchor.company,
            )
        except Exception:
            logger.error(
                "Speculative JD LLM call failed for '%s at %s' — using placeholder",
                anchor.title, anchor.company, exc_info=True,
            )

        # Absolute fallback — cannot fail
        return (
            _SPECULATIVE_JD_PREFIX
            + f"{anchor.title} position at {anchor.company}"
            + (f" ({anchor.location})" if anchor.location else "")
            + ". No additional details could be retrieved from public sources."
        )

    def _ddgs_heal(
        self,
        anchor: ExtractedListing,
        fallback_text: str,
        fallback_links: list[str],
        original_url: str | None,
    ) -> tuple[str, list[str], bool]:
        """Search DuckDuckGo for the job posting using concurrent batch evaluation.

        Flow:
        1. Random sleep (2–7 s) to reduce rate-limit pressure.
        2. Search DDGS with anchored query (site: operator if source_board known).
           Filter out aggregator/tracking URLs from results.
        3. Scrape surviving URLs concurrently via ThreadPoolExecutor. Discard
           any that return None.
        4. Truncate each scraped text to 3,000 chars, then send a single batch
           LLM call asking the judge to pick the best valid job description.
        5. If the judge selects a winner, return it immediately (ok=True).
           If the judge rejects all options, or if scraping yielded no results,
           drop the listing (ok=False) — no speculative synthesis fallback.

        Anti-hallucination guarantee: the DDGS query is always built from
        Stage 1 anchors (company/title/location), never from judge guesses.
        """
        import random as _random
        from concurrent.futures import ThreadPoolExecutor, as_completed

        delay = _random.uniform(2.0, 7.0)
        logger.debug("Stage 3: DDGS rate-limit guard — sleeping %.1fs", delay)
        time.sleep(delay)

        if anchor.source_board:
            query = f"{anchor.company} {anchor.title} (site:{anchor.source_board})"
        else:
            query = f"{anchor.company} {anchor.title} {anchor.location}"
        logger.info("Stage 3 DDGS heal — searching: %s", query)

        search_results = _search_duckduckgo_results(query, max_results=5)
        candidate_urls = [
            r["href"] for r in search_results
            if r.get("href")
            and not _is_aggregator_url(r["href"])
            and not _is_tracking_url(r["href"])
        ]

        if not candidate_urls:
            logger.warning(
                "Stage 3: DuckDuckGo returned no usable results for '%s at %s' — dropping",
                anchor.title, anchor.company,
            )
            return "", [], False

        # Concurrent scraping — fail-fast per URL, discard None/empty results
        scraped: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(len(candidate_urls), 5)) as pool:
            future_map = {pool.submit(_scrape_url, u): u for u in candidate_urls}
            for fut in as_completed(future_map):
                url = future_map[fut]
                try:
                    text = fut.result()
                    if text:
                        scraped[url] = text
                except Exception:
                    logger.debug("Concurrent scrape exception for %s", url, exc_info=True)

        if not scraped:
            logger.info(
                "Stage 3: all concurrent scrapes empty for '%s at %s' — dropping",
                anchor.title, anchor.company,
            )
            return "", [], False

        # Build batch evaluation prompt — truncate each block to 3,000 chars
        parts = [
            f"[OPTION {i}: {url}]\n{text[:3000]}"
            for i, (url, text) in enumerate(scraped.items(), 1)
        ]
        batch_options = "\n\n".join(parts)
        prompt = _BATCH_JUDGE_PROMPT.format(batch_options=batch_options)

        try:
            response = _call_openrouter(
                self._client, self.model, prompt, max_tokens=200, json_mode=True,
                stage="stage3_batch_judge",
            )
            raw = response["text"]
            match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
            data = json.loads(match.group() if match else raw)

            returned_url = data.get("winner_url")
            reason = data.get("reason", "")

            if isinstance(returned_url, str) and returned_url and returned_url != "null":
                # Exact match first; fall back to substring match for minor LLM rewrites
                winner_url = returned_url if returned_url in scraped else next(
                    (u for u in scraped if returned_url in u or u in returned_url), None
                )
                if winner_url:
                    logger.info(
                        "Stage 3: batch judge selected winner %s — %s", winner_url, reason,
                    )
                    return scraped[winner_url], [winner_url], True

            logger.info(
                "Stage 3: batch judge rejected all options for '%s at %s' (%s) — dropping",
                anchor.title, anchor.company, reason,
            )
        except Exception:
            logger.warning(
                "Stage 3: batch judge LLM call failed for '%s at %s' — dropping",
                anchor.title, anchor.company, exc_info=True,
            )

        return "", [], False

    def triage_email(
        self,
        email_text: str,
        email_links: list[str],
        classification: str,
        source: str,
        source_is_scraped_url: bool = False,
        duplicate_check=None,
    ) -> list[JobListing]:
        """Orchestrate the full five-stage triage pipeline for one source text.

        Stage 1 — Anchor Extraction: LLM extracts all listings from source text
            (no judge, no scraping). num_predict=4000 to handle 10+ listing digests.
        Stage 2 — Validation: drop any anchor missing company, title, or location.
            Sets last_failure_reason if all anchors are dropped.
        Stage 3 — Scrape + Heal: per anchor, obtain the best job description body.
            DDGS search always uses Stage 1 anchors, never judge guesses.
        Stage 5 — Ensemble Scoring: evaluate each anchor against the profile.

        Args:
            email_text: raw email body (pipeline) or already-scraped page (!triage URL).
            email_links: links extracted from the email or [source_url] for !triage.
            classification: JOB_DIGEST / RECRUITER_OUTREACH / GOOGLE_ALERT / MANUAL_TRIAGE.
            source: label for the originating system (e.g. "linkedin", "manual").
            source_is_scraped_url: when True, email_text is the pre-scraped content
                of a URL (from !triage); Stage 3 judges it directly instead of
                scraping anchor links from it.
            duplicate_check: optional callable(title, company) -> bool. When provided,
                anchors that return True are skipped before Stage 3 scraping and
                Stage 5 LLM scoring, saving API credits.
        """
        if self._client is None:
            raise RuntimeError("TriageSession must be used as a context manager")

        self.last_failure_reason = None

        # ----------------------------------------------------------------
        # Stage 1: Anchor extraction — LLM on source text, no judge
        # ----------------------------------------------------------------
        extracted = self._run_stage1_extraction(email_text, email_links)
        if not extracted:
            logger.info("Stage 1: no listings found in source text")
            return []

        logger.info("Stage 1: extracted %d anchor listing(s)", len(extracted))

        # ----------------------------------------------------------------
        # Stage 2: Validate anchors — hard stop if required fields missing
        # ----------------------------------------------------------------
        valid_anchors = [a for a in extracted if _anchor_is_valid(a)]
        dropped = len(extracted) - len(valid_anchors)
        if dropped:
            logger.warning(
                "Stage 2: dropped %d/%d anchor(s) with missing company/title/location",
                dropped, len(extracted),
            )
        if not valid_anchors:
            logger.warning("Stage 2: no valid anchors — aborting triage")
            self.last_failure_reason = "stage2_missing_required_fields"
            return []

        logger.info("Stage 2: %d valid anchor(s) passed", len(valid_anchors))

        # ----------------------------------------------------------------
        # Stage 3 + Stage 5: per anchor — scrape/heal then evaluate
        # ----------------------------------------------------------------
        final_listings: list[JobListing] = []

        for anchor in valid_anchors:
            # Pre-Stage-3/5 dedup: skip anchors already in the database to
            # avoid wasting OpenRouter API credits on known listings.
            if duplicate_check and duplicate_check(anchor.title, anchor.company):
                logger.info(
                    "Dedup (pre-Stage5): skipping '%s at %s' — already in DB",
                    anchor.title, anchor.company,
                )
                continue

            job_text, job_links, ok = self._stage3_scrape_and_heal(
                anchor, email_text, email_links,
                source_is_already_scraped=source_is_scraped_url,
                classification=classification,
            )
            if not ok:
                logger.warning(
                    "Stage 3: no valid job description for '%s at %s' — skipping",
                    anchor.title, anchor.company,
                )
                continue

            result = self._stage5_evaluate_anchor(
                anchor, job_text, job_links, classification, source,
            )
            if result is not None:
                final_listings.append(result)

        return final_listings

    # ------------------------------------------------------------------
    # Stage 1 helper
    # ------------------------------------------------------------------

    def _run_stage1_extraction(
        self,
        email_text: str,
        email_links: list[str],
    ) -> list[ExtractedListing]:
        """Run _EXTRACT_PROMPT on source text with max_tokens=4000.

        Uses the dedicated Stage 1 model (OPENROUTER_STAGE1_MODEL) if configured,
        otherwise falls back to the main model.
        """
        extractor_model = self.stage1_model
        prompt = _EXTRACT_PROMPT.format(extracted_email_text=email_text)

        start = time.monotonic()
        response = _call_openrouter(
            self._client, extractor_model, prompt,
            max_tokens=4000,  # digest emails may have 10+ listings
            stage="stage1",
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "Stage 1 extraction: model=%s, tokens=%d, latency=%dms",
            extractor_model, response["tokens"], latency_ms,
        )

        if "NO_LISTINGS_FOUND" in response["text"]:
            return []
        return _parse_extraction_response(response["text"], email_links)

    # ------------------------------------------------------------------
    # Stage 5 helper
    # ------------------------------------------------------------------

    def _stage5_evaluate_anchor(
        self,
        anchor: ExtractedListing,
        job_text: str,
        job_links: list[str],
        classification: str,
        source: str,
    ) -> JobListing | None:
        """Evaluate one validated anchor against the candidate profile.

        Runs the configured Stage 5 model once and applies the confidence
        threshold. Listings scored below ``self.confidence_threshold`` are
        auto-rejected unless ``bypass_rejection`` is set.

        The description fed to _EVALUATE_PROMPT is the best available:
        - If Stage 3 produced a richer scraped page (job_text), use up to 2000 chars.
        - Otherwise fall back to anchor.description (the email excerpt from Stage 1).
        """
        # Choose the richest description available for the evaluation prompt
        if job_text and len(job_text) > len(anchor.description or "") + 100:
            description = job_text[:FULL_JOB_DESCRIPTION_CHARS].strip()
        else:
            description = (anchor.description or job_text[:1000]).strip()

        listing = self._evaluate_single(
            anchor, description, job_text, job_links, classification, source,
            self.model,
        )
        if listing is None:
            return None

        # Fix 4a — Stage 5 expired detection: return the listing so it persists
        # with pipeline_status='expired'. This bypasses the standard NO rejection
        # path so the smart-upsert pass_window_days cooldown can prevent the same
        # dead row from being re-scored on every cycle.
        if listing.final_status == "expired":
            return listing

        # NO is always rejected — confidence is irrelevant. A NO at 95% is still a NO.
        # YES / MAYBE survive only when confidence meets the threshold.
        cutoff_pct = int(round(self.confidence_threshold * 100))
        rejected = listing.verdict == "NO" or listing.confidence < cutoff_pct
        if rejected:
            if self.bypass_rejection:
                logger.info(
                    "  Rejection bypassed (manual !triage) for %s at %s — "
                    "verdict=%s conf=%d%% threshold=%d%%",
                    anchor.title, anchor.company,
                    listing.verdict, listing.confidence, cutoff_pct,
                )
            else:
                logger.info(
                    "  Rejected %s at %s — verdict=%s conf=%d%% threshold=%d%%",
                    anchor.title, anchor.company,
                    listing.verdict, listing.confidence, cutoff_pct,
                )
                return None
        return listing

    def evaluate_listing(
        self,
        anchor: ExtractedListing,
        job_text: str,
        job_links: list[str],
        classification: str = "JOB_DIGEST",
        source: str = "jobspy",
    ) -> "JobListing | None":
        """Public API for evaluating a pre-structured anchor (Track A / JobSpy path).

        Skips Stages 1–3 and runs Stage 5 directly. Use this when structured
        job data is already available (company, title, location, full description)
        and LLM scoring against the candidate profile is all that's needed.
        """
        return self._stage5_evaluate_anchor(
            anchor, job_text, job_links, classification, source,
        )

    @staticmethod
    def _batch_key(anchor: "ExtractedListing") -> str:
        """Identity for cache lookup. Title+company is what dedup already
        treats as a listing's identity, so it is consistent with the rest of
        the pipeline."""
        return f"{(anchor.title or '').strip().lower()}|{(anchor.company or '').strip().lower()}"

    # M-5.1: a synthetic anchor that needs no trust and no ledger. Deliberately
    # absurd for this profile, so any competent scorer ranks it last — which is
    # what makes it a usable *ordering* reference from a cold start. Its
    # description is deliberately as long as a real listing's: a scorer that
    # discounts thin evidence would rank a one-liner last for a reason that has
    # nothing to do with the match, and the ordering check would pass without
    # testing anything.
    _SYNTHETIC_WEAK = (
        "Line Cook (Seasonal)",
        "Harbourside Grill",
        "Harbourside Grill is a family-owned beachfront restaurant serving classic "
        "short-order breakfast and lunch from May through September. We are hiring a "
        "seasonal line cook to join our back-of-house team for the summer rush. You "
        "will work the flat-top and fryer stations, plate short-order breakfast "
        "dishes to spec, and keep your station stocked, clean, and inspection-ready "
        "through double-shift weekends. Responsibilities include prepping mise en "
        "place before service, executing tickets quickly and consistently during "
        "peak hours, rotating and labeling stock, following food-safety and allergen "
        "procedures, breaking down and deep-cleaning stations at close, and "
        "assisting with weekly inventory counts. Requirements: two or more summers "
        "of line experience in a high-volume kitchen; comfort working a busy "
        "flat-top; a current food-handler card or willingness to obtain one in the "
        "first week; ability to lift 20 kg, stand for eight-hour shifts, and work "
        "weekends and holidays without exception. No computer work is involved in "
        "this role. Pay is hourly plus a share of pooled tips, with a "
        "season-completion bonus paid after Labor Day. Housing is not provided; "
        "reliable transportation to the waterfront is required. This posting is for "
        "on-site work only. To apply, drop off a resume between 2 and 4 pm on "
        "weekdays or email the kitchen manager.",
    )

    def _select_anchor(self) -> tuple["ExtractedListing", str, str] | None:
        """Pick a calibration anchor: (listing, description, expected_verdict).

        Sourced from ``data/human_labels.jsonl`` — a listing the user actually
        saved or passed. That is the only verdict in the system not produced
        by a model: autopilot's post-research verdicts are contaminated
        (``process_queue._AUTO_PROMPT`` feeds the scorer the incumbent's own
        reasoning), so anchoring on them would measure agreement with a
        model rather than with the user.

        Prefers a recent decision, since the market moves. Returns None when
        no usable label exists, in which case batching proceeds unanchored.
        """
        from src.db import Database
        from src.human_labels import resolve_labels_path

        path = resolve_labels_path()
        if not path.exists():
            return None

        POSITIVE = {"save", "tailor", "applied", "interview"}
        NEGATIVE = {"pass", "rejected"}
        decisions: list[tuple[str, str]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                action = str(rec.get("human_reaction", "")).lower()
                job_id = rec.get("job_id")
                if not job_id:
                    continue
                if action in POSITIVE:
                    decisions.append((str(job_id), "YES"))
                elif action in NEGATIVE:
                    decisions.append((str(job_id), "NO"))
        except OSError:
            return None
        if not decisions:
            return None

        try:
            with Database() as db:
                for job_id, expected in reversed(decisions):   # most recent first
                    row = db.get_listing_by_id(job_id)
                    if row is None:
                        continue
                    # Full description, not job_summary. The anchor is scored
                    # alongside listings carrying ~2000 chars each; a ~290-char
                    # TL;DR is thin by a factor of seven, and a model that
                    # reads evidence density would discount the anchor for a
                    # reason that has nothing to do with the match.
                    description = job_description_text(row)
                    if not description:
                        continue
                    return (
                        ExtractedListing(
                            title=row["title"], company=row["company"],
                            location=row["location"] or "",
                            salary=row["salary"] or "",
                        ),
                        description,
                        expected,
                    )
        except Exception:
            logger.debug("Anchor selection failed; batching unanchored",
                         exc_info=True)
        return None

    def prescore_batch(self, items: list[tuple["ExtractedListing", str]]) -> int:
        """Score many listings in one call, caching results for later lookup.

        Returns how many were scored. Callers may pass everything they have;
        this chunks by ``STAGE5_LISTWISE_BATCH`` and is a no-op when that is
        unset, so enabling or disabling batching needs no caller change.

        Failure is always partial, never fatal: a batch that errors or returns
        malformed JSON simply leaves those listings uncached, and they take
        the pointwise path as though batching were off.
        """
        size = listwise_batch_size()
        if size < 2 or not items:
            return 0

        anchor = self._select_anchor() if anchor_enabled() else None
        if anchor is not None:
            logger.debug("Listwise anchor: '%s at %s' (expected %s)",
                         anchor[0].title, anchor[0].company, anchor[2])

        weak = None
        if anchor is not None:
            title, company, desc = self._SYNTHETIC_WEAK
            weak = (ExtractedListing(title=title, company=company,
                                     location="", salary=""), desc)

        scored = 0
        batches = 0
        incomplete = 0
        rejected = 0
        consecutive_rejections = 0
        retry: list[list] = []
        anchor_confidences: list[int] = []
        for start in range(0, len(items), size):
            chunk = items[start:start + size]
            batches += 1
            before = scored
            # The anchor and its weak counterpart ride in every batch,
            # formatted identically to the real listings so the model cannot
            # treat them specially. Together they cost two slots (~17% at
            # batch 10) and buy a per-batch calibration check: confidence is
            # meaningful *within* a batch, and without a fixed reference
            # nothing detects drift across batches.
            scoring = list(chunk)
            if anchor:
                scoring.append((anchor[0], anchor[1]))
                if weak:
                    scoring.append(weak)
            block = "\n".join(
                f"### id: {self._batch_key(a)}\n"
                f"Title: {a.title}\nCompany: {a.company}\n"
                f"Location: {a.location}\nSalary: {a.salary}\n"
                f"Description: {(desc or '')[:JOB_DESCRIPTION_CHARS]}\n"
                for a, desc in scoring
            )
            prompt = _LISTWISE_EVAL_PROMPT.format(
                profile_llm_context=self.profile_llm_context,
                listings_block=block,
            )
            try:
                resp = _call_openrouter(
                    self._client, self.model, prompt,
                    max_tokens=min(400 * len(scoring) + 500, 8192),
                    temperature=0.0, json_mode=True, stage="stage5_listwise",
                )
                text = (resp["text"] or "").strip()
                if text.startswith("```"):
                    text = text.strip("`").removeprefix("json").strip()
                data = json.loads(text)
            except Exception:
                logger.warning(
                    "Listwise batch of %d failed — those listings fall back "
                    "to pointwise", len(chunk), exc_info=True,
                )
                continue

            parsed = {
                str(i["id"]).strip().lower(): i
                for i in (data.get("listings") or [])
                if isinstance(i, dict) and i.get("id")
            }

            # M-5: the anchor is a listing whose correct verdict is known from
            # the user's own save/pass decisions — the one signal that is not
            # model-derived. If the batch misjudges it, the batch's whole
            # calibration is suspect, so its scores are discarded and every
            # listing in it takes the pointwise path.
            if anchor is not None:
                akey = self._batch_key(anchor[0])
                wkey = self._batch_key(weak[0]) if weak else None
                got = parsed.pop(akey, None)
                wgot = parsed.pop(wkey, None) if wkey else None
                if got is None:
                    logger.warning("Listwise batch %d omitted the anchor — "
                                   "calibration unverified, accepting anyway",
                                   batches)
                else:
                    try:
                        aconf = int(got.get("confidence", 0) or 0)
                        anchor_confidences.append(aconf)
                    except (TypeError, ValueError):
                        aconf = 0

                    # M-5.1: check ORDERING, not verdict. Verdict-match tested
                    # an absolute-scale judgement — exactly what M-6 says not
                    # to rely on. A batch that ranks a deliberately-weak
                    # listing above one the user actually saved is
                    # miscalibrated in the only way that matters for
                    # rank-based selection.
                    #
                    # Compare on the SIGNED scale. `confidence` is certainty in
                    # whatever verdict was given, not desirability: a line-cook
                    # posting is an obvious NO and scores 95-100 on it. Comparing
                    # raw confidence therefore rejected every batch — a
                    # confident NO always outscores a merely-strong YES — which
                    # is exactly what happened in production on 2026-08-02: four
                    # batches, four rejections, four retries into the same wall,
                    # and 40 listings paying for listwise *and* pointwise.
                    bad_order = False
                    if wgot is not None:
                        wscore = _ordering_score(wgot)
                        ascore = _ordering_score(got)
                        if anchor[2] == "YES" and wscore >= ascore:
                            bad_order = True
                            reason = (f"weak reference ranked {wscore:+d} >= "
                                      f"anchor {ascore:+d}")
                    if bad_order:
                        rejected += 1
                        consecutive_rejections += 1
                        logger.warning(
                            "Listwise batch %d REJECTED — %s. Retrying with a "
                            "reshuffled composition.", batches, reason,
                        )
                        retry.append(chunk)
                        # A reshuffle varies composition, which only helps when
                        # the miscalibration is composition-dependent. A run
                        # that rejects this consistently is failing for a
                        # reason the retry cannot reach, and every further
                        # anchored batch bills listwise + retry + pointwise for
                        # the same listings. Stop anchoring and let the rest of
                        # the run proceed unanchored — the documented
                        # no-usable-label behaviour.
                        if consecutive_rejections >= _MAX_CONSECUTIVE_REJECTIONS:
                            logger.error(
                                "Listwise anchor rejected %d batches in a row — "
                                "disabling the anchor for the rest of this run. "
                                "Calibration is unverified from here; check the "
                                "anchor listing and the ordering check.",
                                consecutive_rejections,
                            )
                            anchor = None
                            weak = None
                        continue
                    consecutive_rejections = 0

            for item in parsed.values():
                key = str(item["id"]).strip().lower()
                try:
                    self._listwise_scores[key] = {
                        "verdict": str(item.get("verdict", "MAYBE")).upper(),
                        "confidence": int(item.get("confidence", 0) or 0),
                        "reasoning": str(item.get("reasoning", "") or ""),
                        "skills_extracted": bool(item.get("skills_extracted", True)),
                        "matching_skills": item.get("matching_skills") or [],
                        "missing_skills": item.get("missing_skills") or [],
                        "job_summary": str(item.get("job_summary", "") or ""),
                        "model": self.model,
                        "_tokens": 0,  # batch cost is logged once, not per item
                    }
                    scored += 1
                except (TypeError, ValueError):
                    continue

            # Coverage is the metric the economics rest on. Omission is
            # stochastic (identical inputs at temperature=0 have returned
            # 24/24 and 14/24), so it has to be observable per batch rather
            # than inferred from a total — a bad stretch should be visible,
            # not silently eroding the saving.
            got = scored - before
            if got < len(chunk):
                incomplete += 1
                logger.warning(
                    "Listwise batch %d returned %d/%d listings — %d fall back "
                    "to pointwise", batches, got, len(chunk), len(chunk) - got,
                )

        # M-5.1: one reshuffled retry before surrendering to pointwise.
        # Measured drift is composition-dependent, so a rejected batch usually
        # succeeds with different neighbours — and a retry is far cheaper than
        # abandoning both the cost saving and the ranking benefit for the whole
        # batch. Exactly one attempt: a second would risk retry-looping on a
        # genuinely broken model.
        if retry:
            import random
            for chunk in retry:
                shuffled = list(chunk)
                random.shuffle(shuffled)
                scoring = list(shuffled)
                # anchor/weak may have been switched off by the circuit
                # breaker after this chunk was queued; retry unanchored rather
                # than not at all.
                if anchor:
                    scoring.append((anchor[0], anchor[1]))
                if weak:
                    scoring.append(weak)
                block = "\n".join(
                    f"### id: {self._batch_key(a)}\n"
                    f"Title: {a.title}\nCompany: {a.company}\n"
                    f"Location: {a.location}\nSalary: {a.salary}\n"
                    f"Description: {(desc or '')[:JOB_DESCRIPTION_CHARS]}\n"
                    for a, desc in scoring
                )
                try:
                    resp = _call_openrouter(
                        self._client, self.model,
                        _LISTWISE_EVAL_PROMPT.format(
                            profile_llm_context=self.profile_llm_context,
                            listings_block=block,
                        ),
                        max_tokens=min(400 * len(scoring) + 500, 8192),
                        temperature=0.0, json_mode=True,
                        stage="stage5_listwise_retry",
                    )
                    text = (resp["text"] or "").strip()
                    if text.startswith("```"):
                        text = text.strip("`").removeprefix("json").strip()
                    data = json.loads(text)
                except Exception:
                    logger.warning("Reshuffled retry failed — %d listing(s) "
                                   "fall back to pointwise", len(chunk),
                                   exc_info=True)
                    continue
                keys = {self._batch_key(a) for a, _ in chunk}
                recovered = 0
                for item in (data.get("listings") or []):
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    key = str(item["id"]).strip().lower()
                    if key not in keys:
                        continue
                    try:
                        self._listwise_scores[key] = {
                            "verdict": str(item.get("verdict", "MAYBE")).upper(),
                            "confidence": int(item.get("confidence", 0) or 0),
                            "reasoning": str(item.get("reasoning", "") or ""),
                            "skills_extracted": bool(item.get("skills_extracted", True)),
                            "matching_skills": item.get("matching_skills") or [],
                            "missing_skills": item.get("missing_skills") or [],
                            "job_summary": str(item.get("job_summary", "") or ""),
                            "model": self.model,
                            "_tokens": 0,
                        }
                        scored += 1
                        recovered += 1
                    except (TypeError, ValueError):
                        continue
                logger.info("Reshuffled retry recovered %d/%d listing(s)",
                            recovered, len(chunk))

        if anchor_confidences and len(anchor_confidences) > 1:
            spread = max(anchor_confidences) - min(anchor_confidences)
            log_fn2 = logger.warning if spread >= 15 else logger.info
            log_fn2(
                "Anchor confidence across %d batches: %s (spread %d) — this is "
                "cross-batch drift, measured directly",
                len(anchor_confidences), anchor_confidences, spread,
            )
        if rejected:
            logger.warning("%d batch(es) rejected on anchor mismatch", rejected)

        missed = len(items) - scored
        log_fn = logger.warning if missed else logger.info
        log_fn(
            "Listwise coverage: %d/%d listings (%.0f%%) in %d batch(es); "
            "%d incomplete, %d fall back to pointwise",
            scored, len(items), scored / len(items) * 100 if items else 0.0,
            batches, incomplete, missed,
        )
        return scored

    def _run_eval_prompt(
        self, anchor: ExtractedListing, description: str, model: str, temperature: float,
    ) -> dict:
        """Call _EVALUATE_PROMPT for one model; return parsed evaluation dict.

        Returns a cached listwise verdict when ``prescore_batch`` produced one
        for this listing (M-4), avoiding the per-listing call entirely.
        """
        cached = self._listwise_scores.pop(self._batch_key(anchor), None)
        if cached is not None:
            logger.debug("Stage 5: using listwise verdict for '%s at %s'",
                         anchor.title, anchor.company)
            return cached

        eval_prompt = _EVALUATE_PROMPT.format(
            profile_llm_context=self.profile_llm_context,
            title=anchor.title,
            company=anchor.company,
            location=anchor.location,
            salary=anchor.salary,
            description=description,
        )
        resp = _call_openrouter(
            self._client, model, eval_prompt,
            max_tokens=self.max_tokens,
            temperature=temperature,
            json_mode=True,
            stage="stage5",
        )
        evaluation = _parse_evaluation_json(resp["text"])
        evaluation["model"] = model
        evaluation["_tokens"] = resp["tokens"]
        return evaluation

    def _apply_recruiter_floor(
        self, verdict: str, classification: str, anchor: ExtractedListing,
    ) -> tuple[str, bool]:
        """Upgrade NO → MAYBE for recruiter outreach. Returns (verdict, overridden)."""
        if classification == "RECRUITER_OUTREACH" and verdict == "NO":
            logger.info(
                "  Recruiter override: %s at %s upgraded %s → MAYBE",
                anchor.title, anchor.company, verdict,
            )
            return "MAYBE", True
        return verdict, False

    def _evaluate_single(
        self,
        anchor: ExtractedListing,
        description: str,
        job_text: str,
        job_links: list[str],
        classification: str,
        source: str,
        model: str,
    ) -> JobListing | None:
        """Single-model evaluation via _EVALUATE_PROMPT."""
        start = time.monotonic()
        try:
            evaluation = self._run_eval_prompt(anchor, description, model, 0.0)
        except Exception:
            logger.error("Single eval failed for '%s at %s'", anchor.title, anchor.company,
                         exc_info=True)
            return None
        latency_ms = int((time.monotonic() - start) * 1000)
        tokens = evaluation.pop("_tokens", 0)

        verdict = evaluation["verdict"]
        verdict, recruiter_override = self._apply_recruiter_floor(verdict, classification, anchor)
        reason = evaluation["reasoning"]
        if recruiter_override:
            reason += " [System Override: Upgraded to MAYBE due to direct recruiter outreach]"

        # Fix 4a: when Stage 5 detects expired-listing language, route to
        # pipeline_status='expired' so the row gets the long pass-cooldown
        # instead of resurfacing as a regular NO.
        final_status = "triaged"
        if verdict == "NO" and reason.strip().lower().startswith("listing expired"):
            final_status = "expired"
            from src.audit_log import log_drop
            log_drop(
                listing_id="",  # set after upsert; row not yet inserted
                source=source,
                gate="stage5",
                anchor_company=anchor.company,
                url=(job_links[0] if job_links else ""),
                reason="stage5: listing expired",
            )

        single_eval = {
            "model": model,
            "verdict": verdict,
            "confidence": evaluation["confidence"],
            "reasoning": reason,
            "skills_extracted": evaluation["skills_extracted"],
            "matching_skills": evaluation["matching_skills"],
            "missing_skills": evaluation["missing_skills"],
        }

        logger.info(
            "  %s (conf=%d): %s at %s — %s",
            verdict, evaluation["confidence"], anchor.title, anchor.company, reason,
        )

        return JobListing(
            source=source,
            email_classification=classification,
            title=anchor.title,
            company=anchor.company,
            location=anchor.location,
            salary=anchor.salary,
            job_summary=evaluation.get("job_summary") or anchor.job_summary,
            verdict=verdict,
            confidence=evaluation["confidence"],
            reason=reason,
            links=job_links,
            recruiter_name=anchor.recruiter_name,
            recruiter_title=anchor.recruiter_title,
            raw_email_text=job_text,
            model_used=model,
            model_scores=json.dumps([single_eval]),
            skills_extracted=evaluation["skills_extracted"],
            matching_skills=json.dumps(evaluation["matching_skills"]) if evaluation["matching_skills"] else "",
            missing_skills=json.dumps(evaluation["missing_skills"]) if evaluation["missing_skills"] else "",
            tokens_used=tokens,
            latency_ms=latency_ms,
            date_posted=anchor.date_posted,
            final_status=final_status,
        )

# ---------------------------------------------------------------------------
# Response parsers
# ---------------------------------------------------------------------------

def _parse_extraction_response(
    text: str,
    email_links: list[str],
) -> list[ExtractedListing]:
    """Parse the extraction-only LLM response into ExtractedListing objects."""
    results: list[ExtractedListing] = []
    blocks = re.split(r"\n---\s*\n?", text)

    for block in blocks:
        block = block.strip()
        if not block or "LISTING:" not in block:
            continue

        fields = _parse_block_fields(block)
        title = fields.get("title", "").strip()
        if not title:
            continue

        link_str = fields.get("links", "none").strip()
        if link_str.lower() == "none" or not link_str:
            links = email_links[:5]
        else:
            links = [link.strip() for link in link_str.split(",") if link.strip().startswith("http")]

        source_board = _clean_source_board(fields.get("source_board", "none"))

        results.append(ExtractedListing(
            title=title,
            company=fields.get("company", "Unknown").strip(),
            location=fields.get("location", "not specified").strip(),
            salary=fields.get("salary", "not listed").strip(),
            job_summary=fields.get("job_summary", "").strip(),
            description=fields.get("description", "").strip(),
            source_board=source_board,
            links=links,
            recruiter_name=fields.get("recruiter_name", "").strip() or None,
            recruiter_title=fields.get("recruiter_title", "").strip() or None,
        ))

    return results


def _parse_evaluation_json(text: str) -> dict:
    """Parse the evaluation LLM response (expected JSON with verdict/confidence/reasoning).

    Falls back to safe defaults if parsing fails.
    """
    text = text.strip()

    # Try to extract JSON from the response (may have markdown fences)
    json_match = re.search(r"\{[^{}]*\}", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            verdict = str(data.get("verdict", "MAYBE")).upper().strip()
            if verdict not in ("YES", "NO", "MAYBE"):
                verdict = "MAYBE"
            confidence = int(data.get("confidence", 50))
            confidence = max(0, min(100, confidence))
            reasoning = str(data.get("reasoning", ""))
            job_summary = str(data.get("job_summary", ""))
            skills_extracted = bool(data.get("skills_extracted", False))
            matching_skills = data.get("matching_skills", [])
            missing_skills = data.get("missing_skills", [])
            if not isinstance(matching_skills, list):
                matching_skills = []
            if not isinstance(missing_skills, list):
                missing_skills = []
            return {
                "verdict": verdict,
                "confidence": confidence,
                "reasoning": reasoning,
                "job_summary": job_summary,
                "skills_extracted": skills_extracted,
                "matching_skills": [str(s) for s in matching_skills],
                "missing_skills": [str(s) for s in missing_skills],
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    logger.warning("Failed to parse evaluation JSON, falling back to defaults: %s", text[:200])
    return {
        "verdict": "MAYBE",
        "confidence": 50,
        "reasoning": "Unable to parse model response",
        "job_summary": "",
        "skills_extracted": False,
        "matching_skills": [],
        "missing_skills": [],
    }


def _parse_triage_response(
    text: str,
    *,
    email_text: str,
    email_links: list[str],
    classification: str,
    source: str,
    model: str,
    tokens: int,
    latency_ms: int,
) -> list[JobListing]:
    """Parse the single-model combined triage response into JobListing objects."""
    listings: list[JobListing] = []

    blocks = re.split(r"\n---\s*\n?", text)

    for block in blocks:
        block = block.strip()
        if not block or "LISTING:" not in block:
            continue

        fields = _parse_block_fields(block)
        title = fields.get("title", "").strip()
        if not title:
            continue

        link_str = fields.get("links", "none").strip()
        if link_str.lower() == "none" or not link_str:
            links = email_links[:5]
        else:
            links = [link.strip() for link in link_str.split(",") if link.strip().startswith("http")]

        verdict = fields.get("verdict", "MAYBE").strip().upper()
        if verdict not in ("YES", "NO", "MAYBE"):
            verdict = "MAYBE"

        confidence_str = fields.get("confidence", "50").strip()
        try:
            confidence = max(0, min(100, int(confidence_str)))
        except ValueError:
            confidence = 50

        # Parse skills fields
        skills_extracted_str = fields.get("skills_extracted", "false").strip().lower()
        skills_extracted = skills_extracted_str == "true"

        matching_skills_str = fields.get("matching_skills", "none").strip()
        if matching_skills_str.lower() == "none" or not matching_skills_str:
            matching_skills: list[str] = []
        else:
            matching_skills = [s.strip() for s in matching_skills_str.split(",") if s.strip()]

        missing_skills_str = fields.get("missing_skills", "none").strip()
        if missing_skills_str.lower() == "none" or not missing_skills_str:
            missing_skills: list[str] = []
        else:
            missing_skills = [s.strip() for s in missing_skills_str.split(",") if s.strip()]

        # Recruiter outreach floor: never score below MAYBE
        reason_text = fields.get("reason", "").strip()
        if classification == "RECRUITER_OUTREACH" and verdict == "NO":
            verdict = "MAYBE"
            reason_text += " [System Override: Upgraded to MAYBE due to direct recruiter outreach]"

        single_eval = {
            "model": model,
            "verdict": verdict,
            "confidence": confidence,
            "reasoning": reason_text,
            "skills_extracted": skills_extracted,
            "matching_skills": matching_skills,
            "missing_skills": missing_skills,
        }

        listings.append(JobListing(
            source=source,
            email_classification=classification,
            title=title,
            company=fields.get("company", "Unknown").strip(),
            location=fields.get("location", "not specified").strip(),
            salary=fields.get("salary", "not listed").strip(),
            job_summary=fields.get("job_summary", "").strip(),
            verdict=verdict,
            confidence=confidence,
            reason=reason_text,
            links=links,
            recruiter_name=fields.get("recruiter_name", "").strip() or None,
            recruiter_title=fields.get("recruiter_title", "").strip() or None,
            raw_email_text=email_text,
            model_used=model,
            model_scores=json.dumps([single_eval]),
            skills_extracted=skills_extracted,
            matching_skills=json.dumps(matching_skills) if matching_skills else "",
            missing_skills=json.dumps(missing_skills) if missing_skills else "",
            tokens_used=tokens,
            latency_ms=latency_ms,
        ))

    return listings


def _parse_block_fields(block: str) -> dict[str, str]:
    """Parse key: value pairs from a LISTING block."""
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in block.split("\n"):
        line = line.strip()
        if not line or line == "LISTING:":
            current_key = None
            continue
        match = re.match(r"^(\w[\w\s]*?):\s*(.+)$", line)
        if match:
            current_key = match.group(1).strip().lower().replace(" ", "_")
            fields[current_key] = match.group(2).strip()
        elif current_key is not None:
            # Continuation line (e.g. LLM wraps a 2-sentence job_summary)
            fields[current_key] = fields[current_key] + " " + line
    return fields


# ---------------------------------------------------------------------------
# OpenRouter client
# ---------------------------------------------------------------------------

def _call_openrouter(
    client: openai.OpenAI,
    model: str,
    prompt: str,
    max_tokens: int = 1000,
    temperature: float = 0.0,
    json_mode: bool = False,
    stage: str = "unknown",
) -> dict:
    """Make a chat completion request via OpenRouter.

    Returns dict with 'text' and 'tokens' keys. Emits an O-1 usage record
    (model slug, stage, token count) to the model-usage telemetry channel.
    """
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0
    log_model_usage(model, stage, tokens)
    return {"text": text, "tokens": tokens}
