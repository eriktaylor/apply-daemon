"""Data models for the apply-daemon pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

# The width batch-scoring paths send to a model. Track A caps stored
# descriptions here at ingest; Track B rows store the full Stage 3 scrape
# (live table: 140 rows over 2000 chars, max 31k), which is why the
# expensive one-shot consumers — tailor, autopilot re-score, pointwise
# Stage 5 — read at FULL_JOB_DESCRIPTION_CHARS instead.
JOB_DESCRIPTION_CHARS = 2000
FULL_JOB_DESCRIPTION_CHARS = 4000

# What a prompt states when a listing has no stored body at all. One
# definition — a stated absence, never a silent omission (card contract).
NO_DESCRIPTION_PLACEHOLDER = "(No job description was stored.)"


@dataclass
class JobListing:
    """A single job listing extracted and scored by the LLM."""

    id: str = field(default_factory=lambda: str(uuid4()))
    source: str = ""  # "linkedin", "indeed", "google_alerts", "recruiter"
    email_classification: str = ""  # JOB_DIGEST, RECRUITER_OUTREACH, GOOGLE_ALERT
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""  # Free text — "$220K-$485K" or "not listed"
    job_summary: str = ""  # 2-sentence TL;DR of company + role
    verdict: str = ""  # YES / NO / MAYBE
    confidence: int = 0  # 0-100 average confidence from model evaluations
    reason: str = ""  # One-sentence LLM explanation
    links: list[str] = field(default_factory=list)
    recruiter_name: str | None = None
    recruiter_title: str | None = None
    # The Stage 3 job-description body — despite the name, not an email. Read
    # it through job_description_text() rather than directly.
    raw_email_text: str = ""
    model_used: str = ""
    # JSON: per-model verdict/confidence, e.g.
    # [{"model":"gemma3:4b","verdict":"YES","confidence":85,"reasoning":"..."}]
    model_scores: str = ""
    skills_extracted: bool = False  # True if explicit skills were found in the listing
    matching_skills: str = ""  # JSON list: top 3 skills in both job and candidate profile
    missing_skills: str = ""  # JSON list: top 2-3 skills the job requires but candidate lacks
    tokens_used: int = 0
    latency_ms: int = 0
    date_ingested: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # When the role was posted (NOT when we ingested it). ISO date string
    # ("2026-02-27") or empty when the source did not supply one. Used for
    # freshness badging in the digest and an optional max-age filter
    # (max_listing_age_days in profile.md Pipeline Settings).
    date_posted: str = ""
    final_status: str = "triaged"  # triaged / saved / passed / tailored / applied


def _field(listing: Any, key: str) -> Any:
    """Read *key* from a dict, sqlite3.Row, or attribute-style object
    (JobListing), missing keys included."""
    try:
        return listing[key]
    except (KeyError, IndexError, TypeError):
        return getattr(listing, key, None)


def job_description_text(
    listing: Any, *, limit: int = JOB_DESCRIPTION_CHARS,
) -> str:
    """Return the fullest description of the role stored for *listing*.

    ``raw_email_text`` holds the Stage 3 job-description body for every row
    written by a production path — ``_stage5_evaluate_anchor`` stores
    ``job_text`` there. (The email-level ``_SINGLE_PROMPT`` writer, the one
    path that would store a whole multi-job email, is reachable only from
    tests.) It is the only stored field carrying the listing's actual stated
    requirements; ``job_summary`` is a ~290-char LLM TL;DR *of* it.

    Never falls back to ``reason``. That is the Stage 5 model's own
    justification for its verdict, and a downstream re-scorer that reads it
    is grading the incumbent's reasoning rather than the job — the
    contamination that made autopilot's post-research verdict unusable as an
    eval gold standard.
    """
    for key in ("raw_email_text", "job_summary"):
        text = str(_field(listing, key) or "").strip()
        if text:
            return text[:limit]
    return ""
