"""Decision policy shared by every review surface (plan item R-1).

Slack and the CLI both let a human save, pass, or tailor a listing. Before
this module they each carried their own copy of *what that means* — which
`pipeline_status` a verb targets, which ledger action it records, and whether
a transition is legal at all. Two copies of a policy diverge; the surfaces
would then disagree about whether a passed listing can be revived.

**Policy lives here; transport does not.** Slack updating a card and the CLI
printing a line are legitimately different, so each adapter keeps its own
I/O. What must never differ is the answer to "is this decision allowed, and
what does it change".

``db.update_pipeline_status`` is an unconditional UPDATE — it will happily
re-apply a status a row already holds, and let a save undo a pass — so the
guard has to live above it, here, rather than being assumed of it.
"""

from __future__ import annotations

import logging
import sqlite3

from src.db import Database
from src.human_labels import append_human_label

logger = logging.getLogger(__name__)

# verb → (target pipeline_status, ledger action)
#
# Ledger actions MUST match the vocabulary eval/preference_pairs.py scores
# (save/tailor → positive, pass → negative), or decisions land in the ledger
# as neutral and silently vanish from the preference pairs the ranking work
# depends on.
DECISIONS: dict[str, tuple[str, str]] = {
    "save": ("saved", "save"),
    "pass": ("passed", "pass"),
    "tailor": ("tailored", "tailor"),
}

# Statuses a listing cannot be saved back out of. Mirrors the documented
# Slack rule that 👎 is terminal (docs/CHATOPS.md): reviving a passed listing
# goes through re-triage, not a save.
TERMINAL_STATUSES = frozenset({"passed", "expired"})

# Statuses a save may not *downgrade* out of. `saved` is the promotion out of
# review; these rows are already past it, and `db.update_pipeline_status` is
# an unconditional UPDATE, so without this rule a 👍 on a tailored card — or
# `cli save <id>` on one — silently regresses it to `saved`. Both surfaces
# allowed that before 2026-08-22 (the CLI by omission, Slack only because an
# unrelated guard refused every non-triaged save; see plan R-6).
NO_DOWNGRADE_TO_SAVED = frozenset({"tailored", "applied", "interviewing"})


def target_status(verb: str) -> str:
    """The `pipeline_status` a verb transitions to."""
    return DECISIONS[verb][0]


def ledger_action(verb: str) -> str:
    """The `human_reaction` value a verb records."""
    return DECISIONS[verb][1]


def is_allowed(current_status: str | None, verb: str) -> bool:
    """Whether ``verb`` may be applied to a listing currently at that status.

    False when the row is already at the target status (so a repeated action
    doesn't append duplicate ledger rows and inflate the preference-pair
    corpus with phantom decisions), False for a save out of a terminal
    status, and False for a save that would downgrade a row already past
    ``saved`` (``NO_DOWNGRADE_TO_SAVED``).
    """
    if verb not in DECISIONS:
        return False
    if current_status == target_status(verb):
        return False
    if verb == "save" and current_status in TERMINAL_STATUSES:
        return False
    if verb == "save" and current_status in NO_DOWNGRADE_TO_SAVED:
        return False
    return True


def apply(
    db: Database,
    row: sqlite3.Row | dict,
    verb: str,
    *,
    surface: str,
    bulk: bool = False,
) -> bool:
    """Guard, transition, and record one decision. True if the status moved.

    The full path, for surfaces whose transport doesn't need to interleave
    (the CLI). Slack composes the pieces itself because its card updates sit
    between the status write and the receipt.
    """
    current = row["pipeline_status"]
    if not is_allowed(current, verb):
        return False

    status = target_status(verb)
    if not db.update_pipeline_status(row["id"], status):
        return False

    append_human_label(
        row["id"], ledger_action(verb), dict(row), surface=surface, bulk=bulk
    )
    logger.info("%s %s → %s", surface, row["id"][:8], status)
    return True
