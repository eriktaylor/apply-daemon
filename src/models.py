"""Data models for the apply-daemon pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


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
    raw_email_text: str = ""  # Full extracted text for debugging
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
    final_status: str = "triaged"  # triaged / saved / passed / tailored / applied
