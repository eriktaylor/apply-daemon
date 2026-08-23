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
EVAL = Path(__file__).resolve().parent.parent / "eval"

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
    (
        "reading autopilot's re-score envelope (auto_assets.json)",
        "listing_card.py",
        # Three surfaces unpacked this envelope key by key — process_queue for
        # the Slack card, cli for deep-dive, sweeper for its tailor thread —
        # and only the ones reading the JSON ever saw the re-score, so the CLI
        # feed showed YES 95% for a listing Slack called MAYBE 58% (R-4).
        # Matches the *envelope* read only: db.py's SQL and the backfill name
        # the same column but store and copy it rather than interpret it, and
        # `card["post_research_verdict"]` is a read of the contract's output.
        r'get\("post_research_verdict"',
    ),
    (
        "which stored field is the job description",
        "models.py",
        r'"raw_email_text",\s*"job_summary"',
    ),
    (
        "claude CLI subprocess transport (subscription-billed route)",
        "claude_cli.py",
        r'"claude",\s*"-p"',
    ),
    (
        "dedicated-logger file-sink attach-once pattern",
        "file_logger.py",
        r"logging\.FileHandler\(",
    ),
    (
        "detached stage launch (fire-and-forget subprocess)",
        "cli.py",
        r"start_new_session\s*=\s*True",
    ),
    (
        "tolerant first-JSON-object parse of a model response",
        # Interim home (plan I-13): the natural neighbour is claude_cli.py's
        # strip_fence, which this imports, but that file was another agent's
        # this round. Move both the helper and this row when I-13 lands.
        "ranking.py",
        r"JSONDecoder\(\)\.raw_decode",
    ),
]

# (concept, module, regex) — concepts whose callers ALL live in one module,
# counted *within* that file. OWNERSHIP cannot see these: a second copy inside
# the owner is not an intruder, so the lint above passes while the drift is
# already there. `status` and `next` both need the enrichment headroom and
# both live in cli.py, which is exactly that shape (C-12).
SINGLE_SITE: list[tuple[str, str, str]] = [
    (
        "enrichment headroom (cap minus what autopilot finalized today)",
        "cli.py",
        r"count_autopilot_processed_today\(",
    ),
    (
        "pipeline stage subprocess launch (refresh's chain and enrich's one)",
        "cli.py",
        r"subprocess\.run\(",
    ),
    (
        "pre-run budget refusal envelope (error: budget_blocked)",
        "cli.py",
        r'"error": "budget_blocked"',
    ),
]

# Patterns no module may contain, with the reason. Distinct from OWNERSHIP:
# these have no owner — the shape itself is the defect.
BANNED: list[tuple[str, str]] = [
    (
        r'job_summary"[^\n]*\bor\b[^\n]*"reason"',
        "Reading the job description as `job_summary or reason` falls back to "
        "the Stage 5 model's own justification, so a downstream consumer ends "
        "up scoring the incumbent's reasoning instead of the job. Use "
        "models.job_description_text().",
    ),
]


def _modules_matching(pattern: str, roots: tuple[Path, ...] = (SRC,)) -> set[str]:
    rx = re.compile(pattern)
    hits = set()
    for root in roots:
        for path in sorted(root.glob("*.py")):
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


@pytest.mark.parametrize(
    "concept,module,pattern", SINGLE_SITE,
    ids=[row[1] + ":" + row[0][:28] for row in SINGLE_SITE],
)
def test_concept_has_exactly_one_call_site(concept: str, module: str,
                                           pattern: str) -> None:
    """One implementation *within* a file, for concepts with one owning module.

    Fails in both directions on purpose: two hits is the copy this guards
    against, zero hits is a stale entry protecting nothing.
    """
    text = (SRC / module).read_text(encoding="utf-8")
    hits = re.findall(pattern, text)
    assert len(hits) == 1, (
        f"{concept!r} appears {len(hits)}x in src/{module}; it must be "
        f"computed in exactly one function and called from the rest "
        f"(see CLAUDE.md → Anti-drift in code)."
    )


@pytest.mark.parametrize("pattern,why", BANNED, ids=[row[0][:32] for row in BANNED])
def test_banned_pattern_absent(pattern: str, why: str) -> None:
    # eval/ is in scope: the original offender for the first entry lived there.
    offenders = _modules_matching(pattern, roots=(SRC, EVAL))
    assert not offenders, f"{', '.join(sorted(offenders))}: {why}"


# Branch headers in sweeper._dispatch_reactions whose first statement must be
# the shared guard. Save and pass only — see the docstring below.
DISPATCH_POLICY_BRANCHES: list[tuple[str, str]] = [
    ("pass", 'if highest == "pass":'),
    ("save", 'elif highest == "save":'),
]


@pytest.mark.parametrize(
    "verb,header", DISPATCH_POLICY_BRANCHES,
    ids=[row[0] for row in DISPATCH_POLICY_BRANCHES],
)
def test_reaction_dispatch_defers_to_decision_policy(verb: str, header: str) -> None:
    """The import being present is not evidence the call site uses it.

    That is exactly how R-6 survived R-1: ``sweeper.py`` imported
    ``decisions.is_allowed`` and ``_handle_save`` called it correctly, so every
    grep for the policy looked clean — while ``_dispatch_reactions`` kept a
    stricter hard-coded ``current_status == "triaged"`` in front of it. The
    correct guard was unreachable, and a 👍 on any autopilot row was counted
    skipped with no ledger write.

    So this asserts the *call site*: the first statement of each of these
    branches must be an ``is_allowed(`` test. Scoped to save and pass — the
    tailor branch legitimately switches on status literals (``in ("triaged",
    "saved")``, ``== "auto"``) because it is routing to different work, not
    applying a second permission rule.
    """
    lines = (SRC / "sweeper.py").read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == header]
    assert len(starts) == 1, (
        f"Expected exactly one {header!r} branch in src/sweeper.py, found "
        f"{len(starts)} — this lint is keyed to that line and is now stale."
    )

    body = next(line for line in lines[starts[0] + 1:] if line.strip())
    assert "is_allowed(" in body, (
        f"The {verb!r} branch of sweeper._dispatch_reactions guards with "
        f"{body.strip()!r} instead of decisions.is_allowed(). A status literal "
        f"here shadows the shared policy rather than consulting it — that is "
        f"the R-6 defect (see CLAUDE.md → Anti-drift in code)."
    )
