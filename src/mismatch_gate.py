"""Fix 2a — hybrid title↔body consistency gate.

Two-stage:

1. Substring/token check on ``anchor.company`` against ``job_summary``
   and the resolved URL host. Free and deterministic.
2. LLM fallback (only on miss) via the cheap ``OPENROUTER_STAGE1_MODEL``
   slot. Decides drop vs. keep.

Empirically most legit JDs name themselves in the body, so the LLM call
only fires on the ambiguous tail (~10-30% of rows surviving Stage 5).

Audit trail: every drop emits a row to ``audit_log.log_drop`` with the
``gate`` column set to ``substring`` or ``llm`` depending on which path
fired the drop. See ``docs/AUDIT.md`` for the schema.

Kill-switch: ``MISMATCH_GATE_MODE`` env var
  - ``hybrid`` (default): substring first, LLM on miss
  - ``substring_only``: substring check decides; LLM is never called
  - ``llm_only``: skip substring, always call the LLM
  - ``off``: gate disabled, every row passes through
"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Tokens shorter than this are dropped from the company-name token set.
# Keeps "AI", "Co", "I/O" from creating spurious substring hits.
_MIN_TOKEN_LEN = 4

# Common corporate suffixes we strip from anchor.company before tokenizing.
_COMPANY_STOPWORDS = frozenset({
    "inc", "llc", "ltd", "corp", "co", "the", "and", "of",
    "limited", "incorporated", "corporation", "company",
    "group", "holdings", "labs", "lab",
})


def _normalize_tokens(company: str) -> set[str]:
    """Return the set of significant lowercase tokens from a company name."""
    if not company:
        return set()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", company.lower())
    tokens = {t for t in cleaned.split() if t and t not in _COMPANY_STOPWORDS}
    return {t for t in tokens if len(t) >= _MIN_TOKEN_LEN}


def _url_host_blob(url: str) -> str:
    """Return a lowercase host string with TLD-ish suffix stripped for matching."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    host = host.lower().removeprefix("www.")
    # Drop the last dot-segment (TLD); keeps "protege" from "protege.health" and
    # "openai" from "openai.com" so substring matching can hit the brand portion.
    parts = host.split(".")
    if len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts)


def _substring_hit(anchor_company: str, job_summary: str, url: str) -> bool:
    """Stage 1 — return True if any company token appears in summary or host."""
    tokens = _normalize_tokens(anchor_company)
    if not tokens:
        # If we can't extract any significant token (e.g. anchor.company is a
        # single short word), be permissive — fall through to Stage 2 / pass.
        return True
    summary = (job_summary or "").lower()
    host_blob = _url_host_blob(url)
    return any(t in summary or t in host_blob for t in tokens)


_LLM_PROMPT = """\
You are evaluating whether a job listing's metadata matches the actual posting body.

Anchor company (claimed): {company}
Job summary (TL;DR of the actual posting body): {summary}

Question: does the summary describe a role at the anchor company?

Respond with ONLY a valid JSON object (no markdown):
{{"matches": true|false, "actual_company": "<company named in the summary, or empty>"}}\
"""


def _llm_check(
    client,
    model: str,
    anchor_company: str,
    job_summary: str,
) -> tuple[bool, str]:
    """Stage 2 — ask the cheap model whether the summary matches the company.

    Returns (matches, observed_company). On any error, returns (True, "") so
    the gate fails open: the row passes through and the error is logged.
    """
    if not client:
        return True, ""
    prompt = _LLM_PROMPT.format(
        company=anchor_company,
        summary=(job_summary or "")[:1500],
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        matches = bool(data.get("matches", True))
        observed = str(data.get("actual_company", "") or "").strip()
        return matches, observed
    except Exception:
        logger.warning(
            "mismatch_gate LLM check failed for %s — failing open",
            anchor_company, exc_info=True,
        )
        return True, ""


def _gate_mode() -> str:
    return (os.getenv("MISMATCH_GATE_MODE", "hybrid").strip().lower() or "hybrid")


def check_mismatch(
    *,
    client,
    model: str,
    listing_id: str,
    source: str,
    anchor_company: str,
    job_summary: str,
    url: str,
) -> tuple[bool, str, str]:
    """Run the hybrid gate. Returns (drop, gate, observed_company).

    ``drop=True`` means the caller should treat the row as a mismatch drop.
    ``gate`` is the audit-log column value: ``substring`` or ``llm`` when
    drop=True, ``""`` when drop=False. The caller is responsible for
    emitting the audit log line.
    """
    mode = _gate_mode()
    if mode == "off":
        return False, "", ""

    if mode == "substring_only":
        hit = _substring_hit(anchor_company, job_summary, url)
        return (not hit), ("substring" if not hit else ""), ""

    if mode == "llm_only":
        matches, observed = _llm_check(client, model, anchor_company, job_summary)
        return (not matches), ("llm" if not matches else ""), observed

    # hybrid (default)
    if _substring_hit(anchor_company, job_summary, url):
        return False, "", ""
    matches, observed = _llm_check(client, model, anchor_company, job_summary)
    if matches:
        return False, "", observed
    return True, "llm", observed
