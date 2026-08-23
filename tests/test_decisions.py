"""Direct tests for the shared decision policy (src/decisions.py).

Every review surface routes through ``is_allowed``; until 2026-08-22 the
policy had no tests of its own and was exercised only through each adapter's
dispatch tests — which is how Slack and the CLI came to disagree about a save
out of ``tailored`` without any test noticing (plan R-6).
"""

import pytest

from src.db import Database
from src.decisions import (
    DECISIONS,
    NO_DOWNGRADE_TO_SAVED,
    TERMINAL_STATUSES,
    is_allowed,
    ledger_action,
    target_status,
)


class TestSave:
    @pytest.mark.parametrize("status", ["auto", "auto_queued", "triaged", None])
    def test_allowed_from_review_statuses(self, status):
        assert is_allowed(status, "save") is True

    def test_refused_when_already_saved(self):
        assert is_allowed("saved", "save") is False

    @pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
    def test_refused_out_of_terminal(self, status):
        assert is_allowed(status, "save") is False

    @pytest.mark.parametrize("status", sorted(NO_DOWNGRADE_TO_SAVED))
    def test_refused_as_a_downgrade(self, status):
        """A save promotes out of review; it must never regress a row that is
        already past ``saved``. Both surfaces allowed this before 2026-08-22."""
        assert is_allowed(status, "save") is False


class TestPass:
    @pytest.mark.parametrize(
        "status", ["auto", "auto_queued", "triaged", "saved", "tailored", None]
    )
    def test_allowed_from_anything_not_passed(self, status):
        assert is_allowed(status, "pass") is True

    def test_refused_when_already_passed(self):
        assert is_allowed("passed", "pass") is False


class TestTailor:
    def test_refused_when_already_tailored(self):
        assert is_allowed("tailored", "tailor") is False

    @pytest.mark.parametrize("status", ["auto", "saved", "triaged"])
    def test_allowed_otherwise(self, status):
        assert is_allowed(status, "tailor") is True


class TestVocabulary:
    def test_unknown_verb_is_refused(self):
        assert is_allowed("triaged", "promote") is False

    def test_every_verb_has_a_target_and_ledger_action(self):
        for verb in DECISIONS:
            assert target_status(verb) in Database.VALID_STATUSES
            assert ledger_action(verb)

    def test_policy_statuses_exist_in_the_schema(self):
        """Drift guard: a status named here but absent from the schema would
        make the rule silently dead."""
        for status in TERMINAL_STATUSES | NO_DOWNGRADE_TO_SAVED:
            assert status in Database.VALID_STATUSES, status

    def test_terminal_and_no_downgrade_are_distinct_ideas(self):
        """``TERMINAL_STATUSES`` documents "👎 is terminal"; the no-downgrade
        set is about not regressing a row already past ``saved``. Keeping
        them disjoint keeps each comment true."""
        assert not (TERMINAL_STATUSES & NO_DOWNGRADE_TO_SAVED)
