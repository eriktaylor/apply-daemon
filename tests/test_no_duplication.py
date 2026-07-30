"""Ownership lint — one concept, one implementation site (plan item R-2).

Source-level, in the same shape as
``test_model_usage.py::TestMeteringCoverage``: grep ``src/`` and fail if a
module other than the owner implements a registered concept.

**Why a lint and not care.** Four parallel implementations were introduced
while the anti-drift principle was being written into CLAUDE.md — one of them
carrying a comment naming the other copy. Reuse is only discoverable if you
ask "what already does this?", and that question doesn't fire while you're
scoped to "add verb X". A fifth (a hard-coded ``"passed"`` literal in
process_queue) was found by the probe that produced this registry, after a
manual audit had already missed it.

**Adding an entry.** Do it when you extract something, while the decision is
fresh. Patterns must be specific enough not to cry wolf — a lint that fires
on legitimate code gets deleted. `split("|")` was rejected as an entry for
that reason: profile_loader parses markdown tables and budget parses the run
log, both legitimately.

**What this cannot catch:** semantic duplication with different syntax.
``db.get_review_queue`` and ``process_queue._select_top_n`` both rank
listings and share almost no tokens. Only review finds that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# (concept, owning module, regex that betrays an implementation of it)
OWNERSHIP: list[tuple[str, str, str]] = [
    (
        "output-folder naming convention (<Company>_<Title>_<id[:8]>)",
        "file_utils.py",
        r"job_id\[:8\] in",
    ),
    (
        "confidence band width",
        "db.py",
        r"BAND_WIDTH\s*=\s*\d",
    ),
    (
        "terminal-status policy (what a save cannot undo)",
        "decisions.py",
        r"TERMINAL_STATUSES\s*=",
    ),
    (
        "verb -> pipeline_status mapping",
        "decisions.py",
        r'"save":\s*\("saved"',
    ),
    (
        "human-label ledger writes",
        "human_labels.py",
        r'with open\(target, "a"',
    ),
    (
        "usage-log line parsing (timestamp|stage|model|tokens)",
        "model_usage.py",
        r"len\(parts\) not in \(4, 6\)",
    ),
    (
        "review-card field assembly",
        "listing_card.py",
        r'"skills_pct":',
    ),
    (
        "skills percentage arithmetic",
        "listing_card.py",
        r"len\(matching\)\s*[/+]|matched\s*/\s*total",
    ),
]


def _modules_matching(pattern: str) -> set[str]:
    rx = re.compile(pattern)
    hits = set()
    for path in sorted(SRC.glob("*.py")):
        if rx.search(path.read_text(encoding="utf-8")):
            hits.add(path.name)
    return hits


@pytest.mark.parametrize(
    "concept,owner,pattern", OWNERSHIP, ids=[row[1] + ":" + row[0][:28] for row in OWNERSHIP]
)
def test_concept_has_one_implementation(concept: str, owner: str, pattern: str) -> None:
    hits = _modules_matching(pattern)
    intruders = hits - {owner}
    assert not intruders, (
        f"{concept!r} should be implemented only in src/{owner}, but also "
        f"appears in: {', '.join(sorted(intruders))}. Import from the owner "
        f"instead of re-implementing (see CLAUDE.md → Anti-drift in code)."
    )


@pytest.mark.parametrize("concept,owner,pattern", OWNERSHIP,
                         ids=[row[1] for row in OWNERSHIP])
def test_owner_still_implements_it(concept: str, owner: str, pattern: str) -> None:
    """Guards the registry itself: a pattern that matches nothing is dead.

    Without this, refactoring the owner silently turns an entry into a no-op
    that passes forever while protecting nothing.
    """
    assert owner in _modules_matching(pattern), (
        f"Pattern for {concept!r} no longer matches its owner src/{owner} — "
        f"the registry entry is stale and is now protecting nothing."
    )
