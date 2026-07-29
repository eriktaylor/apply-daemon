"""Human feedback ledger — the shared writer for `data/human_labels.jsonl`.

Every human decision, on every surface, lands here. The ledger is the sole
input to `eval/preference_pairs.py`, which builds the preference pairs that
`plans/ranking_upgrade.md` E-4 needs — so a surface that forgets to write
here is invisible to the ranking work, silently.

Extracted from `sweeper.py` so the Slack sweeper and the CLI share one
implementation rather than two that can drift.

Records carry two fields beyond the original Slack-era schema:

- ``surface`` — which UI produced the decision. Rows written before this
  field existed have no ``surface`` key and are implicitly ``"slack"``.
  Without it, "where do decisions actually happen?" is unanswerable, which
  is the question `plans/cli_skill_interface.md` S-2 exists to settle.
- ``bulk`` — True when the decision came from a batch action (e.g. passing
  a whole page of 3 at once). A bulk pass is plausibly weaker signal than an
  aimed one; recording it keeps that distinction available for down-weighting
  later instead of merging it away now, which is not recoverable.

Readers use ``rec.get()``, so both fields are additive for existing consumers.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

LABELS_DIR = Path("data")
LABELS_PATH = LABELS_DIR / "human_labels.jsonl"


def resolve_labels_path() -> Path:
    """Ledger path: ``$HUMAN_LABELS_PATH`` → the default under ``data/``.

    The override exists so a smoke test or a throwaway run against a copied
    database cannot append phantom decisions to the real ledger — those rows
    look exactly like genuine human judgments to the preference-pair
    extractor, and they are tedious to find later.
    """
    override = os.getenv("HUMAN_LABELS_PATH", "").strip()
    return Path(override).expanduser() if override else LABELS_PATH

SURFACE_SLACK = "slack"
SURFACE_CLI = "cli"
VALID_SURFACES = (SURFACE_SLACK, SURFACE_CLI)


def _json_default(o: object) -> str:
    """Serialize datetimes and sqlite3.Row date-like values."""
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def append_human_label(
    job_id: str,
    action: str,
    listing: dict,
    *,
    surface: str = SURFACE_SLACK,
    bulk: bool = False,
    path: Path | None = None,
) -> None:
    """Append one human feedback record to the ledger.

    ``surface`` and ``bulk`` are always emitted so downstream consumers never
    have to distinguish "absent" from "false" on rows written after this
    module landed.
    """
    if surface not in VALID_SURFACES:
        logger.warning("Unknown label surface %r; recording anyway", surface)

    target = path or resolve_labels_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "job_id": job_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "human_reaction": action,
        "surface": surface,
        "bulk": bool(bulk),
        "listing": dict(listing),
    }
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=_json_default) + "\n")
    logger.debug("Appended human label: %s → %s (%s)", job_id[:8], action, surface)
