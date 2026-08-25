"""Hygiene guard — ``plans/*.md`` are public-grade prose: aggregates, never rows.

Source-level, in the same shape as
``test_model_usage.py::TestMeteringCoverage``: scan the real files and fail
with ``file:line`` rather than mock a writer. A plan is written by an agent
mid-session, when the interesting thing on screen *is* the row-level output
of a scan — the pull toward pasting it is strongest exactly when nobody is
thinking about the repo shipping. Only a scan catches that.

The rule this enforces (SECURITY.md, mantra item 9): record aggregates, keep
the raw output on real data in ``data/reports/`` (gitignored) and let the plan
carry a pointer.

**Why these four shapes and no company-name denylist.** Each shape here is
decidable from the text alone. "Is this a real company?" is not — a denylist
would either miss the ones nobody listed or fire on every plan that mentions
a job board by name, and a lint that cries wolf gets deleted
(``test_no_duplication.py`` learned this).

**The failure message names the location, not the value.** Echoing a matched
address or id into pytest output copies the thing being removed into a
terminal transcript and any CI log. ``file:line: kind`` is enough to fix it;
``find_violations`` still returns the match so the unit tests below can
assert on it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = REPO_ROOT / "plans"
EXAMPLE_PROFILE_DIR = REPO_ROOT / "my_profile_example"

# A listing id is a uuid4 (src/models.py). The five-group shape is the point:
# a 7-8 character commit SHA is normal, useful plan content and must not match.
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Substrings, not paths: an absolute path from one machine tells a reader
# nothing and leaks a username. Relative repo paths (``data/reports/x.md``)
# are how a plan is supposed to point at its raw output.
LOCAL_PATH_TOKENS = ("/tmp/", "/home/", "scratchpad")

KIND_UUID = "listing id (uuid)"
KIND_EMAIL = "email address"
KIND_PATH = "local path"


def find_violations(
    text: str, *, allowed_emails: set[str]
) -> list[tuple[int, str, str]]:
    """Return ``(line number, kind, matched text)`` for every row-level shape.

    Pure: takes the text, not a path, so the shapes can be tested against
    synthetic strings in a checkout where ``plans/`` does not exist.
    """
    allowed = {addr.lower() for addr in allowed_emails}
    violations: list[tuple[int, str, str]] = []

    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in UUID_RE.finditer(line):
            violations.append((lineno, KIND_UUID, match.group(0)))

        for match in EMAIL_RE.finditer(line):
            if match.group(0).lower() in allowed:
                continue
            violations.append((lineno, KIND_EMAIL, match.group(0)))

        lowered = line.lower()
        for token in LOCAL_PATH_TOKENS:
            index = lowered.find(token)
            if index != -1:
                violations.append((lineno, KIND_PATH, line[index:].strip()))

    return violations


def load_allowed_emails() -> set[str]:
    """Addresses that literally appear in ``my_profile_example/``.

    That directory is the committed synthetic template, so its job-board
    sender addresses are already public and a plan discussing the allowlist
    has to be able to name them. Read from the files rather than restated
    here — a second copy of the list would drift from the template it
    exempts (CLAUDE.md -> Anti-drift in code).
    """
    found: set[str] = set()
    if not EXAMPLE_PROFILE_DIR.is_dir():
        return found

    for path in sorted(EXAMPLE_PROFILE_DIR.rglob("*")):
        if not path.is_file():
            continue
        try:
            # errors="ignore": base_resume.docx is a zip, and skipping it by
            # extension would silently drop a future .txt/.yaml template.
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found.update(match.group(0) for match in EMAIL_RE.finditer(text))

    return found


class TestFindViolations:
    """Unit tests for the shapes. Synthetic fixtures only — every address,
    id, and path below is invented (``.invalid`` is reserved by RFC 2606)."""

    def test_flags_a_full_uuid(self):
        text = "Row 3fa85f64-5717-4562-b3fc-2c963f66afa6 scored YES."
        found = find_violations(text, allowed_emails=set())
        assert [(1, KIND_UUID, "3fa85f64-5717-4562-b3fc-2c963f66afa6")] == found

    def test_flags_a_uuid_on_the_right_line(self):
        text = "\n".join(["intro", "detail", "id: 00000000-0000-4000-8000-000000000000"])
        found = find_violations(text, allowed_emails=set())
        assert [lineno for lineno, _, _ in found] == [3]

    @pytest.mark.parametrize("sha", ["ff3bf1e", "cbd2178a", "d586463", "6059002"])
    def test_does_not_flag_a_short_commit_sha(self, sha):
        """Plans cite commits constantly; flagging those would kill the lint."""
        assert find_violations(f"Shipped in {sha}.", allowed_emails=set()) == []

    def test_does_not_flag_a_bare_hex_run(self):
        assert find_violations("hash 0123456789abcdef", allowed_emails=set()) == []

    def test_flags_an_email_address(self):
        text = "Sender candidate.person@mail.invalid slipped the allowlist."
        found = find_violations(text, allowed_emails=set())
        assert [(1, KIND_EMAIL, "candidate.person@mail.invalid")] == found

    def test_does_not_flag_an_allowed_example_address(self):
        text = "The template ships jobalerts-noreply@boardexample.invalid."
        found = find_violations(
            text, allowed_emails={"jobalerts-noreply@boardexample.invalid"}
        )
        assert found == []

    def test_allowed_address_match_is_case_insensitive(self):
        found = find_violations(
            "From: JobAlerts@BoardExample.invalid",
            allowed_emails={"jobalerts@boardexample.invalid"},
        )
        assert found == []

    @pytest.mark.parametrize(
        "line",
        [
            "wrote /tmp/claude-1000/scan.json",
            "see /home/someone/apply-daemon/notes.md",
            "left it in the scratchpad directory",
            "Scratchpad/notes.md has the rest",
        ],
    )
    def test_flags_local_paths(self, line):
        found = find_violations(line, allowed_emails=set())
        assert [kind for _, kind, _ in found] == [KIND_PATH]

    @pytest.mark.parametrize(
        "line",
        [
            "Raw scan: data/reports/x.md",
            "Owner is src/cli.py; card contract in src/listing_card.py.",
            "Run `pytest tests/test_triage.py -q`.",
            "Home location comes from the profile table.",
        ],
    )
    def test_does_not_flag_relative_repo_paths(self, line):
        assert find_violations(line, allowed_emails=set()) == []

    def test_reports_every_shape_in_one_pass(self):
        text = "\n".join(
            [
                "# Wave 1",
                "Top row 3fa85f64-5717-4562-b3fc-2c963f66afa6 from a@b.invalid",
                "dumped to /tmp/scan.json",
            ]
        )
        found = find_violations(text, allowed_emails=set())
        assert {(lineno, kind) for lineno, kind, _ in found} == {
            (2, KIND_UUID),
            (2, KIND_EMAIL),
            (3, KIND_PATH),
        }

    def test_clean_text_passes(self):
        text = "\n".join(
            [
                "# Wave 2 — dedup audit",
                "",
                "172 rows scanned, 106 shared a pre-research confidence.",
                "Raw output: data/reports/dedup-audit.md (gitignored).",
                "Regression fixed in ff3bf1e.",
            ]
        )
        assert find_violations(text, allowed_emails=set()) == []


class TestAllowedEmailLoader:
    """The exemption is only as good as the loader; a silently empty one
    turns the template's own addresses into violations."""

    def test_loads_addresses_from_the_committed_template(self):
        allowed = load_allowed_emails()
        assert allowed, (
            "no addresses parsed out of my_profile_example/ — the email "
            "exemption is loaded from those files, so an empty set means the "
            "loader broke, not that the template is clean"
        )

    def test_every_loaded_address_is_exempt_from_the_scan(self):
        allowed = load_allowed_emails()
        line = " ".join(sorted(allowed))
        assert find_violations(line, allowed_emails=allowed) == []


def test_plans_carry_no_row_level_data():
    """The live scan. Skips — never silently passes — where plans/ is absent
    (a fresh clone, or a git worktree, which has no gitignored files)."""
    if not PLANS_DIR.is_dir():
        pytest.skip("plans/ not present (gitignored in this checkout)")

    allowed = load_allowed_emails()
    offenders: list[str] = []

    for path in sorted(PLANS_DIR.rglob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        relative = path.relative_to(REPO_ROOT)
        for lineno, kind, _matched in find_violations(text, allowed_emails=allowed):
            offenders.append(f"{relative}:{lineno}: {kind}")

    assert not offenders, (
        "plans/ carries row-level data (SECURITY.md mantra item 9). Move the "
        "raw output to data/reports/ and leave a pointer:\n  "
        + "\n  ".join(offenders)
    )
