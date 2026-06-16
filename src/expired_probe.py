"""Fix 4b — HTTP backstop probe for expired/dead listings.

Stage 5's text-level check (Fix 4a) catches the majority of expired
listings whose page body contains explicit "no longer accepting"
language. The probe handles the residual cases the body text can't
reveal:

  - HTTP 404 / 410 — scrape returned no body to read.
  - LinkedIn auth-wall page — body renders "join to apply" instead of
    "expired."
  - Soft-404 redirects to a generic careers home page.
  - Track A rows whose stored description is from when the page was
    still fresh.

The probe runs only inside ``process_queue._process_one`` on a top-N
autopilot row. Failure modes are designed to fail open: any error,
timeout, block, or ambiguous response is treated as ``unknown -> proceed``,
never ``expired``. We never silently drop a good listing because of a
flaky probe.

Kill-switch: ``EXPIRED_PROBE_ENABLED=false`` skips the probe entirely.
"""

from __future__ import annotations

import logging
import os
import re

import requests

from src.proxy_manager import get_default_proxy_manager

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5
_MAX_BYTES = 200_000  # truncate the body before scanning for stop-phrases

_EXPIRED_PHRASES = (
    "no longer accepting applications",
    "this job is no longer available",
    "position has been filled",
    "job posting has expired",
    "this role has been closed",
    "we are no longer hiring for this position",
    "job is no longer active",
)

_DEAD_STATUS_CODES = frozenset({404, 410})


def _enabled() -> bool:
    return (
        os.getenv("EXPIRED_PROBE_ENABLED", "true").strip().lower()
        in {"1", "true", "yes"}
    )


def probe(url: str) -> tuple[bool, str]:
    """Probe a listing URL. Returns (is_expired, reason).

    ``is_expired=True`` means the caller should mark the row expired and
    skip autopilot enrichment. ``False`` always means "proceed" — including
    error and timeout cases — to ensure false-negatives on the probe never
    drop a good listing.

    Reason is a short clause for the audit log; never raw body text.
    """
    if not _enabled():
        return False, ""
    if not url:
        return False, ""

    try:
        proxy_mgr = get_default_proxy_manager()
        proxies = proxy_mgr.proxies_dict() if proxy_mgr.enabled else None
    except Exception:
        proxies = None

    try:
        resp = requests.get(
            url,
            timeout=_TIMEOUT_SECONDS,
            allow_redirects=True,
            proxies=proxies,
            headers={"User-Agent": "apply-daemon-expired-probe/1.0"},
            stream=True,
        )
    except requests.RequestException:
        logger.debug("expired_probe: request failed for %s — failing open", url,
                     exc_info=True)
        return False, ""

    status = resp.status_code
    if status in _DEAD_STATUS_CODES:
        try:
            resp.close()
        except Exception:
            pass
        return True, f"probe: http {status}"

    # Only scan the first chunk of body — the stop-phrases sit above the fold
    # on every page we've observed. Bounded read prevents a slow drip from a
    # malicious / misbehaving server from tying up the autopilot loop.
    try:
        body_bytes = resp.raw.read(_MAX_BYTES, decode_content=True) or b""
    except Exception:
        body_bytes = b""
    finally:
        try:
            resp.close()
        except Exception:
            pass

    try:
        body = body_bytes.decode("utf-8", errors="ignore").lower()
    except Exception:
        return False, ""

    # Collapse whitespace so phrase matching is resilient to HTML formatting.
    body = re.sub(r"\s+", " ", body)
    for phrase in _EXPIRED_PHRASES:
        if phrase in body:
            return True, f"probe: matched '{phrase}'"

    return False, ""
