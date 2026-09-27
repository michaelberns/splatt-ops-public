"""
Tests for the small helpers in daemon/overdue.py: the priority
translation table and the "auto rescheduled" push counter.

The ladder is tested in tests/test_overdue_ladder.py, and the rule that
no decision reads a task's priority in tests/test_no_priority_logic.py.

Todoist's app shows P1 as the top priority, but its API numbers P1 as 4.
A reversed table would paint every task the engine touches the wrong
colour without any error, so the table is pinned down here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon.overdue import (
    ALERT_MARKER,
    RESCHEDULE_MARKER,
    PriorityError,
    api_priority,
    reschedule_count,
)


# the translation table
def test_priorities_are_written_the_way_todoist_shows_them():
    """p1..p4, as the app shows them, map to API values 4..1."""
    assert api_priority("p1") == 4
    assert api_priority("p2") == 3
    assert api_priority("p3") == 2
    assert api_priority("p4") == 1


def test_priority_is_case_insensitive_and_ignores_stray_spaces():
    assert api_priority("P1") == 4
    assert api_priority("  p2  ") == 3


def test_a_bare_number_is_refused_rather_than_guessed_at():
    """A bare number, or anything that is not p1 to p4, raises
    PriorityError, because a bare 1 reads as P1 to a person and means P4
    to the API."""
    for bad in (1, 4, "1", "5", "urgent", "", None, "pp1"):
        with pytest.raises(PriorityError):
            api_priority(bad)


# the push counter
def test_the_push_count_is_read_out_of_the_description():
    """reschedule_count counts "auto rescheduled" lines in the
    description. It is shown on the pinned line and never used to work
    out a task's age."""
    history = "%s once\n%s twice" % (RESCHEDULE_MARKER, RESCHEDULE_MARKER)
    assert reschedule_count({"description": history}) == 2
    assert reschedule_count({"description": ""}) == 0
    assert reschedule_count({}) == 0


def test_the_markers_are_distinct_strings():
    """Both markers are searched for in the same description, so neither
    may be a substring of the other."""
    assert ALERT_MARKER not in RESCHEDULE_MARKER
    assert RESCHEDULE_MARKER not in ALERT_MARKER


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
